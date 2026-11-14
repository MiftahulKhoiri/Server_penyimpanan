"""Konsistensi set backup: manifest, status, staging, retensi, penundaan hapus, restore tervalidasi."""

from __future__ import annotations

import errno
import json
import os
import sqlite3
from collections import namedtuple

import pytest
from sqlalchemy import select

from app.models import StoredFile, TrashItem
from app.services import backup_pin, backup_service, trash_service
from app.services.storage_paths import StoragePaths
from tests.conftest import (
    auth_header,
    create_user_direct,
    login,
    storage_root,
    upload_simple,
)
from tests.test_deploy_scripts import restore_backup

API = "/api/v1/storage/backup"


def _setup(c):
    create_user_direct(c, "admin", role="admin")
    return auth_header(login(c, "admin"))


def _run(c, h):
    resp = c.post(API, headers=h)
    assert resp.status_code == 202, resp.text
    c.app.state.backup_manager.wait(60)
    return c.get(f"{API}/{resp.json()['id']}", headers=h).json()


def _broot(c):
    return c.app.state.settings.BACKUP_STORAGE_PATH


def _relpath(c, fid):
    with c.app.state.database.session() as db:
        return db.get(StoredFile, fid).storage_path


def test_manifest_contents_and_valid_set(client):
    h = _setup(client)
    data = {upload_simple(client, h, f"{i}.bin", os.urandom(1000 + i)).json()["id"]: 1000 + i for i in range(3)}
    job = _run(client, h)
    assert job["status"] == "completed"
    sets = backup_service.completed_sets(_broot(client))
    assert len(sets) == 1
    assert "files" not in sets[0].manifest  # ringkasan kecil, tanpa daftar file
    manifest = sets[0].load_manifest()
    assert manifest["set_id"] == sets[0].set_id == job["report"]["set_id"]
    assert manifest["status"] == "completed" and manifest["created_at"] and manifest["finished_at"]
    assert manifest["database"]["integrity"] == "ok" and len(manifest["database"]["sha256"]) == 64
    assert manifest["file_count"] == 3 and manifest["total_bytes"] == sum(data.values())
    by_id = {e["file_id"]: e for e in manifest["files"]}
    assert set(by_id) == set(data)
    for fid, size in data.items():
        assert by_id[fid]["size"] == size and by_id[fid]["path"] == _relpath(client, fid)
        assert len(by_id[fid]["sha256"]) == 64
    assert backup_service.validate_set(_broot(client), sets[0]) == []
    assert not list((_broot(client) / "sets").glob(".staging-*"))


def test_trashed_files_included_in_set(client):
    """Snapshot memuat baris file di trash, jadi datanya juga harus ikut dalam set."""
    h = _setup(client)
    fid = upload_simple(client, h, "sampah.bin", b"di trash").json()["id"]
    client.delete(f"/api/v1/files/{fid}", headers=h)
    assert _run(client, h)["status"] == "completed"
    manifest = backup_service.completed_sets(_broot(client))[0].load_manifest()
    assert [e["file_id"] for e in manifest["files"]] == [fid]


def test_missing_source_file_fails_and_keeps_old_set(make_client):
    c = make_client(BACKUP_DB_KEEP=1)
    h = _setup(c)
    fid = upload_simple(c, h, "a.bin", b"a" * 100).json()["id"]
    assert _run(c, h)["status"] == "completed"
    old = backup_service.completed_sets(_broot(c))
    lost = upload_simple(c, h, "b.bin", b"b" * 100).json()["id"]
    (storage_root(c) / _relpath(c, lost)).unlink()  # hilang di luar aplikasi
    job = _run(c, h)
    assert job["status"] == "failed" and job["error_message"] == "required_files_failed"
    assert job["report"]["failures"] == [{"file_id": lost, "reason": "source_missing"}]
    # Retensi tidak berjalan untuk backup gagal: set lama yang valid tetap ada.
    assert [s.set_id for s in backup_service.completed_sets(_broot(c))] == [old[0].set_id]
    assert backup_service.validate_set(_broot(c), old[0]) == []
    assert fid


def test_file_changed_during_backup_detected(client, monkeypatch):
    h = _setup(client)
    fid = upload_simple(client, h, "a.bin", b"x" * 2000).json()["id"]
    src = storage_root(client) / _relpath(client, fid)
    original = backup_service._copy_with_hash

    def changing(source, dest):
        src.write_bytes(b"y" * 2000)  # file berubah tepat saat backup menyalinnya
        return original(source, dest)

    monkeypatch.setattr(backup_service, "_copy_with_hash", changing)
    job = _run(client, h)
    assert job["status"] == "failed"
    assert job["report"]["failures"][0]["reason"] == "source_checksum_mismatch"
    assert not (_broot(client) / _relpath(client, fid)).exists()  # salinan berbeda tidak ditulis
    assert backup_service.completed_sets(_broot(client)) == []


def test_purge_during_backup_is_deferred(client, monkeypatch):
    """Hapus permanen saat backup berjalan tidak boleh membuat set tidak konsisten."""
    h = _setup(client)
    keep = upload_simple(client, h, "a.bin", b"a" * 500).json()["id"]
    victim = upload_simple(client, h, "b.bin", b"b" * 500).json()["id"]
    client.delete(f"/api/v1/files/{victim}", headers=h)  # di trash, masih ada di snapshot
    victim_rel = _relpath(client, victim)
    database = client.app.state.database
    storage = StoragePaths(storage_root(client))
    original = backup_service._copy_with_hash
    purged = []

    def purge_first(source, dest):
        if not purged:
            with database.session() as db:
                item = db.scalar(select(TrashItem).where(TrashItem.file_id == victim))
                trash_service.purge_item(db, storage, item, actor_id=None, client=None, reason="test")
            purged.append(True)
            # Data fisik ditunda, bukan dihapus, karena backup memegang pin.
            assert not (storage.root / victim_rel).exists()
            assert backup_pin.pending_path(storage.root, victim_rel).exists()
        return original(source, dest)

    monkeypatch.setattr(backup_service, "_copy_with_hash", purge_first)
    job = _run(client, h)
    assert job["status"] == "completed", json.dumps(job)
    s = backup_service.completed_sets(_broot(client))[0]
    assert {e["file_id"] for e in s.load_manifest()["files"]} == {keep, victim}
    assert backup_service.validate_set(_broot(client), s) == []
    # Setelah backup selesai, file yang ditunda benar-benar dihapus.
    assert not backup_pin.pending_path(storage.root, victim_rel).exists()


def test_purge_without_backup_deletes_immediately(client):
    h = _setup(client)
    fid = upload_simple(client, h, "a.bin", b"z").json()["id"]
    path = storage_root(client) / _relpath(client, fid)
    client.delete(f"/api/v1/files/{fid}", headers=h)
    assert client.post(f"/api/v1/trash/{fid}/delete", headers=h).status_code == 200
    assert not path.exists() and not (storage_root(client) / backup_pin.PENDING_DIR).exists()


def test_disk_full_keeps_previous_set(make_client, monkeypatch):
    c = make_client(BACKUP_DB_KEEP=1)
    h = _setup(c)
    upload_simple(c, h, "a.bin", b"x" * 500)
    assert _run(c, h)["status"] == "completed"
    before = [s.set_id for s in backup_service.completed_sets(_broot(c))]
    upload_simple(c, h, "b.bin", b"y" * 500)
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(backup_service, "disk_usage", lambda _p: Usage(10_000, 9_990, 10))
    job = _run(c, h)
    assert job["status"] == "failed" and job["error_message"] == "backup_storage_full"
    assert [s.set_id for s in backup_service.completed_sets(_broot(c))] == before


def test_enospc_while_writing_snapshot(client, monkeypatch):
    h = _setup(client)

    def full(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(backup_service, "snapshot_database", full)
    job = _run(client, h)
    assert job["status"] == "failed" and job["error_message"] == "backup_storage_full"
    assert backup_service.completed_sets(_broot(client)) == []


def test_corrupt_database_snapshot_fails(client, monkeypatch):
    """Snapshot yang rusak setelah ditulis (mis. sektor HDD buruk) tidak boleh difinalisasi."""
    h = _setup(client)
    upload_simple(client, h, "a.bin", b"x" * 100)
    real = backup_service.snapshot_database

    def corrupting(settings, target):
        info = real(settings, target)
        with open(target, "r+b") as fh:
            fh.seek(0)
            fh.write(b"\x00" * 100)  # header SQLite rusak
        return info

    monkeypatch.setattr(backup_service, "snapshot_database", corrupting)
    job = _run(client, h)
    assert job["status"] == "failed"
    assert job["error_message"] in ("database_verification_failed", "database_snapshot_unreadable")
    assert backup_service.completed_sets(_broot(client)) == []


def test_interrupted_staging_never_valid_and_cleaned(client):
    h = _setup(client)
    staging = _broot(client) / "sets" / ".staging-20200101T000000Z-job000099"
    staging.mkdir(parents=True)
    (staging / "manifest.json").write_text(json.dumps({"set_id": "x", "status": "in_progress"}))
    # Set final dengan status bukan completed (mis. ditulis tangan) juga diabaikan.
    bogus = _broot(client) / "sets" / "20200101T000000Z-job000098"
    bogus.mkdir()
    (bogus / "manifest.json").write_text(json.dumps({"set_id": bogus.name, "status": "in_progress"}))
    assert backup_service.completed_sets(_broot(client)) == []
    with pytest.raises(SystemExit):
        restore_backup.choose_set(client.app.state.settings, bogus.name)
    job = _run(client, h)
    assert job["status"] == "completed" and job["report"]["stale_staging_removed"] == 1
    assert not staging.exists()


def test_second_backup_process_refused(client):
    h = _setup(client)
    with backup_service.backup_process_lock(_broot(client)):
        job = _run(client, h)
    assert job["status"] == "failed" and job["error_message"] == "backup_running_elsewhere"


def test_restore_full_set(client):
    h = _setup(client)
    keep = upload_simple(client, h, "lama.bin", os.urandom(3000)).json()["id"]
    _run(client, h)
    upload_simple(client, h, "baru.bin", b"setelah backup")
    settings = client.app.state.settings
    path = storage_root(client) / _relpath(client, keep)
    original = path.read_bytes()
    path.unlink()
    client.app.state.database.dispose()

    dry = restore_backup.restore_set(settings, "latest", apply=False, stamp="DRY")
    assert dry.restored == [keep] and not path.exists()
    report = restore_backup.restore_set(settings, "latest", apply=True, stamp="T1")
    assert report.restored == [keep] and report.unrecoverable == []
    assert path.read_bytes() == original
    con = sqlite3.connect(settings.DATABASE_PATH)
    assert {r[0] for r in con.execute("SELECT name FROM files")} == {"lama.bin"}
    con.close()
    assert settings.DATABASE_PATH.with_name("server.db.before-restore-T1.bak").exists()


def test_restore_refuses_invalid_set_without_changes(client):
    h = _setup(client)
    fid = upload_simple(client, h, "a.bin", os.urandom(2000)).json()["id"]
    _run(client, h)
    upload_simple(client, h, "b.bin", b"tetap")
    settings = client.app.state.settings
    (_broot(client) / _relpath(client, fid)).write_bytes(b"\x00" * 2000)  # salinan di HDD rusak
    client.app.state.database.dispose()
    before = settings.DATABASE_PATH.read_bytes()
    with pytest.raises(SystemExit) as exc:
        restore_backup.restore_set(settings, "latest", apply=True, stamp="X")
    assert "file_checksum_mismatch" in str(exc.value.code)
    assert settings.DATABASE_PATH.read_bytes() == before  # DB produksi tidak disentuh
    assert not settings.DATABASE_PATH.with_name("server.db.before-restore-X.bak").exists()


def test_restore_legacy_database_requires_flag(client):
    settings = client.app.state.settings
    legacy_dir = settings.BACKUP_STORAGE_PATH / "database"
    legacy_dir.mkdir(parents=True)
    con = sqlite3.connect(legacy_dir / "server-20200101T000000000000Z.db")
    con.execute("CREATE TABLE files (id)")
    con.close()
    with pytest.raises(SystemExit):
        restore_backup.restore_database(settings, "server-20200101T000000000000Z.db", apply=False, stamp="L")
    restore_backup.restore_database(
        settings, "server-20200101T000000000000Z.db", apply=False, stamp="L", allow_legacy=True
    )
