from __future__ import annotations

import os

from sqlalchemy import select, update

from app.models import StorageDevice, User
from app.services import storage_guard, storage_service
from tests.conftest import auth_header, create_user_direct, login, upload_simple


def _admin(c):
    create_user_direct(c, "admin", role="admin")
    return auth_header(login(c, "admin"))


# ---------------- Storage ----------------
def test_storage_status_for_user(user_client):
    c, h, uid = user_client
    upload_simple(c, h, "a.bin", b"x" * 100)
    gone = upload_simple(c, h, "b.bin", b"y" * 50).json()["id"]
    c.delete(f"/api/v1/files/{gone}", headers=h)
    with c.app.state.database.session() as db:
        db.execute(update(User).where(User.id == uid).values(storage_quota_bytes=1000))
        db.commit()
    body = c.get("/api/v1/storage/status", headers=h).json()
    assert body["usage"] == {
        "file_count": 1, "used_bytes": 100, "trash_bytes": 50, "quota_bytes": 1000, "quota_free_bytes": 850,
    }
    assert body["primary"]["available"] is True and body["primary"]["total_bytes"] > 0
    assert "mount_point" not in body["primary"]  # detail perangkat hanya untuk admin


def test_ssd_hdd_details_admin_only(user_client):
    c, h, _ = user_client
    assert c.get("/api/v1/storage/ssd", headers=h).status_code == 403
    ha = _admin(c)
    ssd = c.get("/api/v1/storage/ssd", headers=ha).json()
    assert ssd["role"] == "primary" and ssd["available"] is True
    assert ssd["total_bytes"] >= ssd["free_bytes"] > 0
    hdd = c.get("/api/v1/storage/hdd", headers=ha).json()
    assert hdd["role"] == "backup"
    with c.app.state.database.session() as db:
        rows = {r.name: r for r in db.scalars(select(StorageDevice)).all()}
    assert set(rows) == {"ssd", "hdd"} and rows["ssd"].last_checked_at is not None


def test_hdd_not_mounted_server_keeps_running(user_client, monkeypatch):
    """Simulasi produksi: SSD ter-mount, HDD tidak."""
    c, h, _ = user_client
    settings = c.app.state.settings
    ssd_mount = str(settings.PRIMARY_MOUNT_POINT)
    real_ismount = os.path.ismount

    def fake_ismount(path):
        return str(path) == ssd_mount or real_ismount(path)

    monkeypatch.setattr(storage_guard.os.path, "ismount", fake_ismount)
    monkeypatch.setattr(storage_service.os.path, "ismount", fake_ismount)
    settings.REQUIRE_MOUNTED_STORAGE = True
    ha = _admin(c)

    hdd = c.get("/api/v1/storage/hdd", headers=ha).json()
    assert hdd["mounted"] is False and hdd["available"] is False and hdd["reason"] == "not_mounted"
    assert hdd["total_bytes"] is None
    ssd = c.get("/api/v1/storage/ssd", headers=ha).json()
    assert ssd["mounted"] is True and ssd["available"] is True
    health = c.get("/api/v1/system/health")
    assert health.status_code == 200 and health.json()["backup_storage"] == "unavailable"
    status = c.get("/api/v1/storage/status", headers=h).json()
    assert status["backup"]["available"] is False and status["primary"]["available"] is True
    # Fungsi utama tetap berjalan tanpa HDD.
    assert upload_simple(c, h, "tetap-bisa.txt", b"ok").status_code == 201
    assert c.get("/api/v1/system/status", headers=ha).json()["status"] == "ok"


def test_storage_requires_auth(client):
    assert client.get("/api/v1/storage/status").status_code == 401


# ---------------- Dashboard sistem ----------------
def test_system_status(user_client):
    c, h, _ = user_client
    assert c.get("/api/v1/system/status", headers=h).status_code == 403
    body = c.get("/api/v1/system/status", headers=_admin(c)).json()
    assert body["status"] == "ok" and body["database"] == "ok"
    assert 0 <= body["cpu"]["percent"] <= 100 and body["cpu"]["count"] >= 1
    assert body["memory"]["total_bytes"] > body["memory"]["available_bytes"] > 0
    assert body["memory"]["process_rss_bytes"] > 0
    assert body["server_uptime_seconds"] >= 0 and body["system_uptime_seconds"] > 0
    assert set(body["storage"]) == {"ssd", "hdd"}
    assert body["media_tools"]["pillow"] is True


def test_system_statistics(user_client):
    c, h, _ = user_client
    upload_simple(c, h, "a.jpg", b"\xff\xd8\xff\xe0" + b"x" * 10)
    upload_simple(c, h, "b.txt", b"12345")
    gone = upload_simple(c, h, "c.txt", b"123").json()["id"]
    c.delete(f"/api/v1/files/{gone}", headers=h)
    c.post("/api/v1/folders", headers=h, json={"name": "F"})
    create_user_direct(c, "nonaktif", is_active=False)
    ha = _admin(c)
    assert c.get("/api/v1/system/statistics", headers=h).status_code == 403
    stats = c.get("/api/v1/system/statistics", headers=ha).json()
    assert stats["users"] == {"total": 3, "active": 2}
    assert stats["files"]["count"] == 2 and stats["files"]["total_bytes"] == 14 + 5
    assert stats["files"]["by_media_type"]["image"] == {"count": 1, "total_bytes": 14}
    assert stats["files"]["by_media_type"]["document"] == {"count": 1, "total_bytes": 5}
    assert stats["folders"] == 1
    assert stats["trash"] == {"items": 1, "total_bytes": 3}
    assert stats["last_24h"]["uploads"] == 3
