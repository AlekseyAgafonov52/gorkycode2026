"""Contract T1–T13 and durable processing against an isolated PostgreSQL schema."""
import json
import os
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.schema import CreateSchema, DropSchema

from contract_models import ContractReport, PoolQuestion
from hr_api.import_data import import_folder
from hr_api.demo_data import create_demo
from hr_api.schemas import CriteriaPatch
from hr_api.service import HRService
from main import create_app
from storage.database import DatabaseSettings, make_engine, make_session_factory
from storage.crud import Repository
from storage.models import MatchResult, Report, Response, Vacancy
from storage.schemas import ResponseUpdate
from test_storage import migration_config


class FakePoolBuilder:
    def __init__(self):
        self.calls = []
        self.callback: Callable[[], None] | None = None
        self.fail = False

    def __call__(self, vacancy_text: str, vacancy_skills: list[str]) -> list[PoolQuestion]:
        self.calls.append((vacancy_text, vacancy_skills))
        if self.callback:
            self.callback()
        if self.fail:
            raise RuntimeError("PRIVATE_PROVIDER_ERROR")
        return [PoolQuestion(skill=skill, question=f"Как работает {skill}: пример {index}?",
                             expected_answer="Конкретное объяснение механизма и пример применения.")
                for skill in vacancy_skills for index in range(2)]


class HRContractTests(unittest.TestCase):
    def test_all_thirteen_routes_in_openapi_without_database_access(self):
        paths = create_app().openapi()["paths"]
        self.assertEqual(sum(len(methods) for methods in paths.values()), 13)
        for path in ("/api/v1/vacancies", "/api/v1/vacancies/{vacancy_id}/criteria",
                     "/api/v1/vacancies/{vacancy_id}/match", "/api/v1/tasks/{task_id}",
                     "/api/v1/responses/{response_id}/report", "/api/v1/responses/{response_id}/questions/generate"):
            self.assertIn(path, paths)

    def test_cors_allows_local_frontend_and_uniform_errors(self):
        with TestClient(create_app()) as client:
            response = client.options("/api/v1/vacancies", headers={"Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET"})
            self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:5173")
            response = client.get("/does-not-exist")
            self.assertEqual(response.status_code, 404)
            self.assertEqual(set(response.json()), {"error"})
            self.assertEqual(response.json()["error"]["code"], "not_found")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL for HR API PostgreSQL tests")
class HRAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_engine = make_engine(DatabaseSettings.from_env(None, variable="TEST_DATABASE_URL"))
        cls.schema = "hr_api_" + uuid4().hex
        with cls.base_engine.begin() as connection:
            connection.execute(CreateSchema(cls.schema))
        cls.engine = cls.base_engine.execution_options(schema_translate_map={None: cls.schema})
        with cls.engine.begin() as connection:
            command.upgrade(migration_config(connection, cls.schema), "head")
        cls.factory = make_session_factory(cls.engine)

    @classmethod
    def tearDownClass(cls):
        assert cls.schema.startswith("hr_api_") and len(cls.schema) == 39
        with cls.base_engine.begin() as connection:
            connection.execute(DropSchema(cls.schema, cascade=True))
        cls.base_engine.dispose()

    def setUp(self):
        self.provider = FakePoolBuilder()
        self.service = HRService(self.factory, self.provider)
        self.client = TestClient(create_app(self.service))
        self.directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.directory.name)
        self.key = "contract-synthetic-test"
        self.vacancy_id = uuid5(NAMESPACE_URL, "hr-platform/vacancies/" + self.key)
        self.ids = [uuid5(NAMESPACE_URL, f"hr-platform/responses/{self.key}/{number}") for number in (1, 2, 3)]
        (self.folder / "Вакансия.txt").write_text(
            "Frontend developer\nКлючевые навыки\nJavaScript\nVueJS\nSQL\nGit", encoding="utf-8")
        self.raw = {}
        for number, skills in ((1, ["JavaScript", "Vue 3", "SQL", "Git"]),
                               (2, ["JavaScript"]), (3, ["VueJS", "SQL"])):
            source = f"Кандидат №{number}\r\nРезюме: Frontend developer\r\nPRIVATE_RESUME_TEXT\r\n"
            data = {"candidate_id": number, "skills": [{"name": name, "level": "advanced"} for name in skills],
                    "source_text": source}
            if number != 2:
                # Explicitly synthetic identities; original organizer data is never modified.
                data.update(candidate_name=f"Демо Кандидат {number}", contacts={"email": f"demo{number}@example.com"})
            self.raw[number] = source
            (self.folder / f"отклик {number}.json").write_text(json.dumps(data), encoding="utf-8")
        self.first_import = import_folder(self.factory, self.folder, self.key)

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()
        with self.factory.begin() as session:
            session.execute(delete(Vacancy))

    def prepare(self):
        return self.service.preparer.prepare_vacancy(self.vacancy_id)

    def report_url(self, number=1):
        return f"/api/v1/responses/{self.ids[number-1]}/report"

    def responses_url(self):
        return f"/api/v1/vacancies/{self.vacancy_id}/responses"

    def test_t1_t2_idempotent_import_and_list(self):
        self.assertEqual(self.first_import, {"vacancies_created": 1, "responses_created": 3})
        self.assertEqual(import_folder(self.factory, self.folder, self.key), {"vacancies_created": 0, "responses_created": 0})
        self.assertEqual(self.prepare(), 3)
        original = self.client.get(self.report_url()).json()
        self.assertEqual(self.prepare(), 0)
        self.assertEqual(self.client.get(self.report_url()).json(), original)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.client.get("/api/v1/vacancies").json(),
            [{"id": str(self.vacancy_id), "title": "Frontend developer", "responses_count": 3}])

    def test_t3_t4_t5_colors_shares_and_order(self):
        self.prepare()
        rows = self.client.get(self.responses_url()).json()
        self.assertEqual([r["match_status"] for r in rows], ["green", "yellow", "red"])
        self.assertEqual([r["match_share"] for r in rows], [1, .5, .25])
        self.assertEqual(rows[-1]["id"], str(self.ids[1]))
        self.assertIsNone(rows[-1]["candidate_name"])
        self.assertEqual(set(rows[0]), {"id", "candidate_name", "match_status", "match_share", "short_summary", "workflow_status"})
        # Even a perfect skill share cannot lift an anonymous candidate above yellow.
        with self.factory.begin() as session:
            row = session.get(Response, self.ids[1])
            assert row is not None
            row.source_json = {**row.source_json, "skills": ["JavaScript", "Vue 3", "SQL", "Git"]}
        self.prepare()
        rows = self.client.get(self.responses_url()).json()
        self.assertEqual([r["match_status"] for r in rows], ["green", "yellow", "red"])
        self.assertEqual(rows[-1]["match_share"], 1)

    def test_t6_t7_t8_t9_t10_complete_cached_report(self):
        self.prepare()
        first = self.client.get(self.report_url(3))
        self.assertEqual(first.status_code, 200)
        report = first.json()
        ContractReport.model_validate(report)
        self.assertEqual(set(report), set(ContractReport.model_fields))
        self.assertEqual(report["source_text"], self.raw[3])
        self.assertEqual(len(report["summary"]), 3)
        self.assertIn("2 из 4", report["summary"][0])
        self.assertIsNone(report["ai_text_level"])
        self.assertIsNone(report["short_summary"])
        self.assertEqual([s["relevance"] for s in report["skills"]], ["red", "red", "green", "green"])
        self.assertEqual({s["skill"] for s in report["skills"]}, {"JavaScript", "Vue.js", "SQL", "Git"})
        for skill in report["skills"]:
            question = skill["questions"][0]
            self.assertEqual(question["kind"], "missing" if skill["relevance"] == "red" else "stack")
            self.assertEqual(question["expected_answer"] is None, skill["relevance"] == "red")
        self.assertEqual(self.client.get(self.report_url(3)).json(), report)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertNotIn("PRIVATE", json.dumps(self.provider.calls))
        with self.factory.begin() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Report)), 3)
            self.assertEqual(session.scalar(select(func.count()).select_from(MatchResult)), 3)

    def test_t11_t13_not_found_and_invalid_enum(self):
        for path in ("/api/v1/responses/nope/report", "/api/v1/vacancies/nope/responses",
                     f"/api/v1/responses/{uuid4()}/report"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["code"], "not_found")
        url = f"/api/v1/responses/{self.ids[0]}/status"
        for value in ("maybe", "contacted", "reviewed", "green"):
            response = self.client.patch(url, json={"workflow_status": value})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["error"]["code"], "validation_error")
        self.assertEqual(self.provider.calls, [])

    def test_t12_status_persists_in_fresh_service_and_reimport(self):
        self.prepare()
        url = f"/api/v1/responses/{self.ids[0]}/status"
        response = self.client.patch(url, json={"workflow_status": "forwarded"})
        self.assertEqual(response.json(), {"id": str(self.ids[0]), "workflow_status": "forwarded"})
        import_folder(self.factory, self.folder, self.key)
        with TestClient(create_app(HRService(self.factory, self.provider))) as fresh:
            report = fresh.get(self.report_url()).json()
            self.assertEqual(report["workflow_status"], "forwarded")
            self.assertEqual(report["match_status"], "green")
        self.assertEqual(self.prepare(), 0)

    def test_matching_jobs_prepare_reports_and_reuse_pool(self):
        url = f"/api/v1/vacancies/{self.vacancy_id}/match"
        task = self.client.post(url).json()
        self.assertFalse(task["is_stub"])
        self.assertEqual(self.client.post(url).json()["task_id"], task["task_id"])
        self.service.run_task(UUID(task["task_id"]))
        self.assertEqual(self.service.task(UUID(task["task_id"])).status, "completed")
        self.assertEqual(self.client.get(self.report_url()).status_code, 200)
        task = self.service.request_questions(self.ids[0], force=True)
        self.service.run_task(task.task_id)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.service.task(task.task_id).status, "completed")

    def test_criteria_change_invalidates_and_reprepares(self):
        self.prepare()
        with self.factory.begin() as session:
            vacancy = session.get(Vacancy, self.vacancy_id)
            assert vacancy is not None
            criterion = next(c for c in vacancy.criteria if c.name == "Vue.js")
        changed = self.service.update_criteria(self.vacancy_id, CriteriaPatch.model_validate({"criteria": [
            {"id": criterion.id, "importance": "irrelevant"}]}))
        self.assertTrue(changed.changed)
        self.assertEqual(self.client.get(self.report_url()).status_code, 500)
        assert changed.task_id is not None
        self.service.run_task(changed.task_id)
        self.assertEqual(self.service.task(changed.task_id).status, "completed")
        self.assertNotIn("Vue.js", [s.skill for s in self.service.report(self.ids[0]).skills])
        self.assertEqual(len(self.provider.calls), 2)

    def test_in_flight_pool_cannot_publish_after_vacancy_changes(self):
        with self.factory.begin() as session:
            vacancy = session.get(Vacancy, self.vacancy_id)
            assert vacancy is not None
            criterion_id = vacancy.criteria[0].id
        def change():
            self.service.update_criteria(self.vacancy_id, CriteriaPatch.model_validate({"criteria": [
                {"id": criterion_id, "importance": "irrelevant"}]}))
        self.provider.callback = change
        task = self.service.request_questions(self.ids[0])
        self.service.run_task(task.task_id)
        self.assertEqual(self.service.task(task.task_id).status, "failed")
        with self.factory.begin() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Report)), 0)

    def test_failure_is_sanitized_and_does_not_publish_fake_report(self):
        self.provider.fail = True
        task = self.service.request_match(self.vacancy_id)
        self.service.run_task(task.task_id)
        failure = self.service.task(task.task_id)
        self.assertEqual(failure.status, "failed")
        self.assertNotIn("PRIVATE", failure.model_dump_json())
        response = self.client.get(self.report_url())
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "internal_error")

    def test_archive_is_explicit_and_red_visible_by_default(self):
        self.prepare()
        self.assertEqual(len(self.client.get(self.responses_url()).json()), 3)
        self.client.patch(f"/api/v1/responses/{self.ids[1]}/archive", json={"is_archived": True})
        self.assertEqual(len(self.client.get(self.responses_url()).json()), 3)
        self.assertEqual(len(self.client.get(self.responses_url(), params={"is_archived": False}).json()), 2)
        self.assertEqual(len(self.client.get(self.responses_url(), params={"match_status": "red"}).json()), 1)

    def test_raw_txt_import_supported_and_source_is_unchanged(self):
        raw = "Кандидат №4\r\nФИО: Демо Четыре\r\nРезюме: Разработчик\r\nКонтакт: demo4@example.com\r\nНавыки\r\nSQL\r\n"
        (self.folder / "отклик 4.txt").write_bytes(raw.encode("utf-8"))
        result = import_folder(self.factory, self.folder, self.key)
        self.assertEqual(result["responses_created"], 1)
        self.prepare()
        response_id = uuid5(NAMESPACE_URL, f"hr-platform/responses/{self.key}/4")
        report = self.service.report(response_id)
        self.assertEqual(report.source_text, raw)
        self.assertEqual(report.candidate_name, "Демо Четыре")

    def test_organizer_sample_1_to_3_with_separate_demo_identities(self):
        root = Path(__file__).resolve().parent
        if not (root / "parsed_responses" / "Frontend developer" / "отклик 1.json").is_file():
            self.skipTest("Organizer samples are not mounted")
        demo = create_demo(root, self.folder / "demo")
        key = "organizer-contract-demo"
        self.assertEqual(import_folder(self.factory, demo, key), {"vacancies_created": 1, "responses_created": 3})
        self.assertEqual(import_folder(self.factory, demo, key), {"vacancies_created": 0, "responses_created": 0})
        vacancy_id = uuid5(NAMESPACE_URL, "hr-platform/vacancies/" + key)
        self.service.preparer.prepare_vacancy(vacancy_id)
        reports = [self.service.report(uuid5(NAMESPACE_URL, f"hr-platform/responses/{key}/{n}")) for n in (1, 2, 3)]
        self.assertEqual(reports[1].match_status, "red")
        for number in (1, 3):
            report = reports[number-1]
            self.assertNotEqual(report.match_status, "red")
            self.assertGreater(report.match_share, reports[1].match_share)
            self.assertEqual(report.source_text, (demo / f"отклик {number}.txt").read_bytes().decode("utf-8"))
            self.assertEqual(len(report.summary), 3)
            self.assertTrue(any(s.relevance == "green" and s.questions[0].expected_answer for s in report.skills))

    def test_changing_source_invalidates_cached_report_but_reuses_pool(self):
        self.prepare()
        with self.factory.begin() as session:
            repo = Repository(session)
            row = repo.get_response(self.ids[0])
            repo.update_response(row.id, ResponseUpdate(source_json={**row.source_json, "skills": ["SQL"]}))
        self.assertEqual(self.client.get(self.report_url()).status_code, 500)
        self.assertEqual(self.prepare(), 1)
        self.assertEqual(self.service.report(self.ids[0]).match_share, .25)
        self.assertEqual(len(self.provider.calls), 1)


if __name__ == "__main__":
    unittest.main()
