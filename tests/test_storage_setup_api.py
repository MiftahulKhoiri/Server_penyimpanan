"""Tes API pemasangan SSD/HDD (/api/v1/storage/setup) dengan helper tiruan lewat socket Unix."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import PROJECT_ROOT
from app.models import ActivityLog
from tests.conftest import auth_header, create_user_direct, login

API = "/api/v1/storage/setup"
HDD_UUID = "bbbbbbbb-0000-0000-0000-000000000002"


class FakeHelper:
    """Server socket Unix yang meniru helper root: satu permintaan JSON per koneksi."""

    def __init__(self, path: Path, handler: Callable[[dict], dict]) -> None:
        self.path = path
        self.handler = handler
        self.requests: list[dict] = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(8)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                while chunk := conn.recv(65536):
                    data += chunk
                request = json.loads(data)
                self.requests.append(request)
                conn.sendall(json.dumps(self.handler(request)).encode() + b"\n")

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def sock_dir():  # type: ignore[no-untyped-def]
    # Path socket Unix maksimal ±108 karakter: pakai direktori pendek, bukan tmp_path pytest.
    directory = Path(tempfile.mkdtemp(prefix="sp-"))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def _job(op: str, role: str, status: str = "succeeded", **extra) -> dict:  # type: ignore[no-untyped-def]
    return {
        "id": 7, "op": op, "role": role, "status": status, "detail": extra.pop("detail", {}),
        "steps": ["langkah"], "message": extra.pop("message", "Selesai"), "error_code": None,
        "started_at": "2026-11-19T02:00:00+00:00", "finished_at": None if status == "running" else "2026-11-19T02:00:01+00:00",
    }


def _status(settings, ssd_mp: str | None = None) -> dict:  # type: ignore[no-untyped-def]
    return {
        "version": 1,
        "roles": [
            {"role": "ssd", "mount_point": ssd_mp or str(settings.PRIMARY_MOUNT_POINT),
             "paths": {"PRIMARY_STORAGE_PATH": str(settings.PRIMARY_STORAGE_PATH), "DATABASE_PATH": str(settings.DATABASE_PATH)},
             "mounted": True, "device": "/dev/sda1", "uuid": "aaaa-1", "fstype": "ext4", "label": "ssd",
             "model": "Samsung", "managed": False, "fstab_uuid": None, "total_bytes": 1000, "free_bytes": 600},
            {"role": "hdd", "mount_point": str(settings.BACKUP_MOUNT_POINT),
             "paths": {"BACKUP_STORAGE_PATH": str(settings.BACKUP_STORAGE_PATH)},
             "mounted": False, "device": None, "uuid": None, "fstype": None, "label": None, "model": None,
             "managed": False, "fstab_uuid": None, "total_bytes": None, "free_bytes": None},
        ],
        "partitions": [
            {"path": "/dev/sdb1", "name": "sdb1", "disk": "sdb", "size_bytes": 2000, "fstype": "ext4", "uuid": HDD_UUID,
             "label": "backup", "model": "WD", "transport": "usb", "removable": False, "mountpoint": None,
             "system": False, "usable": True, "reason": None, "reason_text": None, "role": None},
            {"path": "/dev/mmcblk0p2", "name": "mmcblk0p2", "disk": "mmcblk0", "size_bytes": 3000, "fstype": "ext4",
             "uuid": "1111-2222", "label": None, "model": None, "transport": None, "removable": False, "mountpoint": "/",
             "system": True, "usable": False, "reason": "system_disk", "reason_text": "Disk sistem", "role": None},
        ],
        "job": None,
    }


@pytest.fixture
def setup_env(make_client, sock_dir):  # type: ignore[no-untyped-def]
    """(client, header admin, helper tiruan, settings). Handler bisa diganti lewat helper.handler."""
    sock_path = sock_dir / "helper.sock"
    client = make_client(STORAGE_HELPER_SOCKET=sock_path)
    settings = client.app.state.settings  # type: ignore[attr-defined]

    def default(request: dict) -> dict:
        op = request["op"]
        if op == "status":
            return {"ok": True, "result": _status(settings)}
        if op == "job":
            return {"ok": True, "result": {"job": _job("mount", "hdd")}}
        if op == "mount":
            return {"ok": True, "result": {"job": _job("mount", "hdd", message="HDD terpasang di /mnt/hdd",
                                                       detail={"device": "/dev/sdb1"})}}
        if op == "unmount":
            return {"ok": True, "result": {"job": _job("unmount", "hdd", message="HDD dilepas")}}
        if op == "set_mount_point":
            return {"ok": True, "result": {"job": _job("set_mount_point", request["role"], status="running")}}
        return {"ok": False, "code": "unknown_op", "message": "?"}

    helper = FakeHelper(sock_path, default)
    create_user_direct(client, "admin", role="admin")
    yield client, auth_header(login(client, "admin")), helper, settings
    helper.close()


def _actions(client) -> list[ActivityLog]:  # type: ignore[no-untyped-def]
    with client.app.state.database.session() as db:  # type: ignore[attr-defined]
        return list(db.scalars(select(ActivityLog).where(ActivityLog.action.like("storage.%"))).all())


# ---------------------------------------------------------------- status
def test_status_without_helper(make_client, sock_dir):
    client = make_client(STORAGE_HELPER_SOCKET=sock_dir / "tidak-ada.sock")
    create_user_direct(client, "admin", role="admin")
    resp = client.get(API, headers=auth_header(login(client, "admin")))
    assert resp.status_code == 200
    body = resp.json()
    assert body["helper_available"] is False and "update.sh" in body["helper_error"]
    assert body["roles"] == [] and body["partitions"] == []


def test_status_admin_only(setup_env):
    client, _, helper, _ = setup_env
    create_user_direct(client, "budi")
    resp = client.get(API, headers=auth_header(login(client, "budi")))
    assert resp.status_code == 403
    assert client.get(API).status_code == 401
    assert helper.requests == []


def test_status_with_helper(setup_env):
    client, h, helper, _ = setup_env
    resp = client.get(API, headers=h)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["helper_available"] is True and body["restart_pending"] is False
    roles = {r["role"]: r for r in body["roles"]}
    assert roles["ssd"]["can_mount"] is False and roles["ssd"]["can_unmount"] is False
    assert roles["hdd"]["can_mount"] is True and roles["hdd"]["can_unmount"] is False  # belum terpasang
    parts = {p["path"]: p for p in body["partitions"]}
    assert parts["/dev/sdb1"]["usable"] and parts["/dev/mmcblk0p2"]["reason"] == "system_disk"
    assert helper.requests == [{"op": "status"}]


def test_status_reports_restart_pending(setup_env):
    client, h, helper, settings = setup_env
    helper.handler = lambda r: {"ok": True, "result": _status(settings, ssd_mp="/mnt/data")}
    assert client.get(API, headers=h).json()["restart_pending"] is True


def test_status_helper_error_is_reported(setup_env):
    client, h, helper, _ = setup_env
    helper.handler = lambda r: {"ok": False, "code": "lsblk_failed", "message": "Gagal membaca daftar disk"}
    body = client.get(API, headers=h).json()
    assert body["helper_available"] is True and body["helper_error"] == "Gagal membaca daftar disk"


# ---------------------------------------------------------------- aksi
def test_mount_hdd(setup_env):
    client, h, helper, _ = setup_env
    resp = client.post(f"{API}/hdd/mount", headers=h, json={"uuid": HDD_UUID})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["restarting"] is False and body["detail"] == "HDD terpasang di /mnt/hdd"
    assert body["job"]["status"] == "succeeded"
    assert helper.requests[-1] == {"op": "mount", "role": "hdd", "uuid": HDD_UUID}
    logs = _actions(client)
    assert [log.action for log in logs] == ["storage.mount"] and HDD_UUID in (logs[0].details or "")


def test_ssd_mount_and_unmount_refused_without_helper_call(setup_env):
    client, h, helper, _ = setup_env
    for resp in (
        client.post(f"{API}/ssd/mount", headers=h, json={"uuid": HDD_UUID}),
        client.post(f"{API}/ssd/unmount", headers=h),
    ):
        assert resp.status_code == 422 and resp.json()["code"] == "ssd_via_ssh"
    assert helper.requests == []


@pytest.mark.parametrize(("method", "url", "body"), [
    ("post", "/hdd/mount", {"uuid": "UUID=$(reboot)"}),
    ("post", "/hdd/mount", {}),
    ("post", "/disk/mount", {"uuid": HDD_UUID}),
    ("put", "/ssd/mount-point", {"mount_point": "/etc"}),
    ("put", "/ssd/mount-point", {"mount_point": "/mnt/../etc"}),
    ("put", "/hdd/mount-point", {"mount_point": "/mnt/a b"}),
])
def test_invalid_input_rejected_before_helper(setup_env, method, url, body):
    client, h, helper, _ = setup_env
    resp = getattr(client, method)(f"{API}{url}", headers=h, json=body)
    assert resp.status_code == 422
    assert helper.requests == []


@pytest.mark.parametrize(("code", "status"), [
    ("system_disk", 422), ("unsupported_filesystem", 422), ("busy", 409), ("mount_point_busy", 409),
    ("partition_not_found", 404), ("mount_failed", 502), ("fstab_invalid", 502),
])
def test_helper_errors_mapped_to_http(setup_env, code, status):
    client, h, helper, _ = setup_env
    helper.handler = lambda r: {"ok": False, "code": code, "message": f"pesan {code}"}
    resp = client.post(f"{API}/hdd/mount", headers=h, json={"uuid": HDD_UUID})
    assert resp.status_code == status
    assert resp.json() == {"detail": f"pesan {code}", "code": code}
    assert _actions(client) == []  # aksi gagal tidak dicatat sebagai berhasil


def test_unmount_hdd(setup_env):
    client, h, helper, _ = setup_env
    resp = client.post(f"{API}/hdd/unmount", headers=h)
    assert resp.status_code == 200 and resp.json()["detail"] == "HDD dilepas"
    assert helper.requests[-1] == {"op": "unmount", "role": "hdd"}
    assert [log.action for log in _actions(client)] == ["storage.unmount"]


def test_actions_blocked_while_backup_runs(setup_env, monkeypatch):
    client, h, helper, _ = setup_env
    monkeypatch.setattr(type(client.app.state.backup_manager), "running", property(lambda self: True))  # type: ignore[attr-defined]
    for resp in (
        client.post(f"{API}/hdd/mount", headers=h, json={"uuid": HDD_UUID}),
        client.post(f"{API}/hdd/unmount", headers=h),
        client.put(f"{API}/ssd/mount-point", headers=h, json={"mount_point": "/mnt/data"}),
    ):
        assert resp.status_code == 409 and resp.json()["code"] == "backup_running"
    assert helper.requests == []


def test_set_mount_point_logs_before_restart(setup_env):
    client, h, helper, settings = setup_env
    seen_log: list[bool] = []

    def handler(request: dict) -> dict:
        # Saat helper dipanggil, aktivitas sudah tercatat (server bisa langsung dihentikan).
        seen_log.append(any(a.action == "storage.mount_point" for a in _actions(client)))
        return {"ok": True, "result": {"job": _job("set_mount_point", "ssd", status="running")}}

    helper.handler = handler
    resp = client.put(f"{API}/ssd/mount-point", headers=h, json={"mount_point": "/mnt/data"})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["restarting"] is True and body["job"]["status"] == "running"
    assert helper.requests[-1] == {"op": "set_mount_point", "role": "ssd", "mount_point": "/mnt/data"}
    assert seen_log == [True]
    log = _actions(client)[0]
    assert json.loads(log.details or "{}") == {"from": str(settings.PRIMARY_MOUNT_POINT), "to": "/mnt/data"}


def test_job_endpoint(setup_env, make_client, sock_dir):
    client, h, _, _ = setup_env
    resp = client.get(f"{API}/job", headers=h)
    assert resp.status_code == 200 and resp.json()["job"]["id"] == 7


def test_job_endpoint_without_helper(make_client, sock_dir):
    client = make_client(STORAGE_HELPER_SOCKET=sock_dir / "tidak-ada.sock")
    create_user_direct(client, "admin", role="admin")
    resp = client.get(f"{API}/job", headers=auth_header(login(client, "admin")))
    assert resp.status_code == 503 and resp.json()["code"] == "storage_helper_unavailable"


# ---------------------------------------------------------------- helper asli lewat socket
class SpawningHelper:
    """Meniru systemd socket Accept=yes: tiap koneksi menjalankan proses storage_helper.py
    dengan stdin, stdout, dan fd 3 = koneksi itu."""

    def __init__(self, path: Path, env: dict[str, str]) -> None:
        self.env = env
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(4)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                # Seperti systemd: koneksi juga di fd 3 dengan LISTEN_FDS/LISTEN_PID.
                proc = subprocess.Popen(
                    ["bash", "-c", 'exec 3<&0; export LISTEN_FDS=1 LISTEN_PID=$$; exec "$0" -I "$1"',
                     sys.executable, str(PROJECT_ROOT / "scripts" / "storage_helper.py")],
                    stdin=conn.fileno(), stdout=conn.fileno(), stderr=subprocess.DEVNULL, env=self.env,
                )
            proc.wait(timeout=60)

    def close(self) -> None:
        self.sock.close()


@pytest.mark.skipif(shutil.which("lsblk") is None, reason="lsblk tidak ada")
def test_real_helper_process_over_socket(make_client, sock_dir, tmp_path):
    client = make_client(STORAGE_HELPER_SOCKET=sock_dir / "helper.sock")
    settings = client.app.state.settings  # type: ignore[attr-defined]
    (tmp_path / "server.env").write_text(
        f"PRIMARY_MOUNT_POINT={settings.PRIMARY_MOUNT_POINT}\nPRIMARY_STORAGE_PATH={settings.PRIMARY_STORAGE_PATH}\n"
        f"DATABASE_PATH={settings.DATABASE_PATH}\nBACKUP_MOUNT_POINT={settings.BACKUP_MOUNT_POINT}\n"
        f"BACKUP_STORAGE_PATH={settings.BACKUP_STORAGE_PATH}\n"
    )
    (tmp_path / "fstab").write_text("")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "SP_CONFIG_FILE": str(tmp_path / "server.env"),
        "SP_FSTAB": str(tmp_path / "fstab"),
        "SP_STATE_DIR": str(tmp_path / "state"),
    }
    spawner = SpawningHelper(sock_dir / "helper.sock", env)
    try:
        create_user_direct(client, "admin", role="admin")
        h = auth_header(login(client, "admin"))
        body = client.get(API, headers=h).json()
        assert body["helper_available"] is True and body["helper_error"] is None, body
        assert body["restart_pending"] is False
        assert {r["role"] for r in body["roles"]} == {"ssd", "hdd"}
        # Validasi di helper sendiri (bukan hanya di server): UUID yang tidak ada.
        resp = client.post(f"{API}/hdd/mount", headers=h, json={"uuid": "dddddddd-0000-0000-0000-00000000dead"})
        assert resp.status_code == 404 and resp.json()["code"] == "partition_not_found"
        assert client.get(f"{API}/job", headers=h).json() == {"job": None}
    finally:
        spawner.close()
