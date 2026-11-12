"""Menjalankan migrasi Alembic secara programatik + backup DB sebelum upgrade."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect

from app.database.types import utcnow

logger = logging.getLogger("server_penyimpanan.migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def head_revision(db_path: Path) -> str | None:
    return ScriptDirectory.from_config(alembic_config(db_path)).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def backup_database(db_path: Path, tag: str) -> Path:
    """Salin DB secara konsisten memakai SQLite online backup API."""
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    target = db_path.with_name(f"{db_path.name}.{tag}.{stamp}.bak")
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target


def upgrade_to_head(engine: Engine, db_path: Path) -> None:
    head = head_revision(db_path)
    current = current_revision(engine)
    if current == head:
        return
    has_tables = bool(inspect(engine).get_table_names())
    if has_tables:
        backup = backup_database(db_path, f"pre-{head}")
        logger.info("Backup database sebelum migrasi: %s", backup.name)
    logger.info("Migrasi database %s -> %s", current, head)
    cfg = alembic_config(db_path)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


def is_at_head(engine: Engine, db_path: Path) -> bool:
    return current_revision(engine) == head_revision(db_path)
