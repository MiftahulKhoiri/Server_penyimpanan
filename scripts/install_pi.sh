#!/usr/bin/env bash
# Instalasi / pembaruan Server_penyimpanan di Raspberry Pi (Raspberry Pi OS Lite 64-bit).
#
# Aman dijalankan berulang: konfigurasi (/etc/server-penyimpanan/server.env) dan data
# di SSD/HDD tidak pernah ditimpa. Disk tidak diformat dan tidak di-mount oleh script ini.
#
# Pemakaian (dari folder hasil git clone):
#   sudo ./scripts/install_pi.sh [--no-ffmpeg] [--skip-apt] [--no-start]
#                                [--admin USERNAME]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=scripts/lib.sh
. "$SCRIPT_DIR/lib.sh"

WITH_FFMPEG=1
SKIP_APT=0
START=1
ADMIN_USER=""
while [ $# -gt 0 ]; do
    case "$1" in
        --no-ffmpeg) WITH_FFMPEG=0 ;;
        --skip-apt) SKIP_APT=1 ;;
        --no-start) START=0 ;;
        --admin) ADMIN_USER="${2:?--admin butuh username}"; shift ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) die "Opsi tidak dikenal: $1" ;;
    esac
    shift
done
require_root

# ---------------------------------------------------------------- 1. paket sistem
if [ "$SKIP_APT" -eq 0 ]; then
    info "Memasang paket sistem"
    packages=(python3 python3-venv python3-dev rsync sqlite3 ca-certificates curl)
    [ "$WITH_FFMPEG" -eq 1 ] && packages+=(ffmpeg)
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends "${packages[@]}" >/dev/null
    ok "${packages[*]}"
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || die "Butuh Python 3.11 atau lebih baru (terpasang: $(python3 --version 2>&1))"

# ---------------------------------------------------------------- 2. user layanan
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    info "Membuat user sistem '$SERVICE_USER' (tanpa login, tanpa sudo)"
    useradd --system --user-group --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin "$SERVICE_USER"
fi
ok "user layanan: $SERVICE_USER"

# ---------------------------------------------------------------- 3. kode aplikasi
info "Menyalin aplikasi ke $INSTALL_DIR"
install -d -m 0755 "$INSTALL_DIR"
if [ "$SOURCE_DIR" != "$INSTALL_DIR" ]; then
    rsync -a --delete \
        --exclude '.venv/' --exclude '__pycache__/' --exclude '.pytest_cache/' \
        --exclude '.env' --exclude '*.db' --exclude '*.db-wal' --exclude '*.db-shm' \
        "$SOURCE_DIR/" "$INSTALL_DIR/"
fi
# Kode milik root dan read-only bagi user layanan.
chown -R root:root "$INSTALL_DIR"
chmod -R u=rwX,go=rX "$INSTALL_DIR"
ok "kode di $INSTALL_DIR"

# ---------------------------------------------------------------- 4. virtualenv
info "Menyiapkan virtualenv & dependensi Python"
if [ ! -x "$INSTALL_DIR/.venv/bin/python" ]; then
    python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
"$INSTALL_DIR/.venv/bin/python" -m compileall -q "$INSTALL_DIR/app" "$INSTALL_DIR/migrations" "$INSTALL_DIR/scripts" >/dev/null
chown -R root:root "$INSTALL_DIR/.venv"
ok "$("$INSTALL_DIR/.venv/bin/python" --version)"

# ---------------------------------------------------------------- 5. konfigurasi
install -d -m 0750 -o root -g "$SERVICE_USER" "$CONFIG_DIR"
if [ ! -f "$CONFIG_FILE" ]; then
    info "Membuat konfigurasi baru $CONFIG_FILE (SECRET_KEY acak)"
    secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
    sed -e "s|^SECRET_KEY=.*|SECRET_KEY=${secret}|" "$INSTALL_DIR/.env.example" >"$CONFIG_FILE"
else
    info "Konfigurasi $CONFIG_FILE sudah ada — tidak diubah"
    # Tambahkan kunci baru dari versi terbaru sebagai komentar agar mudah dilihat.
    missing=()
    while IFS= read -r key; do
        grep -qE "^#?\s*${key}=" "$CONFIG_FILE" || missing+=("$key")
    done < <(grep -oP '^[A-Z0-9_]+(?==)' "$INSTALL_DIR/.env.example")
    if [ "${#missing[@]}" -gt 0 ]; then
        warn "Kunci konfigurasi baru tersedia (pakai default): ${missing[*]} — lihat $INSTALL_DIR/.env.example"
    fi
fi
chown root:"$SERVICE_USER" "$CONFIG_FILE"
chmod 0640 "$CONFIG_FILE"
grep -q '^SECRET_KEY=ganti-dengan' "$CONFIG_FILE" && die "SECRET_KEY di $CONFIG_FILE masih nilai contoh"
ok "konfigurasi $CONFIG_FILE (mode 640, root:$SERVICE_USER)"

PRIMARY_MOUNT="$(config_get PRIMARY_MOUNT_POINT /mnt/ssd)"
BACKUP_MOUNT="$(config_get BACKUP_MOUNT_POINT /mnt/hdd)"
PORT="$(config_get PORT 8000)"
for mp in "$PRIMARY_MOUNT" "$BACKUP_MOUNT"; do
    [ -d "$mp" ] || install -d -m 0755 "$mp"  # hanya direktori mount point, tidak me-mount
done

# ---------------------------------------------------------------- 6. storage
info "Memeriksa SSD & HDD"
storage_ok=1
python3 "$INSTALL_DIR/scripts/check_storage.py" --env-file "$CONFIG_FILE" || storage_ok=0
"$INSTALL_DIR/scripts/prepare_storage.sh" || storage_ok=0

# ---------------------------------------------------------------- 7. database
if [ "$storage_ok" -eq 1 ]; then
    info "Inisialisasi / migrasi database"
    run_as_service "$INSTALL_DIR/.venv/bin/python" scripts/init_db.py
    if [ -n "$ADMIN_USER" ]; then
        info "Membuat akun admin '$ADMIN_USER'"
        run_as_service "$INSTALL_DIR/.venv/bin/python" scripts/create_admin.py --username "$ADMIN_USER" </dev/tty \
            || warn "Akun admin tidak dibuat"
    fi
else
    warn "SSD belum siap: database belum dibuat. Pasang & mount SSD, lalu jalankan script ini lagi."
fi

# ---------------------------------------------------------------- 8. systemd
if have_systemd; then
    info "Memasang layanan systemd $SERVICE_NAME"
    sed -e "s|@SERVICE_USER@|${SERVICE_USER}|g" \
        -e "s|@INSTALL_DIR@|${INSTALL_DIR}|g" \
        -e "s|@CONFIG_FILE@|${CONFIG_FILE}|g" \
        -e "s|@PRIMARY_MOUNT@|${PRIMARY_MOUNT}|g" \
        -e "s|@BACKUP_MOUNT@|${BACKUP_MOUNT}|g" \
        "$INSTALL_DIR/deploy/server-penyimpanan.service" >"$UNIT_FILE"
    chmod 0644 "$UNIT_FILE"
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
    ok "aktif otomatis saat boot (systemctl is-enabled: $(systemctl is-enabled "$SERVICE_NAME"))"
    if [ "$START" -eq 1 ]; then
        systemctl restart "$SERVICE_NAME"
        for _ in $(seq 1 30); do
            curl -fsS "http://127.0.0.1:${PORT}/api/v1/system/health" >/dev/null 2>&1 && break
            sleep 1
        done
        if curl -fsS "http://127.0.0.1:${PORT}/api/v1/system/health" >/dev/null 2>&1; then
            ok "server berjalan: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${PORT}"
        else
            warn "server berjalan tapi belum sehat: $(curl -s "http://127.0.0.1:${PORT}/api/v1/system/health" || echo 'tidak merespons')"
            warn "lihat log: sudo journalctl -u $SERVICE_NAME -n 50"
        fi
    fi
else
    warn "systemd tidak terdeteksi: layanan tidak dipasang. Jalankan manual dengan scripts/run_server.sh"
fi

cat <<EOF

Selesai.
  Konfigurasi : $CONFIG_FILE
  Kode        : $INSTALL_DIR
  Log         : sudo journalctl -u $SERVICE_NAME -f
  Status      : sudo systemctl status $SERVICE_NAME
  Buat admin  : sudo $INSTALL_DIR/scripts/manage.sh create-admin <username>
EOF
