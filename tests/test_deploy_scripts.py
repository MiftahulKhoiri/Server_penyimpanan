"""Tes script deployment: check_storage.py, restore_backup.py, backup_now.py, unit systemd."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import PROJECT_ROOT
from tests.conftest import auth_header, create_user_direct, login, storage_root, upload_simple

SCRIPTS = PROJECT_ROOT / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"script_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"script_{name}"] = module  # dataclass butuh modul terdaftar
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


check_storage = _load("check_storage")
restore_backup = _load("restore_backup")


# ---------------- check_storage.py ----------------
def test_parse_env_file(tmp_path):
    env = tmp_path / "server.env"
    env.write_text("# komentar\nPRIMARY_MOUNT_POINT=/mnt/x\nSECRET_KEY='abc=def'\n\nKOSONG=\n")
    assert check_storage.parse_env_file(env) == {"PRIMARY_MOUNT_POINT": "/mnt/x", "SECRET_KEY": "abc=def", "KOSONG": ""}


def test_fstab_parsing_detects_missing_nofail(tmp_path):
    fstab = "UUID=aaaa /mnt/ssd ext4 defaults,noatime 0 2\nUUID=bbbb /mnt/hdd ext4 defaults,nofail 0 2\n"
    entries = check_storage.fstab_entries(fstab)
    assert entries["/mnt/ssd"][1] == "defaults,noatime"
    report = check_storage.inspect_device(
        "SSD", tmp_path / "ssd", tmp_path / "ssd" / "storage", mounts={},
        fstab_text=fstab.replace("/mnt/ssd", str(tmp_path / "ssd")), critical=True,
    )
    assert report.fstab_has_nofail is False
    assert any("nofail" in w for w in report.warnings)


def test_unmounted_ssd_is_critical_and_hdd_only_warning(tmp_path):
    (tmp_path / "ssd").mkdir()
    (tmp_path / "hdd").mkdir()
    config = {
        "PRIMARY_MOUNT_POINT": str(tmp_path / "ssd"), "PRIMARY_STORAGE_PATH": str(tmp_path / "ssd/storage"),
        "DATABASE_PATH": str(tmp_path / "ssd/database/server.db"),
        "BACKUP_MOUNT_POINT": str(tmp_path / "hdd"), "BACKUP_STORAGE_PATH": str(tmp_path / "hdd/backup"),
    }
    (ssd, hdd), code = check_storage.run(config, "")
    assert code == 1
    assert not ssd.mounted and any("TIDAK ter-mount" in e for e in ssd.errors)
    assert ssd.suggested_fstab and "nofail" in ssd.suggested_fstab
    assert not hdd.errors and any("TIDAK ter-mount" in w for w in hdd.warnings)


def test_root_mount_point_rejected():
    report = check_storage.inspect_device("SSD", Path("/"), Path("/storage"), mounts={}, fstab_text="", critical=True)
    assert report.errors


def test_real_mount_point_reported(tmp_path):
    # "/" selalu mount point; uji pembacaan kapasitas & tes tulis lewat mount point nyata.
    mounts = check_storage.mount_table()
    real = next((Path(mp) for mp in ("/boot/firmware", "/boot", "/tmp") if os.path.ismount(mp)), None)
    if real is None:
        pytest.skip("tidak ada mount point selain /")
    report = check_storage.inspect_device("X", real, real / "data", mounts=mounts, fstab_text="", critical=True)
    assert report.mounted and report.total_bytes and report.filesystem


def test_check_storage_cli_json(tmp_path):
    env = tmp_path / "server.env"
    env.write_text(f"PRIMARY_MOUNT_POINT={tmp_path}/ssd\nPRIMARY_STORAGE_PATH={tmp_path}/ssd/s\n"
                   f"DATABASE_PATH={tmp_path}/ssd/db/x.db\nBACKUP_MOUNT_POINT={tmp_path}/hdd\n"
                   f"BACKUP_STORAGE_PATH={tmp_path}/hdd/b\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_storage.py"), "--env-file", str(env), "--json", "--fstab", "/dev/null"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1
    assert '"ok": false' in result.stdout


# ---------------- restore_backup.py ----------------
def _backup(c, h):
    resp = c.post("/api/v1/storage/backup", headers=h)
    assert resp.status_code == 202
    c.app.state.backup_manager.wait(30)


def test_restore_missing_and_corrupt_files(client):
    create_user_direct(client, "admin", role="admin")
    h = auth_header(login(client, "admin"))
    ids = [upload_simple(client, h, f"{i}.bin", os.urandom(3000)).json()["id"] for i in range(3)]
    never_backed_up = None
    _backup(client, h)
    never_backed_up = upload_simple(client, h, "baru.bin", b"belum dibackup").json()["id"]
    settings = client.app.state.settings
    con = sqlite3.connect(settings.DATABASE_PATH)
    paths = dict(con.execute("SELECT id, storage_path FROM files").fetchall())
    con.close()
    root = storage_root(client)
    original = {fid: (root / paths[fid]).read_bytes() for fid in ids}
    (root / paths[ids[0]]).unlink()  # hilang
    (root / paths[ids[1]]).write_bytes(b"\x00" * 3000)  # rusak, ukuran sama
    (root / paths[never_backed_up]).unlink()  # hilang & tidak ada salinan

    dry = restore_backup.restore_files(settings, apply=False, verify=True)
    assert sorted(dry.restored) == sorted(ids[:2])
    assert not (root / paths[ids[0]]).exists()  # simulasi tidak mengubah apa pun

    report = restore_backup.restore_files(settings, apply=True, verify=True)
    assert report.ok == 1 and sorted(report.restored) == sorted(ids[:2])
    assert report.unrecoverable == [(never_backed_up, "no_backup_copy")]
    for fid in ids:
        assert (root / paths[fid]).read_bytes() == original[fid]
    assert (root / (paths[ids[1]] + ".corrupt")).exists()  # file rusak disimpan, tidak dihapus

    # Tanpa --verify kerusakan isi (ukuran sama) tidak terdeteksi: perilaku yang terdokumentasi.
    (root / paths[ids[2]]).write_bytes(b"\x01" * 3000)
    assert restore_backup.restore_files(settings, apply=False, verify=False).restored == []


def test_restore_database(client):
    create_user_direct(client, "admin", role="admin")
    h = auth_header(login(client, "admin"))
    upload_simple(client, h, "sebelum.txt", b"1")
    _backup(client, h)
    upload_simple(client, h, "sesudah.txt", b"2")
    settings = client.app.state.settings
    client.app.state.database.dispose()
    assert len(restore_backup.list_db_backups(settings)) == 1
    restore_backup.restore_database(settings, "latest", apply=True, stamp="TEST")
    con = sqlite3.connect(settings.DATABASE_PATH)
    names = {row[0] for row in con.execute("SELECT name FROM files")}
    con.close()
    assert names == {"sebelum.txt"}
    saved = settings.DATABASE_PATH.with_name("server.db.before-restore-TEST.bak")
    con = sqlite3.connect(saved)
    assert {row[0] for row in con.execute("SELECT name FROM files")} == {"sebelum.txt", "sesudah.txt"}
    con.close()


def test_restore_rejects_corrupt_database_copy(client):
    create_user_direct(client, "admin", role="admin")
    _backup(client, auth_header(login(client, "admin")))
    settings = client.app.state.settings
    copy = restore_backup.list_db_backups(settings)[-1]
    copy.write_bytes(b"bukan database sqlite" * 100)
    with pytest.raises(SystemExit):
        restore_backup.restore_database(settings, "latest", apply=True, stamp="X")


# ---------------- backup_now.py & unit systemd ----------------
def test_backup_now_cli(tmp_path, make_client):
    c = make_client()
    create_user_direct(c, "admin", role="admin")
    upload_simple(c, auth_header(login(c, "admin")), "a.txt", b"isi")
    s = c.app.state.settings
    env = {
        **os.environ, "SECRET_KEY": s.SECRET_KEY, "REQUIRE_MOUNTED_STORAGE": "false",
        "PRIMARY_MOUNT_POINT": str(s.PRIMARY_MOUNT_POINT), "PRIMARY_STORAGE_PATH": str(s.PRIMARY_STORAGE_PATH),
        "DATABASE_PATH": str(s.DATABASE_PATH), "BACKUP_MOUNT_POINT": str(s.BACKUP_MOUNT_POINT),
        "BACKUP_STORAGE_PATH": str(s.BACKUP_STORAGE_PATH),
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "backup_now.py")], capture_output=True, text=True, timeout=60,
        env=env, cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "completed" in result.stdout and "1 disalin" in result.stdout


def test_systemd_unit_template_is_hardened():
    unit = (PROJECT_ROOT / "deploy" / "server-penyimpanan.service").read_text()
    for directive in (
        "User=@SERVICE_USER@", "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes",
        "PrivateTmp=yes", "CapabilityBoundingSet=", "Restart=on-failure", "WantedBy=multi-user.target",
        "EnvironmentFile=@CONFIG_FILE@", "MemoryMax=",
    ):
        assert directive in unit, directive
    assert "User=root" not in unit and "RequiresMountsFor" not in unit.split("[Service]")[1]
