# NOCTURNE 4.4.0 — Manga Reader & Live Network Monitor

## Fitur baru
- Manga Reader dengan pencarian gabungan MangaDex, MangaBaka, dan AniList.
- Detail katalog dan normalisasi metadata lintas penyedia; MangaDex menjadi sumber chapter dan halaman reader.
- Reader chapter dengan scroll vertikal, swipe kiri/kanan untuk berpindah halaman, navigasi keyboard, serta pilihan Original / Data saver.
- Kredit MangaDex, scanlation group, dan uploader ditampilkan pada tampilan pembaca.
- API Monitor baru: waktu probe dalam ms, HTTP status, payload bytes, counter kumulatif, throughput B/s, uptime, dan recent HTTP activity.
- Banner default diperbarui dengan komposisi eclipse/orbit gothic dan animasi shimmer.

## Catatan sumber dan privasi
- MangaBaka dan AniList adalah katalog/metadata, bukan penyedia file halaman chapter. Chapter reader saat ini menggunakan MangaDex.
- MangaDex image requests diproxy melalui backend lokal agar tidak mengandalkan hotlink WebView; host CDN divalidasi dan nama file dibatasi.
- Konten manga dibatasi ke content rating `safe` dan `suggestive`.
- Telemetri menghitung byte body HTTP yang diamati oleh Requests/Flask. Ini bukan pengukuran paket TCP/TLS atau trafik WebView langsung ke host eksternal.
- Penggunaan MangaDex harus mengikuti kebijakan mereka: kredit MangaDex dan scanlation group, hormati permintaan penghapusan konten, serta jangan memonetisasi akses MangaDex.

## API routes baru
- `GET /api/manga/search?q=&provider=`
- `GET /api/manga/detail?provider=&id=`
- `GET /api/manga/chapters?id=&lang=&offset=`
- `GET /api/manga/chapter/<chapter_uuid>?quality=data|data-saver`
- `GET /api/manga/page/<chapter_uuid>/<index>?quality=data|data-saver`
- `GET /api/manga/cover/<manga_uuid>/<filename>`
- `GET /api/telemetry`
