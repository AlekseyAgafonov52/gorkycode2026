import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_sources import extract_instruction_duties, load_vacancy_text
from question_generator import QuestionGenerationError, generate_questions, prepare_input
import test_question_generator as generator_fixtures
from test_question_generator import CANDIDATE, INSTRUCTION, VACANCY, completion, draft, review


class JobSourceTests(unittest.TestCase):
    def test_toc_and_unrelated_sections_are_excluded(self):
        text = ("2.\tОБЯЗАННОСТИ\t3\n3.\tПРАВА\t4\nОБЩИЕ ПОЛОЖЕНИЯ\nОбщие правила\n"
                "ОБЯЗАННОСТИ\nРазрабатывать UI.\nРаботать с API.\nПРАВА\nПраво на отпуск\n"
                "КВАЛИФИКАЦИОННЫЕ ТРЕБОВАНИЯ\nТребуется медицинское образование")
        self.assertEqual(extract_instruction_duties(text), "ОБЯЗАННОСТИ\nРазрабатывать UI.\nРаботать с API.")

    def test_numbered_heading_and_sentence_with_rights(self):
        text = "2. ДОЛЖНОСТНЫЕ ОБЯЗАННОСТИ:\nСоблюдать права пользователей.\n3. Ответственность\nСкрытый раздел"
        self.assertEqual(extract_instruction_duties(text), "2. ДОЛЖНОСТНЫЕ ОБЯЗАННОСТИ:\nСоблюдать права пользователей.")

    def test_missing_empty_or_ambiguous_section_never_uses_full_document(self):
        for text in ("Нет подходящего заголовка", "ОБЯЗАННОСТИ\nПРАВА\nТекст",
                     "ОБЯЗАННОСТИ\nЗадача 1\nПРАВА\nОБЯЗАННОСТИ\nЗадача 2"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                extract_instruction_duties(text)

    def test_vacancy_formats_preserve_source_and_preferred_requirements(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = "Разработчик\nТребования:\nVueJS\nБудет плюсом:\nNuxt"
            for encoding in ("utf-8-sig", "utf-16", "cp1251"):
                path = root / "vacancy.txt"
                path.write_bytes(text.encode(encoding))
                self.assertEqual(load_vacancy_text(path), text)
            path = root / "vacancy.json"
            path.write_text(json.dumps({"source_text": text, "required_skills": ["DO NOT DUPLICATE"]}), encoding="utf-8")
            self.assertEqual(load_vacancy_text(path), text)
            path.write_text(json.dumps({"title": "Developer", "required_skills": ["Vue.js"], "preferred_skills": ["Nuxt"]}), encoding="utf-8")
            self.assertIn("Желательные навыки:\nNuxt", load_vacancy_text(path))

    def test_invalid_vacancy_data_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vacancy.json"
            for value in ([], {}, {"source_text": 10}, {"required_skills": [False]}, {"experience_min_years": -1}):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.subTest(value=value), self.assertRaises(ValueError):
                    load_vacancy_text(path)

    def test_full_instruction_never_reaches_generation_or_review(self):
        source = INSTRUCTION + "\nКВАЛИФИКАЦИОННЫЕ ТРЕБОВАНИЯ\nUNRELATED_QUALIFICATION\nПРАВА\nUNRELATED_RIGHTS"
        helper = generator_fixtures.QuestionGeneratorTests()
        _, requests = helper.call_sequence([completion(draft()), completion(review())], instruction=source)
        for request in requests:
            data = json.loads(json.loads(request.content)["messages"][1]["text"])
            self.assertEqual(data["vacancy"], VACANCY)
            self.assertIn("Чтение кода", data["job_instruction_duties"])
            self.assertNotIn("job_instruction", data)
            self.assertNotIn("UNRELATED_", json.dumps(data))

    def test_quotes_from_vacancy_are_supported_and_other_di_sections_are_not(self):
        helper = generator_fixtures.QuestionGeneratorTests()
        value = draft()
        value["questions"][0]["requirement"] = "Разработка Vue-компонентов"
        result, _ = helper.call_sequence([completion(value), completion(review())], vacancy="Разработка Vue-компонентов")
        self.assertEqual(result.questions[0].requirement, "Разработка Vue-компонентов")
        value["questions"][0]["requirement"] = "Требуется медицинское образование"
        with self.assertRaises(QuestionGenerationError):
            helper.call_sequence([completion(value)], attempts=1,
                                 instruction=INSTRUCTION + "\nКВАЛИФИКАЦИОННЫЕ ТРЕБОВАНИЯ\nТребуется медицинское образование")

    def test_missing_vacancy_and_missing_duties_fail_before_api(self):
        for vacancy, source in ((" ", INSTRUCTION), (VACANCY, "Весь документ без заголовков")):
            with patch("question_generator.YandexGPTClient") as client:
                with self.assertRaises(ValueError):
                    generate_questions(source, CANDIDATE, vacancy_text=vacancy)
                client.assert_not_called()
        with self.assertRaises(ValueError):
            prepare_input(INSTRUCTION, CANDIDATE, vacancy_text="")


if __name__ == "__main__":
    unittest.main()
