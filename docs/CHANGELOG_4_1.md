# NOCTURNE 4.1.0

- Add a GitHub Actions workflow to verify the project and build a debug APK.
- Add the separate **Tampilan Beranda** screen for the main home hero banner.
- Support animated GIF and MP4 home banners, stored as Blob data in IndexedDB (MP4 40 MB maximum, GIF 15 MB maximum).
- MP4 previews autoplay muted and loop; GIF animation is preserved.
- Allow muted MP4 media autoplay in the Android WebView settings.
- Keep profile banner independent from the main home banner.
- Update the Android WebView file chooser to honor requested MIME types, including MP4, GIF, images and JSON backups.
- Bump Android version to 4.1.0 / versionCode 410.
