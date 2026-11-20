import json
import os
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.schema import CreateSchema, DropSchema

from check_candidate import PROFILES
from hr_api.import_data import import_datasets
from hr_api.schemas import CriteriaPatch
from hr_api.service import HRService
from main import create_app
from storage.crud import Repository
from storage.database import DatabaseSettings, make_engine, make_session_factory
from storage.models import MatchResult, Response, Vacancy
from storage.schemas import (
    CriterionCreate, CriterionUpdate, MatchResultWrite, QuestionCreate,
    ResponseCreate, VacancyCreate,
)
from test_storage import migration_config


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.callback: Callable[[], None] | None = None
        self.fail = False

    def generate(self, context):
        self.calls.append(context)
        if self.callback:
            self.callback()
        if self.fail:
            raise RuntimeError("PRIVATE_PROVIDER_ERROR")
        return [QuestionCreate(criterion_id=context.required_criteria[0].id,
            question="Расскажите о задаче, где вы применяли этот навык?",
            expected_answer="Описание задачи и личного вклада")]


class HRContractTests(unittest.TestCase):
    def test_all_thirteen_routes_in_openapi_without_database_access(self):
        paths = create_app().openapi()["paths"]
        self.assertEqual(sum(len(methods) for methods in paths.values()), 13)
        for path in ("/api/v1/vacancies", "/api/v1/vacancies/{vacancy_id}/criteria",
                     "/api/v1/vacancies/{vacancy_id}/match", "/api/v1/tasks/{task_id}",
                     "/api/v1/responses/{response_id}/report", "/api/v1/responses/{response_id}/questions/generate"):
            self.assertIn(path, paths)


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
        self.provider = FakeProvider()
        self.service = HRService(self.factory, self.provider)
        self.client = TestClient(create_app(self.service))
        with self.factory.begin() as session:
            repo = Repository(session)
            vacancy = repo.create_vacancy(VacancyCreate(title="Python developer", source_json={"title": "Python developer"}, criteria=[
                CriterionCreate(name="Python", importance="required"),
                CriterionCreate(name="SQL", importance="preferred"),
                CriterionCreate(name="Git", importance="irrelevant")]))
            self.vacancy_id = vacancy.id
            self.criteria = {c.name: c.id for c in vacancy.criteria}
            response = repo.create_response(ResponseCreate(vacancy_id=vacancy.id, source_json={
                "candidate_id": 1, "skills": [{"name": "Python", "level": "basic", "original": "Python"}],
                "source_text": "PRIVATE_RESUME_TEXT", "work_experience": [{"description": "PRIVATE_WORK"}]}))
            self.response_id = response.id

    def tearDown(self):
        self.client.close()
        with self.factory.begin() as session:
            session.execute(delete(Vacancy))

    def url(self, suffix=""):
        return f"/api/v1/responses/{self.response_id}" + suffix

    def test_vacancy_details_and_response_card(self):
        response = self.client.get("/api/v1/vacancies")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["responses_count"], 1)
        detail = self.client.get(f"/api/v1/vacancies/{self.vacancy_id}").json()
        self.assertEqual(detail["criteria_version"], 1)
        self.assertEqual(len(detail["criteria"]), 3)
        card = self.client.get(self.url()).json()
        self.assertEqual(card["source_json"]["source_text"], "PRIVATE_RESUME_TEXT")
        self.assertEqual(card["structured_resume"]["skills"][0]["name"], "Python")
        self.assertIsNone(card["match_score"])
        self.assertEqual(card["report_state"], "not_ready")
        self.assertEqual(self.provider.calls, [])

    def test_matching_stub_tasks_are_persisted_and_reused(self):
        url = f"/api/v1/vacancies/{self.vacancy_id}/match"
        first = self.client.post(url)
        self.assertEqual(first.status_code, 202)
        task = first.json()
        self.assertTrue(task["is_stub"])
        second = self.client.post(url).json()
        self.assertEqual(second["task_id"], task["task_id"])
        self.assertTrue(second["reused"])
        from uuid import UUID
        self.service.run_task(UUID(task["task_id"]))
        self.assertEqual(self.client.get("/api/v1/tasks/" + task["task_id"]).json()["status"], "completed")
        self.assertIsNone(self.client.get(self.url()).json()["match_status"])
        with self.factory.begin() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(MatchResult)), 0)
        self.assertEqual(self.provider.calls, [])

    def test_questions_only_required_skills_only_and_get_uses_cache(self):
        task = self.service.request_questions(self.response_id)
        self.service.run_task(task.task_id)
        self.assertEqual(self.service.task(task.task_id).status, "completed")
        context = self.provider.calls[0]
        self.assertEqual([c.name for c in context.required_criteria], ["Python"])
        self.assertNotIn("PRIVATE_", context.model_dump_json())
        report = self.client.get(self.url("/report")).json()
        self.assertEqual(report["questions"][0]["requirement"], "Python")
        self.assertTrue(report["questions"][0]["expected_answer"])
        self.assertEqual(report["state"], "partial")
        self.client.get(self.url("/report"))
        reused = self.client.post(self.url("/questions/generate")).json()
        self.assertEqual(reused["task_id"], str(task.task_id))
        self.assertTrue(reused["reused"])
        self.assertEqual(len(self.provider.calls), 1)
        forced = self.client.post(self.url("/questions/generate"), json={"force": True}).json()
        self.assertNotEqual(forced["task_id"], str(task.task_id))

    def test_status_archive_and_restore_keep_report(self):
        task = self.service.request_questions(self.response_id)
        self.service.run_task(task.task_id)
        report_id = self.service.report(self.response_id).report_id
        updated = self.client.patch(self.url("/status"), json={"workflow_status": "contacted"})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["workflow_status"], "contacted")
        self.assertTrue(self.client.patch(self.url("/archive"), json={"is_archived": True}).json()["is_archived"])
        url = f"/api/v1/vacancies/{self.vacancy_id}/responses"
        self.assertEqual(self.client.get(url).json(), [])
        self.assertEqual(len(self.client.get(url, params={"is_archived": "true"}).json()), 1)
        self.client.patch(self.url("/archive"), json={"is_archived": False})
        self.assertEqual(self.service.report(self.response_id).report_id, report_id)

    def test_criteria_change_invalidates_and_regenerates_requested_questions(self):
        first = self.service.request_questions(self.response_id)
        self.service.run_task(first.task_id)
        url = f"/api/v1/vacancies/{self.vacancy_id}/criteria"
        payload = {"criteria": [{"id": str(self.criteria["Python"]), "importance": "required"}]}
        unchanged = self.client.patch(url, json=payload).json()
        self.assertFalse(unchanged["changed"])
        payload["criteria"][0]["importance"] = "irrelevant"
        payload["criteria"].append({"id": str(self.criteria["SQL"]), "importance": "required"})
        updated = self.client.patch(url, json=payload)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["criteria_version"], 2)
        self.assertTrue(self.service.report(self.response_id).is_stale)
        self.assertEqual(self.service.report(self.response_id).questions, [])
        from uuid import UUID
        self.service.run_task(UUID(updated.json()["task_id"]))
        queued = self.service.next_task_id()
        assert queued is not None
        self.service.run_task(queued)
        report = self.service.report(self.response_id)
        self.assertFalse(report.is_stale)
        self.assertEqual(report.questions[0].requirement, "SQL")

    def test_old_in_flight_generation_cannot_publish_after_criteria_change(self):
        task = self.service.request_questions(self.response_id)

        def change():
            self.service.update_criteria(self.vacancy_id, CriteriaPatch.model_validate({"criteria": [
                {"id": self.criteria["Python"], "importance": "irrelevant"}]}))

        self.provider.callback = change
        self.service.run_task(task.task_id)
        self.assertEqual(self.service.task(task.task_id).status, "failed")
        self.assertEqual(self.service.report(self.response_id).questions, [])

    def test_failure_is_sanitized_and_no_required_skips_provider(self):
        self.provider.fail = True
        task = self.service.request_questions(self.response_id)
        self.service.run_task(task.task_id)
        failure = self.service.task(task.task_id)
        self.assertEqual(failure.status, "failed")
        self.assertNotIn("PRIVATE", failure.model_dump_json())
        with self.factory.begin() as session:
            repo = Repository(session)
            repo.update_criterion(self.criteria["Python"], CriterionUpdate(importance="irrelevant"))
        self.provider.calls.clear()
        task = self.service.request_questions(self.response_id, force=True)
        self.service.run_task(task.task_id)
        self.assertEqual(self.service.task(task.task_id).status, "completed")
        self.assertEqual(self.provider.calls, [])

    def test_read_filters_order_and_stale_scores(self):
        with self.factory.begin() as session:
            repo = Repository(session)
            repo.save_match_result(self.response_id, MatchResultWrite(criteria_version=1, match_status="green"))
        url = f"/api/v1/vacancies/{self.vacancy_id}/responses"
        self.assertEqual(len(self.client.get(url, params={"match_status": "green"}).json()), 1)
        with self.factory.begin() as session:
            Repository(session).update_criterion(self.criteria["Python"], CriterionUpdate(description="Changed"))
        self.assertEqual(self.client.get(url, params={"match_status": "green"}).json(), [])
        self.assertIsNone(self.client.get(url).json()[0]["match_score"])

    def test_red_result_is_archived_and_can_be_restored(self):
        self.service.publish_match_result(self.response_id, MatchResultWrite(criteria_version=1, match_status="red"))
        url = f"/api/v1/vacancies/{self.vacancy_id}/responses"
        self.assertEqual(self.client.get(url).json(), [])
        archived = self.client.get(url, params={"is_archived": True, "match_status": "red"}).json()
        self.assertEqual(len(archived), 1)
        self.client.patch(self.url("/archive"), json={"is_archived": False})
        self.assertEqual(len(self.client.get(url).json()), 1)

    def test_validation_and_missing_ids(self):
        self.assertEqual(self.client.get("/api/v1/vacancies/invalid").status_code, 422)
        self.assertEqual(self.client.get(f"/api/v1/vacancies/{uuid4()}").status_code, 404)
        self.assertEqual(self.client.patch(self.url("/status"), json={"workflow_status": "green"}).status_code, 422)
        self.assertEqual(self.client.patch(self.url("/archive"), json={"is_archived": "true"}).status_code, 422)
        url = f"/api/v1/vacancies/{self.vacancy_id}/criteria"
        invalid = {"criteria": [{"id": str(uuid4()), "importance": "required"}]}
        self.assertEqual(self.client.patch(url, json=invalid).status_code, 422)
        self.assertEqual(self.provider.calls, [])

    def test_import_is_idempotent_and_does_not_overwrite_hr_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "parsed_responses" / PROFILES["frontend"]
            folder.mkdir(parents=True)
            (folder / "job_vacancy.txt").write_text("Developer\nТребования:\nPython", encoding="utf-8")
            (folder / "отклик 1.json").write_text(json.dumps({"candidate_id": 1, "skills": ["Python"]}), encoding="utf-8")
            first = import_datasets(self.factory, root)
            second = import_datasets(self.factory, root)
            self.assertEqual(first, {"vacancies_created": 1, "responses_created": 1})
            self.assertEqual(second, {"vacancies_created": 0, "responses_created": 0})
            with self.factory.begin() as session:
                row = session.scalar(select(Response).where(Response.id != self.response_id))
                assert row is not None
                row.workflow_status = "contacted"
            import_datasets(self.factory, root)
            with self.factory.begin() as session:
                current = session.get(Response, row.id)
                assert current is not None
                self.assertEqual(current.workflow_status, "contacted")


if __name__ == "__main__":
    unittest.main()
