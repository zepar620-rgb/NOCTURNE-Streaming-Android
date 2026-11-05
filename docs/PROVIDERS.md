# Provider reference

NOCTURNE 4.2 uses these third-party catalogue and media services:

- Tenrai API (MyAnimeList-compatible metadata): https://api.tenrai.org/documentation
- AniList GraphQL: https://docs.anilist.co/guide/graphql/
- AniList rate limits: https://docs.anilist.co/guide/rate-limiting
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

The local route `/api/quotes/random` proxies Animechan's random quote endpoint. The UI displays a short excerpt with character and anime attribution. Animechan documentation currently states a free default limit of 100 requests per day; cache results and avoid repeated rapid refreshes. Check the provider's documentation before redistribution.

All providers are independent third parties. Their availability, terms, schemas, content policies, and rate limits may change. Verify live endpoints and usage terms before publishing an APK.
