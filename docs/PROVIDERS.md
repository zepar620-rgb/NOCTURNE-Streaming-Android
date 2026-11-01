# Provider reference

NOCTURNE 4.0 uses these third-party catalogue services:

- Tenrai API (MyAnimeList-compatible data): https://api.tenrai.org/documentation
- AniList GraphQL: https://docs.anilist.co/guide/graphql/
- AniList rate limits: https://docs.anilist.co/guide/rate-limiting
- AniList terms: https://docs.anilist.co/guide/terms-of-use
- MiteNime public API: https://mitenime.org/public-api
- AniMap: https://animap.id/
- Anime News Network Encyclopedia API: https://cdn.animenewsnetwork.com/encyclopedia/api.xml

Kitsu and AniPub integrations have been removed from the search, discovery,
detail, and provider-monitor flows. The remaining services are independent
third parties; their availability, terms, schemas and rate limits can change.
Check the provider's current documentation before redistributing the app.


## Anime wallpapers

- Komorebi public wallpaper API: https://github.com/amooo-ooo/komorebi
- Safe JSON endpoint used by NOCTURNE: `GET https://komorebi.orizuru.dev/image?rating=safe&format=json`
- The app proxies requests through `/api/wallpapers` to avoid relying on browser CORS. It validates HTTPS image URLs, requests only `rating=safe`, and shows provider errors instead of fake placeholder results.
- Provider availability is external and can change. Verify the live endpoint before release; no Waifu.im endpoint is used.
