import unittest

from fastapi.testclient import TestClient

from main import MAX_FILE_BYTES, app
from vacancy_parser import Vacancy, decode_txt, parse_vacancy


class ParserTests(unittest.TestCase):
    def test_empty_and_unknown(self):
        with self.assertRaises(ValueError):
            parse_vacancy(" \n")
        result = parse_vacancy("Разработчик")
        self.assertIsNone(result.organization)
        self.assertIsNone(result.experience_min_years)
        self.assertEqual(result.required_skills, [])
        self.assertEqual(set(result.model_dump()), set(Vacancy.model_fields))

    def test_sections_and_originals(self):
        text = "Должность: Frontend\nОбязанности:\nРазработка UI\nТребования:\nОпыт с VueJS от 6 месяцев\nБудет преимуществом:\nNuxt\nУсловия:\nБесплатное обучение Python"
        result = parse_vacancy(text)
        self.assertEqual(result.title, "Frontend")
        self.assertEqual(result.required_skills, ["Vue.js"])
        self.assertEqual(result.preferred_skills, ["Nuxt"])
        self.assertEqual(result.experience_min_years, .5)
        self.assertIn("Опыт с VueJS от 6 месяцев", result.original_requirements.required)
        self.assertEqual(result.source_text, text)

    def test_education_and_format(self):
        result = parse_vacancy("Дизайнер\nОпыт не требуется\nТребования:\nВысшее образование\nУсловия:\nРабота в офисе, возможен гибрид")
        self.assertEqual(result.education, "Высшее образование")
        self.assertEqual(result.experience_min_years, 0)
        self.assertEqual(result.work_format, "hybrid")

    def test_encodings(self):
        for encoding in ("utf-8-sig", "utf-16", "cp1251"):
            self.assertEqual(decode_txt("Требования".encode(encoding)), "Требования")

    def test_company_inline_after_heading(self):
        result = parse_vacancy('Разработчик\nУсловия:\nО компании АНО "Центр" — описание')
        self.assertEqual(result.organization, 'АНО "Центр"')

    def test_api(self):
        with TestClient(app) as client:
            for text in ("Python Developer", "Требования:\nPython"):
                response = client.post("/api/v1/parse/vacancy", files={"file": ("job.txt", text.encode())})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(set(response.json()), set(Vacancy.model_fields))
            for name, data, status in (("job.pdf", b"data", 415), ("job.txt", b"", 422), ("job.txt", b"\x00", 422), ("job.txt", b"x" * (MAX_FILE_BYTES + 1), 413)):
                response = client.post("/api/v1/parse/vacancy", files={"file": (name, data)})
                self.assertEqual(response.status_code, status)


if __name__ == "__main__":
    unittest.main()
