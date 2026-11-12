from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

import app.models  # noqa: F401  (registrasi semua tabel ke metadata)
from app.database.base import Base
from app.database.types import UTCDateTime

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from app.core.config import get_settings

    return f"sqlite:///{get_settings().DATABASE_PATH}"


def _render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    # Tulis UTCDateTime sebagai sa.DateTime() agar file migrasi tidak
    # bergantung pada kode aplikasi yang bisa berubah.
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime()"
    return False


def _configure(**kwargs: object) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        render_item=_render_item,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=_database_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        _configure(connection=conn)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
