"""Race condition upload chunked (ditemukan oleh uji beban TAHAP 10)."""

from __future__ import annotations

import asyncio
import hashlib
import os

import httpx
import pytest

from app.services import upload_service
from app.utils.mime import decide_mime
from tests.conftest import auth_header, create_user_direct, login


@pytest.fixture
def app_and_headers(make_client):
    c = make_client(raise_server_exceptions=False)
    create_user_direct(c, "budi")
    return c.app, auth_header(login(c, "budi")), c


def _run(coro):
    return asyncio.run(coro)


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30)


def test_failed_upload_cannot_be_resurrected_by_inflight_chunk(app_and_headers, monkeypatch):
    """Chunk 0 ditolak (mime) saat chunk lain sedang ditulis: upload harus tetap 'failed'."""
    app, h, c = app_and_headers
    up = c.post("/api/v1/uploads/init", headers=h, json={"filename": "foto.jpg", "total_size": 3072, "chunk_size": 1024}).json()
    data = b"%PDF-1.4" + os.urandom(3064)
    real_record = upload_service.record_chunk
    gate = asyncio.Event()

    async def scenario():
        client = await _client(app)

        async def slow_body(piece):
            await gate.wait()  # tertahan sampai chunk 0 sudah ditolak
            yield piece

        async def chunk1():
            return await client.put(f"/api/v1/uploads/{up['id']}/chunks/1", headers=h, content=slow_body(data[1024:2048]))

        task = asyncio.create_task(chunk1())
        await asyncio.sleep(0.2)
        first = await client.put(f"/api/v1/uploads/{up['id']}/chunks/0", headers=h, content=data[:1024])
        gate.set()
        second = await task
        status = (await client.get(f"/api/v1/uploads/{up['id']}/status", headers=h)).json()
        await client.aclose()
        return first, second, status

    monkeypatch.setattr(upload_service, "record_chunk", real_record)
    first, second, status = _run(scenario())
    assert first.status_code == 415
    assert second.status_code == 409, second.text  # bukan 500, dan tidak menghidupkan upload lagi
    assert status["status"] == "failed"


def test_complete_waits_for_inflight_chunks_and_rejects_late_ones(app_and_headers):
    """Chunk yang terlambat tidak boleh menulis ke file setelah complete menghitung checksum."""
    app, h, c = app_and_headers
    data = os.urandom(2048)
    up = c.post("/api/v1/uploads/init", headers=h, json={"filename": "a.bin", "total_size": 2048, "chunk_size": 1024}).json()
    c.put(f"/api/v1/uploads/{up['id']}/chunks/0", headers=h, content=data[:1024])
    c.put(f"/api/v1/uploads/{up['id']}/chunks/1", headers=h, content=data[1024:])
    gate = asyncio.Event()

    async def scenario():
        client = await _client(app)

        async def late_body():
            await gate.wait()
            yield b"\x00" * 1024  # isi berbeda: akan merusak file bila sempat ditulis

        async def late_chunk():
            return await client.put(f"/api/v1/uploads/{up['id']}/chunks/0", headers=h, content=late_body())

        late = asyncio.create_task(late_chunk())
        await asyncio.sleep(0.2)  # chunk terlambat sudah lolos cek status & memegang kunci bersama
        complete = asyncio.create_task(client.post(f"/api/v1/uploads/{up['id']}/complete", headers=h))
        await asyncio.sleep(0.2)
        assert not complete.done()  # complete menunggu chunk yang sedang ditulis
        gate.set()
        late_resp, done = await late, await complete
        await client.aclose()
        return late_resp, done

    late_resp, done = _run(scenario())
    assert late_resp.status_code == 200  # selesai sebelum complete mengklaim upload
    assert done.status_code == 200, done.text
    file = done.json()["file"]
    expected = b"\x00" * 1024 + data[1024:]
    assert file["sha256"] == hashlib.sha256(expected).hexdigest()
    body = c.get(f"/api/v1/files/{file['id']}/download", headers=h).content
    assert hashlib.sha256(body).hexdigest() == file["sha256"]  # isi di disk = checksum yang dicatat
    after = c.put(f"/api/v1/uploads/{up['id']}/chunks/0", headers=h, content=data[:1024])
    assert after.status_code == 409


def test_chunk_after_cancel_returns_409_not_500(app_and_headers):
    app, h, c = app_and_headers
    up = c.post("/api/v1/uploads/init", headers=h, json={"filename": "a.bin", "total_size": 2048, "chunk_size": 1024}).json()
    gate = asyncio.Event()

    async def scenario():
        client = await _client(app)

        async def body():
            await gate.wait()
            yield b"y" * 1024

        chunk = asyncio.create_task(client.put(f"/api/v1/uploads/{up['id']}/chunks/1", headers=h, content=body()))
        await asyncio.sleep(0.2)
        cancel = asyncio.create_task(client.post(f"/api/v1/uploads/{up['id']}/cancel", headers=h))
        await asyncio.sleep(0.1)
        gate.set()
        chunk_resp, cancel_resp = await chunk, await cancel
        status = (await client.get(f"/api/v1/uploads/{up['id']}/status", headers=h)).json()
        await client.aclose()
        return chunk_resp, cancel_resp, status

    chunk_resp, cancel_resp, status = _run(scenario())
    assert chunk_resp.status_code == 200 and cancel_resp.status_code == 200
    assert status["status"] == "cancelled"


# ---------------- MIME: tanda tangan lemah tidak boleh menolak file ----------------
@pytest.mark.parametrize("head", [b"\xff\xfb\x90\x00", b"\xff\xf3abc", b"\x1f\x8b\x08\x00", b"BMxx"])
def test_weak_signatures_do_not_reject_generic_or_document_files(head):
    for name in ("acak.bin", "tanpa-ekstensi", "catatan.txt", "data.csv"):
        decide_mime(name, head + os.urandom(100))  # tidak boleh melempar MimeMismatch


def test_strong_signature_mismatch_still_rejected():
    from app.utils.mime import MimeMismatch

    with pytest.raises(MimeMismatch):
        decide_mime("foto.jpg", b"%PDF-1.7 ...")
    with pytest.raises(MimeMismatch):
        decide_mime("lagu.mp3", b"\x89PNG\r\n\x1a\n....")
    assert decide_mime("arsip.bin", b"%PDF-1.7").mime_type == "application/pdf"
    assert decide_mime("lagu.mp3", b"\xff\xfb\x90\x00").mime_type == "audio/mpeg"


def test_compressed_archives_detected():
    assert decide_mime("arsip.gz", b"\x1f\x8b\x08\x00").mime_type == "application/gzip"
    assert decide_mime("cadangan.tar.gz", b"\x1f\x8b\x08\x00").mime_type == "application/gzip"
