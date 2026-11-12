from __future__ import annotations

import hashlib
import os
import sqlite3
from collections import namedtuple

from sqlalchemy import select

from app.database.types import utcnow
from app.main import run_maintenance_once
from app.models import ActivityLog, BackupJob, StoredFile
from app.services import backup_service, storage_guard, storage_service
from tests.conftest import auth_header, create_user_direct, login, storage_root, upload_simple

API = "/api/v1/storage/backup"


def _setup(c):
    create_user_direct(c, "admin", role="admin")
    return auth_header(login(c, "admin"))


def _run(c, h, **body):
    resp = c.post(API, headers=h, json=body or None)
    assert resp.status_code == 202, resp.text
    c.app.state.backup_manager.wait(60)
    return c.get(f"{API}/{resp.json()['id']}", headers=h).json()


def _backup_root(c):
    return c.app.state.settings.BACKUP_STORAGE_PATH


def _paths(c, fid):
    with c.app.state.database.session() as db:
        rel = db.get(StoredFile, fid).storage_path
    return storage_root(c) / rel, _backup_root(c) / rel


def test_backup_copies_and_verifies(client):
    h = _setup(client)
    files = {upload_simple(client, h, f"f{i}.bin", os.urandom(5000 + i)).json()["id"] for i in range(5)}
    job = _run(client, h)
    assert job["status"] == "completed", job
    assert job["files_total"] == 5 and job["files_copied"] == 5 and job["files_failed"] == 0
    assert job["bytes_copied"] == sum(5000 + i for i in range(5))
    for fid in files:
        src, dst = _paths(client, fid)
        assert src.read_bytes() == dst.read_bytes()  # salinan identik
        assert src.exists()  # file asli tidak tersentuh
    db_backup = job["report"]["database_backup"]
    assert db_backup["integrity"] == "ok"
    copy = _backup_root(client) / "database" / db_backup["file"]
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == db_backup["sha256"]
    con = sqlite3.connect(copy)
    assert con.execute("SELECT count(*) FROM files").fetchone()[0] == 5
    con.close()
    with client.app.state.database.session() as db:
        assert all(f.backed_up_at is not None for f in db.scalars(select(StoredFile)).all())
        assert db.scalar(select(ActivityLog).where(ActivityLog.action == "backup.completed")) is not None
    assert not list((_backup_root(client) / ".tmp").iterdir())


def test_backup_is_incremental(client):
    h = _setup(client)
    upload_simple(client, h, "a.bin", b"a" * 100)
    _run(client, h)
    upload_simple(client, h, "b.bin", b"b" * 100)
    job = _run(client, h)
    assert job["files_copied"] == 1 and job["files_skipped"] == 1 and job["status"] == "completed"


def test_backup_never_deletes_backup_of_removed_file(client):
    h = _setup(client)
    fid = upload_simple(client, h, "penting.bin", b"data penting").json()["id"]
    _run(client, h)
    _, dst = _paths(client, fid)
    client.delete(f"/api/v1/files/{fid}", headers=h)
    client.post(f"/api/v1/trash/{fid}/delete", headers=h)
    job = _run(client, h)
    assert job["status"] == "completed"
    assert dst.read_bytes() == b"data penting"  # salinan HDD tetap ada


def test_source_corruption_detected(client):
    h = _setup(client)
    good = upload_simple(client, h, "baik.bin", b"x" * 1000).json()["id"]
    bad = upload_simple(client, h, "rusak.bin", b"y" * 1000).json()["id"]
    src, dst = _paths(client, bad)
    src.write_bytes(b"Z" * 1000)  # bit rot / kerusakan di SSD, ukuran sama
    job = _run(client, h)
    assert job["status"] == "partial" and job["files_failed"] == 1 and job["files_copied"] == 1
    assert job["report"]["failures"] == [{"file_id": bad, "reason": "source_checksum_mismatch"}]
    assert not dst.exists()  # salinan rusak tidak pernah ditulis
    assert _paths(client, good)[1].exists()


def test_verify_existing_repairs_corrupt_backup_copy(client):
    h = _setup(client)
    data = os.urandom(4000)
    fid = upload_simple(client, h, "a.bin", data).json()["id"]
    _run(client, h)
    _, dst = _paths(client, fid)
    dst.write_bytes(b"\x00" * len(data))  # kerusakan di HDD
    assert _run(client, h)["files_copied"] == 0  # tanpa verifikasi: dilewati
    job = _run(client, h, verify_existing=True)
    assert job["files_copied"] == 1 and job["report"]["repaired"] == 1
    assert dst.read_bytes() == data


def test_backup_requires_admin_and_hdd(client, monkeypatch):
    h = _setup(client)
    create_user_direct(client, "budi")
    assert client.post(API, headers=auth_header(login(client, "budi"))).status_code == 403
    settings = client.app.state.settings
    ssd = str(settings.PRIMARY_MOUNT_POINT)
    fake = lambda p: str(p) == ssd  # noqa: E731  SSD ter-mount, HDD tidak
    monkeypatch.setattr(storage_guard.os.path, "ismount", fake)
    monkeypatch.setattr(storage_service.os.path, "ismount", fake)
    settings.REQUIRE_MOUNTED_STORAGE = True
    resp = client.post(API, headers=h)
    assert resp.status_code == 503 and resp.json()["code"] == "backup_unavailable"
    assert client.get(API, headers=h).json()["total"] == 0
    assert upload_simple(client, h, "tetap.txt", b"ok").status_code == 201  # server utama tetap jalan


def test_only_one_backup_at_a_time(client):
    h = _setup(client)
    with client.app.state.database.session() as db:
        db.add(BackupJob(status="running", created_at=utcnow()))
        db.commit()
    resp = client.post(API, headers=h)
    assert resp.status_code == 409 and resp.json()["code"] == "backup_running"


def test_interrupted_job_marked_failed_on_restart(make_client):
    c1 = make_client()
    with c1.app.state.database.session() as db:
        job = BackupJob(status="running", created_at=utcnow())
        db.add(job)
        db.commit()
        job_id = job.id
    c2 = make_client()  # "restart": app baru, database yang sama
    h = _setup(c2)
    detail = c2.get(f"{API}/{job_id}", headers=h).json()
    assert detail["status"] == "failed" and detail["error_message"] == "interrupted"


def test_hdd_full_aborts(client, monkeypatch):
    h = _setup(client)
    upload_simple(client, h, "a.bin", b"x" * 500)
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(backup_service, "disk_usage", lambda _p: Usage(10_000, 9_990, 10))
    job = _run(client, h)
    assert job["status"] == "failed" and job["error_message"] == "backup_storage_full"
    assert job["report"]["failures"][0]["reason"] == "insufficient_space"


def test_database_backup_rotation(make_client):
    c = make_client(BACKUP_DB_KEEP=2)
    h = _setup(c)
    for _ in range(3):
        _run(c, h)
    assert len(list((_backup_root(c) / "database").glob("server-*.db"))) == 2


def test_auto_backup_only_when_configured(make_client):
    off = make_client()
    _setup(off)
    assert run_maintenance_once(off.app)["auto_backup_started"] == 0
    on = make_client(BACKUP_AUTO_INTERVAL_HOURS=6)  # database yang sama, admin sudah ada
    h = auth_header(login(on, "admin"))
    assert run_maintenance_once(on.app)["auto_backup_started"] == 1
    on.app.state.backup_manager.wait(30)
    assert run_maintenance_once(on.app)["auto_backup_started"] == 0  # belum waktunya lagi
    jobs = on.get(API, headers=h).json()
    assert jobs["total"] == 1 and jobs["items"][0]["triggered_by_id"] is None


def test_backup_job_not_found(client):
    h = _setup(client)
    assert client.get(f"{API}/999", headers=h).status_code == 404
