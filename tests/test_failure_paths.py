"""Jalur kegagalan yang tidak boleh meninggalkan data setengah jadi (temuan coverage TAHAP 10)."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import func, select, update

from app.database import migrate
from app.database.session import Database, make_engine
from app.models import StoredFile, User
from app.services import file_service
from tests.conftest import auth_header, create_user_direct, login, make_settings, storage_root, upload_simple


@pytest.fixture
def lenient_user(make_client):
    """Client yang mengembalikan respons 500 (bukan melempar exception) untuk uji kegagalan."""
    c = make_client(raise_server_exceptions=False)
    uid = create_user_direct(c, "budi")
    return c, auth_header(login(c, "budi")), uid


def _files_on_disk(c) -> list:
    root = storage_root(c) / "files"
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


# ---------------- Upload chunked: kegagalan sementara bisa diulang ----------------
def test_complete_transient_error_keeps_upload_resumable(lenient_user, monkeypatch):
    c, h, _ = lenient_user
    data = os.urandom(2048)
    up = c.post("/api/v1/uploads/init", headers=h, json={"filename": "a.bin", "total_size": 2048, "chunk_size": 1024}).json()
    for i in range(2):
        c.put(f"/api/v1/uploads/{up['id']}/chunks/{i}", headers=h, content=data[i * 1024 : (i + 1) * 1024])
    real = file_service.finalize_new_file

    def disk_error(*args, **kwargs):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(file_service, "finalize_new_file", disk_error)
    resp = c.post(f"/api/v1/uploads/{up['id']}/complete", headers=h)
    assert resp.status_code == 500
    status = c.get(f"/api/v1/uploads/{up['id']}/status", headers=h).json()
    assert status["status"] == "uploading" and status["missing_chunks"] == 0  # data sementara tetap ada
    monkeypatch.setattr(file_service, "finalize_new_file", real)
    done = c.post(f"/api/v1/uploads/{up['id']}/complete", headers=h)
    assert done.status_code == 200
    assert c.get(f"/api/v1/files/{done.json()['file']['id']}/download", headers=h).content == data


def test_complete_conflict_error_does_not_fail_upload(user_client, monkeypatch):
    c, h, _ = user_client
    up = c.post("/api/v1/uploads/init", headers=h, json={"filename": "a.bin", "total_size": 10, "chunk_size": 1024}).json()
    c.put(f"/api/v1/uploads/{up['id']}/chunks/0", headers=h, content=b"0123456789")
    from app.core.errors import conflict

    def boom(*a, **k):
        raise conflict("name_conflict", "x")

    monkeypatch.setattr(file_service, "finalize_new_file", boom)
    assert c.post(f"/api/v1/uploads/{up['id']}/complete", headers=h).status_code == 409
    assert c.get(f"/api/v1/uploads/{up['id']}/status", headers=h).json()["status"] == "uploading"


def test_double_complete_rejected(user_client):
    c, h, _ = user_client
    up = c.post("/api/v1/uploads/init", headers=h, json={"filename": "a.bin", "total_size": 10, "chunk_size": 1024}).json()
    c.put(f"/api/v1/uploads/{up['id']}/chunks/0", headers=h, content=b"0123456789")
    assert c.post(f"/api/v1/uploads/{up['id']}/complete", headers=h).status_code == 200
    again = c.post(f"/api/v1/uploads/{up['id']}/complete", headers=h)
    assert again.status_code == 409 and again.json()["code"] == "upload_already_completed"
    with c.app.state.database.session() as db:
        assert db.scalar(select(func.count(StoredFile.id))) == 1


def test_finalize_db_failure_leaves_no_orphan_file(lenient_user, monkeypatch):
    c, h, _ = lenient_user
    from sqlalchemy.orm import Session

    original = Session.commit
    state = {"fail": True}

    def failing_commit(self):
        if state["fail"] and any(isinstance(o, StoredFile) for o in self.new):
            state["fail"] = False
            raise OSError("disk penuh saat commit")
        return original(self)

    monkeypatch.setattr(Session, "commit", failing_commit)
    resp = upload_simple(c, h, "a.txt", b"isi")
    assert resp.status_code == 500
    assert _files_on_disk(c) == []  # file fisik ikut dibersihkan
    assert not list((storage_root(c) / ".tmp" / "simple").iterdir())


def test_copy_failure_cleans_up(lenient_user, monkeypatch):
    c, h, _ = lenient_user
    fid = upload_simple(c, h, "asli.bin", b"x" * 5000).json()["id"]
    before = _files_on_disk(c)

    def broken_copy(src, dst):
        open(dst, "wb").write(b"setengah")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(file_service.shutil, "copyfile", broken_copy)
    assert c.post(f"/api/v1/files/{fid}/copy", headers=h, json={}).status_code == 500
    assert _files_on_disk(c) == before
    assert not list((storage_root(c) / ".tmp" / "copy").iterdir())
    assert c.get("/api/v1/files", headers=h).json()["total"] == 1


# ---------------- Upload sekali kirim: body tidak cocok ----------------
def test_simple_upload_requires_content_length(user_client):
    c, h, _ = user_client

    def body():
        yield b"abc"

    resp = c.post("/api/v1/files", params={"filename": "a.txt"}, headers=h, content=body())
    assert resp.status_code == 411 and resp.json()["code"] == "length_required"


def test_record_chunk_survives_parallel_duplicate(user_client, monkeypatch):
    """Dua request chunk yang sama bersamaan: IntegrityError ditangani, tidak 500."""
    c, h, _ = user_client
    up = c.post("/api/v1/uploads/init", headers=h, json={"filename": "a.bin", "total_size": 10, "chunk_size": 1024}).json()
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session

    original = Session.flush
    state = {"raised": False}

    def flaky_flush(self, *a, **k):
        if not state["raised"] and any(type(o).__name__ == "UploadChunk" for o in self.new):
            state["raised"] = True
            # Simulasi: request paralel menyisipkan chunk yang sama lebih dulu.
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))
        return original(self, *a, **k)

    monkeypatch.setattr(Session, "flush", flaky_flush)
    resp = c.put(f"/api/v1/uploads/{up['id']}/chunks/0", headers=h, content=b"0123456789")
    assert resp.status_code == 200 and state["raised"]


# ---------------- Database ----------------
def test_migration_required_when_auto_migrate_off(tmp_path):
    settings = make_settings(tmp_path, AUTO_MIGRATE=False)
    settings.DATABASE_PATH.parent.mkdir(parents=True)
    make_engine(settings.DATABASE_PATH).dispose()  # DB kosong tanpa skema
    database = Database(settings)
    assert database.initialize() is False
    assert database.error == "migration_required"
    engine = make_engine(settings.DATABASE_PATH)
    migrate.upgrade_to_head(engine, settings.DATABASE_PATH)
    engine.dispose()
    assert Database(settings).initialize() is True


def test_database_init_failure_reported(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)

    def broken(*a, **k):
        raise RuntimeError("file database rusak")

    monkeypatch.setattr(migrate, "upgrade_to_head", broken)
    database = Database(settings)
    assert database.initialize() is False and database.error == "database_init_failed"
    assert database.ping() is False


def test_corrupt_database_file_gives_503(make_client, tmp_path):
    settings = make_settings(tmp_path)
    settings.DATABASE_PATH.parent.mkdir(parents=True)
    settings.DATABASE_PATH.write_bytes(b"ini bukan database sqlite" * 100)
    c = make_client()
    resp = c.post("/api/v1/auth/login", json={"username": "a", "password": "b"})
    assert resp.status_code == 503 and resp.json()["code"] == "database_unavailable"
    assert c.get("/api/v1/system/health").status_code == 503


def test_retry_is_throttled(tmp_path):
    database = Database(make_settings(tmp_path, REQUIRE_MOUNTED_STORAGE=True))
    assert database.initialize() is False
    calls = []
    database.initialize = lambda: calls.append(1) or False  # type: ignore[method-assign]
    for _ in range(5):
        database.ensure_ready()
    assert calls == []  # percobaan ulang dibatasi tiap 5 detik


# ---------------- Auth ----------------
def test_refresh_for_deactivated_user_revokes_session(client):
    uid = create_user_direct(client, "budi")
    tokens = login(client, "budi")
    with client.app.state.database.session() as db:
        db.execute(update(User).where(User.id == uid).values(is_active=False))
        db.commit()
    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 401 and resp.json()["code"] == "account_inactive"


def test_token_with_non_numeric_subject_rejected(client):
    create_user_direct(client, "budi")
    tokens = login(client, "budi")
    from datetime import datetime, timezone

    import jwt

    now = int(datetime.now(timezone.utc).timestamp())
    token = jwt.encode(
        {"sub": "bukan-angka", "sid": tokens["session_id"], "type": "access", "iss": "Server_penyimpanan",
         "iat": now, "exp": now + 60},
        client.app.state.settings.SECRET_KEY, algorithm="HS256",
    )
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Basic YTpi"}).status_code == 401


def test_admin_search_escapes_wildcards(client):
    create_user_direct(client, "admin", role="admin")
    create_user_direct(client, "budi_1")
    create_user_direct(client, "budix1")
    h = auth_header(login(client, "admin"))
    body = client.get("/api/v1/admin/users", headers=h, params={"search": "_"}).json()
    assert [u["username"] for u in body["items"]] == ["budi_1"]


@pytest.mark.parametrize("password", ["  spasi-di-depan", "spasi-di-belakang  "])
def test_password_with_surrounding_spaces_rejected(client, password):
    resp = client.post("/api/v1/auth/register", json={"username": "budi", "password": password})
    assert resp.status_code == 422 and resp.json()["code"] == "weak_password"
