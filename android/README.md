# Aplikasi Android "Penyimpanan"

Klien Android untuk Server_penyimpanan (Kotlin + Jetpack Compose). Minimal Android 10 (API 29).

## Fitur
- **Login & sesi**: alamat server bisa IP lokal atau IP Tailscale; token disimpan terenkripsi (Android Keystore) dan diperbarui otomatis.
- **Berkas**: jelajahi folder, cari, urutkan, folder baru, ganti nama, pindah, salin, hapus ke trash, unduh ke `Download/Penyimpanan` (diverifikasi SHA-256), info file.
- **Upload besar di latar belakang**: upload per-chunk yang bisa dilanjutkan setelah koneksi putus atau HP restart; tetap jalan saat aplikasi ditutup (notifikasi progres). Bisa juga lewat menu **Bagikan → Penyimpanan** dari aplikasi lain.
- **Backup galeri otomatis**: foto (dan opsional video) baru diunggah ke folder "Backup HP"; bisa dibatasi hanya Wi-Fi / saat mengisi daya. File yang sudah ada di server tidak diunggah ulang.
- **Galeri**: grid foto/video per tanggal, penampil foto (geser, cubit/ketuk dua kali untuk zoom), pemutar video streaming (bisa di-seek).
- **Transfer**: daftar upload/unduhan beserta progres, batalkan, bersihkan yang selesai.
- **Akun**: penyimpanan terpakai, sesi login (akhiri sesi perangkat lain), riwayat aktivitas, ganti password, trash (pulihkan/hapus permanen), keluar / keluar semua perangkat.
- **Admin** (khusus admin): dashboard sistem (CPU, RAM, suhu, SSD/HDD, statistik), kelola pengguna, backup SSD → HDD, aktivitas semua pengguna.

## Cara instal
1. Buka halaman rilis **android-latest** di GitHub repo ini (`Releases`) dan unduh `Penyimpanan.apk`.
2. Di HP, izinkan "Instal aplikasi tidak dikenal" untuk browser/file manager, lalu buka APK-nya.
3. Buka aplikasi, isi alamat server (mis. `192.168.1.10:8000`), lalu login.

Versi baru bisa langsung dipasang menimpa versi lama (data login tetap ada) selama ditandatangani kunci yang sama.

## Build
APK resmi dibuat otomatis oleh GitHub Actions (`.github/workflows/android.yml`) setiap ada perubahan di `android/` pada branch `main`: menjalankan tes (termasuk tes integrasi ke backend Python asli), membangun APK release, lalu menerbitkannya ke rilis `android-latest`.

Build manual (butuh JDK 17 dan Android SDK 35):
```bash
cd android
./gradlew assembleRelease        # hasil: app/build/outputs/apk/release/app-release.apk
```

Tes:
```bash
./gradlew testDebugUnitTest                         # unit test saja
eval "$(scripts/test-server.sh start | sed 's/^/export /')"   # backend sementara (port 8099)
./gradlew testDebugUnitTest                         # + tes integrasi ke server asli
scripts/test-server.sh stop
```

### Build di Raspberry Pi (ARM64)
Android SDK resmi tidak menyediakan `aapt2` untuk ARM64. Untuk cek kompilasi di Pi dipakai `aapt2` dari paket Debian dengan pengaturan di `~/.gradle/gradle.properties` (bukan di repo):
```
android.aapt2FromMavenOverride=/home/pi5/android-sdk/aapt2-arm64/aapt2
penyimpanan.localArm64=true
```
Mode ini memakai compileSdk 34. APK untuk dipasang sebaiknya tetap dari GitHub Actions.

## Penandatanganan APK
- Tanpa pengaturan tambahan, APK ditandatangani `signing/debug.keystore` (kunci debug publik di repo) agar update bisa menimpa versi lama.
- **Risiko**: karena kunci itu publik, orang lain bisa membuat APK palsu yang bisa menimpa aplikasi Anda. Pasang APK hanya dari rilis repo ini. Untuk keamanan lebih, buat kunci rilis sendiri dan isi secrets GitHub `ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, `ANDROID_KEY_PASSWORD` (APK dengan kunci baru harus di-uninstall dulu sekali).

## Catatan keamanan
- Koneksi HTTP biasa (tanpa TLS) diizinkan karena server dipakai di jaringan rumah / Tailscale. Jangan buka port server langsung ke internet tanpa HTTPS.
