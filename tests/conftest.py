from __future__ import annotations

import warnings
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

warnings.filterwarnings("ignore", message=".*httpx2.*")

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.core.security import SecurityService  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Role, User  # noqa: E402

TEST_SECRET = "test-secret-key-yang-panjang-dan-acak-0123456789"
DEFAULT_PASSWORD = "Rahasia123"


def make_settings(base: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "SECRET_KEY": TEST_SECRET,
        "ENVIRONMENT": "test",
        "LOG_LEVEL": "WARNING",
        "REQUIRE_MOUNTED_STORAGE": False,
        "PRIMARY_MOUNT_POINT": base / "ssd",
        "PRIMARY_STORAGE_PATH": base / "ssd" / "storage",
        "DATABASE_PATH": base / "ssd" / "database" / "server.db",
        "BACKUP_MOUNT_POINT": base / "hdd",
        "BACKUP_STORAGE_PATH": base / "hdd" / "backup",
        # Argon2 dibuat ringan supaya tes cepat; produksi memakai default.
        "ARGON2_TIME_COST": 1,
        "ARGON2_MEMORY_COST_KIB": 8192,
        "UPLOAD_CLEANUP_INTERVAL_SECONDS": 0,
        "MIN_FREE_SPACE_BYTES": 0,
        "UPLOAD_CHUNK_SIZE_MIN": 1024,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def make_client(tmp_path: Path) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _factory(raise_server_exceptions: bool = True, **overrides: Any) -> TestClient:
        settings = make_settings(tmp_path, **overrides)
        client = TestClient(create_app(settings), raise_server_exceptions=raise_server_exceptions)
        client.__enter__()
        clients.append(client)
        return client

    yield _factory
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client(ALLOW_REGISTRATION=True)


def create_user_direct(
    client: TestClient, username: str, password: str = DEFAULT_PASSWORD, role: str = Role.USER.value,
    is_active: bool = True,
) -> int:
    app = client.app
    security: SecurityService = app.state.security  # type: ignore[attr-defined]
    with app.state.database.session() as db:  # type: ignore[attr-defined]
        user = User(username=username, password_hash=security.hash_password(password), role=role,
                    is_active=is_active)
        db.add(user)
        db.commit()
        return user.id


def login(client: TestClient, username: str, password: str = DEFAULT_PASSWORD, device: str | None = None) -> dict:
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password, "device_name": device}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def auth_header(tokens: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"


@pytest.fixture
def user_client(client: TestClient) -> tuple[TestClient, dict[str, str], int]:
    uid = create_user_direct(client, "budi")
    return client, auth_header(login(client, "budi")), uid


def upload_simple(
    client: TestClient, headers: dict[str, str], name: str, data: bytes, folder_id: str | None = None,
    extra_headers: dict[str, str] | None = None,
):
    params = {"filename": name}
    if folder_id:
        params["folder_id"] = folder_id
    return client.post(
        "/api/v1/files", params=params, content=data,
        headers={**headers, "Content-Type": "application/octet-stream", **(extra_headers or {})},
    )


def storage_root(client: TestClient) -> Path:
    return client.app.state.settings.PRIMARY_STORAGE_PATH  # type: ignore[attr-defined]
