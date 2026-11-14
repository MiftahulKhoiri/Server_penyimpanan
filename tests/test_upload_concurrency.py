"""Konkurensi upload: upload paralel, kedaluwarsa vs finalisasi, pemulihan crash, disk penuh, kunci instance."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
from datetime import timedelta

import httpx
from sqlalchemy import update

from app.api.v1 import uploads as uploads_api
from app.database.types import utcnow
from app.main import run_maintenance_once
from app.models import Upload, User
from app.services import upload_service
from app.services.storage_paths import StoragePaths
from app.utils.request_info import ClientInfo
from tests.conftest import auth_header, create_user_direct, login, storage_root

CHUNK = 1024


def _init(c, h, name, data):
    resp = c.post("/api/v1/uploads/init", headers=h,
                  json={"filename": name, "total_size": len(data), "chunk_size": CHUNK,
                        "sha256": hashlib.sha256(data).hexdigest()})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_parallel_uploads_do_not_mix_chunks(make_client):
    c = make_client()
    create_user_direct(c, "budi")
    h = auth_header(login(c, "budi"))
    blobs = [os.urandom(CHUNK * 6 + 77) for _ in range(3)]
    ids = [_init(c, h, f"f{i}.bin", blob) for i, blob in enumerate(blobs)]

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url="http://t", timeout=30) as ac:
            jobs = []
            for idx in reversed(range(7)):  # urutan acak + saling bersilang antar-upload
                for uid, blob in zip(ids, blobs, strict=True):
                    piece = blob[idx * CHUNK:(idx + 1) * CHUNK]
                    jobs.append(ac.put(f"/api/v1/uploads/{uid}/chunks/{idx}", headers=h, content=piece))
            codes = [r.status_code for r in await asyncio.gather(*jobs)]
            assert set(codes) == {200}, codes
            done = await asyncio.gather(*[ac.post(f"/api/v1/uploads/{uid}/complete", headers=h) for uid in ids])
            return [r.json() for r in done]

    results = asyncio.run(scenario())
    for blob, res in zip(blobs, results, strict=True):
        fid = res["file"]["id"]
        assert c.get(f"/api/v1/files/{fid}/download", headers=h).content == blob


def test_expiry_does_not_touch_upload_being_finalized(make_client):
    c = make_client()
    create_user_direct(c, "budi")
    h = auth_header(login(c, "budi"))
    data = os.urandom(CHUNK * 2)
    uid = _init(c, h, "a.bin", data)
    for i in range(2):
        assert c.put(f"/api/v1/uploads/{uid}/chunks/{i}", headers=h, content=data[i * CHUNK:(i + 1) * CHUNK]).status_code == 200
    app = c.app
    storage = StoragePaths(storage_root(c))
    with app.state.database.session() as db:
        upload = db.get(Upload, uid)
        upload_service.claim_upload(db, upload)  # complete sedang berjalan ('processing')
        db.execute(update(Upload).where(Upload.id == uid).values(expires_at=utcnow() - timedelta(hours=1)))
        db.commit()
    stats = run_maintenance_once(app)
    assert stats["expired"] == 0
    assert storage.upload_dir(uid).is_dir()  # data sementara tidak dihapus di tengah finalisasi
    with app.state.database.session() as db:
        upload = db.get(Upload, uid)
        assert upload.status == "processing"
        stored = upload_service.finish_claimed_upload(
            db, app.state.settings, storage, app.state.upload_hashes, db.get(User, upload.owner_id), upload,
            ClientInfo(None, None),
        )
    assert c.get(f"/api/v1/files/{stored.id}/download", headers=h).content == data


def test_writable_upload_still_expires(make_client):
    c = make_client()
    create_user_direct(c, "budi")
    h = auth_header(login(c, "budi"))
    uid = _init(c, h, "a.bin", os.urandom(CHUNK * 2))
    with c.app.state.database.session() as db:
        db.execute(update(Upload).where(Upload.id == uid).values(expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    assert run_maintenance_once(c.app)["expired"] == 1
    assert not StoragePaths(storage_root(c)).upload_dir(uid).exists()


def test_processing_upload_recovered_after_crash(make_client):
    c1 = make_client(SINGLE_INSTANCE_LOCK=True)
    create_user_direct(c1, "budi")
    h = auth_header(login(c1, "budi"))
    data = os.urandom(CHUNK * 3)
    uid = _init(c1, h, "a.bin", data)
    for i in range(3):
        c1.put(f"/api/v1/uploads/{uid}/chunks/{i}", headers=h, content=data[i * CHUNK:(i + 1) * CHUNK])
    with c1.app.state.database.session() as db:
        db.execute(update(Upload).where(Upload.id == uid).values(status="processing"))
        db.commit()
    c1.__exit__(None, None, None)  # "crash" di tengah finalisasi, lalu server start lagi
    c2 = make_client(SINGLE_INSTANCE_LOCK=True)
    h2 = auth_header(login(c2, "budi"))
    assert c2.get(f"/api/v1/uploads/{uid}/status", headers=h2).json()["status"] == "uploading"
    done = c2.post(f"/api/v1/uploads/{uid}/complete", headers=h2)
    assert done.status_code == 200, done.text
    assert c2.get(f"/api/v1/files/{done.json()['file']['id']}/download", headers=h2).content == data


def test_second_server_instance_refused(make_client):
    first = make_client(SINGLE_INSTANCE_LOCK=True)
    create_user_direct(first, "budi")
    second = make_client(SINGLE_INSTANCE_LOCK=True)  # mis. uvicorn --workers 2
    assert second.app.state.database.error == "another_instance_running"
    resp = second.post("/api/v1/auth/login", json={"username": "budi", "password": "x" * 10})
    assert resp.status_code == 503
    assert first.post("/api/v1/auth/login", json={"username": "budi", "password": "Rahasia123"}).status_code == 200


def test_disk_full_during_chunk_returns_507_and_stays_resumable(make_client, monkeypatch):
    c = make_client(raise_server_exceptions=False)
    create_user_direct(c, "budi")
    h = auth_header(login(c, "budi"))
    data = os.urandom(CHUNK * 2)
    uid = _init(c, h, "a.bin", data)
    real = uploads_api.write_all

    def full(fd, piece, offset):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(uploads_api, "write_all", full)
    resp = c.put(f"/api/v1/uploads/{uid}/chunks/0", headers=h, content=data[:CHUNK])
    assert resp.status_code == 507 and resp.json()["code"] == "insufficient_storage"
    status = c.get(f"/api/v1/uploads/{uid}/status", headers=h).json()
    assert status["received_chunks"] == [] and status["status"] in ("pending", "uploading")
    monkeypatch.setattr(uploads_api, "write_all", real)  # ruang tersedia lagi: lanjutkan
    for i in range(2):
        assert c.put(f"/api/v1/uploads/{uid}/chunks/{i}", headers=h, content=data[i * CHUNK:(i + 1) * CHUNK]).status_code == 200
    assert c.post(f"/api/v1/uploads/{uid}/complete", headers=h).status_code == 200


def test_disk_full_on_simple_upload_cleans_temp(make_client, monkeypatch):
    from app.api.v1 import files as files_api

    c = make_client(raise_server_exceptions=False)
    create_user_direct(c, "budi")
    h = auth_header(login(c, "budi"))

    def full(fd, piece, offset):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(files_api, "write_all", full)
    resp = c.post("/api/v1/files", params={"filename": "a.bin"}, content=b"x" * 2000,
                  headers={**h, "Content-Type": "application/octet-stream"})
    assert resp.status_code == 507
    assert not list(StoragePaths(storage_root(c)).simple_tmp.iterdir())
