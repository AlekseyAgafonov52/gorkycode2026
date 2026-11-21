"""Contract T1–T13 and durable processing against an isolated PostgreSQL schema."""
import json
import os
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from unittest.mock import patch

import pymupdf
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.schema import CreateSchema, DropSchema

from contract_models import ContractReport, MAX_SOURCE_TEXT_CHARS, PoolQuestion, SourceTextUpdate
from department_bot.service import DepartmentService
from hr_api.import_data import import_folder
from hr_api.demo_data import create_demo
from hr_api.schemas import CriteriaPatch
from hr_api.service import HRService
from hr_api.pdf_reports import INDEX_NAME, PdfEntry, PdfIndex, PdfReports, source_digest
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
    def test_all_routes_in_openapi_without_database_access(self):
        paths = create_app().openapi()["paths"]
        self.assertEqual(sum(len(methods) for methods in paths.values()), 20)
        for path in ("/api/v1/vacancies", "/api/v1/vacancies/{vacancy_id}/criteria",
                     "/api/v1/vacancies/{vacancy_id}/match", "/api/v1/tasks/{task_id}",
                     "/api/v1/responses/{response_id}/report", "/api/v1/responses/{response_id}/questions/generate",
                     "/api/v1/responses/{response_id}/viewed", "/api/v1/responses/{response_id}/source-text",
                     "/api/v1/responses/{response_id}/report/pdf/preview",
                     "/api/v1/responses/{response_id}/report/html"):
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
            if number == 1:
                data["experience_total_months"] = 61
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
            [{"id": str(self.vacancy_id), "title": "Frontend developer", "responses_count": 3, "status": "active", "hired_candidates": []}])

    def test_pdf_report_is_scoped_streamed_and_marks_changed_source(self):
        self.prepare()
        root = self.folder / "pdf"
        root.mkdir()
        with pymupdf.open() as document:
            page = document.new_page(width=300, height=400)
            page.insert_text((44, 60), "Offline assessment", fontsize=20, fontname="hebo")
            page.insert_text((44, 90), 'Resume excerpt <script>alert(1)</script>')
            document.new_page(width=400, height=300)
            pdf = document.tobytes()
        (root / "report-1.pdf").write_bytes(pdf)
        with self.factory.begin() as session:
            original = Repository(session).get_response(self.ids[0]).source_json
        index = PdfIndex(source_key=self.key, vacancy_title="Frontend developer", entries=[
            PdfEntry(candidate_id="1", filename="report-1.pdf", source_sha256=source_digest(original))])
        (root / INDEX_NAME).write_text(index.model_dump_json(), encoding="utf-8")
        self.service.pdf_reports = PdfReports(root)
        report = self.client.get(self.report_url()).json()
        self.assertEqual(report["pdf_report"], {"filename": "report-1.pdf", "is_stale": False})
        self.assertIsNone(self.client.get(self.report_url(2)).json()["pdf_report"])
        url = self.report_url() + "/pdf"
        streamed = self.client.get(url)
        self.assertEqual(streamed.status_code, 200)
        self.assertEqual(streamed.headers["content-type"], "application/pdf")
        self.assertIn("inline", streamed.headers["content-disposition"])
        self.assertEqual(streamed.content, pdf)
        self.assertIn("attachment", self.client.get(url + "?download=true").headers["content-disposition"])
        ranged = self.client.get(url, headers={"Range": "bytes=0-4"})
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.content, b"%PDF-")
        preview = self.client.get(url + "/preview")
        self.assertEqual(preview.json(), {"pages": [{"width": 300, "height": 400}, {"width": 400, "height": 300}]})
        page = self.client.get(url + "/preview?page=2")
        self.assertEqual(page.headers["content-type"], "image/png")
        self.assertTrue(page.content.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(self.client.get(url + "/preview?page=3").status_code, 404)
        html_url = self.report_url() + "/html"
        html = self.client.get(html_url)
        self.assertEqual(html.status_code, 200)
        self.assertEqual(html.headers["content-type"], "text/html; charset=utf-8")
        self.assertIn('<!doctype html>', html.text)
        self.assertIn('>Offline assessment</h1>', html.text)
        self.assertIn('Resume excerpt &lt;script&gt;alert(1)&lt;/script&gt;', html.text)
        self.assertNotIn("<iframe", html.text)
        self.assertEqual(self.client.get(self.report_url(2) + "/html").status_code, 404)
        for path in (self.report_url(2) + "/pdf", f"/api/v1/responses/{uuid4()}/report/pdf", "/api/v1/responses/not-a-uuid/report/pdf"):
            missing = self.client.get(path)
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(missing.json()["error"]["code"], "not_found")
        changed = self.client.patch(f"/api/v1/responses/{self.ids[0]}/source-text", json={
            "source_text": "Резюме: Новая версия\nНавыки\nGit"})
        self.assertEqual(changed.status_code, 200)
        self.assertTrue(changed.json()["pdf_report"]["is_stale"])
        self.assertIn('class="stale"', self.client.get(html_url).text)
        self.assertEqual(self.client.get(url).content, pdf)
        self.assertEqual(len(self.provider.calls), 1)

    def test_colors_depend_on_skills_without_identity_gate(self):
        self.prepare()
        rows = self.client.get(self.responses_url()).json()
        self.assertEqual([r["match_status"] for r in rows], ["green", "yellow", "yellow"])
        self.assertEqual([r["match_share"] for r in rows], [1, .5, .25])
        self.assertEqual(rows[-1]["id"], str(self.ids[1]))
        self.assertIsNone(rows[-1]["candidate_name"])
        self.assertEqual(set(rows[0]), {"id", "candidate_name", "experience_months", "match_status", "match_share", "short_summary", "workflow_status", "is_viewed", "is_closed", "closed_at", "hired_at"})
        self.assertEqual(rows[0]["experience_months"], 61)
        self.assertIsNone(rows[-1]["experience_months"])
        # Anonymous candidates with sufficient skills receive the same green status.
        with self.factory.begin() as session:
            row = session.get(Response, self.ids[1])
            assert row is not None
            row.source_json = {**row.source_json, "skills": ["JavaScript", "Vue 3", "SQL", "Git"]}
        self.prepare()
        rows = self.client.get(self.responses_url()).json()
        self.assertEqual([r["match_status"] for r in rows], ["green", "green", "yellow"])
        anonymous = next(row for row in rows if row["id"] == str(self.ids[1]))
        self.assertEqual(anonymous["match_share"], 1)
        self.assertEqual(anonymous["match_status"], "green")

    def test_t6_t7_t8_t9_t10_complete_cached_report(self):
        self.prepare()
        first = self.client.get(self.report_url(3))
        self.assertEqual(first.status_code, 200)
        report = first.json()
        ContractReport.model_validate(report)
        self.assertEqual(set(report), set(ContractReport.model_fields))
        self.assertEqual(report["candidate_number"], "3")
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

    def test_archive_is_explicit_and_anonymous_candidates_are_visible(self):
        self.prepare()
        self.assertEqual(len(self.client.get(self.responses_url()).json()), 3)
        self.client.patch(f"/api/v1/responses/{self.ids[1]}/archive", json={"is_archived": True})
        self.assertEqual(len(self.client.get(self.responses_url()).json()), 3)
        self.assertEqual(len(self.client.get(self.responses_url(), params={"is_archived": False}).json()), 2)
        self.assertEqual(len(self.client.get(self.responses_url(), params={"match_status": "yellow"}).json()), 2)
        self.assertEqual(self.client.get(self.responses_url(), params={"match_status": "red"}).json(), [])

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
        self.assertEqual(reports[1].match_status, "yellow")
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

    def test_old_identity_statuses_refresh_without_changing_questions(self):
        with patch("hr_api.preparation.PREPARATION_VERSION", 1):
            self.prepare()
        before = [self.service.report(identifier).model_dump(mode="json") for identifier in self.ids]
        with self.factory.begin() as session:
            for report in session.scalars(select(Report)):
                assert report.contract_json is not None
                report.contract_json = {**report.contract_json, "match_status": "red", "match_label": "Нет ФИО или контактов"}
            for saved in session.scalars(select(MatchResult)):
                saved.match_status = "red"
        self.provider.fail = True
        self.assertEqual(self.prepare(), 3)
        for identifier, expected in zip(self.ids, before):
            self.assertEqual(self.service.report(identifier).model_dump(mode="json"), expected)
        self.assertEqual(self.prepare(), 0)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual([r["match_status"] for r in self.client.get(self.responses_url()).json()], ["green", "yellow", "yellow"])

    def test_viewed_is_manual_persistent_and_independent_of_workflow_and_matching(self):
        self.prepare()
        before = self.service.report(self.ids[0]).model_dump(mode="json")
        self.assertFalse(before["is_viewed"])
        self.assertFalse(self.client.get(self.report_url()).json()["is_viewed"])
        with self.factory.begin() as session:
            report = Repository(session).get_report(self.ids[0])
            assert report is not None
            cached_hash = report.prepared_hash
        url = f"/api/v1/responses/{self.ids[0]}/viewed"
        updated = self.client.patch(url, json={"is_viewed": True})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json(), {"id": str(self.ids[0]), "is_viewed": True})
        self.assertEqual(self.service.report(self.ids[0]).model_dump(mode="json"), {**before, "is_viewed": True})
        with self.factory.begin() as session:
            report = Repository(session).get_report(self.ids[0])
            assert report is not None
            self.assertEqual(report.prepared_hash, cached_hash)
        viewed = self.client.get(self.responses_url(), params={"is_viewed": "true"}).json()
        self.assertEqual([row["id"] for row in viewed], [str(self.ids[0])])
        self.assertEqual(len(self.client.get(self.responses_url(), params={"is_viewed": "false"}).json()), 2)
        self.client.patch(f"/api/v1/responses/{self.ids[0]}/status", json={"workflow_status": "forwarded"})
        self.assertTrue(self.service.report(self.ids[0]).is_viewed)
        import_folder(self.factory, self.folder, self.key)
        self.assertEqual(self.prepare(), 0)
        with TestClient(create_app(HRService(self.factory, self.provider))) as fresh:
            report = fresh.get(self.report_url()).json()
            self.assertTrue(report["is_viewed"])
            self.assertEqual(report["workflow_status"], "forwarded")
        self.assertEqual(self.client.patch(url, json={"is_viewed": False}).status_code, 200)
        report = self.service.report(self.ids[0])
        self.assertFalse(report.is_viewed)
        self.assertEqual(report.workflow_status, "forwarded")
        self.assertEqual(len(self.provider.calls), 1)

    def test_viewed_validates_boolean_and_not_found_without_requiring_report(self):
        url = f"/api/v1/responses/{self.ids[0]}/viewed"
        for value in ("true", "false", 1, 0, None):
            response = self.client.patch(url, json={"is_viewed": value})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["error"]["code"], "validation_error")
        self.assertEqual(self.client.patch(url, json={}).status_code, 422)
        self.assertEqual(self.client.patch(url, json={"is_viewed": True}).status_code, 200)
        for identifier in ("nope", str(uuid4())):
            response = self.client.patch(f"/api/v1/responses/{identifier}/viewed", json={"is_viewed": False})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["code"], "not_found")
        self.assertEqual(self.provider.calls, [])

    def test_edit_source_reparses_and_updates_report_without_model_calls(self):
        self.prepare()
        self.service.update_status(self.ids[0], "forwarded")
        self.service.update_viewed(self.ids[0], True)
        before = self.service.report(self.ids[0])
        raw = ("Кандидат №999\r\nФИО: Тестовый Кандидат\r\nРезюме: Разработчик\r\n"
               "Email: changed@example.com\r\nНавыки\r\nПродвинутый уровень\r\nSQL\r\n")
        self.provider.fail = True
        response = self.client.patch(f"/api/v1/responses/{self.ids[0]}/source-text", json={
            "source_text": raw, "expected_source_text": before.source_text})
        self.assertEqual(response.status_code, 200)
        report = response.json()
        self.assertEqual(report["source_text"], raw)
        self.assertEqual(report["candidate_name"], "Тестовый Кандидат")
        self.assertEqual(report["contacts"]["email"], "changed@example.com")
        self.assertEqual(report["match_share"], .25)
        self.assertEqual(report["workflow_status"], "forwarded")
        self.assertTrue(report["is_viewed"])
        with self.factory.begin() as session:
            saved = Repository(session).get_response(self.ids[0])
            self.assertEqual(saved.source_json["candidate_id"], 1)
            skills = saved.source_json["skills"]
            assert isinstance(skills, list)
            self.assertEqual([item["name"] for item in skills if isinstance(item, dict)], ["SQL"])
        self.assertEqual(self.client.get(self.report_url()).json(), report)
        import_folder(self.factory, self.folder, self.key)
        with TestClient(create_app(HRService(self.factory, self.provider))) as fresh:
            self.assertEqual(fresh.get(self.report_url()).json(), report)
        self.assertEqual(self.prepare(), 0)
        self.assertEqual(len(self.provider.calls), 1)

    def test_department_closure_is_visible_without_regeneration_and_survives_source_edit(self):
        self.prepare()
        self.service.update_status(self.ids[0], "forwarded")
        department = DepartmentService(self.factory, frozenset({12345}), "test-only-secret")
        with self.factory.begin() as session:
            cached = Repository(session).get_response(self.ids[0]).report
            assert cached is not None
            before_cache = cached.contract_json
        _, token = department.confirmation(12345, "r", self.ids[0], True, hiring=True)
        department.confirm(12345, token)
        vacancy = self.client.get("/api/v1/vacancies").json()[0]
        self.assertEqual(vacancy["status"], "active")
        self.assertEqual(vacancy["hired_candidates"][0]["id"], str(self.ids[0]))
        self.assertEqual(vacancy["hired_candidates"][0]["candidate_name"], "Демо Кандидат 1")
        closed = self.client.patch(f"/api/v1/vacancies/{self.vacancy_id}/status", json={"status": "closed"})
        self.assertEqual(closed.status_code, 200)
        report = self.client.get(self.report_url()).json()
        self.assertTrue(report["is_closed"])
        self.assertIsNotNone(report["hired_at"])
        self.assertIsNotNone(report["closed_at"])
        rows = self.client.get(self.responses_url()).json()
        self.assertTrue(next(row for row in rows if row["id"] == str(self.ids[0]))["is_closed"])
        self.assertEqual(self.client.get("/api/v1/vacancies").json()[0]["status"], "closed")
        with self.factory.begin() as session:
            cached = Repository(session).get_response(self.ids[0]).report
            assert cached is not None
            self.assertEqual(cached.contract_json, before_cache)
        self.provider.fail = True
        updated = self.client.patch(f"/api/v1/responses/{self.ids[0]}/source-text", json={
            "source_text": "Резюме: Разработчик\nНавыки\nSQL"})
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(updated.json()["is_closed"])
        self.assertEqual(updated.json()["closed_at"], report["closed_at"])
        self.assertEqual(updated.json()["hired_at"], report["hired_at"])
        self.assertEqual(updated.json()["workflow_status"], "forwarded")
        self.assertEqual(len(self.provider.calls), 1)

    def test_recruiter_summary_forward_and_update_queue_notifications_without_regeneration(self):
        self.prepare()
        recommendation = {"strengths": "SQL на практике", "weaknesses": "Мало опыта с Git", "interview_focus": "Работу с ветками"}
        url = f"/api/v1/responses/{self.ids[0]}"
        forwarded = self.client.patch(url + "/status", json={"workflow_status": "forwarded", "recruiter_recommendation": recommendation})
        self.assertEqual(forwarded.status_code, 200)
        self.assertEqual(self.client.get(self.report_url()).json()["recruiter_recommendation"], {**recommendation, "notes": None})
        with self.factory.begin() as session:
            row = Repository(session).get_response(self.ids[0])
            self.assertTrue(row.department_notification_pending)
            version = row.department_notification_version
            self.assertEqual(version, 1)
        department = DepartmentService(self.factory, frozenset({12345}), "test-only-secret")
        department.mark_delivered(12345, self.ids[0], version, "packet")
        department.acknowledge_notification(12345, self.ids[0], version)
        with self.factory.begin() as session:
            Repository(session).get_response(self.ids[0]).recruiter_recommendation = dict(recommendation)
        self.client.patch(url + "/status", json={"workflow_status": "forwarded", "recruiter_recommendation": recommendation})
        self.assertEqual(department.pending_notifications(12345), [])
        recommendation["notes"] = "Уточнить доступность на следующей неделе."
        updated = self.client.patch(url + "/department-summary", json={"recruiter_recommendation": recommendation})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(self.client.get(self.report_url()).json()["recruiter_recommendation"], recommendation)
        department.acknowledge_notification(12345, self.ids[0], version)
        self.assertEqual(len(department.pending_notifications(12345)), 1)
        self.client.patch(url + "/status", json={"workflow_status": "not_forwarded"})
        self.assertEqual(department.pending_notifications(12345), [])
        self.assertEqual(len(self.provider.calls), 1)
        for data in ({"strengths": 123}, {"weaknesses": "x\x00"}, {**recommendation, "strengths": "x" * 801}):
            self.assertEqual(self.client.patch(url + "/department-summary", json={"recruiter_recommendation": data}).status_code, 422)
        minimal = self.client.patch(url + "/status", json={"workflow_status": "forwarded",
            "recruiter_recommendation": {"strengths": "Python", "weaknesses": "SQL"}})
        self.assertEqual(minimal.status_code, 200)
        self.assertIsNone(self.client.get(self.report_url()).json()["recruiter_recommendation"]["interview_focus"])

    def test_forward_and_clear_recommendations_with_all_fields_optional(self):
        self.prepare()
        url = f"/api/v1/responses/{self.ids[0]}"
        empty = {"strengths": None, "weaknesses": None, "interview_focus": None, "notes": None}
        for recommendation in ({}, {"strengths": ""}, {"weaknesses": None},
                               {"strengths": "  ", "weaknesses": "\r\n", "interview_focus": ""}):
            response = self.client.patch(url + "/status", json={"workflow_status": "forwarded",
                "recruiter_recommendation": recommendation})
            self.assertEqual(response.status_code, 200)
            report = self.client.get(self.report_url()).json()
            self.assertEqual(report["workflow_status"], "forwarded")
            self.assertEqual(report["recruiter_recommendation"], empty)
        saved = self.client.patch(url + "/department-summary", json={"recruiter_recommendation": {"notes": " Удобно после 16:00 "}})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["recruiter_recommendation"], {**empty, "notes": "Удобно после 16:00"})
        cleared = self.client.patch(url + "/department-summary", json={"recruiter_recommendation": {}})
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json()["recruiter_recommendation"], empty)
        self.assertEqual(self.client.get(self.report_url()).json()["recruiter_recommendation"], empty)
        self.assertEqual(len(self.provider.calls), 1)

    def test_edit_source_does_not_keep_removed_identity_contacts_or_skills(self):
        self.prepare()
        with self.factory.begin() as session:
            row = Repository(session).get_response(self.ids[0])
            row.source_json = {**row.source_json, "full_name": "Old Name", "custom_metadata": {"keep": True}}
        raw = "Кандидат №1\nРезюме: Разработчик\nНавыки\nБазовый уровень\nGit\n"
        response = self.client.patch(f"/api/v1/responses/{self.ids[0]}/source-text", json={"source_text": raw})
        self.assertEqual(response.status_code, 200)
        report = response.json()
        self.assertIsNone(report["candidate_name"])
        self.assertIsNone(report["contacts"]["email"])
        with self.factory.begin() as session:
            row = Repository(session).get_response(self.ids[0])
            self.assertIsNone(row.candidate_name)
            self.assertIsNone(row.source_json["full_name"])
            self.assertEqual(row.source_json["custom_metadata"], {"keep": True})
            skills = row.source_json["skills"]
            assert isinstance(skills, list)
            self.assertEqual([item["name"] for item in skills if isinstance(item, dict)], ["Git"])

    def test_edit_source_rejects_stale_text_and_preserves_latest_edit(self):
        self.prepare()
        before = self.service.report(self.ids[0])
        url = f"/api/v1/responses/{self.ids[0]}/source-text"
        first = self.client.patch(url, json={"source_text": "Резюме: Новый текст\nНавыки\nSQL", "expected_source_text": before.source_text})
        self.assertEqual(first.status_code, 200)
        second = self.client.patch(url, json={"source_text": "Another edit", "expected_source_text": before.source_text})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["error"]["code"], "conflict")
        self.assertEqual(self.client.get(self.report_url()).json(), first.json())

    def test_edit_source_is_idempotent_and_rolls_back_if_analysis_fails(self):
        self.prepare()
        before = self.service.report(self.ids[0])
        data = SourceTextUpdate(source_text=before.source_text)
        self.assertEqual(self.service.update_source_text(self.ids[0], data), before)
        with self.factory.begin() as session:
            original_json = Repository(session).get_response(self.ids[0]).source_json
        with patch.object(self.service.preparer, "prepare_response_in_session", side_effect=RuntimeError("analysis failed")):
            with self.assertRaises(RuntimeError):
                self.service.update_source_text(self.ids[0], SourceTextUpdate(source_text="Резюме: New\nНавыки\nGit"))
        self.assertEqual(self.service.report(self.ids[0]), before)
        with self.factory.begin() as session:
            self.assertEqual(Repository(session).get_response(self.ids[0]).source_json, original_json)
        self.assertEqual(len(self.provider.calls), 1)

    def test_edit_source_validates_text_unknown_response_and_cached_pool(self):
        self.prepare()
        url = f"/api/v1/responses/{self.ids[0]}/source-text"
        before = self.service.report(self.ids[0])
        for value in (None, 123, "", " \r\n", "text\x00binary", "a" * (MAX_SOURCE_TEXT_CHARS + 1)):
            response = self.client.patch(url, json={"source_text": value})
            self.assertEqual(response.status_code, 422)
        for identifier in ("nope", str(uuid4())):
            response = self.client.patch(f"/api/v1/responses/{identifier}/source-text", json={"source_text": "Updated resume"})
            self.assertEqual(response.status_code, 404)
        with self.factory.begin() as session:
            row = Repository(session).get_vacancy(self.vacancy_id)
            row.question_pool_hash = None
        response = self.client.patch(url, json={"source_text": "Updated resume"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.service.report(self.ids[0]), before)
        self.assertEqual(len(self.provider.calls), 1)


if __name__ == "__main__":
    unittest.main()
