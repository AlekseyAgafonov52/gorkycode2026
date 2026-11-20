"""Private-chat menus. Every callback is authorized again before reading data."""
from __future__ import annotations

import logging
from datetime import timezone
from typing import Protocol
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from storage.crud import NotFoundError
from .service import AccessDeniedError, ConfirmationExpiredError, DepartmentService, Item, PAGE_SIZE, Page
from .materials import Document, MaterialsNotReadyError

logger = logging.getLogger(__name__)


class BotTransport(Protocol):
    def screen(self, chat_id: int, text: str, rows: list[list[dict]], message_id: int | None = None): ...
    def answer(self, callback_id: str, text: str | None = None): ...
    def packet(self, chat_id: int, text: str, documents: tuple[Document, Document], rows: list[list[dict]]): ...


def button(text: str, data: str) -> dict:
    if len(data.encode("utf-8")) > 64:
        raise ValueError("Callback превышает лимит Telegram")
    return {"text": text[:60], "callback_data": data}


def label(item: Item) -> str:
    # Plain text (no parse_mode): candidate data cannot introduce Telegram markup.
    return " ".join(item.title.split())[:90]


class BotController:
    def __init__(self, service: DepartmentService, client: BotTransport):
        self.service = service
        self.client = client

    def handle(self, update: dict) -> None:
        callback = update.get("callback_query")
        message = callback.get("message") if isinstance(callback, dict) else update.get("message")
        if not isinstance(message, dict):
            if isinstance(callback, dict) and callback.get("id"):
                self.client.answer(callback["id"], "Откройте личный чат с ботом.")
            return
        actor_data = callback.get("from", {}) if callback else message.get("from", {})
        actor, chat = actor_data.get("id"), message.get("chat", {})
        if chat.get("type") != "private" or not isinstance(actor, int) or chat.get("id") != actor:
            if callback:
                self.client.answer(callback["id"], "Действие доступно только в личном чате.")
            return
        command = str(message.get("text", "")).split("@", 1)[0].strip()
        if not callback and command == "/id":
            self.client.screen(actor, f"Ваш Telegram ID: {actor}\nПередайте его администратору для настройки доступа.", [])
            return
        acknowledged = False
        try:
            self.service.authorize(actor)
            data = callback.get("data", "") if callback else "vl:0:0"
            if not isinstance(data, str) or len(data.encode()) > 64:
                raise ValueError("Некорректная кнопка")
            # Acknowledge immediately; DB/Telegram failures must not leave a spinner.
            if callback:
                self.client.answer(callback["id"])
                acknowledged = True
            text, rows = self.route(actor, data)
        except (AccessDeniedError, NotFoundError, ConfirmationExpiredError, ValueError) as error:
            text, rows = str(error), [[button("К вакансиям", "vl:0:0")]]
            if callback and not acknowledged:
                self.client.answer(callback["id"], text)
        except SQLAlchemyError:
            # DB errors can contain SQL parameters and candidate data.
            logger.error("Database operation failed while processing a Telegram update")
            text, rows = "Не удалось сохранить или загрузить данные. Попробуйте снова.", [[button("К вакансиям", "vl:0:0")]]
        # Keep the two attachments when opening a card from the rich notification.
        message_id = message.get("message_id") if callback and not message.get("rich_message") else None
        self.client.screen(actor, text, rows, message_id)

    def route(self, actor: int, data: str) -> tuple[str, list[list[dict]]]:
        if data.startswith("!"):
            kind, item = self.service.confirm(actor, data)
            return self.card(kind, item)
        parts = data.split(":")
        if parts[0] == "vl" and len(parts) == 3:
            closed, number = self._list_args(parts[1], parts[2])
            page = self.service.vacancies(actor, closed, number)
            title = "Закрытые вакансии" if closed else "Текущие вакансии"
            rows = [[button(label(item), f"v:{item.id.hex}")] for item in page.items]
            rows += self.pager(page, lambda n: f"vl:{int(closed)}:{n}")
            rows.append([button("Текущие", "vl:0:0"), button("Закрытые", "vl:1:0")])
            return f"{title}: {page.total}\nВыберите вакансию." if page.total else f"{title}: нет записей.", rows
        if parts[0] in ("v", "r") and len(parts) == 2:
            return self.card(parts[0], self.service.get(actor, parts[0], UUID(parts[1])))
        if parts[0] == "close-vacancy" and len(parts) == 2:
            return self.card("v", self.service.close_vacancy(actor, UUID(parts[1])))
        if parts[0] == "files" and len(parts) == 2:
            identifier = UUID(parts[1])
            if not self.send_materials(actor, identifier):
                raise MaterialsNotReadyError()
            text, rows = self.card("r", self.service.get(actor, "r", identifier))
            return text + "\n\nОтчёт и исходное резюме отправлены в этот чат.", rows
        if parts[0] == "rl" and len(parts) == 4:
            vacancy_id = UUID(parts[1])
            closed, number = self._list_args(parts[2], parts[3])
            vacancy = self.service.get(actor, "v", vacancy_id)
            page = self.service.responses(actor, vacancy_id, closed, number)
            title = "Закрытые отклики" if closed else "Отклики в работе"
            rows = [[button(label(item), f"r:{item.id.hex}")] for item in page.items]
            rows += self.pager(page, lambda n: f"rl:{vacancy_id.hex}:{int(closed)}:{n}")
            rows.extend([[button("Закрытые", f"rl:{vacancy_id.hex}:1:0")],
                         [button("К вакансии", f"v:{vacancy_id.hex}")]])
            return f"{label(vacancy)}\n{title}: {page.total}", rows
        if parts[0] == "ask" and len(parts) == 4 and parts[1] in ("v", "r") and parts[3] in ("0", "1"):
            kind, closed = parts[1], parts[3] == "1"
            item, token = self.service.confirmation(actor, kind, UUID(parts[2]), closed)
            noun = "вакансию" if kind == "v" else "отклик"
            action = "Закрыть" if closed else "Открыть заново"
            note = "\nЗакрытие вакансии не закрывает её отклики." if kind == "v" else "\nЗакрытие означает завершение работы с откликом независимо от результата."
            return f"{action} {noun}?\n{label(item)}{note}", [
                [button(f"Подтвердить: {action.lower()}", token)], [button("Отмена", f"{kind}:{item.id.hex}")]]
        raise ValueError("Кнопка устарела. Откройте список вакансий заново.")

    def deliver_pending(self) -> None:
        actors = sorted(self.service.manager_ids)
        if not actors:
            return
        for item, _, version in self.service.pending_notifications(actors[0]):
            for actor in actors:
                try:
                    if not self.send_materials(actor, item.id, version=version):
                        break
                except NotFoundError:
                    break
                except MaterialsNotReadyError:
                    logger.info("Department materials are not ready or the HR transfer was recalled")
                    self.service.defer_notification(actor, item.id, version)
                    break
            else:
                self.service.acknowledge_notification(actors[0], item.id, version)

    def send_materials(self, actor: int, identifier: UUID, *, version: int | None = None) -> bool:
        materials = self.service.materials(actor, identifier, version=version)
        item = materials.item
        if "packet" in materials.completed:
            return True
        text, _ = self.card("r", item)
        self.client.packet(actor, f"HR передал кандидата или обновил материалы\nВакансия: {materials.vacancy_title[:150]}\n\n{text}",
                           (materials.report, materials.resume), [[button("Открыть кандидата", f"r:{item.id.hex}")]])
        return self.service.mark_delivered(actor, identifier, materials.version, "packet")

    @staticmethod
    def _list_args(closed: str, number: str) -> tuple[bool, int]:
        if closed not in ("0", "1") or not number.isdecimal():
            raise ValueError("Некорректная страница")
        return closed == "1", int(number)

    @staticmethod
    def pager(page: Page, callback) -> list[list[dict]]:
        row = []
        if page.page:
            row.append(button("← Назад", callback(page.page - 1)))
        if (page.page + 1) * PAGE_SIZE < page.total:
            row.append(button("Далее →", callback(page.page + 1)))
        return [row] if row else []

    @staticmethod
    def card(kind: str, item: Item) -> tuple[str, list[list[dict]]]:
        noun = "Вакансия" if kind == "v" else "Отклик"
        if kind == "v":
            status = "Закрыта" if item.is_closed else "Активна"
        else:
            status = "Работа завершена" if item.is_closed else "В работе"
        if item.hired_at:
            status = "Найм закрыт — трудовой договор подписан"
        text = f"{noun}: {label(item)}\nСтатус: {status}"
        if item.closed_at:
            text += f"\nЗакрытие: {item.closed_at.astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M UTC')}"
        rows = []
        if kind == "v":
            rows.append([button("Переданные отклики", f"rl:{item.id.hex}:0:0")])
            if not item.is_closed:
                rows.append([button("Найм завершён", f"close-vacancy:{item.id.hex}")])
        else:
            recommendation = item.recommendation
            notes = [] if recommendation is None else [
                ("Сильные стороны", recommendation.strengths), ("Слабые места", recommendation.weaknesses),
                ("Проверить на собеседовании", recommendation.interview_focus)]
            filled = [f"{label}: {value}" for label, value in notes if value]
            if filled:
                text += "\n\nРекомендации рекрутера\n" + "\n".join(filled)
            else:
                text += "\n\nРекомендации рекрутера пока не заполнены."
            rows.append([button("Отчёт и исходное резюме", f"files:{item.id.hex}")])
            if not item.hired_at:
                rows.append([button("Вернуть в работу" if item.is_closed else "Завершить работу с откликом",
                                    f"ask:r:{item.id.hex}:{int(not item.is_closed)}")])
        if kind == "v":
            rows.append([button("К вакансиям", "vl:0:0")])
        elif item.vacancy_id is not None:
            rows.append([button("К откликам", f"rl:{item.vacancy_id.hex}:{int(item.is_closed)}:0")])
        return text, rows
