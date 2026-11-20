"""Alembic uses DATABASE_URL, never credentials in alembic.ini."""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool

from storage.database import DatabaseSettings, make_engine
from storage.models import Base


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)
target_metadata = Base.metadata


def run_on_connection(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True,
                      compare_server_default=True, version_table_schema=config.attributes.get("version_table_schema"))
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    settings = DatabaseSettings.from_env()
    context.configure(url=settings.database_url.get_secret_value(), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
else:
    connection = config.attributes.get("connection")
    if connection is not None:
        run_on_connection(connection)
    else:
        engine = make_engine(poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                run_on_connection(connection)
        finally:
            engine.dispose()
