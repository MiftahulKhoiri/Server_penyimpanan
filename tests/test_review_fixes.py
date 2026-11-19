"""Tes untuk perbaikan hasil review: antrean thumbnail, retensi activity log, pemesanan kuota."""

from __future__ import annotations

import io
from datetime import timedelta

import pytest
from PIL import Image
from sqlalchemy import func, select, update

from app.database.types import utcnow
from app.models import ActivityLog, User
from app.services import file_service, media_service
from app.services.activity_service import purge_old_activity
from app.services.storage_paths import StoragePaths
from tests.conftest import upload_simple


@pytest.fixture(autouse=True)
def _reset_media_cache():
    media_service.reset_failure_cache()
    yield
    media_service.reset_failure_cache()


def _jpeg(width=400, height=300) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buf, "JPEG", quality=85)
    return buf.getvalue()


# ---------------- Antrean thumbnail ----------------
def test_thumbnail_queue_times_out_with_503(user_client, monkeypatch):
    c, h, _ = user_client
    monkeypatch.setattr(media_service, "QUEUE_WAIT_SECONDS", 0.2)
    file_id = upload_simple(c, h, "foto.jpg", _jpeg()).json()["id"]
    gate = c.app.state.thumbnail_gate  # type: ignore[attr-defined]
    c.portal.call(gate.acquire)  # type: ignore[union-attr]  # seolah thumbnail lain sedang dibuat
    try:
        resp = c.get(f"/api/v1/media/{file_id}/thumbnail", headers=h)
        assert resp.status_code == 503
        assert resp.json()["code"] == "thumbnail_busy"
        assert resp.headers["Retry-After"] == "5"
    finally:
        c.portal.call(gate.release)  # type: ignore[union-attr]
    resp = c.get(f"/api/v1/media/{file_id}/thumbnail", headers=h)
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"


def test_cached_thumbnail_served_while_queue_busy(user_client, monkeypatch):
    c, h, _ = user_client
    monkeypatch.setattr(media_service, "QUEUE_WAIT_SECONDS", 0.2)
    file_id = upload_simple(c, h, "foto.jpg", _jpeg()).json()["id"]
    assert c.get(f"/api/v1/media/{file_id}/thumbnail", headers=h).status_code == 200
    gate = c.app.state.thumbnail_gate  # type: ignore[attr-defined]
    c.portal.call(gate.acquire)  # type: ignore[union-attr]
    try:
        # Thumbnail yang sudah ada tidak perlu antre.
        resp = c.get(f"/api/v1/media/{file_id}/thumbnail", headers=h)
        assert resp.status_code == 200
    finally:
        c.portal.call(gate.release)  # type: ignore[union-attr]


def test_thumbnail_gate_released_after_error(user_client):
    c, h, _ = user_client
    file_id = upload_simple(c, h, "catatan.txt", b"bukan gambar").json()["id"]
    assert c.get(f"/api/v1/media/{file_id}/thumbnail", headers=h).status_code == 404
    gate = c.app.state.thumbnail_gate  # type: ignore[attr-defined]
    assert not gate.locked()


# ---------------- Retensi activity log ----------------
def _count(db) -> int:
    return int(db.scalar(select(func.count(ActivityLog.id))) or 0)


def test_purge_old_activity(client):
    with client.app.state.database.session() as db:  # type: ignore[attr-defined]
        db.add_all([
            ActivityLog(action="test.old", created_at=utcnow() - timedelta(days=400)),
            ActivityLog(action="test.old", created_at=utcnow() - timedelta(days=200)),
            ActivityLog(action="test.new", created_at=utcnow() - timedelta(days=5)),
        ])
        db.commit()
        before = _count(db)
        assert purge_old_activity(db, 0) == 0  # 0 = simpan selamanya
        assert _count(db) == before
        assert purge_old_activity(db, 180) == 2
        assert _count(db) == before - 2
        actions = set(db.scalars(select(ActivityLog.action)).all())
        assert "test.old" not in actions and "test.new" in actions


def test_purge_old_activity_in_batches(client, monkeypatch):
    from app.services import activity_service

    monkeypatch.setattr(activity_service, "_PURGE_BATCH", 3)
    with client.app.state.database.session() as db:  # type: ignore[attr-defined]
        old = utcnow() - timedelta(days=365)
        db.add_all([ActivityLog(action="test.old", created_at=old) for _ in range(10)])
        db.commit()
        assert purge_old_activity(db, 30) == 10
        assert db.scalar(select(func.count()).where(ActivityLog.action == "test.old")) == 0


# ---------------- Pemesanan kapasitas upload sekali kirim ----------------
def _set_quota(client, uid: int, quota: int) -> None:
    with client.app.state.database.session() as db:  # type: ignore[attr-defined]
        db.execute(update(User).where(User.id == uid).values(storage_quota_bytes=quota))
        db.commit()


def test_parallel_simple_upload_cannot_exceed_quota(user_client):
    c, h, uid = user_client
    _set_quota(c, uid, 100)
    settings = c.app.state.settings  # type: ignore[attr-defined]
    storage = StoragePaths(settings.PRIMARY_STORAGE_PATH)
    storage.ensure_layout()
    with c.app.state.database.session() as db:  # type: ignore[attr-defined]
        user = db.get(User, uid)
        # Upload lain (belum selesai) sudah memesan 80 byte.
        reservation = file_service.reserve_capacity(db, settings, storage, user, 80)
    try:
        resp = upload_simple(c, h, "b.txt", b"x" * 50)
        assert resp.status_code == 507 and resp.json()["code"] == "quota_exceeded"
    finally:
        reservation.release()
    assert upload_simple(c, h, "b.txt", b"x" * 50).status_code == 201


def test_reservation_released_after_failed_upload(user_client):
    c, h, _ = user_client
    bad_sha = "0" * 64
    resp = upload_simple(c, h, "a.txt", b"halo", extra_headers={"X-Content-SHA256": bad_sha})
    assert resp.status_code == 422
    assert file_service._reservations.total == 0
    assert file_service._reservations.per_user == {}


def test_reservation_release_is_idempotent(user_client):
    c, _, uid = user_client
    settings = c.app.state.settings  # type: ignore[attr-defined]
    storage = StoragePaths(settings.PRIMARY_STORAGE_PATH)
    storage.ensure_layout()
    with c.app.state.database.session() as db:  # type: ignore[attr-defined]
        reservation = file_service.reserve_capacity(db, settings, storage, db.get(User, uid), 10)
    reservation.release()
    reservation.release()
    assert file_service._reservations.total == 0
