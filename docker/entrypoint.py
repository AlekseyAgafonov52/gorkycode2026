"""Build a safely encoded PostgreSQL URL from container environment values."""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence

from sqlalchemy.engine import URL


def database_url(environment: Mapping[str, str]) -> str:
    if environment.get("DATABASE_URL"):
        return environment["DATABASE_URL"]
    required = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
    if any(not environment.get(name) for name in required):
        raise ValueError("Set DATABASE_URL or all POSTGRES_* connection variables")
    try:
        port = int(environment["POSTGRES_PORT"])
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError("POSTGRES_PORT must be an integer from 1 to 65535") from None
    return URL.create("postgresql+psycopg", username=environment["POSTGRES_USER"],
                      password=environment["POSTGRES_PASSWORD"], host=environment["POSTGRES_HOST"],
                      port=port, database=environment["POSTGRES_DB"]).render_as_string(hide_password=False)


def main(arguments: Sequence[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if not arguments:
        raise SystemExit("Container command is required")
    try:
        os.environ["DATABASE_URL"] = database_url(os.environ)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    os.execvp(arguments[0], arguments)


if __name__ == "__main__":
    main()
