# Build status — NOCTURNE 4.3.0

- Anime List menggunakan parser struktur heading dan link dari `https://otakudesu.blog/anime-list/`, lalu disajikan dalam daftar vertikal per kelompok.
- Genre menggunakan `https://otakudesu.blog/genre-list/`, disajikan sebagai chip inline berderet dengan filter lokal.
- Wallpaper dibuka di pratinjau fullscreen dalam aplikasi; tombol download memakai endpoint gambar terbatas ke Pinterest CDN dan validasi MIME/ukuran.
- Fitur 4.2 (Bintang Pinterest, pilihan resolusi/server streaming, Anime Quotes, ikon launcher, dan animasi splash) dipertahankan.
- Source checks: Python compile, parser/media unit tests, and inline JavaScript syntax checks.
- A complete Android Gradle build must still be confirmed by a successful GitHub Actions run or a local Android SDK build.
- Live OtakuDesu/Bintang API behavior depends on the user's network and upstream site. The build environment could not reach `otakudesu.blog`; no live success is claimed.
