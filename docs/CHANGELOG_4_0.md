# NOCTURNE 4.0 — Streaming Anime

## Branding and appearance
- App display name is now **NOCTURNE: Streaming Anime**.
- Welcome heading is **Selamat datang di NOCTURNE**.
- Main theme has been shifted to charcoal black / semi-grey with muted crimson accents.
- Added a custom local SVG home banner in `app/src/main/python/kolom/banner.svg`.
- Added an editable launcher-mark source in `icon/nocturne-icon.svg`; Android loads the VectorDrawable in `app/src/main/res/drawable/ic_nocturne.xml`.

## Account and profile
- Added local registration and login using username and password.
- Passwords are stored as PBKDF2-SHA256 hashes with random per-account salts, not plaintext.
- Login session, profile, biography, profile image and profile banner stay in the device WebView's local storage.
- Profile banner also becomes the home welcome banner. Images are resized before local storage.

## Streaming and downloads
- Streaming action is labelled **PLAY**; the player layout includes server selection, reload and an in-app top-level player window.
- Downloads received by Android DownloadManager are saved under the public `Download` folder.
- No-embed episodes still show a PLAY action for the source page. Playback remains dependent on the hosting provider and device WebView.

## Provider cleanup
- Removed Kitsu and AniPub API/scraper integrations from search, detail, discovery, mapping and API monitoring.
- Current multi-source catalogue providers: AniList, MyAnimeList-compatible Tenrai, MiteNime, AniMap, and Anime News Network.

## Additional archive features
- Personal anime collection and watch history remain available.
- JSON backup import/export, endpoint diagnostics and provider status monitor remain available.

## Limitations
- Accounts are device-local only; there is no cloud sync, account recovery, or multi-device login.
- Clearing app/WebView data can remove local accounts, session and profile images.
- Video sites may block embedding, require their own browser, or restrict playback by region/device. NOCTURNE does not bypass DRM or anti-bot protection.
