#!/usr/bin/env python3
"""Offline unit checks for the new media response normalizers (no Flask install needed)."""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app/src/main/python/otakudesu_gothic.py"
SOURCE_TEXT = SOURCE.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE_TEXT, filename=str(SOURCE))


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def safe_http_url(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlparse(value)
    return value if parsed.scheme in ("http", "https") else None


NAMES = {
    "_https_url",
    "_nested_image_value",
    "_walk_dicts",
    "normalize_pinterest_results",
    "normalize_stream_groups",
}

namespace: dict[str, Any] = {
    "Any": Any,
    "re": re,
    "urlparse": urlparse,
    "clean_text": clean_text,
    "safe_http_url": safe_http_url,
    "_UNSAFE_MEDIA_TERMS": re.compile(
        r"\b(?:hentai|ecchi|porn|pornography|nsfw|r18|adult only|nude|naked|lewd|explicit)\b",
        re.IGNORECASE,
    ),
}
selected = [node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name in NAMES]
if {node.name for node in selected} != NAMES:
    raise SystemExit("One or more media helper functions are missing from the backend.")
module = ast.Module(body=selected, type_ignores=[])
exec(compile(ast.fix_missing_locations(module), str(SOURCE), "exec"), namespace)


class MediaNormalizerTests(unittest.TestCase):
    def test_pinterest_extracts_nested_image_and_source(self) -> None:
        payload = {
            "success": True,
            "data": [{
                "title": "Naruto wallpaper",
                "images": {"orig": {"url": "https://i.pinimg.com/originals/demo.jpg"}},
                "pin_url": "https://www.pinterest.com/pin/123/",
            }],
        }
        results = namespace["normalize_pinterest_results"](payload, 12)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://i.pinimg.com/originals/demo.jpg")
        self.assertEqual(results[0]["source_url"], "https://www.pinterest.com/pin/123/")

    def test_pinterest_drops_non_https_and_adult_labels(self) -> None:
        payload = {"data": [
            {"title": "Blocked label NSFW", "image_url": "https://i.pinimg.com/a.jpg"},
            {"title": "Naruto", "description": "adult only collection", "image_url": "https://i.pinimg.com/c.jpg"},
            {"title": "Naruto", "image_url": "http://i.pinimg.com/b.jpg"},
        ]}
        self.assertEqual(namespace["normalize_pinterest_results"](payload, 12), [])

    def test_stream_groups_keep_resolution_and_only_https_embeds(self) -> None:
        payload = {"streams": [{
            "resolution": "720p",
            "links": [
                {"name": "host-secure", "embed": "https://video.example/embed/abc"},
                {"name": "host-insecure", "embed": "http://video.example/embed/no"},
            ],
        }]}
        groups = namespace["normalize_stream_groups"](payload)
        self.assertEqual(groups, [{
            "resolution": "720p",
            "links": [{"name": "host-secure", "embed": "https://video.example/embed/abc"}],
        }])

    def test_expected_proxy_routes_exist(self) -> None:
        self.assertIn('"/api/search/pinterest"', SOURCE_TEXT)
        self.assertIn('"/api/nonton/otakudesu/stream"', SOURCE_TEXT)
        self.assertIn('"https://api.animechan.io/v1/quotes/random"', SOURCE_TEXT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
