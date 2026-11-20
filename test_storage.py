"""Unit checks and opt-in tests against an isolated real PostgreSQL schema."""
import os
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CreateSchema, DropSchema

from storage.crud import NotFoundError, Repository, StaleCriteriaError
from storage.database import DatabaseSettings, make_engine, make_session_factory, session_scope
from storage.models import AnalysisTask, Base, CriterionEvaluation, MatchResult, Question, Report, Response, Vacancy, VacancyCriterion
from storage.schemas import (
    CriterionCreate, CriterionUpdate, EvaluationWrite, MatchResultRead, MatchResultWrite,
    QuestionCreate, ReportRead, ReportWrite, ResponseCreate, ResponseRead, ResponseUpdate,
    TaskCreate, TaskRead, VacancyCreate, VacancyRead, VacancyUpdate,
)


ROOT = Path(__file__).resolve().parent
TABLES = {"vacancies", "vacancy_criteria", "responses", "analysis_tasks", "match_results",
          "criterion_evaluations", "reports", "questions"}
INDEXES = {"idx_criteria_vacancy", "idx_responses_vacancy", "idx_responses_status",
           "idx_match_results_score", "idx_tasks_status", "idx_evaluations_match", "idx_questions_report"}


def migration_config(connection, schema: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    config.attributes["version_table_schema"] = schema
    return config


class StorageUnitTests(unittest.TestCase):
    def test_eight_models_relationships_and_exact_indexes(self):
        configure_mappers()
        self.assertEqual(set(Base.metadata.tables), TABLES)
        self.assertEqual({index.name for table in Base.metadata.tables.values() for index in table.indexes}, INDEXES)
        foreign_keys = [key for table in Base.metadata.tables.values() for key in table.foreign_keys]
        self.assertEqual(len(foreign_keys), 10)
        self.assertEqual([key.ondelete for key in foreign_keys].count("SET NULL"), 1)
        self.assertTrue(all(key.ondelete in ("CASCADE", "SET NULL") for key in foreign_keys))

    def test_settings_are_postgres_only_and_hide_secrets(self):
        settings = DatabaseSettings.model_validate({"database_url": "postgresql://demo:p%40ss%25word@localhost/example"})
        self.assertTrue(settings.database_url.get_secret_value().startswith("postgresql+psycopg://"))
        self.assertNotIn("p%40ss", repr(settings))
        for value in ("sqlite:///test.db", "postgresql://localhost/", "not-a-url"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                DatabaseSettings.model_validate({"database_url": value})

    def test_dotenv_literal_values_and_environment_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('DATABASE_URL="postgresql://u:secret-${LITERAL}@localhost/test"\n', encoding="utf-8-sig")
            with patch.dict(os.environ, {}, clear=True):
                settings = DatabaseSettings.from_env(path)
                self.assertEqual(make_url(settings.database_url.get_secret_value()).password, "secret-${LITERAL}")
                self.assertNotIn("DATABASE_URL", os.environ)
            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://other/db"}, clear=True):
                self.assertIn("other/db", DatabaseSettings.from_env(path).database_url.get_secret_value())
            with patch.dict(os.environ, {"DATABASE_URL": "not-a-url-CONFIDENTIAL"}, clear=True):
                with self.assertRaises(ValueError) as failure:
                    DatabaseSettings.from_env(path)
                self.assertNotIn("CONFIDENTIAL", str(failure.exception))
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "DATABASE_URL"):
            DatabaseSettings.from_env(None)

    def test_pydantic_rejects_invalid_inputs_and_preserves_json(self):
        original = {"candidate_id": 1, "skills": [{"name": "Vue.js", "original": "VueJS", "level": None}],
                    "original_sections": {"about_me": "Исходная формулировка"}, "unknown": [0, False, None]}
        data = ResponseCreate(vacancy_id=uuid4(), source_json=original)
        self.assertEqual(data.source_json, original)
        for factory in (
            lambda: VacancyCreate(title=" "), lambda: VacancyCreate.model_validate({"title": "X", "status": "wrong"}),
            lambda: CriterionCreate.model_validate({"name": "SQL", "importance": "wrong"}), lambda: TaskCreate(task_type="matching"),
            lambda: ResponseUpdate.model_validate({"workflow_status": "green"}), lambda: ResponseUpdate.model_validate({"is_archived": "true"}),
            lambda: VacancyUpdate(title=None), lambda: ResponseUpdate(source_json=None),
            lambda: MatchResultWrite(criteria_version=0, match_status="yellow"),
            lambda: MatchResultWrite(criteria_version=1, match_status="green", match_score=Decimal(101)),
            lambda: MatchResultWrite(criteria_version=1, match_status="green", match_score=Decimal("12.345")),
            lambda: VacancyCreate(title="X", source_json={"nested": [float("nan")]}),
        ):
            with self.subTest(factory=factory), self.assertRaises(ValidationError):
                factory()
        self.assertIsNone(ResponseUpdate(candidate_name=None).candidate_name)
        self.assertIsNone(CriterionUpdate(description=None).description)

    def test_evaluation_pairs_must_be_unique(self):
        evaluation = EvaluationWrite(criterion_id=uuid4(), status="unknown")
        with self.assertRaises(ValidationError):
            MatchResultWrite(criteria_version=1, match_status="yellow", evaluations=[evaluation, evaluation])


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL for real PostgreSQL integration tests")
class PostgreSQLStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_engine = make_engine(DatabaseSettings.from_env(None, variable="TEST_DATABASE_URL"))
        cls.schema = "hr_storage_" + uuid4().hex
        with cls.base_engine.begin() as connection:
            connection.execute(CreateSchema(cls.schema))
        cls.engine = cls.base_engine.execution_options(schema_translate_map={None: cls.schema})
        try:
            with cls.engine.begin() as connection:
                command.upgrade(migration_config(connection, cls.schema), "head")
        except BaseException:
            with cls.base_engine.begin() as connection:
                connection.execute(DropSchema(cls.schema, cascade=True))
            cls.base_engine.dispose()
            raise
        cls.factory = make_session_factory(cls.engine)

    @classmethod
    def tearDownClass(cls):
        assert re.fullmatch(r"hr_storage_[0-9a-f]{32}", cls.schema)
        with cls.base_engine.begin() as connection:
            connection.execute(DropSchema(cls.schema, cascade=True))
        cls.base_engine.dispose()

    def setUp(self):
        self.session = self.factory()
        self.repo = Repository(self.session)

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def seed(self):
        vacancy = self.repo.create_vacancy(VacancyCreate(title="Python developer", source_json={"title": "Python developer"}, criteria=[
            CriterionCreate(name="Python", importance="required"), CriterionCreate(name="SQL", importance="preferred")]))
        response = self.repo.create_response(ResponseCreate(vacancy_id=vacancy.id, source_json={
            "candidate_id": 1, "skills": [{"name": "Python", "level": None, "original": "Python"}],
            "source_text": "Исходный текст", "extra": [False, 0, None]}))
        return vacancy, response, vacancy.criteria[0]

    def save_analysis(self, vacancy, response, criterion):
        match = self.repo.save_match_result(response.id, MatchResultWrite(
            criteria_version=vacancy.criteria_version, match_score=Decimal("75.25"), match_status="yellow",
            missing_data=[{"criterion_id": str(criterion.id), "detail": "Практический опыт не раскрыт"}],
            evaluations=[EvaluationWrite(criterion_id=criterion.id, status="partial", evidence=["Python"])],
        ))
        report = self.repo.save_report(response.id, ReportWrite(
            criteria_version=vacancy.criteria_version, summary="Описание кандидата", strengths=["Python"],
            structured_resume={"skills": response.source_json["skills"]}, questions=[
                QuestionCreate(criterion_id=criterion.id, question="Пример применения Python?", expected_answer="Пример проекта", position=2),
                QuestionCreate(question="Как проверяли результат?", follow_up_hint="Уточнить личный вклад", position=0)]))
        return match, report

    def test_migration_columns_foreign_keys_indexes_and_constraints(self):
        with self.engine.connect() as connection:
            inspector = inspect(connection)
            self.assertEqual(set(inspector.get_table_names(schema=self.schema)), TABLES | {"alembic_version"})
            for name, table in Base.metadata.tables.items():
                columns = inspector.get_columns(name, schema=self.schema)
                self.assertEqual({column["name"] for column in columns}, set(table.columns.keys()))
                self.assertEqual({column["name"]: column["nullable"] for column in columns},
                                 {column.name: column.nullable for column in table.columns})
                self.assertEqual({index["name"] for index in inspector.get_indexes(name, schema=self.schema)
                                  if not index.get("duplicates_constraint")}, {index.name for index in table.indexes})
                self.assertEqual(len(inspector.get_foreign_keys(name, schema=self.schema)), len(table.foreign_keys))
            defaults = {column["name"]: column.get("default") for column in inspector.get_columns("vacancies", schema=self.schema)}
            self.assertIn("gen_random_uuid", defaults["id"] or "")
            self.assertIn("now()", defaults["created_at"] or "")

    def test_vacancy_response_json_roundtrip_and_read_models(self):
        vacancy, response, _ = self.seed()
        self.session.expire_all()
        self.assertEqual(self.repo.get_vacancy(vacancy.id).criteria_version, 1)
        vacancy_data = VacancyRead.model_validate(self.repo.get_vacancy(vacancy.id))
        response_data = ResponseRead.model_validate(self.repo.get_response(response.id))
        self.assertEqual(len(vacancy_data.criteria), 2)
        self.assertEqual(response_data.source_json["extra"], [False, 0, None])
        self.assertEqual(response_data.source_json["source_text"], "Исходный текст")
        self.assertEqual(response_data.workflow_status, "new")
        self.assertFalse(response_data.is_archived)
        self.assertIsNotNone(response_data.created_at.tzinfo)
        self.repo.update_vacancy(vacancy.id, VacancyUpdate(status="closed"))
        self.assertEqual(len(self.repo.list_vacancies(status="closed")), 1)

    def test_migrated_schema_matches_the_supplied_sql(self):
        reference = "hr_storage_" + uuid4().hex
        with self.base_engine.begin() as connection:
            connection.execute(CreateSchema(reference))
        try:
            source = (ROOT / "sql/hr_platform_schema.sql").read_text(encoding="utf-8-sig")
            source = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("--"))
            with self.base_engine.begin() as connection:
                connection.exec_driver_sql(f'SET LOCAL search_path TO "{reference}", public')
                for statement in source.split(";"):
                    statement = statement.strip()
                    if not statement or statement in ("BEGIN", "COMMIT") or statement.startswith("CREATE EXTENSION"):
                        continue
                    self.assertTrue(statement.startswith(("CREATE TABLE ", "CREATE INDEX ")))
                    connection.exec_driver_sql(statement)
                # Original agreed SQL is revision 0001; apply the same additive
                # MVP migration to compare both paths at the current revision.
                connection = connection.execution_options(schema_translate_map={None: reference})
                config = migration_config(connection, reference)
                command.stamp(config, "0001")
                command.upgrade(config, "head")
                inspector = inspect(connection)
                self.assertEqual(set(inspector.get_table_names(schema=reference)), TABLES | {"alembic_version"})
                for name in TABLES:
                    def columns(schema):
                        return {c["name"]: (str(c["type"]), c["nullable"], c.get("default"))
                                for c in inspector.get_columns(name, schema=schema)}

                    def foreign_keys(schema):
                        return {(tuple(c["constrained_columns"]), c["referred_table"], tuple(c["referred_columns"]),
                                 c.get("options", {}).get("ondelete")) for c in inspector.get_foreign_keys(name, schema=schema)}

                    def unique_keys(schema):
                        return {tuple(c["column_names"]) for c in inspector.get_unique_constraints(name, schema=schema)}

                    def checks(schema):
                        return {c["sqltext"] for c in inspector.get_check_constraints(name, schema=schema)}

                    def indexes(schema):
                        return {c["name"]: (c["column_names"], c.get("column_sorting", {}))
                                for c in inspector.get_indexes(name, schema=schema) if c["name"] in INDEXES}

                    for structure in (columns, foreign_keys, unique_keys, checks, indexes):
                        self.assertEqual(structure(reference), structure(self.schema), (name, structure.__name__))
        finally:
            assert re.fullmatch(r"hr_storage_[0-9a-f]{32}", reference)
            with self.base_engine.begin() as connection:
                connection.execute(DropSchema(reference, cascade=True))

    def test_criteria_version_changes_once_per_real_batch(self):
        vacancy, _, first = self.seed()
        second = vacancy.criteria[1]
        self.repo.update_criteria(vacancy.id, {first.id: CriterionUpdate(importance="required")})
        self.assertEqual(vacancy.criteria_version, 1)
        self.repo.update_criteria(vacancy.id, {first.id: CriterionUpdate(importance="preferred"),
                                              second.id: CriterionUpdate(importance="required")})
        self.assertEqual(vacancy.criteria_version, 2)
        added = self.repo.create_criterion(vacancy.id, CriterionCreate(name="Git"))
        self.assertEqual(vacancy.criteria_version, 3)
        self.repo.delete_criterion(added.id)
        self.assertEqual(self.repo.get_vacancy(vacancy.id).criteria_version, 4)

    def test_upserts_replace_children_without_duplicate_results(self):
        vacancy, response, criterion = self.seed()
        match, report = self.save_analysis(vacancy, response, criterion)
        match_id, report_id = match.id, report.id
        match, report = self.save_analysis(vacancy, response, criterion)
        self.assertEqual((match.id, report.id), (match_id, report_id))
        self.assertEqual(self.session.scalar(select(func.count()).select_from(MatchResult)), 1)
        self.assertEqual(self.session.scalar(select(func.count()).select_from(Report)), 1)
        self.assertEqual(self.session.scalar(select(func.count()).select_from(CriterionEvaluation)), 1)
        self.assertEqual(self.session.scalar(select(func.count()).select_from(Question)), 2)
        self.assertEqual(MatchResultRead.model_validate(match).match_score, Decimal("75.25"))
        current = self.repo.get_report(response.id)
        self.assertEqual([q.position for q in ReportRead.model_validate(current).questions], [0, 2])

    def test_stale_results_are_detected_and_old_writes_rejected(self):
        vacancy, response, criterion = self.seed()
        self.assertIsNone(self.repo.analysis_freshness(response.id).match_is_stale)
        self.save_analysis(vacancy, response, criterion)
        self.assertFalse(self.repo.analysis_freshness(response.id).report_is_stale)
        self.repo.update_criterion(criterion.id, CriterionUpdate(description="Новый аспект"))
        freshness = self.repo.analysis_freshness(response.id)
        self.assertEqual(freshness.criteria_version, 2)
        self.assertTrue(freshness.match_is_stale)
        self.assertTrue(freshness.report_is_stale)
        for version in (1, 3):
            with self.subTest(version=version), self.assertRaises(StaleCriteriaError):
                self.repo.save_match_result(response.id, MatchResultWrite(criteria_version=version, match_status="red"))
            with self.subTest(version=version), self.assertRaises(StaleCriteriaError):
                self.repo.save_report(response.id, ReportWrite(criteria_version=version))
        saved = self.repo.get_match_result(response.id)
        assert saved is not None
        self.assertEqual(saved.match_status, "yellow")
        with self.assertRaises(StaleCriteriaError):
            self.repo.replace_questions(response.id, [], criteria_version=2)
        self.save_analysis(vacancy, response, criterion)
        self.assertFalse(self.repo.analysis_freshness(response.id).match_is_stale)

    def test_response_workflow_and_archive_are_independent_of_matching(self):
        vacancy, response, criterion = self.seed()
        self.save_analysis(vacancy, response, criterion)
        self.repo.update_response(response.id, ResponseUpdate(workflow_status="forwarded", is_archived=True))
        saved = self.repo.get_match_result(response.id)
        assert saved is not None
        self.assertEqual(saved.match_status, "yellow")
        self.assertEqual(len(self.repo.list_responses(vacancy.id, workflow_status="forwarded", is_archived=True)), 1)
        self.assertEqual(self.repo.list_responses(vacancy.id, is_archived=False), [])
        self.repo.update_response(response.id, ResponseUpdate(is_archived=False, candidate_name=None))
        self.assertIsNotNone(self.repo.get_report(response.id))

    def test_response_sorting_scores_and_color_filter(self):
        vacancy, first, _ = self.seed()
        second = self.repo.create_response(ResponseCreate(vacancy_id=vacancy.id, source_json={}))
        third = self.repo.create_response(ResponseCreate(vacancy_id=vacancy.id, source_json={}))
        self.repo.save_match_result(first.id, MatchResultWrite(criteria_version=1, match_score=Decimal(25), match_status="red"))
        self.repo.save_match_result(second.id, MatchResultWrite(criteria_version=1, match_score=Decimal(90), match_status="green"))
        self.assertEqual([r.id for r in self.repo.list_responses(vacancy.id)], [second.id, first.id, third.id])
        self.assertEqual([r.id for r in self.repo.list_responses(vacancy.id, match_status="red")], [first.id])

    def test_tasks_validate_targets_and_store_state(self):
        vacancy, response, _ = self.seed()
        task = self.repo.create_task(TaskCreate(task_type="questions", vacancy_id=vacancy.id, response_id=response.id))
        self.assertEqual(TaskRead.model_validate(task).status, "pending")
        self.repo.update_task(task.id, "completed")
        self.assertIsNotNone(task.completed_at)
        self.repo.update_task(task.id, "failed", error_message="Ошибка обработки")
        self.assertEqual(task.error_message, "Ошибка обработки")
        self.repo.update_task(task.id, "processing")
        self.assertIsNone(task.completed_at)
        self.assertIsNone(task.error_message)
        self.assertEqual(len(self.repo.list_tasks(vacancy_id=vacancy.id, status="processing")), 1)
        other = self.repo.create_vacancy(VacancyCreate(title="Other"))
        with self.assertRaises(ValueError):
            self.repo.create_task(TaskCreate(task_type="matching", vacancy_id=other.id, response_id=response.id))
        self.repo.delete_task(task.id)
        with self.assertRaises(NotFoundError):
            self.repo.get_task(task.id)

    def test_cross_vacancy_criteria_are_rejected_before_changes(self):
        vacancy, response, _ = self.seed()
        other = self.repo.create_vacancy(VacancyCreate(title="Other", criteria=[CriterionCreate(name="Foreign")]))
        foreign = other.criteria[0]
        with self.assertRaises(ValueError):
            self.repo.update_criteria(vacancy.id, {foreign.id: CriterionUpdate(importance="required")})
        self.assertEqual(vacancy.criteria_version, 1)
        with self.assertRaises(ValueError):
            self.repo.save_match_result(response.id, MatchResultWrite(criteria_version=1, match_status="yellow",
                evaluations=[EvaluationWrite(criterion_id=foreign.id, status="unknown")]))
        with self.assertRaises(ValueError):
            self.repo.save_report(response.id, ReportWrite(criteria_version=1,
                questions=[QuestionCreate(criterion_id=foreign.id, question="Вопрос?")]))
        self.assertIsNone(self.repo.get_match_result(response.id))
        self.assertIsNone(self.repo.get_report(response.id))

    def test_delete_criterion_sets_question_fk_to_null_and_invalidates_versions(self):
        vacancy, response, criterion = self.seed()
        self.save_analysis(vacancy, response, criterion)
        self.repo.delete_criterion(criterion.id)
        report = self.repo.get_report(response.id)
        assert report is not None
        self.assertTrue(all(q.criterion_id is None for q in report.questions))
        self.assertEqual(self.session.scalar(select(func.count()).select_from(CriterionEvaluation)), 0)
        self.assertTrue(self.repo.analysis_freshness(response.id).report_is_stale)

    def test_delete_vacancy_cascades_through_all_eight_tables(self):
        vacancy, response, criterion = self.seed()
        self.save_analysis(vacancy, response, criterion)
        self.repo.create_task(TaskCreate(task_type="matching", vacancy_id=vacancy.id))
        self.repo.delete_vacancy(vacancy.id)
        for table in Base.metadata.sorted_tables:
            self.assertEqual(self.session.scalar(select(func.count()).select_from(table)), 0, table.name)

    def test_replace_questions_and_delete_report_or_result(self):
        vacancy, response, criterion = self.seed()
        self.save_analysis(vacancy, response, criterion)
        self.repo.replace_questions(response.id, [QuestionCreate(question="Новый вопрос?")], criteria_version=1)
        report = self.repo.get_report(response.id)
        assert report is not None
        self.assertEqual(len(report.questions), 1)
        self.repo.delete_match_result(response.id)
        self.assertIsNone(self.repo.get_match_result(response.id))
        self.assertIsNotNone(self.repo.get_report(response.id))
        self.repo.delete_report(response.id)
        self.assertIsNone(self.repo.get_report(response.id))
        self.assertEqual(self.session.scalar(select(func.count()).select_from(Question)), 0)
        self.repo.delete_response(response.id)
        with self.assertRaises(NotFoundError):
            self.repo.get_response(response.id)

    def test_database_checks_are_enforced_without_pydantic(self):
        vacancy, response, _ = self.seed()
        rows = [Vacancy(title="bad", status="bad"), Vacancy(title="bad", criteria_version=0),
                Response(vacancy_id=vacancy.id, source_json={}, workflow_status="bad"),
                AnalysisTask(task_type="matching"),
                VacancyCriterion(vacancy_id=vacancy.id, name="SQL", importance="bad"),
                MatchResult(response_id=response.id, match_score=101, match_status="green", criteria_version=1),
                MatchResult(response_id=response.id, match_status="blue", criteria_version=1),
                Report(response_id=response.id, criteria_version=0)]
        for row in rows:
            with self.subTest(row=type(row).__name__), self.assertRaises(IntegrityError):
                with self.session.begin_nested():
                    self.session.add(row)
                    self.session.flush()

    def test_database_uniqueness_and_foreign_keys(self):
        vacancy, response, criterion = self.seed()
        match, _ = self.save_analysis(vacancy, response, criterion)
        duplicates = [MatchResult(response_id=response.id, match_status="green", criteria_version=1),
                      Report(response_id=response.id, criteria_version=1),
                      CriterionEvaluation(match_result_id=match.id, criterion_id=criterion.id, status="matched"),
                      Response(vacancy_id=uuid4(), source_json={})]
        for row in duplicates:
            with self.subTest(row=type(row).__name__), self.assertRaises(IntegrityError):
                with self.session.begin_nested():
                    self.session.add(row)
                    self.session.flush()

    def test_session_scope_commits_and_rolls_back(self):
        with session_scope(self.factory) as session:
            identifier = Repository(session).create_vacancy(VacancyCreate(title="Committed")).id
        try:
            with session_scope(self.factory) as session:
                self.assertEqual(Repository(session).get_vacancy(identifier).title, "Committed")
            rolled_back = None
            with self.assertRaises(RuntimeError):
                with session_scope(self.factory) as session:
                    rolled_back = Repository(session).create_vacancy(VacancyCreate(title="Rolled back")).id
                    raise RuntimeError("abort")
            assert rolled_back is not None
            with session_scope(self.factory) as session, self.assertRaises(NotFoundError):
                Repository(session).get_vacancy(rolled_back)
        finally:
            with session_scope(self.factory) as session:
                Repository(session).delete_vacancy(identifier)

    def test_concurrent_criteria_updates_do_not_lose_version_increments(self):
        with session_scope(self.factory) as session:
            row = Repository(session).create_vacancy(VacancyCreate(title="Concurrent", criteria=[
                CriterionCreate(name="A"), CriterionCreate(name="B")]))
            identifier, criterion_ids = row.id, [criterion.id for criterion in row.criteria]
        barrier = Barrier(2)

        def change(criterion_id):
            with session_scope(self.factory) as session:
                repo = Repository(session)
                repo.get_vacancy(identifier)  # Cache the old version in both sessions.
                barrier.wait(timeout=10)
                repo.update_criteria(identifier, {criterion_id: CriterionUpdate(importance="required")})

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(change, criterion_ids))
            with session_scope(self.factory) as session:
                row = Repository(session).get_vacancy(identifier)
                self.assertEqual(row.criteria_version, 3)
                self.assertTrue(all(criterion.importance == "required" for criterion in row.criteria))
        finally:
            with session_scope(self.factory) as session:
                Repository(session).delete_vacancy(identifier)

    def test_response_publication_uses_latest_source_after_an_earlier_read(self):
        with session_scope(self.factory) as session:
            repo = Repository(session)
            vacancy_id = repo.create_vacancy(VacancyCreate(title="Concurrent source")).id
            response_id = repo.create_response(ResponseCreate(vacancy_id=vacancy_id, source_json={"revision": 1})).id
        try:
            with session_scope(self.factory) as first:
                repo = Repository(first)
                cached = repo.get_response(response_id)
                with session_scope(self.factory) as second:
                    Repository(second).update_response(response_id, ResponseUpdate(source_json={"revision": 2}))
                repo.save_match_result(response_id, MatchResultWrite(criteria_version=1, match_status="red"))
                self.assertEqual(cached.source_json, {"revision": 2})
        finally:
            with session_scope(self.factory) as session:
                Repository(session).delete_vacancy(vacancy_id)

    def test_migration_downgrade_and_upgrade_in_separate_schema(self):
        schema = "hr_storage_" + uuid4().hex
        with self.base_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        try:
            with self.base_engine.begin() as connection:
                connection = connection.execution_options(schema_translate_map={None: schema})
                config = migration_config(connection, schema)
                command.upgrade(config, "head")
                self.assertEqual(set(inspect(connection).get_table_names(schema=schema)), TABLES | {"alembic_version"})
                command.downgrade(config, "base")
                self.assertEqual(inspect(connection).get_table_names(schema=schema), ["alembic_version"])
                command.upgrade(config, "head")
                self.assertEqual(connection.scalar(text(f'SELECT count(*) FROM "{schema}".vacancies')), 0)
        finally:
            assert re.fullmatch(r"hr_storage_[0-9a-f]{32}", schema)
            with self.base_engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))

    def test_mvp_migration_preserves_old_status_without_guessing_decision(self):
        schema = "hr_storage_" + uuid4().hex
        with self.base_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        try:
            with self.base_engine.begin() as connection:
                connection = connection.execution_options(schema_translate_map={None: schema})
                config = migration_config(connection, schema)
                command.upgrade(config, "0001")
                vacancy_id = connection.scalar(text(f'INSERT INTO "{schema}".vacancies (title) VALUES (\'Legacy\') RETURNING id'))
                connection.execute(text(f'INSERT INTO "{schema}".responses (vacancy_id, source_json, workflow_status) '
                                        "VALUES (:vacancy, '{}'::jsonb, 'contacted')"), {"vacancy": vacancy_id})
                command.upgrade(config, "head")
                row = connection.execute(text(f'SELECT workflow_status, legacy_workflow_status, is_viewed FROM "{schema}".responses')).one()
                self.assertEqual(tuple(row), ("new", "contacted", False))
                command.downgrade(config, "0001")
                self.assertEqual(connection.scalar(text(f'SELECT workflow_status FROM "{schema}".responses')), "contacted")
        finally:
            assert re.fullmatch(r"hr_storage_[0-9a-f]{32}", schema)
            with self.base_engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))


if __name__ == "__main__":
    unittest.main()
