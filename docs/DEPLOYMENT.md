# Panduan Deployment Server_penyimpanan di Raspberry Pi 3 Model B+

Panduan ini membawa Anda dari Raspberry Pi kosong sampai server berjalan otomatis setiap kali
Pi dinyalakan, termasuk cara update, backup, dan restore.

**Target:** Raspberry Pi 3 Model B+ (RAM 1 GB), Raspberry Pi OS Lite 64-bit, SSD 960 GB
(penyimpanan utama) dan HDD 250 GB (backup).

---

## 1. Persiapan perangkat keras

- **Catu daya:** pakai adaptor resmi 5 V 2,5 A. Port USB Pi 3 hanya memberi total ±1,2 A, jadi SSD
  dan *terutama* HDD 2,5" sebaiknya dipasang lewat **USB hub yang punya adaptor daya sendiri**.
  Daya kurang menyebabkan disk terputus sendiri dan data rusak. Tanda daya kurang: ikon petir
  di layar atau `vcgencmd get_throttled` tidak bernilai `throttled=0x0`.
- **Kecepatan:** SSD, HDD, dan Ethernet di Pi 3 berbagi satu jalur USB 2.0. Realistisnya:
  upload/download ±20–30 MB/s, backup SSD → HDD ±15 MB/s. Backup pertama 900 GB bisa memakan
  belasan jam; backup berikutnya hanya menyalin file baru.
- **Kartu SD** hanya untuk sistem operasi. Semua data dan database ada di SSD.

## 2. Instal sistem operasi

1. Tulis **Raspberry Pi OS Lite (64-bit)** ke kartu SD dengan Raspberry Pi Imager.
   Di pengaturan Imager: atur hostname (mis. `cloudpi`), aktifkan SSH, buat user Anda sendiri.
2. Boot, lalu login lewat SSH dan perbarui sistem:
   ```bash
   sudo apt update && sudo apt full-upgrade -y
   sudo reboot
   ```
3. Disarankan: beri Pi alamat IP tetap (reservasi DHCP di router) agar aplikasi Android selalu
   menemukan server di alamat yang sama.

## 3. Siapkan SSD dan HDD

> ⚠️ **Memformat menghapus seluruh isi disk.** Pastikan Anda memilih disk yang benar.
> Script instalasi Server_penyimpanan **tidak pernah** memformat atau me-mount disk; langkah
> ini Anda lakukan sendiri sekali saja.

1. Colokkan SSD dan HDD, lalu lihat namanya:
   ```bash
   lsblk -o NAME,SIZE,MODEL,FSTYPE,MOUNTPOINT
   ```
   Contoh: SSD = `sda` (894G), HDD = `sdb` (232G).
2. **Hanya untuk disk baru/kosong**, buat partisi dan format ext4 (ganti `sdX` dengan benar):
   ```bash
   sudo parted /dev/sdX --script mklabel gpt mkpart primary ext4 0% 100%
   sudo mkfs.ext4 -L ssd-data /dev/sdX1      # untuk SSD
   # sudo mkfs.ext4 -L hdd-backup /dev/sdY1  # untuk HDD
   ```
3. Buat mount point dan cari UUID partisi:
   ```bash
   sudo mkdir -p /mnt/ssd /mnt/hdd
   sudo blkid /dev/sdX1 /dev/sdY1
   ```
4. Tambahkan ke `/etc/fstab` (pakai **UUID**, bukan `/dev/sda1`, karena urutan bisa berubah):
   ```
   UUID=<uuid-ssd>  /mnt/ssd  ext4  defaults,noatime,nofail,x-systemd.device-timeout=30s  0  2
   UUID=<uuid-hdd>  /mnt/hdd  ext4  defaults,noatime,nofail,x-systemd.device-timeout=30s  0  2
   ```
   Opsi `nofail` **penting**: tanpa itu Pi akan macet di *emergency mode* saat boot bila satu
   disk tidak terpasang.
5. Mount dan periksa:
   ```bash
   sudo systemctl daemon-reload
   sudo mount -a
   findmnt /mnt/ssd; findmnt /mnt/hdd
   ```

## 4. Instal Server_penyimpanan

```bash
sudo apt install -y git
git clone https://github.com/MiftahulKhoiri/Server_penyimpanan.git
cd Server_penyimpanan
python3 scripts/check_storage.py          # cek SSD & HDD (hanya membaca)
sudo ./scripts/install_pi.sh --admin admin  # --admin opsional: langsung buat akun admin
```

Script instalasi melakukan:

| Langkah | Hasil |
|---|---|
| Paket sistem | `python3-venv`, `ffmpeg` (lewati dengan `--no-ffmpeg`), `rsync`, `sqlite3`, `curl` |
| User layanan | user sistem `penyimpanan` tanpa login dan tanpa sudo; server **tidak** berjalan sebagai root |
| Kode | disalin ke `/opt/server_penyimpanan`, milik root, read-only bagi layanan |
| Python | virtualenv di `/opt/server_penyimpanan/.venv` |
| Konfigurasi | `/etc/server-penyimpanan/server.env` (mode 640) dengan `SECRET_KEY` acak; **tidak pernah ditimpa** saat dijalankan ulang |
| Storage | memeriksa mount, lalu membuat `/mnt/ssd/storage`, `/mnt/ssd/database`, `/mnt/hdd/backup` milik `penyimpanan` |
| Database | dibuat & dimigrasi ke skema terbaru |
| systemd | layanan `server-penyimpanan` diaktifkan agar **otomatis berjalan saat boot**, lalu dijalankan |

Bila SSD belum ter-mount, instalasi tetap selesai tetapi database tidak dibuat. Setelah SSD
terpasang, jalankan `sudo ./scripts/install_pi.sh` lagi.

Buat akun admin (bila belum dibuat dengan `--admin`):
```bash
sudo /opt/server_penyimpanan/scripts/manage.sh create-admin admin
```

Cek hasilnya:
```bash
sudo /opt/server_penyimpanan/scripts/manage.sh status
curl http://localhost:8000/api/v1/system/health
```
Dokumentasi API interaktif: `http://<ip-pi>:8000/docs`.

## 5. Konfigurasi

Semua pengaturan ada di `/etc/server-penyimpanan/server.env` (daftar lengkap dan penjelasannya
di `.env.example`). Setelah mengubahnya, restart layanan:
```bash
sudo nano /etc/server-penyimpanan/server.env
sudo systemctl restart server-penyimpanan
```

Yang paling sering diubah:

| Kunci | Default | Keterangan |
|---|---|---|
| `PORT` | 8000 | Port HTTP |
| `ALLOW_REGISTRATION` | false | true = siapa saja bisa daftar dari aplikasi |
| `TRASH_RETENTION_DAYS` | 30 | Lama item disimpan di trash sebelum dihapus permanen |
| `BACKUP_AUTO_INTERVAL_HOURS` | 0 (mati) | Mis. `24` untuk backup otomatis harian |
| `BACKUP_DB_KEEP` | 0 (semua) | Jumlah salinan database yang disimpan di HDD |
| `MAX_UPLOAD_SIZE_BYTES` | 64 GiB | Batas ukuran per file |
| `ENABLE_DOCS` | true | false = matikan `/docs` |

## 6. Akses dari aplikasi Android

- **Di jaringan rumah:** `http://<ip-pi>:8000`.
- **Dari luar rumah:** jangan membuka port 8000 langsung ke internet tanpa HTTPS, karena token
  login akan terkirim tanpa enkripsi. Pilihan yang disarankan:
  1. **VPN (paling mudah & aman):** pasang [Tailscale](https://tailscale.com) di Pi dan di HP, lalu
     akses lewat IP Tailscale Pi.
  2. **Reverse proxy HTTPS** (Caddy/Nginx) dengan domain. Setel `FORWARDED_ALLOW_IPS=127.0.0.1`
     agar IP asli klien tercatat dengan benar di activity log.
- Pemutar video (ExoPlayer/Media3) perlu mengirim header `Authorization: Bearer <token>`
  pada request stream.

## 7. Operasi harian

```bash
M=/opt/server_penyimpanan/scripts/manage.sh
sudo $M status              # status layanan + health
sudo $M logs                # log realtime (Ctrl+C keluar)
sudo $M check-storage       # periksa SSD & HDD
sudo $M backup-now          # backup sekarang (laporan langsung tampil)
sudo systemctl restart server-penyimpanan
```

## 8. Update aplikasi

```bash
cd ~/Server_penyimpanan
sudo ./scripts/update.sh
```

`update.sh` akan:
1. Mem-backup database ke `server.db.pre-update-<versi>-<waktu>.bak` di samping database.
2. `git pull` versi terbaru.
3. Memasang ulang kode & dependensi. Konfigurasi dan data tidak disentuh.
4. Restart layanan; migrasi database berjalan otomatis (database juga di-backup otomatis
   sebelum migrasi).
5. Mengecek health dan, bila gagal, menampilkan cara kembali ke versi sebelumnya.

Kembali ke versi sebelumnya secara manual:
```bash
git -C ~/Server_penyimpanan checkout <commit-lama>
sudo ./scripts/update.sh --no-pull
```

## 9. Backup

**Isi backup** (`/mnt/hdd/backup`):
- `files/...`: salinan setiap file dengan struktur yang sama seperti di SSD.
- `database/server-<waktu>.db`: salinan database (dicek `integrity_check`).

**Cara menjalankan:**
- Dari aplikasi/API (admin): `POST /api/v1/storage/backup`, lalu pantau `GET /api/v1/storage/backup/{id}`.
- Dari terminal: `sudo /opt/server_penyimpanan/scripts/manage.sh backup-now`.
- Otomatis: setel `BACKUP_AUTO_INTERVAL_HOURS=24` di konfigurasi.

**Jaminan:**
- File asli di SSD **tidak pernah** dihapus atau diubah oleh backup.
- Setiap salinan dicocokkan SHA-256-nya dengan catatan saat upload. File SSD yang ternyata rusak
  tidak akan menimpa salinan lama yang masih baik, dan dilaporkan sebagai `source_checksum_mismatch`.
- Salinan di HDD tidak ikut terhapus saat file dihapus dari server (bukan mirror). Akibatnya HDD
  lama-lama bisa penuh; pantau lewat `GET /api/v1/storage/hdd`.

Disarankan sesekali menjalankan backup dengan verifikasi penuh untuk mendeteksi kerusakan di HDD:
```bash
sudo /opt/server_penyimpanan/scripts/manage.sh backup-now --verify-existing
```

## 10. Restore

Semua perintah restore **hanya simulasi** sampai Anda menambahkan `--apply`, dan tidak pernah
menghapus file.

```bash
M=/opt/server_penyimpanan/scripts/manage.sh
sudo systemctl stop server-penyimpanan

sudo $M restore --list                          # daftar salinan database di HDD
sudo $M restore --files --verify                # simulasi: file hilang/rusak di SSD
sudo $M restore --files --verify --apply        # pulihkan file dari HDD

sudo systemctl start server-penyimpanan
```

**Database rusak atau hilang:**
```bash
sudo systemctl stop server-penyimpanan
sudo $M restore --database latest --apply       # atau nama file dari --list
sudo $M restore --files --apply                 # pastikan file yang dirujuk tersedia
sudo systemctl start server-penyimpanan
```
Database saat ini selalu disimpan dulu sebagai `server.db.before-restore-<waktu>.bak`.
Catatan: file yang di-upload **setelah** salinan database itu dibuat tidak akan terdaftar
(file fisiknya tetap ada di SSD).

**SSD rusak total, diganti SSD baru:**
1. Format dan mount SSD baru di `/mnt/ssd` (bagian 3), dengan UUID baru di `/etc/fstab`.
2. `sudo ./scripts/install_pi.sh` (membuat ulang direktori; database baru yang kosong juga dibuat).
3. `sudo systemctl stop server-penyimpanan`
4. `sudo $M restore --database latest --apply` lalu `sudo $M restore --files --apply`
5. `sudo systemctl start server-penyimpanan`. Thumbnail dibuat ulang otomatis saat diminta.

## 11. Pemecahan masalah

| Gejala | Penyebab & solusi |
|---|---|
| Health `503`, `primary_storage: unavailable` | SSD tidak ter-mount. `findmnt /mnt/ssd`, `sudo mount -a`, cek kabel/daya. Server pulih sendiri dalam beberapa detik setelah SSD tersedia. |
| `backup_storage: unavailable` | HDD tidak ter-mount. Server utama tetap berjalan; backup ditolak sampai HDD kembali. |
| SSD baru ter-mount setelah boot (USB lambat) | Tidak perlu tindakan: server mendeteksi SSD/HDD yang di-mount belakangan dalam ±5 detik tanpa restart. |
| Layanan restart terus | `sudo journalctl -u server-penyimpanan -n 100`. Biasanya konfigurasi tidak valid (pesan jelas di log). |
| `Konfigurasi tidak valid` | Periksa nilai di `server.env`, mis. `SECRET_KEY` minimal 32 karakter, path harus absolut dan berada di bawah mount point. |
| Upload besar gagal di tengah | Klien cukup memanggil `GET /api/v1/uploads/{id}/status` dan mengirim chunk yang belum ada (resume). |
| Disk sering terputus / data rusak | Daya kurang. Pakai USB hub berdaya; cek `vcgencmd get_throttled`. |
| Thumbnail video tidak muncul | `ffmpeg` belum terpasang: `sudo apt install ffmpeg`, lalu restart layanan. |
| Lupa password admin | `sudo $M create-admin admin2`, lalu reset password user lama lewat `PATCH /api/v1/admin/users/{id}`. |

## 12. Keamanan layanan

Layanan systemd berjalan sebagai user `penyimpanan` dengan:
- Hanya bisa menulis ke `/mnt/ssd` dan `/mnt/hdd` (`ProtectSystem=strict`); sistem dan home
  directory read-only/tersembunyi.
- Tanpa capability Linux apa pun dan tanpa kemungkinan menaikkan hak akses (`NoNewPrivileges`).
- Batas RAM `MemoryMax=650M` agar Pi tetap responsif bila terjadi kebocoran memori.
- File baru dibuat dengan izin 600 (`UMask=0077`).

## 13. Melepas instalasi

```bash
sudo ./scripts/uninstall.sh
```
Yang dihapus hanya layanan dan kode di `/opt/server_penyimpanan`. Data di SSD/HDD, database,
dan konfigurasi tidak dihapus.

## 14. Uji deployment (untuk pengembang)

`tests/deploy/run_e2e.sh` menguji seluruh alur di container Debian Bookworm (Python 3.11) dengan
systemd sungguhan: instalasi, user non-root, upload/download, backup & restore, update, reboot,
HDD dicabut, SSD dicabut, SSD/HDD yang baru ter-mount setelah layanan start, hardening, dan
seluruh pytest. Container dibuat dengan propagasi mount `shared` seperti Raspberry Pi OS
(Docker defaultnya `private`, yang membuat mount baru tidak terlihat oleh layanan). Butuh Docker:
```bash
tests/deploy/run_e2e.sh
```
