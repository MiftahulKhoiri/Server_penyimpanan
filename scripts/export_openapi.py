#!/usr/bin/env python3
"""Ekspor spesifikasi OpenAPI ke docs/openapi.json (untuk generator client Android, mis.
openapi-generator `kotlin`). Tidak butuh server berjalan, SSD, atau .env.

Pemakaian:  .venv/bin/python scripts/export_openapi.py [--check]
--check: keluar dengan kode 1 bila docs/openapi.json belum sesuai kode (dipakai di tes).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TARGET = ROOT / "docs" / "openapi.json"


def build_spec() -> dict:
    from app.core.config import Settings
    from app.main import create_app

    base = Path(tempfile.mkdtemp(prefix="openapi-"))
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        SECRET_KEY="openapi-export-" + "x" * 40,
        REQUIRE_MOUNTED_STORAGE=False,
        PRIMARY_MOUNT_POINT=base / "ssd",
        PRIMARY_STORAGE_PATH=base / "ssd" / "storage",
        DATABASE_PATH=base / "ssd" / "db" / "server.db",
        BACKUP_MOUNT_POINT=base / "hdd",
        BACKUP_STORAGE_PATH=base / "hdd" / "backup",
        ENABLE_DOCS=True,
    )
    return create_app(settings).openapi()


def render(spec: dict) -> str:
    return json.dumps(spec, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Ekspor OpenAPI ke docs/openapi.json")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = render(build_spec())
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != content:
            print("docs/openapi.json belum diperbarui: jalankan scripts/export_openapi.py", file=sys.stderr)
            return 1
        print("docs/openapi.json sesuai kode")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(content, encoding="utf-8")
    spec = json.loads(content)
    operations = sum(len(ops) for ops in spec["paths"].values())
    print(f"Ditulis {TARGET.relative_to(ROOT)}: {len(spec['paths'])} path, {operations} operasi")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
