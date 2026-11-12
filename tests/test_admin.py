from __future__ import annotations

from app.models import Folder
from tests.conftest import auth_header, create_user_direct, login

API = "/api/v1/admin/users"


def _admin(client):
    uid = create_user_direct(client, "admin", role="admin")
    return uid, auth_header(login(client, "admin"))


def test_admin_endpoints_require_admin(client):
    create_user_direct(client, "budi")
    h = auth_header(login(client, "budi"))
    resp = client.get(API, headers=h)
    assert resp.status_code == 403
    assert resp.json()["code"] == "admin_required"
    assert client.get(API).status_code == 401


def test_admin_create_list_get_user(client):
    _, h = _admin(client)
    resp = client.post(API, headers=h, json={"username": "siti", "password": "Rahasia123", "role": "user"})
    assert resp.status_code == 201, resp.text
    uid = resp.json()["id"]
    login(client, "siti")
    listing = client.get(API, headers=h, params={"search": "sit"}).json()
    assert listing["total"] == 1 and listing["items"][0]["username"] == "siti"
    assert client.get(f"{API}/{uid}", headers=h).json()["username"] == "siti"
    assert client.get(f"{API}/9999", headers=h).status_code == 404


def test_admin_pagination(client):
    _, h = _admin(client)
    for i in range(5):
        create_user_direct(client, f"user{i}")
    page = client.get(API, headers=h, params={"page": 2, "page_size": 2}).json()
    assert page["total"] == 6
    assert len(page["items"]) == 2
    assert client.get(API, headers=h, params={"page_size": 1000}).status_code == 422


def test_admin_promote_revokes_old_tokens(client):
    _, h = _admin(client)
    uid = create_user_direct(client, "budi")
    budi = login(client, "budi")
    resp = client.patch(f"{API}/{uid}", headers=h, json={"role": "admin"})
    assert resp.status_code == 200 and resp.json()["role"] == "admin"
    # Token lama membawa role lama, jadi harus dicabut.
    assert client.get("/api/v1/auth/me", headers=auth_header(budi)).status_code == 401


def test_admin_deactivate_user(client):
    _, h = _admin(client)
    uid = create_user_direct(client, "budi")
    budi = login(client, "budi")
    assert client.patch(f"{API}/{uid}", headers=h, json={"is_active": False}).status_code == 200
    assert client.get("/api/v1/auth/me", headers=auth_header(budi)).status_code == 401
    assert client.post("/api/v1/auth/login", json={"username": "budi", "password": "Rahasia123"}).status_code == 401


def test_admin_reset_password(client):
    _, h = _admin(client)
    uid = create_user_direct(client, "budi")
    assert client.patch(f"{API}/{uid}", headers=h, json={"password": "PasswordBaru456"}).status_code == 200
    login(client, "budi", "PasswordBaru456")


def test_admin_cannot_demote_or_delete_self(client):
    uid, h = _admin(client)
    resp = client.patch(f"{API}/{uid}", headers=h, json={"role": "user"})
    assert resp.status_code == 409 and resp.json()["code"] == "cannot_demote_self"
    resp = client.delete(f"{API}/{uid}", headers=h)
    assert resp.status_code == 409 and resp.json()["code"] == "cannot_delete_self"


def test_admin_delete_user(client):
    _, h = _admin(client)
    uid = create_user_direct(client, "budi")
    assert client.delete(f"{API}/{uid}", headers=h).status_code == 200
    assert client.get(f"{API}/{uid}", headers=h).status_code == 404


def test_admin_delete_user_with_data_refused(client):
    _, h = _admin(client)
    uid = create_user_direct(client, "budi")
    with client.app.state.database.session() as db:
        db.add(Folder(owner_id=uid, name="Foto"))
        db.commit()
    resp = client.delete(f"{API}/{uid}", headers=h)
    assert resp.status_code == 409 and resp.json()["code"] == "user_has_data"


def test_last_admin_protected(client):
    uid, h = _admin(client)
    other = create_user_direct(client, "admin2", role="admin")
    h2 = auth_header(login(client, "admin2"))
    # admin2 menonaktifkan admin pertama -> boleh (masih ada admin2).
    assert client.patch(f"{API}/{uid}", headers=h2, json={"is_active": False}).status_code == 200
    # Kini admin2 admin aktif terakhir; tidak boleh menurunkan diri (dicegah juga oleh aturan self).
    resp = client.patch(f"{API}/{other}", headers=h2, json={"role": "user"})
    assert resp.status_code == 409
