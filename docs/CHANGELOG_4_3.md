# NOCTURNE 4.3.0

## Katalog OtakuDesu
- Anime List sekarang menggunakan `/anime-list/` dan disajikan sebagai daftar teks vertikal yang dikelompokkan menurut `#`, angka, dan huruf A–Z; tidak lagi memakai poster card.
- Parser lebih dulu membaca heading daftar dari halaman, lalu memiliki fallback pengelompokan judul berdasarkan karakter pertama jika markup berubah.
- Genre List memakai `/genre-list/` dan ditampilkan sebagai chip yang mengalir berderet/baris, tanpa poster.
- Filter judul anime dan filter genre bekerja lokal pada hasil yang sudah diterima, tanpa menambah request.
- Genre dewasa/eksplisit tidak ditampilkan pada layar kategori.

## Wallpaper
- Mengetuk wallpaper membuka pratinjau fullscreen di dalam aplikasi, bukan jendela WebView eksternal.
- Tombol download ditambahkan di kartu dan pratinjau. Endpoint unduhan hanya menerima host CDN gambar Pinterest HTTPS (`*.pinimg.com`), membatasi ukuran sampai 15 MB, serta memvalidasi tipe MIME gambar.
- Pratinjau bisa ditutup dengan tombol, ketuk area gelap, atau tombol Escape (perangkat dengan keyboard).

## Verifikasi
- Pemeriksaan kompilasi Python, tes unit parser/normalizer, dan pemeriksaan sintaks JavaScript inline dijalankan secara offline.
- Sumber OtakuDesu dan Bintang API tidak dapat diuji live dari lingkungan build saat ini; perlu tes konektivitas pada perangkat.
- APK belum diklaim berhasil dibuat sampai GitHub Actions menyelesaikan build.
