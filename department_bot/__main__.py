"""python -m department_bot [--check-config]. Only one poller per bot is allowed."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import signal
import threading

from sqlalchemy import text

from storage.database import DatabaseSettings, make_engine, make_session_factory
from .controller import BotController
from .service import DepartmentService
from .settings import BotSettings
from .telegram import TelegramClient, TelegramError

logger = logging.getLogger(__name__)


class Cursor:
    def __init__(self, path: Path, bot_id: int):
        self.path, self.bot_id = path, bot_id
        self.offset = None
        if path.exists():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if value["bot_id"] != bot_id or type(value["offset"]) is not int or value["offset"] < 0:
                    raise ValueError
                self.offset = value["offset"]
            except (ValueError, KeyError, TypeError):
                raise ValueError("Некорректный Telegram cursor или файл принадлежит другому боту") from None

    def save(self, offset: int):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as output:
            json.dump({"bot_id": self.bot_id, "offset": offset}, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(self.path)
        self.offset = offset


def poll(client: TelegramClient, controller: BotController, cursor: Cursor, stop: threading.Event):
    while not stop.is_set():
        try:
            controller.deliver_pending()
            updates = client.call("getUpdates", offset=cursor.offset, timeout=25, limit=20,
                                  allowed_updates=["message", "callback_query"])
            if not isinstance(updates, list):
                raise TelegramError()
            for update in updates:
                if stop.is_set():
                    break
                if not isinstance(update, dict) or type(update.get("update_id")) is not int:
                    raise TelegramError()
                if cursor.offset is not None and update["update_id"] < cursor.offset:
                    continue
                try:
                    controller.handle(update)
                except TelegramError as error:
                    if error.code not in (400, 403):
                        raise
                    # Deleted messages / a blocked bot must not stall all updates.
                    logger.warning("Telegram message could not be delivered (code %s)", error.code)
                cursor.save(update["update_id"] + 1)
        except TelegramError as error:
            if error.code in (401, 404, 409):
                raise ValueError("Проверьте токен и отсутствие второго процесса Telegram-бота/webhook") from None
            logger.warning("Telegram temporary failure (code %s); retrying", error.code)
            stop.wait(error.retry_after)


def main() -> int:
    parser = argparse.ArgumentParser(description="Бот руководителя департамента")
    parser.add_argument("--check-config", action="store_true", help="Проверка настроек без запросов в Telegram/БД")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = None
    engine = None
    try:
        settings = BotSettings.from_env()
        db = DatabaseSettings.from_env()
        if args.check_config:
            print("Настройки прочитаны. Руководителей: " + str(len(settings.manager_ids)))
            return 0
        stop = threading.Event()
        for name in (signal.SIGINT, signal.SIGTERM):
            signal.signal(name, lambda *_: stop.set())
        token = settings.token.get_secret_value()
        client = TelegramClient(token)
        identity = client.call("getMe")
        if not isinstance(identity, dict) or type(identity.get("id")) is not int:
            raise ValueError("Telegram не подтвердил идентификатор бота")
        bot_id = identity["id"]
        webhook = client.call("getWebhookInfo")
        if not isinstance(webhook, dict) or webhook.get("url"):
            raise ValueError("У бота установлен webhook. Отключите его перед запуском long polling.")
        cursor = Cursor(settings.offset_file, bot_id)
        engine = make_engine(db)
        with engine.connect() as lock:
            key = f"hr-platform/department-bot/{bot_id}"
            acquired = lock.scalar(text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), {"key": key})
            lock.commit()
            if not acquired:
                raise ValueError("Этот Telegram-бот уже запущен")
            try:
                service = DepartmentService(make_session_factory(engine), settings.manager_ids, token)
                if not settings.manager_ids:
                    logger.warning("TELEGRAM_MANAGER_IDS is empty: only /id is available")
                logger.info("Department bot started")
                poll(client, BotController(service, client), cursor, stop)
            finally:
                lock.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), {"key": key})
                lock.commit()
        return 0
    except ValueError as error:
        logger.error("%s", error)
        return 1
    except TelegramError as error:
        logger.error("Telegram startup failed (code %s)", error.code)
        return 1
    except Exception:
        # Never include exception repr/traceback: connection errors may contain credentials.
        logger.error("Bot stopped: check PostgreSQL availability, migrations and state volume permissions")
        return 1
    finally:
        if client:
            client.close()
        if engine:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
