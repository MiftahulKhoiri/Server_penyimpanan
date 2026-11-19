"""Uji helper pemasangan disk dengan mount SUNGGUHAN (loop device berisi ext4/vfat).

Butuh root dan sengaja opt-in agar tidak pernah berjalan tanpa sengaja:
    sudo SP_REAL_MOUNT_TESTS=1 .venv/bin/python -m pytest tests/test_storage_helper_real.py

fstab, konfigurasi, dan unit yang dipakai adalah file sementara (bukan /etc/fstab).
systemd ditiru (server tidak benar-benar dihentikan); mount/umount/lsblk/findmnt asli.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.test_storage_helper import ENV, helper_mod

pytestmark = pytest.mark.skipif(
    os.environ.get("SP_REAL_MOUNT_TESTS") != "1" or os.geteuid() != 0
    or not all(shutil.which(t) for t in ("losetup", "mkfs.ext4", "mkfs.vfat", "findmnt", "lsblk")),
    reason="uji mount sungguhan: butuh root + SP_REAL_MOUNT_TESTS=1 + losetup/mkfs",
)


def sh(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


class RealSystemFakeSystemd(helper_mod.System):
    """Semua perintah asli, kecuali systemctl (dicatat saja) dan health (selalu sehat)."""

    def __init__(self) -> None:
        self.systemctl_calls: list[list[str]] = []

    def run(self, args, timeout=120):  # type: ignore[no-untyped-def]
        if args[0] == "systemctl":
            self.systemctl_calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "", "")
        return super().run(args, timeout)

    def chown_service(self, path: Path, user: str) -> None:
        os.chown(path, 65534, 65534)  # nobody: cukup untuk memastikan chown berjalan

    def health(self, host: str, port: str):  # type: ignore[no-untyped-def]
        return 200

    def sleep(self, seconds: float) -> None:
        pass


@pytest.fixture
def lab(tmp_path: Path):  # type: ignore[no-untyped-def]
    tag = uuid.uuid4().hex[:6]
    loops: list[str] = []
    mounts: list[str] = []

    def disk(name: str, mkfs: list[str], size: str = "64M") -> str:
        image = tmp_path / f"{name}.img"
        sh("truncate", "-s", size, str(image))
        dev = sh("losetup", "-f", "--show", str(image))
        loops.append(dev)
        sh(*mkfs, dev)
        return dev

    ssd = disk("ssd", ["mkfs.ext4", "-q", "-L", "it-ssd"])
    hdd = disk("hdd", ["mkfs.ext4", "-q", "-L", "it-hdd"])
    fat = disk("fat", ["mkfs.vfat", "-n", "ITFAT"])
    ssd_mp, hdd_mp = f"/mnt/sp-it-ssd-{tag}", f"/mnt/sp-it-hdd-{tag}"
    Path(ssd_mp).mkdir()
    sh("mount", ssd, ssd_mp)
    mounts.append(ssd_mp)
    Path(ssd_mp, "storage").mkdir()
    Path(ssd_mp, "storage", "foto.jpg").write_text("data asli di SSD")

    etc = tmp_path / "etc"
    etc.mkdir()
    env_text = ENV.replace("/mnt/ssd", ssd_mp).replace("/mnt/hdd", hdd_mp)
    (etc / "server.env").write_text(env_text)
    ssd_uuid = sh("blkid", "-s", "UUID", "-o", "value", ssd)
    (etc / "fstab").write_text(f"UUID={ssd_uuid} {ssd_mp} ext4 defaults,noatime,nofail 0 2\n")
    (etc / "unit.service").write_text("[Service]\nProtectSystem=strict\nReadWritePaths=-/x -/y\n")
    env = {
        "SP_CONFIG_FILE": str(etc / "server.env"),
        "SP_UNIT_FILE": str(etc / "unit.service"),
        "SP_FSTAB": str(etc / "fstab"),
        "SP_STATE_DIR": str(tmp_path / "state"),
        "SP_ALLOW_LOOP": "1",
        "SP_HEALTH_WAIT": "2",
    }
    info = {
        "env": env, "ssd": ssd, "hdd": hdd, "fat": fat, "ssd_mp": ssd_mp, "hdd_mp": hdd_mp, "tag": tag,
        "uuid": {d: sh("blkid", "-s", "UUID", "-o", "value", d) for d in (ssd, hdd, fat)},
        "system": RealSystemFakeSystemd(),
    }
    yield info
    table = helper_mod.System().mounts()
    for mount_point, (source, _) in table.items():
        if source in loops:
            subprocess.run(["umount", mount_point], check=False)
    for dev in loops:
        subprocess.run(["losetup", "-d", dev], check=False)
    for path in Path("/mnt").glob(f"sp-it-*-{tag}*"):
        shutil.rmtree(path, ignore_errors=True)


def helper(lab):  # type: ignore[no-untyped-def]
    return helper_mod.StorageHelper(lab["system"], lab["env"])


def findmnt(target: str) -> dict[str, str] | None:
    result = subprocess.run(
        ["findmnt", "-n", "-o", "SOURCE,FSTYPE,OPTIONS", "--mountpoint", target], check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    source, fstype, options = result.stdout.split()
    return {"source": source, "fstype": fstype, "options": options}


def test_real_status_lists_loop_disks(lab):
    status = helper(lab).status()
    parts = {p["path"]: p for p in status["partitions"]}
    assert parts[lab["hdd"]]["usable"] and parts[lab["hdd"]]["fstype"] == "ext4"
    assert parts[lab["fat"]]["reason"] == "unsupported_filesystem"
    assert parts[lab["ssd"]]["role"] == "ssd"
    roles = {r["role"]: r for r in status["roles"]}
    assert roles["ssd"]["mounted"] and roles["ssd"]["device"] == lab["ssd"]
    assert roles["ssd"]["uuid"] == lab["uuid"][lab["ssd"]] and roles["ssd"]["total_bytes"] > 0


def test_real_mount_unmount_hdd(lab):
    h = helper(lab)
    hdd_uuid = lab["uuid"][lab["hdd"]]
    result, _ = h.op_mount("hdd", hdd_uuid)
    h.release()
    assert result["job"]["status"] == "succeeded"
    mounted = findmnt(lab["hdd_mp"])
    assert mounted is not None and mounted["source"] == lab["hdd"] and mounted["fstype"] == "ext4"
    for option in ("nosuid", "nodev", "noexec", "noatime"):
        assert option in mounted["options"].split(","), mounted
    backup_dir = Path(lab["hdd_mp"], "backup")
    assert backup_dir.is_dir() and backup_dir.stat().st_uid == 65534
    fstab = Path(lab["env"]["SP_FSTAB"]).read_text()
    assert helper_mod.parse_managed(fstab)["hdd"]["uuid"] == hdd_uuid
    verify = subprocess.run(["findmnt", "--verify", "--tab-file", lab["env"]["SP_FSTAB"]], check=False, capture_output=True, text=True)
    assert "[E]" not in verify.stdout + verify.stderr, verify.stdout
    (backup_dir / "salinan.txt").write_text("backup")

    h = helper(lab)
    result, _ = h.op_unmount("hdd")
    h.release()
    assert result["job"]["status"] == "succeeded"
    assert findmnt(lab["hdd_mp"]) is None
    assert "hdd" not in helper_mod.parse_managed(Path(lab["env"]["SP_FSTAB"]).read_text())
    # Data tetap di disk: pasang lagi, file masih ada.
    h = helper(lab)
    h.op_mount("hdd", hdd_uuid)
    h.release()
    assert (backup_dir / "salinan.txt").read_text() == "backup"


def test_real_refuses_vfat_and_busy_mount_point(lab):
    h = helper(lab)
    with pytest.raises(helper_mod.HelperError) as exc:
        h.op_mount("hdd", lab["uuid"][lab["fat"]])
    assert exc.value.code == "unsupported_filesystem"
    assert findmnt(lab["hdd_mp"]) is None


def test_real_move_ssd_mount_point(lab):
    new_mp = f"/mnt/sp-it-data-{lab['tag']}"
    h = helper(lab)
    result, cont = h.op_set_mount_point("ssd", new_mp)
    assert result["job"]["status"] == "running"
    cont()
    h.release()
    job = h.read_job()
    assert job["status"] == "succeeded", job
    assert findmnt(lab["ssd_mp"]) is None and not Path(lab["ssd_mp"]).exists()
    mounted = findmnt(new_mp)
    assert mounted is not None and mounted["source"] == lab["ssd"]
    assert Path(new_mp, "storage", "foto.jpg").read_text() == "data asli di SSD"
    config = helper_mod.parse_env(Path(lab["env"]["SP_CONFIG_FILE"]).read_text())
    assert config["PRIMARY_MOUNT_POINT"] == new_mp
    assert config["DATABASE_PATH"] == f"{new_mp}/database/server.db"
    assert Path(new_mp, "database").is_dir()
    assert f"ReadWritePaths=-{new_mp} -{lab['hdd_mp']}" in Path(lab["env"]["SP_UNIT_FILE"]).read_text()
    assert [c[1] for c in lab["system"].systemctl_calls] == ["stop", "daemon-reload", "start"]


def test_real_move_rolls_back_when_mount_fails(lab, monkeypatch):
    new_mp = f"/mnt/sp-it-data-{lab['tag']}"
    real_run = lab["system"].run

    def failing_mount(args, timeout=120):  # type: ignore[no-untyped-def]
        if args[0] == "mount" and args[-1] == new_mp:
            return subprocess.CompletedProcess(args, 32, "", "mount: simulasi gagal")
        return real_run(args, timeout)

    monkeypatch.setattr(lab["system"], "run", failing_mount)
    before = Path(lab["env"]["SP_CONFIG_FILE"]).read_text()
    h = helper(lab)
    _, cont = h.op_set_mount_point("ssd", new_mp)
    cont()
    h.release()
    assert h.read_job()["error_code"] == "mount_failed"
    mounted = findmnt(lab["ssd_mp"])
    assert mounted is not None and mounted["source"] == lab["ssd"]  # SSD kembali di tempat lama
    assert Path(lab["ssd_mp"], "storage", "foto.jpg").read_text() == "data asli di SSD"
    assert Path(lab["env"]["SP_CONFIG_FILE"]).read_text() == before
    assert not Path(new_mp).exists()
