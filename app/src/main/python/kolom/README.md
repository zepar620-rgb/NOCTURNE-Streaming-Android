# NOCTURNE home banner assets

- `banner.svg` is the bundled default home hero artwork. The app animates its light field, sigil and atmospheric overlay with CSS, so the home banner moves immediately after install without uploading a file or internet access.
- The default motion is part of the application source and is restored by choosing **Gunakan banner bawaan**.
- Optional custom `.mp4` and animated `.gif` home banners remain supported from the in-app **Tampilan Beranda** screen. MP4 plays muted, automatically and in a loop (maximum 40 MB); GIF animation is preserved (maximum 15 MB).
- Uploaded home media is stored in Android WebView IndexedDB and is independent from the profile banner.
