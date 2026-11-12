#!/usr/bin/env bash
# Memperbarui Server_penyimpanan ke versi terbaru dari GitHub.
#
# Langkah: backup database -> git pull -> pasang ulang (konfigurasi & data tetap)
#          -> migrasi database otomatis -> restart -> cek health.
#
# Pemakaian (dari folder git clone):  sudo ./scripts/update.sh [--no-pull]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=scripts/lib.sh
. "$SCRIPT_DIR/lib.sh"
require_root

PULL=1
[ "${1:-}" = "--no-pull" ] && PULL=0

DATABASE_PATH="$(config_get DATABASE_PATH /mnt/ssd/database/server.db)"
PORT="$(config_get PORT 8000)"
before="$(git -C "$SOURCE_DIR" rev-parse --short HEAD 2>/dev/null || echo '?')"

# 1. Backup database sebelum apa pun berubah (konsisten walau server berjalan).
if [ -f "$DATABASE_PATH" ]; then
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    copy="${DATABASE_PATH}.pre-update-${before}-${stamp}.bak"
    info "Backup database -> $copy"
    runuser -u "$SERVICE_USER" -- python3 - "$DATABASE_PATH" "$copy" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1], timeout=30)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
dst.close(); src.close()
PY
    ok "database aman di $copy"
else
    warn "Database belum ada ($DATABASE_PATH), backup dilewati"
fi

# 2. Ambil kode terbaru (sebagai pemilik folder clone, bukan root).
if [ "$PULL" -eq 1 ]; then
    owner="$(stat -c %U "$SOURCE_DIR")"
    info "git pull di $SOURCE_DIR (sebagai $owner)"
    runuser -u "$owner" -- git -C "$SOURCE_DIR" pull --ff-only
fi
after="$(git -C "$SOURCE_DIR" rev-parse --short HEAD 2>/dev/null || echo '?')"
info "Versi: $before -> $after"

# 3. Pasang ulang tanpa apt & tanpa menyentuh konfigurasi; migrasi DB berjalan otomatis.
"$SOURCE_DIR/scripts/install_pi.sh" --skip-apt

# 4. Cek akhir.
if curl -fsS "http://127.0.0.1:${PORT}/api/v1/system/health" >/dev/null 2>&1; then
    ok "Update selesai, server sehat ($after)"
else
    warn "Server belum sehat setelah update. Cek: sudo journalctl -u $SERVICE_NAME -n 100"
    warn "Kembali ke versi sebelumnya: git -C $SOURCE_DIR checkout $before && sudo ./scripts/update.sh --no-pull"
    [ -n "${copy:-}" ] && warn "Database sebelum update tersimpan di: $copy"
    exit 1
fi
