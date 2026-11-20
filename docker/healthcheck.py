"""Check both the HTTP server and the migrated PostgreSQL database."""
from __future__ import annotations

import sys
from urllib.request import urlopen

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from docker.entrypoint import database_url
from storage.database import DatabaseSettings, make_engine


def main() -> int:
    import os

    engine = None
    try:
        with urlopen("http://127.0.0.1:8000/openapi.json", timeout=2) as response:
            if response.status != 200:
                return 1
        settings = DatabaseSettings.model_validate({"database_url": database_url(os.environ)})
        engine = make_engine(settings, connect_args={"connect_timeout": 2})
        with engine.connect() as connection:
            expected = set(ScriptDirectory.from_config(Config("alembic.ini")).get_heads())
            actual = set(MigrationContext.configure(connection).get_current_heads())
            return 0 if actual == expected else 1
    except Exception:
        # Docker keeps health check output; never expose connection values there.
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
