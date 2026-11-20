import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from docx import Document

from check_candidate import PROFILES, check_candidate, main
from question_generator import ClarificationResult, QuestionGenerationError


class CheckCandidateTests(unittest.TestCase):
    def make_profile(self, root: Path, profile: str, number: int = 1) -> Path:
        folder = root / PROFILES[profile]
        folder.mkdir(parents=True)
        document = Document()
        document.add_paragraph("ОБЯЗАННОСТИ")
        document.add_paragraph("Чтение кода на Python. Знание SQL. Разработка документации.")
        document.add_paragraph("КВАЛИФИКАЦИОННЫЕ ТРЕБОВАНИЯ")
        document.add_paragraph("Секретный устаревший пункт, который не должен попасть в запрос")
        document.save(str(folder / "job_instruction.docx"))
        (folder / "job_vacancy.txt").write_text("Разработчик\nТребования:\nPython и SQL", encoding="utf-8")
        (folder / f"отклик {number}.json").write_text(json.dumps({"candidate_id": number, "skills": ["Git"]}), encoding="utf-8")
        return folder

    def result(self):
        return ClarificationResult.model_validate({"candidate_id": "1", "questions": [
            {"requirement": requirement, "question": question, "reason": "Конкретный аспект не раскрыт"}
            for requirement, question in [
                ("Чтение кода на Python.", "Как вы анализировали чужой код на Python?"),
                ("Знание SQL.", "Какие SQL-запросы вам приходилось писать?"),
                ("Разработка документации.", "Какую техническую документацию вы разрабатывали?"),
            ]]})

    def test_prepare_only_does_not_call_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = self.make_profile(root, "frontend")
            with patch("check_candidate.generate_questions") as generate:
                output = check_candidate("frontend", 1, root=root, prepare_only=True)
            generate.assert_not_called()
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data["candidate"]["candidate_id"], 1)
            self.assertIn("Чтение кода", data["job_instruction_duties"])
            self.assertIn("Python и SQL", data["vacancy"])
            self.assertNotIn("job_instruction", data)
            self.assertNotIn("Секретный устаревший пункт", json.dumps(data, ensure_ascii=False))
            self.assertEqual(data["prompt_profile"], "frontend")
            self.assertEqual(data["domain"], "it")
            preview = json.loads((folder / "questions" / "prompts_1.json").read_text(encoding="utf-8"))
            self.assertEqual(preview["prompt_profile"], "frontend")
            self.assertIn("Frontend-разработчик", preview["generation"])
            self.assertFalse((folder / "questions" / "отклик 1.json").exists())

    def test_result_saved_as_json_and_readable_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_profile(root, "qa")
            with patch("check_candidate.generate_questions", return_value=self.result()) as generate:
                output = check_candidate("qa", 1, root=root)
            self.assertEqual(generate.call_count, 1)
            self.assertEqual(generate.call_args.kwargs["prompt_profile"], "qa")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), self.result().model_dump())
            self.assertIn("Как вы анализировали", output.with_suffix(".md").read_text(encoding="utf-8"))

    def test_wrong_candidate_and_missing_instruction_never_call_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = self.make_profile(root, "frontend")
            (folder / "отклик 1.json").write_text('{"candidate_id": 2}', encoding="utf-8")
            with patch("check_candidate.generate_questions") as generate:
                with self.assertRaisesRegex(ValueError, "не совпадает"):
                    check_candidate("frontend", 1, root=root)
                with self.assertRaises(ValueError):
                    check_candidate("qa", 1, root=root)
            generate.assert_not_called()

    def test_all_profiles_can_be_prepared_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for profile in PROFILES:
                self.make_profile(root, profile)
            with redirect_stdout(io.StringIO()), patch("check_candidate.generate_questions") as generate:
                exit_code = main(["--all", "--prepare-only", "--root", str(root)])
            self.assertEqual(exit_code, 0)
            self.assertEqual(len(list(root.glob("*/questions/input_1.json"))), 5)
            self.assertEqual(len(list(root.glob("*/questions/prompts_1.json"))), 5)
            generate.assert_not_called()

    def test_api_failure_does_not_create_success_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = self.make_profile(root, "frontend")
            with redirect_stdout(io.StringIO()), patch("check_candidate.generate_questions", side_effect=QuestionGenerationError("test error")):
                self.assertEqual(main(["frontend", "1", "--root", str(root)]), 1)
            self.assertFalse((folder / "questions").exists())


if __name__ == "__main__":
    unittest.main()
