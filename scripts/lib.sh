#!/usr/bin/env bash
# Fungsi & default bersama untuk script deployment Server_penyimpanan.
# shellcheck disable=SC2034  # variabel dipakai oleh script yang men-source file ini

SERVICE_NAME="${SERVICE_NAME:-server-penyimpanan}"
SERVICE_USER="${SERVICE_USER:-penyimpanan}"
INSTALL_DIR="${INSTALL_DIR:-/opt/server_penyimpanan}"
CONFIG_DIR="${CONFIG_DIR:-/etc/server-penyimpanan}"
CONFIG_FILE="${CONFIG_FILE:-${CONFIG_DIR}/server.env}"
UNIT_FILE="${UNIT_FILE:-/etc/systemd/system/${SERVICE_NAME}.service}"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok() { printf '\033[1;32m OK\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mGAGAL:\033[0m %s\n' "$*" >&2; exit 1; }

require_root() {
    [ "$(id -u)" -eq 0 ] || die "Jalankan dengan sudo: sudo $0"
}

# Baca satu nilai dari file konfigurasi (KEY=VALUE), dengan default.
config_get() {
    local key="$1" default="${2:-}" file="${3:-$CONFIG_FILE}" value=""
    if [ -f "$file" ]; then
        value="$(grep -E "^${key}=" "$file" | tail -n 1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//')"
    fi
    printf '%s' "${value:-$default}"
}

have_systemd() {
    [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1
}

# Jalankan perintah sebagai user layanan dengan konfigurasi dimuat ke environment.
run_as_service() {
    # shellcheck disable=SC2016  # $0/$1/$@ sengaja diekspansi oleh bash di dalam, bukan di sini
    runuser -u "$SERVICE_USER" -- env -i PATH=/usr/bin:/bin HOME=/nonexistent PYTHONDONTWRITEBYTECODE=1 \
        bash -c 'set -a; . "$0"; set +a; cd "$1"; shift; exec "$@"' "$CONFIG_FILE" "$INSTALL_DIR" "$@"
}
