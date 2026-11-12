"""Dokumentasi tidak boleh melenceng dari kode (dijalankan bersama pytest)."""

from __future__ import annotations

import json
import re
import subprocess
import sys

from app.core.config import PROJECT_ROOT, Settings
from tests.conftest import auth_header, create_user_direct, login, upload_simple

API_DOC = (PROJECT_ROOT / "docs" / "API.md").read_text(encoding="utf-8")


def _spec(client) -> dict:
    return client.get("/openapi.json").json()


def test_every_endpoint_is_documented_and_vice_versa(client):
    actual = {(m.upper(), p) for p, ops in _spec(client)["paths"].items() for m in ops}
    summary = API_DOC.split("## 11. Ringkasan semua endpoint")[1].split("## 12.")[0]
    documented = set(re.findall(r"\| (GET|POST|PUT|PATCH|DELETE) \| `(/api/v1/[^`]+)`", summary))
    assert actual - documented == set(), f"endpoint belum didokumentasikan: {sorted(actual - documented)}"
    assert documented - actual == set(), f"endpoint di dokumen tidak ada di kode: {sorted(documented - actual)}"


def test_every_error_code_is_documented():
    source = "\n".join(p.read_text() for p in (PROJECT_ROOT / "app").rglob("*.py"))
    codes = set(re.findall(r"AppError\(\s*\d{3},\s*\"([a-z_]+)\"", source))
    codes |= set(re.findall(r"(?:unauthorized|forbidden|not_found|conflict|bad_request|unprocessable|service_unavailable)\(\s*\"([a-z_]+)\"", source))
    codes |= set(re.findall(r"TokenError\(\"([a-z_]+)\"\)", source))
    codes |= set(re.findall(r"\"code\": \"([a-z_]+)\"", source))
    codes |= set(re.findall(r"_error_body\(\"([a-z_]+)\"", source))
    codes |= {"not_authenticated", "refresh_token_reused", "too_many_attempts", "http_error"} - {"http_error"}
    table = API_DOC.split("## 10. Daftar semua kode error")[1].split("## 11.")[0]
    missing = sorted(c for c in codes if f"`{c}`" not in table)
    assert not missing, f"kode error belum ada di tabel docs/API.md bagian 10: {missing}"


def test_operation_ids_unique_and_stable(client):
    ids = [op["operationId"] for ops in _spec(client)["paths"].values() for op in ops.values()]
    assert len(ids) == len(set(ids)), "operationId duplikat membingungkan generator client"
    assert "upload_chunk" in ids and "upload_chunk_post" in ids


def test_streaming_uploads_declare_binary_body(client):
    """Tanpa requestBody, generator client membuat fungsi upload tanpa parameter data."""
    paths = _spec(client)["paths"]
    for path, method in (("/api/v1/files", "post"), ("/api/v1/uploads/{upload_id}/chunks/{chunk_index}", "put"),
                         ("/api/v1/uploads/{upload_id}/chunks/{chunk_index}", "post")):
        content = paths[path][method]["requestBody"]["content"]
        assert content["application/octet-stream"]["schema"] == {"type": "string", "format": "binary"}, path


def test_openapi_json_is_up_to_date():
    result = subprocess.run(
        [sys.executable, "-W", "ignore", str(PROJECT_ROOT / "scripts" / "export_openapi.py"), "--check"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr


def test_documented_defaults_match_settings():
    defaults = Settings.model_fields
    expectations = {
        "ACCESS_TOKEN_EXPIRE_MINUTES": ("15 menit", 15),
        "REFRESH_TOKEN_EXPIRE_DAYS": ("30 hari", 30),
        "MAX_REQUEST_BODY_BYTES": ("1 MiB", 1024**2),
        "MAX_SIMPLE_UPLOAD_BYTES": ("100 MiB", 100 * 1024**2),
        "MAX_UPLOAD_SIZE_BYTES": ("64 GiB", 64 * 1024**3),
        "UPLOAD_CHUNK_SIZE_DEFAULT": ("default 8 MiB", 8 * 1024**2),
        "UPLOAD_CHUNK_SIZE_MIN": ("256 KiB", 256 * 1024),
        "UPLOAD_CHUNK_SIZE_MAX": ("64 MiB", 64 * 1024**2),
        "UPLOAD_EXPIRE_HOURS": ("24 jam", 24),
        "MAX_ACTIVE_UPLOADS_PER_USER": ("| 10 |", 10),
        "TRASH_RETENTION_DAYS": ("30 hari", 30),
        "THUMBNAIL_SIZE": ("320 px", 320),
        "LOGIN_MAX_ATTEMPTS": ("5 per username", 5),
    }
    limits = API_DOC.split("## 12. Batas default")[1]
    for key, (text, value) in expectations.items():
        assert defaults[key].default == value, f"default {key} berubah: perbarui docs/API.md"
        assert text in limits or text in API_DOC, f"{key}: '{text}' tidak ada di docs/API.md"


def _example_keys(marker: str) -> set[str]:
    block = API_DOC.split(marker)[1].split("```json")[1].split("```")[0]
    return set(re.findall(r'"([a-z_0-9]+)":', block))


def test_documented_response_shapes_match_real_responses(user_client):
    c, h, _ = user_client
    tokens = c.post("/api/v1/auth/login", json={"username": "budi", "password": "Rahasia123"}).json()
    assert set(tokens) == _example_keys("### Login\n")
    assert tokens["expires_in"] == 900 and tokens["refresh_expires_in"] == 30 * 86400

    file = upload_simple(c, h, "liburan.jpg", b"\xff\xd8\xff\xe0" + b"x" * 50).json()
    assert set(file) == _example_keys("### Objek file")

    up = c.post("/api/v1/uploads/init", headers=h, json={"filename": "v.bin", "total_size": 2048, "chunk_size": 1024}).json()
    assert set(up) == _example_keys("### 1) Mulai")
    chunk = c.put(f"/api/v1/uploads/{up['id']}/chunks/0", headers=h, content=b"a" * 1024).json()
    assert set(chunk) == _example_keys("### 2) Kirim chunk")

    storage = c.get("/api/v1/storage/status", headers=h).json()
    assert set(storage) | set(storage["primary"]) | set(storage["usage"]) == _example_keys("## 7. Penyimpanan & kuota")

    err = c.get("/api/v1/files/00000000-0000-0000-0000-000000000000", headers=h).json()
    assert err == {"detail": "File tidak ditemukan", "code": "file_not_found"}  # contoh persis di bagian 1


def test_documented_admin_only_endpoints_reject_users(client):
    """Kolom 'Akses' di tabel ringkasan: endpoint 'admin' menolak pengguna biasa."""
    create_user_direct(client, "budi")
    h = auth_header(login(client, "budi"))
    summary = API_DOC.split("## 11. Ringkasan semua endpoint")[1].split("## 12.")[0]
    rows = re.findall(r"\| (GET|POST|PUT|PATCH|DELETE) \| `(/api/v1/[^`]+)` \| (\w+)", summary)
    checked = 0
    for method, path, access in rows:
        if path.startswith("/api/v1/auth/"):
            continue  # logout/ganti password akan mencabut sesi uji ini
        concrete = re.sub(r"\{[a-z_]+\}", "1", path)
        resp = client.request(method, concrete, headers=h, json={} if method in ("POST", "PATCH") else None)
        if access == "admin":
            assert resp.status_code == 403, f"{method} {path} seharusnya khusus admin ({resp.status_code})"
            checked += 1
        else:
            assert resp.status_code != 403 or resp.json().get("code") == "registration_disabled", f"{method} {path}"
    assert checked >= 12


def test_api_doc_json_examples_are_valid_json():
    for block in re.findall(r"```json\n(.*?)```", API_DOC, re.S):
        cleaned = block.replace("…", "x").replace("[...]", "[]").replace("{...status \"completed\"...}", "{}")
        try:
            json.loads(cleaned)
        except ValueError as exc:  # contoh JSON di dokumen harus bisa di-parse developer
            raise AssertionError(f"contoh JSON tidak valid di docs/API.md:\n{block}\n{exc}") from exc
