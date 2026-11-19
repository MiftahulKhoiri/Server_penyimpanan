#!/usr/bin/env bash
# Melepas layanan Server_penyimpanan.
#
# Yang dihapus: layanan systemd dan kode di /opt/server_penyimpanan.
# Yang TIDAK PERNAH dihapus: data di SSD/HDD, database, dan konfigurasi
# /etc/server-penyimpanan (hapus manual bila memang tidak diperlukan lagi).
#
# Pemakaian: sudo scripts/uninstall.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
. "$SCRIPT_DIR/lib.sh"
require_root

if have_systemd && [ -f "$UNIT_FILE" ]; then
    systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
    rm -f "$UNIT_FILE"
    systemctl daemon-reload
    ok "layanan $SERVICE_NAME dihapus"
fi
if have_systemd && [ -f "$HELPER_SOCKET_UNIT" ]; then
    systemctl disable --now "$(basename "$HELPER_SOCKET_UNIT")" >/dev/null 2>&1 || true
    rm -f "$HELPER_SOCKET_UNIT" "$HELPER_SERVICE_UNIT"
    systemctl daemon-reload
    # Disk yang sudah dipasang & entri fstab sengaja dibiarkan (data tidak disentuh).
    ok "helper pemasangan disk dihapus (entri /etc/fstab dibiarkan)"
fi
case "$INSTALL_DIR" in
    /opt/*) rm -rf --one-file-system "$INSTALL_DIR"; ok "kode $INSTALL_DIR dihapus" ;;
    *) warn "INSTALL_DIR=$INSTALL_DIR di luar /opt, tidak dihapus otomatis" ;;
esac
cat <<EOF
Data TIDAK dihapus:
  konfigurasi : $CONFIG_DIR
  database    : $(config_get DATABASE_PATH /mnt/ssd/database/server.db)
  file        : $(config_get PRIMARY_STORAGE_PATH /mnt/ssd/storage)
  backup      : $(config_get BACKUP_STORAGE_PATH /mnt/hdd/backup)
User sistem '$SERVICE_USER' juga dibiarkan (hapus manual: sudo userdel $SERVICE_USER).
EOF
