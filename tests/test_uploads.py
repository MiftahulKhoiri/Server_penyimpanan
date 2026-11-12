from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import timedelta

from sqlalchemy import select, update

from app.database.types import utcnow
from app.main import run_maintenance_once
from app.models import ActivityLog, Upload, User
from app.services.storage_paths import StoragePaths
from app.services.upload_service import cleanup_uploads
from tests.conftest import JPEG_HEAD, auth_header, create_user_direct, login, storage_root

API = "/api/v1/uploads"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _init(c, h, name, data, chunk_size, **extra):
    body = {"filename": name, "total_size": len(data), "chunk_size": chunk_size, **extra}
    resp = c.post(f"{API}/init", headers=h, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _chunk(c, h, upload, index, data, sha=None):
    size = upload["chunk_size"]
    piece = data[index * size : (index + 1) * size]
    headers = {**h, "Content-Type": "application/octet-stream"}
    if sha:
        headers["X-Chunk-SHA256"] = sha
    return c.put(f"{API}/{upload['id']}/chunks/{index}", headers=headers, content=piece)


def _upload_all(c, h, name, data, chunk_size, order=None, **extra):
    up = _init(c, h, name, data, chunk_size, **extra)
    for i in order or range(up["total_chunks"]):
        resp = _chunk(c, h, up, i, data)
        assert resp.status_code == 200, resp.text
    return up, c.post(f"{API}/{up['id']}/complete", headers=h)


def _verified_from(c, file_id):
    with c.app.state.database.session() as db:
        log = db.scalar(select(ActivityLog).where(ActivityLog.action == "file.upload", ActivityLog.target_id == file_id))
    return json.loads(log.details)["verified_from"]


# ---------------- Alur dasar ----------------
def test_chunked_upload_basic(user_client):
    c, h, _ = user_client
    data = os.urandom(10_000)
    up = _init(c, h, "data.bin", data, 4096, sha256=_sha(data))
    assert up["total_chunks"] == 3 and up["status"] == "pending" and up["missing_chunks"] == 3
    for i in range(3):
        r = _chunk(c, h, up, i, data, sha=_sha(data[i * 4096 : (i + 1) * 4096]))
        assert r.status_code == 200
    assert r.json()["all_received"] is True and r.json()["received_bytes"] == len(data)
    done = c.post(f"{API}/{up['id']}/complete", headers=h)
    assert done.status_code == 200, done.text
    file = done.json()["file"]
    assert file["sha256"] == _sha(data) and file["size_bytes"] == len(data)
    assert done.json()["upload"]["status"] == "completed"
    assert c.get(f"/api/v1/files/{file['id']}/download", headers=h).content == data
    assert not (storage_root(c) / ".tmp" / "uploads" / up["id"]).exists()
    assert _verified_from(c, file["id"]) == "stream"


def test_large_file_upload(user_client):
    """File 24 MB dalam chunk 1 MB: hasil di disk identik byte-per-byte."""
    c, h, _ = user_client
    data = os.urandom(24 * 1024 * 1024 + 123)
    up, done = _upload_all(c, h, "video.mp4", b"\x00\x00\x00\x18ftypmp42" + data[12:], 1024 * 1024)
    assert done.status_code == 200, done.text
    file = done.json()["file"]
    assert up["total_chunks"] == 25
    assert file["media_type"] == "video" and file["mime_type"] == "video/mp4"
    downloaded = c.get(f"/api/v1/files/{file['id']}/download", headers=h).content
    assert _sha(downloaded) == file["sha256"] and len(downloaded) == len(data)


def test_resume_after_interruption(user_client):
    c, h, _ = user_client
    data = os.urandom(5 * 2048 + 7)
    up = _init(c, h, "resume.bin", data, 2048, sha256=_sha(data))
    for i in (0, 1, 4):
        assert _chunk(c, h, up, i, data).status_code == 200
    # "Koneksi putus": klien menanyakan status lalu mengirim yang hilang saja.
    status = c.get(f"{API}/{up['id']}/status", headers=h).json()
    assert status["received_chunks"] == [0, 1, 4] and status["missing_chunks"] == 3
    assert status["status"] == "uploading"
    early = c.post(f"{API}/{up['id']}/complete", headers=h)
    assert early.status_code == 409 and early.json()["code"] == "upload_incomplete"
    assert [u["id"] for u in c.get(API, headers=h).json()] == [up["id"]]
    missing = sorted(set(range(up["total_chunks"])) - set(status["received_chunks"]))
    for i in missing:
        assert _chunk(c, h, up, i, data).status_code == 200
    done = c.post(f"{API}/{up['id']}/complete", headers=h)
    assert done.status_code == 200
    fid = done.json()["file"]["id"]
    assert c.get(f"/api/v1/files/{fid}/download", headers=h).content == data
    assert _verified_from(c, fid) == "disk"  # urutan acak -> hash dihitung ulang dari disk


def test_resend_same_chunk_is_idempotent(user_client):
    c, h, _ = user_client
    data = os.urandom(3000)
    up = _init(c, h, "a.bin", data, 1024)
    for i in (0, 0, 1, 1, 2):
        assert _chunk(c, h, up, i, data).status_code == 200
    done = c.post(f"{API}/{up['id']}/complete", headers=h)
    assert done.status_code == 200 and done.json()["file"]["sha256"] == _sha(data)
    assert _verified_from(c, done.json()["file"]["id"]) == "stream"


def test_server_restart_mid_upload(user_client):
    c, h, _ = user_client
    data = os.urandom(4096)
    up = _init(c, h, "a.bin", data, 1024, sha256=_sha(data))
    _chunk(c, h, up, 0, data)
    _chunk(c, h, up, 1, data)
    c.app.state.upload_hashes.discard(up["id"])  # simulasi hash in-memory hilang
    _chunk(c, h, up, 2, data)
    _chunk(c, h, up, 3, data)
    done = c.post(f"{API}/{up['id']}/complete", headers=h)
    assert done.status_code == 200 and _verified_from(c, done.json()["file"]["id"]) == "disk"


# ---------------- Validasi chunk ----------------
def test_chunk_size_validation(user_client):
    c, h, _ = user_client
    data = os.urandom(3000)
    up = _init(c, h, "a.bin", data, 1024)
    url = f"{API}/{up['id']}/chunks"
    short = c.put(f"{url}/0", headers=h, content=b"x" * 10)
    assert short.status_code == 400 and short.json()["code"] == "chunk_size_mismatch"
    last_wrong = c.put(f"{url}/2", headers=h, content=b"x" * 1024)
    assert last_wrong.status_code == 400
    assert c.put(f"{url}/2", headers=h, content=b"x" * (3000 - 2048)).status_code == 200
    out_of_range = c.put(f"{url}/3", headers=h, content=b"x")
    assert out_of_range.status_code == 422 and out_of_range.json()["code"] == "invalid_chunk_index"
    assert c.put(f"{url}/-1", headers=h, content=b"x").status_code == 422


def test_chunk_streamed_without_content_length_too_large(user_client):
    c, h, _ = user_client
    data = os.urandom(2048)
    up = _init(c, h, "a.bin", data, 1024)

    def body():
        for _ in range(4):
            yield b"y" * 512

    resp = c.put(f"{API}/{up['id']}/chunks/0", headers=h, content=body())
    assert resp.status_code == 413 and resp.json()["code"] == "chunk_too_large"
    assert c.get(f"{API}/{up['id']}/status", headers=h).json()["received_chunks"] == []


def test_chunk_checksum_mismatch_not_recorded(user_client):
    c, h, _ = user_client
    data = os.urandom(2048)
    up = _init(c, h, "a.bin", data, 1024)
    resp = _chunk(c, h, up, 0, data, sha="a" * 64)
    assert resp.status_code == 422 and resp.json()["code"] == "chunk_checksum_mismatch"
    assert c.get(f"{API}/{up['id']}/status", headers=h).json()["received_chunks"] == []
    assert _chunk(c, h, up, 0, data, sha=_sha(data[:1024])).status_code == 200


def test_final_checksum_mismatch_fails_upload(user_client):
    c, h, _ = user_client
    data = os.urandom(3000)
    up, done = _upload_all(c, h, "a.bin", data, 1024, sha256="b" * 64)
    assert done.status_code == 422 and done.json()["code"] == "checksum_mismatch"
    status = c.get(f"{API}/{up['id']}/status", headers=h).json()
    assert status["status"] == "failed" and status["error_message"] == "checksum_mismatch"
    assert not (storage_root(c) / ".tmp" / "uploads" / up["id"]).exists()
    assert c.get("/api/v1/files", headers=h).json()["total"] == 0


def test_corruption_on_disk_detected(user_client):
    c, h, _ = user_client
    data = os.urandom(4096)
    up = _init(c, h, "a.bin", data, 1024)
    for i in (1, 0, 2, 3):  # urutan acak memaksa verifikasi dari disk
        _chunk(c, h, up, i, data)
    part = storage_root(c) / ".tmp" / "uploads" / up["id"] / "data.part"
    with open(part, "r+b") as fh:
        fh.seek(1500)
        fh.write(b"\x00\x01\x02")
    done = c.post(f"{API}/{up['id']}/complete", headers=h)
    assert done.status_code == 422 and done.json()["code"] == "upload_corrupted"


def test_mime_mismatch_detected_on_first_chunk(user_client):
    c, h, _ = user_client
    data = b"%PDF-1.5" + os.urandom(3000)
    up = _init(c, h, "foto.jpg", data, 1024)
    resp = _chunk(c, h, up, 0, data)
    assert resp.status_code == 415 and resp.json()["code"] == "mime_mismatch"
    assert c.get(f"{API}/{up['id']}/status", headers=h).json()["status"] == "failed"
    assert _chunk(c, h, up, 1, data).status_code == 409


# ---------------- Duplikat, batal, hak akses ----------------
def test_duplicate_detection(user_client):
    c, h, _ = user_client
    data = JPEG_HEAD + os.urandom(2000)
    _, done = _upload_all(c, h, "foto.jpg", data, 1024, sha256=_sha(data))
    first_id = done.json()["file"]["id"]
    again = _init(c, h, "foto-lagi.jpg", data, 1024, sha256=_sha(data))
    assert again["duplicate_file_id"] == first_id
    other = _init(c, h, "lain.jpg", data, 1024, sha256="c" * 64)
    assert other["duplicate_file_id"] is None


def test_cancel_upload(user_client):
    c, h, _ = user_client
    data = os.urandom(3000)
    up = _init(c, h, "a.bin", data, 1024)
    _chunk(c, h, up, 0, data)
    resp = c.post(f"{API}/{up['id']}/cancel", headers=h)
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
    assert not (storage_root(c) / ".tmp" / "uploads" / up["id"]).exists()
    assert _chunk(c, h, up, 1, data).status_code == 409
    assert c.post(f"{API}/{up['id']}/complete", headers=h).status_code == 409
    assert c.post(f"{API}/{up['id']}/cancel", headers=h).status_code == 409


def test_other_user_cannot_touch_upload(user_client):
    c, h, _ = user_client
    data = os.urandom(2048)
    up = _init(c, h, "a.bin", data, 1024)
    create_user_direct(c, "siti")
    hs = auth_header(login(c, "siti"))
    assert _chunk(c, hs, up, 0, data).status_code == 404
    assert c.get(f"{API}/{up['id']}/status", headers=hs).status_code == 404
    assert c.post(f"{API}/{up['id']}/complete", headers=hs).status_code == 404
    assert c.post(f"{API}/{up['id']}/cancel", headers=hs).status_code == 404
    assert c.get(f"{API}/../../etc/passwd/status", headers=h).status_code == 404


def test_init_validation(make_client):
    c = make_client(MAX_UPLOAD_SIZE_BYTES=10_000, MAX_ACTIVE_UPLOADS_PER_USER=2, UPLOAD_CHUNK_SIZE_MAX=4096, UPLOAD_CHUNK_SIZE_DEFAULT=2048)
    create_user_direct(c, "budi")
    h = auth_header(login(c, "budi"))
    big = c.post(f"{API}/init", headers=h, json={"filename": "a.bin", "total_size": 10_001})
    assert big.status_code == 413
    bad_chunk = c.post(f"{API}/init", headers=h, json={"filename": "a.bin", "total_size": 100, "chunk_size": 999_999})
    assert bad_chunk.status_code == 422 and bad_chunk.json()["code"] == "invalid_chunk_size"
    assert c.post(f"{API}/init", headers=h, json={"filename": "../a", "total_size": 10}).status_code == 422
    assert c.post(f"{API}/init", headers=h, json={"filename": "a", "total_size": 0}).status_code == 422
    assert c.post(f"{API}/init", headers=h, json={"filename": "a", "total_size": 10, "sha256": "zz"}).status_code == 422
    for i in range(2):
        assert c.post(f"{API}/init", headers=h, json={"filename": f"{i}.bin", "total_size": 2048, "chunk_size": 1024}).status_code == 201
    many = c.post(f"{API}/init", headers=h, json={"filename": "x.bin", "total_size": 2048, "chunk_size": 1024})
    assert many.status_code == 429 and many.json()["code"] == "too_many_uploads"


def test_init_quota_counts_active_uploads(user_client):
    c, h, uid = user_client
    with c.app.state.database.session() as db:
        db.execute(update(User).where(User.id == uid).values(storage_quota_bytes=5000))
        db.commit()
    _init(c, h, "a.bin", b"x" * 3000, 1024)
    resp = c.post(f"{API}/init", headers=h, json={"filename": "b.bin", "total_size": 3000, "chunk_size": 1024})
    assert resp.status_code == 507 and resp.json()["code"] == "quota_exceeded"


def test_target_folder_deleted_during_upload(user_client):
    c, h, _ = user_client
    folder = c.post("/api/v1/folders", headers=h, json={"name": "Tujuan"}).json()
    data = os.urandom(2048)
    up = _init(c, h, "a.bin", data, 1024, folder_id=folder["id"])
    _chunk(c, h, up, 0, data)
    _chunk(c, h, up, 1, data)
    c.delete(f"/api/v1/folders/{folder['id']}", headers=h)
    done = c.post(f"{API}/{up['id']}/complete", headers=h)
    assert done.status_code == 200 and done.json()["file"]["folder_id"] is None


# ---------------- Kedaluwarsa & pembersihan ----------------
def test_expired_upload_rejected_and_cleaned(user_client):
    c, h, _ = user_client
    data = os.urandom(2048)
    up = _init(c, h, "a.bin", data, 1024)
    _chunk(c, h, up, 0, data)
    with c.app.state.database.session() as db:
        db.execute(update(Upload).where(Upload.id == up["id"]).values(expires_at=utcnow() - timedelta(minutes=1)))
        db.commit()
    resp = _chunk(c, h, up, 1, data)
    assert resp.status_code == 410 and resp.json()["code"] == "upload_expired"
    stats = run_maintenance_once(c.app)
    assert stats["expired"] == 1
    assert c.get(f"{API}/{up['id']}/status", headers=h).json()["status"] == "expired"
    assert not (storage_root(c) / ".tmp" / "uploads" / up["id"]).exists()


def test_cleanup_orphans_and_old_rows(user_client):
    c, h, _ = user_client
    storage = StoragePaths(storage_root(c))
    storage.ensure_layout()
    orphan = storage.uploads_tmp / "00000000-0000-0000-0000-000000000001"
    orphan.mkdir()
    (orphan / "data.part").write_bytes(b"sisa")
    stale = storage.simple_tmp / "lama.part"
    stale.write_bytes(b"sisa")
    old = time.time() - 7 * 3600
    for p in (orphan, stale):
        os.utime(p, (old, old))
    data = os.urandom(1024)
    up = _init(c, h, "a.bin", data, 1024)
    c.post(f"{API}/{up['id']}/cancel", headers=h)
    with c.app.state.database.session() as db:
        db.execute(update(Upload).where(Upload.id == up["id"]).values(updated_at=utcnow() - timedelta(days=8)))
        db.commit()
        stats = cleanup_uploads(db, storage)
    assert stats["orphan_dirs"] == 1 and stats["temp_files"] == 1 and stats["rows_deleted"] == 1
    assert not orphan.exists() and not stale.exists()
    assert c.get(f"{API}/{up['id']}/status", headers=h).status_code == 404
