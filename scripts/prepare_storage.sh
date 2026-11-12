#!/usr/bin/env bash
# Menyiapkan direktori data di SSD & HDD yang SUDAH ter-mount.
#
# Script ini TIDAK memformat, mempartisi, atau me-mount disk, dan tidak menghapus
# apa pun. Ia hanya membuat direktori storage/database/backup dan mengatur
# kepemilikannya untuk user layanan. Bila SSD tidak ter-mount, script berhenti
# supaya data tidak tertulis ke kartu SD.
#
# Pemakaian: sudo scripts/prepare_storage.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
. "$SCRIPT_DIR/lib.sh"
require_root

id "$SERVICE_USER" >/dev/null 2>&1 || die "User layanan '$SERVICE_USER' belum ada; jalankan install_pi.sh dulu"

PRIMARY_MOUNT="$(config_get PRIMARY_MOUNT_POINT /mnt/ssd)"
PRIMARY_STORAGE="$(config_get PRIMARY_STORAGE_PATH /mnt/ssd/storage)"
DATABASE_PATH="$(config_get DATABASE_PATH /mnt/ssd/database/server.db)"
BACKUP_MOUNT="$(config_get BACKUP_MOUNT_POINT /mnt/hdd)"
BACKUP_STORAGE="$(config_get BACKUP_STORAGE_PATH /mnt/hdd/backup)"

make_dir() {
    local dir="$1"
    install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$dir"
    # Perbaiki kepemilikan tanpa menyentuh isi yang sudah ada.
    chown "$SERVICE_USER:$SERVICE_USER" "$dir"
    chmod 0750 "$dir"
    ok "$dir"
}

status=0
if mountpoint -q "$PRIMARY_MOUNT"; then
    info "SSD ter-mount di $PRIMARY_MOUNT"
    make_dir "$PRIMARY_STORAGE"
    make_dir "$(dirname "$DATABASE_PATH")"
else
    warn "SSD TIDAK ter-mount di $PRIMARY_MOUNT — direktori data tidak dibuat (mencegah penulisan ke kartu SD)"
    status=1
fi

if mountpoint -q "$BACKUP_MOUNT"; then
    info "HDD ter-mount di $BACKUP_MOUNT"
    make_dir "$BACKUP_STORAGE"
else
    warn "HDD tidak ter-mount di $BACKUP_MOUNT — backup tidak tersedia sampai HDD dipasang (server utama tetap jalan)"
fi
exit "$status"
