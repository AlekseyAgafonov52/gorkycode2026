"""Small Bot API client. Provider URLs contain the token: never log HTTP errors."""
from __future__ import annotations

import logging
import time

import httpx
from .materials import MAX_DOCUMENT_BYTES


class TelegramError(RuntimeError):
    def __init__(self, code: int = 0, retry_after: int = 5):
        super().__init__(f"Telegram API недоступен (код {code})")
        self.code = code
        self.retry_after = max(1, min(retry_after, 300))


class TelegramClient:
    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None):
        # httpx logs request URLs at INFO, which would expose a bot token.
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
        self._client = httpx.Client(base_url=f"https://api.telegram.org/bot{token}/",
                                    timeout=httpx.Timeout(40, connect=10), transport=transport)
        self._next_send: dict[int, float] = {}

    def close(self):
        self._client.close()

    def call(self, method: str, **data):
        return self._request(method, data)

    def _request(self, method: str, data: dict, files: dict | None = None):
        try:
            if method in ("sendMessage", "sendDocument"):
                chat_id = int(data["chat_id"])
                delay = self._next_send.get(chat_id, 0) - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self._next_send[chat_id] = time.monotonic() + 1.05
            response = self._client.post(method, data=data, files=files) if files else self._client.post(method, json=data)
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
            if not response.is_success or result.get("ok") is not True:
                # Editing an unchanged screen is a successful idempotent operation.
                if method == "editMessageText" and response.status_code == 400 and "message is not modified" in str(result.get("description", "")):
                    return None
                parameters = result.get("parameters") or {}
                raise TelegramError(response.status_code, int(parameters.get("retry_after", 5)))
            return result.get("result")
        except TelegramError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise TelegramError() from None

    def screen(self, chat_id: int, text: str, rows: list[list[dict]], message_id: int | None = None):
        safe_text = text.encode("utf-16-le")[:7800].decode("utf-16-le", errors="ignore")
        data = {"chat_id": chat_id, "text": safe_text, "reply_markup": {"inline_keyboard": rows}}
        if message_id is not None:
            return self.call("editMessageText", message_id=message_id, **data)
        return self.call("sendMessage", **data)

    def answer(self, callback_id: str, text: str | None = None):
        data: dict = {"callback_query_id": callback_id}
        if text:
            data.update(text=text[:180], show_alert=True)
        return self.call("answerCallbackQuery", **data)

    def document(self, chat_id: int, filename: str, content: bytes, mime_type: str, caption: str):
        if not content or len(content) > MAX_DOCUMENT_BYTES:
            raise ValueError("Недопустимый размер документа")
        data = {"chat_id": str(chat_id), "caption": caption.encode("utf-16-le")[:1800].decode("utf-16-le", errors="ignore")}
        return self._request("sendDocument", data, files={"document": (filename, content, mime_type)})
