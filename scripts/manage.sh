#!/usr/bin/env bash
# Perintah administrasi Server_penyimpanan setelah terpasang.
#
#   sudo scripts/manage.sh status            status layanan + health
#   sudo scripts/manage.sh logs              ikuti log (Ctrl+C untuk keluar)
#   sudo scripts/manage.sh check-storage     periksa SSD & HDD
#   sudo scripts/manage.sh prepare-storage   buat direktori data di SSD/HDD yang ter-mount
#   sudo scripts/manage.sh init-db           buat / migrasi database
#   sudo scripts/manage.sh create-admin NAMA buat akun administrator
#   sudo scripts/manage.sh backup-now [--verify-existing]
#   sudo scripts/manage.sh restore ARGUMEN   lihat: manage.sh restore --help
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
. "$SCRIPT_DIR/lib.sh"

cmd="${1:-help}"
[ $# -gt 0 ] && shift
PY="$INSTALL_DIR/.venv/bin/python"

case "$cmd" in
    status)
        if have_systemd; then
            systemctl --no-pager status "$SERVICE_NAME" | head -n 12 || true
        fi
        port="$(config_get PORT 8000)"
        echo
        if curl -sS "http://127.0.0.1:${port}/api/v1/system/health"; then
            echo
        else
            warn "server tidak merespons di port $port"
        fi
        ;;
    logs)
        exec journalctl -u "$SERVICE_NAME" -f -n 100
        ;;
    check-storage)
        exec python3 "$INSTALL_DIR/scripts/check_storage.py" --env-file "$CONFIG_FILE" "$@"
        ;;
    prepare-storage)
        exec "$INSTALL_DIR/scripts/prepare_storage.sh"
        ;;
    init-db)
        require_root
        run_as_service "$PY" scripts/init_db.py
        ;;
    create-admin)
        require_root
        [ $# -ge 1 ] || die "Pemakaian: manage.sh create-admin <username>"
        run_as_service "$PY" scripts/create_admin.py --username "$@"
        ;;
    backup-now)
        require_root
        run_as_service "$PY" scripts/backup_now.py "$@"
        ;;
    restore)
        require_root
        run_as_service "$PY" scripts/restore_backup.py "$@"
        ;;
    help|-h|--help)
        sed -n '2,12p' "$0"
        ;;
    *)
        die "Perintah tidak dikenal: $cmd (lihat: manage.sh help)"
        ;;
esac
