"""Small Bot API client. Provider URLs contain the token: never log HTTP errors."""
from __future__ import annotations

import logging
import json
import time

import httpx
from .materials import Document


class TelegramError(RuntimeError):
    def __init__(self, code: int = 0, retry_after: int = 5, *, reason: str = "api_error"):
        super().__init__(f"Telegram API недоступен (код {code}; {reason})")
        self.code = code
        self.reason = reason
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
            if method in ("sendMessage", "sendRichMessage"):
                chat_id = int(data["chat_id"])
                delay = self._next_send.get(chat_id, 0) - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self._next_send[chat_id] = time.monotonic() + 1.05
            response = self._client.post(method, data=data, files=files) if files else self._client.post(method, json=data)
            try:
                result = response.json()
            except ValueError:
                raise TelegramError(response.status_code if not response.is_success else 0, reason="invalid_response") from None
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
        except httpx.HTTPError as error:
            reasons = {httpx.ConnectTimeout: "connect_timeout", httpx.ReadTimeout: "read_timeout",
                       httpx.WriteTimeout: "write_timeout", httpx.PoolTimeout: "pool_timeout",
                       httpx.ConnectError: "connect_error", httpx.ProxyError: "proxy_error"}
            reason = next((value for kind, value in reasons.items() if isinstance(error, kind)), "network_error")
            raise TelegramError(reason=reason) from None
        except (ValueError, TypeError, AttributeError):
            raise TelegramError(reason="invalid_response") from None

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

    def packet(self, chat_id: int, text: str, documents: tuple[Document, Document], rows: list[list[dict]]):
        # Rich messages support multiple files and full HR recommendations in ONE Message.
        # Plain text blocks prevent candidate/recruiter text from becoming markup.
        blocks: list[dict] = [{"type": "paragraph", "text": text}]
        files = {}
        for number, document in enumerate(documents):
            name = f"file{number}"
            blocks.append({"type": "document", "document": {"type": "document", "media": f"attach://{name}"}})
            files[name] = (document.filename, document.content, document.mime_type)
        data = {"chat_id": str(chat_id),
                "rich_message": json.dumps({"blocks": blocks, "skip_entity_detection": True}, ensure_ascii=False),
                "reply_markup": json.dumps({"inline_keyboard": rows}, ensure_ascii=False)}
        return self._request("sendRichMessage", data, files=files)
