#!/usr/bin/env bash
# Uji end-to-end deployment di container Docker (systemd sungguhan, Debian Bookworm/Python 3.11).
# SSD & HDD disimulasikan dengan volume Docker (mount point asli, data bertahan saat "reboot").
#
# Pemakaian (dari root proyek, user harus bisa menjalankan docker):
#   tests/deploy/run_e2e.sh            jalankan semua skenario
#   KEEP=1 tests/deploy/run_e2e.sh     jangan hapus container/volume setelah selesai
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NAME=sp-e2e
IMAGE=sp-e2e:bookworm
PASS=0
FAIL=0

say() { printf '\n\033[1;36m### %s\033[0m\n' "$*"; }
check() {  # check "deskripsi" perintah...
    local desc="$1"; shift
    if "$@" >/tmp/sp-e2e-check.log 2>&1; then
        printf '  \033[32mLULUS\033[0m %s\n' "$desc"; PASS=$((PASS + 1))
    else
        printf '  \033[31mGAGAL\033[0m %s\n' "$desc"; sed 's/^/        /' /tmp/sp-e2e-check.log | tail -n 15; FAIL=$((FAIL + 1))
    fi
}
inside() { docker exec -i "$NAME" bash -c "$*"; }
api() { inside "curl -sS -m 30 $*"; }

remove_all() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker volume rm -f sp-e2e-ssd sp-e2e-hdd >/dev/null 2>&1 || true
}
cleanup() { [ "${KEEP:-0}" = "1" ] || remove_all; }
trap cleanup EXIT
remove_all

say "Menyiapkan container"
docker build -q -t "$IMAGE" "$ROOT/tests/deploy" >/dev/null
docker volume create sp-e2e-ssd >/dev/null
docker volume create sp-e2e-hdd >/dev/null
docker run -d --name "$NAME" --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
    --tmpfs /run --tmpfs /run/lock \
    -v sp-e2e-ssd:/mnt/ssd -v sp-e2e-hdd:/mnt/hdd -v "$ROOT:/src:ro" "$IMAGE" >/dev/null
for _ in $(seq 1 30); do
    state="$(docker exec "$NAME" systemctl is-system-running 2>/dev/null || true)"
    case "$state" in running|degraded) break ;; esac
    sleep 1
done
# Raspberry Pi OS: root mount "shared" (systemd). Docker defaultnya "private", yang membuat
# mount/unmount baru tidak terlihat oleh layanan. Samakan dengan Pi asli.
inside "mount --make-rshared /"
inside "mkdir -p /home/tester/Server_penyimpanan && tar -C /src --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache -cf - . | tar -C /home/tester/Server_penyimpanan -xf - && chown -R tester:tester /home/tester/Server_penyimpanan"
APP=/home/tester/Server_penyimpanan

say "1. Instalasi"
check "install_pi.sh selesai tanpa error" inside "cd $APP && ./scripts/install_pi.sh"
check "layanan aktif" inside "systemctl is-active server-penyimpanan"
check "layanan aktif otomatis saat boot (enabled)" inside "systemctl is-enabled server-penyimpanan"
check "proses berjalan sebagai user 'penyimpanan', bukan root" \
    inside "[ \"\$(ps -o user= -p \$(systemctl show -p MainPID --value server-penyimpanan))\" = penyimpanan ]"
check "user layanan tanpa shell login" inside "getent passwd penyimpanan | grep -q nologin"
check "konfigurasi 640 root:penyimpanan dengan SECRET_KEY acak" \
    inside "stat -c '%a %U %G' /etc/server-penyimpanan/server.env | grep -qx '640 root penyimpanan' && ! grep -q '^SECRET_KEY=ganti' /etc/server-penyimpanan/server.env"
check "kode milik root (read-only bagi layanan)" inside "[ \"\$(stat -c %U /opt/server_penyimpanan/app/main.py)\" = root ] && ! runuser -u penyimpanan -- touch /opt/server_penyimpanan/x 2>/dev/null"
check "database dibuat di SSD" inside "test -f /mnt/ssd/database/server.db"
check "health 200" inside "curl -fsS http://127.0.0.1:8000/api/v1/system/health | grep -q '\"status\":\"ok\"'"

say "2. Admin & pemakaian API"
check "buat admin lewat manage.sh" inside "printf 'AdminKuat2026' | $APP/scripts/manage.sh create-admin admin --password-stdin"
TOKEN="$(api "-X POST http://127.0.0.1:8000/api/v1/auth/login -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"AdminKuat2026\"}'" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')"
AUTH="-H 'Authorization: Bearer $TOKEN'"
inside "head -c 3000000 /dev/urandom > /root/data.bin"
FILE_ID="$(api "-X POST $AUTH 'http://127.0.0.1:8000/api/v1/files?filename=data.bin' --data-binary @/root/data.bin -H 'Content-Type: application/octet-stream'" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')"
check "upload file" test -n "$FILE_ID"
check "download identik" inside "curl -fsS $AUTH http://127.0.0.1:8000/api/v1/files/$FILE_ID/download -o /root/back.bin && cmp /root/data.bin /root/back.bin"
check "file di SSD milik penyimpanan, mode 600 (UMask)" \
    inside "f=\$(find /mnt/ssd/storage/files -type f | head -1); [ \"\$(stat -c '%U %a' \"\$f\")\" = 'penyimpanan 600' ]"

say "3. Backup & restore"
check "backup lewat API (202) lalu selesai" inside "curl -fsS -X POST $AUTH http://127.0.0.1:8000/api/v1/storage/backup >/dev/null && sleep 3 && curl -fsS $AUTH http://127.0.0.1:8000/api/v1/storage/backup/1 | grep -q '\"status\":\"completed\"'"
check "backup-now lewat CLI (inkremental, 0 gagal)" inside "$APP/scripts/manage.sh backup-now | grep -q ', 0 gagal'"
check "salinan database di HDD" inside "ls /mnt/hdd/backup/database/server-*.db"
inside "rm -f \$(find /mnt/ssd/storage/files -type f | head -1)"
check "restore menolak berjalan saat server hidup" inside "! $APP/scripts/manage.sh restore --files --apply"
check "restore simulasi mendeteksi 1 file hilang" inside "$APP/scripts/manage.sh restore --files | grep -q '1 akan dipulihkan'"
inside "systemctl stop server-penyimpanan"
check "restore --files --apply memulihkan file" inside "$APP/scripts/manage.sh restore --files --apply | grep -q '1 dipulihkan'"
check "restore database dari HDD (DB lama disimpan)" inside "$APP/scripts/manage.sh restore --database latest --apply && ls /mnt/ssd/database/*.before-restore-*.bak"
inside "systemctl start server-penyimpanan && sleep 4"
check "file hasil restore identik" inside "curl -fsS $AUTH http://127.0.0.1:8000/api/v1/files/$FILE_ID/download -o /root/back2.bin && cmp /root/data.bin /root/back2.bin"

say "4. Update (idempoten, konfigurasi & data tetap)"
SECRET_BEFORE="$(inside "grep ^SECRET_KEY /etc/server-penyimpanan/server.env")"
check "update.sh --no-pull" inside "cd $APP && ./scripts/update.sh --no-pull"
check "SECRET_KEY tidak berubah" test "$SECRET_BEFORE" = "$(inside "grep ^SECRET_KEY /etc/server-penyimpanan/server.env")"
check "backup DB pra-update dibuat" inside "ls /mnt/ssd/database/server.db.pre-update-*.bak"
check "token lama masih berlaku (secret sama), data tetap" inside "curl -fsS $AUTH http://127.0.0.1:8000/api/v1/files/$FILE_ID >/dev/null"

say "5. Reboot"
docker restart "$NAME" >/dev/null
sleep 2
# Setelah restart container, systemd membuat namespace layanan sebelum propagasi bisa
# di-set ke "shared" (khas Docker). Di Pi asli root sudah shared sejak boot.
inside "mount --make-rshared / && systemctl restart server-penyimpanan"
for _ in $(seq 1 40); do
    inside "curl -fsS http://127.0.0.1:8000/api/v1/system/health" >/dev/null 2>&1 && break
    sleep 1
done
check "server hidup otomatis setelah reboot" inside "curl -fsS http://127.0.0.1:8000/api/v1/system/health | grep -q '\"status\":\"ok\"'"
TOKEN="$(api "-X POST http://127.0.0.1:8000/api/v1/auth/login -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"AdminKuat2026\"}'" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')"
AUTH="-H 'Authorization: Bearer $TOKEN'"
check "data bertahan setelah reboot" inside "curl -fsS $AUTH http://127.0.0.1:8000/api/v1/files/$FILE_ID/download -o /root/back3.bin && cmp /root/data.bin /root/back3.bin"

say "6. HDD dicabut"
inside "umount /mnt/hdd"
check "health tetap 200, backup_storage unavailable" inside "curl -fsS http://127.0.0.1:8000/api/v1/system/health | grep -q '\"backup_storage\":\"unavailable\"'"
check "backup ditolak 503" inside "[ \"\$(curl -s -o /dev/null -w '%{http_code}' -X POST $AUTH http://127.0.0.1:8000/api/v1/storage/backup)\" = 503 ]"
check "upload tetap bisa" inside "curl -fsS -X POST $AUTH 'http://127.0.0.1:8000/api/v1/files?filename=tanpa-hdd.txt' --data-binary 'ok' -H 'Content-Type: application/octet-stream' >/dev/null"

say "7. SSD dicabut"
inside "systemctl stop server-penyimpanan; umount /mnt/ssd; systemctl start server-penyimpanan; sleep 4"
check "layanan tetap hidup (mode degraded)" inside "systemctl is-active server-penyimpanan"
check "health 503 primary_storage unavailable" inside "[ \"\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/v1/system/health)\" = 503 ]"
check "login dijawab 503 yang jelas" inside "curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"x\"}' | grep -q primary_storage_unavailable"
check "TIDAK ada file yang ditulis ke /mnt/ssd kosong (kartu SD)" inside "[ -z \"\$(ls -A /mnt/ssd)\" ]"
check "check_storage.py melaporkan SSD gagal (exit 1)" inside "! python3 /opt/server_penyimpanan/scripts/check_storage.py --env-file /etc/server-penyimpanan/server.env"
check "prepare_storage.sh menolak membuat direktori" inside "! /opt/server_penyimpanan/scripts/prepare_storage.sh && [ -z \"\$(ls -A /mnt/ssd)\" ]"

say "8. SSD & HDD baru ter-mount setelah layanan start (boot USB lambat)"
inside "uid=\$(id -u penyimpanan); gid=\$(id -g penyimpanan); mount -t tmpfs -o size=300m,uid=\$uid,gid=\$gid,mode=0750 tmpfs /mnt/ssd; sleep 7"
check "server pulih sendiri tanpa restart" inside "curl -fsS http://127.0.0.1:8000/api/v1/system/health | grep -q '\"status\":\"ok\"'"
check "database baru dibuat di SSD yang baru dipasang" inside "test -f /mnt/ssd/database/server.db"
inside "uid=\$(id -u penyimpanan); gid=\$(id -g penyimpanan); mount -t tmpfs -o size=50m,uid=\$uid,gid=\$gid,mode=0750 tmpfs /mnt/hdd; sleep 1"
check "HDD yang dipasang belakangan terdeteksi" inside "curl -fsS http://127.0.0.1:8000/api/v1/system/health | grep -q '\"backup_storage\":\"ok\"'"
check "hardening: layanan tidak bisa menulis /etc /var /opt" inside "pid=\$(systemctl show -p MainPID --value server-penyimpanan); for d in /etc /var /opt/server_penyimpanan; do ! nsenter -t \$pid -m -S \$(id -u penyimpanan) -G \$(id -g penyimpanan) touch \$d/uji 2>/dev/null || exit 1; done"

say "9. Tes otomatis di Python 3.11"
check "pytest lolos di Python $(inside 'python3 --version')" inside "cd $APP && python3 -m venv /tmp/venv && /tmp/venv/bin/pip install -q -r requirements-dev.txt && /tmp/venv/bin/python -m pytest -q -p no:cacheprovider"

printf '\nHasil: %s lulus, %s gagal\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
