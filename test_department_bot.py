"""No real Telegram messages. DB tests run only in an isolated test schema."""
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import NAMESPACE_URL, uuid4, uuid5

from alembic import command
import httpx
from sqlalchemy import delete
from sqlalchemy.schema import CreateSchema, DropSchema

from department_bot.__main__ import Cursor, poll
from department_bot.controller import BotController, button
from department_bot.service import AccessDeniedError, ConfirmationExpiredError, DepartmentService, Item
from department_bot.settings import BotSettings
from department_bot.telegram import TelegramClient, TelegramError
from department_bot.materials import MaterialsNotReadyError
from hr_api.service import HRService
from hr_api.pdf_reports import INDEX_NAME, PdfEntry, PdfIndex, PdfReports, source_digest
from contract_models import Contacts, ContractReport, RecruiterRecommendation
from storage.crud import NotFoundError, Repository
from storage.database import DatabaseSettings, make_engine, make_session_factory
from storage.models import Report, Response, Vacancy
from storage.schemas import ResponseCreate, ResponseUpdate, VacancyCreate
from test_storage import migration_config

ACTOR = 123456789
SECRET = "123456789:TEST_TOKEN_FOR_MOCKS_ONLY_123456789"


class RecordingTelegram:
    def __init__(self):
        self.screens = []
        self.answers = []
        self.documents = []

    def screen(self, chat_id, text, rows, message_id=None):
        self.screens.append((chat_id, text, rows, message_id))

    def answer(self, callback_id, text=None):
        self.answers.append((callback_id, text))

    def document(self, chat_id, filename, content, mime_type, caption):
        self.documents.append((chat_id, filename, content, mime_type, caption))


def message(text="/start", actor=ACTOR, chat_type="private"):
    return {"update_id": 1, "message": {"message_id": 10, "from": {"id": actor},
            "chat": {"id": actor, "type": chat_type}, "text": text}}


def callback(data, actor=ACTOR):
    return {"update_id": 2, "callback_query": {"id": "callback-test", "from": {"id": actor},
        "data": data, "message": message()["message"]}}


class BotUnitTests(unittest.TestCase):
    def test_document_is_uploaded_as_multipart_without_truncating_raw_text(self):
        requests = []
        raw = ("Резюме\r\n" + "Навыки Python <script>\n" * 2000).encode("utf-8")

        def transport(request):
            requests.append(request)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        client = TelegramClient(SECRET, transport=httpx.MockTransport(transport))
        try:
            client.document(ACTOR, "resume-test.txt", raw, "text/plain; charset=utf-8", "Исходное резюме")
            self.assertIn("multipart/form-data", requests[0].headers["Content-Type"])
            self.assertIn(raw, requests[0].content)
            self.assertIn(b'filename="resume-test.txt"', requests[0].content)
            self.assertTrue(requests[0].url.path.endswith("sendDocument"))
        finally:
            client.close()

    def test_document_errors_hide_credentials_and_are_retriable(self):
        client = TelegramClient(SECRET, transport=httpx.MockTransport(lambda _: httpx.Response(
            429, json={"ok": False, "description": SECRET, "parameters": {"retry_after": 3}})))
        try:
            with self.assertRaises(TelegramError) as failure:
                client.document(ACTOR, "x.txt", b"private resume", "text/plain", "Resume")
            self.assertNotIn(SECRET, str(failure.exception))
            self.assertEqual(failure.exception.retry_after, 3)
        finally:
            client.close()

    def test_settings_literal_file_override_and_hidden_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(f"TELEGRAM_BOT_TOKEN={SECRET}\nTELEGRAM_MANAGER_IDS={ACTOR}\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                settings = BotSettings.from_env(path)
                self.assertEqual(settings.manager_ids, {ACTOR})
                self.assertNotIn(SECRET, repr(settings))
                self.assertNotIn("TELEGRAM_BOT_TOKEN", os.environ)
            with patch.dict(os.environ, {"TELEGRAM_MANAGER_IDS": "7,8"}, clear=True):
                self.assertEqual(BotSettings.from_env(path).manager_ids, {7, 8})
            for value in ("-1", "0", "name", "9223372036854775808"):
                with patch.dict(os.environ, {"TELEGRAM_MANAGER_IDS": value}, clear=True), self.assertRaises(ValueError):
                    BotSettings.from_env(path)
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "TELEGRAM_BOT_TOKEN"):
            BotSettings.from_env(None)
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "PRIVATE_INVALID_TOKEN"}, clear=True):
            with self.assertRaises(ValueError) as failure:
                BotSettings.from_env(None)
            self.assertNotIn("PRIVATE_INVALID_TOKEN", str(failure.exception))

    def test_telegram_json_transport_and_unchanged_edit(self):
        seen = []

        def transport(request):
            seen.append(json.loads(request.content))
            if request.url.path.endswith("editMessageText"):
                return httpx.Response(400, json={"ok": False, "description": "Bad Request: message is not modified"})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 10}})

        client = TelegramClient(SECRET, transport=httpx.MockTransport(transport))
        try:
            self.assertEqual(client.screen(ACTOR, "<script>plain text</script>", []), {"message_id": 10})
            self.assertIsNone(client.screen(ACTOR, "same", [], 10))
            self.assertNotIn("parse_mode", seen[0])
            self.assertEqual(seen[0]["reply_markup"], {"inline_keyboard": []})
        finally:
            client.close()

    def test_telegram_network_and_provider_errors_never_expose_token(self):
        def broken(request):
            raise httpx.ConnectError(f"PRIVATE_ERROR {request.url}")

        for transport, code, delay in (
            (broken, 0, 5),
            (lambda _: httpx.Response(429, json={"ok": False, "description": SECRET, "parameters": {"retry_after": 12}}), 429, 12),
            (lambda _: httpx.Response(503, text=SECRET), 0, 5),
        ):
            client = TelegramClient(SECRET, transport=httpx.MockTransport(transport))
            try:
                with self.assertRaises(TelegramError) as failure:
                    client.call("getMe")
                self.assertEqual(failure.exception.code, code)
                self.assertEqual(failure.exception.retry_after, delay)
                self.assertNotIn(SECRET, str(failure.exception))
                self.assertNotIn("PRIVATE_ERROR", str(failure.exception))
            finally:
                client.close()

    def test_id_command_and_group_do_not_read_database(self):
        service, telegram = Mock(), RecordingTelegram()
        controller = BotController(service, telegram)
        controller.handle(message("/id"))
        self.assertIn(str(ACTOR), telegram.screens[0][1])
        service.assert_not_called()
        self.assertEqual(service.mock_calls, [])
        controller.handle(message(chat_type="group"))
        self.assertEqual(len(telegram.screens), 1)
        self.assertEqual(service.mock_calls, [])

    def test_denied_callback_cannot_query_records(self):
        service, telegram = Mock(), RecordingTelegram()
        service.authorize.side_effect = AccessDeniedError("Доступ закрыт")
        BotController(service, telegram).handle(callback(f"r:{uuid4().hex}"))
        service.get.assert_not_called()
        service.confirm.assert_not_called()
        self.assertEqual(len(telegram.answers), 1)
        self.assertIn("Доступ закрыт", telegram.screens[0][1])

    def test_confirmation_menu_does_not_mutate_and_fits_callback_limit(self):
        service, telegram = Mock(), RecordingTelegram()
        item = Item(uuid4(), "<b>Кандидат</b>", False, "1", uuid4())
        service.confirmation.return_value = item, "!signed"
        controller = BotController(service, telegram)
        controller.handle(callback(f"ask:r:{item.id.hex}:1"))
        service.confirm.assert_not_called()
        self.assertEqual(len(telegram.answers), 1)
        self.assertIn("независимо от результата", telegram.screens[-1][1])
        for row in telegram.screens[-1][2]:
            for value in row:
                self.assertLessEqual(len(value["callback_data"].encode()), 64)
        with self.assertRaises(ValueError):
            button("x", "я" * 33)

    def test_cursor_roundtrip_rejects_other_bot_and_poll_saves_after_handling(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "offset.json"
            cursor = Cursor(path, 42)
            self.assertIsNone(cursor.offset)
            cursor.save(9)
            self.assertEqual(Cursor(path, 42).offset, 9)
            with self.assertRaises(ValueError):
                Cursor(path, 43)
            stop = threading.Event()
            client, controller = Mock(), Mock()
            client.call.return_value = [{"update_id": 9, "message": {}}]

            def handle(_):
                self.assertEqual(Cursor(path, 42).offset, 9)
                stop.set()

            controller.handle.side_effect = handle
            poll(client, controller, cursor, stop)
            self.assertEqual(Cursor(path, 42).offset, 10)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL for department PostgreSQL tests")
class DepartmentDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_engine = make_engine(DatabaseSettings.from_env(None, variable="TEST_DATABASE_URL"))
        cls.schema = "hr_bot_" + uuid4().hex
        with cls.base_engine.begin() as connection:
            connection.execute(CreateSchema(cls.schema))
        cls.engine = cls.base_engine.execution_options(schema_translate_map={None: cls.schema})
        with cls.engine.begin() as connection:
            command.upgrade(migration_config(connection, cls.schema), "head")
        cls.factory = make_session_factory(cls.engine)

    @classmethod
    def tearDownClass(cls):
        assert re.fullmatch(r"hr_bot_[0-9a-f]{32}", cls.schema)
        with cls.base_engine.begin() as connection:
            connection.execute(DropSchema(cls.schema, cascade=True))
        cls.base_engine.dispose()

    def setUp(self):
        self.service = DepartmentService(self.factory, frozenset({ACTOR}), SECRET)
        with self.factory.begin() as session:
            repo = Repository(session)
            self.vacancy_id = repo.create_vacancy(VacancyCreate(title="Тестовая вакансия")).id
            self.response_id = repo.create_response(ResponseCreate(vacancy_id=self.vacancy_id,
                candidate_name="Демо Кандидат", source_json={"source_text": "PRIVATE_RESUME", "skills": ["Python"]},
                workflow_status="forwarded", is_viewed=True, is_archived=True)).id
            self.hidden_id = repo.create_response(ResponseCreate(vacancy_id=self.vacancy_id,
                candidate_name="PRIVATE_NOT_FORWARDED", source_json={})).id
            contract = ContractReport(id=str(self.response_id), vacancy_id=str(self.vacancy_id),
                candidate_name="Демо Кандидат", contacts=Contacts(), match_status="green", match_label="Подходит",
                match_share=1, summary=("Python подтверждён", "Опыт указан", "Образование указано"),
                skills=[], missing_data=[], workflow_status="forwarded", source_text="PRIVATE_RESUME")
            session.add(Report(response_id=self.response_id, criteria_version=1, contract_json=contract.model_dump(mode="json")))

    def tearDown(self):
        with self.factory.begin() as session:
            session.execute(delete(Vacancy))

    def change(self, kind, identifier, closed):
        _, token = self.service.confirmation(ACTOR, kind, identifier, closed)
        self.assertLessEqual(len(token.encode()), 64)
        return self.service.confirm(ACTOR, token)[1]

    def test_forwarded_only_in_lists_detail_and_mutation_and_empty_allowlist(self):
        page = self.service.responses(ACTOR, self.vacancy_id, False)
        self.assertEqual([item.id for item in page.items], [self.response_id])
        for operation in (
            lambda: self.service.get(ACTOR, "r", self.hidden_id),
            lambda: self.service.confirmation(ACTOR, "r", self.hidden_id, True),
        ):
            with self.assertRaises(NotFoundError):
                operation()
        for actor in (0, ACTOR + 1):
            with self.assertRaises(AccessDeniedError):
                self.service.vacancies(actor, False)
        with self.assertRaises(AccessDeniedError):
            DepartmentService(self.factory, frozenset(), SECRET).get(ACTOR, "v", self.vacancy_id)

    def test_response_close_is_neutral_audited_and_does_not_change_hr_data(self):
        self.change("r", self.response_id, True)
        with self.factory.begin() as session:
            row = Repository(session).get_response(self.response_id)
            self.assertTrue(row.is_closed)
            self.assertTrue(row.is_viewed)
            self.assertTrue(row.is_archived)
            self.assertEqual(row.workflow_status, "forwarded")
            self.assertEqual(row.source_json, {"source_text": "PRIVATE_RESUME", "skills": ["Python"]})
            self.assertEqual(row.closed_by_telegram_id, ACTOR)
            original_time = row.closed_at
            self.assertIsNotNone(original_time)
            self.assertEqual(row.vacancy.status, "active")
        self.change("r", self.response_id, True)
        self.assertEqual(self.service.get(ACTOR, "r", self.response_id).closed_at, original_time)
        self.assertEqual(self.service.responses(ACTOR, self.vacancy_id, False).total, 0)
        self.assertEqual(self.service.responses(ACTOR, self.vacancy_id, True).total, 1)
        self.change("r", self.response_id, False)
        with self.factory.begin() as session:
            row = Repository(session).get_response(self.response_id)
            self.assertFalse(row.is_closed)
            self.assertIsNone(row.closed_at)
            self.assertIsNone(row.closed_by_telegram_id)

    def test_vacancy_close_and_reopen_do_not_close_or_reopen_responses(self):
        with self.assertRaises(ValueError):
            self.service.confirmation(ACTOR, "v", self.vacancy_id, True)
        hr = HRService(self.factory)
        hr.update_vacancy_status(self.vacancy_id, "closed")
        self.assertEqual(self.service.vacancies(ACTOR, False).total, 0)
        self.assertEqual(self.service.vacancies(ACTOR, True).total, 1)
        self.assertFalse(self.service.get(ACTOR, "r", self.response_id).is_closed)
        self.change("r", self.response_id, True)
        hr.update_vacancy_status(self.vacancy_id, "active")
        self.assertTrue(self.service.get(ACTOR, "r", self.response_id).is_closed)

    def test_hire_is_candidate_specific_does_not_close_vacancy_and_can_be_corrected(self):
        _, token = self.service.confirmation(ACTOR, "r", self.response_id, True, hiring=True)
        self.service.confirm(ACTOR, token)
        with self.factory.begin() as session:
            row = Repository(session).get_response(self.response_id)
            self.assertEqual(row.hired_by_telegram_id, ACTOR)
            self.assertIsNotNone(row.hired_at)
            self.assertTrue(row.is_closed)
            self.assertEqual(row.vacancy.status, "active")
            self.assertIsNone(Repository(session).get_response(self.hidden_id).hired_at)
        with self.assertRaises(ConfirmationExpiredError):
            self.service.confirm(ACTOR, token)
        _, undo = self.service.confirmation(ACTOR, "r", self.response_id, False, hiring=True)
        self.service.confirm(ACTOR, undo)
        item = self.service.get(ACTOR, "r", self.response_id)
        self.assertIsNone(item.hired_at)
        self.assertTrue(item.is_closed)

    def test_summary_delivery_and_retry_are_persistent_without_real_messages(self):
        hr = HRService(self.factory)
        recommendation = RecruiterRecommendation(strengths="SQL", weaknesses="Git", interview_focus="Ветки Git")
        hr.update_recommendation(self.response_id, recommendation)
        _, confirmation = self.service.confirmation(ACTOR, "r", self.response_id, True, hiring=True)
        telegram = RecordingTelegram()
        controller = BotController(self.service, telegram)
        controller.deliver_pending()
        self.assertEqual(len(telegram.screens), 1)
        self.assertEqual(len(telegram.documents), 2)
        self.assertEqual(telegram.documents[1][2], b"PRIVATE_RESUME")
        self.assertIn("Сильные стороны: SQL", telegram.screens[0][1])
        self.assertIn("Слабые места: Git", telegram.screens[0][1])
        self.assertIn("Ветки Git", telegram.screens[0][1])
        controller.deliver_pending()
        self.assertEqual(len(telegram.screens), 1)
        # A delivery acknowledgment must not invalidate a hiring confirmation.
        self.service.confirm(ACTOR, confirmation)
        recommendation.weaknesses = "Недостаточно примеров Git"
        hr.update_recommendation(self.response_id, recommendation)
        failed = Mock()
        failed.screen.side_effect = TelegramError()
        with self.assertRaises(TelegramError):
            BotController(self.service, failed).deliver_pending()
        self.assertEqual(len(self.service.pending_notifications(ACTOR)), 1)
        controller.deliver_pending()
        self.assertEqual(self.service.pending_notifications(ACTOR), [])

    def test_materials_require_hr_approval_and_recommendations(self):
        with self.assertRaises(MaterialsNotReadyError):
            self.service.materials(ACTOR, self.response_id)
        with self.factory.begin() as session:
            row = Repository(session).get_response(self.response_id)
            row.department_notification_pending = True
        self.assertEqual(self.service.pending_notifications(ACTOR), [])
        self.assertEqual(RecordingTelegram().documents, [])
        recommendation = RecruiterRecommendation(strengths="SQL", weaknesses="Git", interview_focus="Практика")
        hr = HRService(self.factory)
        hr.update_recommendation(self.hidden_id, recommendation)
        with self.assertRaises(NotFoundError):
            self.service.materials(ACTOR, self.hidden_id)
        with self.assertRaises(AccessDeniedError):
            self.service.materials(ACTOR + 1, self.response_id)

    def test_partial_delivery_resumes_after_restart_and_repeated_calls_do_not_duplicate(self):
        recommendation = RecruiterRecommendation(strengths="Python", weaknesses="SQL", interview_focus="Практика")
        HRService(self.factory).update_recommendation(self.response_id, recommendation)
        telegram = RecordingTelegram()
        original = telegram.document

        def fail_on_resume(chat_id, filename, content, mime_type, caption):
            if filename.startswith("resume-"):
                raise TelegramError(503)
            original(chat_id, filename, content, mime_type, caption)

        telegram.document = fail_on_resume
        with self.assertRaises(TelegramError):
            BotController(self.service, telegram).deliver_pending()
        self.assertEqual(len(telegram.screens), 1)
        self.assertEqual(len(telegram.documents), 1)
        self.assertEqual(len(self.service.pending_notifications(ACTOR)), 1)
        restarted = DepartmentService(self.factory, frozenset({ACTOR}), SECRET)
        continued = RecordingTelegram()
        BotController(restarted, continued).deliver_pending()
        self.assertEqual(continued.screens, [])
        self.assertEqual(len(continued.documents), 1)
        self.assertTrue(continued.documents[0][1].startswith("resume-"))
        self.assertEqual(restarted.pending_notifications(ACTOR), [])
        BotController(restarted, continued).deliver_pending()
        BotController(restarted, continued).handle(callback(f"files:{self.response_id.hex}"))
        self.assertEqual(len(continued.documents), 1)

    def test_current_report_txt_and_full_raw_resume_are_sent_without_model_calls(self):
        raw = "Резюме\r\n" + "Очень длинный исходный текст\n" * 1000
        with self.factory.begin() as session:
            row = Repository(session).get_response(self.response_id)
            row.source_json = {**row.source_json, "source_text": raw}
        hr = HRService(self.factory)
        hr.update_recommendation(self.response_id, RecruiterRecommendation(strengths="Python", weaknesses="SQL", interview_focus="Практика"))
        telegram = RecordingTelegram()
        BotController(self.service, telegram).deliver_pending()
        self.assertEqual(len(telegram.documents), 2)
        report, resume = telegram.documents
        self.assertTrue(report[1].endswith(".txt"))
        self.assertIn("Python подтверждён", report[2].decode("utf-8"))
        self.assertIn("Сильные стороны: Python", report[2].decode("utf-8"))
        self.assertEqual(resume[2], raw.encode("utf-8"))

    def test_only_strengths_and_weaknesses_are_required_to_send_materials(self):
        HRService(self.factory).update_recommendation(self.response_id,
            RecruiterRecommendation(strengths="Python", weaknesses="SQL"))
        telegram = RecordingTelegram()
        BotController(self.service, telegram).deliver_pending()
        self.assertEqual(len(telegram.documents), 2)
        self.assertNotIn("None", telegram.screens[0][1])
        self.assertNotIn("None", telegram.documents[0][2].decode("utf-8"))
        self.assertEqual(self.service.pending_notifications(ACTOR), [])

    def test_pdf_is_sent_for_matching_resume_and_stale_pdf_uses_current_report(self):
        key = "bot-pdf-test"
        vacancy_id = uuid5(NAMESPACE_URL, "hr-platform/vacancies/" + key)
        response_id = uuid5(NAMESPACE_URL, f"hr-platform/responses/{key}/1")
        source = {"candidate_id": 1, "source_text": "PDF candidate original"}
        with self.factory.begin() as session:
            session.add(Vacancy(id=vacancy_id, title="PDF vacancy", source_json={}))
            session.flush()
            session.add(Response(id=response_id, vacancy_id=vacancy_id, source_json=source, workflow_status="forwarded",
                recruiter_recommendation={"strengths": "Python", "weaknesses": "SQL", "interview_focus": "Практика"}))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = b"%PDF-1.4\nSynthetic\n%%EOF"
            (root / "report-1.pdf").write_bytes(pdf)
            index = PdfIndex(source_key=key, vacancy_title="PDF vacancy", entries=[
                PdfEntry(candidate_id="1", filename="report-1.pdf", source_sha256=source_digest(source))])
            (root / INDEX_NAME).write_text(index.model_dump_json(), encoding="utf-8")
            service = DepartmentService(self.factory, frozenset({ACTOR}), SECRET, PdfReports(root))
            materials = service.materials(ACTOR, response_id)
            self.assertEqual(materials.report.content, pdf)
            self.assertEqual(materials.report.mime_type, "application/pdf")
            with self.factory.begin() as session:
                row = Repository(session).get_response(response_id)
                row.source_json = {**source, "source_text": "Edited"}
                template = Repository(session).get_response(self.response_id).report
                assert template is not None and template.contract_json is not None
                session.add(Report(response_id=response_id, criteria_version=1, contract_json={
                    **template.contract_json, "id": str(response_id), "vacancy_id": str(vacancy_id)}))
            refreshed = service.materials(ACTOR, response_id)
            self.assertTrue(refreshed.report.filename.endswith(".txt"))
            self.assertEqual(refreshed.resume.content, b"Edited")

    def test_missing_report_or_raw_resume_does_not_acknowledge_delivery(self):
        HRService(self.factory).update_recommendation(self.response_id,
            RecruiterRecommendation(strengths="Python", weaknesses="SQL", interview_focus="Практика"))
        with self.factory.begin() as session:
            report = Repository(session).get_response(self.response_id).report
            assert report is not None
            report.contract_json = None
        telegram = RecordingTelegram()
        BotController(self.service, telegram).deliver_pending()
        self.assertEqual(telegram.documents, [])
        self.service.acknowledge_notification(ACTOR, self.response_id, 1)
        with self.factory.begin() as session:
            self.assertTrue(Repository(session).get_response(self.response_id).department_notification_pending)
        with patch("department_bot.service.time.time", return_value=9999999999):
            self.assertEqual(len(self.service.pending_notifications(ACTOR)), 1)

    def test_revoke_after_first_file_stops_remaining_materials(self):
        hr = HRService(self.factory)
        hr.update_recommendation(self.response_id, RecruiterRecommendation(strengths="Python", weaknesses="SQL", interview_focus="Практика"))
        telegram = RecordingTelegram()
        original = telegram.document

        def revoke(chat_id, filename, content, mime_type, caption):
            original(chat_id, filename, content, mime_type, caption)
            hr.update_status(self.response_id, "not_forwarded")

        telegram.document = revoke
        BotController(self.service, telegram).deliver_pending()
        self.assertEqual(len(telegram.documents), 1)
        self.assertTrue(telegram.documents[0][1].startswith("report-"))
        self.assertEqual(self.service.pending_notifications(ACTOR), [])

    def test_confirmation_replay_tampering_other_actor_expiry_and_changed_record(self):
        _, token = self.service.confirmation(ACTOR, "r", self.response_id, True)
        with self.assertRaises(ConfirmationExpiredError):
            self.service.confirm(ACTOR, token[:-1] + ("A" if token[-1] != "A" else "B"))
        other = DepartmentService(self.factory, frozenset({ACTOR + 1}), SECRET)
        with self.assertRaises(ConfirmationExpiredError):
            other.confirm(ACTOR + 1, token)
        with patch("department_bot.service.time.time", return_value=9999999999), self.assertRaises(ConfirmationExpiredError):
            self.service.confirm(ACTOR, token)
        self.service.confirm(ACTOR, token)
        self.change("r", self.response_id, False)
        with self.assertRaises(ConfirmationExpiredError):
            self.service.confirm(ACTOR, token)
        self.assertFalse(self.service.get(ACTOR, "r", self.response_id).is_closed)
        _, fresh = self.service.confirmation(ACTOR, "r", self.response_id, True)
        with self.factory.begin() as session:
            Repository(session).update_response(self.response_id, ResponseUpdate(is_viewed=False))
        with self.assertRaises(ConfirmationExpiredError):
            self.service.confirm(ACTOR, fresh)

    def test_hr_revoke_between_confirmation_and_submit_blocks_close(self):
        _, token = self.service.confirmation(ACTOR, "r", self.response_id, True)
        with self.factory.begin() as session:
            Repository(session).update_response(self.response_id, ResponseUpdate(workflow_status="not_forwarded"))
        with self.assertRaises(NotFoundError):
            self.service.confirm(ACTOR, token)
        with self.factory.begin() as session:
            self.assertFalse(Repository(session).get_response(self.response_id).is_closed)

    def test_pagination_and_drafts(self):
        with self.factory.begin() as session:
            repo = Repository(session)
            for index in range(13):
                repo.create_vacancy(VacancyCreate(title=f"Extra {index}"))
            draft = repo.create_vacancy(VacancyCreate(title="Draft", status="draft")).id
        page = self.service.vacancies(ACTOR, False)
        self.assertEqual((page.total, len(page.items)), (14, 6))
        self.assertEqual(len(self.service.vacancies(ACTOR, False, 100).items), 2)
        with self.assertRaises(NotFoundError):
            self.service.get(ACTOR, "v", draft)

    def test_menu_confirmation_and_cancel_end_to_end_without_real_telegram(self):
        telegram = RecordingTelegram()
        controller = BotController(self.service, telegram)
        controller.handle(message())
        self.assertIn("Текущие вакансии", telegram.screens[-1][1])
        controller.handle(callback(f"rl:{self.vacancy_id.hex}:0:0"))
        rendered = json.dumps(telegram.screens[-1], ensure_ascii=False)
        self.assertIn("Демо Кандидат", rendered)
        self.assertNotIn("PRIVATE_NOT_FORWARDED", rendered)
        self.assertNotIn("PRIVATE_RESUME", rendered)
        controller.handle(callback(f"ask:r:{self.response_id.hex}:1"))
        self.assertFalse(self.service.get(ACTOR, "r", self.response_id).is_closed)
        confirm_button = telegram.screens[-1][2][0][0]["callback_data"]
        cancel_button = telegram.screens[-1][2][1][0]["callback_data"]
        controller.handle(callback(cancel_button))
        self.assertFalse(self.service.get(ACTOR, "r", self.response_id).is_closed)
        controller.handle(callback(confirm_button))
        self.assertTrue(self.service.get(ACTOR, "r", self.response_id).is_closed)
        self.assertIn("Работа завершена", telegram.screens[-1][1])


if __name__ == "__main__":
    unittest.main()
