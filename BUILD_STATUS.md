# Build status — NOCTURNE 4.1

- Added GitHub Actions build pipeline: `.github/workflows/build-apk.yml`.
- CI verifies Python syntax, runs offline provider/branding tests, checks inline JavaScript syntax, and runs `:app:assembleDebug`.
- CI toolchain: JDK 17, Python 3.11, Node 22, Android SDK Platform 36 / Build Tools 36.0.0, Gradle 8.13.
- Added separate home appearance settings for animated MP4/GIF banners; blobs are stored in IndexedDB.
- Profile banner is now independent and does not affect the home hero.
- Android file picker now uses each HTML input's MIME accept types.
- A complete Android build still needs to be confirmed by the first GitHub Actions run.

- Added `docs/GITHUB_TERMUX.md` with repository setup, PAT authentication guidance, push commands, and artifact download instructions.

- NOCTURNE 4.1.1: bundled animated home banner added (CSS motion over local banner artwork); no upload required for the default animation.
