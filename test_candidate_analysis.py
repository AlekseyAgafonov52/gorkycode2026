import unittest
from unittest.mock import patch

from candidate_analysis import extract_vacancy_skills, match, parse_resume, resume_from_json
from contract_models import Contacts, PoolQuestion, ResumeData, ResumeSkill
from question_pool import pick_questions, validate_pool


class CandidateAnalysisTests(unittest.TestCase):
    def test_tags_stop_before_address_and_aliases_collapse(self):
        text = "Frontend developer\nТребования:\nPython\nКлючевые навыки\nVueJS\nVue 3\nJavaScript\nГде предстоит работать\nМосква, 6"
        self.assertEqual(extract_vacancy_skills(text), ["Vue.js", "JavaScript"])

    def test_requirements_fallback_without_tags(self):
        self.assertEqual(extract_vacancy_skills("Python developer\nТребования:\n- Python\n- SQL"), ["Python", "SQL"])

    def test_match_counts_basic_and_indirect_but_not_substrings(self):
        resume = resume_from_json({"candidate_name": "Демо кандидат", "contacts": {"email": "demo@example.com"},
            "skills": [{"name": "VueJS", "level": "basic"}, {"name": "SQL", "level": "advanced"}],
            "source_text": "В проектах использовал JavaScript и Python."})
        result = match(resume, ["Vue 3", "SQL", "Python", "Java", "Git"])
        self.assertEqual(result.match_share, 3 / 5)
        self.assertEqual(result.match_status, "yellow")
        self.assertEqual([s.relevance for s in result.skills], ["red", "red", "yellow", "yellow", "green"])
        for skill in result.skills:
            self.assertEqual(len(skill.questions), int(skill.relevance == "red"))
        self.assertEqual(len(result.summary), 3)
        self.assertIn("2 из 5", result.summary[0])

    def test_identity_and_contacts_do_not_affect_skill_matching(self):
        for name, contacts in ((None, Contacts()), (None, Contacts(email="demo@example.com")),
                               ("Демо", Contacts()), ("Демо", Contacts(other=["https://example.com"]))):
            resume = ResumeData(candidate_name=name, contacts=contacts, skills=[ResumeSkill(name="SQL")])
            self.assertEqual(match(resume, ["SQL"]).match_status, "green")
            self.assertEqual(match(resume, ["SQL", "Git"]).match_status, "yellow")
            self.assertEqual(match(resume, ["SQL"]).match_share, 1)
        missing = {item.field for item in match(ResumeData(), ["SQL"]).missing_data}
        self.assertTrue({"candidate_name", "phone", "email", "telegram"} <= missing)

    def test_threshold_and_unknown_level(self):
        resume = ResumeData(candidate_name="Демо", contacts=Contacts(telegram="@demo"), skills=[ResumeSkill(name="SQL")])
        self.assertEqual(match(resume, ["SQL"]).match_status, "green")
        self.assertIsNone(match(resume, ["SQL"]).skills[0].resume_level)
        self.assertEqual(match(resume, []).match_share, 0)
        resume.candidate_name = "  "
        self.assertEqual(match(resume, ["SQL"]).match_status, "green")
        self.assertIsNone(Contacts(email="  ").email)

    def test_parse_extracts_only_explicit_identity_and_contacts(self):
        raw = "Кандидат №1\r\nФИО: Демо Кандидат\r\nРезюме: Frontend developer\r\nemail: demo@example.com / tg: @demo_user\r\nТелефон: +7 (999) 123-45-67\r\n"
        resume = parse_resume(raw)
        self.assertEqual(resume.candidate_name, "Демо Кандидат")
        self.assertEqual(resume.contacts.email, "demo@example.com")
        self.assertEqual(resume.contacts.telegram, "@demo_user")
        self.assertEqual(resume.contacts.phone, "+7 (999) 123-45-67")
        self.assertEqual(resume._source_text, raw)
        self.assertIsNone(parse_resume("Кандидат №2\nРезюме: Разработчик\nМеня зовут Александр").candidate_name)

    def test_ready_json_does_not_reparse_resume(self):
        with patch("candidate_analysis.parse_response", side_effect=AssertionError("must not parse")):
            resume = resume_from_json({"skills": [{"name": "Vue 3", "level": "advanced", "original": "VueJS"}]})
        self.assertEqual(resume.skills[0].name, "Vue.js")
        self.assertEqual(set(resume.model_dump()), {"candidate_name", "contacts", "title", "experience_months", "skills", "education", "about"})

    def test_pool_validation_and_selection_without_network(self):
        pool = [PoolQuestion(skill=skill, question=f"{skill}: {i}?", expected_answer="Конкретный ответ")
                for skill in ("Vue.js", "SQL") for i in range(2)]
        validate_pool(pool, ["Vue.js", "SQL"])
        resume = ResumeData(skills=[ResumeSkill(name="VueJS")])
        self.assertEqual([q.skill for q in pick_questions(pool, resume, None)], ["Vue.js"])
        self.assertEqual(len(pick_questions(pool, resume, "high")), 2)
        with self.assertRaises(ValueError):
            validate_pool(pool[:1], ["Vue.js"])


if __name__ == "__main__":
    unittest.main()
