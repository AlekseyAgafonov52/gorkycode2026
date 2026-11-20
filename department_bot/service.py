"""Authorized, transactional department actions; no model/API calls to YandexGPT."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import time
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from contract_models import ContractReport, RecruiterRecommendation
from hr_api.pdf_reports import PdfReports
from storage.crud import NotFoundError, Repository
from storage.models import Response, Vacancy
from .materials import DELIVERY_STAGES, Document, MaterialsNotReadyError, pdf_document, report_text, resume_text

PAGE_SIZE = 6


class AccessDeniedError(ValueError):
    pass


class ConfirmationExpiredError(ValueError):
    pass


@dataclass(frozen=True)
class Item:
    id: UUID
    title: str
    is_closed: bool
    version: str
    vacancy_id: UUID | None = None
    closed_at: datetime | None = None
    hired_at: datetime | None = None
    recommendation: RecruiterRecommendation | None = None


@dataclass(frozen=True)
class Page:
    items: list[Item]
    total: int
    page: int


@dataclass(frozen=True)
class Materials:
    item: Item
    vacancy_title: str
    version: int
    report: Document
    resume: Document
    completed: frozenset[str]


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class DepartmentService:
    def __init__(self, factory: sessionmaker[Session], manager_ids: frozenset[int], secret: str,
                 pdf_reports: PdfReports | None = None):
        self.factory = factory
        self.manager_ids = manager_ids
        self._key = secret.encode()
        self.pdf_reports = pdf_reports or PdfReports()

    def authorize(self, actor: int) -> None:
        if actor not in self.manager_ids:
            raise AccessDeniedError("Доступ руководителя не настроен для вашего Telegram ID.")

    @staticmethod
    def _item(row: Vacancy | Response) -> Item:
        is_vacancy = isinstance(row, Vacancy)
        closed = row.status == "closed" if is_vacancy else row.is_closed
        title = row.title if is_vacancy else row.candidate_name or "ФИО не указано"
        version = f"{row.updated_at.isoformat()}:{closed}"
        if not is_vacancy:
            version += f":{row.workflow_status}:{row.hired_at}"
        recommendation = None if is_vacancy or row.recruiter_recommendation is None else RecruiterRecommendation.model_validate(row.recruiter_recommendation)
        return Item(row.id, title, closed, version, None if is_vacancy else row.vacancy_id, row.closed_at,
                    None if is_vacancy else row.hired_at, recommendation)

    @staticmethod
    def _vacancy(repo: Repository, identifier: UUID, *, lock: bool = False) -> Vacancy:
        row = repo._lock_vacancy(identifier) if lock else repo.get_vacancy(identifier)
        if row.status == "draft":
            raise NotFoundError("Вакансия недоступна")
        return row

    def _target(self, repo: Repository, kind: str, identifier: UUID, *, lock: bool = False):
        if kind == "v":
            return self._vacancy(repo, identifier, lock=lock)
        if kind != "r":
            raise ValueError("Неизвестный тип записи")
        if lock:
            row, vacancy = repo._lock_response_vacancy(identifier)
        else:
            row = repo.get_response(identifier)
            vacancy = row.vacancy
        if row.workflow_status != "forwarded" or vacancy.status == "draft":
            raise NotFoundError("Отклик не передан HR в департамент или больше недоступен")
        return row

    def get(self, actor: int, kind: str, identifier: UUID) -> Item:
        self.authorize(actor)
        with self.factory.begin() as session:
            return self._item(self._target(Repository(session), kind, identifier))

    def vacancies(self, actor: int, closed: bool, page: int = 0) -> Page:
        self.authorize(actor)
        with self.factory.begin() as session:
            query = select(Vacancy).where(Vacancy.status == ("closed" if closed else "active"))
            return self._page(session, query, Vacancy, page)

    def responses(self, actor: int, vacancy_id: UUID, closed: bool, page: int = 0) -> Page:
        self.authorize(actor)
        with self.factory.begin() as session:
            self._vacancy(Repository(session), vacancy_id)
            query = select(Response).where(Response.vacancy_id == vacancy_id,
                Response.workflow_status == "forwarded", Response.is_closed == closed)
            return self._page(session, query, Response, page)

    def _page(self, session, query, model, page: int) -> Page:
        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        page = min(max(page, 0), max((total - 1) // PAGE_SIZE, 0))
        rows = session.scalars(query.order_by(model.created_at, model.id).limit(PAGE_SIZE).offset(page * PAGE_SIZE))
        return Page([self._item(row) for row in rows], total, page)

    def _signature(self, actor: int, payload: str, item: Item) -> str:
        return _b64(hmac.new(self._key, f"{actor}|{payload}|{item.version}".encode(), hashlib.sha256).digest()[:16])

    def confirmation(self, actor: int, kind: str, identifier: UUID, closed: bool, *, hiring: bool = False) -> tuple[Item, str]:
        if kind != "r":
            raise ValueError("Вакансию закрывает HR на платформе после сигнала о найме")
        item = self.get(actor, kind, identifier)
        action = ("H" if closed else "h") if hiring else ("R" if closed else "r")
        payload = f"{action}.{_b64(identifier.bytes)}.{int(time.time()) + 600}"
        return item, f"!{payload}.{self._signature(actor, payload, item)}"

    def confirm(self, actor: int, token: str) -> tuple[str, Item]:
        self.authorize(actor)
        try:
            action, encoded_id, expires, signature = token.removeprefix("!").split(".")
            if action not in ("R", "r", "H", "h") or not token.startswith("!"):
                raise ValueError
            identifier = UUID(bytes=base64.urlsafe_b64decode(encoded_id + "=="))
            expiry = int(expires)
            if not time.time() <= expiry <= time.time() + 600:
                raise ValueError
        except (ValueError, TypeError):
            raise ConfirmationExpiredError("Подтверждение недействительно. Откройте запись заново.") from None
        kind, closed = "r", action.isupper()
        with self.factory.begin() as session:
            row = self._target(Repository(session), kind, identifier, lock=True)
            item = self._item(row)
            payload = f"{action}.{encoded_id}.{expires}"
            if not hmac.compare_digest(signature, self._signature(actor, payload, item)):
                raise ConfirmationExpiredError("Запись изменилась. Откройте её заново и подтвердите действие.")
            assert isinstance(row, Response)
            if action in ("H", "h"):
                if bool(row.hired_at) != closed:
                    row.hired_at = datetime.now(timezone.utc) if closed else None
                    row.hired_by_telegram_id = actor if closed else None
                if closed and not row.is_closed:
                    row.is_closed = True
                    row.closed_at = row.hired_at
                    row.closed_by_telegram_id = actor
            elif item.is_closed != closed:
                if not closed and row.hired_at is not None:
                    raise ValueError("Сначала снимите ошибочную отметку найма")
                row.is_closed = closed
                row.closed_at = datetime.now(timezone.utc) if closed else None
                row.closed_by_telegram_id = actor if closed else None
            session.flush()
            return kind, self._item(row)

    def pending_notifications(self, actor: int) -> list[tuple[Item, str, int]]:
        self.authorize(actor)
        with self.factory.begin() as session:
            rows = session.execute(select(Response, Vacancy.title).join(Vacancy).where(
                Response.department_notification_pending.is_(True), Response.workflow_status == "forwarded",
                func.coalesce(Response.department_delivery_state["retry_at"].as_float(), 0) <= time.time(),
                Vacancy.status != "draft").order_by(Response.updated_at, Response.id).limit(10))
            return [(self._item(row), title, row.department_notification_version) for row, title in rows]

    def acknowledge_notification(self, actor: int, identifier: UUID, version: int) -> None:
        self.authorize(actor)
        with self.factory.begin() as session:
            row, _ = Repository(session)._lock_response_vacancy(identifier)
            if row.department_notification_version == version and all(
                    self._completed(row, recipient, version) == DELIVERY_STAGES for recipient in self.manager_ids):
                self._delivery_update(session, row, department_notification_pending=False)

    def defer_notification(self, actor: int, identifier: UUID, version: int) -> None:
        self.authorize(actor)
        with self.factory.begin() as session:
            row, _ = Repository(session)._lock_response_vacancy(identifier)
            if row.department_notification_version == version and row.department_notification_pending:
                self._delivery_update(session, row, department_delivery_state={
                    **row.department_delivery_state, "retry_at": time.time() + 60})

    @staticmethod
    def _delivery_update(session: Session, row: Response, **values) -> None:
        from sqlalchemy import update
        # A receipt must not invalidate the manager's hiring confirmation button.
        session.execute(update(Response).where(Response.id == row.id).values(**values, updated_at=Response.updated_at))

    @staticmethod
    def _completed(row: Response, actor: int, version: int) -> frozenset[str]:
        state = row.department_delivery_state
        if state.get("version") != version:
            return frozenset()
        recipients = state.get("recipients")
        if not isinstance(recipients, dict):
            return frozenset()
        stages = recipients.get(str(actor))
        # Fully delivered packets from the previous three-message format remain done.
        if isinstance(stages, list) and {"card", "report", "resume"}.issubset(stages):
            return DELIVERY_STAGES
        return frozenset(stage for stage in stages if isinstance(stage, str) and stage in DELIVERY_STAGES) if isinstance(stages, list) else frozenset()

    def mark_delivered(self, actor: int, identifier: UUID, version: int, stage: str) -> bool:
        self.authorize(actor)
        if stage not in DELIVERY_STAGES:
            raise ValueError("Неизвестный этап доставки")
        with self.factory.begin() as session:
            row = self._target(Repository(session), "r", identifier, lock=True)
            assert isinstance(row, Response)
            if row.department_notification_version != version:
                return False
            recipients = {str(recipient): sorted(self._completed(row, recipient, version)) for recipient in self.manager_ids}
            recipients[str(actor)] = sorted(self._completed(row, actor, version) | {stage})
            state = {"version": version, "recipients": recipients}
            self._delivery_update(session, row, department_delivery_state=state)
            return True

    def materials(self, actor: int, identifier: UUID, *, version: int | None = None) -> Materials:
        self.authorize(actor)
        with self.factory.begin() as session:
            row = self._target(Repository(session), "r", identifier, lock=True)
            assert isinstance(row, Response)
            if version is not None and version != row.department_notification_version:
                raise MaterialsNotReadyError()
            item = self._item(row)
            raw = row.source_json.get("source_text")
            if not isinstance(raw, str) or not raw.strip():
                raise MaterialsNotReadyError()
            resume = Document("Резюме.txt", resume_text(raw).encode("utf-8"), "text/plain; charset=utf-8")
            info = self.pdf_reports.info(row.id, row.vacancy_id, row.source_json)
            path = self.pdf_reports.path(row.id, row.vacancy_id) if info is not None and not info.is_stale else None
            if path is not None:
                report = pdf_document(path, "Отчёт.pdf")
            else:
                saved = row.report
                if saved is None or saved.contract_json is None or saved.criteria_version != row.vacancy.criteria_version:
                    raise MaterialsNotReadyError()
                structured = ContractReport.model_validate({**saved.contract_json, "source_text": raw,
                    "workflow_status": row.workflow_status, "recruiter_recommendation": row.recruiter_recommendation})
                report = Document("Отчёт.txt", report_text(structured, row.vacancy.title).encode("utf-8"), "text/plain; charset=utf-8")
            return Materials(item, row.vacancy.title, row.department_notification_version, report, resume,
                             self._completed(row, actor, row.department_notification_version))
