"""Tes temuan audit TAHAP 10: batas ukuran body & kunci database selama backup."""

from __future__ import annotations

import sqlite3
import threading
import time

from app.services import backup_service
from tests.conftest import auth_header, create_user_direct, login, upload_simple


# ---------------- Batas ukuran body untuk endpoint non-upload ----------------
def test_huge_json_body_rejected_with_content_length(client):
    body = b'{"username":"a","password":"' + b"x" * (2 * 1024 * 1024) + b'"}'
    resp = client.post("/api/v1/auth/login", content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 413
    assert resp.json()["code"] == "request_too_large"


def test_huge_json_body_rejected_when_streamed(client):
    def chunks():
        yield b'{"username":"a","password":"'
        for _ in range(64):
            yield b"x" * 65536
        yield b'"}'

    resp = client.post("/api/v1/auth/login", content=chunks(), headers={"Content-Type": "application/json"})
    assert resp.status_code == 413


def test_normal_requests_and_uploads_not_limited(user_client):
    c, h, _ = user_client
    big = b"y" * (3 * 1024 * 1024)  # lebih besar dari batas JSON, tetapi ini endpoint upload
    assert upload_simple(c, h, "besar.bin", big).status_code == 201
    up = c.post("/api/v1/uploads/init", headers=h, json={"filename": "c.bin", "total_size": len(big), "chunk_size": len(big)}).json()
    assert c.put(f"/api/v1/uploads/{up['id']}/chunks/0", headers=h, content=big).status_code == 200
    assert c.post("/api/v1/folders", headers=h, json={"name": "Biasa"}).status_code == 201


# ---------------- Backup tidak boleh menahan kunci tulis SQLite ----------------
def test_backup_does_not_hold_write_lock_while_copying(client, monkeypatch):
    create_user_direct(client, "admin", role="admin")
    h = auth_header(login(client, "admin"))
    for i in range(3):
        upload_simple(client, h, f"{i}.bin", b"x" * 1000)
    db_path = client.app.state.settings.DATABASE_PATH
    results: list[str] = []
    real_copy = backup_service._copy_with_hash
    calls = {"n": 0}

    def slow_copy(src, dest):
        calls["n"] += 1
        if calls["n"] == 2:
            # Simulasi salinan file besar yang lama di Pi 3: pengguna lain menulis ke DB saat ini.
            con = sqlite3.connect(db_path, timeout=0.5)
            try:
                con.execute("UPDATE users SET updated_at = updated_at WHERE id = 1")
                con.commit()
                results.append("ok")
            except sqlite3.OperationalError as exc:
                results.append(str(exc))
            finally:
                con.close()
        return real_copy(src, dest)

    monkeypatch.setattr(backup_service, "_copy_with_hash", slow_copy)
    assert client.post("/api/v1/storage/backup", headers=h).status_code == 202
    client.app.state.backup_manager.wait(30)
    assert results == ["ok"], f"database terkunci selama backup: {results}"
    job = client.get("/api/v1/storage/backup/1", headers=h).json()
    assert job["status"] == "completed" and job["files_copied"] == 3


def test_concurrent_requests_during_backup(client, monkeypatch):
    """Login & upload tetap berhasil ketika backup sedang menyalin."""
    create_user_direct(client, "admin", role="admin")
    h = auth_header(login(client, "admin"))
    for i in range(5):
        upload_simple(client, h, f"{i}.bin", b"z" * 500)
    real_copy = backup_service._copy_with_hash
    copying = threading.Event()

    def slow(src, dest):
        copying.set()
        time.sleep(0.3)
        return real_copy(src, dest)

    monkeypatch.setattr(backup_service, "_copy_with_hash", slow)
    client.post("/api/v1/storage/backup", headers=h)
    assert copying.wait(5)
    for i in range(3):
        assert login(client, "admin")["access_token"]
        assert upload_simple(client, h, f"selama-backup-{i}.txt", b"ok").status_code == 201
    client.app.state.backup_manager.wait(30)


# ---------------- File pengguna tidak boleh dieksekusi di origin server ----------------
def test_user_files_served_with_sandbox_csp(user_client):
    c, h, _ = user_client
    svg = upload_simple(c, h, "x.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>").json()
    html = upload_simple(c, h, "x.html", b"<script>alert(1)</script>").json()
    for url in (f"/api/v1/media/{svg['id']}/stream", f"/api/v1/files/{svg['id']}/download",
                f"/api/v1/files/{html['id']}/download"):
        resp = c.get(url, headers=h)
        assert resp.status_code == 200, url
        assert "sandbox" in resp.headers["content-security-policy"]
        assert resp.headers["x-content-type-options"] == "nosniff"
