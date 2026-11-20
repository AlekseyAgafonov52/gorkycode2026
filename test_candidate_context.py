import json
import tempfile
import unittest
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch

import httpx
from docx import Document

from candidate_context import extract_candidate_skills
from question_generator import generate_questions, main as generator_main, prepare_input
import test_question_generator as generator_fixtures


def candidate() -> dict:
    return {"candidate_id": 1, "skills": [
                {"name": "SQL", "level": "advanced", "original": "SQL"},
                {"name": "Vue.js", "level": "basic", "original": "VueJS"},
                {"name": "CustomTool", "level": None, "original": "CustomTool"}],
            "work_experience": [{"description": "PRIVATE_WORK RabbitMQ"}],
            "education": [{"institution": "PRIVATE_EDUCATION"}],
            "languages": ["PRIVATE_LANGUAGE"], "about_me": "PRIVATE_ABOUT",
            "source_text": "PRIVATE_SOURCE Python",
            "original_sections": {"skills": "PRIVATE_SECTION Redis"},
            "cover_letter": "PRIVATE_LETTER", "extra_field": "PRIVATE_EXTRA"}


class CandidateContextTests(unittest.TestCase):
    def test_skills_extracted_verbatim_without_other_fields(self):
        data = candidate()
        self.assertEqual(extract_candidate_skills(data), data["skills"])
        prepared = prepare_input(generator_fixtures.INSTRUCTION, data,
                                 vacancy_text=generator_fixtures.VACANCY)
        self.assertEqual(prepared["candidate"], {"skills": data["skills"]})
        self.assertEqual(prepared["candidate_id"], "1")
        self.assertNotIn("PRIVATE_", json.dumps(prepared))

    def test_input_is_unchanged_and_extracted_array_is_independent(self):
        data = candidate()
        original = deepcopy(data)
        skills = extract_candidate_skills(data)
        self.assertEqual(data, original)
        first = skills[0]
        assert isinstance(first, dict)
        first["name"] = "Changed"
        skills.append("New skill")
        self.assertEqual(data, original)

    def test_missing_null_and_empty_skills_do_not_fall_back_to_resume(self):
        cases: list[dict] = [
            {"candidate_id": 1, "source_text": "Python", "original_sections": {"skills": "SQL"}},
            {"candidate_id": 1, "skills": None, "about_me": "SQL"},
            {"candidate_id": 1, "skills": [], "work_experience": [{"description": "SQL"}]},
        ]
        for data in cases:
            with self.subTest(data=data):
                self.assertEqual(extract_candidate_skills(data), [])
                prepared = prepare_input(generator_fixtures.INSTRUCTION, data,
                                         vacancy_text=generator_fixtures.VACANCY)
                self.assertEqual(prepared["candidate"], {"skills": []})

    def test_non_array_skills_fail_before_api(self):
        for value in ("SQL", {"name": "SQL"}, 1, False):
            with self.subTest(value=value), patch("question_generator.YandexGPTClient") as client:
                with self.assertRaisesRegex(ValueError, "skills.*массив"):
                    generate_questions(generator_fixtures.INSTRUCTION,
                                       {"candidate_id": 1, "skills": value},
                                       vacancy_text=generator_fixtures.VACANCY)
                client.assert_not_called()

    def test_both_generation_and_review_receive_only_skills_and_identifier(self):
        data = candidate()
        case = generator_fixtures.QuestionGeneratorTests()
        result, requests = case.call_sequence([
            generator_fixtures.completion(generator_fixtures.draft()),
            generator_fixtures.completion(generator_fixtures.review())], candidate=data)
        self.assertEqual(result.candidate_id, "1")
        self.assertEqual(len(requests), 2)
        for request in requests:
            payload = json.loads(json.loads(request.content)["messages"][1]["text"])
            self.assertEqual(payload["candidate"], {"skills": data["skills"]})
            self.assertEqual(payload["candidate_id"], "1")
            self.assertNotIn("PRIVATE_", request.content.decode())


    def test_cli_generation_keeps_identifier_and_sends_only_skills(self):
        data = candidate()
        case = generator_fixtures.QuestionGeneratorTests()
        replies = [generator_fixtures.completion(generator_fixtures.draft()),
                   generator_fixtures.completion(generator_fixtures.review())]
        requests = []

        def handler(request):
            requests.append(request)
            return replies.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = Document()
            document.add_paragraph(generator_fixtures.INSTRUCTION)
            document.save(str(root / "instruction.docx"))
            (root / "candidate.json").write_text(json.dumps(data), encoding="utf-8")
            (root / "vacancy.txt").write_text(generator_fixtures.VACANCY, encoding="utf-8")
            args = ["question_generator.py", str(root / "instruction.docx"),
                    str(root / "candidate.json"), "--vacancy", str(root / "vacancy.txt"),
                    "--output", str(root / "result.json")]
            with httpx.Client(transport=httpx.MockTransport(handler)) as http:
                client = generator_fixtures.YandexGPTClient(case.settings(), http_client=http)
                with patch("question_generator.YandexGPTClient", return_value=client), \
                        patch("question_generator.YandexGPTSettings.from_env", return_value=case.settings()), \
                        patch("sys.argv", args):
                    self.assertEqual(generator_main(), 0)
            self.assertEqual(json.loads((root / "result.json").read_text(encoding="utf-8"))["candidate_id"], "1")
        self.assertEqual(len(requests), 2)
        for request in requests:
            payload = json.loads(json.loads(request.content)["messages"][1]["text"])
            self.assertEqual(payload["candidate"], {"skills": data["skills"]})
            self.assertEqual(payload["candidate_id"], "1")


if __name__ == "__main__":
    unittest.main()
