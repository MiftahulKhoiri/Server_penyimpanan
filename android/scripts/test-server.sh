#!/usr/bin/env bash
# Menjalankan backend Python sementara untuk tes integrasi aplikasi Android.
#   android/scripts/test-server.sh start   -> cetak SP_SERVER_URL & SP_ADMIN_PASSWORD (format env)
#   android/scripts/test-server.sh stop
# Data disimpan di direktori sementara dan dihapus saat stop. Tidak menyentuh .env produksi.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PORT="${SP_TEST_PORT:-8099}"
WORK="${SP_TEST_DIR:-${TMPDIR:-/tmp}/sp-android-test}"
PY="${PYTHON:-python3}"
[ -x "$ROOT/.venv/bin/python" ] && PY="$ROOT/.venv/bin/python"

stop() {
    if [ -f "$WORK/server.pid" ]; then
        kill "$(cat "$WORK/server.pid")" 2>/dev/null || true
        sleep 1
    fi
    rm -rf "$WORK"
}

start() {
    stop
    mkdir -p "$WORK/primary/storage" "$WORK/primary/database" "$WORK/backup/data"
    local password
    password="$("$PY" -c 'import secrets; print("Tes-" + secrets.token_urlsafe(18))')"

    export ENVIRONMENT=development HOST=127.0.0.1 PORT="$PORT" LOG_LEVEL=WARNING
    export SECRET_KEY="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(48))')"
    export PRIMARY_MOUNT_POINT="$WORK/primary" PRIMARY_STORAGE_PATH="$WORK/primary/storage"
    export DATABASE_PATH="$WORK/primary/database/server.db"
    export BACKUP_MOUNT_POINT="$WORK/backup" BACKUP_STORAGE_PATH="$WORK/backup/data"
    export REQUIRE_MOUNTED_STORAGE=false MIN_FREE_SPACE_BYTES=0 BACKUP_MIN_FREE_SPACE_BYTES=0
    export UPLOAD_CHUNK_SIZE_MIN=262144

    cd "$WORK" # tanpa .env di sini: konfigurasi hanya dari variabel di atas
    "$PY" "$ROOT/scripts/init_db.py" >"$WORK/init.log" 2>&1
    printf '%s' "$password" | "$PY" "$ROOT/scripts/create_admin.py" --username admin --password-stdin >"$WORK/admin.log" 2>&1
    nohup "$PY" "$ROOT/run.py" >"$WORK/server.log" 2>&1 &
    echo $! >"$WORK/server.pid"

    for _ in $(seq 1 60); do
        if curl -fsS "http://127.0.0.1:$PORT/api/v1/system/health" >/dev/null 2>&1; then
            echo "SP_SERVER_URL=http://127.0.0.1:$PORT"
            echo "SP_ADMIN_PASSWORD=$password"
            return 0
        fi
        sleep 1
    done
    echo "Server tes gagal start:" >&2
    cat "$WORK/init.log" "$WORK/admin.log" "$WORK/server.log" >&2 || true
    return 1
}

case "${1:-start}" in
    start) start ;;
    stop) stop ;;
    *) echo "pakai: $0 start|stop" >&2; exit 2 ;;
esac
