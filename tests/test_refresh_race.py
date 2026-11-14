"""Rotasi refresh token di bawah request bersamaan."""

from __future__ import annotations

import threading

from sqlalchemy import func, select

from app.core.errors import AppError
from app.models import RefreshToken
from app.services import auth_service
from app.utils.request_info import ClientInfo
from tests.conftest import create_user_direct, login

CLIENT = ClientInfo(ip_address="127.0.0.1", user_agent="pytest")


def _rotate_parallel(app, raw: str, n: int) -> tuple[list, list]:
    barrier = threading.Barrier(n)
    ok: list = []
    errors: list = []

    def worker() -> None:
        db = app.state.database.session()
        try:
            barrier.wait()
            tokens = auth_service.rotate_refresh_token(db, app.state.settings, app.state.security, raw, CLIENT)
            db.commit()
            ok.append(tokens)
        except AppError as exc:
            db.rollback()
            errors.append(exc.code)
        except Exception as exc:  # noqa: BLE001 - harus tidak terjadi
            db.rollback()
            errors.append(f"unexpected:{exc.__class__.__name__}")
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return ok, errors


def test_concurrent_refresh_only_one_wins(client):
    create_user_direct(client, "budi")
    raw = login(client, "budi")["refresh_token"]
    for _ in range(5):  # ulangi beberapa kali agar balapan benar-benar terjadi
        ok, errors = _rotate_parallel(client.app, raw, 4)
        assert len(ok) <= 1, "refresh token yang sama tidak boleh menghasilkan dua token baru"
        assert all(not e.startswith("unexpected") for e in errors), errors
        if ok:
            raw = ok[0].refresh_token
        else:
            break
    with client.app.state.database.session() as db:
        # Tiap token lama paling banyak punya SATU penerus.
        children = db.execute(
            select(RefreshToken.replaced_by_id, func.count()).where(RefreshToken.replaced_by_id.is_not(None))
            .group_by(RefreshToken.replaced_by_id)
        ).all()
        assert all(count == 1 for _, count in children)


def test_concurrent_refresh_via_api(client):
    create_user_direct(client, "budi")
    raw = login(client, "budi")["refresh_token"]
    results: list[int] = []
    barrier = threading.Barrier(3)

    def call() -> None:
        barrier.wait()
        results.append(client.post("/api/v1/auth/refresh", json={"refresh_token": raw}).status_code)

    threads = [threading.Thread(target=call) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(200) <= 1
    assert all(code in (200, 401, 503) for code in results), results
    assert 500 not in results


def test_reused_after_rotation_revokes_session(client):
    create_user_direct(client, "budi")
    first = login(client, "budi")
    second = client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}).json()
    reuse = client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert reuse.status_code == 401 and reuse.json()["code"] == "refresh_token_reused"
    # Token penerus juga ikut dicabut (seluruh family).
    after = client.post("/api/v1/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert after.status_code == 401


def test_expired_refresh_token(client):
    from datetime import timedelta

    from app.database.types import utcnow

    create_user_direct(client, "budi")
    raw = login(client, "budi")["refresh_token"]
    with client.app.state.database.session() as db:
        db.execute(RefreshToken.__table__.update().values(expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": raw})
    assert resp.status_code == 401 and resp.json()["code"] == "refresh_token_expired"
