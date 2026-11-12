from __future__ import annotations

import hashlib
import os
from collections import namedtuple

from sqlalchemy import select, update

from app.models import StoredFile, TrashItem, User
from app.services import file_service
from tests.conftest import (
    JPEG_HEAD,
    auth_header,
    create_user_direct,
    login,
    storage_root,
    upload_simple,
)

API = "/api/v1/files"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------- Upload kecil ----------------
def test_upload_small_photo(user_client):
    c, h, uid = user_client
    data = JPEG_HEAD + os.urandom(5000)
    resp = upload_simple(c, h, "liburan.jpg", data)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "liburan.jpg"
    assert body["mime_type"] == "image/jpeg" and body["media_type"] == "image"
    assert body["size_bytes"] == len(data) and body["sha256"] == _sha(data)
    assert "storage_path" not in body
    with c.app.state.database.session() as db:
        row = db.get(StoredFile, body["id"])
        assert row.storage_path == f"files/{uid}/{body['id'][:2]}/{body['id']}"
    stored = storage_root(c) / row.storage_path
    assert stored.read_bytes() == data
    assert not list((storage_root(c) / ".tmp" / "simple").iterdir())


def test_upload_document_and_unknown_types(user_client):
    c, h, _ = user_client
    pdf = upload_simple(c, h, "laporan.pdf", b"%PDF-1.7\n" + b"x" * 100).json()
    assert pdf["media_type"] == "document" and pdf["mime_type"] == "application/pdf"
    other = upload_simple(c, h, "data.bin", os.urandom(64)).json()
    assert other["media_type"] == "other" and other["mime_type"] == "application/octet-stream"
    empty = upload_simple(c, h, "kosong.txt", b"")
    assert empty.status_code == 201 and empty.json()["size_bytes"] == 0


def test_upload_requires_auth(client):
    assert upload_simple(client, {}, "a.txt", b"x").status_code == 401


def test_upload_checksum_header(user_client):
    c, h, _ = user_client
    data = b"isi dokumen penting"
    ok = upload_simple(c, h, "a.txt", data, extra_headers={"X-Content-SHA256": _sha(data)})
    assert ok.status_code == 201
    bad = upload_simple(c, h, "b.txt", data, extra_headers={"X-Content-SHA256": "0" * 64})
    assert bad.status_code == 422 and bad.json()["code"] == "checksum_mismatch"
    assert not list((storage_root(c) / ".tmp" / "simple").iterdir())


def test_upload_too_large(make_client):
    c = make_client(MAX_SIMPLE_UPLOAD_BYTES=100)
    create_user_direct(c, "budi")
    h = auth_header(login(c, "budi"))
    resp = upload_simple(c, h, "besar.bin", b"x" * 101)
    assert resp.status_code == 413 and resp.json()["code"] == "file_too_large"


def test_path_traversal_names_rejected(user_client):
    c, h, _ = user_client
    for name in ["../../etc/passwd", "a/b.txt", "..", "a\\b.txt", "nul\x00.txt", "baris\nbaru.txt"]:
        resp = upload_simple(c, h, name, b"x")
        assert resp.status_code == 422, name
    # Tidak ada file yang tercipta di luar direktori penyimpanan.
    assert not (storage_root(c).parent / "etc").exists()


def test_mime_mismatch_rejected(user_client):
    c, h, _ = user_client
    resp = upload_simple(c, h, "foto.jpg", b"%PDF-1.4 bukan foto")
    assert resp.status_code == 415 and resp.json()["code"] == "mime_mismatch"
    assert not list((storage_root(c) / ".tmp" / "simple").iterdir())
    assert not (storage_root(c) / "files").exists() or not any((storage_root(c) / "files").rglob("*-*"))


def test_blocked_extension(make_client):
    c = make_client(BLOCKED_EXTENSIONS="exe, .bat")
    create_user_direct(c, "budi")
    h = auth_header(login(c, "budi"))
    assert upload_simple(c, h, "virus.EXE", b"MZ").status_code == 415
    assert upload_simple(c, h, "x.bat", b"echo").status_code == 415
    assert upload_simple(c, h, "aman.txt", b"ok").status_code == 201


def test_duplicate_names_get_numbered(user_client):
    c, h, _ = user_client
    assert upload_simple(c, h, "catatan.txt", b"1").json()["name"] == "catatan.txt"
    assert upload_simple(c, h, "CATATAN.txt", b"2").json()["name"] == "CATATAN (1).txt"
    assert upload_simple(c, h, "catatan.txt", b"3").json()["name"] == "catatan (2).txt"


# ---------------- Download ----------------
def test_download_full(user_client):
    c, h, _ = user_client
    data = os.urandom(70_000)
    fid = upload_simple(c, h, "arsip.bin", data).json()["id"]
    resp = c.get(f"{API}/{fid}/download", headers=h)
    assert resp.status_code == 200
    assert resp.content == data
    assert resp.headers["content-disposition"].startswith("attachment")
    assert resp.headers["x-content-sha256"] == _sha(data)
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_download_range_request(user_client):
    c, h, _ = user_client
    data = bytes(range(256)) * 100
    fid = upload_simple(c, h, "x.bin", data).json()["id"]
    resp = c.get(f"{API}/{fid}/download", headers={**h, "Range": "bytes=100-199"})
    assert resp.status_code == 206
    assert resp.content == data[100:200]
    assert resp.headers["content-range"] == f"bytes 100-199/{len(data)}"
    tail = c.get(f"{API}/{fid}/download", headers={**h, "Range": "bytes=-50"})
    assert tail.status_code == 206 and tail.content == data[-50:]
    bad = c.get(f"{API}/{fid}/download", headers={**h, "Range": f"bytes={len(data) + 10}-"})
    assert bad.status_code == 416


def test_download_unicode_filename(user_client):
    c, h, _ = user_client
    fid = upload_simple(c, h, "Foto Pernikahan Ö.txt", b"x").json()["id"]
    resp = c.get(f"{API}/{fid}/download", headers=h)
    assert "filename*=utf-8''Foto%20Pernikahan%20%C3%96.txt" in resp.headers["content-disposition"]


def test_download_missing_physical_file(user_client):
    c, h, _ = user_client
    fid = upload_simple(c, h, "a.txt", b"x").json()["id"]
    with c.app.state.database.session() as db:
        (storage_root(c) / db.get(StoredFile, fid).storage_path).unlink()
    resp = c.get(f"{API}/{fid}/download", headers=h)
    assert resp.status_code == 404 and resp.json()["code"] == "file_data_missing"


def test_tampered_storage_path_cannot_escape(user_client, tmp_path):
    c, h, _ = user_client
    secret = tmp_path / "rahasia.txt"
    secret.write_text("JANGAN BOCOR")
    fid = upload_simple(c, h, "a.txt", b"x").json()["id"]
    with c.app.state.database.session() as db:
        db.execute(update(StoredFile).where(StoredFile.id == fid).values(storage_path="../../rahasia.txt"))
        db.commit()
    resp = c.get(f"{API}/{fid}/download", headers=h)
    assert resp.status_code == 404
    assert "JANGAN BOCOR" not in resp.text


# ---------------- Hak akses ----------------
def test_other_user_cannot_access_file(user_client):
    c, h, _ = user_client
    fid = upload_simple(c, h, "pribadi.txt", b"rahasia").json()["id"]
    create_user_direct(c, "siti")
    hs = auth_header(login(c, "siti"))
    assert c.get(f"{API}/{fid}", headers=hs).status_code == 404
    assert c.get(f"{API}/{fid}/download", headers=hs).status_code == 404
    assert c.patch(f"{API}/{fid}/rename", headers=hs, json={"name": "x.txt"}).status_code == 404
    assert c.post(f"{API}/{fid}/copy", headers=hs, json={}).status_code == 404
    assert c.delete(f"{API}/{fid}", headers=hs).status_code == 404
    assert c.get(API, headers=hs).json()["total"] == 0
    assert c.get(f"{API}/{fid}", headers=h).status_code == 200


def test_file_not_found(user_client):
    c, h, _ = user_client
    resp = c.get(f"{API}/00000000-0000-0000-0000-000000000000", headers=h)
    assert resp.status_code == 404 and resp.json()["code"] == "file_not_found"
    assert c.get(f"{API}/..%2F..%2Fetc%2Fpasswd/download", headers=h).status_code == 404
    assert c.get(f"{API}/{'a' * 300}", headers=h).status_code == 404


# ---------------- Rename / move / copy / delete ----------------
def test_rename(user_client):
    c, h, _ = user_client
    fid = upload_simple(c, h, "lama.txt", b"x").json()["id"]
    upload_simple(c, h, "lain.txt", b"y")
    resp = c.patch(f"{API}/{fid}/rename", headers=h, json={"name": "baru.md"})
    assert resp.status_code == 200 and resp.json()["name"] == "baru.md" and resp.json()["extension"] == "md"
    conflict = c.patch(f"{API}/{fid}/rename", headers=h, json={"name": "LAIN.txt"})
    assert conflict.status_code == 409 and conflict.json()["code"] == "name_conflict"
    assert c.patch(f"{API}/{fid}/rename", headers=h, json={"name": "../x"}).status_code == 422


def test_move_between_folders(user_client):
    c, h, _ = user_client
    folder = c.post("/api/v1/folders", headers=h, json={"name": "Dokumen"}).json()
    fid = upload_simple(c, h, "a.txt", b"x").json()["id"]
    resp = c.post(f"{API}/{fid}/move", headers=h, json={"folder_id": folder["id"]})
    assert resp.status_code == 200 and resp.json()["folder_id"] == folder["id"]
    assert c.get(API, headers=h).json()["total"] == 0
    assert c.get(API, headers=h, params={"folder_id": folder["id"]}).json()["total"] == 1
    upload_simple(c, h, "a.txt", b"y")  # di root
    conflict = c.post(f"{API}/{fid}/move", headers=h, json={"folder_id": None})
    assert conflict.status_code == 409
    missing = c.post(f"{API}/{fid}/move", headers=h, json={"folder_id": "00000000-0000-0000-0000-000000000000"})
    assert missing.status_code == 404 and missing.json()["code"] == "folder_not_found"


def test_move_into_other_users_folder_denied(user_client):
    c, h, _ = user_client
    create_user_direct(c, "siti")
    hs = auth_header(login(c, "siti"))
    siti_folder = c.post("/api/v1/folders", headers=hs, json={"name": "Siti"}).json()
    fid = upload_simple(c, h, "a.txt", b"x").json()["id"]
    assert c.post(f"{API}/{fid}/move", headers=h, json={"folder_id": siti_folder["id"]}).status_code == 404
    assert upload_simple(c, h, "b.txt", b"x", folder_id=siti_folder["id"]).status_code == 404


def test_copy(user_client):
    c, h, _ = user_client
    data = os.urandom(10_000)
    src = upload_simple(c, h, "asli.bin", data).json()
    copy = c.post(f"{API}/{src['id']}/copy", headers=h, json={})
    assert copy.status_code == 201
    body = copy.json()
    assert body["id"] != src["id"] and body["name"] == "asli (1).bin" and body["sha256"] == src["sha256"]
    assert c.get(f"{API}/{body['id']}/download", headers=h).content == data
    folder = c.post("/api/v1/folders", headers=h, json={"name": "Salinan"}).json()
    named = c.post(f"{API}/{src['id']}/copy", headers=h, json={"folder_id": folder["id"], "name": "cadangan.bin"})
    assert named.status_code == 201 and named.json()["folder_id"] == folder["id"]
    dup = c.post(f"{API}/{src['id']}/copy", headers=h, json={"folder_id": folder["id"], "name": "cadangan.bin"})
    assert dup.status_code == 409
    # Hapus salinan tidak memengaruhi file asli.
    c.delete(f"{API}/{body['id']}", headers=h)
    assert c.get(f"{API}/{src['id']}/download", headers=h).content == data


def test_soft_delete_keeps_physical_file(user_client):
    c, h, _ = user_client
    fid = upload_simple(c, h, "hapus.txt", b"masih ada").json()["id"]
    resp = c.delete(f"{API}/{fid}", headers=h)
    assert resp.status_code == 200
    assert c.get(f"{API}/{fid}", headers=h).status_code == 404
    assert c.get(API, headers=h).json()["total"] == 0
    with c.app.state.database.session() as db:
        row = db.get(StoredFile, fid)
        trash = db.scalar(select(TrashItem).where(TrashItem.file_id == fid))
    assert row.is_deleted and trash is not None and trash.original_name == "hapus.txt"
    assert (storage_root(c) / row.storage_path).read_bytes() == b"masih ada"
    # Nama yang sudah di trash boleh dipakai lagi.
    assert upload_simple(c, h, "hapus.txt", b"baru").json()["name"] == "hapus.txt"


# ---------------- Daftar, sort, pagination, search ----------------
def test_list_sort_search_pagination(user_client):
    c, h, _ = user_client
    upload_simple(c, h, "apel.txt", b"1")
    upload_simple(c, h, "Belimbing.txt", b"333")
    upload_simple(c, h, "ceri.jpg", JPEG_HEAD + b"22")
    folder = c.post("/api/v1/folders", headers=h, json={"name": "Sub"}).json()
    upload_simple(c, h, "apel-merah.txt", b"4444", folder_id=folder["id"])

    by_name = c.get(API, headers=h, params={"sort": "name", "order": "asc"}).json()
    assert [f["name"] for f in by_name["items"]] == ["apel.txt", "Belimbing.txt", "ceri.jpg"]
    by_size = c.get(API, headers=h, params={"sort": "size", "order": "desc"}).json()
    assert [f["size_bytes"] for f in by_size["items"]] == [len(JPEG_HEAD) + 2, 3, 1]
    page = c.get(API, headers=h, params={"sort": "name", "order": "asc", "page": 2, "page_size": 2}).json()
    assert page["total"] == 3 and [f["name"] for f in page["items"]] == ["ceri.jpg"]
    search = c.get(API, headers=h, params={"q": "APEL"}).json()
    assert sorted(f["name"] for f in search["items"]) == ["apel-merah.txt", "apel.txt"]
    in_folder = c.get(API, headers=h, params={"q": "apel", "folder_id": folder["id"]}).json()
    assert [f["name"] for f in in_folder["items"]] == ["apel-merah.txt"]
    images = c.get(API, headers=h, params={"media_type": "image"}).json()
    assert [f["name"] for f in images["items"]] == ["ceri.jpg"]
    assert c.get(API, headers=h, params={"sort": "hacker"}).status_code == 422


def test_search_wildcards_are_escaped(user_client):
    c, h, _ = user_client
    upload_simple(c, h, "diskon_100%.txt", b"x")
    upload_simple(c, h, "biasa.txt", b"x")
    assert [f["name"] for f in c.get(API, headers=h, params={"q": "%"}).json()["items"]] == ["diskon_100%.txt"]
    assert [f["name"] for f in c.get(API, headers=h, params={"q": "_"}).json()["items"]] == ["diskon_100%.txt"]


# ---------------- Kapasitas ----------------
def test_quota_exceeded(user_client):
    c, h, uid = user_client
    with c.app.state.database.session() as db:
        db.execute(update(User).where(User.id == uid).values(storage_quota_bytes=100))
        db.commit()
    assert upload_simple(c, h, "a.bin", b"x" * 60).status_code == 201
    resp = upload_simple(c, h, "b.bin", b"x" * 60)
    assert resp.status_code == 507 and resp.json()["code"] == "quota_exceeded"


def test_disk_full(user_client, monkeypatch):
    c, h, _ = user_client
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(file_service, "disk_usage", lambda _p: Usage(1000, 990, 10))
    resp = upload_simple(c, h, "a.bin", b"x" * 100)
    assert resp.status_code == 507 and resp.json()["code"] == "insufficient_storage"


def test_ssd_unplugged_while_running(user_client):
    c, h, _ = user_client
    fid = upload_simple(c, h, "a.txt", b"x").json()["id"]
    c.app.state.settings.REQUIRE_MOUNTED_STORAGE = True  # direktori tes bukan mount point
    resp = upload_simple(c, h, "b.txt", b"x")
    assert resp.status_code == 503 and resp.json()["code"] == "primary_storage_unavailable"
    assert c.get(f"{API}/{fid}/download", headers=h).status_code == 503
