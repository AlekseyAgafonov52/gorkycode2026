"""Explicit PostgreSQL configuration and transaction-scoped sessions."""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError, field_validator
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, sessionmaker


DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database_url: SecretStr

    @field_validator("database_url", mode="before")
    @classmethod
    def postgres_url(cls, value: str | SecretStr) -> str:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        try:
            url = make_url(raw.strip())
            if url.drivername not in ("postgres", "postgresql", "postgresql+psycopg") or not url.database:
                raise ValueError("PostgreSQL URL required")
            return url.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)
        except (ArgumentError, AttributeError, TypeError, ValueError):
            raise ValueError("DATABASE_URL должен содержать PostgreSQL URL с именем базы") from None

    @classmethod
    def from_env(cls, env_file: str | Path | None = DEFAULT_ENV_FILE, *,
                 variable: str = "DATABASE_URL") -> DatabaseSettings:
        values = {}
        if env_file is not None:
            path = Path(env_file)
            if path.is_file():
                values = dotenv_values(path, encoding="utf-8-sig", interpolate=False)
        value = os.environ.get(variable, values.get(variable))
        if not value:
            raise ValueError(f"Задайте {variable} в окружении или .env")
        try:
            return cls(database_url=SecretStr(value))
        except ValidationError:
            raise ValueError(f"Проверьте {variable}: нужен PostgreSQL URL с именем базы") from None


def make_engine(settings: DatabaseSettings | None = None, **options) -> Engine:
    settings = settings or DatabaseSettings.from_env()
    return create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True,
                         hide_parameters=True, **options)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit on success, roll back on any failure; one session per caller."""
    with factory.begin() as session:
        yield session
