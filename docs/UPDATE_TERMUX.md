# Update NOCTURNE 4.4.0 from Termux

ZIP source ini tidak menyertakan `.git`; riwayat, branch, dan kredensial Git lokal tetap utuh.

## 1. Siapkan Termux

```bash
pkg update -y
pkg install -y git unzip
termux-setup-storage
mkdir -p ~/projects
```

Simpan `NOCTURNE-Streaming-Android-4.4.0.zip` ke folder Download Android.

## 2. Perbarui checkout yang sudah ada

Jika repo belum pernah di-clone, clone sekali: 

```bash
git clone https://github.com/zepar620-rgb/NOCTURNE-Streaming-Android.git ~/projects/NOCTURNE-Streaming-Android
```

Lalu overlay file baru ke checkout (gunakan folder induk repo, bukan folder repo itu sendiri):

```bash
cd ~/projects/NOCTURNE-Streaming-Android
git pull --rebase origin main
unzip -o /sdcard/Download/NOCTURNE-Streaming-Android-4.4.0.zip -d ~/projects
git status
```

Jika checkout berada di folder lain, ganti `~/projects` pada perintah unzip dengan folder induk checkout. Periksa `git status` dan selesaikan konflik sebelum commit.

## 3. Commit dan push

```bash
cd ~/projects/NOCTURNE-Streaming-Android
git add -A
git commit -m "NOCTURNE 4.4: manga reader and live network monitor"
git push origin main
```

Jangan menaruh Personal Access Token langsung di perintah atau file source.

## 4. Build APK

Setelah push, buka GitHub → **Actions** → **Build NOCTURNE Android APK**. Pilih run paling baru yang berhasil dan unduh artifact **`NOCTURNE-4.4-debug-APK`**. Extract artifact ZIP untuk memperoleh `app-debug.apk`.

Build lokal memerlukan JDK 17, Android SDK Platform 36, Gradle 8.13, dan Python 3.11:

```bash
cd ~/projects/NOCTURNE-Streaming-Android
gradle --no-daemon --stacktrace :app:assembleDebug
```

APK lokal akan berada di `app/build/outputs/apk/debug/app-debug.apk`. Arsip source ZIP bukan APK installer.

## 5. Setelah memasang APK

Sebelum mengganti APK versi lama, ekspor data dari **Tools & Backup → Ekspor JSON**. Setelah instalasi, impor kembali jika diperlukan. Untuk unduhan wallpaper, gunakan tombol **Simpan** atau **Download wallpaper**; file gambar akan diproses melalui Android Download Manager.
