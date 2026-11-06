#!/usr/bin/env python3
"""Offline checks for MangaDex/MangaBaka/AniList helpers; Flask is not required."""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app/src/main/python/otakudesu_gothic.py"
SOURCE_TEXT = SOURCE.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE_TEXT, filename=str(SOURCE))


def clean_text(value: str | None) -> str:
    value = re.sub(r"<[^>]*>", " ", value or "")
    return re.sub(r"\s+", " ", value).strip()


NAMES = {
    "_manga_uuid", "_manga_https_url", "_manga_title", "_mangadex_cover", "_manga_normalize_mangadex",
    "_manga_search_mangadex", "_mangabaka_collection", "_mangabaka_cover",
    "_manga_search_mangabaka", "_anilist_manga_media", "_manga_search_anilist",
    "manga_search", "manga_detail", "manga_chapters", "manga_chapter_reader",
}
selected = [node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name in NAMES]
if {node.name for node in selected} != NAMES:
    raise SystemExit("One or more Manga integration helpers are missing from the backend.")

MANGADEX_ID = "123e4567-e89b-42d3-a456-426614174000"
CHAPTER_ID = "123e4567-e89b-42d3-a456-426614174001"
namespace: dict[str, Any] = {
    "Any": Any,
    "re": re,
    "urlparse": urlparse,
    "clean_text": clean_text,
    "ThreadPoolExecutor": ThreadPoolExecutor,
    "as_completed": as_completed,
    "MANGADEX_API": "https://api.mangadex.org",
    "MANGABAKA_API": "https://api.mangabaka.org",
    "MANGABAKA_SITE": "https://mangabaka.org",
    "MANGADEX_LANG_DEFAULT": ("id", "en"),
    "_MANGA_UUID_RE": re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"),
    "_MANGA_FILE_RE": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}\.(?:jpg|jpeg|png|webp|avif)$", re.I),
}
module = ast.Module(body=selected, type_ignores=[])
exec(compile(ast.fix_missing_locations(module), str(SOURCE), "exec"), namespace)

API_CALLS: list[tuple[str, str, dict[str, Any]]] = []
API_RESPONSES: dict[str, Any] = {}
ANILIST_CALLS: list[tuple[str, dict[str, Any]]] = []
ANILIST_RESPONSE: dict[str, Any] = {}


def fake_manga_api(provider: str, url: str, params: dict[str, Any] | None = None, ttl: int = 900) -> Any:
    API_CALLS.append((provider, url, params or {}))
    for key, value in API_RESPONSES.items():
        if key in url:
            return value
    raise AssertionError(f"Unexpected API request: {provider} {url} {params}")


def fake_anilist(query: str, variables: dict[str, Any], ttl: int = 900) -> dict[str, Any]:
    ANILIST_CALLS.append((query, variables))
    return ANILIST_RESPONSE


namespace["_manga_public_json"] = fake_manga_api
namespace["_anilist_graphql"] = fake_anilist


class MangaIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        API_CALLS.clear()
        API_RESPONSES.clear()
        ANILIST_CALLS.clear()
        ANILIST_RESPONSE.clear()

    def test_mangadex_uuid_rejects_path_injection(self) -> None:
        self.assertEqual(namespace["_manga_uuid"](MANGADEX_ID), MANGADEX_ID)
        with self.assertRaises(ValueError):
            namespace["_manga_uuid"](MANGADEX_ID + "/../../chapter")

    def test_external_catalogue_links_must_be_https(self) -> None:
        safe = namespace["_manga_https_url"]
        self.assertEqual(safe("https://anilist.co/manga/42"), "https://anilist.co/manga/42")
        self.assertEqual(safe("javascript:alert(1)"), "")
        self.assertEqual(safe("http://example.com"), "")

    def test_mangadex_metadata_and_cover_proxy_are_normalized(self) -> None:
        item = {
            "id": MANGADEX_ID,
            "attributes": {
                "title": {"en": "The Example"},
                "altTitles": [{"ja-ro": "Rei no Hon"}],
                "description": {"en": "A story about <b>change</b>."},
                "status": "ongoing", "year": 2024, "contentRating": "safe",
                "tags": [{"attributes": {"name": {"en": "Drama"}}}],
            },
            "relationships": [{"type": "cover_art", "attributes": {"fileName": "cover-demo.jpg"}}],
        }
        result = namespace["_manga_normalize_mangadex"](item)
        self.assertEqual(result["title"], "The Example")
        self.assertEqual(result["content_rating"], "safe")
        self.assertEqual(result["cover"], f"/api/manga/cover/{MANGADEX_ID}/cover-demo.jpg.256.jpg")
        self.assertEqual(result["url"], f"https://mangadex.org/title/{MANGADEX_ID}")
        self.assertIn("Drama", result["tags"])

    def test_mangabaka_v2_search_uses_documented_rating_filters(self) -> None:
        API_RESPONSES["/v2/series/search"] = {"data": [{
            "id": 1995, "title": {"en": "Frieren"},
            "main_cover": {"original": "https://cdn.example/frieren.jpg"},
            "description": "An elf's journey.", "status": "completed", "year": 2020,
        }]}
        results = namespace["_manga_search_mangabaka"]("Frieren")
        self.assertEqual(results[0]["id"], 1995)
        self.assertEqual(results[0]["title"], "Frieren")
        self.assertEqual(results[0]["cover"], "https://cdn.example/frieren.jpg")
        provider, url, params = API_CALLS[0]
        self.assertEqual(provider, "mangabaka")
        self.assertTrue(url.endswith("/v2/series/search"))
        self.assertEqual(params["content_rating"], ["safe", "suggestive"])
        self.assertEqual(params["type"], ["manga", "manhwa", "manhua"])

    def test_mangabaka_collection_handles_common_envelopes(self) -> None:
        self.assertEqual(namespace["_mangabaka_collection"]({"data": {"items": [1, 2]}}), [1, 2])
        self.assertEqual(namespace["_mangabaka_collection"]({"results": [3]}), [3])
        self.assertEqual(namespace["_mangabaka_collection"]({"data": "unexpected"}), [])

    def test_anilist_manga_search_filters_adult_titles(self) -> None:
        ANILIST_RESPONSE.update({"Page": {"media": [{
            "id": 42, "title": {"userPreferred": "Sample Manga", "english": "Sample"},
            "coverImage": {"large": "https://s4.anilist.co/file/sample.jpg"},
            "description": "An <i>adventure</i>.", "status": "RELEASING",
            "startDate": {"year": 2022}, "format": "MANGA", "chapters": 12,
            "averageScore": 86, "siteUrl": "https://anilist.co/manga/42",
        }]}})
        results = namespace["_manga_search_anilist"]("Sample Manga")
        self.assertEqual(results[0]["provider"], "anilist")
        self.assertEqual(results[0]["id"], 42)
        self.assertEqual(results[0]["title"], "Sample Manga")
        self.assertEqual(results[0]["description"], "An adventure .")
        self.assertIn("isAdult: false", ANILIST_CALLS[0][0])
        self.assertEqual(ANILIST_CALLS[0][1], {"search": "Sample Manga"})

    def test_mangabaka_detail_uses_full_schema(self) -> None:
        API_RESPONSES["/v2/series/1995"] = {"data": {
            "id": 1995, "title": "Frieren", "description": "Long description.",
            "url": "https://mangabaka.org/series/frieren", "authors": ["Kanehito Yamada"],
        }}
        result = namespace["manga_detail"]("mangabaka", "1995")
        self.assertEqual(result["title"], "Frieren")
        self.assertEqual(result["authors"], ["Kanehito Yamada"])
        self.assertEqual(API_CALLS[0][2], {"schema": "full"})

    def test_mangadex_chapter_feed_retains_language_group_and_uploader(self) -> None:
        API_RESPONSES["/feed"] = {"total": 1, "limit": 100, "data": [{
            "id": CHAPTER_ID,
            "attributes": {"chapter": "12", "volume": "2", "title": "The Door", "translatedLanguage": "id", "pages": 18, "publishAt": "2026-01-01T00:00:00+00:00"},
            "relationships": [
                {"type": "scanlation_group", "attributes": {"name": "Night Scan"}},
                {"type": "user", "attributes": {"username": "reader42"}},
            ],
        }]}
        result = namespace["manga_chapters"](MANGADEX_ID, ["id", "en"], 0)
        self.assertEqual(result["items"][0]["group"], "Night Scan")
        self.assertEqual(result["items"][0]["uploader"], "reader42")
        self.assertEqual(result["items"][0]["language"], "id")
        self.assertEqual(API_CALLS[0][2]["translatedLanguage[]"], ["id", "en"])

    def test_reader_returns_local_proxy_page_urls_and_credits(self) -> None:
        API_RESPONSES["/chapter/"] = {"data": {
            "id": CHAPTER_ID,
            "attributes": {"chapter": "1", "title": "First", "translatedLanguage": "en"},
            "relationships": [
                {"type": "manga", "attributes": {"title": {"en": "Sample Manga"}}},
                {"type": "scanlation_group", "attributes": {"name": "Example Group"}},
                {"type": "user", "attributes": {"username": "uploader"}},
            ],
        }}
        API_RESPONSES["/at-home/server/"] = {"baseUrl": "https://uploads.mangadex.org", "chapter": {
            "hash": "abcdefgh1234", "data": ["page-1.jpg", "page-2.jpg"], "dataSaver": ["small-1.jpg"],
        }}
        result = namespace["manga_chapter_reader"](CHAPTER_ID, "data")
        self.assertEqual(len(result["pages"]), 2)
        self.assertEqual(result["pages"][0], f"/api/manga/page/{CHAPTER_ID}/0?quality=data")
        self.assertEqual(result["group"], "Example Group")
        self.assertEqual(result["uploader"], "uploader")
        self.assertEqual(result["manga_title"], "Sample Manga")

    def test_requested_ui_and_api_routes_are_present(self) -> None:
        required = [
            'Manga Reader', '/api/manga/search', '/api/manga/detail', '/api/manga/chapters',
            '/api/manga/chapter/', '/api/manga/page/', '/api/manga/cover/', '/api/telemetry',
            'IntersectionObserver', 'touchstart', 'touchend', 'Original', 'Data saver',
            'last_upstream_ms', 'upstream_bps', 'app_tx_bps', 'app_rx_bps',
            'LIVE SAMPLING', 'HTTP round trip terakhir',
        ]
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, SOURCE_TEXT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
