"""Tes konfigurasi, migrasi, pemeriksaan mount, dan penanganan error."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from pydantic import ValidationError
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError, OperationalError

import app.models  # noqa: F401
from app.core.config import PROJECT_ROOT
from app.database import migrate
from app.database.base import Base
from app.database.session import make_engine
from app.services import auth_service
from app.services.storage_guard import check_location
from tests.conftest import create_user_direct, make_settings

EXPECTED_TABLES = {
    "users", "refresh_tokens", "files", "folders", "uploads", "upload_chunks",
    "storage_devices", "backup_jobs", "activity_logs", "trash",
}


# ---------------- Konfigurasi ----------------
def test_placeholder_secret_rejected(tmp_path):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, SECRET_KEY="ganti-dengan-secret-acak-minimal-32-karakter")


def test_short_secret_rejected(tmp_path):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, SECRET_KEY="pendek")


def test_cors_wildcard_rejected(tmp_path):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, CORS_ORIGINS="*")
    s = make_settings(tmp_path, CORS_ORIGINS="http://192.168.1.5:3000, https://app.local/")
    assert s.CORS_ORIGINS == ["http://192.168.1.5:3000", "https://app.local"]


def test_storage_inside_code_dir_rejected(tmp_path):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, PRIMARY_STORAGE_PATH=PROJECT_ROOT / "storage")


def test_paths_must_be_under_mount_when_required(tmp_path):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, REQUIRE_MOUNTED_STORAGE=True, DATABASE_PATH=tmp_path / "lain" / "x.db")
    with pytest.raises(ValidationError):
        make_settings(
            tmp_path, REQUIRE_MOUNTED_STORAGE=True, PRIMARY_MOUNT_POINT=Path("/"),
            PRIMARY_STORAGE_PATH=Path("/srv/storage"), DATABASE_PATH=Path("/srv/db.sqlite"),
        )


# ---------------- Mount SSD/HDD ----------------
def test_check_location_unmounted_directory(tmp_path):
    status = check_location(tmp_path / "ssd" / "storage", tmp_path / "ssd", require_mounted=True)
    assert not status.available and status.reason == "not_mounted"


def test_check_location_real_mount_point(tmp_path):
    # "/" pasti mount point; path di bawahnya yang bisa ditulis => tersedia.
    assert check_location(tmp_path / "x", Path("/"), require_mounted=True).available


def test_ssd_not_mounted_server_degraded_and_no_db_written(make_client, tmp_path):
    c = make_client(REQUIRE_MOUNTED_STORAGE=True)
    health = c.get("/api/v1/system/health")
    assert health.status_code == 503
    assert health.json()["primary_storage"] == "unavailable"
    resp = c.post("/api/v1/auth/login", json={"username": "budi", "password": "Rahasia123"})
    assert resp.status_code == 503
    assert resp.json()["code"] == "primary_storage_unavailable"
    assert "Retry-After" in resp.headers
    # Tidak boleh ada DB yang dibuat di direktori mount kosong.
    assert not (tmp_path / "ssd").exists()


def test_health_ok(client):
    resp = client.get("/api/v1/system/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok", "primary_storage": "ok", "backup_storage": "ok"}


# ---------------- Migrasi ----------------
def test_migration_creates_all_tables_and_matches_models(tmp_path):
    db_path = tmp_path / "m.db"
    engine = make_engine(db_path)
    migrate.upgrade_to_head(engine, db_path)
    assert EXPECTED_TABLES <= set(inspect(engine).get_table_names())
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"Model dan migrasi tidak sinkron: {diff}"
    assert migrate.is_at_head(engine, db_path)
    engine.dispose()


def test_migration_is_idempotent_and_keeps_data(tmp_path, make_client):
    c = make_client()
    create_user_direct(c, "budi")
    settings = c.app.state.settings
    c.app.state.database.dispose()
    engine = make_engine(settings.DATABASE_PATH)
    migrate.upgrade_to_head(engine, settings.DATABASE_PATH)  # sudah di head: no-op
    engine.dispose()
    con = sqlite3.connect(settings.DATABASE_PATH)
    assert con.execute("SELECT username FROM users").fetchall() == [("budi",)]
    con.close()
    assert not list(settings.DATABASE_PATH.parent.glob("*.bak"))


def test_backup_before_migration_copies_data(tmp_path):
    db_path = tmp_path / "b.db"
    engine = make_engine(db_path)
    migrate.upgrade_to_head(engine, db_path)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO users (username, password_hash, role, is_active, created_at, updated_at, "
            "password_changed_at) VALUES ('budi', 'x', 'user', 1, '2026-01-01', '2026-01-01', '2026-01-01')"
        )
    engine.dispose()
    backup = migrate.backup_database(db_path, "test")
    con = sqlite3.connect(backup)
    assert con.execute("SELECT username FROM users").fetchall() == [("budi",)]
    con.close()


def test_foreign_keys_enforced(tmp_path):
    db_path = tmp_path / "fk.db"
    engine = make_engine(db_path)
    migrate.upgrade_to_head(engine, db_path)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO folders (id, owner_id, name, is_deleted, created_at, updated_at) "
            "VALUES ('f1', 999, 'x', 0, '2026-01-01', '2026-01-01')"
        )
    engine.dispose()


# ---------------- Penanganan error ----------------
def test_database_error_returns_503_without_details(client, monkeypatch):
    def boom(*args, **kwargs):
        raise OperationalError("SELECT rahasia FROM users", {}, Exception("disk I/O error /mnt/ssd"))

    monkeypatch.setattr(auth_service, "authenticate", boom)
    resp = client.post("/api/v1/auth/login", json={"username": "budi", "password": "Rahasia123"})
    assert resp.status_code == 503
    assert resp.json()["code"] == "database_error"
    assert "rahasia" not in resp.text and "/mnt/ssd" not in resp.text


def test_unhandled_error_returns_generic_500(make_client, monkeypatch):
    c = make_client(raise_server_exceptions=False)

    def boom(*args, **kwargs):
        raise RuntimeError("detail internal /home/pi/secret")

    monkeypatch.setattr(auth_service, "authenticate", boom)
    resp = c.post("/api/v1/auth/login", json={"username": "budi", "password": "Rahasia123"})
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Terjadi kesalahan pada server", "code": "internal_error"}


def test_unknown_route_404_json(client):
    resp = client.get("/api/v1/tidak-ada")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_security_headers(client):
    resp = client.get("/api/v1/system/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
