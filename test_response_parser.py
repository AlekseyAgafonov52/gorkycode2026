import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from main import MAX_FILE_BYTES, app
from parse_responses import parse_directories
from response_parser import CandidateResponse, normalize_skill, parse_duration, parse_response, parse_response_file


def export(*, experience="Не указано в доступной версии резюме.", skills="VueJS\nНеизвестная технология",
           education="Уровень\nСреднее образование", about="Не указано в доступной версии резюме.",
           cover="Не указано в доступной версии резюме."):
    return (f"Кандидат №17\nРезюме: Разработчик\n\nСопроводительное письмо\n{cover}\n\n"
            f"Опыт работы\n{experience}\n\nНавыки\n{skills}\n\nЯзыки\nРодной\nРусский\n"
            f"Другие языки\nАнглийский — B2 — Средне-продвинутый\n\nОбо мне\n{about}\n\n"
            f"Образование\n{education}\n\nДополнительно\nЖелательное время в пути до работы :\n"
            "Не\u00a0имеет значения\nКомандировки:\nНе готов к командировкам\nГражданство: Россия\n"
            "Разрешение на работу:\nРоссия\nОпыт вождения:\nПрава категории B\nСамозанятость:\nЕсть\nМедкнижка:\nНет\n")


class ResponseParserTests(unittest.TestCase):
    def test_missing_is_null_not_zero(self):
        result = parse_response(export())
        self.assertEqual(result.candidate_id, 17)
        self.assertEqual(result.resume_title, "Разработчик")
        self.assertIsNone(result.cover_letter)
        self.assertIsNone(result.about_me)
        self.assertIsNone(result.experience_total_months)
        self.assertIsNone(result.similar_experience_months)
        self.assertEqual(result.work_experience, [])
        self.assertEqual(result.education, [])
        self.assertEqual(result.warnings, [])

    def test_explicit_no_experience(self):
        self.assertEqual(parse_response(export(experience="Без опыта работы")).experience_total_months, 0)

    def test_missing_skills_are_empty(self):
        result = parse_response(export(skills="Не указано в доступной версии резюме."))
        self.assertEqual(result.skills, [])
        self.assertEqual(result.original_sections.skills, "Не указано в доступной версии резюме.")

    def test_duration(self):
        for text, expected in [("5 лет 7 месяцев", 67), ("1 год 10 мес", 22), ("6 месяцев", 6),
                               ("2 года", 24), ("1 мес", 1), ("Не указано", None), ("Без опыта", 0)]:
            with self.subTest(text=text):
                self.assertEqual(parse_duration(text), expected)

    def test_levels_unknown_skills_and_versions(self):
        result = parse_response(export(skills="Продвинутый уровень\nVueJS\nVue 3\nСредний уровень\nDjango Rest Framework\nБазовый уровень\nНеизвестная технология\nУровень не указан\nGit"))
        self.assertEqual([(s.name, s.level) for s in result.skills], [
            ("Vue.js", "advanced"), ("Vue.js 3", "advanced"), ("Django REST Framework", "intermediate"),
            ("Неизвестная технология", "basic"), ("Git", None)])
        self.assertEqual(result.skills[0].original, "VueJS")
        self.assertEqual(normalize_skill("Docker-compose"), "Docker Compose")
        self.assertEqual(normalize_skill("Java"), "Java")
        self.assertEqual(normalize_skill("Vue3"), "Vue.js 3")

    def test_language_level_is_not_skill_level(self):
        result = parse_response(export(skills="Базовый уровень\nАнглийский язык"))
        self.assertEqual(result.skills[0].level, "basic")
        self.assertEqual(result.languages[0].level, "native")
        self.assertEqual(result.languages[1].name, "Английский")
        self.assertEqual(result.languages[1].level, "B2")

    def test_work_periods_and_overlap_are_not_summed(self):
        text = ("Опыт работы: 5 лет 7 месяцев\nОпыт в похожих должностях: 1 год\n"
                "Июн 2026 -\nсейчас\n5 мес\nОрганизация 1\nМосква\n"
                "Информационные технологии, системная интеграция, интернет\nВеб-разработчик\nРазработка UI\n"
                "Окт 2022 -\nСен 2025\n3 года\nОрганизация 2\nPython-разработчик\nРазработка API")
        result = parse_response(export(experience=text))
        self.assertEqual(result.experience_total_months, 67)
        self.assertEqual(result.similar_experience_months, 12)
        self.assertEqual(len(result.work_experience), 2)
        current, past = result.work_experience
        self.assertEqual(current.start_date, "2026-06")
        self.assertIsNone(current.end_date)
        self.assertTrue(current.is_current)
        self.assertEqual(current.duration_months, 5)
        self.assertEqual(current.position, "Веб-разработчик")
        self.assertEqual(current.description, "Разработка UI")
        self.assertEqual(len(current.employer_details), 2)
        self.assertFalse(past.is_current)
        self.assertEqual(past.end_date, "2025-09")
        self.assertEqual(past.description, "Разработка API")

    def test_unknown_position_retains_all_lines(self):
        result = parse_response(export(experience="Опыт работы: 1 месяц\nЯнв 2025 - Фев 2025\n1 мес\nОрганизация 1\nНеобычное занятие\nОписание"))
        job = result.work_experience[0]
        self.assertIsNone(job.position)
        self.assertIn("Необычное занятие", job.description)
        self.assertTrue(result.warnings)

    def test_nested_headings_stay_inside_about_me(self):
        about = "Рассказ\nОпыт работы\nБез коммерческого опыта\nНавыки\nPython\nОбразование\nКурсы\nДополнительно\n• Обучаемость"
        result = parse_response(export(about=about))
        self.assertEqual(result.about_me, about)
        self.assertEqual(result.education_level, "Среднее образование")
        self.assertEqual(result.warnings, [])
        self.assertEqual(len(result.skills), 2)

    def test_nested_headings_stay_inside_work_description(self):
        experience = ("Опыт работы: 2 года\nЯнв 2024 -\nДек 2025\n2 года\n"
                      "Организация 1\nBackend-разработчик\nОбо мне\nРассказ из описания работы\n"
                      "Навыки\nFigma Expert — прототипирование\nКонтакты\nКонец описания")
        result = parse_response(export(experience=experience, skills="Продвинутый уровень\nFigma", about="Настоящий раздел обо мне"))
        self.assertEqual([skill.name for skill in result.skills], ["Figma"])
        self.assertEqual(result.about_me, "Настоящий раздел обо мне")
        self.assertIn("Навыки\nFigma Expert", result.work_experience[0].description)
        self.assertEqual(result.warnings, [])

    def test_actual_employer_and_role_ending_in_dot(self):
        result = parse_response(export(experience="Опыт работы: 1 год\nЯнв 2024 - Дек 2024\n1 год\nООО Студия\nProduct designer Sr.\nРазработка интерфейсов"))
        self.assertEqual(result.work_experience[0].organization, "ООО Студия")
        self.assertEqual(result.work_experience[0].position, "Product designer Sr.")
        self.assertEqual(result.warnings, [])

    def test_design_and_qa_normalization(self):
        result = parse_response(export(skills="Средний уровень\nfigma\nPhotoshop\nCorel Draw\nJIRA\njmeter\nk8s\nНагрузочное тестирование"))
        self.assertEqual([s.name for s in result.skills], ["Figma", "Adobe Photoshop", "CorelDRAW", "Jira", "Apache JMeter", "Kubernetes", "Нагрузочное тестирование"])
        self.assertEqual(result.skills[2].original, "Corel Draw")

    def test_qa_and_design_job_titles(self):
        for title in ("QA Manual", "QA Lead", "3D-Проектировщик", "SYSTEM ADMINISTRATOR", "Mенеджер региональных продаж", "Ведущий инженер по тестированию."):
            with self.subTest(title=title):
                result = parse_response(export(experience=f"Опыт работы: 1 год\nЯнв 2024 - Дек 2024\n1 год\nОрганизация 1\nМосква\n{title}\nОписание работы"))
                self.assertEqual(result.work_experience[0].position, title)
                self.assertEqual(result.work_experience[0].description, "Описание работы")
                self.assertEqual(result.warnings, [])

    def test_education_inline_multiline_and_future_year(self):
        body = ("Уровень\nВысшее образование\nУниверситет, Информатика • 2031 • Высшее образование\n"
                "Колледж\nПрограммирование\n•\n2026\n•\nСреднее специальное образование")
        result = parse_response(export(education=body))
        self.assertEqual(len(result.education), 2)
        first, second = result.education
        self.assertEqual(first.institution, "Университет, Информатика")
        self.assertEqual(first.year, 2031)
        self.assertEqual(second.institution, "Колледж")
        self.assertEqual(second.details, ["Программирование"])
        self.assertEqual(second.year, 2026)
        self.assertEqual(result.warnings, [])

    def test_unparsed_education_is_preserved(self):
        result = parse_response(export(education="Уровень\nВысшее образование\nНеобычная запись без года"))
        self.assertIsNone(result.education[0].institution)
        self.assertEqual(result.education[0].original, "Необычная запись без года")
        self.assertTrue(result.warnings)

    def test_additional_fields_and_whitespace(self):
        result = parse_response(export())
        self.assertEqual(result.additional.commute, "Не\u00a0имеет значения")
        self.assertEqual(result.additional.citizenship, "Россия")
        self.assertEqual(result.additional.medical_book, "Нет")
        self.assertEqual(result.additional.self_employment, "Есть")

    def test_missing_title_is_not_inferred_from_skills(self):
        result = parse_response(export().replace("Резюме: Разработчик\n", ""))
        self.assertIsNone(result.resume_title)
        self.assertIn("Не найдено название резюме", result.warnings)

    def test_source_text_and_schema_round_trip(self):
        text = "\ufeff" + export(cover="Произвольное письмо\nВторая строка", education="Уровень\nВысшее образование\nУниверситет\n • \n2031\n • \nВысшее образование").replace("\n", "\r\n")
        result = parse_response(text)
        self.assertEqual(result.source_text, text)
        self.assertEqual(result.original_sections.cover_letter, "Произвольное письмо\r\nВторая строка")
        self.assertIn(result.education[0].original, result.original_sections.education)
        restored = CandidateResponse.model_validate_json(result.model_dump_json())
        self.assertEqual(restored, result)

    def test_auto_invitation_is_not_cover_letter(self):
        body = "Кандидат не прислал сопроводительное, потому что его пригласил ИИ-помощник. Посмотрите отчёт по кандидату в результатах его работы"
        result = parse_response(export(cover=body))
        self.assertIsNone(result.cover_letter)
        self.assertEqual(result.original_sections.cover_letter, body)

    def test_reject_empty_and_binary(self):
        for text in ("", " \n", "abc\x00"):
            with self.assertRaises(ValueError):
                parse_response(text)

    def test_file_encodings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "отклик.txt"
            for encoding in ("utf-8-sig", "utf-16", "cp1251"):
                path.write_bytes(export().encode(encoding))
                self.assertEqual(parse_response_file(path).candidate_id, 17)
            with self.assertRaises(ValueError):
                parse_response_file(path.with_suffix(".pdf"))

    def test_batch_preserves_folder_identity_and_reports_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("Frontend", "Python"):
                (root / name).mkdir()
                (root / name / "отклик 17.txt").write_text(export(), encoding="utf-8")
                (root / name / "Вакансия.txt").write_text("Not a response", encoding="utf-8")
            (root / "Python" / "отклик 18.txt").write_bytes(b"")
            result = parse_directories([root / "Frontend", root / "Python"], root / "out")
            self.assertEqual((result["files_total"], result["parsed"], result["failed"]), (3, 2, 1))
            for name in ("Frontend", "Python"):
                parsed = json.loads((root / "out" / name / "отклик 17.json").read_text(encoding="utf-8"))
                self.assertEqual(parsed["candidate_id"], 17)
            self.assertTrue((root / "out" / "_schema.json").exists())

    def test_batch_rejects_colliding_folders(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sources = [root / name / "same" for name in ("a", "b")]
            for source in sources:
                source.mkdir(parents=True)
                (source / "отклик 1.txt").write_text(export(), encoding="utf-8")
            with self.assertRaises(ValueError):
                parse_directories(sources, root / "out")

    def test_api_and_validation(self):
        with TestClient(app) as client:
            response = client.post("/api/v1/parse/response", files={"file": ("отклик.txt", export().encode("utf-8"))})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(CandidateResponse.model_validate(response.json()).candidate_id, 17)
            self.assertEqual(set(response.json()), set(CandidateResponse.model_fields))
            for name, body, status in [("x.pdf", b"a", 415), ("x.txt", b"", 422), ("x.txt", b"\x00", 422),
                                       ("x.txt", b"a" * (MAX_FILE_BYTES + 1), 413)]:
                self.assertEqual(client.post("/api/v1/parse/response", files={"file": (name, body)}).status_code, status)
            self.assertEqual(client.post("/api/v1/parse/response").status_code, 422)


if __name__ == "__main__":
    unittest.main()
