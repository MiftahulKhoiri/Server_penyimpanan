"""Tes helper root pemasangan SSD/HDD (scripts/storage_helper.py) dengan sistem tiruan.

Uji mount sungguhan (loop device, root) ada di tests/test_storage_helper_real.py.
"""

from __future__ import annotations

import copy
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import PROJECT_ROOT

_spec = importlib.util.spec_from_file_location("storage_helper", PROJECT_ROOT / "scripts" / "storage_helper.py")
helper_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["storage_helper"] = helper_mod
_spec.loader.exec_module(helper_mod)  # type: ignore[union-attr]
HelperError = helper_mod.HelperError

SSD_UUID = "aaaaaaaa-0000-0000-0000-000000000001"
HDD_UUID = "bbbbbbbb-0000-0000-0000-000000000002"
HDD2_UUID = "cccccccc-0000-0000-0000-000000000003"

LSBLK = {
    "blockdevices": [
        {"name": "mmcblk0", "path": "/dev/mmcblk0", "type": "disk", "size": "31914983424", "rm": "0",
         "children": [
             {"name": "mmcblk0p1", "path": "/dev/mmcblk0p1", "type": "part", "size": "536870912",
              "fstype": "vfat", "uuid": "AAAA-BBBB", "mountpoint": "/boot/firmware"},
             {"name": "mmcblk0p2", "path": "/dev/mmcblk0p2", "type": "part", "size": "31000000000",
              "fstype": "ext4", "uuid": "11111111-2222-3333-4444-555555555555", "mountpoint": "/"},
         ]},
        {"name": "sda", "path": "/dev/sda", "type": "disk", "size": 960000000000, "model": "Samsung SSD 870 ",
         "tran": "usb", "rm": False,
         "children": [{"name": "sda1", "path": "/dev/sda1", "type": "part", "size": 960000000000,
                       "fstype": "ext4", "uuid": SSD_UUID, "label": "ssd", "mountpoint": "/mnt/ssd"}]},
        {"name": "sdb", "path": "/dev/sdb", "type": "disk", "size": 2000000000000, "model": "WD Elements",
         "tran": "usb", "rm": False,
         "children": [{"name": "sdb1", "path": "/dev/sdb1", "type": "part", "size": 2000000000000,
                       "fstype": "ext4", "uuid": HDD_UUID, "label": "backup", "mountpoint": None}]},
        {"name": "sdc", "path": "/dev/sdc", "type": "disk", "size": 64000000000, "model": "Flashdisk",
         "tran": "usb", "rm": True,
         "children": [{"name": "sdc1", "path": "/dev/sdc1", "type": "part", "size": 64000000000,
                       "fstype": "exfat", "uuid": "1234-ABCD", "mountpoint": None}]},
        {"name": "sdd", "path": "/dev/sdd", "type": "disk", "size": 500000000000, "model": "Disk Baru",
         "tran": "usb", "rm": False, "fstype": None, "uuid": None, "mountpoint": None},
        {"name": "sde", "path": "/dev/sde", "type": "disk", "size": 1000000000000, "model": "HDD Kedua",
         "tran": "usb", "rm": False,
         "children": [{"name": "sde1", "path": "/dev/sde1", "type": "part", "size": 1000000000000,
                       "fstype": "xfs", "uuid": HDD2_UUID, "mountpoint": None}]},
        {"name": "zram0", "path": "/dev/zram0", "type": "disk", "size": 0},
    ]
}

FSTAB = (
    "proc            /proc           proc    defaults          0       0\n"
    "PARTUUID=abc-01  /boot/firmware  vfat    defaults          0       2\n"
    "PARTUUID=abc-02  /               ext4    defaults,noatime  0       1\n"
    f"UUID={SSD_UUID} /mnt/ssd ext4 defaults,noatime,nofail,x-systemd.device-timeout=30s 0 2\n"
)

ENV = (
    "# konfigurasi server\n"
    "SECRET_KEY=rahasia\n"
    "PORT=8000\n"
    "PRIMARY_MOUNT_POINT=/mnt/ssd\n"
    "PRIMARY_STORAGE_PATH=/mnt/ssd/storage\n"
    "DATABASE_PATH=/mnt/ssd/database/server.db\n"
    "BACKUP_MOUNT_POINT=/mnt/hdd\n"
    "BACKUP_STORAGE_PATH=/mnt/hdd/backup\n"
)


class FakeSystem(helper_mod.System):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[list[str]] = []
        self.blk = copy.deepcopy(LSBLK)
        self.table: dict[str, tuple[str, str]] = {"/": ("/dev/mmcblk0p2", "ext4"), "/mnt/ssd": ("/dev/sda1", "ext4")}
        self.fail_mount: set[str] = set()  # mount point yang gagal di-mount
        self.busy: set[str] = set()  # mount point yang tidak bisa dilepas
        self.start_rc = 0
        self.healthy = True
        self.chowned: list[Path] = []
        self.disks = root.parent / "isi-disk"  # isi tiap disk saat tidak terpasang
        self.path("/mnt/ssd").mkdir(parents=True)

    def _move_children(self, source: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            for child in list(source.iterdir()):
                child.rename(target / child.name)

    def _node(self, key: str, value: str) -> dict | None:
        stack = list(self.blk["blockdevices"])
        while stack:
            node = stack.pop()
            if node.get(key) == value:
                return node
            stack.extend(node.get("children") or [])
        return None

    def run(self, args, timeout=120):  # type: ignore[no-untyped-def]
        self.calls.append(list(args))
        ok = subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "mount":
            mount_point = args[-1]
            node = self._node("uuid", args[-2].removeprefix("UUID="))
            if node is None or mount_point in self.fail_mount:
                return subprocess.CompletedProcess(args, 32, "", "mount: wrong fs type, bad option")
            self.table[mount_point] = (node["path"], args[2])
            node["mountpoint"] = mount_point
            self._move_children(self.disks / node["name"], self.path(mount_point))  # isi disk muncul
            return ok
        if args[0] == "umount":
            mount_point = args[-1]
            if mount_point in self.busy:
                return subprocess.CompletedProcess(args, 32, "", f"umount: {mount_point}: target is busy.")
            source = self.table.pop(mount_point, None)
            if source:
                node = self._node("path", source[0])
                if node:
                    node["mountpoint"] = None
                    self._move_children(self.path(mount_point), self.disks / node["name"])  # isi ikut disk
            return ok
        if args[:2] == ["systemctl", "start"]:
            return subprocess.CompletedProcess(args, self.start_rc, "", "" if self.start_rc == 0 else "Job failed")
        return ok  # systemctl lain, findmnt --verify

    def path(self, value: str) -> Path:
        return self.root / value.lstrip("/")

    def mounts(self):  # type: ignore[no-untyped-def]
        return dict(self.table)

    def lsblk(self) -> dict:
        return copy.deepcopy(self.blk)

    def chown_service(self, path: Path, user: str) -> None:
        self.chowned.append(path)

    def disk_usage(self, path: Path):  # type: ignore[no-untyped-def]
        return (1000, 400)

    def health(self, host: str, port: str):  # type: ignore[no-untyped-def]
        return 200 if self.healthy else None

    def sleep(self, seconds: float) -> None:
        pass

    def ran(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "server.env").write_text(ENV)
    (etc / "fstab").write_text(FSTAB)
    unit = (PROJECT_ROOT / "deploy" / "server-penyimpanan.service").read_text()
    unit = unit.replace("@PRIMARY_MOUNT@", "/mnt/ssd").replace("@BACKUP_MOUNT@", "/mnt/hdd")
    (etc / "server-penyimpanan.service").write_text(unit)
    return {
        "SP_CONFIG_FILE": str(etc / "server.env"),
        "SP_UNIT_FILE": str(etc / "server-penyimpanan.service"),
        "SP_FSTAB": str(etc / "fstab"),
        "SP_STATE_DIR": str(tmp_path / "state"),
        "SP_HEALTH_WAIT": "4",
    }


@pytest.fixture
def system(tmp_path: Path) -> FakeSystem:
    return FakeSystem(tmp_path / "root")


_helpers: list = []


def make(system: FakeSystem, env: dict[str, str]):  # type: ignore[no-untyped-def]
    """Helper baru = proses helper baru di produksi: kunci helper sebelumnya dilepas."""
    while _helpers:
        _helpers.pop().release()
    helper = helper_mod.StorageHelper(system, env)
    _helpers.append(helper)
    return helper


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    while _helpers:
        _helpers.pop().release()


def fstab(env: dict[str, str]) -> str:
    return Path(env["SP_FSTAB"]).read_text()


def config(env: dict[str, str]) -> dict[str, str]:
    return helper_mod.parse_env(Path(env["SP_CONFIG_FILE"]).read_text())


# ---------------------------------------------------------------- fungsi murni
def test_parse_lsblk_marks_reasons_and_roles():
    managed = helper_mod.parse_managed(FSTAB)
    parts = {p["name"]: p for p in helper_mod.parse_lsblk(LSBLK, {"ssd": "/mnt/ssd", "hdd": "/mnt/hdd"}, managed)}
    assert "zram0" not in parts and "mmcblk0" not in parts and "sdb" not in parts  # disk berpartisi: hanya partisinya
    assert parts["mmcblk0p1"]["reason"] == "system_disk" and parts["mmcblk0p2"]["system"] is True
    assert parts["sda1"]["usable"] is True and parts["sda1"]["role"] == "ssd"
    assert parts["sda1"]["model"] == "Samsung SSD 870" and parts["sda1"]["size_bytes"] == 960000000000
    assert parts["sdb1"]["usable"] is True and parts["sdb1"]["role"] is None
    assert parts["sdc1"]["reason"] == "unsupported_filesystem" and parts["sdc1"]["removable"] is True
    assert parts["sdd"]["reason"] == "no_filesystem"  # disk tanpa partisi & tanpa filesystem
    assert parts["sde1"]["usable"] is True


def test_parse_lsblk_mounted_elsewhere():
    data = copy.deepcopy(LSBLK)
    data["blockdevices"][2]["children"][0]["mountpoint"] = "/media/pi/backup"
    parts = {p["name"]: p for p in helper_mod.parse_lsblk(data, {"ssd": "/mnt/ssd", "hdd": "/mnt/hdd"}, {})}
    assert parts["sdb1"]["reason"] == "mounted_elsewhere" and not parts["sdb1"]["usable"]


def test_set_env_keys_preserves_other_lines():
    text = "# komentar\nSECRET_KEY=x\nPRIMARY_MOUNT_POINT=/mnt/ssd\n"
    new = helper_mod.set_env_keys(text, {"PRIMARY_MOUNT_POINT": "/mnt/data", "PRIMARY_STORAGE_PATH": "/mnt/data/storage"})
    assert new.startswith("# komentar\nSECRET_KEY=x\nPRIMARY_MOUNT_POINT=/mnt/data\n")
    assert new.rstrip().endswith("PRIMARY_STORAGE_PATH=/mnt/data/storage")
    assert helper_mod.parse_env(new)["PRIMARY_STORAGE_PATH"] == "/mnt/data/storage"


def test_reroot():
    assert helper_mod.reroot("/mnt/ssd/database/server.db", "/mnt/ssd", "/mnt/data") == "/mnt/data/database/server.db"
    assert helper_mod.reroot("/mnt/ssd", "/mnt/ssd", "/mnt/data") == "/mnt/data"
    with pytest.raises(HelperError) as exc:
        helper_mod.reroot("/srv/lain", "/mnt/ssd", "/mnt/data")
    assert exc.value.code == "config_inconsistent"


@pytest.mark.parametrize("value", ["/etc/x", "/mnt", "/mnt/", "/mnt/a/b", "/mnt/../etc", "relatif", "/mnt/-x", "/mnt/a b", 5])
def test_invalid_mount_points(value):
    with pytest.raises(HelperError) as exc:
        helper_mod.validate_mount_point(value)
    assert exc.value.code == "invalid_mount_point"


@pytest.mark.parametrize("value", ["/mnt/data", "/media/backup_2", "/srv/hdd.1"])
def test_valid_mount_points(value):
    assert helper_mod.validate_mount_point(value) == value


def test_rewrite_fstab_manages_block_and_disables_manual_lines():
    new = helper_mod.rewrite_fstab(FSTAB, "ssd", (SSD_UUID, "/mnt/data", "ext4"), {"/mnt/ssd", "/mnt/data"}, {f"UUID={SSD_UUID}"})
    lines = new.splitlines()
    assert helper_mod.DISABLED_PREFIX + f"UUID={SSD_UUID} /mnt/ssd" in new
    assert lines[-2].startswith("# server-penyimpanan:ssd")
    assert lines[-1] == f"UUID={SSD_UUID} /mnt/data ext4 {helper_mod.FSTAB_OPTIONS} 0 2"
    assert "PARTUUID=abc-02  /               ext4" in new  # baris sistem utuh
    assert helper_mod.parse_managed(new) == {"ssd": {"uuid": SSD_UUID, "mount_point": "/mnt/data", "fstype": "ext4"}}
    # Tambahkan HDD lalu ubah SSD lagi: blok HDD tidak tersentuh, tidak ada duplikat.
    new = helper_mod.rewrite_fstab(new, "hdd", (HDD_UUID, "/mnt/hdd", "ext4"), {"/mnt/hdd"}, {f"UUID={HDD_UUID}"})
    new = helper_mod.rewrite_fstab(new, "ssd", (SSD_UUID, "/mnt/ssd2", "ext4"), {"/mnt/data", "/mnt/ssd2"}, set())
    managed = helper_mod.parse_managed(new)
    assert managed["hdd"]["mount_point"] == "/mnt/hdd" and managed["ssd"]["mount_point"] == "/mnt/ssd2"
    assert new.count("# server-penyimpanan:ssd") == 1 and new.count("# server-penyimpanan:hdd") == 1
    # Hapus blok HDD.
    new = helper_mod.rewrite_fstab(new, "hdd", None, {"/mnt/hdd"}, set())
    assert "hdd" not in helper_mod.parse_managed(new)


def test_rewrite_unit_updates_read_write_paths():
    unit = (PROJECT_ROOT / "deploy" / "server-penyimpanan.service").read_text()
    new = helper_mod.rewrite_unit(unit, "/mnt/data", "/media/backup")
    assert "ReadWritePaths=-/mnt/data -/media/backup -/run/server-penyimpanan-storage" in new
    assert new.count("ReadWritePaths=") == 1
    old_style = "ProtectSystem=strict\nProtectHome=yes\n"
    assert "ReadWritePaths=-/a" in helper_mod.rewrite_unit(old_style, "/a", "/b")


# ---------------------------------------------------------------- status
def test_status_reports_roles_and_partitions(system, env):
    result = make(system, env).status()
    roles = {r["role"]: r for r in result["roles"]}
    assert roles["ssd"]["mounted"] and roles["ssd"]["device"] == "/dev/sda1" and roles["ssd"]["uuid"] == SSD_UUID
    assert roles["ssd"]["paths"] == {"PRIMARY_STORAGE_PATH": "/mnt/ssd/storage", "DATABASE_PATH": "/mnt/ssd/database/server.db"}
    assert roles["ssd"]["managed"] is False  # baris fstab manual, belum dikelola aplikasi
    assert roles["hdd"]["mounted"] is False and roles["hdd"]["total_bytes"] is None
    assert any(p["uuid"] == HDD_UUID for p in result["partitions"])
    assert result["job"] is None and result["version"] == helper_mod.PROTOCOL_VERSION


# ---------------------------------------------------------------- mount / unmount HDD
def test_mount_hdd(system, env):
    result, cont = make(system, env).op_mount("hdd", HDD_UUID)
    assert cont is None
    job = result["job"]
    assert job["status"] == "succeeded" and job["op"] == "mount"
    assert system.ran("mount") == [["mount", "-t", "ext4", "-o", "noatime,nosuid,nodev,noexec", f"UUID={HDD_UUID}", "/mnt/hdd"]]
    assert helper_mod.parse_managed(fstab(env))["hdd"] == {"uuid": HDD_UUID, "mount_point": "/mnt/hdd", "fstype": "ext4"}
    backup_dir = system.path("/mnt/hdd/backup")
    assert backup_dir.is_dir() and backup_dir in system.chowned and oct(backup_dir.stat().st_mode & 0o777) == "0o750"
    assert make(system, env).read_job()["status"] == "succeeded"
    # Memasang ulang disk yang sama: idempoten, tanpa mount kedua.
    make(system, env).op_mount("hdd", HDD_UUID)
    assert len(system.ran("mount")) == 1


def test_mount_ssd_refused(system, env):
    with pytest.raises(HelperError) as exc:
        make(system, env).op_mount("ssd", HDD_UUID)
    assert exc.value.code == "ssd_via_ssh"


@pytest.mark.parametrize(("uuid", "code"), [
    ("11111111-2222-3333-4444-555555555555", "system_disk"),
    ("1234-ABCD", "unsupported_filesystem"),
    ("dddddddd-0000-0000-0000-00000000dead", "partition_not_found"),
    (SSD_UUID, "used_by_other_role"),
    ("UUID=$(reboot)", "invalid_uuid"),
])
def test_mount_rejections(system, env, uuid, code):
    with pytest.raises(HelperError) as exc:
        make(system, env).op_mount("hdd", uuid)
    assert exc.value.code == code
    assert system.ran("mount") == []
    assert fstab(env) == FSTAB


def test_mount_point_busy_with_other_disk(system, env):
    make(system, env).op_mount("hdd", HDD_UUID)
    with pytest.raises(HelperError) as exc:
        make(system, env).op_mount("hdd", HDD2_UUID)
    assert exc.value.code == "mount_point_busy"


def test_mount_point_with_files_on_sd_card_refused(system, env):
    folder = system.path("/mnt/hdd")
    folder.mkdir(parents=True)
    (folder / "nyasar.txt").write_text("tertulis ke kartu SD")
    with pytest.raises(HelperError) as exc:
        make(system, env).op_mount("hdd", HDD_UUID)
    assert exc.value.code == "mount_point_not_empty"


def test_failed_mount_leaves_fstab_untouched(system, env):
    system.fail_mount.add("/mnt/hdd")
    helper = make(system, env)
    with pytest.raises(HelperError) as exc:
        helper.op_mount("hdd", HDD_UUID)
    assert exc.value.code == "mount_failed"
    assert fstab(env) == FSTAB
    assert helper.read_job()["status"] == "failed" and helper.read_job()["error_code"] == "mount_failed"


def test_unmount_hdd(system, env):
    make(system, env).op_mount("hdd", HDD_UUID)
    result, _ = make(system, env).op_unmount("hdd")
    assert result["job"]["status"] == "succeeded"
    assert ["umount", "/mnt/hdd"] in system.calls and "/mnt/hdd" not in system.table
    assert "hdd" not in helper_mod.parse_managed(fstab(env))


def test_unmount_busy_keeps_fstab(system, env):
    make(system, env).op_mount("hdd", HDD_UUID)
    before = fstab(env)
    system.busy.add("/mnt/hdd")
    with pytest.raises(HelperError) as exc:
        make(system, env).op_unmount("hdd")
    assert exc.value.code == "device_busy"
    assert fstab(env) == before


def test_unmount_ssd_refused(system, env):
    with pytest.raises(HelperError) as exc:
        make(system, env).op_unmount("ssd")
    assert exc.value.code == "ssd_via_ssh"


# ---------------------------------------------------------------- pindah lokasi mount
def test_move_ssd_mount_point(system, env):
    helper = make(system, env)
    result, cont = helper.op_set_mount_point("ssd", "/mnt/data")
    assert result["job"]["status"] == "running" and cont is not None
    cont()
    job = helper.read_job()
    assert job["status"] == "succeeded", job
    order = [c[:2] for c in system.calls if c[0] in ("systemctl", "mount", "umount")]
    assert order == [
        ["systemctl", "stop"], ["umount", "/mnt/ssd"], ["mount", "-t"], ["systemctl", "daemon-reload"],
        ["systemctl", "start"],
    ]
    assert system.table["/mnt/data"] == ("/dev/sda1", "ext4") and "/mnt/ssd" not in system.table
    cfg = config(env)
    assert cfg["PRIMARY_MOUNT_POINT"] == "/mnt/data"
    assert cfg["PRIMARY_STORAGE_PATH"] == "/mnt/data/storage"
    assert cfg["DATABASE_PATH"] == "/mnt/data/database/server.db"
    assert cfg["SECRET_KEY"] == "rahasia" and cfg["BACKUP_MOUNT_POINT"] == "/mnt/hdd"
    assert "ReadWritePaths=-/mnt/data -/mnt/hdd -/run/server-penyimpanan-storage" in Path(env["SP_UNIT_FILE"]).read_text()
    text = fstab(env)
    assert helper_mod.parse_managed(text)["ssd"]["mount_point"] == "/mnt/data"
    assert helper_mod.DISABLED_PREFIX + f"UUID={SSD_UUID} /mnt/ssd" in text
    assert system.path("/mnt/data/storage").is_dir() and system.path("/mnt/data/database").is_dir()
    assert not system.path("/mnt/ssd").exists()  # folder mount lama yang kosong dibersihkan


def test_move_rolls_back_when_server_fails_to_start(system, env):
    before = {k: Path(env[k]).read_text() for k in ("SP_CONFIG_FILE", "SP_FSTAB", "SP_UNIT_FILE")}
    system.start_rc = 1
    helper = make(system, env)
    _, cont = helper.op_set_mount_point("ssd", "/mnt/data")
    system.calls.clear()
    cont()
    job = helper.read_job()
    assert job["status"] == "failed" and job["error_code"] == "service_start_failed"
    assert "dipulihkan" in job["message"]
    for key, text in before.items():
        assert Path(env[key]).read_text() == text, key
    assert system.table["/mnt/ssd"] == ("/dev/sda1", "ext4") and "/mnt/data" not in system.table
    assert ["umount", "/mnt/data"] in system.calls
    assert system.ran("systemctl", "start")  # dicoba jalankan lagi dengan pengaturan lama
    assert not system.path("/mnt/data").exists()


def test_move_rolls_back_when_server_unhealthy(system, env):
    system.healthy = False
    helper = make(system, env)
    _, cont = helper.op_set_mount_point("ssd", "/mnt/data")
    cont()
    assert helper.read_job()["error_code"] == "service_unhealthy"
    assert config(env)["PRIMARY_MOUNT_POINT"] == "/mnt/ssd"
    assert "/mnt/ssd" in system.table


def test_move_unmounted_hdd_only_changes_config(system, env):
    helper = make(system, env)
    _, cont = helper.op_set_mount_point("hdd", "/media/backup")
    cont()
    assert helper.read_job()["status"] == "succeeded"
    assert system.ran("mount") == [] and system.ran("umount") == []
    assert config(env)["BACKUP_STORAGE_PATH"] == "/media/backup/backup"
    assert fstab(env) == FSTAB


def test_move_mounted_hdd_keeps_managed_entry(system, env):
    make(system, env).op_mount("hdd", HDD_UUID)
    helper = make(system, env)
    _, cont = helper.op_set_mount_point("hdd", "/mnt/backup")
    cont()
    assert helper.read_job()["status"] == "succeeded"
    managed = helper_mod.parse_managed(fstab(env))
    assert managed["hdd"] == {"uuid": HDD_UUID, "mount_point": "/mnt/backup", "fstype": "ext4"}
    assert system.table["/mnt/backup"] == ("/dev/sdb1", "ext4")


@pytest.mark.parametrize(("role", "target", "code"), [
    ("ssd", "/mnt/ssd", "no_change"),
    ("ssd", "/mnt/hdd", "mount_point_conflict"),
    ("ssd", "/etc", "invalid_mount_point"),
    ("ssd", "/", "invalid_mount_point"),
    ("hdd", "/mnt/ssd", "mount_point_conflict"),
    ("disk", "/mnt/x", "invalid_role"),
])
def test_move_validation(system, env, role, target, code):
    with pytest.raises(HelperError) as exc:
        make(system, env).op_set_mount_point(role, target)
    assert exc.value.code == code


def test_move_to_non_empty_folder_refused(system, env):
    folder = system.path("/mnt/data")
    folder.mkdir(parents=True)
    (folder / "isi").write_text("x")
    with pytest.raises(HelperError) as exc:
        make(system, env).op_set_mount_point("ssd", "/mnt/data")
    assert exc.value.code == "mount_point_not_empty"


def test_move_unmounted_ssd_refused(system, env):
    del system.table["/mnt/ssd"]
    with pytest.raises(HelperError) as exc:
        make(system, env).op_set_mount_point("ssd", "/mnt/data")
    assert exc.value.code == "not_mounted"


# ---------------------------------------------------------------- penguncian & protokol
def test_concurrent_operation_is_rejected(system, env):
    state = Path(env["SP_STATE_DIR"])
    state.mkdir()
    fd = os.open(state / "lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(HelperError) as exc:
            make(system, env).op_mount("hdd", HDD_UUID)
        assert exc.value.code == "busy"
    finally:
        os.close(fd)


def test_respond_protocol(system, env):
    helper = make(system, env)
    assert helper_mod.respond(helper, b"bukan json")[0]["code"] == "bad_request"
    assert helper_mod.respond(helper, b"[1]")[0]["code"] == "bad_request"
    assert helper_mod.respond(helper, b"x" * (helper_mod.MAX_REQUEST_BYTES + 1))[0]["code"] == "request_too_large"
    assert helper_mod.respond(helper, json.dumps({"op": "hapus"}).encode())[0]["code"] == "unknown_op"
    ping, cont = helper_mod.respond(helper, b'{"op": "ping"}')
    assert ping == {"ok": True, "result": {"version": helper_mod.PROTOCOL_VERSION}} and cont is None
    response, _ = helper_mod.respond(helper, json.dumps({"op": "mount", "role": "hdd", "uuid": "1234-ABCD"}).encode())
    assert response == {"ok": False, "code": "unsupported_filesystem", "message": helper_mod.REASON_TEXT["unsupported_filesystem"]}


def test_backups_are_pruned(system, env):
    helper = make(system, env)
    for _ in range(helper_mod.BACKUPS_KEEP + 3):
        helper.op_mount("hdd", HDD_UUID)
        helper.release()
    backups = sorted((Path(env["SP_STATE_DIR"]) / "backups").iterdir())
    assert len(backups) == helper_mod.BACKUPS_KEEP
    assert (backups[-1] / "fstab").exists() and (backups[-1] / "server.env").exists()


# ---------------------------------------------------------------- koneksi ala systemd (Accept=yes)
_CHILD = """
import importlib.util, os, sys, time
os.environ["LISTEN_PID"] = str(os.getpid())
spec = importlib.util.spec_from_file_location("h", {path!r})
h = importlib.util.module_from_spec(spec)
sys.modules["h"] = h
spec.loader.exec_module(h)
sys.stdout.write("jawaban\\n")
sys.stdout.flush()
{close}
time.sleep(5)  # pekerjaan lanjutan (mis. menghentikan server) masih berjalan
"""


def _spawn_like_systemd(conn_fd: int, close_code: str) -> subprocess.Popen:
    """systemd memberi koneksi sebagai stdin, stdout, DAN fd 3 (LISTEN_FDS=1)."""
    code = _CHILD.format(path=str(PROJECT_ROOT / "scripts" / "storage_helper.py"), close=close_code)
    return subprocess.Popen(
        ["bash", "-c", 'exec 3<&0; exec "$0" -c "$1"', sys.executable, code],
        stdin=conn_fd, stdout=conn_fd, stderr=subprocess.DEVNULL, env={**os.environ, "LISTEN_FDS": "1"},
    )


def _read_until_eof(sock, timeout: float) -> bytes:  # type: ignore[no-untyped-def]
    sock.settimeout(timeout)
    data = b""
    while chunk := sock.recv(4096):
        data += chunk
    return data


def test_server_gets_answer_before_continuation_runs():
    """Regresi: dulu fd 3 tetap terbuka, server menunggu EOF sementara helper menghentikan server
    (saling tunggu sampai TimeoutStopSec, lalu server dibunuh paksa)."""
    import socket as socket_mod

    server_end, helper_end = socket_mod.socketpair()
    proc = _spawn_like_systemd(helper_end.fileno(), "h.close_connection()")
    helper_end.close()
    try:
        assert _read_until_eof(server_end, timeout=3) == b"jawaban\n"
        assert proc.poll() is None  # EOF diterima saat helper MASIH bekerja
    finally:
        proc.kill()
        proc.wait()
        server_end.close()


def test_closing_only_stdin_stdout_keeps_server_waiting():
    """Pembanding: cara lama (hanya stdin/stdout) membuat server tidak pernah menerima EOF."""
    import socket as socket_mod

    old_way = "d = os.open(os.devnull, os.O_RDWR); os.dup2(d, 0); os.dup2(d, 1)"
    server_end, helper_end = socket_mod.socketpair()
    proc = _spawn_like_systemd(helper_end.fileno(), old_way)
    helper_end.close()
    try:
        with pytest.raises(TimeoutError):
            _read_until_eof(server_end, timeout=1.5)
    finally:
        proc.kill()
        proc.wait()
        server_end.close()
