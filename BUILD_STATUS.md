# Build status — NOCTURNE 4.4.0

- Manga Reader menggabungkan pencarian katalog MangaDex, MangaBaka v2, dan AniList GraphQL. MangaDex menyediakan chapter feed, at-home page metadata, proxy halaman, serta cover.
- Pembaca mendukung scroll vertikal, swipe horizontal untuk berpindah halaman, posisi halaman berbasis IntersectionObserver, dan Original / Data saver. Kredit scanlation group dan uploader tampil di reader.
- API Monitor menampilkan RTT/ms, HTTP status, response-body bytes, cumulative transfer, B/s sampling, uptime, dan event HTTP terbaru.
- Banner default diperbarui menjadi komposisi eclipse/orbit gothic dengan animasi shimmer dan orbit.
- Source checks: Python compile, inline JavaScript syntax, offline media integration tests, dan manga helper tests.
- Full Android Gradle build belum dijalankan pada lingkungan ini; hasil APK hanya dinyatakan berhasil setelah GitHub Actions / build lokal benar-benar sukses.
- Live provider behavior memerlukan koneksi internet dari perangkat pengguna dan tunduk pada rate limit / ketersediaan masing-masing API.
