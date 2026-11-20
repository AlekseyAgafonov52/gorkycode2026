"""Read secrets without interpolation or changing the process environment."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, SecretStr, field_validator


class BotSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    token: SecretStr
    manager_ids: frozenset[int] = frozenset()
    offset_file: Path = Path(".local/telegram_bot/offset.json")

    @field_validator("token")
    @classmethod
    def valid_token(cls, value: SecretStr) -> SecretStr:
        import re
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", value.get_secret_value()):
            raise ValueError("Некорректный TELEGRAM_BOT_TOKEN")
        return value

    @field_validator("manager_ids")
    @classmethod
    def positive_ids(cls, value: frozenset[int]) -> frozenset[int]:
        if any(identifier <= 0 or identifier >= 2**63 for identifier in value):
            raise ValueError("TELEGRAM_MANAGER_IDS должен содержать положительные Telegram ID")
        return value

    @classmethod
    def from_env(cls, path: Path | str | None = ".env") -> BotSettings:
        values = dotenv_values(path, encoding="utf-8-sig", interpolate=False) if path is not None else {}
        values.update(os.environ)
        if not values.get("TELEGRAM_BOT_TOKEN"):
            raise ValueError("Укажите TELEGRAM_BOT_TOKEN в .env")
        try:
            ids = frozenset(int(item.strip()) for item in (values.get("TELEGRAM_MANAGER_IDS") or "").split(",") if item.strip())
            return cls(token=SecretStr(values["TELEGRAM_BOT_TOKEN"] or ""), manager_ids=ids,
                       offset_file=Path(values.get("TELEGRAM_OFFSET_FILE") or ".local/telegram_bot/offset.json"))
        except (ValueError, TypeError):
            raise ValueError("Проверьте TELEGRAM_BOT_TOKEN, TELEGRAM_MANAGER_IDS и TELEGRAM_OFFSET_FILE") from None
