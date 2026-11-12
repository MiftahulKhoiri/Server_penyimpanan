from __future__ import annotations

import io
from datetime import timedelta

from PIL import Image
from sqlalchemy import func, select, update

from app.database.types import utcnow
from app.main import run_maintenance_once
from app.models import ActivityLog, Folder, StoredFile, TrashItem
from tests.conftest import auth_header, create_user_direct, login, storage_root, upload_simple

API = "/api/v1/trash"


def _folder(c, h, name, parent=None):
    return c.post("/api/v1/folders", headers=h, json={"name": name, "parent_id": parent}).json()


def _phys(c, fid):
    with c.app.state.database.session() as db:
        row = db.get(StoredFile, fid)
        return storage_root(c) / row.storage_path, row.thumbnail_path


def test_list_trash(user_client):
    c, h, _ = user_client
    f = upload_simple(c, h, "a.txt", b"123").json()
    d = _folder(c, h, "Map")
    c.delete(f"/api/v1/files/{f['id']}", headers=h)
    c.delete(f"/api/v1/folders/{d['id']}", headers=h)
    body = c.get(API, headers=h).json()
    assert body["total"] == 2 and body["retention_days"] == 30
    assert [(i["type"], i["name"]) for i in body["items"]] == [("folder", "Map"), ("file", "a.txt")]
    item = body["items"][1]
    assert item["id"] == f["id"] and item["size_bytes"] == 3
    assert item["purge_after"] > item["deleted_at"]


def test_restore_file_to_original_folder(user_client):
    c, h, _ = user_client
    d = _folder(c, h, "Dokumen")
    f = upload_simple(c, h, "a.txt", b"x", folder_id=d["id"]).json()
    c.delete(f"/api/v1/files/{f['id']}", headers=h)
    resp = c.post(f"{API}/{f['id']}/restore", headers=h)
    assert resp.status_code == 200
    assert resp.json() == {"type": "file", "id": f["id"], "name": "a.txt", "folder_id": d["id"]}
    assert c.get(f"/api/v1/files/{f['id']}", headers=h).status_code == 200
    assert c.get(API, headers=h).json()["total"] == 0
    assert c.post(f"{API}/{f['id']}/restore", headers=h).status_code == 404


def test_restore_name_conflict_and_missing_folder(user_client):
    c, h, _ = user_client
    d = _folder(c, h, "Lama")
    f = upload_simple(c, h, "laporan.txt", b"lama", folder_id=d["id"]).json()
    c.delete(f"/api/v1/files/{f['id']}", headers=h)
    c.delete(f"/api/v1/folders/{d['id']}", headers=h)  # folder asal ikut ke trash
    upload_simple(c, h, "laporan.txt", b"baru")  # nama sama di root
    body = c.post(f"{API}/{f['id']}/restore", headers=h).json()
    assert body["folder_id"] is None and body["name"] == "laporan (1).txt"


def test_restore_folder_as_unit(user_client):
    c, h, _ = user_client
    a = _folder(c, h, "A")
    b = _folder(c, h, "B", a["id"])
    f1 = upload_simple(c, h, "satu.txt", b"1", folder_id=a["id"]).json()
    f2 = upload_simple(c, h, "dua.txt", b"2", folder_id=b["id"]).json()
    alone = upload_simple(c, h, "sendiri.txt", b"3", folder_id=b["id"]).json()
    c.delete(f"/api/v1/files/{alone['id']}", headers=h)  # dihapus lebih dulu, terpisah
    c.delete(f"/api/v1/folders/{a['id']}", headers=h)
    assert c.post(f"{API}/{a['id']}/restore", headers=h).json()["type"] == "folder"
    assert c.get(f"/api/v1/folders/{b['id']}", headers=h).status_code == 200
    assert c.get(f"/api/v1/files/{f1['id']}", headers=h).status_code == 200
    assert c.get(f"/api/v1/files/{f2['id']}", headers=h).status_code == 200
    # File yang dihapus sendiri-sendiri tetap di trash.
    assert c.get(f"/api/v1/files/{alone['id']}", headers=h).status_code == 404
    assert [i["id"] for i in c.get(API, headers=h).json()["items"]] == [alone["id"]]


def test_purge_file_removes_data_and_thumbnail(user_client):
    c, h, _ = user_client
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (1, 2, 3)).save(buf, "JPEG")
    f = upload_simple(c, h, "foto.jpg", buf.getvalue()).json()
    assert c.get(f"/api/v1/media/{f['id']}/thumbnail", headers=h).status_code == 200
    path, thumb_rel = _phys(c, f["id"])
    thumb = storage_root(c) / thumb_rel
    assert path.exists() and thumb.exists()
    c.delete(f"/api/v1/files/{f['id']}", headers=h)
    assert path.exists()  # soft delete: data masih ada
    resp = c.post(f"{API}/{f['id']}/delete", headers=h)
    assert resp.status_code == 200 and resp.json()["files"] == 1
    assert not path.exists() and not thumb.exists()
    with c.app.state.database.session() as db:
        assert db.get(StoredFile, f["id"]) is None
    assert c.post(f"{API}/{f['id']}/restore", headers=h).status_code == 404
    assert c.get("/api/v1/storage/status", headers=h).json()["usage"]["trash_bytes"] == 0


def test_purge_folder_removes_everything_inside(user_client):
    c, h, _ = user_client
    a = _folder(c, h, "A")
    b = _folder(c, h, "B", a["id"])
    f1 = upload_simple(c, h, "x.txt", b"1", folder_id=a["id"]).json()
    f2 = upload_simple(c, h, "y.txt", b"22", folder_id=b["id"]).json()
    f3 = upload_simple(c, h, "z.txt", b"333", folder_id=b["id"]).json()
    c.delete(f"/api/v1/files/{f3['id']}", headers=h)
    c.delete(f"/api/v1/folders/{a['id']}", headers=h)
    paths = [_phys(c, f["id"])[0] for f in (f1, f2, f3)]
    resp = c.delete(f"{API}/{a['id']}/delete", headers=h)
    assert resp.status_code == 200 and resp.json()["files"] == 3 and resp.json()["bytes"] == 6
    assert not any(p.exists() for p in paths)
    with c.app.state.database.session() as db:
        assert db.scalar(select(func.count(Folder.id))) == 0
        assert db.scalar(select(func.count(StoredFile.id))) == 0
        assert db.scalar(select(func.count(TrashItem.id))) == 0


def test_empty_trash(user_client):
    c, h, _ = user_client
    for i in range(3):
        fid = upload_simple(c, h, f"{i}.txt", b"x").json()["id"]
        c.delete(f"/api/v1/files/{fid}", headers=h)
    keep = upload_simple(c, h, "simpan.txt", b"x").json()["id"]
    assert c.delete(API, headers=h).json() == {"purged_items": 3}
    assert c.get(API, headers=h).json()["total"] == 0
    assert c.get(f"/api/v1/files/{keep}", headers=h).status_code == 200


def test_trash_isolated_between_users(user_client):
    c, h, _ = user_client
    fid = upload_simple(c, h, "a.txt", b"x").json()["id"]
    c.delete(f"/api/v1/files/{fid}", headers=h)
    create_user_direct(c, "siti")
    hs = auth_header(login(c, "siti"))
    assert c.get(API, headers=hs).json()["total"] == 0
    assert c.post(f"{API}/{fid}/restore", headers=hs).status_code == 404
    assert c.post(f"{API}/{fid}/delete", headers=hs).status_code == 404
    assert c.delete(API, headers=hs).json()["purged_items"] == 0
    assert c.get(API, headers=h).json()["total"] == 1


def test_retention_purge_automatic(user_client):
    c, h, _ = user_client
    old = upload_simple(c, h, "lama.txt", b"x").json()["id"]
    new = upload_simple(c, h, "baru.txt", b"y").json()["id"]
    c.delete(f"/api/v1/files/{old}", headers=h)
    c.delete(f"/api/v1/files/{new}", headers=h)
    path = _phys(c, old)[0]
    with c.app.state.database.session() as db:
        db.execute(update(TrashItem).where(TrashItem.file_id == old).values(purge_after=utcnow() - timedelta(seconds=1)))
        db.commit()
    stats = run_maintenance_once(c.app)
    assert stats["trash_purged"] == 1
    assert not path.exists()
    assert [i["id"] for i in c.get(API, headers=h).json()["items"]] == [new]
    with c.app.state.database.session() as db:
        log = db.scalar(select(ActivityLog).where(ActivityLog.action == "trash.purge"))
    assert log.user_id is None and "retention_expired" in log.details


def test_purge_file_created_by_chunked_upload(user_client):
    c, h, _ = user_client
    up = c.post("/api/v1/uploads/init", headers=h, json={"filename": "a.bin", "total_size": 10, "chunk_size": 1024}).json()
    c.put(f"/api/v1/uploads/{up['id']}/chunks/0", headers=h, content=b"0123456789")
    fid = c.post(f"/api/v1/uploads/{up['id']}/complete", headers=h).json()["file"]["id"]
    c.delete(f"/api/v1/files/{fid}", headers=h)
    assert c.post(f"{API}/{fid}/delete", headers=h).status_code == 200
    status = c.get(f"/api/v1/uploads/{up['id']}/status", headers=h).json()
    assert status["status"] == "completed" and status["file_id"] is None  # FK SET NULL
