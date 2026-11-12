from __future__ import annotations

from datetime import timedelta

import jwt
from sqlalchemy import select, update

from app.core.security import SecurityService
from app.database.types import utcnow
from app.models import ActivityLog, RefreshToken, User
from tests.conftest import DEFAULT_PASSWORD, TEST_SECRET, auth_header, create_user_direct, login

API = "/api/v1/auth"


# ---------------- Registrasi ----------------
def test_register_disabled_by_default(make_client):
    c = make_client()
    resp = c.post(f"{API}/register", json={"username": "budi", "password": DEFAULT_PASSWORD})
    assert resp.status_code == 403
    assert resp.json()["code"] == "registration_disabled"


def test_register_success_and_password_hashed(client):
    resp = client.post(
        f"{API}/register", json={"username": "Budi", "password": DEFAULT_PASSWORD, "email": "Budi@Example.com"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["username"] == "budi"
    assert body["email"] == "budi@example.com"
    assert body["role"] == "user"
    assert "password" not in body and "password_hash" not in body
    with client.app.state.database.session() as db:
        user = db.scalar(select(User).where(User.username == "budi"))
        assert user.password_hash.startswith("$argon2id$")
        assert DEFAULT_PASSWORD not in user.password_hash


def test_register_duplicate_username(client):
    client.post(f"{API}/register", json={"username": "budi", "password": DEFAULT_PASSWORD})
    resp = client.post(f"{API}/register", json={"username": "BUDI", "password": DEFAULT_PASSWORD})
    assert resp.status_code == 409
    assert resp.json()["code"] == "username_taken"


def test_register_validation_does_not_echo_password(client):
    resp = client.post(f"{API}/register", json={"username": "../etc", "password": "pendek"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"
    assert "pendek" not in resp.text
    assert "../etc" not in resp.text


def test_register_rejects_password_equal_username(client):
    resp = client.post(f"{API}/register", json={"username": "budiman123", "password": "budiman123"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "weak_password"


# ---------------- Login ----------------
def test_login_success(client):
    create_user_direct(client, "budi")
    tokens = login(client, "budi", device="Pixel 7")
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] == 15 * 60
    me = client.get(f"{API}/me", headers=auth_header(tokens))
    assert me.status_code == 200
    assert me.json()["username"] == "budi"
    assert me.json()["last_login_at"] is not None


def test_login_wrong_password_and_unknown_user_same_response(client):
    create_user_direct(client, "budi")
    r1 = client.post(f"{API}/login", json={"username": "budi", "password": "salah-salah"})
    r2 = client.post(f"{API}/login", json={"username": "tidakada", "password": "salah-salah"})
    assert r1.status_code == r2.status_code == 401
    assert r1.json() == r2.json() == {"detail": "Username atau password salah", "code": "invalid_credentials"}


def test_login_inactive_user_rejected(client):
    create_user_direct(client, "budi", is_active=False)
    resp = client.post(f"{API}/login", json={"username": "budi", "password": DEFAULT_PASSWORD})
    assert resp.status_code == 401


def test_login_rate_limited(make_client):
    c = make_client(LOGIN_MAX_ATTEMPTS=3)
    create_user_direct(c, "budi")
    for _ in range(3):
        assert c.post(f"{API}/login", json={"username": "budi", "password": "salah-salah"}).status_code == 401
    # Bahkan password benar ditolak selama terkunci.
    resp = c.post(f"{API}/login", json={"username": "budi", "password": DEFAULT_PASSWORD})
    assert resp.status_code == 429
    assert resp.json()["code"] == "too_many_attempts"
    assert int(resp.headers["Retry-After"]) > 0


def test_login_logs_activity_without_secrets(client):
    create_user_direct(client, "budi")
    login(client, "budi")
    client.post(f"{API}/login", json={"username": "budi", "password": "PasswordSalah99"})
    with client.app.state.database.session() as db:
        logs = db.scalars(select(ActivityLog)).all()
    actions = [log.action for log in logs]
    assert "auth.login" in actions and "auth.login_failed" in actions
    dump = " ".join(f"{log.details} {log.target_id}" for log in logs)
    assert DEFAULT_PASSWORD not in dump and "PasswordSalah99" not in dump


# ---------------- Access token ----------------
def test_me_requires_token(client):
    resp = client.get(f"{API}/me")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_expired_access_token(client):
    uid = create_user_direct(client, "budi")
    tokens = login(client, "budi")
    security: SecurityService = client.app.state.security
    expired = security.create_access_token(
        user_id=uid, role="user", session_id=tokens["session_id"],
        now=utcnow() - timedelta(hours=1), expires_delta=timedelta(minutes=5),
    )
    resp = client.get(f"{API}/me", headers={"Authorization": f"Bearer {expired.token}"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "token_expired"


def test_tampered_and_wrong_key_tokens_rejected(client):
    uid = create_user_direct(client, "budi")
    tokens = login(client, "budi")
    bad_sig = tokens["access_token"][:-4] + ("AAAA" if not tokens["access_token"].endswith("AAAA") else "BBBB")
    assert client.get(f"{API}/me", headers={"Authorization": f"Bearer {bad_sig}"}).status_code == 401
    now = int(utcnow().timestamp())
    forged = jwt.encode(
        {"sub": str(uid), "sid": tokens["session_id"], "role": "admin", "type": "access",
         "iss": "Server_penyimpanan", "iat": now, "exp": now + 600},
        "kunci-lain-" + TEST_SECRET, algorithm="HS256",
    )
    assert client.get(f"{API}/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    none_alg = jwt.encode(
        {"sub": str(uid), "sid": tokens["session_id"], "type": "access", "iss": "Server_penyimpanan",
         "iat": now, "exp": now + 600}, None, algorithm="none",
    )
    assert client.get(f"{API}/me", headers={"Authorization": f"Bearer {none_alg}"}).status_code == 401


def test_deactivated_user_token_rejected(client):
    uid = create_user_direct(client, "budi")
    tokens = login(client, "budi")
    with client.app.state.database.session() as db:
        db.execute(update(User).where(User.id == uid).values(is_active=False))
        db.commit()
    assert client.get(f"{API}/me", headers=auth_header(tokens)).status_code == 401


# ---------------- Refresh token ----------------
def test_refresh_rotates_tokens(client):
    create_user_direct(client, "budi")
    tokens = login(client, "budi")
    resp = client.post(f"{API}/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 200
    new = resp.json()
    assert new["refresh_token"] != tokens["refresh_token"]
    assert new["session_id"] == tokens["session_id"]
    assert client.get(f"{API}/me", headers=auth_header(new)).status_code == 200


def test_refresh_reuse_revokes_whole_session(client):
    create_user_direct(client, "budi")
    tokens = login(client, "budi")
    new = client.post(f"{API}/refresh", json={"refresh_token": tokens["refresh_token"]}).json()
    reuse = client.post(f"{API}/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert reuse.status_code == 401
    assert reuse.json()["code"] == "refresh_token_reused"
    # Token baru hasil rotasi ikut dicabut.
    assert client.post(f"{API}/refresh", json={"refresh_token": new["refresh_token"]}).status_code == 401
    assert client.get(f"{API}/me", headers=auth_header(new)).status_code == 401


def test_refresh_expired(client):
    create_user_direct(client, "budi")
    tokens = login(client, "budi")
    with client.app.state.database.session() as db:
        db.execute(update(RefreshToken).values(expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    resp = client.post(f"{API}/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 401
    assert resp.json()["code"] == "refresh_token_expired"


def test_refresh_invalid_token(client):
    resp = client.post(f"{API}/refresh", json={"refresh_token": "x" * 64})
    assert resp.status_code == 401
    assert resp.json()["code"] == "refresh_token_invalid"


def test_refresh_tokens_stored_hashed(client):
    create_user_direct(client, "budi")
    tokens = login(client, "budi")
    with client.app.state.database.session() as db:
        hashes = db.scalars(select(RefreshToken.token_hash)).all()
    assert tokens["refresh_token"] not in hashes
    assert all(len(h) == 64 for h in hashes)


# ---------------- Logout & sesi ----------------
def test_logout_revokes_access_and_refresh(client):
    create_user_direct(client, "budi")
    tokens = login(client, "budi")
    assert client.post(f"{API}/logout", headers=auth_header(tokens)).status_code == 200
    assert client.get(f"{API}/me", headers=auth_header(tokens)).status_code == 401
    assert client.post(f"{API}/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401


def test_sessions_list_and_revoke_other_device(client):
    create_user_direct(client, "budi")
    phone = login(client, "budi", device="HP")
    tablet = login(client, "budi", device="Tablet")
    sessions = client.get(f"{API}/sessions", headers=auth_header(phone)).json()
    assert {s["device_name"] for s in sessions} == {"HP", "Tablet"}
    assert [s["current"] for s in sessions if s["device_name"] == "HP"] == [True]

    resp = client.delete(f"{API}/sessions/{tablet['session_id']}", headers=auth_header(phone))
    assert resp.status_code == 200
    assert client.get(f"{API}/me", headers=auth_header(tablet)).status_code == 401
    assert client.get(f"{API}/me", headers=auth_header(phone)).status_code == 200


def test_cannot_revoke_other_users_session(client):
    create_user_direct(client, "budi")
    create_user_direct(client, "siti")
    budi = login(client, "budi")
    siti = login(client, "siti")
    resp = client.delete(f"{API}/sessions/{siti['session_id']}", headers=auth_header(budi))
    assert resp.status_code == 404
    assert client.get(f"{API}/me", headers=auth_header(siti)).status_code == 200


def test_logout_all(client):
    create_user_direct(client, "budi")
    a = login(client, "budi", device="A")
    b = login(client, "budi", device="B")
    assert client.post(f"{API}/logout-all", headers=auth_header(a)).status_code == 200
    assert client.get(f"{API}/me", headers=auth_header(a)).status_code == 401
    assert client.get(f"{API}/me", headers=auth_header(b)).status_code == 401


# ---------------- Ganti password ----------------
def test_change_password_wrong_current(client):
    create_user_direct(client, "budi")
    tokens = login(client, "budi")
    resp = client.post(
        f"{API}/change-password", headers=auth_header(tokens),
        json={"current_password": "salah-salah", "new_password": "PasswordBaru456"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "wrong_password"


def test_change_password_success_revokes_other_sessions(client):
    create_user_direct(client, "budi")
    phone = login(client, "budi", device="HP")
    laptop = login(client, "budi", device="Laptop")
    resp = client.post(
        f"{API}/change-password", headers=auth_header(phone),
        json={"current_password": DEFAULT_PASSWORD, "new_password": "PasswordBaru456"},
    )
    assert resp.status_code == 200
    fresh = resp.json()
    assert client.get(f"{API}/me", headers=auth_header(fresh)).status_code == 200
    assert client.get(f"{API}/me", headers=auth_header(phone)).status_code == 401
    assert client.get(f"{API}/me", headers=auth_header(laptop)).status_code == 401
    assert client.post(f"{API}/login", json={"username": "budi", "password": DEFAULT_PASSWORD}).status_code == 401
    login(client, "budi", "PasswordBaru456")
    sessions = client.get(f"{API}/sessions", headers=auth_header(fresh)).json()
    assert any(s["device_name"] == "HP" and s["current"] for s in sessions)
