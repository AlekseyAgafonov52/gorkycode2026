import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from check_candidate import PROFILES
from question_generator import YandexGPTClient, YandexGPTSettings, generate_questions
from question_prompts import PROMPT_PROFILES, PROMPT_ROOT, build_prompts
from test_question_generator import CANDIDATE, INSTRUCTION, VACANCY, completion, draft, review


class QuestionPromptTests(unittest.TestCase):
    def make_prompts(self, root: Path):
        (root / "it").mkdir()
        for name, text in {"generation.txt": "COMMON GENERATION", "review.txt": "COMMON REVIEW",
                           "it/common.txt": "IT CONTEXT", "it/frontend.txt": "FRONTEND ONLY",
                           "it/qa.txt": "QA ONLY"}.items():
            (root / name).write_text(text, encoding="utf-8")

    def test_only_selected_role_is_composed_for_both_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_prompts(root)
            prompts = build_prompts("frontend", root=root)
            self.assertEqual(prompts.domain, "it")
            for text in (prompts.generation, prompts.review):
                self.assertIn("IT CONTEXT", text)
                self.assertIn("FRONTEND ONLY", text)
                self.assertNotIn("QA ONLY", text)
            self.assertTrue(prompts.generation.startswith("COMMON GENERATION"))
            self.assertTrue(prompts.review.startswith("COMMON REVIEW"))

    def test_general_profile_does_not_receive_it_assumptions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_prompts(root)
            prompts = build_prompts(root=root)
            self.assertEqual(prompts.profile, "general")
            self.assertEqual(prompts.generation, "COMMON GENERATION")
            self.assertEqual(prompts.review, "COMMON REVIEW")

    def test_edits_take_effect_without_restarting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_prompts(root)
            first = build_prompts("frontend", root=root)
            (root / "it" / "frontend.txt").write_text("CHANGED ROLE", encoding="utf-8-sig")
            second = build_prompts("frontend", root=root)
            self.assertNotEqual(first.generation, second.generation)
            self.assertIn("CHANGED ROLE", second.review)

    def test_unknown_empty_and_missing_profiles_fail_explicitly(self):
        with self.assertRaises(ValueError):
            build_prompts("../../other")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_prompts(root)
            with self.assertRaisesRegex(ValueError, "прочитать"):
                build_prompts("designer", root=root)
            (root / "it" / "frontend.txt").write_text(" \n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "пуст"):
                build_prompts("frontend", root=root)

    def test_all_five_existing_vacancies_have_role_profiles(self):
        self.assertEqual(set(PROFILES), set(PROMPT_PROFILES))
        self.assertTrue(PROMPT_ROOT.is_absolute())
        generations = set()
        for profile in PROFILES:
            prompts = build_prompts(profile)
            self.assertEqual(prompts.domain, "it")
            self.assertIn(PROMPT_PROFILES[profile].label, prompts.generation)
            generations.add(prompts.generation)
        self.assertEqual(len(generations), 5)

    def test_profile_is_used_in_generation_and_review_requests(self):
        requests = []
        responses = [completion(draft()), completion(review())]

        def handler(request):
            requests.append(json.loads(request.content))
            return responses.pop(0)

        settings = YandexGPTSettings.model_validate({"folder_id": "folder", "api_key": "test-secret"})
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            client = YandexGPTClient(settings, http_client=http)
            result = generate_questions(INSTRUCTION, CANDIDATE, vacancy_text=VACANCY, client=client, prompt_profile="qa")
        self.assertEqual(len(result.questions), 3)
        prompts = build_prompts("qa")
        self.assertEqual(requests[0]["messages"][0]["text"], prompts.generation)
        self.assertEqual(requests[1]["messages"][0]["text"], prompts.review)
        self.assertEqual(len(requests), 2)

    def test_invalid_profile_is_rejected_before_client_creation(self):
        with patch("question_generator.YandexGPTClient") as client:
            with self.assertRaises(ValueError):
                generate_questions(INSTRUCTION, CANDIDATE, vacancy_text=VACANCY, prompt_profile="unknown")
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
