# Provider reference

NOCTURNE 4.4 uses these third-party catalogue and media services:

- Tenrai API (MyAnimeList-compatible metadata): https://api.tenrai.org/documentation
- AniList GraphQL: https://docs.anilist.co/guide/graphql/
- AniList rate limits: https://docs.anilist.co/guide/rate-limiting
- MangaDex API: https://api.mangadex.org/docs/
- MangaDex acceptable use policy: https://mangadex.org/acceptable-use-policy
- MangaBaka API v2: https://api.mangabaka.org/docs
- MiteNime public API: https://mitenime.org/public-api
- AniMap: https://animap.id/
- Anime News Network Encyclopedia API: https://cdn.animenewsnetwork.com/encyclopedia/api.xml
- Bintang API Pinterest search: `GET https://bintangapi.my.id/api/search/pinterest?q=<query>`
- Bintang API OtakuDesu stream metadata: `GET https://bintangapi.my.id/api/nonton/otakudesu/stream?id=<episode-url>`
- Animechan quotes: `GET https://api.animechan.io/v1/quotes/random`; official docs: https://animechan.io/docs

## Wallpapers

The local route `/api/wallpapers?q=<character>` proxies Bintang API Pinterest search. Returned image URLs are accepted only over HTTPS; common adult-result labels are filtered before display. Pinterest is user-generated content, so this filter is a best-effort metadata check, not a guarantee that every source image has been reviewed. Always report the source link where available.

## Streaming

The local route `/api/stream?url=<otakudesu-episode-url>` proxies Bintang API's OtakuDesu stream response. It normalizes `streams[].resolution` and `streams[].links[].embed` into resolution/server selectors. Only HTTPS embed URLs are used. The download array is intentionally not exposed by the player. NOCTURNE does not scrape hidden media URLs, download video, or bypass DRM/login/anti-bot protections.

## Anime quotes

The local route `/api/quotes/random` proxies Animechan's random quote endpoint. The UI displays a short excerpt with character and anime attribution. Animechan documentation states a free default daily request limit; cache results and avoid repeated rapid refreshes. Check the provider's current documentation before redistribution.

## Manga catalogue and reader

The Manga Reader uses three catalogue sources:

- **MangaDex** provides title search/detail, translated chapter feeds, and MangaDex@Home page metadata. Routes used include `GET /manga`, `GET /manga/{id}/feed`, `GET /chapter/{chapterId}`, and `GET /at-home/server/{chapterId}` on `https://api.mangadex.org`. Chapter images and covers are fetched through the local Flask proxy after validating the MangaDex CDN host and filename.
- **MangaBaka API v2** provides catalogue search and detail through `GET https://api.mangabaka.org/v2/series/search` and `GET /v2/series/{id}?schema=full`. The implementation uses the v2 `type` and `content_rating` filters. Review the current API docs and licence before distributing or monetizing an application using this data; the current documentation describes personal, non-commercial usage and attribution to MangaBaka and upstream providers.
- **AniList GraphQL** provides manga catalogue metadata through `Page.media(type: MANGA)` and `Media(type: MANGA)` at `https://graphql.anilist.co`. AniList supplies metadata and external links, not chapter image files. Keep requests within its published rate limits and Terms of Use.

MangaBaka and AniList metadata can be searched against MangaDex to find a matching edition. This version uses MangaDex as the in-app chapter/page provider; it does not claim to load chapter files from MangaBaka or AniList. The reader filters content ratings to `safe` and `suggestive`, displays the MangaDex chapter source and available scanlation-group/uploader attribution, and uses a slower throttle for MangaDex @Home requests. Respect provider rules and takedown requests; MangaDex chapter access must not be monetized.

## Live API and traffic monitor

`/api/providers/status` probes provider health concurrently and reports round-trip time in milliseconds, HTTP status, and the latest response bytes observed for each upstream host. `/api/telemetry` reports counters and interval throughput for response-body bytes observed by Requests and Flask. It separates upstream-to-backend, backend-to-WebView, and WebView-request body bytes, and deliberately excludes its own polling requests. This is not a packet-level meter: TCP/TLS headers, Android radio overhead, and assets fetched directly by WebView from third-party hosts are outside its measurement boundary.

All providers are independent third parties. Their availability, terms, schemas, content policies, and rate limits may change. Verify live endpoints and usage terms before publishing an APK.
