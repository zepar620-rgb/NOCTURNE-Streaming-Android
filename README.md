# ✠ NOCTURNE: Streaming Anime — Android Project 4.1

Project Android untuk aplikasi **NOCTURNE: Streaming Anime**. Aplikasi menjalankan UI web lokal melalui Android WebView dan backend Python/Flask yang dibundel memakai Chaquopy. Flask hanya bind ke `127.0.0.1:8765`, jadi backend tidak dibuka ke jaringan Wi-Fi/LAN.

## Perubahan 4.1

- Nama aplikasi launcher: **NOCTURNE: Streaming Anime**.
- Sambutan beranda: **Selamat datang di NOCTURNE**.
- Tema charcoal black / semi-abu-abu dengan aksen crimson yang lebih kalem.
- Banner beranda default dari file lokal `app/src/main/python/kolom/banner.svg`; dapat diganti melalui menu **Tampilan Beranda** dengan file MP4 atau GIF animasi.
- Banner beranda MP4/GIF disimpan lokal di IndexedDB, terpisah dari banner profil; video berjalan otomatis, loop, dan mute. Ukuran upload maksimal 40 MB untuk MP4 dan 15 MB untuk GIF.
- Android file picker mengikuti MIME type tiap input, jadi upload MP4/GIF, gambar profil, dan file backup dapat dipilih dengan tipe yang sesuai.
- Ikon launcher custom: `icon/res/drawable/ic_nocturne.xml`; file sumber desain ada di `icon/nocturne-icon.svg`.
- Login/register lokal menggunakan username dan password. Password disimpan sebagai hash PBKDF2-SHA256 + salt, bukan teks biasa.
- Foto profil, nama tampilan, bio, dan banner profil disimpan di local storage WebView. Banner profil hanya tampil di profil dan tidak mengubah hero Beranda.
- Tombol streaming bernama **PLAY**, dengan pemilihan server, reload dan popup player di dalam aplikasi.
- DownloadManager Android mengarahkan file ke folder publik `Download`.
- API/scraper Kitsu dan AniPub telah dihapus dari pencarian, detail, discovery dan API monitor.
- Fitur archive: koleksi anime, riwayat tonton, backup JSON, pemeriksaan endpoint, dan status provider.

## Folder aset yang bisa diganti

| Folder / file | Fungsi |
|---|---|
| `icon/nocturne-icon.svg` | Sumber desain ikon. Untuk mengganti launcher, perbarui VectorDrawable Android `icon/res/drawable/ic_nocturne.xml`. |
| `app/src/main/python/kolom/banner.svg` | Banner statis bawaan beranda. Dipakai saat belum ada MP4/GIF custom. |

Banner disajikan dari endpoint lokal `/assets/kolom/banner.svg`, tidak mengambil gambar dari URL eksternal. Rekomendasi gambar banner: lebar, rasio 16:5 atau 3:1; tempatkan fokus visual di sebelah kanan agar teks sambutan di kiri terbaca.

## Fitur akun lokal

1. Saat pertama kali dibuka, pilih **Buat akun** untuk mendaftarkan username dan password.
2. Login berikutnya memakai data akun yang sama di perangkat/browser WebView yang sama.
3. Buka menu **Profil Saya** untuk mengganti foto profil, banner profil, nama tampilan dan bio.
4. Buka menu **Tampilan Beranda** untuk memilih MP4/GIF animasi sebagai banner utama.
5. Tombol **Keluar** mengakhiri sesi tanpa menghapus akun.

Akun tidak memakai server, email, atau sinkronisasi cloud. Login tidak berpindah ke perangkat lain. Hapus data aplikasi/WebView atau uninstall dapat menghapus akun dan profil; tidak ada fitur reset password server-side. Foto dan banner diperkecil sebelum disimpan, tetapi kapasitas local storage Android WebView tetap terbatas.

## Streaming dan unduhan

Pilih episode lalu tekan **PLAY**. Jika embed di halaman tidak dapat tampil karena kebijakan X-Frame-Options/CSP dari server, tombol PLAY membuka URL player sebagai halaman utama di jendela WebView terpisah dalam aplikasi. Beberapa server mungkin tetap menolak WebView, meminta browser mereka sendiri, membatasi wilayah, atau tidak menyediakan embed aktif. Aplikasi tidak melewati DRM, login, atau proteksi anti-bot.

Unduhan yang diserahkan ke Android DownloadManager masuk ke folder `Download` bawaan perangkat. Tidak semua situs menyediakan file unduhan langsung; player streaming bukan fitur pengunduh video.

## Persyaratan build

- Android Studio yang mendukung Android Gradle Plugin 8.13.x, atau Gradle 8.13.
- JDK 17.
- Android SDK Platform 36 dan Build Tools 36.0.0.
- Python 3.11 (sesuai runtime Chaquopy pada project ini).
- Koneksi internet pada build pertama untuk mengunduh Gradle, Android Gradle Plugin, Chaquopy, Python runtime dan wheels.

Konfigurasi: AGP `8.13.2`, Chaquopy `17.2.0`, Gradle `8.13`, Python `3.11`, `minSdk 24`, `targetSdk 35`. Chaquopy 17.2 mendukung AGP 7.3–9.4 serta Python build 3.10–3.15; project ini konsisten memakai Python 3.11. Dokumentasi: https://chaquo.com/chaquopy/doc/current/android.html.

## Build otomatis melalui GitHub Actions

Workflow ada di `.github/workflows/build-apk.yml`. Setelah folder project di-push ke branch `main`, GitHub Actions otomatis menjalankan pemeriksaan sumber dan membangun APK debug. Kamu juga bisa menjalankannya manual melalui tab **Actions → Build NOCTURNE Android APK → Run workflow**. Hasil APK diunduh dari bagian **Artifacts** pada workflow run dengan nama `NOCTURNE-4.1-debug-APK`. Workflow menyiapkan JDK 17, Python 3.11, Android SDK Platform 36, dan Gradle 8.13.

## Upload ke GitHub dari Termux

Petunjuk lengkap termasuk setup Git, inisialisasi repo, autentikasi Personal Access Token, dan perintah push ada di [`docs/GITHUB_TERMUX.md`](docs/GITHUB_TERMUX.md). Ringkasnya, buat repo kosong di GitHub, ekstrak ZIP di Termux, lalu jalankan `git init -b main`, `git add .`, `git commit`, tambahkan remote HTTPS repo kamu, dan `git push -u origin main`. Setiap push ke `main` akan memulai build APK melalui GitHub Actions.

## Build dengan Android Studio

1. Ekstrak ZIP project.
2. Android Studio → **Open** → pilih folder yang berisi `settings.gradle`.
3. Atur Gradle JDK ke **JDK 17**.
4. Pastikan SDK Platform 36 tersedia melalui SDK Manager.
5. Tunggu Gradle Sync dan unduhan dependency selesai.
6. Pilih **Build → Build Bundle(s) / APK(s) → Build APK(s)**.
7. APK debug ada di `app/build/outputs/apk/debug/app-debug.apk`.

## Build melalui terminal

Setelah Gradle, JDK, Python, dan Android SDK tersedia serta `ANDROID_HOME` atau `ANDROID_SDK_ROOT` sudah diatur:

```bash
./build_apk.sh
```

Perintah build menggunakan `gradle --no-daemon --stacktrace assembleDebug`.

## Catatan Android

- Splash screen, launcher icon, status bar, navigation bar, dan popup player menggunakan tema native charcoal.
- Database SQLite disimpan di direktori privat aplikasi melalui bridge Chaquopy.
- `DownloadManager` menaruh unduhan yang didukung di folder `Download` publik.
- Untuk migrasi koleksi/riwayat dari instalasi lain, gunakan menu **Tools & Backup** dan ekspor/impor JSON.
- APK biner tidak disertakan dalam paket sumber ini. Jalankan build di atas untuk menghasilkan APK.

## Pemeriksaan

Jalankan tes backend dari folder project:

```bash
python docs/test_providers.py
```

Pemeriksaan ini mencakup normalisasi provider tersisa, agregasi multi-source, identitas aplikasi, banner bawaan, UI upload media MP4/GIF, penyimpanan terpisah dari profil, serta elemen login/profil/PLAY. Workflow juga memeriksa sintaks JavaScript inline sebelum Gradle build. Uji playback tetap perlu dilakukan di perangkat Android nyata karena tiap host video memiliki kebijakan dan dukungan WebView berbeda.
