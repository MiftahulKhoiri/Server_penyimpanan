#!/usr/bin/env bash
# Menjalankan server secara manual (tanpa systemd), mis. untuk pengembangan atau debugging.
#
#   scripts/run_server.sh                     memakai .env di folder proyek
#   CONFIG_FILE=/etc/server-penyimpanan/server.env scripts/run_server.sh
#
# Di produksi gunakan layanan systemd (lihat docs/DEPLOYMENT.md), jangan jalankan sebagai root.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if [ "$(id -u)" -eq 0 ]; then
    echo "Jangan menjalankan server sebagai root. Gunakan: sudo systemctl start server-penyimpanan" >&2
    exit 1
fi
if [ ! -x .venv/bin/python ]; then
    echo "Virtualenv belum ada. Buat dengan: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi
if [ -n "${CONFIG_FILE:-}" ]; then
    [ -r "$CONFIG_FILE" ] || { echo "Tidak bisa membaca $CONFIG_FILE" >&2; exit 1; }
    set -a
    # shellcheck disable=SC1090
    . "$CONFIG_FILE"
    set +a
elif [ ! -f .env ]; then
    echo "Tidak ada .env. Salin dulu: cp .env.example .env && chmod 600 .env (lalu isi SECRET_KEY)" >&2
    exit 1
fi
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"  # sama seperti layanan systemd
exec .venv/bin/python run.py
