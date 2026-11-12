from __future__ import annotations

from sqlalchemy import func, select

from app.models import Folder, StoredFile, TrashItem
from app.services.folder_service import MAX_FOLDER_DEPTH
from tests.conftest import auth_header, create_user_direct, login, storage_root, upload_simple

API = "/api/v1/folders"


def _mk(c, h, name, parent=None):
    resp = c.post(API, headers=h, json={"name": name, "parent_id": parent})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_list_and_detail(user_client):
    c, h, _ = user_client
    foto = _mk(c, h, "Foto")
    _mk(c, h, "Dokumen")
    liburan = _mk(c, h, "Liburan 2026", foto["id"])
    upload_simple(c, h, "pantai.txt", b"12345", folder_id=liburan["id"])

    root = c.get(API, headers=h).json()
    assert [f["name"] for f in root] == ["Dokumen", "Foto"]
    sub = c.get(API, headers=h, params={"parent_id": foto["id"]}).json()
    assert [f["name"] for f in sub] == ["Liburan 2026"]

    detail = c.get(f"{API}/{liburan['id']}", headers=h).json()
    assert [p["name"] for p in detail["path"]] == ["Foto", "Liburan 2026"]
    assert detail["file_count"] == 1 and detail["total_size_bytes"] == 5
    assert detail["parent_id"] == foto["id"]


def test_name_conflicts(user_client):
    c, h, _ = user_client
    _mk(c, h, "Foto")
    assert c.post(API, headers=h, json={"name": "foto"}).status_code == 409
    upload_simple(c, h, "laporan", b"x")
    assert c.post(API, headers=h, json={"name": "Laporan"}).status_code == 409
    assert c.post(API, headers=h, json={"name": "../x"}).status_code == 422
    assert c.post(API, headers=h, json={"name": "   "}).status_code == 422


def test_rename_folder(user_client):
    c, h, _ = user_client
    a = _mk(c, h, "A")
    _mk(c, h, "B")
    resp = c.patch(f"{API}/{a['id']}/rename", headers=h, json={"name": "Arsip"})
    assert resp.status_code == 200 and resp.json()["name"] == "Arsip"
    assert c.patch(f"{API}/{a['id']}/rename", headers=h, json={"name": "b"}).status_code == 409


def test_move_folder_and_cycle_prevention(user_client):
    c, h, _ = user_client
    a = _mk(c, h, "A")
    b = _mk(c, h, "B", a["id"])
    cc = _mk(c, h, "C", b["id"])
    other = _mk(c, h, "Lain")
    assert c.post(f"{API}/{a['id']}/move", headers=h, json={"parent_id": cc["id"]}).json()["code"] == "folder_cycle"
    assert c.post(f"{API}/{a['id']}/move", headers=h, json={"parent_id": a["id"]}).status_code == 409
    moved = c.post(f"{API}/{b['id']}/move", headers=h, json={"parent_id": other["id"]})
    assert moved.status_code == 200 and moved.json()["parent_id"] == other["id"]
    to_root = c.post(f"{API}/{cc['id']}/move", headers=h, json={"parent_id": None})
    assert to_root.status_code == 200 and to_root.json()["parent_id"] is None


def test_depth_limit(user_client):
    c, h, _ = user_client
    parent = None
    for i in range(MAX_FOLDER_DEPTH):
        parent = _mk(c, h, f"L{i}", parent)["id"]
    resp = c.post(API, headers=h, json={"name": "terlalu-dalam", "parent_id": parent})
    assert resp.status_code == 422 and resp.json()["code"] == "folder_too_deep"


def test_delete_folder_recursive_to_trash(user_client):
    c, h, _ = user_client
    a = _mk(c, h, "A")
    b = _mk(c, h, "B", a["id"])
    f1 = upload_simple(c, h, "satu.txt", b"1", folder_id=a["id"]).json()
    f2 = upload_simple(c, h, "dua.txt", b"22", folder_id=b["id"]).json()
    keep = upload_simple(c, h, "root.txt", b"3").json()

    assert c.delete(f"{API}/{a['id']}", headers=h).status_code == 200
    assert c.get(f"{API}/{a['id']}", headers=h).status_code == 404
    assert c.get(f"{API}/{b['id']}", headers=h).status_code == 404
    assert c.get(f"/api/v1/files/{f2['id']}", headers=h).status_code == 404
    assert c.get("/api/v1/files", headers=h, params={"folder_id": b["id"]}).status_code == 404
    search = c.get("/api/v1/files", headers=h, params={"q": "txt"}).json()
    assert [f["name"] for f in search["items"]] == ["root.txt"]
    assert c.get(f"/api/v1/files/{keep['id']}", headers=h).status_code == 200

    with c.app.state.database.session() as db:
        items = db.scalars(select(TrashItem)).all()
        assert len(items) == 1 and items[0].folder_id == a["id"] and items[0].size_bytes == 3
        deleted_at = {
            db.get(Folder, a["id"]).deleted_at, db.get(Folder, b["id"]).deleted_at,
            db.get(StoredFile, f1["id"]).deleted_at, db.get(StoredFile, f2["id"]).deleted_at,
        }
        assert len(deleted_at) == 1  # satu kesatuan untuk restore
        paths = [db.get(StoredFile, f["id"]).storage_path for f in (f1, f2)]
    assert all((storage_root(c) / p).exists() for p in paths)
    _mk(c, h, "A")  # nama boleh dipakai lagi


def test_other_users_folders_invisible(user_client):
    c, h, _ = user_client
    mine = _mk(c, h, "Pribadi")
    create_user_direct(c, "siti")
    hs = auth_header(login(c, "siti"))
    assert c.get(f"{API}/{mine['id']}", headers=hs).status_code == 404
    assert c.get(API, headers=hs).json() == []
    assert c.post(API, headers=hs, json={"name": "x", "parent_id": mine["id"]}).status_code == 404
    assert c.delete(f"{API}/{mine['id']}", headers=hs).status_code == 404
    assert c.patch(f"{API}/{mine['id']}/rename", headers=hs, json={"name": "x"}).status_code == 404
    with c.app.state.database.session() as db:
        assert db.scalar(select(func.count(Folder.id))) == 1
