"""Authorized, transactional department actions; no model/API calls to YandexGPT."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import time
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from contract_models import RecruiterRecommendation
from storage.crud import NotFoundError, Repository
from storage.models import Response, Vacancy

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


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class DepartmentService:
    def __init__(self, factory: sessionmaker[Session], manager_ids: frozenset[int], secret: str):
        self.factory = factory
        self.manager_ids = manager_ids
        self._key = secret.encode()

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
                Vacancy.status != "draft").order_by(Response.updated_at, Response.id).limit(10))
            return [(self._item(row), title, row.department_notification_version) for row, title in rows]

    def acknowledge_notification(self, actor: int, identifier: UUID, version: int) -> None:
        self.authorize(actor)
        with self.factory.begin() as session:
            session.execute(update(Response).where(Response.id == identifier,
                Response.department_notification_version == version).values(
                    department_notification_pending=False, updated_at=Response.updated_at))
