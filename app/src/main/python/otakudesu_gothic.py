#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OtakuDesu Gothic — katalog/scraper metadata dan pemutar embed sumber.

Catatan: aplikasi ini hanya membaca halaman publik dan menampilkan embed yang
memang dipasang oleh halaman episode. Tidak ada pengunduhan, ripping stream,
atau bypass DRM/login/anti-bot.
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import deque
from pathlib import Path
from datetime import datetime, timezone
import json
import logging
import os
import re
import hashlib
import io
import xml.etree.ElementTree as ET
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import quote, urljoin, urlparse, urlencode

import requests
from bs4 import BeautifulSoup, Tag
from flask import Flask, g, jsonify, render_template_string, request, send_file
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://otakudesu.blog"
SITE_HOST = "otakudesu.blog"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 18
CACHE_TTL = 120
# Jikan's former public instance was discontinued on 2026-10-01.
# Tenrai provides a Jikan-v4-compatible public catalogue API and is the default.
# You can override it for a private/self-hosted compatible API with environment vars.
MAL_API_BASE = os.environ.get("NOCTURNE_MAL_API_BASE", "https://api.tenrai.org/v1").strip().rstrip("/")
MAL_API_KEY = os.environ.get("NOCTURNE_MAL_API_KEY", "").strip()
MAL_CACHE_TTL = 1800
MAL_CONNECT_TIMEOUT = 4
MAL_READ_TIMEOUT = 9
MAL_MIN_INTERVAL = 0.55  # Stay below Tenrai public tier limit of 120 requests/minute.
_mal_lock = threading.Lock()
_mal_last_request = 0.0

_mal_session = requests.Session()
_mal_adapter = HTTPAdapter(
    max_retries=Retry(total=0, connect=0, read=0, status=0, redirect=0, allowed_methods=frozenset(("GET",)))
)
_mal_session.mount("https://", _mal_adapter)
_mal_session.headers.update({
    "User-Agent": "NOCTURNE/4.0 (personal anime archive)",
    "Accept": "application/json",
})

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="[%(asctime)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("otakudesu-gothic")

app = Flask(__name__, static_folder=str(Path(__file__).resolve().parent / "kolom"), static_url_path="/assets/kolom")
APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("NOCTURNE_DB", str(APP_DIR / "nocturne.db")))


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS library (
            id INTEGER PRIMARY KEY AUTOINCREMENT, mal_id INTEGER UNIQUE,
            title TEXT NOT NULL, image TEXT, mal_url TEXT, status TEXT NOT NULL DEFAULT 'Plan to Watch',
            score INTEGER, notes TEXT DEFAULT '', updated_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, episode_url TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL, anime_title TEXT DEFAULT '', image TEXT DEFAULT '',
            watched_at TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0)""")


init_db()


def library_rows():
    with db_connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM library ORDER BY updated_at DESC")]


_session = requests.Session()
_retry = Retry(
    total=2,
    connect=2,
    read=1,
    backoff_factor=0.45,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(("GET",)),
    raise_on_status=False,
)
_adapter = HTTPAdapter(max_retries=_retry, pool_connections=8, pool_maxsize=8)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)
_session.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.7,en;q=0.6",
        "Connection": "keep-alive",
    }
)

_api_session = requests.Session()
_api_adapter = HTTPAdapter(max_retries=Retry(total=0, connect=0, read=0, status=0, redirect=0, allowed_methods=frozenset(("GET", "POST"))))
_api_session.mount("https://", _api_adapter)
_api_session.mount("http://", _api_adapter)
_api_session.headers.update({"User-Agent": "NOCTURNE/4.4 (NOCTURNE Android; personal archive)", "Accept": "application/json, application/xml;q=0.9, text/xml;q=0.8"})

# Lightweight in-process traffic telemetry. Counts response-body bytes observed by
# Requests and Flask, not lower-level TCP/TLS framing or Android radio overhead.
_telemetry_lock = threading.Lock()
_telemetry_started = time.monotonic()
_telemetry = {
    "upstream_bytes": 0, "upstream_requests": 0,
    "app_tx_bytes": 0, "app_rx_bytes": 0, "app_requests": 0,
    "last_upstream_ms": None, "last_http_event": None,
}
_telemetry_events = deque(maxlen=24)
_telemetry_snapshot = {"at": time.monotonic(), "upstream_bytes": 0, "app_tx_bytes": 0, "app_rx_bytes": 0}


def _telemetry_response_hook(response, *args, **kwargs):
    try:
        body_bytes = len(response.content or b"")
    except Exception:
        body_bytes = int(response.headers.get("Content-Length", 0) or 0)
    try:
        elapsed_ms = round(response.elapsed.total_seconds() * 1000, 1)
    except Exception:
        elapsed_ms = None
    parsed = urlparse(response.url)
    event = {
        "host": parsed.hostname or "unknown", "status": int(response.status_code),
        "bytes": max(0, body_bytes), "elapsed_ms": elapsed_ms,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with _telemetry_lock:
        _telemetry["upstream_bytes"] += event["bytes"]
        _telemetry["upstream_requests"] += 1
        _telemetry["last_upstream_ms"] = elapsed_ms
        _telemetry["last_http_event"] = event
        _telemetry_events.appendleft(event)
    return response


for _tracked_session in (_session, _api_session, _mal_session):
    _tracked_session.hooks.setdefault("response", []).append(_telemetry_response_hook)


@app.before_request
def _telemetry_request_start():
    if request.path != "/api/telemetry":
        g.nocturne_request_started = time.monotonic()
        g.nocturne_request_rx = int(request.content_length or 0)


@app.after_request
def _telemetry_request_finish(response):
    if request.path == "/api/telemetry":
        return response
    # Prefer the declared body length; calculating it can consume a streamed/file response.
    try:
        tx_bytes = max(0, int(response.headers.get("Content-Length", 0) or 0))
    except (TypeError, ValueError):
        tx_bytes = 0
    elapsed_ms = None
    started = getattr(g, "nocturne_request_started", None)
    if started is not None:
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    with _telemetry_lock:
        _telemetry["app_tx_bytes"] += max(0, tx_bytes)
        _telemetry["app_rx_bytes"] += max(0, int(getattr(g, "nocturne_request_rx", 0)))
        _telemetry["app_requests"] += 1
        _telemetry_events.appendleft({
            "host": "NOCTURNE / WebView", "status": int(response.status_code),
            "bytes": max(0, tx_bytes), "elapsed_ms": elapsed_ms,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
    return response


def telemetry_snapshot() -> dict[str, Any]:
    global _telemetry_snapshot
    now = time.monotonic()
    with _telemetry_lock:
        current = {k: int(_telemetry[k]) for k in ("upstream_bytes", "app_tx_bytes", "app_rx_bytes", "upstream_requests", "app_requests")}
        previous = dict(_telemetry_snapshot)
        duration = max(0.25, now - float(previous.get("at", now)))
        rates = {
            "upstream_bps": max(0, round((current["upstream_bytes"] - int(previous.get("upstream_bytes", 0))) / duration)),
            "app_tx_bps": max(0, round((current["app_tx_bytes"] - int(previous.get("app_tx_bytes", 0))) / duration)),
            "app_rx_bps": max(0, round((current["app_rx_bytes"] - int(previous.get("app_rx_bytes", 0))) / duration)),
        }
        _telemetry_snapshot = {**current, "at": now}
        events = list(_telemetry_events)[:12]
        latest = dict(_telemetry.get("last_http_event") or {})
        last_ms = _telemetry.get("last_upstream_ms")
    return {
        "ok": True, **current, **rates,
        "uptime_s": round(now - _telemetry_started, 1),
        "last_upstream_ms": last_ms, "last_http_event": latest,
        "events": events,
        "measurement": "HTTP response-body bytes; excludes TCP/TLS overhead and direct WebView traffic outside NOCTURNE proxy routes",
    }

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def cache_get(key: str) -> Any | None:
    with _cache_lock:
        item = _cache.get(key)
        if not item:
            return None
        expires, value = item
        if expires < time.time():
            _cache.pop(key, None)
            return None
        return value


def cache_set(key: str, value: Any, ttl: int = CACHE_TTL) -> None:
    with _cache_lock:
        _cache[key] = (time.time() + ttl, value)


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def safe_http_url(url: str | None) -> str | None:
    """Accept HTTP(S) only. Used for displayed remote assets/embeds."""
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    return url


def site_url(url: str) -> str:
    """Normalize and constrain scraper requests to otakudesu.blog."""
    absolute = urljoin(BASE_URL + "/", url)
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in (SITE_HOST, "www." + SITE_HOST):
        raise ValueError("URL harus berasal dari otakudesu.blog")
    return absolute


def fetch_html(url: str, *, use_cache: bool = True) -> tuple[str, int, str]:
    url = site_url(url)
    cached = cache_get("html:" + url) if use_cache else None
    if cached is not None:
        return cached
    try:
        response = _session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host not in (SITE_HOST, "www." + SITE_HOST):
            raise RuntimeError("Situs mengalihkan permintaan ke host lain; permintaan dihentikan.")
        result = (response.text, response.status_code, response.url)
        if response.status_code == 200 and use_cache:
            cache_set("html:" + url, result)
        return result
    except requests.RequestException as exc:
        raise RuntimeError(f"Gagal menghubungi situs: {exc}") from exc


def get_soup(url: str, *, use_cache: bool = True) -> tuple[BeautifulSoup, str]:
    html, status, final_url = fetch_html(url, use_cache=use_cache)
    if status == 403:
        raise RuntimeError("HTTP 403: server menolak permintaan dari koneksi ini. Aplikasi tidak mencoba membypass proteksi situs.")
    if status >= 400:
        raise RuntimeError(f"Server mengembalikan HTTP {status} untuk {final_url}")
    return BeautifulSoup(html, "html.parser"), final_url


def get_img_url(img: Tag | None) -> str | None:
    """Read real poster URLs from common lazy-loading attributes used by anime sites."""
    if not isinstance(img, Tag):
        return None
    # Lazy attributes must be checked before src: src is often a 1x1 placeholder.
    keys = (
        "data-src", "data-lazy-src", "data-lazy", "data-original",
        "data-cfsrc", "data-url", "data-srcset", "data-lazy-srcset",
        "srcset", "src",
    )
    for key in keys:
        raw = img.get(key)
        if not raw:
            continue
        if "srcset" in key:
            candidates = [part.strip().split()[0] for part in str(raw).split(",") if part.strip()]
            raw = candidates[-1] if candidates else ""
        candidate = safe_http_url(urljoin(BASE_URL + "/", str(raw)))
        if candidate and not candidate.startswith("data:"):
            # Ignore known transparent/blank placeholders so later attributes can win.
            if re.search(r"(?:blank|placeholder|transparent|1x1)\.(?:gif|png|jpe?g|webp)(?:[?#]|$)", candidate, re.I):
                continue
            return candidate
    # Some themes put the poster in a CSS background instead of an <img>.
    style = str(img.get("style", ""))
    match = re.search(r"background(?:-image)?\s*:[^;]*url\([\"']?([^\"')]+)", style, re.I)
    if match:
        candidate = safe_http_url(urljoin(BASE_URL + "/", match.group(1).strip()))
        if candidate:
            return candidate
    return None


def page_title(soup: BeautifulSoup) -> str:
    for selector in ("h1.entry-title", "h1.post-title", "main h1", "article h1", "h1"):
        node = soup.select_one(selector)
        if node:
            value = clean_text(node.get_text(" ", strip=True))
            if value:
                return value
    title = soup.title.get_text(" ", strip=True) if soup.title else "OtakuDesu"
    return re.sub(r"\s*[|–-]\s*Otaku\s*Desu.*$", "", clean_text(title), flags=re.I)


def best_image(soup: BeautifulSoup, scope: Tag | BeautifulSoup | None = None) -> str | None:
    og = soup.select_one('meta[property="og:image"], meta[name="twitter:image"]')
    if og and og.get("content"):
        candidate = safe_http_url(urljoin(BASE_URL + "/", str(og.get("content"))))
        if candidate:
            return candidate
    root = scope or soup.select_one("main, article, .venutama, .venser, .chivsrc") or soup
    selectors = [
        ".fotoanime img", ".thumb img", ".thumbz img", ".detpost img",
        "article img", "main img", "img",
    ]
    for selector in selectors:
        for img in root.select(selector):
            alt = clean_text(img.get("alt", ""))
            src = get_img_url(img)
            if src and not re.search(r"logo|avatar|banner|icon|ads?", alt, re.I):
                return src
    return None


def card_text_container(anchor: Tag) -> Tag:
    """Find a card wrapper that contains both title and, where possible, its poster."""
    parent: Tag = anchor
    best: Tag = anchor.parent if isinstance(anchor.parent, Tag) else anchor
    for _ in range(9):
        if not isinstance(parent.parent, Tag):
            break
        parent = parent.parent
        classes = " ".join(parent.get("class", [])).lower()
        has_image = bool(parent.find("img") or parent.select_one("picture source[srcset]"))
        looks_like_card = parent.name in ("article", "li") or any(
            token in classes for token in ("venz", "detpost", "thumb", "item", "card", "anime", "bsx", "listupd")
        )
        if has_image:
            return parent
        if looks_like_card:
            best = parent
    return best


def parse_anime_cards(soup: BeautifulSoup, *, limit: int = 100) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    anchors = soup.select('a[href*="/anime/"]')
    for anchor in anchors:
        href = safe_http_url(urljoin(BASE_URL + "/", str(anchor.get("href", ""))))
        if not href:
            continue
        try:
            href = site_url(href)
        except ValueError:
            continue
        text = clean_text(anchor.get_text(" ", strip=True))
        container = card_text_container(anchor)
        if not text:
            img = container.find("img")
            text = clean_text(img.get("alt", "")) if img else ""
        if not text:
            continue
        # Avoid generic category / navigation labels.
        if text.lower() in {"anime", "selengkapnya", "detail", "read more"}:
            continue
        image = None
        # Poster and title links are frequently siblings, so inspect the selected
        # wrapper and then progressively wider ancestors instead of only 4 levels.
        image_scopes: list[Tag] = [container]
        ancestor: Tag = container
        for _ in range(5):
            if not isinstance(ancestor.parent, Tag):
                break
            ancestor = ancestor.parent
            image_scopes.append(ancestor)
        seen_images: set[int] = set()
        for scope in image_scopes:
            for img in scope.select("img"):
                marker = id(img)
                if marker in seen_images:
                    continue
                seen_images.add(marker)
                image = get_img_url(img)
                if image:
                    break
            if image:
                break
            for source in scope.select("picture source[srcset]"):
                raw = str(source.get("data-srcset") or source.get("srcset") or "")
                if raw:
                    candidate = safe_http_url(urljoin(BASE_URL + "/", raw.split(",")[-1].strip().split()[0]))
                    if candidate:
                        image = candidate
                        break
            if image:
                break
        date_text = ""
        for selector in (".set", ".date", "time", ".epz", ".ttl"):
            node = container.select_one(selector)
            if node:
                date_text = clean_text(node.get_text(" ", strip=True))
                if date_text:
                    break
        if href not in found:
            found[href] = {"title": text, "url": href, "image": image, "subtitle": date_text}
        else:
            if not found[href].get("image") and image:
                found[href]["image"] = image
            if len(text) > len(found[href]["title"]):
                found[href]["title"] = text
        if len(found) >= limit:
            break
    return list(found.values())


def parse_episode_links(soup: BeautifulSoup) -> list[dict[str, str]]:
    results: dict[str, dict[str, str]] = {}
    for anchor in soup.select('a[href*="/episode/"]'):
        href = safe_http_url(urljoin(BASE_URL + "/", str(anchor.get("href", ""))))
        if not href:
            continue
        try:
            href = site_url(href)
        except ValueError:
            continue
        title = clean_text(anchor.get_text(" ", strip=True)) or clean_text(anchor.get("title", ""))
        if title:
            results[href] = {"title": title, "url": href}
    return list(results.values())


def parse_genres(soup: BeautifulSoup) -> list[dict[str, str]]:
    data: dict[str, dict[str, str]] = {}
    for anchor in soup.select('a[href*="/genres/"]'):
        href = safe_http_url(urljoin(BASE_URL + "/", str(anchor.get("href", ""))))
        title = clean_text(anchor.get_text(" ", strip=True))
        if href and title:
            data[href] = {"title": title, "url": href}
    return list(data.values())


def parse_home() -> dict[str, Any]:
    soup, final_url = get_soup(BASE_URL + "/")
    items = parse_anime_cards(soup, limit=80)
    if not items:
        # Some site versions link the latest episode first, while the anime card
        # is lower in the page. This fallback is still limited to the same site.
        ongoing, _ = get_soup(BASE_URL + "/ongoing-anime/")
        items = parse_anime_cards(ongoing, limit=60)
    return {"title": page_title(soup), "items": items, "source": final_url}


def _anime_group_key(label: str) -> str | None:
    value = clean_text(label).upper()
    if value == "#" or re.fullmatch(r"[0-9]", value) or re.fullmatch(r"[A-Z]", value):
        return value
    return None


def _group_anime_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group plain anime links by their leading symbol/number/letter."""
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for item in items:
        title = clean_text(str(item.get("title") or ""))
        if not title:
            continue
        first = title[0].upper()
        key = first if first.isalnum() and (first.isdigit() or "A" <= first <= "Z") else "#"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)
    # Match the source's common order: symbols, 0–9, then A–Z.
    order.sort(key=lambda x: (0, x) if x == "#" else (1, int(x)) if x.isdigit() else (2, x))
    return [{"label": key, "items": groups[key]} for key in order]


def parse_anime_list_groups(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Read the site's alphabetic heading/list structure instead of scraping poster cards."""
    groups: list[dict[str, Any]] = []
    seen: set[str] = set()
    headings = soup.find_all(["h2", "h3", "h4"])
    for heading in headings:
        key = _anime_group_key(heading.get_text(" ", strip=True))
        if not key:
            continue
        group_items: list[dict[str, Any]] = []
        for node in heading.find_all_next(["h2", "h3", "h4", "a"]):
            if node.name in ("h2", "h3", "h4"):
                break
            href = safe_http_url(urljoin(BASE_URL + "/", str(node.get("href", ""))))
            title = clean_text(node.get_text(" ", strip=True))
            if not href or not title or _UNSAFE_MEDIA_TERMS.search(title):
                continue
            try:
                href = site_url(href)
            except ValueError:
                continue
            if "/anime/" not in urlparse(href).path or href in seen:
                continue
            seen.add(href)
            group_items.append({"title": title, "url": href})
        if group_items:
            groups.append({"label": key, "items": group_items})

    # If the theme changes and removes alphabet headings, preserve the full list
    # as vertical groups based on each title's first character.
    if not groups:
        flat: list[dict[str, Any]] = []
        for anchor in soup.select('a[href*="/anime/"]'):
            href = safe_http_url(urljoin(BASE_URL + "/", str(anchor.get("href", ""))))
            title = clean_text(anchor.get_text(" ", strip=True))
            if not href or not title or _UNSAFE_MEDIA_TERMS.search(title):
                continue
            try:
                href = site_url(href)
            except ValueError:
                continue
            if "/anime/" in urlparse(href).path and href not in seen:
                seen.add(href)
                flat.append({"title": title, "url": href})
        groups = _group_anime_items(flat)
    return groups


def parse_catalog(section: str, page: int = 1) -> dict[str, Any]:
    page = max(1, min(int(page), 100))
    if section == "ongoing":
        path = "/ongoing-anime/" if page == 1 else f"/ongoing-anime/page/{page}/"
        soup, final_url = get_soup(urljoin(BASE_URL, path))
        items = parse_anime_cards(soup, limit=100)
        return {"title": page_title(soup), "items": items, "page": page, "source": final_url}
    if section == "anime-list":
        soup, final_url = get_soup(BASE_URL + "/anime-list/")
        groups = parse_anime_list_groups(soup)
        items = [item for group in groups for item in group["items"]]
        return {"title": page_title(soup), "groups": groups, "items": items,
                "total": len(items), "page": 1, "source": final_url}
    raise ValueError("Katalog tidak dikenal")


def parse_schedule() -> dict[str, Any]:
    soup, final_url = get_soup(BASE_URL + "/jadwal-rilis")
    days = ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu", "Random")
    groups: list[dict[str, Any]] = []
    for heading in soup.find_all(["h2", "h3", "h4"]):
        name = clean_text(heading.get_text(" ", strip=True))
        if name not in days:
            continue
        links: list[dict[str, str]] = []
        for sibling in heading.next_elements:
            if sibling is not heading and isinstance(sibling, Tag) and sibling.name in ("h2", "h3", "h4"):
                break
            if isinstance(sibling, Tag) and sibling.name == "a":
                href = safe_http_url(urljoin(BASE_URL + "/", str(sibling.get("href", ""))))
                title = clean_text(sibling.get_text(" ", strip=True))
                if href and title and "/anime/" in urlparse(href).path:
                    try:
                        site_url(href)
                        links.append({"title": title, "url": href})
                    except ValueError:
                        pass
        unique = {x["url"]: x for x in links}
        if unique:
            groups.append({"day": name, "items": list(unique.values())})
    if not groups:
        # Fallback: present all discovered anime links in a single schedule bucket.
        cards = parse_anime_cards(soup, limit=200)
        if cards:
            groups.append({"day": "Jadwal dari halaman sumber", "items": [{"title": x["title"], "url": x["url"]} for x in cards]})
    return {"title": page_title(soup), "groups": groups, "source": final_url}


def parse_genre_list() -> dict[str, Any]:
    soup, final_url = get_soup(BASE_URL + "/genre-list/")
    genres = [g for g in parse_genres(soup)
              if not re.search(r"\b(?:ecchi|hentai|adult|r18)\b|18\+", g["title"], re.I)]
    return {"title": page_title(soup), "genres": genres, "source": final_url}


def parse_search(query: str) -> dict[str, Any]:
    q = clean_text(query)[:100]
    if not q:
        return {"query": q, "items": []}
    url = BASE_URL + "/?s=" + quote(q)
    try:
        soup, final_url = get_soup(url)
        items = parse_anime_cards(soup, limit=80)
        if items:
            return {"query": q, "items": items, "source": final_url, "method": "pencarian situs"}
    except Exception as exc:
        log.info("Search form unavailable: %s", exc)

    # Fallback that still works when the site's search route returns a block:
    # scan the public alphabetic listing and filter titles locally.
    soup, final_url = get_soup(BASE_URL + "/anime-list/")
    all_items = parse_anime_cards(soup, limit=1000)
    needle = q.casefold()
    items = [x for x in all_items if needle in x["title"].casefold()][:80]
    return {"query": q, "items": items, "source": final_url, "method": "filter daftar anime"}


def parse_anime_detail(url: str) -> dict[str, Any]:
    soup, final_url = get_soup(site_url(url))
    title = page_title(soup)
    cover = best_image(soup)
    meta: dict[str, str] = {}
    main = soup.select_one(".venutama, .venser, .detpost, article, main") or soup
    for node in main.select("p, li, tr, .infozingle"):
        line = clean_text(node.get_text(" ", strip=True))
        if ":" in line and len(line) < 250:
            key, value = line.split(":", 1)
            key = clean_text(key).strip(" •-–")
            value = clean_text(value)
            if key and value and len(key) < 45:
                meta.setdefault(key, value)
    genres = parse_genres(main)
    episodes = parse_episode_links(main)
    description = ""
    for selector in (".sinopsis", ".desc", ".entry-content", ".venutama", "article"):
        node = soup.select_one(selector)
        if node:
            paragraphs = [clean_text(p.get_text(" ", strip=True)) for p in node.select("p")]
            description = " ".join(p for p in paragraphs if len(p) > 40)[:1200]
            if description:
                break
    if not episodes:
        episodes = parse_episode_links(soup)
    return {
        "title": title,
        "url": final_url,
        "image": cover,
        "meta": meta,
        "genres": genres,
        "episodes": episodes,
        "description": description,
    }


def parse_episode_detail(url: str) -> dict[str, Any]:
    soup, final_url = get_soup(site_url(url))
    title = page_title(soup)
    embeds: list[str] = []
    for frame in soup.find_all("iframe"):
        raw = frame.get("src") or frame.get("data-src") or frame.get("data-lazy-src")
        candidate = safe_http_url(urljoin(final_url, str(raw or "")))
        if candidate and candidate not in embeds:
            embeds.append(candidate)
    # On this site's current layout the stream iframe may also be exposed as
    # an anchor labelled "iframe" by the site HTML/link markup.
    for anchor in soup.find_all("a", href=True):
        label = clean_text(anchor.get_text(" ", strip=True)).lower()
        if label in {"iframe", "stream", "streaming", "player"}:
            candidate = safe_http_url(urljoin(final_url, str(anchor.get("href"))))
            if candidate and candidate not in embeds:
                embeds.append(candidate)
    episode_nav = parse_episode_links(soup)
    previous = None
    next_episode = None
    for anchor in soup.find_all("a", href=True):
        label = clean_text(anchor.get_text(" ", strip=True)).lower()
        candidate = safe_http_url(urljoin(final_url, str(anchor.get("href"))))
        if not candidate:
            continue
        try:
            candidate = site_url(candidate)
        except ValueError:
            continue
        if "previous" in label or "sebelumnya" in label:
            previous = {"title": clean_text(anchor.get_text(" ", strip=True)), "url": candidate}
        elif "next" in label or "berikutnya" in label:
            next_episode = {"title": clean_text(anchor.get_text(" ", strip=True)), "url": candidate}
    # Keep the page's source URL as a usable fallback when embed extraction is
    # blocked or the publisher switches player providers.
    return {
        "title": title,
        "url": final_url,
        "embeds": embeds,
        "episodes": episode_nav,
        "previous": previous,
        "next": next_episode,
        "image": best_image(soup),
    }


def mal_api_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fetch public MAL catalogue data through the configured Jikan-compatible API.

    The default provider is Tenrai. Requests are serialized, cached, and given
    short connect/read timeouts so a provider outage cannot leave the UI spinning
    for minutes. No automatic retry is performed for 429/5xx responses.
    """
    global _mal_last_request
    from urllib.parse import urlencode

    base = MAL_API_BASE
    parsed_base = urlparse(base)
    if parsed_base.scheme != "https" or not parsed_base.netloc:
        raise RuntimeError("NOCTURNE_MAL_API_BASE harus berupa URL HTTPS yang valid.")

    clean_path = path.lstrip("/")
    if not clean_path or ".." in clean_path.split("/"):
        raise ValueError("Path API MAL tidak valid.")

    query = urlencode(params or {}, doseq=True)
    cache_url = base + "/" + clean_path + ("?" + query if query else "")
    cached = cache_get("mal:" + cache_url)
    if cached is not None:
        return cached

    headers = {}
    if MAL_API_KEY:
        # Keep optional provider key server-side; never render it into HTML.
        headers["X-Server-Key"] = MAL_API_KEY

    with _mal_lock:
        wait = MAL_MIN_INTERVAL - (time.monotonic() - _mal_last_request)
        if wait > 0:
            time.sleep(wait)
        _mal_last_request = time.monotonic()
        try:
            response = _mal_session.get(
                base + "/" + clean_path,
                params=params or {},
                headers=headers,
                timeout=(MAL_CONNECT_TIMEOUT, MAL_READ_TIMEOUT),
            )
        except requests.Timeout as exc:
            raise RuntimeError(
                f"Koneksi katalog MAL ({urlparse(base).hostname}) timeout "
                f"setelah {MAL_CONNECT_TIMEOUT + MAL_READ_TIMEOUT} detik. "
                "Cek internet atau coba lagi nanti."
            ) from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"Tidak dapat menghubungi katalog MAL: {exc}") from exc

    if response.status_code == 429:
        retry = response.headers.get("Retry-After", "").strip()
        if retry and retry.replace(".", "", 1).isdigit():
            retry_hint = f"Tunggu sekitar {retry} detik sebelum mencoba lagi."
        elif retry:
            retry_hint = f"Ikuti nilai Retry-After dari server ({retry}) sebelum mencoba lagi."
        else:
            retry_hint = "Tunggu beberapa saat sebelum mencoba lagi."
        raise RuntimeError(f"Provider MAL membatasi request (HTTP 429). {retry_hint}")
    if response.status_code == 403:
        raise RuntimeError(
            "Provider MAL menolak koneksi (HTTP 403). Ini bisa berupa blokir sementara atau pembatasan jaringan."
        )
    if response.status_code == 404:
        raise RuntimeError("Endpoint atau data MAL tidak ditemukan (HTTP 404).")
    if response.status_code in (502, 503, 504):
        raise RuntimeError(
            f"Provider katalog MAL sedang tidak tersedia (HTTP {response.status_code}). "
            "Coba lagi beberapa saat; NOCTURNE sudah menghentikan request agar tidak menggantung."
        )
    if response.status_code >= 400:
        body_hint = clean_text(response.text[:180])
        raise RuntimeError(f"Provider MAL mengembalikan HTTP {response.status_code}. {body_hint}")

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Provider MAL mengirim respons JSON yang tidak valid.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Format respons provider MAL tidak dikenali.")

    cache_set("mal:" + cache_url, data, MAL_CACHE_TTL)
    return data


def mal_search(query: str, limit: int = 12) -> list[dict[str, Any]]:
    query = clean_text(query)[:64]
    if len(query) < 2:
        raise ValueError("Masukkan minimal 2 karakter untuk pencarian MyAnimeList.")
    payload = mal_api_get("anime", {"q": query, "limit": max(1, min(limit, 20)), "sfw": "true"})
    results = []
    for item in payload.get("data", []):
        images = item.get("images") or {}
        jpg = images.get("jpg") or images.get("webp") or {}
        titles = item.get("titles") or []
        results.append({
            "mal_id": item.get("mal_id"), "title": item.get("title") or "Untitled",
            "title_english": item.get("title_english"), "title_japanese": item.get("title_japanese"),
            "image": jpg.get("large_image_url") or jpg.get("image_url"),
            "score": item.get("score"), "year": item.get("year"), "type": item.get("type"),
            "episodes": item.get("episodes"), "url": item.get("url"),
        })
    return results


def mal_anime_detail(mal_id: int) -> dict[str, Any]:
    if not 1 <= mal_id <= 99999999:
        raise ValueError("MAL ID tidak valid.")
    payload = mal_api_get(f"anime/{mal_id}/full")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Detail anime tidak ditemukan di MyAnimeList.")
    return data


def mal_anime_extra(mal_id: int, kind: str) -> list[dict[str, Any]] | dict[str, Any]:
    allowed = {"characters", "staff", "recommendations", "reviews", "statistics", "relations"}
    if kind not in allowed:
        raise ValueError("Jenis informasi MAL tidak dikenal.")
    payload = mal_api_get(f"anime/{mal_id}/{kind}", {"limit": 10} if kind in {"reviews", "recommendations"} else None)
    return payload.get("data", [])



# ---------------------------------------------------------------------------
# Multi-source anime metadata integrations (read-only public catalogue data)
# ---------------------------------------------------------------------------
ANILIST_URL = "https://graphql.anilist.co"
ANILIST_ENABLED = os.environ.get("NOCTURNE_ENABLE_ANILIST", "1").strip().lower() not in {"0", "false", "no", "off"}
MITENIME_BASE = "https://mitenime.org/api"
ANIMAP_BASE = "https://animap.id"
ANN_BASE = "https://cdn.animenewsnetwork.com/encyclopedia"

_PROVIDER_INTERVALS = {
    "anilist": 2.15,   # docs currently report a degraded 30 req/min ceiling
    "mitenime": 3.1,   # public docs: 20 requests/min/IP
    "animap": 0.35,
    "ann": 1.1,
    "tenrai": MAL_MIN_INTERVAL,
    "mangadex": 0.24,
    "mangabaka": 0.45,
}
_provider_locks: dict[str, threading.Lock] = {}
_provider_last: dict[str, float] = {}
_provider_guard = threading.Lock()


def _provider_lock(name: str) -> threading.Lock:
    with _provider_guard:
        if name not in _provider_locks:
            _provider_locks[name] = threading.Lock()
        return _provider_locks[name]


def _external_json(provider: str, url: str, params: dict[str, Any] | None = None, ttl: int = 1800) -> Any:
    'GET a fixed first-party/public API endpoint with cache, throttling and timeouts.'
    key_material = json.dumps([provider, url, params or {}], sort_keys=True, ensure_ascii=False)
    cache_key = "ext:" + hashlib.sha256(key_material.encode("utf-8")).hexdigest()
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    lock = _provider_lock(provider)
    with lock:
        wait = _PROVIDER_INTERVALS.get(provider, 0.8) - (time.monotonic() - _provider_last.get(provider, 0.0))
        if wait > 0:
            time.sleep(wait)
        _provider_last[provider] = time.monotonic()
        try:
            response = _api_session.get(url, params=params or {}, timeout=(4, 8), allow_redirects=True)
        except requests.Timeout as exc:
            raise RuntimeError(f"{provider}: koneksi timeout (maks. 12 detik).") from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"{provider}: koneksi gagal: {exc}") from exc
    if response.status_code == 429:
        retry = response.headers.get("Retry-After", "beberapa saat")
        raise RuntimeError(f"{provider}: rate limit HTTP 429. Tunggu {retry} sebelum mencoba lagi.")
    if response.status_code in (403, 451):
        raise RuntimeError(f"{provider}: akses ditolak HTTP {response.status_code}; aplikasi tidak membypass pembatasan.")
    if response.status_code >= 400:
        raise RuntimeError(f"{provider}: server mengembalikan HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{provider}: respons bukan JSON yang valid.") from exc
    cache_set(cache_key, payload, ttl)
    return payload


def _anilist_graphql(query: str, variables: dict[str, Any], ttl: int = 1800) -> dict[str, Any]:
    'AniList GraphQL public read API. Calls are serialized and cached.'
    if not ANILIST_ENABLED:
        raise RuntimeError('Integrasi AniList dinonaktifkan melalui NOCTURNE_ENABLE_ANILIST=0.')
    key_material = json.dumps([query, variables], sort_keys=True, ensure_ascii=False)
    cache_key = "anilist:" + hashlib.sha256(key_material.encode("utf-8")).hexdigest()
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    lock = _provider_lock("anilist")
    with lock:
        wait = _PROVIDER_INTERVALS["anilist"] - (time.monotonic() - _provider_last.get("anilist", 0.0))
        if wait > 0:
            time.sleep(wait)
        _provider_last["anilist"] = time.monotonic()
        try:
            response = _api_session.post(
                ANILIST_URL,
                json={"query": query, "variables": variables},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=(4, 9),
            )
        except requests.Timeout as exc:
            raise RuntimeError("AniList: GraphQL timeout (maks. 13 detik).") from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"AniList: koneksi gagal: {exc}") from exc
    if response.status_code == 429:
        raise RuntimeError("AniList: HTTP 429, rate limit aktif. Tunggu sekitar satu menit.")
    if response.status_code >= 400:
        raise RuntimeError(f"AniList: HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("AniList: respons GraphQL bukan JSON valid.") from exc
    if payload.get("errors"):
        message = clean_text(str((payload["errors"][0] or {}).get("message", "GraphQL error")))
        raise RuntimeError("AniList: " + message[:220])
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("AniList: struktur data tidak dikenali.")
    if ttl > 0:
        cache_set(cache_key, data, ttl)
    return data


def _first_list(payload: Any) -> list[dict[str, Any]]:
    'Normalize common JSON API list containers while keeping only objects.'
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        # Search APIs sometimes return a single record instead of an array.
        if any(k in payload for k in ("Name", "canonicalTitle", "title", "slug")) and not any(k in payload for k in ("data", "results", "items", "anime", "series", "result")):
            return [payload]
        for key in ("data", "results", "items", "anime", "series", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
            if isinstance(value, dict):
                nested = _first_list(value)
                if nested:
                    return nested
    return []


def _anilist_search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    gql = '''query ($search: String, $perPage: Int) {
      Page(page: 1, perPage: $perPage) {
        media(search: $search, type: ANIME, sort: SEARCH_MATCH, isAdult: false) {
          id idMal siteUrl format status episodes seasonYear averageScore popularity
          title { romaji english native }
          coverImage { large }
          genres
          studios(isMain: true) { nodes { name } }
        }
      }
    }'''
    data = _anilist_graphql(gql, {"search": clean_text(query)[:80], "perPage": max(1, min(limit, 15))})
    media = ((data.get("Page") or {}).get("media") or [])
    out = []
    for x in media:
        title = (x.get("title") or {})
        out.append({
            "provider": "AniList", "provider_key": "anilist", "provider_id": x.get("id"),
            "title": title.get("english") or title.get("romaji") or title.get("native") or "Untitled",
            "image": (x.get("coverImage") or {}).get("large"),
            "score": x.get("averageScore"), "year": x.get("seasonYear"),
            "subtitle": f"{x.get('format') or 'Anime'} · {x.get('status') or 'Unknown'} · {x.get('episodes') or '?'} eps",
            "url": x.get("siteUrl"), "mal_id": x.get("idMal"),
            "genres": x.get("genres") or [], "studios": [s.get("name") for s in (x.get("studios") or {}).get("nodes", [])],
        })
    return out


def _mitenime_search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    payload = _external_json("mitenime", MITENIME_BASE + "/search", {"q": clean_text(query)[:80]}, ttl=1200)
    out = []
    for x in _first_list(payload)[:limit]:
        attrs = x.get("attributes") if isinstance(x.get("attributes"), dict) else x
        ident = x.get("slug") or attrs.get("slug") or x.get("id") or attrs.get("id")
        title = attrs.get("title") or attrs.get("name") or x.get("title") or x.get("name") or "Untitled"
        image = attrs.get("poster") or attrs.get("image") or attrs.get("cover") or attrs.get("poster_url")
        if isinstance(image, dict): image = image.get("large") or image.get("url")
        url = ("https://mitenime.org/series/" + str(ident)) if ident else "https://mitenime.org/"
        out.append({
            "provider": "MiteNime", "provider_key": "mitenime", "provider_id": ident,
            "title": title, "image": image, "score": attrs.get("rating") or attrs.get("score"),
            "year": attrs.get("year") or attrs.get("released"),
            "subtitle": f"{attrs.get('type') or 'Series'} · {attrs.get('status') or 'Catalog'}",
            "url": url, "synopsis": attrs.get("synopsis") or attrs.get("description"),
        })
    return out


def _animap_search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    payload = _external_json("animap", ANIMAP_BASE + "/api/search", {"q": clean_text(query)[:80], "limit": max(1, min(limit, 20))}, ttl=3600)
    out = []
    for x in _first_list(payload)[:limit]:
        titles = x.get("titles") or []
        title = (titles[0] if titles else None) or x.get("title") or "Untitled"
        malids = x.get("mal_id")
        if not isinstance(malids, list): malids = [malids] if malids else []
        anilistids = x.get("anilist_id")
        if not isinstance(anilistids, list): anilistids = [anilistids] if anilistids else []
        out.append({
            "provider": "AniMap", "provider_key": "animap", "provider_id": malids[0] if malids else (anilistids[0] if anilistids else None),
            "title": title, "image": None, "score": None, "year": x.get("year"),
            "subtitle": "Cross-reference · MAL " + (str(malids[0]) if malids else "—") + " · AniList " + (str(anilistids[0]) if anilistids else "—"),
            "url": "https://animap.id/", "mapping": {"mal_id": malids, "anilist_id": anilistids, "ann_id": x.get("ann_id") or [], "anidb_id": x.get("anidb_id") or []},
        })
    return out


def _ann_search(query: str, limit: int = 8) -> list[dict[str, Any]]:
    xml_text = _external_text("ann", ANN_BASE + "/api.xml", {"title": "~" + clean_text(query)[:70]})
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise RuntimeError("Anime News Network: XML tidak valid.") from exc
    out = []
    for node in root.iter():
        if node.tag.lower().split("}")[-1] != "anime":
            continue
        ident = node.attrib.get("id") or node.attrib.get("annid")
        title = node.attrib.get("name") or ""
        synopsis = ""
        for child in node.iter():
            tag = child.tag.lower().split("}")[-1]
            if tag == "info":
                typ = (child.attrib.get("type") or "").lower()
                if typ in {"main title", "alternative title"} and clean_text(child.text or ""):
                    title = title or clean_text(child.text)
                if typ == "synopsis": synopsis = clean_text(child.text or "")
            elif tag in {"name", "title"} and not title:
                title = clean_text(child.text or "")
        if ident and title:
            out.append({"provider": "Anime News Network", "provider_key": "ann", "provider_id": ident,
                        "title": title, "image": None, "score": None, "year": None,
                        "subtitle": "Encyclopedia record", "url": f"https://www.animenewsnetwork.com/encyclopedia/anime.php?id={quote(str(ident))}",
                        "synopsis": synopsis})
        if len(out) >= limit: break
    return out


def _external_text(provider: str, url: str, params: dict[str, Any] | None = None, ttl: int = 3600) -> str:
    key_material = json.dumps([provider, url, params or {}], sort_keys=True, ensure_ascii=False)
    cache_key = "exttxt:" + hashlib.sha256(key_material.encode("utf-8")).hexdigest()
    cached = cache_get(cache_key)
    if cached is not None: return cached
    lock = _provider_lock(provider)
    with lock:
        wait = _PROVIDER_INTERVALS.get(provider, 0.8) - (time.monotonic() - _provider_last.get(provider, 0.0))
        if wait > 0: time.sleep(wait)
        _provider_last[provider] = time.monotonic()
        try:
            response = _api_session.get(url, params=params or {}, timeout=(4, 8), headers={"Accept": "application/xml,text/xml,*/*"})
        except requests.RequestException as exc:
            raise RuntimeError(f"{provider}: koneksi gagal: {exc}") from exc
    if response.status_code == 429: raise RuntimeError(f"{provider}: rate limit HTTP 429.")
    if response.status_code >= 400: raise RuntimeError(f"{provider}: HTTP {response.status_code}.")
    text = response.text
    cache_set(cache_key, text, ttl)
    return text


def multiverse_search(query: str) -> dict[str, Any]:
    query = clean_text(query)[:80]
    if len(query) < 2:
        raise ValueError("Masukkan minimal dua karakter untuk pencarian multi-sumber.")
    tasks = {
        "AniList": lambda: _anilist_search(query, 8),
        "MiteNime": lambda: _mitenime_search(query, 8),
        "AniMap": lambda: _animap_search(query, 8),
        "Anime News Network": lambda: _ann_search(query, 6),
        "MyAnimeList / Tenrai": lambda: mal_search(query, 8),
    }
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {pool.submit(fn): name for name, fn in tasks.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                rows = future.result()
                if name == "MyAnimeList / Tenrai":
                    for row in rows:
                        row.update({"provider": name, "provider_key": "tenrai", "provider_id": row.get("mal_id"), "subtitle": f"MAL · {row.get('type') or 'Anime'} · {row.get('year') or '?'}"})
                results[name] = {"ok": True, "items": rows, "count": len(rows)}
            except Exception as exc:
                results[name] = {"ok": False, "items": [], "count": 0, "error": str(exc)[:240]}
    total = sum(x["count"] for x in results.values())
    return {"query": query, "providers": results, "total": total, "provider_count": sum(1 for x in results.values() if x["ok"])}


def _anilist_detail(media_id: int) -> dict[str, Any]:
    gql = '''query ($id: Int) { Media(id: $id, type: ANIME) {
      id idMal siteUrl format status episodes duration season seasonYear averageScore meanScore popularity favourites
      description(asHtml: false) startDate { year month day } endDate { year month day }
      title { romaji english native } coverImage { extraLarge large } bannerImage genres tags { name rank }
      studios(isMain: true) { nodes { name siteUrl } }
      characters(perPage: 18, sort: ROLE) { edges { role node { id name { full } image { large } } voiceActors(language: JAPANESE, sort: RELEVANCE) { id name { full } image { large } } } }
      staff(perPage: 18, sort: RELEVANCE) { edges { role node { id name { full } } } }
      recommendations(perPage: 10, sort: RATING) { nodes { mediaRecommendation { id title { romaji english } coverImage { large } averageScore siteUrl } } }
    } }'''
    data = _anilist_graphql(gql, {"id": int(media_id)})
    media = data.get("Media")
    if not media: raise RuntimeError("Anime tidak ditemukan pada AniList.")
    return media


def multiverse_detail(provider: str, ident: str) -> dict[str, Any]:
    provider = provider.lower().strip()
    if not ident or len(ident) > 100: raise ValueError("ID provider tidak valid.")
    if provider == "anilist":
        return {"provider": "AniList", "data": _anilist_detail(int(ident))}
    if provider == "mitenime":
        payload = _external_json("mitenime", MITENIME_BASE + "/series/" + quote(ident, safe=""), ttl=1800)
        return {"provider": "MiteNime", "data": payload.get("data", payload)}
    if provider == "animap":
        payload = _external_json("animap", ANIMAP_BASE + "/api/v1/map/mal/" + quote(ident, safe=""), ttl=3600)
        return {"provider": "AniMap cross-reference", "data": payload}
    if provider == "tenrai":
        return {"provider": "MyAnimeList / Tenrai", "data": mal_anime_detail(int(ident))}
    if provider == "ann":
        xml_text = _external_text("ann", ANN_BASE + "/api.xml", {"anime": ident})
        try: root = ET.fromstring(xml_text)
        except ET.ParseError as exc: raise RuntimeError("ANN: XML tidak valid.") from exc
        return {"provider": "Anime News Network", "data": {"id": ident, "xml": ET.tostring(root, encoding="unicode")[:18000], "url": f"https://www.animenewsnetwork.com/encyclopedia/anime.php?id={quote(ident)}"}}
    raise ValueError("Provider detail tidak dikenal.")


def multiverse_discover() -> dict[str, Any]:
    """Limited discovery feed from AniList and MyAnimeList catalogues."""
    gql = '''query { Page(page: 1, perPage: 18) { media(type: ANIME, sort: TRENDING_DESC, isAdult: false) {
      id idMal siteUrl format status episodes seasonYear averageScore title { romaji english } coverImage { large }
    } } }'''
    output: dict[str, Any] = {"trending": [], "mal_top": [], "errors": {}}
    jobs = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs[pool.submit(_anilist_graphql, gql, {}, 900)] = "anilist"
        jobs[pool.submit(mal_api_get, "top/anime", {"page": 1, "limit": 18, "filter": "bypopularity"})] = "tenrai"
        for future in as_completed(jobs):
            source = jobs[future]
            try:
                payload = future.result()
                if source == "anilist":
                    for x in ((payload.get("Page") or {}).get("media") or []):
                        title = x.get("title") or {}
                        output["trending"].append({"provider": "AniList", "provider_key": "anilist", "provider_id": x.get("id"),
                            "mal_id": x.get("idMal"), "title": title.get("english") or title.get("romaji") or "Untitled",
                            "image": (x.get("coverImage") or {}).get("large"), "score": x.get("averageScore"),
                            "subtitle": f"TRENDING · {x.get('format') or 'Anime'} · {x.get('seasonYear') or ''}", "url": x.get("siteUrl")})
                elif source == "tenrai":
                    for x in payload.get("data", [])[:18]:
                        im = ((x.get("images") or {}).get("jpg") or {}).get("large_image_url") or ((x.get("images") or {}).get("jpg") or {}).get("image_url")
                        output["mal_top"].append({"provider": "MyAnimeList / Tenrai", "provider_key": "tenrai", "provider_id": x.get("mal_id"), "title": x.get("title") or "Untitled", "image": im, "score": x.get("score"), "subtitle": f"MAL POPULARITY · #{x.get('popularity') or '—'}", "url": x.get("url")})
            except Exception as exc:
                output["errors"][source] = str(exc)[:220]
    return output


def provider_status() -> list[dict[str, Any]]:
    probes = {
        "Tenrai / MAL": lambda: mal_api_get("anime", {"q": "Cowboy Bebop", "limit": 1, "sfw": "true"}),
        "AniList": lambda: _anilist_graphql("query { Page(page:1,perPage:1){ media(type:ANIME,sort:POPULARITY_DESC){ id } } }", {}, ttl=300),
        "MiteNime": lambda: _external_json("mitenime", MITENIME_BASE + "/search", {"q": "Cowboy Bebop"}, 300),
        "AniMap": lambda: _external_json("animap", ANIMAP_BASE + "/api/search", {"q": "Cowboy Bebop", "limit": 1}, 300),
        "Anime News Network": lambda: _external_text("ann", ANN_BASE + "/api.xml", {"title": "~Cowboy Bebop"}, 300),
        "MangaDex API": lambda: _manga_public_json("mangadex", MANGADEX_API + "/manga", {"limit": 1, "contentRating[]": ["safe"]}, ttl=0),
        "MangaBaka API": lambda: _manga_public_json("mangabaka", MANGABAKA_API + "/v2/series/search", {"q": "One Piece", "type": ["manga", "manhwa", "manhua"], "content_rating": ["safe"], "limit": 1}, ttl=0),
        "AniList Manga GraphQL": lambda: _anilist_graphql("query { Page(page:1,perPage:1){ media(type:MANGA,sort:POPULARITY_DESC){ id } } }", {}, ttl=0),
    }
    host_map = {
        "Tenrai / MAL": urlparse(MAL_API_BASE).hostname,
        "AniList": urlparse(ANILIST_URL).hostname,
        "AniList Manga GraphQL": urlparse(ANILIST_URL).hostname,
        "MiteNime": urlparse(MITENIME_BASE).hostname,
        "AniMap": urlparse(ANIMAP_BASE).hostname,
        "Anime News Network": urlparse(ANN_BASE).hostname,
        "MangaDex API": urlparse(MANGADEX_API).hostname,
        "MangaBaka API": urlparse(MANGABAKA_API).hostname,
    }
    output = []
    with ThreadPoolExecutor(max_workers=len(probes)) as pool:
        starts = {name: time.monotonic() for name in probes}
        futures = {pool.submit(fn): name for name, fn in probes.items()}
        for future in as_completed(futures):
            name = futures[future]
            elapsed = int((time.monotonic() - starts[name]) * 1000)
            try:
                future.result()
                ok, error = True, None
            except Exception as exc:
                ok, error = False, str(exc)[:220]
            host = host_map.get(name)
            with _telemetry_lock:
                event = next((item for item in _telemetry_events if host and item.get("host") == host), {})
            output.append({"name": name, "ok": ok, "status": "ONLINE" if ok else "OFFLINE / ERROR",
                           "status_code": event.get("status"), "bytes": event.get("bytes", 0),
                           "elapsed_ms": elapsed, "error": error})
    return sorted(output, key=lambda item: item["name"])


def diagnose_endpoint(path: str, label: str) -> dict[str, Any]:
    url = urljoin(BASE_URL + "/", path.lstrip("/"))
    started = time.time()
    try:
        _, status, final_url = fetch_html(url, use_cache=False)
        return {
            "label": label,
            "path": path,
            "status": status,
            "ok": status == 200,
            "final_url": final_url,
            "elapsed_ms": int((time.time() - started) * 1000),
            "note": "OK" if status == 200 else ("Diblokir oleh server" if status == 403 else f"HTTP {status}"),
        }
    except Exception as exc:
        return {
            "label": label,
            "path": path,
            "status": None,
            "ok": False,
            "final_url": url,
            "elapsed_ms": int((time.time() - started) * 1000),
            "note": str(exc)[:180],
        }


def run_diagnostics() -> list[dict[str, Any]]:
    # Ini adalah halaman publik / jalur yang teramati, bukan daftar API internal.
    probes = [
        ("Beranda", "/"),
        ("Daftar anime", "/anime-list/"),
        ("Jadwal rilis", "/jadwal-rilis"),
        ("Anime ongoing", "/ongoing-anime/"),
        ("Genre list", "/genre-list/"),
        ("Genre Action", "/genres/action/"),
        ("Detail anime contoh", "/anime/kikansha-mahou-tokubetsu-s2-sub-indo/"),
        ("Halaman episode contoh", "/episode/kmt-s2-episode-4-sub-indo/"),
        ("Pencarian WordPress", "/?s=naruto"),
        ("WordPress REST (opsional)", "/wp-json/"),
    ]
    output: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(diagnose_endpoint, path, label) for label, path in probes]
        for future in as_completed(futures):
            output.append(future.result())
    order = {path: i for i, (_, path) in enumerate(probes)}
    output.sort(key=lambda item: order.get(item["path"], 999))
    return output


@app.get("/")
def index():
    return render_template_string(PAGE)


@app.get("/api/health")
def api_health():
    return jsonify({"ok": True, "site": BASE_URL, "app": "NOCTURNE: streaming anime", "mal_provider": urlparse(MAL_API_BASE).hostname, "anilist_enabled": ANILIST_ENABLED})


# Public, keyless anime-wallpaper catalogue. The provider documents a safe-only
# JSON endpoint; requests are proxied server-side to avoid browser CORS issues.

# External media APIs are proxied here so the Android WebView does not depend on CORS.
# All providers are called over HTTPS with bounded timeouts; no DRM or anti-bot bypass is used.
BINTANG_API_BASE = "https://bintangapi.my.id"
BINTANG_TIMEOUT = (6, 20)
_bintang_headers = {
    "User-Agent": "NOCTURNE-Android/4.4 (personal anime archive)",
    "Accept": "application/json",
}
_wallpaper_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_wallpaper_cache_lock = threading.Lock()
_stream_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_stream_cache_lock = threading.Lock()
_quote_cache: tuple[float, dict[str, Any]] | None = None
_quote_cache_lock = threading.Lock()

# Pinterest search results are public user-generated content. Keep searches and
# returned labels away from adult/explicit categories for this teen-facing app.
_UNSAFE_MEDIA_TERMS = re.compile(
    r"(?:\b(?:hentai|ecchi|porn|pornography|nsfw|r18|adult only|nude|naked|lewd|explicit)\b|18\+)",
    re.IGNORECASE,
)


def _bintang_get(endpoint: str, params: dict[str, Any]) -> Any:
    response = requests.get(
        BINTANG_API_BASE + endpoint,
        params=params,
        headers=_bintang_headers,
        timeout=BINTANG_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict):
        if payload.get("success") is False or str(payload.get("status", "")).lower() in {"error", "failed"}:
            raise RuntimeError(clean_text(str(payload.get("message") or payload.get("error") or "Bintang API menolak permintaan."))[:180])
    return payload


def _https_url(value: Any) -> str | None:
    candidate = safe_http_url(str(value or ""))
    if not candidate:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    return candidate


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _nested_image_value(value: Any) -> str | None:
    if isinstance(value, str):
        return _https_url(value)
    if isinstance(value, dict):
        for key in ("url", "src", "original", "orig", "large", "medium", "image_url", "736x", "474x", "1200x"):
            candidate = _nested_image_value(value.get(key))
            if candidate:
                return candidate
    if isinstance(value, list):
        for child in value:
            candidate = _nested_image_value(child)
            if candidate:
                return candidate
    return None


def normalize_pinterest_results(payload: Any, limit: int = 12) -> list[dict[str, Any]]:
    """Accept common Pinterest scraper shapes and expose a small safe image list."""
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    image_keys = ("image_url", "imageUrl", "image", "img", "src", "thumbnail", "thumb", "display_url")
    source_keys = ("pin_url", "pinUrl", "source_url", "sourceUrl", "link", "pin", "permalink")

    for row in _walk_dicts(payload):
        metadata_parts = []
        for key in ("title", "name", "alt", "description", "tags", "tag", "category", "type"):
            value = row.get(key)
            if isinstance(value, (list, tuple, set)):
                metadata_parts.extend(str(item) for item in value)
            elif value is not None:
                metadata_parts.append(str(value))
        metadata_text = clean_text(" ".join(metadata_parts))
        title = clean_text(str(row.get("title") or row.get("name") or row.get("alt") or row.get("description") or ""))[:160]
        if _UNSAFE_MEDIA_TERMS.search(metadata_text):
            continue

        image_url = None
        for key in image_keys:
            image_url = _nested_image_value(row.get(key))
            if image_url:
                break
        if not image_url:
            image_url = _nested_image_value(row.get("images"))
        # Some scraper variants store the image itself in `url`; reject ordinary
        # Pinterest page URLs so a Pin page is never accidentally used as an <img>.
        if not image_url:
            possible = _https_url(row.get("url"))
            if possible:
                parsed = urlparse(possible)
                if "pinimg.com" in (parsed.hostname or "").lower() or re.search(r"\.(?:jpe?g|png|webp|gif)(?:$|\?)", parsed.path, re.I):
                    image_url = possible
        if not image_url or image_url in seen:
            continue

        source_url = ""
        for key in source_keys:
            source_url = _https_url(row.get(key)) or ""
            if source_url:
                break
        if not source_url:
            possible = _https_url(row.get("url"))
            if possible and possible != image_url:
                source_url = possible
        results.append({
            "id": clean_text(str(row.get("id") or row.get("pin_id") or len(results) + 1))[:80],
            "title": title or "Anime wallpaper",
            "url": image_url,
            "thumbnail": _nested_image_value(row.get("thumbnail") or row.get("thumb")) or image_url,
            "source_url": source_url or "https://www.pinterest.com/",
            "source": "Pinterest",
        })
        seen.add(image_url)
        if len(results) >= limit:
            break

    # A few endpoints return a bare list of image URLs instead of objects.
    if not results and isinstance(payload, list):
        for raw in payload:
            image_url = _https_url(raw) if isinstance(raw, str) else None
            if image_url and image_url not in seen:
                results.append({"id": str(len(results) + 1), "title": "Anime wallpaper", "url": image_url,
                                "thumbnail": image_url, "source_url": "https://www.pinterest.com/", "source": "Pinterest"})
                seen.add(image_url)
                if len(results) >= limit:
                    break
    return results


def fetch_pinterest_wallpapers(query: str, count: int = 12) -> list[dict[str, Any]]:
    payload = _bintang_get("/api/search/pinterest", {"q": query})
    items = normalize_pinterest_results(payload, count)
    if not items:
        raise RuntimeError("API Pinterest merespons, tetapi struktur hasil gambar tidak dikenali atau hasilnya kosong.")
    return items


@app.get("/api/wallpapers")
def api_wallpapers():
    query = clean_text(request.args.get("q", request.args.get("tag", "anime wallpaper")))[:80]
    if not query:
        query = "anime wallpaper"
    if _UNSAFE_MEDIA_TERMS.search(query):
        return jsonify({"ok": False, "error": "Pencarian ini tidak tersedia. Gunakan kata kunci wallpaper anime yang aman."}), 400
    try:
        count = max(1, min(int(request.args.get("count", "12")), 12))
    except (TypeError, ValueError):
        count = 12
    force_refresh = request.args.get("refresh") == "1"
    cache_key = query.casefold()
    now = time.time()
    with _wallpaper_cache_lock:
        cached = _wallpaper_cache.get(cache_key)
        if not force_refresh and cached and now - cached[0] < 180 and len(cached[1]) >= count:
            return jsonify({"ok": True, "items": cached[1][:count], "provider": "Bintang API · Pinterest", "cached": True})
    try:
        items = fetch_pinterest_wallpapers(query, count)
    except Exception as exc:
        log.warning("Pinterest wallpaper gagal: %s", str(exc)[:220])
        return jsonify({"ok": False, "error": "Pinterest/Bintang API tidak dapat diakses: " + str(exc)[:160],
                        "provider": "Bintang API · Pinterest"}), 502
    with _wallpaper_cache_lock:
        _wallpaper_cache[cache_key] = (time.time(), items)
    return jsonify({"ok": True, "items": items, "provider": "Bintang API · Pinterest", "cached": False,
                    "partial": len(items) < count})


def _is_pinterest_image_host(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        return (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port in (None, 443)
                and (host == "pinimg.com" or host.endswith(".pinimg.com")))
    except ValueError:
        return False


@app.get("/api/wallpapers/download")
def api_wallpaper_download():
    """Download an image from Pinterest's image CDN; never proxy arbitrary URLs."""
    image_url = request.args.get("url", "")[:2048]
    if not _is_pinterest_image_host(image_url):
        return jsonify({"ok": False, "error": "Unduhan hanya tersedia untuk gambar dari CDN Pinterest yang aman."}), 400
    try:
        response = requests.get(
            image_url,
            headers={"User-Agent": USER_AGENT, "Referer": "https://www.pinterest.com/", "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"},
            timeout=(6, 18),
            allow_redirects=False,
            stream=True,
        )
        if 300 <= response.status_code < 400:
            return jsonify({"ok": False, "error": "CDN meminta pengalihan; unduhan dihentikan demi keamanan."}), 400
        response.raise_for_status()
        content_length = response.headers.get("Content-Length")
        if content_length and content_length.isdigit() and int(content_length) > 15 * 1024 * 1024:
            return jsonify({"ok": False, "error": "Ukuran gambar melebihi batas 15 MB."}), 413
        mime = response.headers.get("Content-Type", "image/jpeg").split(";", 1)[0].strip().lower()
        allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif"}
        if mime not in allowed:
            return jsonify({"ok": False, "error": "Sumber tidak mengirim file gambar yang didukung."}), 415
        data = io.BytesIO()
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > 15 * 1024 * 1024:
                return jsonify({"ok": False, "error": "Ukuran gambar melebihi batas 15 MB."}), 413
            data.write(chunk)
        if total == 0:
            return jsonify({"ok": False, "error": "File gambar kosong."}), 502
        data.seek(0)
        raw_name = clean_text(request.args.get("name", "NOCTURNE-wallpaper"))[:80]
        base_name = re.sub(r"[^A-Za-z0-9_-]+", "_", raw_name).strip("_-") or "NOCTURNE-wallpaper"
        return send_file(data, mimetype=mime, as_attachment=True,
                         download_name=base_name + allowed[mime], max_age=0)
    except requests.RequestException as exc:
        log.warning("Wallpaper download gagal: %s", str(exc)[:180])
        return jsonify({"ok": False, "error": "Server gambar Pinterest tidak dapat dijangkau. Coba lagi."}), 502
    finally:
        try:
            response.close()
        except (UnboundLocalError, AttributeError):
            pass


def normalize_stream_groups(data: dict[str, Any]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for group in data.get("streams", []) if isinstance(data.get("streams"), list) else []:
        if not isinstance(group, dict):
            continue
        resolution = clean_text(str(group.get("resolution") or ""))[:16]
        if not resolution:
            continue
        links = []
        for link in group.get("links", []) if isinstance(group.get("links"), list) else []:
            if not isinstance(link, dict):
                continue
            embed = _https_url(link.get("embed") or link.get("url"))
            if not embed:
                continue
            links.append({"name": clean_text(str(link.get("name") or "Server"))[:50], "embed": embed})
        if links:
            groups.append({"resolution": resolution, "links": links})
    return groups


@app.get("/api/stream")
def api_stream():
    try:
        episode_url = site_url(request.args.get("url", ""))
        if not urlparse(episode_url).path.startswith("/episode/"):
            raise ValueError("URL harus berupa halaman episode OtakuDesu.")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    now = time.time()
    with _stream_cache_lock:
        cached = _stream_cache.get(episode_url)
        if cached and now - cached[0] < 90:
            return jsonify({"ok": True, **cached[1], "provider": "Bintang API · OtakuDesu", "cached": True})
    try:
        payload = _bintang_get("/api/nonton/otakudesu/stream", {"id": episode_url})
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError("Respons stream tidak memiliki objek data yang valid.")
        groups = normalize_stream_groups(data)
        if not groups:
            raise RuntimeError("Tidak ada pilihan stream HTTPS yang tersedia untuk episode ini.")
        normalized = {
            "title": clean_text(str(data.get("title") or "Episode anime"))[:200],
            "embedUrl": _https_url(data.get("embedUrl")) or groups[0]["links"][0]["embed"],
            "streams": groups,
            "slug": clean_text(str(data.get("slug") or ""))[:120],
            "otakudesuUrl": _https_url(data.get("otakudesuUrl")) or episode_url,
        }
        with _stream_cache_lock:
            _stream_cache[episode_url] = (time.time(), normalized)
        return jsonify({"ok": True, **normalized, "provider": "Bintang API · OtakuDesu", "cached": False})
    except Exception as exc:
        log.warning("Stream API gagal: %s", str(exc)[:220])
        return jsonify({"ok": False, "error": "Gagal mengambil pilihan resolusi/server: " + str(exc)[:170],
                        "provider": "Bintang API · OtakuDesu"}), 502


def _quote_is_safe(content: str) -> bool:
    return not _UNSAFE_MEDIA_TERMS.search(content) and not re.search(
        r"\b(?:rape|sexual assault|molest(?:ation)?|child abuse)\b", content, re.IGNORECASE
    )


def fetch_random_anime_quote() -> dict[str, Any]:
    global _quote_cache
    response = requests.get(
        "https://api.animechan.io/v1/quotes/random",
        headers={"User-Agent": "NOCTURNE-Android/4.4", "Accept": "application/json"},
        timeout=(5, 12),
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("Struktur respons Animechan tidak dikenali.")
    content = clean_text(str(data.get("content") or ""))
    if not content or not _quote_is_safe(content):
        raise RuntimeError("Kutipan yang diterima tidak lolos filter konten aman. Coba muat kutipan lain.")
    words = content.split()
    truncated = len(words) > 24
    if truncated:
        content = " ".join(words[:24]).rstrip(".,;:") + "…"
    anime = data.get("anime") if isinstance(data.get("anime"), dict) else {}
    character = data.get("character") if isinstance(data.get("character"), dict) else {}
    return {
        "content": content,
        "anime": clean_text(str(anime.get("name") or "Anime"))[:100],
        "character": clean_text(str(character.get("name") or "Unknown character"))[:100],
        "truncated": truncated,
        "source": "Animechan",
    }


@app.get("/api/quotes/random")
def api_random_anime_quote():
    global _quote_cache
    force_refresh = request.args.get("refresh") == "1"
    now = time.time()
    with _quote_cache_lock:
        cached = _quote_cache
        if not force_refresh and cached and now - cached[0] < 12:
            return jsonify({"ok": True, "quote": cached[1], "provider": "Animechan", "cached": True})
    try:
        quote_data = fetch_random_anime_quote()
        with _quote_cache_lock:
            _quote_cache = (time.time(), quote_data)
        return jsonify({"ok": True, "quote": quote_data, "provider": "Animechan", "cached": False})
    except Exception as exc:
        log.warning("Anime quote gagal: %s", str(exc)[:180])
        return jsonify({"ok": False, "error": "Anime Quotes belum dapat dimuat: " + str(exc)[:150],
                        "provider": "Animechan"}), 502


@app.get("/api/home")
def api_home():
    try:
        data = parse_home()
        return jsonify({"ok": True, **data})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/catalog")
def api_catalog():
    section = request.args.get("section", "ongoing")
    try:
        page = int(request.args.get("page", "1"))
        data = parse_catalog(section, page)
        return jsonify({"ok": True, **data})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/schedule")
def api_schedule():
    try:
        return jsonify({"ok": True, **parse_schedule()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/genres")
def api_genres():
    try:
        return jsonify({"ok": True, **parse_genre_list()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/genre")
def api_genre():
    try:
        url = site_url(request.args.get("url", ""))
        soup, final_url = get_soup(url)
        return jsonify({"ok": True, "title": page_title(soup), "items": parse_anime_cards(soup, limit=150), "source": final_url})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/search")
def api_search():
    try:
        return jsonify({"ok": True, **parse_search(request.args.get("q", ""))})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/anime")
def api_anime():
    try:
        return jsonify({"ok": True, **parse_anime_detail(request.args.get("url", ""))})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/episode")
def api_episode():
    try:
        return jsonify({"ok": True, **parse_episode_detail(request.args.get("url", ""))})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/mal/status")
def api_mal_status():
    """Small connectivity probe to show a finite, actionable provider result."""
    started = time.monotonic()
    try:
        payload = mal_api_get("anime", {"q": "Cowboy Bebop", "limit": 1, "sfw": "true"})
        return jsonify({
            "ok": True,
            "provider": urlparse(MAL_API_BASE).hostname or "configured provider",
            "status": "online",
            "sample_count": len(payload.get("data", [])),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        })
    except Exception as exc:
        return jsonify({
            "ok": False,
            "provider": urlparse(MAL_API_BASE).hostname or "configured provider",
            "status": "unavailable",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error": str(exc),
        }), 502


@app.get("/api/mal/search")
def api_mal_search():
    try:
        return jsonify({"ok": True, "items": mal_search(request.args.get("q", ""))})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/mal/anime/<int:mal_id>")
def api_mal_anime(mal_id: int):
    try:
        return jsonify({"ok": True, "data": mal_anime_detail(mal_id)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/mal/extra/<int:mal_id>/<kind>")
def api_mal_extra(mal_id: int, kind: str):
    try:
        return jsonify({"ok": True, "data": mal_anime_extra(mal_id, kind)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502



@app.get("/api/multiverse/search")
def api_multiverse_search():
    try:
        return jsonify({"ok": True, **multiverse_search(request.args.get("q", ""))})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/api/multiverse/detail")
def api_multiverse_detail():
    try:
        data = multiverse_detail(request.args.get("provider", ""), request.args.get("id", ""))
        return jsonify({"ok": True, **data})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/multiverse/discover")
def api_multiverse_discover():
    try:
        return jsonify({"ok": True, **multiverse_discover()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


# ---------------------------------------------------------------------------
# Manga catalogue + reader (MangaDex chapters, MangaBaka/AniList metadata)
# ---------------------------------------------------------------------------
MANGADEX_API = "https://api.mangadex.org"
MANGABAKA_API = "https://api.mangabaka.org"
MANGADEX_LANG_DEFAULT = ("id", "en")
_MANGA_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
_MANGA_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}\.(?:jpg|jpeg|png|webp|avif)$", re.I)
_MANGA_PROVIDER_INTERVALS = {"mangadex": 0.24, "mangabaka": 0.45}
_MANGA_AT_HOME_MIN_INTERVAL = 1.6  # MangaDex @Home has a stricter per-IP request budget.


def _manga_uuid(value: str) -> str:
    value = str(value or "").strip()
    if not _MANGA_UUID_RE.fullmatch(value):
        raise ValueError("ID MangaDex tidak valid.")
    return value.lower()


def _manga_public_json(provider: str, url: str, params: dict[str, Any] | None = None, ttl: int = 900) -> Any:
    allowed = {"mangadex": "api.mangadex.org", "mangabaka": "api.mangabaka.org"}
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != allowed.get(provider):
        raise ValueError("Host API manga tidak diizinkan.")
    material = json.dumps([provider, url, params or {}], sort_keys=True, ensure_ascii=False)
    key = "manga:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
    cached = cache_get(key)
    if cached is not None:
        return cached
    throttle_key = "mangadex_at_home" if provider == "mangadex" and "/at-home/server/" in parsed.path else provider
    min_interval = _MANGA_AT_HOME_MIN_INTERVAL if throttle_key == "mangadex_at_home" else _MANGA_PROVIDER_INTERVALS[provider]
    lock = _provider_lock(throttle_key)
    with lock:
        wait = min_interval - (time.monotonic() - _provider_last.get(throttle_key, 0.0))
        if wait > 0:
            time.sleep(wait)
        _provider_last[throttle_key] = time.monotonic()
        try:
            response = _api_session.get(url, params=params or {}, timeout=(5, 12), allow_redirects=True)
        except requests.Timeout as exc:
            raise RuntimeError(f"{provider}: request timeout.") from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"{provider}: koneksi gagal: {exc}") from exc
    if response.status_code == 429:
        retry = response.headers.get("Retry-After", "60")
        raise RuntimeError(f"{provider}: HTTP 429, tunggu {retry} detik sebelum mencoba lagi.")
    if response.status_code in (403, 451):
        raise RuntimeError(f"{provider}: akses ditolak HTTP {response.status_code}.")
    if response.status_code >= 400:
        raise RuntimeError(f"{provider}: HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{provider}: respons bukan JSON valid.") from exc
    if ttl > 0:
        cache_set(key, payload, ttl)
    return payload


def _manga_https_url(value: Any) -> str:
    candidate = str(value or "").strip()
    parsed = urlparse(candidate)
    return candidate if parsed.scheme == "https" and parsed.hostname else ""


def _manga_title(value: Any) -> str:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        for key in ("en", "en_us", "ja-ro", "ja", "romaji", "english", "native", "userPreferred"):
            if value.get(key):
                return clean_text(str(value[key]))
        for item in value.values():
            if item:
                return clean_text(str(item))
    return "Untitled"


def _mangadex_cover(item: dict[str, Any]) -> str:
    manga_id = str(item.get("id") or "")
    for rel in item.get("relationships") or []:
        if rel.get("type") == "cover_art":
            filename = ((rel.get("attributes") or {}).get("fileName") or "")
            if _MANGA_UUID_RE.fullmatch(manga_id) and _MANGA_FILE_RE.fullmatch(filename):
                return f"/api/manga/cover/{manga_id}/{filename}.256.jpg"
    return ""


def _manga_normalize_mangadex(item: dict[str, Any]) -> dict[str, Any]:
    attrs = item.get("attributes") or {}
    title = _manga_title(attrs.get("title"))
    alt = [_manga_title(x) for x in (attrs.get("altTitles") or [])[:3]]
    desc = _manga_title(attrs.get("description")) if attrs.get("description") else ""
    if desc == "Untitled":
        desc = ""
    tags = [clean_text(str(((x.get("attributes") or {}).get("name") or {}).get("en") or "")) for x in (attrs.get("tags") or [])]
    return {
        "provider": "mangadex", "id": item.get("id"), "title": title,
        "alt_titles": [x for x in alt if x and x != "Untitled"],
        "cover": _mangadex_cover(item), "description": desc,
        "status": attrs.get("status"), "year": attrs.get("year"),
        "content_rating": attrs.get("contentRating"), "demographic": attrs.get("publicationDemographic"),
        "tags": [x for x in tags if x][:8],
        "url": f"https://mangadex.org/title/{item.get('id')}" if item.get("id") else "",
    }


def _manga_search_mangadex(q: str) -> list[dict[str, Any]]:
    payload = _manga_public_json("mangadex", MANGADEX_API + "/manga", {
        "title": q, "limit": 24, "offset": 0, "order[relevance]": "desc",
        "contentRating[]": ["safe", "suggestive"],
        "includes[]": ["cover_art", "author", "artist"],
    }, ttl=600)
    return [_manga_normalize_mangadex(x) for x in (payload.get("data") or []) if isinstance(x, dict)]


def _mangabaka_collection(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "series", "results", "data"):
            if isinstance(data.get(key), list):
                return data[key]
    for key in ("items", "series", "results"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return []


def _mangabaka_cover(item: dict[str, Any]) -> str:
    # API schemas and upstream adapters may expose the same artwork with different names.
    for key in ("cover", "cover_image", "cover_url", "coverUrl", "main_cover", "mainCover", "image", "thumbnail", "poster"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            return value
        if isinstance(value, dict):
            for candidate in ("x450", "x350", "x250", "large", "original", "url", "src", "image_url"):
                url = value.get(candidate)
                if isinstance(url, str) and url.startswith("https://"):
                    return url
    return ""


def _manga_search_mangabaka(q: str) -> list[dict[str, Any]]:
    payload = _manga_public_json("mangabaka", MANGABAKA_API + "/v2/series/search", {
        "q": q, "type": ["manga", "manhwa", "manhua"], "page": 1, "limit": 24,
        "content_rating": ["safe", "suggestive"],
    }, ttl=900)
    rows = _mangabaka_collection(payload)
    out = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        title = _manga_title(item.get("title") or item.get("titles") or item.get("name"))
        out.append({
            "provider": "mangabaka", "id": item.get("id") or item.get("series_id"),
            "title": title, "cover": _mangabaka_cover(item),
            "description": clean_text(str(item.get("description") or item.get("synopsis") or "")),
            "status": item.get("status"), "year": item.get("year"),
            "url": _manga_https_url(item.get("url") or item.get("site_url")),
            "source_ids": item.get("source") or item.get("sources") or {},
        })
    return out


def _anilist_manga_media(media: dict[str, Any]) -> dict[str, Any]:
    title = media.get("title") or {}
    cover = media.get("coverImage") or {}
    return {
        "provider": "anilist", "id": media.get("id"),
        "title": title.get("userPreferred") or title.get("english") or title.get("romaji") or "Untitled",
        "alt_titles": [x for x in (title.get("english"), title.get("romaji"), title.get("native")) if x],
        "cover": cover.get("extraLarge") or cover.get("large") or "",
        "description": clean_text(re.sub(r"<[^>]*>", " ", str(media.get("description") or ""))),
        "status": media.get("status"), "year": (media.get("startDate") or {}).get("year"),
        "format": media.get("format"), "chapters": media.get("chapters"),
        "score": media.get("averageScore"), "url": _manga_https_url(media.get("siteUrl") or f"https://anilist.co/manga/{media.get('id')}"),
        "external_links": media.get("externalLinks") or [],
    }


def _manga_search_anilist(q: str) -> list[dict[str, Any]]:
    gql = """query ($search: String) { Page(page: 1, perPage: 24) { media(search: $search, type: MANGA, isAdult: false, sort: [SEARCH_MATCH]) { id title { romaji english native userPreferred } coverImage { extraLarge large } description(asHtml: false) format status chapters startDate { year } averageScore siteUrl externalLinks { site url } } } }"""
    payload = _anilist_graphql(gql, {"search": q}, ttl=900)
    return [_anilist_manga_media(x) for x in (((payload.get("Page") or {}).get("media")) or []) if isinstance(x, dict)]


def manga_search(q: str, provider: str = "all") -> dict[str, Any]:
    q = clean_text(q)[:100]
    if len(q) < 2:
        raise ValueError("Masukkan minimal 2 karakter untuk mencari manga.")
    allowed = {"mangadex", "mangabaka", "anilist"}
    selected = list(allowed) if provider == "all" else [provider]
    if any(x not in allowed for x in selected):
        raise ValueError("Provider manga tidak dikenal.")
    functions = {"mangadex": _manga_search_mangadex, "mangabaka": _manga_search_mangabaka, "anilist": _manga_search_anilist}
    results: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        futures = {pool.submit(functions[name], q): name for name in selected}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:
                errors[name] = str(exc)[:220]
    ordered = []
    for name in ("mangadex", "mangabaka", "anilist"):
        ordered.extend(results.get(name, []))
    return {"query": q, "items": ordered, "providers": {k: len(v) for k, v in results.items()}, "errors": errors}


def manga_detail(provider: str, manga_id: str) -> dict[str, Any]:
    if provider == "mangadex":
        manga_id = _manga_uuid(manga_id)
        payload = _manga_public_json("mangadex", MANGADEX_API + "/manga/" + manga_id, {
            "includes[]": ["cover_art", "author", "artist"],
        }, ttl=900)
        data = payload.get("data") or {}
        result = _manga_normalize_mangadex(data)
        authors, artists = [], []
        for rel in data.get("relationships") or []:
            name = clean_text(str(((rel.get("attributes") or {}).get("name")) or ""))
            if name and rel.get("type") == "author": authors.append(name)
            if name and rel.get("type") == "artist": artists.append(name)
        result["authors"] = authors[:8]
        result["artists"] = artists[:8]
        result["chapter_source"] = "mangadex"
        return result
    if provider == "mangabaka":
        manga_id = str(int(manga_id))
        payload = _manga_public_json("mangabaka", MANGABAKA_API + "/v2/series/" + manga_id, {"schema": "full"}, ttl=900)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict): data = payload if isinstance(payload, dict) else {}
        return {
            "provider": "mangabaka", "id": manga_id,
            "title": _manga_title(data.get("title") or data.get("titles") or data.get("name")),
            "cover": _mangabaka_cover(data), "description": clean_text(str(data.get("description") or data.get("synopsis") or "")),
            "status": data.get("status"), "year": data.get("year"), "authors": data.get("authors") or [],
            "source_ids": data.get("source") or data.get("sources") or {},
            "chapter_source": "mangadex", "external_url": _manga_https_url(data.get("url") or data.get("site_url")),
        }
    if provider == "anilist":
        media_id = int(manga_id)
        gql = """query ($id: Int) { Media(id: $id, type: MANGA) { id title { romaji english native userPreferred } coverImage { extraLarge large } description(asHtml: false) format status chapters volumes startDate { year } averageScore siteUrl externalLinks { site url } } }"""
        payload = _anilist_graphql(gql, {"id": media_id}, ttl=900)
        media = payload.get("Media")
        if not isinstance(media, dict): raise RuntimeError("Manga tidak ditemukan pada AniList.")
        result = _anilist_manga_media(media)
        result["volumes"] = media.get("volumes")
        result["chapter_source"] = "mangadex"
        return result
    raise ValueError("Provider manga tidak dikenal.")


def manga_chapters(manga_id: str, languages: list[str], offset: int = 0) -> dict[str, Any]:
    manga_id = _manga_uuid(manga_id)
    safe_languages = [x for x in languages[:5] if re.fullmatch(r"[a-zA-Z-]{2,5}", x)]
    if not safe_languages: safe_languages = list(MANGADEX_LANG_DEFAULT)
    offset = max(0, min(10000, int(offset or 0)))
    params: dict[str, Any] = {
        "limit": 100, "offset": offset,
        "translatedLanguage[]": safe_languages,
        "contentRating[]": ["safe", "suggestive"],
        "includes[]": ["scanlation_group", "user"],
        "order[volume]": "desc", "order[chapter]": "desc",
    }
    payload = _manga_public_json("mangadex", f"{MANGADEX_API}/manga/{manga_id}/feed", params, ttl=240)
    chapters = []
    for item in payload.get("data") or []:
        attrs = item.get("attributes") or {}
        group, uploader = "Unknown group", ""
        for rel in item.get("relationships") or []:
            rel_attrs = rel.get("attributes") or {}
            if rel.get("type") == "scanlation_group": group = clean_text(str(rel_attrs.get("name") or group))
            elif rel.get("type") == "user": uploader = clean_text(str(rel_attrs.get("username") or ""))
        chapters.append({
            "id": item.get("id"), "chapter": attrs.get("chapter"), "volume": attrs.get("volume"),
            "title": clean_text(str(attrs.get("title") or "")), "language": attrs.get("translatedLanguage"),
            "pages": attrs.get("pages"), "published_at": attrs.get("publishAt"),
            "group": group, "uploader": uploader,
        })
    return {"items": chapters, "total": int(payload.get("total") or len(chapters)), "offset": offset, "limit": int(payload.get("limit") or 100), "languages": safe_languages}


def manga_chapter_reader(chapter_id: str, quality: str = "data") -> dict[str, Any]:
    chapter_id = _manga_uuid(chapter_id)
    quality = "data-saver" if quality == "data-saver" else "data"
    chapter_payload = _manga_public_json("mangadex", f"{MANGADEX_API}/chapter/{chapter_id}", {
        "includes[]": ["manga", "scanlation_group", "user"],
    }, ttl=900)
    chapter = chapter_payload.get("data") or {}
    attrs = chapter.get("attributes") or {}
    relations = {x.get("type"): x for x in (chapter.get("relationships") or [])}
    group = ((relations.get("scanlation_group") or {}).get("attributes") or {}).get("name") or "Unknown group"
    uploader = ((relations.get("user") or {}).get("attributes") or {}).get("username") or ""
    manga_rel = relations.get("manga") or {}
    manga_rel_attrs = manga_rel.get("attributes") or {}
    home = _manga_public_json("mangadex", f"{MANGADEX_API}/at-home/server/{chapter_id}", {}, ttl=600)
    chapter_data = home.get("chapter") or {}
    filenames = chapter_data.get("dataSaver" if quality == "data-saver" else "data") or []
    if not isinstance(filenames, list) or not filenames:
        raise RuntimeError("MangaDex tidak menyediakan halaman untuk chapter ini.")
    pages = [f"/api/manga/page/{chapter_id}/{i}?quality={quality}" for i in range(len(filenames))]
    return {
        "id": chapter_id, "chapter": attrs.get("chapter"), "volume": attrs.get("volume"),
        "title": clean_text(str(attrs.get("title") or "")), "language": attrs.get("translatedLanguage"),
        "published_at": attrs.get("publishAt"), "pages": pages, "page_count": len(pages),
        "group": clean_text(str(group)), "uploader": clean_text(str(uploader)),
        "manga_title": _manga_title(manga_rel_attrs.get("title")) if manga_rel_attrs else "MangaDex chapter",
        "quality": quality, "source_url": f"https://mangadex.org/chapter/{chapter_id}",
    }


def _mangadex_image_response(url: str):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "uploads.mangadex.org" or host.endswith(".mangadex.network")):
        raise ValueError("CDN MangaDex tidak valid.")
    response = _api_session.get(url, headers={"Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8", "Referer": "https://mangadex.org/"}, timeout=(5, 18), allow_redirects=True)
    final = urlparse(response.url)
    final_host = (final.hostname or "").lower()
    if final.scheme != "https" or not (final_host == "uploads.mangadex.org" or final_host.endswith(".mangadex.network")):
        raise RuntimeError("CDN mengalihkan ke host yang tidak diizinkan.")
    if response.status_code >= 400:
        raise RuntimeError(f"CDN MangaDex HTTP {response.status_code}.")
    if len(response.content) > 18 * 1024 * 1024:
        raise RuntimeError("Halaman gambar melebihi batas 18 MB.")
    mime = response.headers.get("Content-Type", "image/jpeg").split(";", 1)[0].lower()
    if mime not in {"image/jpeg", "image/png", "image/webp", "image/avif"}:
        raise RuntimeError("CDN mengirim tipe file yang tidak didukung.")
    return response.content, mime


@app.get("/api/providers/status")
def api_providers_status():
    return jsonify({"ok": True, "providers": provider_status()})


@app.get("/api/telemetry")
def api_telemetry():
    return jsonify(telemetry_snapshot())


@app.get("/api/manga/search")
def api_manga_search():
    try:
        data = manga_search(request.args.get("q", ""), request.args.get("provider", "all"))
        return jsonify({"ok": True, **data})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/manga/detail")
def api_manga_detail():
    try:
        data = manga_detail(request.args.get("provider", ""), request.args.get("id", ""))
        return jsonify({"ok": True, **data})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/manga/chapters")
def api_manga_chapters():
    try:
        manga_id = request.args.get("id", "")
        languages = [x.strip() for x in request.args.get("lang", "id,en").split(",") if x.strip()]
        offset = request.args.get("offset", 0, type=int)
        return jsonify({"ok": True, **manga_chapters(manga_id, languages, offset)})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/manga/chapter/<chapter_id>")
def api_manga_chapter_reader(chapter_id: str):
    try:
        return jsonify({"ok": True, **manga_chapter_reader(chapter_id, request.args.get("quality", "data"))})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/manga/page/<chapter_id>/<int:index>")
def api_manga_page(chapter_id: str, index: int):
    try:
        chapter_id = _manga_uuid(chapter_id)
        quality = "data-saver" if request.args.get("quality") == "data-saver" else "data"
        home = _manga_public_json("mangadex", f"{MANGADEX_API}/at-home/server/{chapter_id}", {}, ttl=600)
        data = home.get("chapter") or {}
        filenames = data.get("dataSaver" if quality == "data-saver" else "data") or []
        if index < 0 or index >= len(filenames):
            return jsonify({"ok": False, "error": "Indeks halaman di luar rentang."}), 404
        filename = str(filenames[index])
        if not _MANGA_FILE_RE.fullmatch(filename):
            return jsonify({"ok": False, "error": "Nama file chapter tidak valid."}), 400
        base = str(home.get("baseUrl") or "").rstrip("/")
        host = (urlparse(base).hostname or "").lower()
        if urlparse(base).scheme != "https" or not (host == "uploads.mangadex.org" or host.endswith(".mangadex.network")):
            return jsonify({"ok": False, "error": "Host gambar MangaDex tidak valid."}), 502
        image_url = f"{base}/{quality}/{data.get('hash','')}/{filename}"
        if not re.fullmatch(r"[A-Za-z0-9]{8,64}", str(data.get("hash") or "")):
            return jsonify({"ok": False, "error": "Hash chapter tidak valid."}), 502
        body, mime = _mangadex_image_response(image_url)
        response = send_file(io.BytesIO(body), mimetype=mime, max_age=900, conditional=False)
        response.headers["Cache-Control"] = "public, max-age=900, immutable"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/manga/cover/<manga_id>/<filename>")
def api_manga_cover(manga_id: str, filename: str):
    try:
        manga_id = _manga_uuid(manga_id)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,180}\.256\.jpg", filename, re.I):
            return jsonify({"ok": False, "error": "Nama cover tidak valid."}), 400
        body, mime = _mangadex_image_response(f"https://uploads.mangadex.org/covers/{manga_id}/{filename}")
        response = send_file(io.BytesIO(body), mimetype=mime, max_age=3600, conditional=False)
        response.headers["Cache-Control"] = "public, max-age=3600"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/multiverse/airing")
def api_multiverse_airing():
    try:
        gql = '''query { Page(page:1,perPage:25) { airingSchedules(notYetAired:true, sort:TIME) { id airingAt timeUntilAiring episode media { id title { romaji english } coverImage { large } siteUrl } } } }'''
        data = _anilist_graphql(gql, {}, ttl=600)
        return jsonify({"ok": True, "items": ((data.get("Page") or {}).get("airingSchedules") or [])})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/multiverse/random")
def api_multiverse_random():
    try:
        payload = mal_api_get("random/anime", {"sfw": "true"})
        return jsonify({"ok": True, "data": payload.get("data", {})})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/multiverse/news")
def api_multiverse_news():
    try:
        payload = mal_api_get("news", {"limit": 12})
        rows = payload.get("data", [])
        return jsonify({"ok": True, "items": rows})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/multiverse/mitenime-home")
def api_mitenime_home():
    try:
        return jsonify({"ok": True, "data": _external_json("mitenime", MITENIME_BASE + "/home", ttl=900)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/diagnose")
def api_diagnose():
    return jsonify({"ok": True, "results": run_diagnostics()})


@app.get("/api/mal/top")
def api_mal_top():
    try:
        page = max(1, min(int(request.args.get("page", "1")), 20))
        payload = mal_api_get("top/anime", {"page": page, "limit": 24, "filter": request.args.get("filter", "bypopularity")})
        return jsonify({"ok": True, "items": payload.get("data", []), "pagination": payload.get("pagination", {})})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/mal/seasons/now")
def api_mal_season_now():
    try:
        payload = mal_api_get("seasons/now", {"limit": 24})
        return jsonify({"ok": True, "items": payload.get("data", [])})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/library")
def api_library_get():
    return jsonify({"ok": True, "items": library_rows()})


@app.post("/api/library")
def api_library_save():
    data = request.get_json(silent=True) or {}
    try:
        mal_id = int(data.get("mal_id", 0))
        title = clean_text(str(data.get("title", "")))[:240]
        if not 1 <= mal_id <= 99999999 or not title:
            raise ValueError("MAL ID dan judul wajib diisi.")
        status = str(data.get("status", "Plan to Watch"))
        allowed = {"Watching", "Completed", "On-Hold", "Dropped", "Plan to Watch"}
        if status not in allowed:
            raise ValueError("Status koleksi tidak valid.")
        score = data.get("score")
        score = int(score) if score not in (None, "") else None
        if score is not None and not 1 <= score <= 10:
            raise ValueError("Skor pribadi harus 1–10.")
        with db_connect() as db:
            db.execute("""INSERT INTO library(mal_id,title,image,mal_url,status,score,notes,updated_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(mal_id) DO UPDATE SET
                title=excluded.title,image=excluded.image,mal_url=excluded.mal_url,status=excluded.status,
                score=excluded.score,notes=excluded.notes,updated_at=excluded.updated_at""",
                (mal_id,title,safe_http_url(data.get("image")),safe_http_url(data.get("mal_url")),status,score,str(data.get("notes", ""))[:3000],datetime.now(timezone.utc).isoformat()))
        return jsonify({"ok": True, "items": library_rows()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.delete("/api/library/<int:mal_id>")
def api_library_delete(mal_id):
    with db_connect() as db:
        db.execute("DELETE FROM library WHERE mal_id=?", (mal_id,))
    return jsonify({"ok": True})


@app.post("/api/history")
def api_history_save():
    data = request.get_json(silent=True) or {}
    try:
        episode_url = site_url(str(data.get("episode_url", "")))
        title = clean_text(str(data.get("title", "Episode")))[:240]
        anime_title = clean_text(str(data.get("anime_title", "")))[:240]
        image = safe_http_url(data.get("image")) or ""
        with db_connect() as db:
            db.execute("""INSERT INTO history(episode_url,title,anime_title,image,watched_at,completed)
                VALUES(?,?,?,?,?,?) ON CONFLICT(episode_url) DO UPDATE SET
                title=excluded.title,anime_title=excluded.anime_title,image=excluded.image,
                watched_at=excluded.watched_at,completed=excluded.completed""",
                (episode_url,title,anime_title,image,datetime.now(timezone.utc).isoformat(),int(bool(data.get("completed", False)))))
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/api/history")
def api_history_get():
    with db_connect() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM history ORDER BY watched_at DESC LIMIT 200")]
    return jsonify({"ok": True, "items": rows})


@app.delete("/api/history")
def api_history_clear():
    with db_connect() as db:
        db.execute("DELETE FROM history")
    return jsonify({"ok": True})


@app.get("/api/export")
def api_export():
    with db_connect() as db:
        library = [dict(r) for r in db.execute("SELECT * FROM library ORDER BY updated_at DESC")]
        history = [dict(r) for r in db.execute("SELECT * FROM history ORDER BY watched_at DESC")]
    return jsonify({"ok": True, "app": "NOCTURNE", "exported_at": datetime.now(timezone.utc).isoformat(), "library": library, "history": history})


@app.post("/api/import")
def api_import():
    data = request.get_json(silent=True) or {}
    imported = 0
    try:
        for item in data.get("library", [])[:1000]:
            mal_id = int(item.get("mal_id", 0)); title = clean_text(str(item.get("title", "")))[:240]
            if not mal_id or not title: continue
            status = item.get("status", "Plan to Watch")
            if status not in {"Watching", "Completed", "On-Hold", "Dropped", "Plan to Watch"}: status = "Plan to Watch"
            with db_connect() as db:
                db.execute("INSERT OR REPLACE INTO library(mal_id,title,image,mal_url,status,score,notes,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (mal_id,title,safe_http_url(item.get("image")),safe_http_url(item.get("mal_url")),status,item.get("score"),str(item.get("notes", ""))[:3000],datetime.now(timezone.utc).isoformat()))
            imported += 1
        return jsonify({"ok": True, "imported": imported})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


PAGE = r'''<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#100c10">
<title>NOCTURNE: streaming anime</title>
<style>
:root{--bg:#101113;--panel:#191b1e;--panel2:#22252a;--line:#383b41;--text:#e9e9ec;--muted:#a5a8af;--red:#9e304c;--red2:#cb647d;--gold:#b6b0a5;--shadow:0 18px 48px #0009}
*{box-sizing:border-box}html{min-height:100%;scroll-behavior:smooth}body{margin:0;background:radial-gradient(ellipse at 12% -8%,#34363b66,transparent 36%),radial-gradient(ellipse at 98% 30%,#292b3066,transparent 32%),var(--bg);color:var(--text);font-family:Georgia,'Times New Roman',serif;min-height:100vh;overflow-y:auto}
body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.13;background-image:repeating-linear-gradient(0deg,transparent 0 3px,#fff1 4px),repeating-linear-gradient(90deg,transparent 0 3px,#0002 4px);mix-blend-mode:soft-light;z-index:0}
button,input{font:inherit}button{cursor:pointer}.app{position:relative;z-index:1;min-height:100vh}.topbar{position:sticky;top:0;z-index:20;background:#101113ed;backdrop-filter:blur(14px);border-bottom:1px solid var(--line);padding:12px clamp(14px,3vw,34px);display:flex;gap:18px;align-items:center;justify-content:space-between}.brand{display:flex;gap:12px;align-items:center;min-width:max-content}.sigil{width:42px;height:42px;display:grid;place-items:center;overflow:hidden;border-radius:50%;background:#050505;box-shadow:0 0 18px #ffffff20}.sigil img{display:block;width:100%;height:100%;object-fit:cover}.brandname{font-size:18px;letter-spacing:3px;font-weight:bold}.brandname small{display:block;color:var(--gold);font-size:9px;letter-spacing:4px;margin-top:3px}.searchbar{display:flex;max-width:580px;width:100%;border:1px solid var(--line);background:#1d1f23;border-radius:4px;overflow:hidden}.searchbar input{min-width:0;flex:1;background:transparent;color:var(--text);border:0;outline:none;padding:11px 14px;font-family:Arial,sans-serif}.searchbar button{border:0;background:var(--red);color:white;padding:0 17px;font-family:Arial,sans-serif;font-weight:bold;letter-spacing:1px}.status{font:11px Arial,sans-serif;color:var(--muted);white-space:nowrap}
.shell{display:grid;grid-template-columns:220px minmax(0,1fr);max-width:1600px;margin:0 auto;min-height:calc(100vh - 66px)}.sidebar{border-right:1px solid var(--line);padding:28px 16px;position:sticky;top:65px;align-self:start;height:calc(100vh - 65px);overflow-y:auto}.side-label{color:var(--gold);font-size:10px;letter-spacing:3px;margin:8px 10px 14px}.navbtn{width:100%;border:1px solid transparent;background:transparent;color:var(--muted);padding:12px 13px;text-align:left;margin:3px 0;border-radius:4px;font-size:14px;transition:.18s}.navbtn:hover,.navbtn.active{background:linear-gradient(90deg,#76253a44,transparent);border-color:#713045;color:var(--text)}.navbtn .ico{display:inline-block;width:25px;color:var(--red2)}.side-note{margin:35px 9px 0;padding:14px 12px;border-top:1px solid var(--line);color:#8d7883;font:11px/1.7 Arial,sans-serif}.main{padding:28px clamp(14px,3vw,38px) 70px;min-width:0;min-height:calc(100vh - 66px)}
/* 4.4 cinematic banner + manga reader + live transport console */
.hero{min-height:218px;border-color:#654150;background-position:center 46%;}
.hero:after{content:"";right:5%;top:50%;width:clamp(170px,23vw,290px);height:clamp(170px,23vw,290px);border:1px solid #c36a8138;border-radius:50%;background:radial-gradient(circle at center,transparent 52%,#a33c5922 53%,transparent 54%),repeating-conic-gradient(from 12deg,#c8788d28 0 1deg,transparent 1deg 19deg);transform:translateY(-50%);animation:nocturneSigilTurn 48s linear infinite,nocturneSigilGlow 8s ease-in-out infinite;}
.hero h1{background:linear-gradient(100deg,#fff 0%,#eed9df 36%,#cb788e 66%,#fff 90%);background-size:220% auto;-webkit-background-clip:text;background-clip:text;color:transparent;text-shadow:0 0 24px #b3425b25;animation:nocturneTitleShimmer 9s linear infinite;}
@keyframes nocturneTitleShimmer{to{background-position:220% center}}
.hero-content{max-width:760px}.manga-searchbar{display:grid;grid-template-columns:minmax(0,1fr) 170px auto;gap:9px;margin:18px 0}.field{border:1px solid #41434a;border-radius:4px;background:#111317;color:var(--text);padding:12px 13px;min-width:0;width:100%;font:13px Arial,sans-serif;outline:none}.field:focus{border-color:#a54a65;box-shadow:0 0 0 2px #a54a6526}.manga-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(142px,1fr));gap:15px}.manga-card{background:linear-gradient(155deg,#252329,#17181c);border:1px solid #3b3940;border-radius:6px;overflow:hidden;cursor:pointer;transition:transform .2s,border-color .2s,box-shadow .2s}.manga-card:hover{transform:translateY(-4px);border-color:#a54a65;box-shadow:0 13px 30px #0008}.manga-cover{aspect-ratio:2/2.9;background:linear-gradient(145deg,#38212d,#111);overflow:hidden}.manga-cover img{width:100%;height:100%;object-fit:cover;display:block}.manga-card-info{padding:10px}.manga-card-title{font-size:13px;line-height:1.35;min-height:34px;color:#f2e9ec}.manga-provider{display:inline-block;font:9px Arial,sans-serif;letter-spacing:1.2px;color:#d8a0ae;border:1px solid #754153;padding:4px 6px;border-radius:2px;margin-top:7px;text-transform:uppercase}.manga-detail{display:grid;grid-template-columns:minmax(130px,210px) minmax(0,1fr);gap:20px}.manga-detail-cover{width:100%;max-height:330px;object-fit:cover;border:1px solid #57404a;border-radius:5px}.manga-desc{font:13px/1.65 Arial,sans-serif;color:#bbb6bd;white-space:pre-line;max-height:180px;overflow:auto}.manga-chapter-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:center;padding:12px 6px;border-bottom:1px solid #34353b}.manga-chapter-row:last-child{border-bottom:0}.manga-chapter-title{font-size:13px;color:#eee;line-height:1.4}.manga-chapter-meta{font:10px/1.6 Arial,sans-serif;color:var(--muted);margin-top:4px}.reader-toolbar{position:sticky;top:65px;z-index:10;display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;background:#141519ee;border:1px solid #424149;border-radius:5px;padding:9px;margin-bottom:12px;backdrop-filter:blur(12px)}.reader-shell{background:#08090b;border:1px solid #34343a;border-radius:5px;overflow:hidden}.reader-pages{display:flex;flex-direction:column;align-items:center;gap:3px;touch-action:pan-y}.reader-page-wrap{position:relative;width:100%;max-width:980px;min-height:160px;display:flex;justify-content:center;background:#0c0d10;scroll-margin-top:122px}.reader-page{display:block;width:auto;max-width:100%;height:auto;max-height:none;object-fit:contain}.reader-page-index{position:absolute;right:9px;bottom:9px;background:#0b0b0dcc;color:#d8cbd0;border:1px solid #ffffff22;border-radius:3px;padding:4px 7px;font:10px Arial,sans-serif}.reader-loading{padding:24px;text-align:center;font:11px Arial,sans-serif;letter-spacing:2px;color:#be7c8f}.reader-credit{padding:12px;background:#17171b;color:#b7aab0;font:11px/1.7 Arial,sans-serif;text-align:center;border-top:1px solid #34343a}.reader-credit a{color:#e1a6b5}.telemetry-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:15px 0}.telemetry-card{padding:13px;border:1px solid #393a42;background:linear-gradient(145deg,#24252b,#17181d);border-radius:5px;min-width:0}.telemetry-card small{display:block;color:#a9a2aa;font:9px Arial,sans-serif;letter-spacing:1.3px;text-transform:uppercase}.telemetry-card strong{display:block;font:20px/1.3 Arial,sans-serif;color:#f1dce2;margin-top:7px;overflow-wrap:anywhere}.telemetry-card em{display:block;font:10px Arial,sans-serif;color:#bb8091;margin-top:5px;font-style:normal}.traffic-panel{border:1px solid #3b3940;border-radius:6px;background:#17181c;padding:15px;margin:14px 0}.traffic-bar{height:7px;border-radius:20px;background:#303139;overflow:hidden;margin:9px 0}.traffic-bar span{display:block;height:100%;width:0;background:linear-gradient(90deg,#693348,#d26d89,#e3bdc8);box-shadow:0 0 16px #c453744d;transition:width .75s ease}.traffic-label{display:flex;justify-content:space-between;gap:12px;font:10px Arial,sans-serif;color:#b8b1b9}.live-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#8ec7a0;box-shadow:0 0 12px #8ec7a0;animation:livePulse 1.4s infinite}@keyframes livePulse{50%{opacity:.3;transform:scale(.72)}}.provider-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:9px}.provider-node{border:1px solid #393940;background:#1a1b20;border-radius:5px;padding:12px}.provider-node-head{display:flex;justify-content:space-between;gap:8px;align-items:center}.provider-node strong{font-size:13px}.provider-metrics{display:flex;gap:12px;flex-wrap:wrap;font:10px Arial,sans-serif;color:#aaa3ab;margin-top:9px}.provider-meter{height:3px;background:#34343a;margin-top:10px;overflow:hidden}.provider-meter span{display:block;height:100%;background:linear-gradient(90deg,#7d354d,#d87d95);width:0;transition:width .5s}.http-event{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:9px;padding:8px 0;border-bottom:1px solid #303139;font:10px Arial,sans-serif;color:#bab3bb}.http-event:last-child{border:0}.http-event b{font-weight:normal;color:#eee}.traffic-legend{font:10px/1.7 Arial,sans-serif;color:#a8a2ab}
@media(max-width:800px){.manga-searchbar{grid-template-columns:1fr}.manga-detail{grid-template-columns:110px minmax(0,1fr);gap:13px}.telemetry-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.reader-toolbar{top:0}.reader-page-wrap{scroll-margin-top:92px}}
@media(max-width:430px){.manga-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.manga-chapter-row{grid-template-columns:minmax(0,1fr)}.manga-chapter-row .action{justify-self:start}.telemetry-card strong{font-size:17px}}
@media(prefers-reduced-motion:reduce){.hero h1,.hero:after,.live-dot{animation:none}}
.hero{background-image:url('/assets/kolom/banner.svg');background-color:#202226;background-size:cover;background-position:center;border:1px solid #45484f;border-radius:7px;padding:28px clamp(18px,3vw,34px);margin-bottom:28px;position:relative;overflow:hidden;box-shadow:var(--shadow);isolation:isolate;min-height:170px}.hero:before{content:"";position:absolute;inset:-38%;z-index:1;pointer-events:none;opacity:.78;background:radial-gradient(ellipse at 18% 42%,#9e304c55 0,transparent 30%),radial-gradient(ellipse at 76% 58%,#b7a2ad24 0,transparent 26%),linear-gradient(112deg,transparent 35%,#d7a5b51c 49%,transparent 62%);background-size:155% 155%;animation:nocturneBannerDrift 13s ease-in-out infinite alternate,nocturneBannerPulse 7s ease-in-out infinite}
@keyframes nocturneBannerDrift{0%{transform:translate3d(-3%,1%,0) rotate(-3deg);background-position:0% 45%}50%{transform:translate3d(2%,-2%,0) rotate(1deg);background-position:65% 52%}100%{transform:translate3d(4%,2%,0) rotate(4deg);background-position:100% 48%}}
@keyframes nocturneBannerPulse{0%,100%{opacity:.45}50%{opacity:.95}}
.hero:after{content:"✦";position:absolute;right:7%;top:-50px;color:#b3445b18;font-size:240px;line-height:1;z-index:1;pointer-events:none;transform-origin:center;animation:nocturneSigilTurn 32s linear infinite,nocturneSigilGlow 6s ease-in-out infinite}
@keyframes nocturneSigilTurn{to{transform:rotate(360deg)}}
@keyframes nocturneSigilGlow{0%,100%{opacity:.45}50%{opacity:1}}
.hero:not(.has-custom-media) .hero-content{animation:nocturneContentIn 1.2s ease-out both}
@keyframes nocturneContentIn{from{opacity:.55;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
@media(prefers-reduced-motion:reduce){.hero:before,.hero:after,.hero:not(.has-custom-media) .hero-content{animation:none}}
.hero-media{position:absolute;inset:0;z-index:0;overflow:hidden}.hero-media img,.hero-media video{display:block;width:100%;height:100%;object-fit:cover;object-position:center}.hero-shade{position:absolute;inset:0;z-index:1;pointer-events:none;background:linear-gradient(90deg,#111318e8,#111318b5 62%,#11131875)}.hero-content{position:relative;z-index:2}.hero.has-custom-media{background-image:none}.hero.has-custom-media:before,.hero.has-custom-media:after{display:none}.appearance-preview{position:relative;overflow:hidden;min-height:150px;aspect-ratio:16/5;border:1px solid #45484f;border-radius:6px;background:linear-gradient(120deg,#303238,#15171b);margin:12px 0 16px}.appearance-preview img,.appearance-preview video{width:100%;height:100%;position:absolute;inset:0;object-fit:cover}.appearance-preview .preview-fallback{position:absolute;inset:0;display:grid;place-items:center;color:#b9aeb4;font:12px Arial,sans-serif;letter-spacing:2px}
.appearance-preview.default-motion:after{content:"";position:absolute;inset:-45%;pointer-events:none;background:radial-gradient(ellipse at 18% 45%,#9e304c66,transparent 32%),radial-gradient(ellipse at 78% 58%,#c5b5be30,transparent 28%),linear-gradient(110deg,transparent 38%,#fff2 50%,transparent 62%);background-size:160% 160%;animation:nocturneBannerDrift 13s ease-in-out infinite alternate,nocturneBannerPulse 7s ease-in-out infinite}
.appearance-preview .default-banner{width:100%;height:100%;position:absolute;inset:0;object-fit:cover}
.eyebrow{font:10px Arial,sans-serif;letter-spacing:4px;color:var(--gold);text-transform:uppercase}.hero h1{font-size:clamp(26px,4vw,42px);font-weight:normal;letter-spacing:1px;margin:9px 0}.hero p{color:#c0adb7;max-width:660px;font-size:14px;line-height:1.65;margin:0}.section-head{display:flex;align-items:end;justify-content:space-between;gap:12px;margin:25px 0 16px;padding-bottom:10px;border-bottom:1px solid var(--line)}.section-head h2{font-size:21px;font-weight:normal;letter-spacing:1.5px;margin:0}.section-head small{font:10px Arial,sans-serif;color:var(--muted);letter-spacing:2px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:17px}.card{min-width:0;background:linear-gradient(160deg,#222429,#181a1e);border:1px solid #363940;border-radius:5px;overflow:hidden;cursor:pointer;transition:transform .18s,border-color .18s,box-shadow .18s}.card:hover{transform:translateY(-4px);border-color:#934157;box-shadow:0 12px 26px #0008}.poster{aspect-ratio:2/2.85;background:linear-gradient(145deg,#30202b,#100c10);position:relative;overflow:hidden}.poster img{display:block;width:100%;height:100%;object-fit:cover}.poster .noimg{height:100%;display:grid;place-items:center;color:#754052;font-size:38px}.cardbody{padding:10px 11px 12px}.cardtitle{font-size:13px;line-height:1.35;min-height:35px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;color:#f0e5e9}.cardsub{font:10px Arial,sans-serif;color:var(--gold);margin-top:7px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.pill{display:inline-block;border:1px solid #714052;color:#d7a2af;padding:4px 8px;font:10px Arial,sans-serif;letter-spacing:1px;border-radius:20px}.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 18px}.action{border:1px solid #633448;background:#241720;color:#eadde2;padding:9px 13px;border-radius:3px;font:12px Arial,sans-serif}.action:hover{background:#592338}.action.primary{background:var(--red);border-color:var(--red);color:white}.content-card{background:#1b1d21;border:1px solid var(--line);border-radius:6px;padding:19px;margin-bottom:18px}.detail-grid{display:grid;grid-template-columns:minmax(160px,230px) minmax(0,1fr);gap:23px}.detail-poster{width:100%;max-height:340px;object-fit:cover;border:1px solid #503344;border-radius:4px}.detail-title{font-size:clamp(22px,3vw,34px);font-weight:normal;line-height:1.2;margin:2px 0 13px}.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px;margin:14px 0}.meta div{background:#131518;border:1px solid #34373d;padding:9px 10px;font:12px/1.45 Arial,sans-serif;color:#d8cbd1}.meta b{display:block;color:var(--gold);font-size:10px;letter-spacing:1px;text-transform:uppercase;margin-bottom:3px}.desc{font:13px/1.7 Arial,sans-serif;color:#bcaeb6}.list{display:grid;gap:7px}.listbtn{width:100%;text-align:left;background:#1a1c20;border:1px solid #383b42;color:var(--text);padding:12px 13px;border-radius:3px;font-size:13px}.listbtn:hover{border-color:var(--red);background:#28151f}.daytitle{color:var(--gold);letter-spacing:2px;font-size:13px;margin:22px 0 10px}
.anime-index{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0 18px}.anime-index a{min-width:29px;text-align:center;text-decoration:none;color:#e7dce1;background:#1c1e22;border:1px solid #41444b;border-radius:3px;padding:6px 8px;font:12px Arial,sans-serif}.anime-index a:hover{border-color:var(--red2);background:#2b1720}.anime-filter{width:100%;max-width:520px;margin:0 0 16px}.anime-group{margin:19px 0 24px;scroll-margin-top:90px}.anime-group-head{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #3b4550;padding:0 4px 8px;margin-bottom:9px}.anime-group-head h3{font-size:17px;font-weight:normal;letter-spacing:1.5px;margin:0;color:#f0e8ec}.anime-group-head span{font:10px Arial,sans-serif;color:var(--muted)}.anime-rows{display:grid;gap:2px}.anime-row{display:flex;align-items:flex-start;gap:10px;width:100%;border:0;border-bottom:1px solid #ffffff0b;background:transparent;color:#d5d0d3;text-align:left;padding:8px 7px;font:14px/1.55 Arial,sans-serif;border-radius:2px}.anime-row:before{content:'•';color:#888;flex:0 0 10px}.anime-row:hover{background:#ffffff09;color:#fff}.genre-tools{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 16px}.genre-cloud{display:flex;flex-wrap:wrap;align-items:center;gap:9px 12px;padding:4px 2px 18px}.genre-chip{display:inline-flex;align-items:center;justify-content:center;text-decoration:none;border:1px solid #3c4149;border-radius:3px;background:#1b1e23;color:#e2d9de;padding:9px 13px;font:13px Arial,sans-serif;min-height:38px;max-width:100%}.genre-chip:hover{border-color:var(--red2);background:#291822;color:#fff}.wallpaper-card{overflow:hidden}.wallpaper-thumb{display:block;width:100%;aspect-ratio:3/4;background:#101115;object-fit:cover;cursor:zoom-in}.wallpaper-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}.wallpaper-lightbox{position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.94);display:flex;align-items:center;justify-content:center;padding:16px;overflow:auto}.wallpaper-lightbox-inner{width:min(100%,980px);max-height:100%;display:flex;flex-direction:column;gap:12px;align-items:center}.wallpaper-lightbox img{display:block;max-width:100%;max-height:calc(100dvh - 170px);width:auto;height:auto;object-fit:contain;border:1px solid #393b40;box-shadow:0 18px 60px #000}.wallpaper-lightbox-bar{width:100%;display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:space-between}.wallpaper-lightbox-title{flex:1;min-width:120px;color:#f4edf0;font:13px/1.4 Arial,sans-serif}.wallpaper-lightbox-close{position:fixed;top:12px;right:12px;z-index:2;width:42px;height:42px;border:1px solid #777;background:#17191d;color:white;border-radius:50%;font-size:22px}.wallpaper-lightbox .action{display:inline-flex;align-items:center;justify-content:center;text-decoration:none}.wallpaper-lightbox-error{color:#e0b5bf;font:12px Arial,sans-serif;text-align:center}@media(max-width:600px){.wallpaper-lightbox{padding:10px}.wallpaper-lightbox img{max-height:calc(100dvh - 190px)}.genre-cloud{gap:7px}.genre-chip{padding:8px 10px;font-size:12px}.anime-row{font-size:13px}}
.playerbox{background:#08090b;border:1px solid #45484f;border-radius:5px;overflow:hidden}.playerbox iframe{display:block;width:100%;aspect-ratio:16/9;min-height:230px;border:0;background:#000}.playerbox{box-shadow:0 14px 32px #0009}.player-tools .action.primary{min-width:112px;font-weight:bold;letter-spacing:1px}.quote-card{max-width:900px;margin:22px auto;padding:clamp(22px,5vw,48px);background:radial-gradient(ellipse at 50% 0%,#ffffff12,transparent 65%),#17191d;border:1px solid #555960;text-align:center}.quote-text{font-size:clamp(22px,4vw,34px);line-height:1.55;color:#f0edf0;margin:28px auto;max-width:760px;font-family:Georgia,serif}.quote-meta{display:grid;gap:5px;color:#d7c7ce}.quote-meta strong{font-size:14px;letter-spacing:1px}.quote-meta span{font:12px Arial,sans-serif;color:var(--gold)}.quote-meta small{font:9px Arial,sans-serif;letter-spacing:2px;color:var(--muted)}.player-state{border-radius:0 3px 3px 0}.player-poster{padding:10px;border:1px solid var(--line);background:#15171b;border-radius:6px}.player-poster .ep-name{font-size:18px}.player-poster .eyebrow{margin-bottom:6px}.player-tools{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.server-select{background:#23252a;color:#eee5e8;border:1px solid #494c54;padding:9px 10px;border-radius:3px;max-width:100%;font:12px Arial,sans-serif}.player-state{font:12px/1.6 Arial,sans-serif;color:#cbb9c2;padding:10px 12px;background:#15171b;border-left:3px solid #9e304c;margin-top:10px}.player-poster{display:flex;gap:14px;align-items:center;margin-bottom:14px}.player-poster img{width:76px;height:108px;object-fit:cover;border:1px solid #704052;border-radius:3px}.player-poster .ep-name{font-size:17px;line-height:1.35}.server-chip{display:inline-block;padding:5px 9px;border:1px solid #5c3546;color:#d8a7b4;font:10px Arial,sans-serif;margin:3px;border-radius:20px}.player-placeholder{padding:45px 16px;text-align:center;color:var(--muted);font:13px/1.7 Arial,sans-serif}.source-link{display:inline-block;color:#e9b6c2;text-decoration:none;border-bottom:1px solid #8e4258;padding-bottom:2px;font:12px Arial,sans-serif;margin:12px 0}.source-link:hover{color:white}.notice{border:1px solid #795d3d;background:#281f17;color:#d6c09e;padding:12px 14px;border-radius:4px;font:12px/1.6 Arial,sans-serif;margin:12px 0}.error{border-color:#8c3347;background:#2b151d;color:#f0b5c1}.loading{padding:50px 15px;text-align:center;color:var(--gold);letter-spacing:3px}.loading:after{content:"";display:block;width:26px;height:26px;border:2px solid #663347;border-top-color:var(--red2);border-radius:50%;margin:15px auto 0;animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}.empty{padding:40px 15px;text-align:center;color:var(--muted);border:1px dashed #493340;border-radius:4px;font:13px Arial,sans-serif}.diagrow{display:grid;grid-template-columns:minmax(120px,1fr) 70px minmax(0,2fr);gap:10px;padding:10px;border-bottom:1px solid var(--line);font:12px Arial,sans-serif;align-items:center}.ok{color:#8ccea2}.bad{color:#e18b9b}.muted{color:var(--muted);font:11px Arial,sans-serif}.footer{margin-top:55px;padding:20px 0;border-top:1px solid var(--line);color:#7e6d77;font:10px/1.7 Arial,sans-serif;letter-spacing:.5px}
@media(max-width:800px){.shell{grid-template-columns:1fr}.sidebar{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line);padding:8px 12px;display:flex;gap:4px;overflow-x:auto}.side-label,.side-note{display:none}.navbtn{width:auto;min-width:max-content;padding:9px 12px;margin:0;font-size:12px}.navbtn .ico{width:auto;margin-right:5px}.main{padding-top:18px}.topbar{flex-wrap:wrap;gap:11px}.searchbar{order:3;max-width:none}.status{margin-left:auto}.hero{padding:21px 18px}.grid{grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:11px}.detail-grid{grid-template-columns:120px minmax(0,1fr);gap:14px}.detail-poster{max-height:220px}.content-card{padding:13px}}
@media(max-width:420px){.brandname{font-size:15px}.sigil{width:34px;height:34px}.detail-grid{grid-template-columns:1fr}.detail-poster{width:145px;max-height:220px}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.diagrow{grid-template-columns:1fr 55px}.diagrow .diag-note{grid-column:1/-1}}
.feature-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}.feature-tile{background:#24262b;border:1px solid #44474e;padding:16px;border-radius:5px}.feature-tile h3{margin:0 0 7px;font-weight:normal;color:#f0dce4}.feature-tile p{font:12px/1.55 Arial,sans-serif;color:var(--muted)}.field{background:#120e13;border:1px solid var(--line);color:var(--text);padding:10px;border-radius:3px;max-width:100%}.library-row{display:flex;gap:13px;align-items:center;padding:12px;border-bottom:1px solid var(--line)}.library-row img{width:58px;height:78px;object-fit:cover}.library-row .grow{flex:1;min-width:0}.library-row select,.library-row input{max-width:150px}.tool-panel{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.tool-panel textarea{width:100%;min-height:110px}.stat-number{font-size:25px;color:var(--gold)}
@media(max-width:800px){.shell{grid-template-columns:1fr}.sidebar{position:relative;top:0;height:auto;display:flex;overflow-x:auto;gap:4px;padding:8px;border-right:0;border-bottom:1px solid var(--line)}.side-label,.side-note{display:none}.navbtn{white-space:nowrap;width:auto;min-width:max-content;padding:9px}.main{padding-top:18px}.topbar{flex-wrap:wrap}.status{display:none}}

.auth-gate{position:fixed;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center;padding:22px;background:radial-gradient(ellipse at 50% 0%,#383a4055,transparent 55%),#0c0d0f;}
.auth-card{width:min(100%,440px);padding:28px;background:linear-gradient(155deg,#222429,#15171b);border:1px solid #474a51;border-radius:10px;box-shadow:0 26px 80px #000b;}
.auth-mark{width:82px;height:82px;margin:0 auto 18px;display:grid;place-items:center;border:1px solid #555960;border-radius:50%;overflow:hidden;background:#050505;box-shadow:0 0 26px #ffffff18}.auth-mark img{width:100%;height:100%;object-fit:cover}
.auth-card .eyebrow{text-align:center}.auth-card h1{text-align:center;font-size:25px;font-weight:normal;margin:9px 0}.auth-card p{text-align:center;color:var(--muted);font:12px/1.7 Arial,sans-serif;margin:0 0 18px}.auth-card label{display:block;font:11px Arial,sans-serif;color:#c6c7cb;margin:12px 0 6px}.auth-card input{width:100%;background:#111317;border:1px solid #41444a;color:var(--text);border-radius:5px;padding:12px;outline:none}.auth-card input:focus,.field:focus{border-color:var(--red2)}.auth-card .auth-actions{display:flex;gap:10px;margin-top:18px}.auth-card .auth-actions button{flex:1}.auth-switch{display:block;margin:16px auto 0;background:none;border:0;color:#d38a9c;text-decoration:underline;font:12px Arial,sans-serif}.auth-error{display:none;color:#f0a6b3;background:#2a1b21;border:1px solid #623443;border-radius:4px;padding:9px 10px;font:12px/1.5 Arial,sans-serif;margin-top:12px}
.account-actions{display:flex;align-items:center;gap:7px}.account-actions button{white-space:nowrap}.profile-cover{height:170px;border-radius:7px;border:1px solid #45484f;background:linear-gradient(110deg,#34373d,#202226 60%,#17191d);background-size:cover;background-position:center;position:relative;overflow:hidden}.profile-cover:after{content:"";position:absolute;inset:0;background:linear-gradient(0deg,#0a0b0dcc,transparent 75%)}.profile-head{display:flex;align-items:end;gap:14px;margin:-46px 0 16px 18px;position:relative;z-index:1}.avatar-preview{width:90px;height:90px;flex:0 0 90px;border:3px solid #1a1c20;border-radius:50%;background:linear-gradient(145deg,#383a40,#17191d);display:grid;place-items:center;color:#d8d9dd;font-size:30px;overflow:hidden}.avatar-preview img{width:100%;height:100%;object-fit:cover}.profile-name{padding-bottom:7px}.profile-name h2{font-size:20px;font-weight:normal;margin:0 0 4px}.profile-name small{font:10px Arial,sans-serif;color:var(--muted);letter-spacing:1px}.profile-form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.profile-form-grid .field{width:100%}.profile-upload{padding:14px;background:#15171b;border:1px solid var(--line);border-radius:6px}.profile-upload label{display:block;color:#d6d7da;font:12px Arial,sans-serif;margin-bottom:8px}.profile-upload input{max-width:100%;font:11px Arial,sans-serif;color:var(--muted)}.hero.has-custom-media{border-color:#555960}.hero.has-custom-media h1,.hero.has-custom-media p,.hero.has-custom-media .eyebrow{text-shadow:0 2px 12px #000}
@media(max-width:620px){.profile-form-grid{grid-template-columns:1fr}.profile-cover{height:140px}.auth-card{padding:22px}.account-actions{width:100%;justify-content:flex-end}.topbar{align-items:flex-start}}

/* Final banner composition override */
.hero:after{content:"";right:5%;top:50%;width:clamp(170px,23vw,290px);height:clamp(170px,23vw,290px);border:1px solid #c36a8138;border-radius:50%;background:radial-gradient(circle at center,transparent 52%,#a33c5922 53%,transparent 54%),repeating-conic-gradient(from 12deg,#c8788d28 0 1deg,transparent 1deg 19deg);transform:translateY(-50%);animation:nocturneOrbitSpin 54s linear infinite,nocturneSigilGlow 8s ease-in-out infinite;}
@keyframes nocturneOrbitSpin{to{transform:translateY(-50%) rotate(360deg)}}
</style>
</head>
<body>
<div class="app" id="appRoot">
<header class="topbar">
  <div class="brand"><div class="sigil"><img src="/assets/kolom/nocturne-logo.png" alt="NOCTURNE"></div><div class="brandname">NOCTURNE<small>ANIME STREAMING ARCHIVE</small></div></div>
  <form class="searchbar" id="searchForm"><input id="searchInput" type="search" autocomplete="off" placeholder="Cari judul anime..."><button type="submit">CARI</button></form>
  <div class="account-actions"><div class="status" id="statusText">SHADOW INDEX · LOCAL</div><button class="action" id="accountButton" style="display:none" onclick="loadTab('profile')">PROFIL</button><button class="action" id="logoutButton" style="display:none" onclick="logout()">KELUAR</button></div>
</header>
<div class="shell">
  <aside class="sidebar">
    <div class="side-label">NAVIGASI</div>
    <button class="navbtn active" data-tab="home" onclick="loadTab('home',this)"><span class="ico">⌂</span> Beranda</button>
    <button class="navbtn" data-tab="ongoing" onclick="loadTab('ongoing',this)"><span class="ico">◈</span> Ongoing</button>
    <button class="navbtn" data-tab="schedule" onclick="loadTab('schedule',this)"><span class="ico">☾</span> Jadwal Rilis</button>
    <button class="navbtn" data-tab="anime-list" onclick="loadTab('anime-list',this)"><span class="ico">✧</span> Daftar Anime</button>
    <button class="navbtn" data-tab="genres" onclick="loadTab('genres',this)"><span class="ico">❖</span> Genre</button>
    <button class="navbtn" data-tab="mal" onclick="loadTab('mal',this)"><span class="ico">✥</span> MyAnimeList</button>
    <button class="navbtn" data-tab="multiverse" onclick="loadTab('multiverse',this)"><span class="ico">✧</span> Multi-Source Search</button>
    <button class="navbtn" data-tab="manga" onclick="loadTab('manga',this)"><span class="ico">▤</span> Manga Reader</button>
    <button class="navbtn" data-tab="discover" onclick="loadTab('discover',this)"><span class="ico">♛</span> Discovery Hub</button>
    <button class="navbtn" data-tab="news" onclick="loadTab('news',this)"><span class="ico">☷</span> Anime News</button>
    <button class="navbtn" data-tab="wallpapers" onclick="loadTab('wallpapers',this)"><span class="ico">▧</span> Anime Wallpapers</button>
    <button class="navbtn" data-tab="quotes" onclick="loadTab('quotes',this)"><span class="ico">❝</span> Anime Quotes</button>
    <button class="navbtn" data-tab="airing" onclick="loadTab('airing',this)"><span class="ico">◷</span> Airing Calendar</button>
    <button class="navbtn" data-tab="top" onclick="loadTab('top',this)"><span class="ico">♛</span> MAL Top Anime</button>
    <button class="navbtn" data-tab="season" onclick="loadTab('season',this)"><span class="ico">☀</span> Musim Ini</button>
    <div class="side-label">APP EXPERIENCE</div>
    <button class="navbtn" data-tab="appearance" onclick="loadTab('appearance',this)"><span class="ico">◈</span> Tampilan Beranda</button>
    <div class="side-label">PERSONAL ARCHIVE</div>
    <button class="navbtn" data-tab="profile" onclick="loadTab('profile',this)"><span class="ico">♙</span> Profil Saya</button>
    <button class="navbtn" data-tab="library" onclick="loadTab('library',this)"><span class="ico">♡</span> Koleksi Saya</button>
    <button class="navbtn" data-tab="history" onclick="loadTab('history',this)"><span class="ico">◷</span> Riwayat Tonton</button>
    <button class="navbtn" data-tab="tools" onclick="loadTab('tools',this)"><span class="ico">⚙</span> Tools & Backup</button>
    <button class="navbtn" data-tab="diagnose" onclick="loadTab('diagnose',this)"><span class="ico">⌬</span> Cek Endpoint</button>
    <button class="navbtn" data-tab="providers" onclick="loadTab('providers',this)"><span class="ico">◉</span> API Monitor</button>
    <div class="side-note">NOCTURNE · STREAMING ANIME<br>Charcoal / Grey Edition<br><br>Profil dan akses tersimpan lokal di perangkat.</div>
  </aside>
  <main class="main" id="mainContent" aria-live="polite">
    <div class="hero" id="homeHero"><div class="hero-media" id="homeHeroMedia" aria-hidden="true"></div><div class="hero-shade"></div><div class="hero-content"><div class="eyebrow">THE MIDNIGHT ANIME INDEX</div><h1>Selamat datang di NOCTURNE</h1><p>Jelajahi katalog anime, pilih episode, lalu tekan PLAY untuk membuka pemutar.</p></div></div>
    <div class="loading">MEMANGGIL ARSIP</div>
  </main>
</div>
</div>

<div class="auth-gate" id="authGate">
  <section class="auth-card">
    <div class="auth-mark"><img src="/assets/kolom/nocturne-logo.png" alt="NOCTURNE"></div>
    <div class="eyebrow">NOCTURNE · PRIVATE LOCAL ACCESS</div>
    <h1 id="authTitle">Masuk ke NOCTURNE</h1>
    <p id="authDescription">Masuk menggunakan akun lokal yang sudah dibuat di perangkat ini.</p>
    <form id="authForm" autocomplete="on">
      <label for="authUsername">Username</label>
      <input id="authUsername" name="username" minlength="3" maxlength="24" autocomplete="username" autocapitalize="none" required>
      <label for="authPassword">Password</label>
      <input id="authPassword" name="password" type="password" minlength="8" autocomplete="current-password" required>
      <div id="passwordConfirmWrap" style="display:none"><label for="authPasswordConfirm">Konfirmasi password</label><input id="authPasswordConfirm" type="password" minlength="8" autocomplete="new-password"></div>
      <div id="authError" class="auth-error" role="alert"></div>
      <div class="auth-actions"><button id="authSubmit" class="action primary" type="submit">MASUK</button></div>
    </form>
    <button id="authSwitch" class="auth-switch" type="button" onclick="toggleAuthMode()">Belum punya akun? Buat akun</button>
  </section>
</div>

<script>
const main = document.getElementById('mainContent');
const statusText = document.getElementById('statusText');
let currentTab = 'home';
let backStack = [];
let ongoingPage = 1;
let currentPlayerUrl = ''; 
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function jsArg(v){return encodeURIComponent(String(v??'')).replace(/[!'()*]/g,c=>'%'+c.charCodeAt(0).toString(16));}
function rememberCurrent(){backStack.push({tab:currentTab,html:main.innerHTML});}
function setLoading(label='MEMANGGIL ARSIP'){main.innerHTML=`<div class="loading">${esc(label)}</div>`;}
async function api(path,timeoutMs=15000){const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),timeoutMs);try{const r=await fetch(path,{headers:{'Accept':'application/json'},signal:controller.signal});let d;try{d=await r.json()}catch(e){throw new Error('Respons server tidak dapat dibaca.')}if(!r.ok||!d.ok)throw new Error(d?.error||`HTTP ${r.status}`);return d;}catch(e){if(e.name==='AbortError')throw new Error(`Permintaan melewati batas waktu ${Math.round(timeoutMs/1000)} detik. Periksa koneksi atau status provider.`);throw e;}finally{clearTimeout(timer);}}
function setTabActive(tab){document.querySelectorAll('.navbtn').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));}
function itemGrid(items,sub=''){if(!items||!items.length)return '<div class="empty">Tidak ada data yang ditemukan dari halaman sumber.</div>';return `<div class="grid">${items.map(x=>`<article class="card" onclick="openAnime(decodeURIComponent('${jsArg(x.url)}'))"><div class="poster">${x.image?`<img loading="lazy" referrerpolicy="no-referrer" src="${esc(x.image)}" alt="${esc(x.title)}" onerror="this.style.display='none';this.parentElement.innerHTML='<div class=\'noimg\'>✧</div>'">`:'<div class="noimg">✧</div>'}</div><div class="cardbody"><div class="cardtitle">${esc(x.title)}</div><div class="cardsub">${esc(x.subtitle||sub||'OTAKUDESU')}</div></div></article>`).join('')}</div>`;}
function sectionHead(title,sub='ARCHIVE'){return `<div class="section-head"><h2>${esc(title)}</h2><small>${esc(sub)}</small></div>`;}
function showError(err){main.innerHTML=`<div class="hero"><div class="eyebrow">ARSIP TIDAK DAPAT DIAKSES</div><h1>Connection interrupted</h1><p>${esc(err.message||err)}</p></div><div class="notice error">Periksa koneksi internet, coba lagi, atau jalankan menu <b>Cek Endpoint</b>. Jika server sumber mengembalikan 403/anti-bot, aplikasi tidak akan mencoba melewati proteksi tersebut.</div><div class="toolbar"><button class="action" onclick="loadTab('${esc(currentTab)}')">Coba lagi</button><button class="action" onclick="loadTab('diagnose')">Cek endpoint</button></div>`;}
async function loadTab(tab,btn=null){if(tab!=='providers'&&window.nocturneTelemetryTimer){clearInterval(window.nocturneTelemetryTimer);window.nocturneTelemetryTimer=null;}if(tab!=='home'&&activeHomeBannerUrl){URL.revokeObjectURL(activeHomeBannerUrl);activeHomeBannerUrl='';}if(tab!=='appearance'&&appearancePreviewUrl){URL.revokeObjectURL(appearancePreviewUrl);appearancePreviewUrl='';}currentTab=tab;backStack=[];setTabActive(tab);if(btn)setTabActive(tab);statusText.textContent='FETCHING · '+tab.toUpperCase();setLoading();try{
 if(tab==='home'){const d=await api('/api/home',40000);main.innerHTML=`<div class="hero" id="homeHero"><div class="hero-media" id="homeHeroMedia" aria-hidden="true"></div><div class="hero-shade"></div><div class="hero-content"><div class="eyebrow">THE MIDNIGHT ANIME INDEX</div><h1>Selamat datang di NOCTURNE</h1><p>APK streaming berbasis scraper dan API. Pilih poster untuk membuka detail judul dan daftar episode.</p></div></div>${sectionHead('Rilisan dari Beranda','LATEST FROM SOURCE')}${itemGrid(d.items)}`;applyHomeBanner();}
 else if(tab==='ongoing'){ongoingPage=1;await loadOngoing(1,false);}
 else if(tab==='anime-list'){const d=await api('/api/catalog?section=anime-list',30000);renderAnimeList(d);}
 else if(tab==='schedule'){const d=await api('/api/schedule');main.innerHTML=`${sectionHead('Jadwal Rilis','WEEKLY SCHEDULE')}${d.groups?.length?d.groups.map(g=>`<h3 class="daytitle">☾ ${esc(g.day)}</h3><div class="list">${g.items.map(x=>`<button class="listbtn" onclick="openAnime(decodeURIComponent('${jsArg(x.url)}'))">✧ &nbsp;${esc(x.title)}</button>`).join('')}</div>`).join(''):'<div class="empty">Jadwal tidak ditemukan.</div>'}`;}
 else if(tab==='genres'){const d=await api('/api/genres',25000);renderGenreList(d);}
 else if(tab==='mal'){renderMalSearch();}
 else if(tab==='multiverse'){renderMultiverseSearch();}
 else if(tab==='manga'){renderMangaSearch();}
 else if(tab==='discover'){await loadMultiverseDiscover();}
 else if(tab==='news'){await loadAnimeNews();}
 else if(tab==='wallpapers'){await renderAnimeWallpapers();}
 else if(tab==='quotes'){await renderAnimeQuotes();}
 else if(tab==='airing'){await loadAiringCalendar();}
 else if(tab==='providers'){await loadProviderStatus();}
 else if(tab==='top'){await loadMalTop();}
 else if(tab==='season'){await loadSeasonNow();}
 else if(tab==='appearance'){await renderAppearance();}
 else if(tab==='profile'){renderProfile();}
 else if(tab==='library'){await loadLibrary();}
 else if(tab==='history'){await loadHistory();}
 else if(tab==='tools'){renderTools();}
 else if(tab==='diagnose'){await loadDiagnostics();}
 }catch(e){showError(e);}finally{statusText.textContent='SHADOW INDEX · '+(tab==='diagnose'?'DIAGNOSTICS':'CONNECTED');}}

function renderAnimeList(d){
 const groups=Array.isArray(d.groups)?d.groups:[];
 const safeGroups=groups.map((g,i)=>({label:String(g.label||'#'),items:Array.isArray(g.items)?g.items:[],id:'animeGroup'+i})).filter(g=>g.items.length);
 const index=safeGroups.map(g=>`<a href="#${g.id}" aria-label="Lompat ke ${esc(g.label)}">${esc(g.label)}</a>`).join('');
 const sections=safeGroups.map(g=>`<section class="anime-group" id="${g.id}" data-letter="${esc(g.label)}"><div class="anime-group-head"><h3>${esc(g.label)}</h3><span>${g.items.length} JUDUL</span></div><div class="anime-rows">${g.items.map(x=>`<button class="anime-row" type="button" data-title="${esc(x.title)}" onclick="openAnime(decodeURIComponent('${jsArg(x.url)}'))">${esc(x.title)}</button>`).join('')}</div></section>`).join('');
 main.innerHTML=`${sectionHead('Daftar Anime','OTAKUDESU · ALPHABETICAL INDEX')}<div class="genre-tools"><span class="pill">${Number(d.total||d.items?.length||0)} JUDUL</span><span class="muted">Tampilan daftar teks seperti halaman sumber</span></div><input class="field anime-filter" id="animeListFilter" type="search" placeholder="Cari judul anime..." autocomplete="off"><nav class="anime-index" aria-label="Indeks huruf">${index}</nav><div id="animeListSections">${sections||'<div class="empty">Daftar anime tidak berhasil dibaca dari halaman sumber.</div>'}</div><div class="notice">Daftar ini dibaca dari halaman publik OtakuDesu Anime List. Pilih judul untuk membuka detail dan episode.</div>`;
 const input=document.getElementById('animeListFilter');
 input.addEventListener('input',()=>{const q=input.value.trim().toLocaleLowerCase();let visible=0;document.querySelectorAll('#animeListSections .anime-group').forEach(group=>{let count=0;group.querySelectorAll('.anime-row').forEach(row=>{const match=!q||row.dataset.title.toLocaleLowerCase().includes(q);row.style.display=match?'flex':'none';if(match)count++;});group.style.display=count?'block':'none';const counter=group.querySelector('.anime-group-head span');if(counter)counter.textContent=count+' JUDUL';visible+=count;});document.querySelectorAll('.anime-index a').forEach(link=>{const target=document.querySelector(link.getAttribute('href'));link.style.display=target&&target.style.display!=='none'?'':'none';});let status=document.getElementById('animeFilterStatus');if(status)status.textContent=visible+' judul cocok';});
 const tools=document.querySelector('.genre-tools');if(tools){const span=document.createElement('span');span.id='animeFilterStatus';span.className='muted';span.textContent='';tools.appendChild(span);}
}
function renderGenreList(d){
 const genres=(Array.isArray(d.genres)?d.genres:[]).filter(g=>g&&g.title&&g.url);
 main.innerHTML=`${sectionHead('Genre Anime','OTAKUDESU · GENRE INDEX')}<div class="genre-tools"><span class="pill">${genres.length} GENRE</span><span class="muted">Pilih genre untuk melihat daftar anime.</span></div><input class="field anime-filter" id="genreFilter" type="search" placeholder="Cari genre..." autocomplete="off"><div class="genre-cloud" id="genreCloud">${genres.map(g=>`<button class="genre-chip" type="button" data-genre="${esc(g.title.toLocaleLowerCase())}" onclick="openGenre(decodeURIComponent('${jsArg(g.url)}'))">${esc(g.title)}</button>`).join('')}</div><div class="empty" id="genreNoResults" style="display:none">Genre tidak ditemukan.</div><div class="notice">Daftar genre dibaca dari halaman publik OtakuDesu Genre List.</div>`;
 const input=document.getElementById('genreFilter');input.addEventListener('input',()=>{const q=input.value.trim().toLocaleLowerCase();let count=0;document.querySelectorAll('#genreCloud .genre-chip').forEach(chip=>{const match=!q||chip.dataset.genre.includes(q);chip.style.display=match?'inline-flex':'none';if(match)count++;});document.getElementById('genreNoResults').style.display=count?'none':'block';});
}
function closeWallpaperPreview(){const old=document.getElementById('wallpaperLightbox');if(old)old.remove();document.body.style.overflow='';}
function openWallpaperPreview(url,title,source){
 if(!/^https:\/\//i.test(url)){alert('URL gambar tidak valid.');return;}
 closeWallpaperPreview();
 const overlay=document.createElement('div');overlay.className='wallpaper-lightbox';overlay.id='wallpaperLightbox';
 const filename=(String(title||'NOCTURNE-wallpaper').normalize('NFKD').replace(/[^A-Za-z0-9_-]+/g,'_').replace(/^_+|_+$/g,'').slice(0,64)||'NOCTURNE-wallpaper')+'.jpg';
 const downloadUrl='/api/wallpapers/download?url='+encodeURIComponent(url)+'&name='+encodeURIComponent(filename.replace(/\.jpg$/i,''));
 overlay.innerHTML=`<button class="wallpaper-lightbox-close" type="button" aria-label="Tutup" onclick="closeWallpaperPreview()">×</button><div class="wallpaper-lightbox-inner"><img src="${esc(url)}" alt="${esc(title)}" referrerpolicy="no-referrer"><div class="wallpaper-lightbox-error" style="display:none">Gambar tidak dapat ditampilkan dari sumber. Coba wallpaper lain.</div><div class="wallpaper-lightbox-bar"><div class="wallpaper-lightbox-title">${esc(title||'Anime wallpaper')}</div><a class="action primary" href="${downloadUrl}" download="${esc(filename)}">↓ Download wallpaper</a><a class="action" href="${esc(source||'https://www.pinterest.com/')}" target="_blank" rel="noopener noreferrer">Sumber ↗</a><button class="action" type="button" onclick="closeWallpaperPreview()">Tutup</button></div></div>`;
 overlay.addEventListener('click',event=>{if(event.target===overlay)closeWallpaperPreview();});
 const image=overlay.querySelector('img');image.addEventListener('error',()=>{image.style.display='none';overlay.querySelector('.wallpaper-lightbox-error').style.display='block';});
 document.body.appendChild(overlay);document.body.style.overflow='hidden';
}
document.addEventListener('keydown',event=>{if(event.key==='Escape')closeWallpaperPreview();});

async function renderAnimeWallpapers(query=''){
 main.innerHTML=`<div class="hero"><div class="eyebrow">NOCTURNE · VISUAL ARCHIVE</div><h1>Anime Wallpapers</h1><p>Cari wallpaper karakter anime melalui Pinterest. Hasil gambar ditampilkan dari URL provider, dengan tautan sumber jika tersedia.</p></div><form class="toolbar" id="wallpaperSearchForm"><input class="field" id="wallpaperQuery" style="flex:1;min-width:170px" maxlength="80" placeholder="Contoh: Naruto, Frieren, Gojo..." value="${esc(query)}"><button class="action primary">Cari Wallpaper</button><button class="action" type="button" id="wallpaperRefresh">Acak Ulang</button></form><div id="wallpaperResults"><div class="loading">MEMANGGIL GALERI VISUAL</div></div><div class="notice">Sumber: Bintang API → Pinterest. Gunakan kata kunci aman. Hak cipta gambar tetap milik pemilik aslinya; NOCTURNE hanya menampilkan hasil dan atribusi yang tersedia.</div>`;
 const form=document.getElementById('wallpaperSearchForm');
 form.addEventListener('submit',async e=>{e.preventDefault();await loadAnimeWallpapers(document.getElementById('wallpaperQuery').value.trim());});
 document.getElementById('wallpaperRefresh').addEventListener('click',async()=>loadAnimeWallpapers(document.getElementById('wallpaperQuery').value.trim(),true));
 await loadAnimeWallpapers(query||'anime wallpaper');
}
async function loadAnimeWallpapers(query='anime wallpaper',refresh=false){
 const box=document.getElementById('wallpaperResults');if(!box)return;box.innerHTML='<div class="loading">MENGAMBIL HASIL PINTEREST</div>';
 try{
  const d=await api('/api/wallpapers?count=12&q='+encodeURIComponent(query)+(refresh?'&refresh=1':''),24000);
  const cards=(d.items||[]).map(x=>`<article class="card wallpaper-card"><img class="wallpaper-thumb" loading="lazy" referrerpolicy="no-referrer" src="${esc(x.thumbnail||x.url)}" alt="${esc(x.title||'Anime wallpaper')}" onclick="openWallpaperPreview(decodeURIComponent('${jsArg(x.url)}'),decodeURIComponent('${jsArg(x.title||'Anime wallpaper')}'),decodeURIComponent('${jsArg(x.source_url||'https://www.pinterest.com/')}'))" onerror="this.style.opacity='.25';this.alt='Gambar tidak tersedia'"><div class="cardbody"><div class="cardtitle">${esc(x.title||'Anime wallpaper')}</div><div class="wallpaper-actions"><button class="action" type="button" onclick="openWallpaperPreview(decodeURIComponent('${jsArg(x.url)}'),decodeURIComponent('${jsArg(x.title||'Anime wallpaper')}'),decodeURIComponent('${jsArg(x.source_url||'https://www.pinterest.com/')}'))">Lihat</button><a class="action" href="/api/wallpapers/download?url=${encodeURIComponent(x.url)}&amp;name=${encodeURIComponent(String(x.title||'NOCTURNE-wallpaper').replace(/[^A-Za-z0-9_-]+/g,'_').slice(0,50)||'NOCTURNE-wallpaper')}" download="${esc((String(x.title||'NOCTURNE-wallpaper').replace(/[^A-Za-z0-9_-]+/g,'_').slice(0,50)||'NOCTURNE-wallpaper')+'.jpg')}">↓ Simpan</a></div><div class="cardsub">PINTEREST · <a href="${esc(x.source_url||'https://www.pinterest.com/')}" target="_blank" rel="noopener noreferrer" style="color:#d8a7b4">SUMBER</a></div></div></article>`).join('');
  box.innerHTML=`<div class="toolbar"><span class="pill">${(d.items||[]).length} WALLPAPERS</span><span class="pill">${esc(d.provider||'Bintang API · Pinterest')}</span><span class="muted">Ketuk gambar untuk pratinjau layar penuh.</span></div>${cards?`<div class="grid">${cards}</div>`:'<div class="empty">Belum ada wallpaper untuk kata kunci ini.</div>'}`;
 }catch(e){box.innerHTML=`<div class="notice error">Wallpaper belum dapat dimuat: ${esc(e.message||e)}<br>Periksa koneksi atau coba kata kunci anime lain.</div>`;}
}

async function renderAnimeQuotes(){
 main.innerHTML=`<div class="hero"><div class="eyebrow">NOCTURNE · CHARACTER ARCHIVE</div><h1>Anime Quotes</h1><p>Kutipan acak dari karakter anime, lengkap dengan nama karakter dan judul serialnya.</p></div><section class="content-card quote-card"><div class="eyebrow">RANDOM CHARACTER QUOTE</div><div id="animeQuoteText" class="quote-text">MEMANGGIL KUTIPAN...</div><div id="animeQuoteMeta" class="quote-meta"></div><div class="toolbar" style="margin-top:20px"><button class="action primary" id="nextAnimeQuote" type="button">❝ Kutipan berikutnya</button></div></section><div class="notice">Provider: Animechan API. Kutipan ditampilkan sebagai cuplikan pendek dan diatribusikan ke karakter/serial yang dikembalikan provider. Ketersediaan mengikuti layanan pihak ketiga.</div>`;
 document.getElementById('nextAnimeQuote').addEventListener('click',()=>loadAnimeQuote(true));
 await loadAnimeQuote(false);
}
async function loadAnimeQuote(refresh=true){
 const quoteBox=document.getElementById('animeQuoteText');const metaBox=document.getElementById('animeQuoteMeta');if(!quoteBox||!metaBox)return;
 quoteBox.textContent='MENGAMBIL KUTIPAN DARI ARSIP...';metaBox.textContent='';
 try{
  const d=await api('/api/quotes/random'+(refresh?'?refresh=1':''),15000);const q=d.quote||{};
  quoteBox.textContent='“'+String(q.content||'Kutipan tidak tersedia.')+'”';
  metaBox.innerHTML=`<strong>${esc(q.character||'Unknown character')}</strong><span>${esc(q.anime||'Anime')}</span><small>SUMBER · ${esc(d.provider||'Animechan')}</small>`;
 }catch(e){quoteBox.textContent='Kutipan belum dapat dimuat.';metaBox.innerHTML=`<span class="bad">${esc(e.message||e)}</span><button class="action" type="button" onclick="loadAnimeQuote(true)">Coba lagi</button>`;}
}

function renderMultiverseSearch(){
 main.innerHTML=`<div class="hero"><div class="eyebrow">NOCTURNE MULTIVERSE · CROSS DATABASE</div><h1>Telusuri lima arsip anime</h1><p>Pencarian paralel ke AniList, MyAnimeList/Tenrai, MiteNime, AniMap, dan Anime News Network. Tiap sumber berjalan mandiri; kegagalan satu provider tidak menghapus hasil provider lain.</p></div><form class="toolbar" id="multiSearchForm"><input class="field" id="multiQuery" style="flex:1;min-width:190px" placeholder="Contoh: Frieren, One Piece, Jujutsu Kaisen..." required><button class="action primary">Cari Semua Sumber</button></form><div id="multiResults"><div class="empty">Masukkan judul untuk memulai pencarian multi-sumber.</div></div><div class="notice">Data dikutip dari penyedia masing-masing. AniList memiliki batas dan ketentuan penggunaan tersendiri, termasuk pembatasan aplikasi tracker yang bersaing; periksa Terms sebelum memakai atau mendistribusikan integrasi tersebut. NOCTURNE hanya melakukan pencarian langsung dan cache sementara, bukan mengarsipkan massal. Anime News Network ditautkan dan diberi atribusi.</div>`;
 document.getElementById('multiSearchForm').addEventListener('submit',async e=>{e.preventDefault();await searchMultiverse(document.getElementById('multiQuery').value);});
}
async function searchMultiverse(q){
 const box=document.getElementById('multiResults');if(!box)return;box.innerHTML='<div class="loading">MENGHUBUNGI 5 ARSIP · MOHON TUNGGU MAKSIMAL 18 DETIK</div>';
 try{const d=await api('/api/multiverse/search?q='+encodeURIComponent(q),24000);let html=`<div class="toolbar"><span class="pill">${Number(d.total)} HASIL</span><span class="pill">${Number(d.provider_count)}/5 PROVIDER MERESPONS</span></div>`;
 for(const [name,p] of Object.entries(d.providers||{})){html+=`<div class="section-head"><h2>${esc(name)}</h2><small>${p.ok?`${p.count} RESULTS`:'UNAVAILABLE'}</small></div>`;
 if(!p.ok){html+=`<div class="notice error">${esc(p.error||'Sumber tidak tersedia')}</div>`;continue;}
 if(!p.items?.length){html+='<div class="empty">Tidak ada hasil di sumber ini.</div>';continue;}
 html+=`<div class="grid">${p.items.map(x=>{const provider=esc(x.provider_key||'');const id=esc(String(x.provider_id??''));const title=esc(x.title||'Untitled');const image=x.image?`<img loading="lazy" referrerpolicy="no-referrer" src="${esc(x.image)}" alt="${title}" onerror="this.style.display='none'">`:'<div class="noimg">✧</div>';const meta=x.score?`★ ${esc(x.score)}`:esc(x.subtitle||'Catalog record');return `<article class="card" onclick="openMultiDetail('${provider}','${jsArg(id)}','${jsArg(title)}')"><div class="poster">${image}</div><div class="cardbody"><div class="cardtitle">${title}</div><div class="cardsub">${meta}</div><div class="muted">${esc(x.year||'')} · ${esc(x.provider||name)}</div></div></article>`;}).join('')}</div>`;
 }
 box.innerHTML=html;
 }catch(e){box.innerHTML=`<div class="notice error">${esc(e.message)}</div>`;}
}
async function openMultiDetail(provider,id,title){
 title=decodeURIComponent(title);rememberCurrent();setLoading('MEMBUKA DOSSIER MULTI-SUMBER');
 try{
  const res=await api('/api/multiverse/detail?provider='+encodeURIComponent(provider)+'&id='+encodeURIComponent(decodeURIComponent(id)),18000);
  const d=res.data||{};let entries=[];let heading=title;let synopsis='';let image='';let external='';
  if(provider==='anilist'){
   const t=d.title||{};heading=t.english||t.romaji||title;synopsis=d.description||'';image=d.coverImage?.extraLarge||d.coverImage?.large||'';external=d.siteUrl||'';
   entries=[['Skor AniList',d.averageScore],['Popularity',d.popularity],['Format',d.format],['Status',d.status],['Episode',d.episodes],['Season',`${d.season||''} ${d.seasonYear||''}`],['Studios',(d.studios?.nodes||[]).map(x=>x.name).join(', ')],['Genres',(d.genres||[]).join(', ')],['MAL ID',d.idMal],['Favourites',d.favourites]];
  }else if(provider==='mitenime'){
   const a=d.attributes||d;heading=a.title||a.name||title;synopsis=a.synopsis||a.description||'';image=a.poster||a.image||a.cover||'';external='https://mitenime.org/';
   entries=[['Rating',a.rating],['Status',a.status],['Type',a.type],['Studio',a.studio],['Season',a.season],['Year',a.year],['Genres',Array.isArray(a.genres)?a.genres.join(', '):a.genres]];
  }else if(provider==='tenrai'){
   heading=d.title||title;synopsis=d.synopsis||'';image=d.images?.jpg?.large_image_url||'';external=d.url||'';
   entries=[['Score',d.score],['Rank',d.rank],['Popularity',d.popularity],['Episodes',d.episodes],['Status',d.status],['Studio',(d.studios||[]).map(x=>x.name).join(', ')],['Genres',(d.genres||[]).map(x=>x.name).join(', ')]];
  }else if(provider==='animap'){
   heading=d.title||title;external='https://animap.id/';
   entries=[['MAL IDs',(d.mal_id||[]).join(', ')],['AniList IDs',(d.anilist_id||[]).join(', ')],['ANN IDs',(d.ann_id||[]).join(', ')],['AniDB IDs',(d.anidb_id||[]).join(', ')],['Year',d.year],['Synonyms',(d.synonyms||[]).join(', ')]];
  }else if(provider==='ann'){
   heading='Anime News Network · '+title;external=d.url||'';entries=[['ANN ID',d.id],['Source','Anime News Network Encyclopedia']];
  }
  const meta=entries.filter(x=>x[1]!==undefined&&x[1]!==null&&x[1]!=='').map(([k,v])=>`<div><b>${esc(k)}</b>${esc(v)}</div>`).join('');
  let cast='';
  if(provider==='anilist'&&d.characters?.edges){
   cast=`${sectionHead('Characters & Voice Actors','ANILIST CAST')}<div class="grid">${d.characters.edges.slice(0,18).map(e=>`<article class="card"><div class="poster">${e.node?.image?.large?`<img src="${esc(e.node.image.large)}" loading="lazy">`:'<div class="noimg">✧</div>'}</div><div class="cardbody"><div class="cardtitle">${esc(e.node?.name?.full||'Character')}</div><div class="cardsub">${esc(e.role||'')}</div><div class="muted">${esc((e.voiceActors||[]).slice(0,2).map(v=>v.name?.full).join(', '))}</div></div></article>`).join('')}</div>`;
  }
  let staff='';
  if(provider==='anilist'&&d.staff?.edges){staff=`${sectionHead('Production Staff','ANILIST STAFF')}<div class="list">${d.staff.edges.slice(0,24).map(e=>`<div class="listbtn"><b>${esc(e.node?.name?.full||'Staff')}</b><div class="muted">${esc(e.role||'')}</div></div>`).join('')}</div>`;}
  let rec='';
  if(provider==='anilist'&&d.recommendations?.nodes){
   const rs=d.recommendations.nodes.map(x=>x.mediaRecommendation).filter(Boolean);
   rec=`${sectionHead('Related & Recommended','ANILIST RECOMMENDATIONS')}<div class="grid">${rs.map(x=>`<article class="card" onclick="openMultiDetail('anilist','${Number(x.id)}','${jsArg(x.title?.english||x.title?.romaji||'Anime')}')"><div class="poster">${x.coverImage?.large?`<img src="${esc(x.coverImage.large)}" loading="lazy">`:'<div class="noimg">✧</div>'}</div><div class="cardbody"><div class="cardtitle">${esc(x.title?.english||x.title?.romaji||'Anime')}</div><div class="cardsub">★ ${esc(x.averageScore||'—')}</div></div></article>`).join('')}</div>`;
  }
  main.innerHTML=`<div class="toolbar"><button class="action" onclick="goBack()">← Kembali</button>${external?`<a class="source-link" href="${esc(external)}" target="_blank" rel="noopener noreferrer">Buka sumber ↗</a>`:''}</div><section class="content-card"><div class="detail-grid"><div>${image?`<img class="detail-poster" src="${esc(image)}" loading="lazy" referrerpolicy="no-referrer">`:'<div class="poster detail-poster noimg">✧</div>'}</div><div><div class="eyebrow">${esc(res.provider)} · CROSS-SOURCE DOSSIER</div><h1 class="detail-title">${esc(heading)}</h1><div class="meta">${meta}</div><h3>Sinopsis / Deskripsi</h3><p class="desc">${esc(synopsis||'Sinopsis tidak disediakan oleh sumber ini.')}</p><div class="notice">Metadata berasal dari ${esc(res.provider)}. Perbedaan judul, skor, jumlah episode, dan status antardatabase mungkin terjadi.</div></div></div></section>${cast}${staff}${rec}`;
  window.scrollTo({top:0,behavior:'smooth'});
 }catch(e){showError(e);}
}
async function loadMultiverseDiscover(){
 setLoading('MEMUAT DISCOVERY DARI DUA KATALOG');const d=await api('/api/multiverse/discover',22000);
 const makeGrid=(items)=>items?.length?`<div class="grid">${items.map(x=>`<article class="card" onclick="openMultiDetail('${esc(x.provider_key)}','${jsArg(x.provider_id)}','${jsArg(x.title)}')"><div class="poster">${x.image?`<img src="${esc(x.image)}" loading="lazy" referrerpolicy="no-referrer">`:'<div class="noimg">✧</div>'}</div><div class="cardbody"><div class="cardtitle">${esc(x.title)}</div><div class="cardsub">${x.score?'★ '+esc(x.score)+' · ':''}${esc(x.subtitle||'')}</div></div></article>`).join('')}</div>`:'<div class="empty">Tidak ada data untuk sumber ini.</div>';
 main.innerHTML=`<div class="hero"><div class="eyebrow">DISCOVERY HUB · TWO DATABASES</div><h1>Anime yang sedang ramai</h1><p>Penemuan lintas katalog dari AniList dan MyAnimeList melalui Tenrai. Perbedaan ranking dan skor mengikuti masing-masing sumber.</p><div class="toolbar"><button class="action primary" onclick="randomAnime()">✧ Acak Anime</button><button class="action" onclick="loadAiringCalendar()">◷ Kalender Tayang</button><button class="action" onclick="loadProviderStatus()">◉ Status API</button></div></div>${sectionHead('AniList Trending','COMMUNITY TRENDING')}${makeGrid(d.trending)}${sectionHead('MAL Popularity','MYANIMELIST / TENRAI')}${makeGrid(d.mal_top)}${Object.keys(d.errors||{}).length?`<div class="notice error">Beberapa sumber tidak merespons: ${esc(Object.keys(d.errors).join(', '))}. Sumber lainnya tetap ditampilkan.</div>`:''}`;
}
async function randomAnime(){setLoading('MENGAMBIL RANDOM ANIME');try{const r=await api('/api/multiverse/random',16000);const d=r.data||{};const id=Number(d.mal_id||0);if(id){await openMultiDetail('tenrai',String(id),d.title||'Random Anime');}else{main.innerHTML=`${sectionHead('Random Anime','MAL RANDOM ENTRY')}<div class="content-card"><h2>${esc(d.title||'Anime acak')}</h2><p class="desc">${esc(d.synopsis||'Sinopsis tidak tersedia.')}</p><a class="source-link" href="${esc(d.url||'https://myanimelist.net/anime.php')}" target="_blank" rel="noopener">Buka sumber ↗</a></div>`;}}catch(e){showError(e);}}
async function loadAiringCalendar(){setLoading('MEMUAT JADWAL PENAYANGAN');try{const d=await api('/api/multiverse/airing',18000);const items=d.items||[];main.innerHTML=`${sectionHead('Airing Calendar','ANILIST · UPCOMING EPISODES')}<div class="notice">Waktu tayang dikirim sebagai Unix timestamp dari AniList. Konversi ditampilkan mengikuti zona waktu browser/perangkat.</div>${items.length?`<div class="list">${items.map(x=>`<article class="content-card"><div class="library-row"><div class="grow"><b>${esc(x.media?.title?.english||x.media?.title?.romaji||'Anime')}</b><div class="muted">Episode ${esc(x.episode)} · ${new Date(Number(x.airingAt)*1000).toLocaleString()}</div><div class="muted">${Math.max(0,Math.floor(Number(x.timeUntilAiring||0)/3600))} jam lagi</div></div>${x.media?.coverImage?.large?`<img src="${esc(x.media.coverImage.large)}" loading="lazy">`:''}<button class="action" onclick="openMultiDetail('anilist','${Number(x.media?.id||0)}','${jsArg(x.media?.title?.english||x.media?.title?.romaji||'Anime')}')">Detail</button></div></article>`).join('')}</div>`:'<div class="empty">Belum ada jadwal yang tersedia dari AniList.</div>'}`;}catch(e){showError(e);}}

async function loadAnimeNews(){
 setLoading('MEMUAT BERITA ANIME');try{const d=await api('/api/multiverse/news',16000);const rows=d.items||[];main.innerHTML=`${sectionHead('Anime News','MYANIMELIST / TENRAI NEWS FEED')}<div class="notice">Feed berita katalog Tenrai. Ketersediaan feed bergantung provider. Untuk liputan penuh, buka sumber penerbit.</div>${rows.length?`<div class="list">${rows.map(x=>`<article class="content-card"><div class="eyebrow">${esc(x.date||x.date_posted||'NEWS')}</div><h3>${esc(x.title||'Anime news')}</h3><p class="desc">${esc((x.excerpt||x.intro||'').slice(0,500))}</p>${x.url?`<a class="source-link" href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">Baca sumber ↗</a>`:''}</article>`).join('')}</div>`:'<div class="empty">Feed berita kosong saat ini.</div>'}`;}catch(e){showError(e);}}
let currentMangaDetail=null;
let currentMangaChapterId='';
let currentMangaPages=[];
let currentMangaQuality='data';
let currentReaderIndex=0;
let readerTouchStart=null;
function renderMangaSearch(){main.innerHTML=`${sectionHead('Manga Reader','MANGADEX · MANGABAKA · ANILIST')}<div class="hero"><div class="eyebrow">NOCTURNE · INK & SHADOW ARCHIVE</div><h1>Beyond the midnight pages.</h1><p>Telusuri manga dari tiga katalog. Baca chapter MangaDex dengan mode scroll vertikal yang dipadukan navigasi swipe horizontal.</p></div><div class="manga-searchbar"><input id="mangaQuery" class="field" type="search" placeholder="Cari judul manga / manhwa..." autocomplete="off"><select id="mangaProvider" class="field"><option value="all">Semua katalog</option><option value="mangadex">MangaDex</option><option value="mangabaka">MangaBaka</option><option value="anilist">AniList</option></select><button class="action primary" onclick="searchManga()">Cari manga</button></div><div class="notice">MangaDex menyediakan chapter dan halaman reader. MangaBaka serta AniList digunakan untuk metadata, detail katalog, dan referensi sumber. Kredit katalog: <a href="https://api.mangabaka.org/docs" target="_blank" rel="noopener noreferrer">MangaBaka</a> dan <a href="https://docs.anilist.co/guide/graphql/" target="_blank" rel="noopener noreferrer">AniList docs</a>. Hasil katalog MangaBaka/AniList dapat dicari lagi di MangaDex untuk menemukan chapter yang tersedia.</div><div id="mangaResults"><div class="empty">Masukkan judul lalu mulai pencarian.</div></div>`;const input=document.getElementById('mangaQuery');input.addEventListener('keydown',e=>{if(e.key==='Enter')searchManga();});}
async function searchManga(){const q=document.getElementById('mangaQuery')?.value.trim();const provider=document.getElementById('mangaProvider')?.value||'all';const box=document.getElementById('mangaResults');if(!q||!box)return;box.innerHTML='<div class="loading">SEARCHING THE THREE CATALOGUES...</div>';try{const d=await api('/api/manga/search?q='+encodeURIComponent(q)+'&provider='+encodeURIComponent(provider),45000);const items=d.items||[];box.innerHTML=`<div class="toolbar"><span class="pill">${items.length} RESULTS</span><span class="muted">${Object.entries(d.providers||{}).map(([k,v])=>k.toUpperCase()+': '+v).join(' · ')}</span></div>${Object.keys(d.errors||{}).length?`<div class="notice error">Sebagian katalog tidak merespons: ${esc(Object.entries(d.errors).map(([k,v])=>k+': '+v).join(' | '))}</div>`:''}<div class="manga-grid">${items.map(x=>`<article class="manga-card" onclick="openMangaDetail('${esc(x.provider)}',decodeURIComponent('${jsArg(x.id)}'))"><div class="manga-cover">${x.cover?`<img loading="lazy" referrerpolicy="no-referrer" src="${esc(x.cover)}" alt="${esc(x.title)}" onerror="this.style.display='none'">`:'<div class="noimg">✧</div>'}</div><div class="manga-card-info"><div class="manga-card-title">${esc(x.title)}</div><span class="manga-provider">${esc(x.provider)}</span>${x.year?`<div class="cardsub">${esc(x.year)}</div>`:''}</div></article>`).join('')||'<div class="empty">Tidak ada judul ditemukan.</div>'}</div>`;}catch(e){box.innerHTML=`<div class="notice error">${esc(e.message)}</div>`;}}
async function openMangaDetail(provider,id){rememberCurrent();setLoading('OPENING MANGA DOSSIER');try{const d=await api('/api/manga/detail?provider='+encodeURIComponent(provider)+'&id='+encodeURIComponent(id),30000);currentMangaDetail=d;const detailBox=`<div class="content-card"><div class="manga-detail"><div>${d.cover?`<img class="manga-detail-cover" src="${esc(d.cover)}" referrerpolicy="no-referrer" alt="cover">`:'<div class="manga-detail-cover noimg">✧</div>'}</div><div><div class="eyebrow">${esc(provider.toUpperCase())} CATALOGUE</div><h2 class="detail-title">${esc(d.title)}</h2><div class="manga-chapter-meta">${esc(d.status||'Status unknown')} · ${esc(d.year||'Year unknown')} ${d.format?'· '+esc(d.format):''} ${d.score?'· Score '+esc(d.score):''}</div><div class="manga-desc" style="margin-top:12px">${esc((d.description||'Deskripsi belum tersedia.').slice(0,3200))}</div><div class="manga-chapter-meta" style="margin-top:10px">${esc([...(d.authors||[]),...(d.artists||[])].join(' · ')||'Author info unavailable')}</div><div class="toolbar" style="margin-top:12px">${d.url||d.external_url?`<a class="action" href="${esc(d.url||d.external_url)}" target="_blank" rel="noopener noreferrer">Sumber katalog ↗</a>`:''}<button class="action primary" onclick="findMangaDexChapters(decodeURIComponent('${jsArg(d.title)}'))">Cari chapter di MangaDex</button></div></div></div></div>`;main.innerHTML=`<div class="toolbar"><button class="action" onclick="goBack()">← Kembali ke hasil</button></div>${detailBox}<div id="mangaChapterArea">${sectionHead('Daftar chapter','MANGADEX FEED')}<div class="notice">Chapter reader tersedia bila judul memiliki rilis di MangaDex. Pilih bahasa Indonesia / Inggris atau ubah filter.</div><div class="empty">Memuat sumber chapter...</div></div>`;if(provider==='mangadex'){await loadMangaChapters(d.id);}else{const area=document.getElementById('mangaChapterArea');if(area)area.innerHTML=`${sectionHead('Chapter availability','MANGADEX SEARCH')}<div class="notice">Katalog ${esc(provider)} tidak menjadi host halaman chapter. Cari judul yang sama di MangaDex untuk mendapatkan chapter yang dapat dibaca.</div>`;}}catch(e){showError(e);}}
async function findMangaDexChapters(title){try{const d=await api('/api/manga/search?q='+encodeURIComponent(title)+'&provider=mangadex',30000);const items=d.items||[];const area=document.getElementById('mangaChapterArea');if(!area)return;area.innerHTML=`${sectionHead('Pilih edisi MangaDex','MATCHED TITLES')}<div class="list">${items.map(x=>`<button class="listbtn" onclick="loadMangaChapters(decodeURIComponent('${jsArg(x.id)}'))">${esc(x.title)} <span class="muted">${esc(x.status||'')}</span></button>`).join('')||'<div class="empty">Tidak ada pasangan judul di MangaDex.</div>'}</div>`;}catch(e){alert(e.message);}}
async function loadMangaChapters(id,offset=0){const area=document.getElementById('mangaChapterArea');if(!area)return;area.innerHTML='<div class="loading">INDEXING CHAPTER RELEASES...</div>';try{const lang='id,en';const d=await api('/api/manga/chapters?id='+encodeURIComponent(id)+'&lang='+lang+'&offset='+offset,30000);const rows=d.items||[];area.innerHTML=`${sectionHead('Daftar chapter','MANGADEX · ID / EN RELEASES')}<div class="toolbar"><span class="pill">${d.total} CHAPTERS INDEXED</span><span class="muted">Bahasa: ${esc((d.languages||[]).join(', '))}</span></div><div class="content-card">${rows.map(x=>`<div class="manga-chapter-row"><div><div class="manga-chapter-title">${esc(x.volume?'Vol. '+x.volume+' · ':'')}${esc(x.chapter?'Chapter '+x.chapter:'Oneshot')}${x.title?' — '+esc(x.title):''}</div><div class="manga-chapter-meta">${esc((x.language||'').toUpperCase())} · ${esc(x.group)}${x.uploader?' · Uploaded by '+esc(x.uploader):''} · ${esc(x.published_at||'')}</div></div><button class="action primary" onclick="openMangaChapter(decodeURIComponent('${jsArg(x.id)}'))">Baca →</button></div>`).join('')||'<div class="empty">Chapter untuk bahasa Indonesia/Inggris belum tersedia.</div>'}</div>${d.total>offset+rows.length?`<div class="toolbar"><button class="action" onclick="loadMangaChapters(decodeURIComponent('${jsArg(id)}'),${offset+rows.length})">Muat chapter berikutnya</button></div>`:''}<div class="reader-credit">Chapter content from <a href="https://mangadex.org" target="_blank" rel="noopener noreferrer">MangaDex</a>. Credit ditampilkan per chapter untuk scanlation group dan uploader yang tercatat.</div>`;}catch(e){area.innerHTML=`<div class="notice error">${esc(e.message)}</div>`;}}
async function openMangaChapter(chapterId,quality='data'){rememberCurrent();currentMangaChapterId=chapterId;currentMangaQuality=quality;currentReaderIndex=0;setLoading('PREPARING MANGADEX PAGE STREAM');try{const d=await api('/api/manga/chapter/'+encodeURIComponent(chapterId)+'?quality='+encodeURIComponent(quality),35000);currentMangaPages=d.pages||[];main.innerHTML=`<div class="reader-toolbar"><button class="action" onclick="goBack()">← Chapter list</button><div style="min-width:0;flex:1"><b style="font-size:12px">${esc(d.manga_title)} · ${esc(d.chapter?'Ch. '+d.chapter:'Oneshot')}</b><div class="manga-chapter-meta">${esc(d.title||'')} · ${currentMangaPages.length} pages</div></div><button class="action" onclick="switchMangaQuality('${quality==='data'?'data-saver':'data'}')">${quality==='data'?'Data saver':'Original'}</button></div><div class="reader-shell"><div class="reader-pages" id="readerPages">${currentMangaPages.map((url,i)=>`<div class="reader-page-wrap" id="readerPage${i}" data-page-index="${i}"><img class="reader-page" src="${esc(url)}" alt="Manga page ${i+1}" loading="${i<2?'eager':'lazy'}" referrerpolicy="no-referrer" onload="mangaPageLoaded(${i})" onerror="mangaPageFailed(this,${i})"><span class="reader-page-index">${i+1} / ${currentMangaPages.length}</span></div>`).join('')}</div><div class="reader-credit">Scanlation group: <b>${esc(d.group||'Unknown group')}</b>${d.uploader?' · Uploader: '+esc(d.uploader):''}<br>Source: <a href="${esc(d.source_url)}" target="_blank" rel="noopener noreferrer">MangaDex chapter</a> · MangaDex and scanlation-group credits are retained.</div></div><div class="toolbar" style="justify-content:space-between;margin-top:12px"><button class="action" onclick="moveReaderPage(-1)">← Previous page</button><span class="pill" id="readerPageProgress">PAGE 1 / ${currentMangaPages.length}</span><button class="action primary" onclick="moveReaderPage(1)">Next page →</button></div><div class="notice">Scroll vertikal untuk membaca seperti webtoon. Swipe ke kiri untuk maju satu halaman dan swipe ke kanan untuk kembali. Kualitas Data saver / Original bisa diganti dari toolbar.</div>`;bindMangaReaderGestures();window.scrollTo({top:0,behavior:'smooth'});}catch(e){showError(e);}}
function mangaPageLoaded(index){const el=document.getElementById('readerPageProgress');if(el)el.textContent=`PAGE ${Math.min(currentMangaPages.length,Math.max(1,currentReaderIndex+1))} / ${currentMangaPages.length}`;}
function mangaPageFailed(img,index){img.style.display='none';const parent=img.parentElement;if(parent&&!parent.querySelector('.reader-loading')){const msg=document.createElement('div');msg.className='reader-loading';msg.textContent='HALAMAN '+(index+1)+' GAGAL DIMUAT · COBA GANTI QUALITY';parent.appendChild(msg);}}
function moveReaderPage(delta){const next=Math.max(0,Math.min(currentMangaPages.length-1,currentReaderIndex+delta));if(next===currentReaderIndex)return;currentReaderIndex=next;document.getElementById('readerPage'+next)?.scrollIntoView({behavior:'smooth',block:'start'});const el=document.getElementById('readerPageProgress');if(el)el.textContent=`PAGE ${next+1} / ${currentMangaPages.length}`;}
function bindMangaReaderGestures(){const pages=document.getElementById('readerPages');if(!pages)return;if(window.nocturneReaderObserver)window.nocturneReaderObserver.disconnect();if('IntersectionObserver' in window){window.nocturneReaderObserver=new IntersectionObserver(entries=>{for(const entry of entries){if(entry.isIntersecting&&entry.intersectionRatio>.15){currentReaderIndex=Number(entry.target.dataset.pageIndex||0);const label=document.getElementById('readerPageProgress');if(label)label.textContent=`PAGE ${currentReaderIndex+1} / ${currentMangaPages.length}`;}}},{threshold:[.55,.75]});pages.querySelectorAll('.reader-page-wrap').forEach(page=>window.nocturneReaderObserver.observe(page));}pages.addEventListener('touchstart',e=>{const t=e.changedTouches?.[0];if(t)readerTouchStart={x:t.clientX,y:t.clientY};},{passive:true});pages.addEventListener('touchend',e=>{const t=e.changedTouches?.[0];if(!t||!readerTouchStart)return;const dx=t.clientX-readerTouchStart.x,dy=t.clientY-readerTouchStart.y;readerTouchStart=null;if(Math.abs(dx)>58&&Math.abs(dx)>Math.abs(dy)*1.25)moveReaderPage(dx<0?1:-1);},{passive:true});pages.addEventListener('wheel',e=>{if(Math.abs(e.deltaX)>45&&Math.abs(e.deltaX)>Math.abs(e.deltaY))moveReaderPage(e.deltaX>0?1:-1);},{passive:true});document.onkeydown=e=>{if(currentTab!=='manga'||!document.getElementById('readerPages'))return;if(e.key==='ArrowRight'){moveReaderPage(1);e.preventDefault();}if(e.key==='ArrowLeft'){moveReaderPage(-1);e.preventDefault();}};}
function switchMangaQuality(quality){if(!currentMangaChapterId)return;openMangaChapter(currentMangaChapterId,quality);}

async function loadProviderStatus(){
 setLoading('MEMBANGUN LIVE NETWORK CONSOLE');try{
  const d=await api('/api/providers/status',30000);
  main.innerHTML=`${sectionHead('API Monitor','LIVE NETWORK · HTTP TELEMETRY')}<div class="toolbar"><span class="pill"><span class="live-dot"></span> LIVE SAMPLING</span><span class="pill">${d.providers.filter(x=>x.ok).length}/${d.providers.length} PROVIDER ONLINE</span><button class="action primary" onclick="refreshProviderNodes(this)">⟳ Tes Endpoint</button></div>
  <div class="telemetry-grid" id="telemetryCards"><div class="telemetry-card"><small>Remote → NOCTURNE</small><strong id="upstreamTotal">—</strong><em id="upstreamRate">—</em></div><div class="telemetry-card"><small>NOCTURNE → WebView</small><strong id="appTxTotal">—</strong><em id="appTxRate">—</em></div><div class="telemetry-card"><small>WebView → NOCTURNE</small><strong id="appRxTotal">—</strong><em id="appRxRate">—</em></div><div class="telemetry-card"><small>HTTP round trip terakhir</small><strong id="lastLatency">—</strong><em id="requestCount">Menunggu sampel</em></div></div>
  <div class="traffic-panel"><div class="traffic-label"><b>TRANSFER PIPELINE</b><span id="telemetryUptime">MENGUKUR...</span></div><div class="traffic-label"><span>Provider web mengirim data ke backend</span><b id="upstreamRateLabel">0 B/s</b></div><div class="traffic-bar"><span id="upstreamBar"></span></div><div class="traffic-label"><span>Backend mengirim payload ke APK / WebView</span><b id="appTxRateLabel">0 B/s</b></div><div class="traffic-bar"><span id="appTxBar"></span></div><div class="traffic-label"><span>Request body dari WebView</span><b id="appRxRateLabel">0 B/s</b></div><div class="traffic-bar"><span id="appRxBar"></span></div><div class="traffic-legend">Perhitungan berdasarkan byte body HTTP yang benar-benar melewati sesi Requests dan Flask. Bukan penghitung paket TCP, TLS, atau trafik gambar yang dimuat langsung dari host luar oleh WebView.</div></div>
  ${sectionHead('Provider health','STATUS · HTTP CODE · RTT · PAYLOAD')}<div class="provider-grid" id="providerNodes">${d.providers.map(x=>providerNodeHtml(x)).join('')}</div>
  ${sectionHead('Recent HTTP activity','LIVE EVENT STREAM')}<div class="content-card" id="httpEvents"><div class="empty">Mengumpulkan aktivitas request...</div></div>
  <div class="notice">MS = waktu respons request probe. Counter trafik bersifat kumulatif sejak proses NOCTURNE berjalan. Cache dapat membuat pencarian metadata berikutnya tidak mengirim request baru ke provider. MangaBaka dan AniList dipakai untuk katalog/metadata; halaman chapter reader disajikan oleh MangaDex.</div>`;
  await updateTelemetryPanel();
  if(window.nocturneTelemetryTimer)clearInterval(window.nocturneTelemetryTimer);
  window.nocturneTelemetryTimer=setInterval(()=>{if(currentTab==='providers')updateTelemetryPanel().catch(()=>{});},1100);
 }catch(e){showError(e);}
}
function providerNodeHtml(x){const ms=Number(x.elapsed_ms||0);const width=Math.max(4,Math.min(100,100-ms/30));return `<div class="provider-node"><div class="provider-node-head"><strong>${esc(x.name)}</strong><span class="${x.ok?'ok':'bad'}">${x.ok?'ONLINE':'ERROR'}</span></div><div class="provider-metrics"><span>RTT <b>${ms} ms</b></span><span>HTTP <b>${x.status_code??'—'}</b></span><span>RX <b>${formatBytes(x.bytes||0)}</b></span></div><div class="provider-meter"><span style="width:${x.ok?width:0}%"></span></div><div class="muted" style="font-size:10px;margin-top:7px">${esc(x.error||'Probe endpoint selesai')}</div></div>`;}
async function refreshProviderNodes(button){if(button){button.disabled=true;button.textContent='Menguji...';}try{const d=await api('/api/providers/status',30000);const node=document.getElementById('providerNodes');if(node)node.innerHTML=d.providers.map(x=>providerNodeHtml(x)).join('');}catch(e){console.warn(e);}finally{if(button){button.disabled=false;button.textContent='⟳ Tes Endpoint';}}}
function formatBytes(n){n=Math.max(0,Number(n)||0);if(n<1024)return Math.round(n)+' B';if(n<1048576)return (n/1024).toFixed(1)+' KB';if(n<1073741824)return (n/1048576).toFixed(2)+' MB';return (n/1073741824).toFixed(2)+' GB';}
function formatRate(n){return formatBytes(n)+'/s';}
async function updateTelemetryPanel(){const d=await api('/api/telemetry',7000);const put=(id,value)=>{const el=document.getElementById(id);if(el)el.textContent=value;};put('upstreamTotal',formatBytes(d.upstream_bytes));put('upstreamRate',formatRate(d.upstream_bps)+' · inbound provider');put('appTxTotal',formatBytes(d.app_tx_bytes));put('appTxRate',formatRate(d.app_tx_bps)+' · payload ke WebView');put('appRxTotal',formatBytes(d.app_rx_bytes));put('appRxRate',formatRate(d.app_rx_bps)+' · request dari WebView');put('lastLatency',d.last_upstream_ms==null?'—':Number(d.last_upstream_ms).toFixed(0)+' ms');put('requestCount',`${d.upstream_requests} remote · ${d.app_requests} local`);put('telemetryUptime','UPTIME '+Math.floor(d.uptime_s/60)+'m '+Math.floor(d.uptime_s%60)+'s');put('upstreamRateLabel',formatRate(d.upstream_bps));put('appTxRateLabel',formatRate(d.app_tx_bps));put('appRxRateLabel',formatRate(d.app_rx_bps));const max=Math.max(1024,d.upstream_bps,d.app_tx_bps,d.app_rx_bps);[['upstreamBar',d.upstream_bps],['appTxBar',d.app_tx_bps],['appRxBar',d.app_rx_bps]].forEach(([id,val])=>{const el=document.getElementById(id);if(el)el.style.width=Math.max(1,Math.min(100,Number(val)/max*100))+'%';});const events=document.getElementById('httpEvents');if(events)events.innerHTML=(d.events||[]).slice(0,12).map(x=>`<div class="http-event"><b>${esc(x.host)}</b><span class="${Number(x.status)<400?'ok':'bad'}">${x.status}</span><span>${formatBytes(x.bytes)} · ${x.elapsed_ms??'—'} ms</span></div>`).join('')||'<div class="empty">Belum ada request.</div>';}

async function loadOngoing(page,append){const d=await api(`/api/catalog?section=ongoing&page=${page}`);ongoingPage=page;if(!append)main.innerHTML=`${sectionHead('Anime Ongoing','CURRENTLY AIRING')}<div id="ongoingGrid">${itemGrid(d.items)}</div><div class="toolbar" style="margin-top:18px"><button class="action" onclick="loadOngoing(${Math.max(1,page-1)},false)" ${page<=1?'disabled':''}>← Sebelumnya</button><span class="pill">PAGE ${page}</span><button class="action" onclick="loadOngoing(${page+1},false)">Berikutnya →</button></div>`;else{} }
async function openGenre(url){rememberCurrent();setLoading('MEMBACA GENRE');try{const d=await api('/api/genre?url='+encodeURIComponent(url));main.innerHTML=`<div class="toolbar"><button class="action" onclick="goBack()">← Kembali</button></div>${sectionHead(d.title,'GENRE INDEX')}${itemGrid(d.items)}`;}catch(e){showError(e);}}
async function openAnime(url){rememberCurrent();setLoading('MEMBACA DETAIL ANIME');try{const d=await api('/api/anime?url='+encodeURIComponent(url));const meta=Object.entries(d.meta||{}).slice(0,18).map(([k,v])=>`<div><b>${esc(k)}</b>${esc(v)}</div>`).join('');main.innerHTML=`<div class="toolbar"><button class="action" onclick="goBack()">← Kembali</button></div><section class="content-card"><div class="detail-grid"><div>${d.image?`<img class="detail-poster" loading="lazy" referrerpolicy="no-referrer" src="${esc(d.image)}" alt="${esc(d.title)}">`:'<div class="poster detail-poster noimg">✧</div>'}</div><div><div class="eyebrow">ANIME DOSSIER</div><h1 class="detail-title">${esc(d.title)}</h1><div class="toolbar">${(d.genres||[]).map(g=>`<span class="pill">${esc(g.title)}</span>`).join('')}</div><div class="meta">${meta||'<div>Detail belum tersedia di halaman ini.</div>'}</div><p class="desc">${esc(d.description||'')}</p><div class="toolbar" style="margin-top:14px"><button class="action primary" onclick="lookupMal('${jsArg(d.title)}')">✥ Cari informasi MyAnimeList</button></div><div id="malMatch"></div></div></div></section>${sectionHead('Daftar Episode','STREAMING EPISODES')}<div class="list">${(d.episodes||[]).length?d.episodes.map(ep=>`<button class="listbtn" onclick="openEpisode(decodeURIComponent('${jsArg(ep.url)}'))">▶ &nbsp;${esc(ep.title)}</button>`).join(''):'<div class="empty">Daftar episode tidak berhasil dibaca. Buka halaman asli untuk melihat struktur terbaru.</div>'}</div>`;}catch(e){showError(e);}}
function renderMalSearch(){main.innerHTML=`<div class="hero"><div class="eyebrow">MYANIMELIST · PUBLIC CATALOGUE</div><h1>Anime dossier & knowledge archive</h1><p>Cari judul untuk melihat sinopsis, skor komunitas, peringkat, studio, produser, tema, karakter, staf, rekomendasi, dan statistik dari katalog publik MyAnimeList.</p></div><div class="toolbar"><span class="pill">PROVIDER · TENRAI API</span><button class="action" onclick="checkMalStatus()">◈ Tes koneksi MAL</button></div><div id="malProviderStatus" class="notice">Provider metadata menggunakan Tenrai API. Pengecekan koneksi tersedia di tombol ini.</div><form class="toolbar" id="malSearchForm"><input class="server-select" id="malQuery" style="flex:1;min-width:180px" placeholder="Cari judul anime di MyAnimeList..." required><button class="action primary">Cari MAL</button></form><div id="malResults"><div class="empty">Masukkan judul anime untuk mencari di katalog MyAnimeList.</div></div><div class="notice">Metadata MyAnimeList publik disediakan melalui Tenrai API (kompatibel dengan format Jikan v4). Layanan pihak ketiga dapat berubah atau mengalami gangguan; setiap request memiliki batas waktu dan pesan error yang jelas.</div>`;document.getElementById('malSearchForm').addEventListener('submit',async e=>{e.preventDefault();await searchMal(document.getElementById('malQuery').value);});}
async function checkMalStatus(){const box=document.getElementById('malProviderStatus');if(!box)return;box.innerHTML='<span class="loading">MEMERIKSA KONEKSI TENRAI...</span>';try{const d=await api('/api/mal/status',14000);box.innerHTML=`<span class="pill">ONLINE</span> Provider ${esc(d.provider)} · ${Number(d.elapsed_ms)} ms · ${Number(d.sample_count)} contoh hasil.`;}catch(e){box.innerHTML=`<div class="notice error"><b>Provider belum dapat dihubungi.</b><br>${esc(e.message)}<br><small>Provider: Tenrai API</small></div>`;}}
async function searchMal(q){const box=document.getElementById('malResults');if(!box)return;box.innerHTML='<div class="loading">MENCARI DI MYANIMELIST</div>';try{const d=await api('/api/mal/search?q='+encodeURIComponent(q));box.innerHTML=sectionHead('Hasil MyAnimeList','TENRAI · MAL CATALOGUE')+(d.items.length?`<div class="grid">${d.items.map(x=>`<article class="card" onclick="openMal(${Number(x.mal_id)})"><div class="poster">${x.image?`<img loading="lazy" src="${esc(x.image)}" referrerpolicy="no-referrer" alt="${esc(x.title)}">`:'<div class="noimg">✧</div>'}</div><div class="cardbody"><div class="cardtitle">${esc(x.title)}</div><div class="cardsub">${x.score?'★ '+esc(x.score):'NO SCORE'} · ${esc(x.year||x.type||'MAL')}</div></div></article>`).join('')}</div>`:'<div class="empty">Tidak ada hasil.</div>');}catch(e){box.innerHTML=`<div class="notice error">${esc(e.message)}</div>`;}}
async function lookupMal(title){const target=document.getElementById('malMatch');if(!target)return;target.innerHTML='<div class="loading">MENCARI PADANAN DI TENRAI / MAL...</div>';try{const d=await api('/api/mal/search?q='+encodeURIComponent(title),14000);target.innerHTML=d.items.length?`<div class="notice">Pilih padanan MyAnimeList yang paling sesuai. Hasil dari provider Tenrai.</div><div class="grid">${d.items.slice(0,6).map(x=>`<article class="card" onclick="openMal(${Number(x.mal_id)})"><div class="poster">${x.image?`<img loading="lazy" src="${esc(x.image)}" referrerpolicy="no-referrer">`:'<div class="noimg">✧</div>'}</div><div class="cardbody"><div class="cardtitle">${esc(x.title)}</div><div class="cardsub">MAL ID ${Number(x.mal_id)} · ${x.score||'N/A'}</div></div></article>`).join('')}</div>`:'<div class="empty">Tidak ditemukan padanan MAL untuk judul ini. Coba kata kunci judul Inggris/Jepang atau gunakan menu MyAnimeList.</div>';}catch(e){target.innerHTML=`<div class="notice error"><b>Pencarian MAL gagal.</b><br>${esc(e.message)}<div class="toolbar" style="margin-top:10px"><button class="action" onclick="lookupMal(decodeURIComponent('${jsArg(title)}'))">Coba lagi</button><button class="action" onclick="loadTab('mal')">Buka pencarian MAL</button></div></div>`;}}
async function openMal(id){rememberCurrent();setLoading('MEMBUKA MYANIMELIST DOSSIER');try{const res=await api('/api/mal/anime/'+id);const d=res.data;const img=d.images?.jpg?.large_image_url||d.images?.webp?.large_image_url||'';const genres=(d.genres||[]).concat(d.themes||[],d.demographics||[]).map(x=>`<span class="pill">${esc(x.name)}</span>`).join('');const meta=[['Skor MAL',d.score?`★ ${d.score} / 10`:'Belum dinilai'],['Peringkat',d.rank?`#${d.rank}`:'—'],['Popularitas',d.popularity?`#${d.popularity}`:'—'],['Anggota',d.members?.toLocaleString('id-ID')||'—'],['Tipe',d.type||'—'],['Episode',d.episodes??'—'],['Status',d.status||'—'],['Musim',d.season&&d.year?`${d.season} ${d.year}`:d.year||'—'],['Durasi',d.duration||'—'],['Rating usia',d.rating||'—'],['Studio',(d.studios||[]).map(x=>x.name).join(', ')||'—'],['Produser',(d.producers||[]).map(x=>x.name).join(', ')||'—']].map(([k,v])=>`<div><b>${esc(k)}</b>${esc(v)}</div>`).join('');main.innerHTML=`<div class="toolbar"><button class="action" onclick="goBack()">← Kembali</button><a class="source-link" href="${esc(d.url||'https://myanimelist.net/anime/'+id)}" target="_blank" rel="noopener">Buka MyAnimeList ↗</a></div><section class="content-card"><div class="detail-grid"><div>${img?`<img class="detail-poster" src="${esc(img)}" referrerpolicy="no-referrer" alt="poster MAL">`:''}</div><div><div class="eyebrow">MYANIMELIST · ANIME ID ${Number(id)}</div><h1 class="detail-title">${esc(d.title||'Anime')}</h1><div class="desc">${esc(d.title_english||'')} ${d.title_japanese?' · '+esc(d.title_japanese):''}</div><div class="toolbar" style="margin-top:12px">${genres}</div><div class="meta">${meta}</div><h3>Sinopsis</h3><p class="desc">${esc(d.synopsis||'Sinopsis belum tersedia.')}</p>${d.background?`<h3>Latar Belakang</h3><p class="desc">${esc(d.background)}</p>`:''}<p class="muted">Sumber metadata: MyAnimeList · Tenrai API. Informasi dapat berubah mengikuti pembaruan MAL.</p></div></div></section><div class="toolbar"><button class="action primary" onclick="addToLibrary(${Number(id)},decodeURIComponent('${jsArg(d.title||'Anime')}'),decodeURIComponent('${jsArg(img)}'),decodeURIComponent('${jsArg(d.url||'')}'))">♡ Simpan ke Koleksi</button><button class="action" onclick="loadMalExtra(${Number(id)},'characters')">Karakter & VA</button><button class="action" onclick="loadMalExtra(${Number(id)},'staff')">Staf</button><button class="action" onclick="loadMalExtra(${Number(id)},'recommendations')">Rekomendasi</button><button class="action" onclick="loadMalExtra(${Number(id)},'reviews')">Review</button><button class="action" onclick="loadMalExtra(${Number(id)},'statistics')">Statistik</button><button class="action" onclick="loadMalExtra(${Number(id)},'relations')">Relasi</button></div><div id="malExtra"></div>${sectionHead('Tema Musik','OPENING & ENDING THEMES')}<div class="content-card">${[...(d.theme?.openings||[]),...(d.theme?.endings||[])].length?[...(d.theme?.openings||[]).map(x=>'Opening · '+x),...(d.theme?.endings||[]).map(x=>'Ending · '+x)].map(x=>`<div class="listbtn">${esc(x)}</div>`).join(''):'<div class="empty">Informasi lagu tema tidak tersedia.</div>'}</div>`;window.scrollTo({top:0,behavior:'smooth'});}catch(e){showError(e);}}
async function loadMalExtra(id,kind){const box=document.getElementById('malExtra');if(!box)return;box.innerHTML='<div class="loading">MEMUAT DATA KOMUNITAS MAL</div>';try{const d=await api(`/api/mal/extra/${id}/${kind}`);const data=d.data;let html='';if(kind==='statistics'){html=`<div class="content-card"><h3>Distribusi anggota</h3><div class="meta">${Object.entries(data||{}).filter(([k,v])=>typeof v!=='object').map(([k,v])=>`<div><b>${esc(k.replaceAll('_',' '))}</b>${esc(v)}</div>`).join('')}</div></div>`;}else if(kind==='characters'){html=`<div class="grid">${(data||[]).slice(0,30).map(x=>`<article class="card"><div class="poster">${x.character?.images?.jpg?.image_url?`<img loading="lazy" src="${esc(x.character.images.jpg.image_url)}">`:'<div class="noimg">✧</div>'}</div><div class="cardbody"><div class="cardtitle">${esc(x.character?.name||'Unknown')}</div><div class="cardsub">${esc(x.role||'Character')}</div><div class="desc">${esc((x.voice_actors||[]).slice(0,2).map(v=>v.person?.name+' · '+v.language).join(' / '))}</div></div></article>`).join('')}</div>`;}else if(kind==='staff'){html=`<div class="content-card"><div class="list">${(data||[]).slice(0,35).map(x=>`<div class="listbtn">${esc(x.person?.name||'Unknown')} <span class="muted">— ${esc((x.positions||[]).join(', '))}</span></div>`).join('')}</div></div>`;}else if(kind==='recommendations'){html=`<div class="grid">${(data||[]).slice(0,10).map(x=>`<article class="card" onclick="openMal(${Number(x.entry?.mal_id||0)})"><div class="poster">${x.entry?.images?.jpg?.image_url?`<img loading="lazy" src="${esc(x.entry.images.jpg.image_url)}">`:'<div class="noimg">✧</div>'}</div><div class="cardbody"><div class="cardtitle">${esc(x.entry?.title||'Recommendation')}</div><div class="cardsub">${esc(x.votes||'')} votes</div></div></article>`).join('')}</div>`;}else if(kind==='reviews'){html=`<div class="list">${(data||[]).slice(0,8).map(x=>`<article class="content-card"><b>${esc(x.user?.username||'MAL user')}</b><p class="desc">${esc((x.review||'').slice(0,900))}</p><span class="muted">${esc(x.score||'')} / 10 · ${esc(x.date||'')}</span></article>`).join('')}</div>`;}else{html=`<div class="list">${(data||[]).map(x=>`<div class="listbtn">${esc(x.relation||'Related')}: ${(x.entry||[]).map(e=>esc(e.name)).join(', ')}</div>`).join('')}</div>`;}box.innerHTML=sectionHead(kind.toUpperCase(),'MYANIMELIST COMMUNITY DATA')+(html||'<div class="empty">Data belum tersedia.</div>');}catch(e){box.innerHTML=`<div class="notice error">${esc(e.message)}</div>`;}}
let currentStreamGroups=[];
let currentStreamResolution='';
function currentResolutionGroup(){return currentStreamGroups.find(x=>x.resolution===currentStreamResolution)||currentStreamGroups[0]||{links:[]};}
function setStreamResolution(resolution){
 currentStreamResolution=String(resolution||'');const group=currentResolutionGroup();const server=document.getElementById('serverSelect');
 if(server){server.innerHTML=(group.links||[]).map((x,i)=>`<option value="${i}">${esc(x.name||'Server '+(i+1))}</option>`).join('');server.value='0';}
 const first=group.links?.[0];if(first)switchPlayer(first.embed);
}
async function openEpisode(url){
 rememberCurrent();setLoading('MENYIAPKAN RESOLUTION STREAM PLAYER');
 try{
  const metadataPromise=api('/api/episode?url='+encodeURIComponent(url),8000).catch(()=>null);
  const stream=await api('/api/stream?url='+encodeURIComponent(url),24000);
  const meta=await Promise.race([metadataPromise,new Promise(resolve=>setTimeout(()=>resolve(null),650))])||{};
  const title=stream.title||meta.title||'Episode anime';const pageUrl=meta.url||stream.otakudesuUrl||url;
  const image=meta.image||'';const episodes=meta.episodes||[];
  postApi('/api/history',{episode_url:pageUrl,title,anime_title:title.split(/\s+Episode\s+/i)[0],image}).catch(()=>{});
  currentStreamGroups=(stream.streams||[]).filter(x=>Array.isArray(x.links)&&x.links.length);
  if(!currentStreamGroups.length)throw new Error('API tidak mengembalikan pilihan resolusi/server yang bisa digunakan.');
  currentStreamResolution=currentStreamGroups[0].resolution;
  const resolutionOptions=currentStreamGroups.map(x=>`<option value="${esc(x.resolution)}">${esc(x.resolution)}</option>`).join('');
  const firstLinks=currentResolutionGroup().links;
  const serverOptions=firstLinks.map((x,i)=>`<option value="${i}">${esc(x.name||'Server '+(i+1))}</option>`).join('');
  currentPlayerUrl=firstLinks[0].embed;
  const player=`<div class="player-tools"><label class="muted" for="resolutionSelect">RESOLUSI</label><select class="server-select" id="resolutionSelect" onchange="setStreamResolution(this.value)">${resolutionOptions}</select><label class="muted" for="serverSelect">SERVER</label><select class="server-select" id="serverSelect" onchange="switchPlayerByIndex(this.value)">${serverOptions}</select><button class="action primary" onclick="openExternalPlayer()">▶ PLAY</button><button class="action" onclick="reloadPlayer()">⟳ Muat ulang</button></div><div class="playerbox"><iframe id="episodeFrame" src="${esc(currentPlayerUrl)}" title="${esc(title)}" allow="autoplay; fullscreen; picture-in-picture; encrypted-media" allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe></div><div class="player-state" id="playerState">✧ Pilih resolusi dan server yang tersedia. Jika embed ditolak oleh host, tekan PLAY untuk membuka player pada jendela aplikasi.</div><div class="muted">${currentStreamGroups.length} resolusi tersedia · ${currentStreamGroups.reduce((n,g)=>n+g.links.length,0)} pilihan server. Kualitas dan ketersediaan ditentukan provider.</div>`;
  const poster=image?`<img src="${esc(image)}" loading="lazy" referrerpolicy="no-referrer" alt="poster">`:'';
  const safePage=esc(pageUrl);
  const nav=episodes.map(ep=>`<button class="listbtn" onclick="openEpisode(decodeURIComponent('${jsArg(ep.url)}'))">▶ &nbsp;${esc(ep.title)}</button>`).join('');
  const metadataNotice=!meta.title?'<div class="notice">Metadata episode tambahan belum tersedia. Player tetap memakai data streaming dari Bintang API; gunakan tombol Kembali untuk memilih episode lain.</div>':'';
  main.innerHTML=`<div class="toolbar"><button class="action" onclick="goBack()">← Kembali</button><a class="source-link" target="_blank" rel="noopener noreferrer" href="${safePage}">Halaman sumber ↗</a></div>${sectionHead(title,'NOCTURNE · MULTI-RESOLUTION PLAYER')}<div class="content-card"><div class="player-poster">${poster}<div><div class="eyebrow">NOW PLAYING</div><div class="ep-name">${esc(title)}</div><div class="muted">Bintang API · OtakuDesu</div></div></div>${player}${metadataNotice}<div class="notice">Pilih resolusi seperti 480p atau 720p, lalu pilih server yang tersedia. NOCTURNE hanya menampilkan embed yang dikembalikan provider; aplikasi tidak mengunduh video, mengekstrak stream tersembunyi, atau melewati DRM/login/proteksi host.</div></div>${sectionHead('Navigasi Episode','EPISODE INDEX')}<div class="list">${nav||'<div class="empty">Navigasi episode tidak tersedia dari halaman sumber ini.</div>'}</div><div class="toolbar" style="margin-top:14px">${meta.previous?`<button class="action" onclick="openEpisode(decodeURIComponent('${jsArg(meta.previous.url)}'))">← Sebelumnya</button>`:''}${meta.next?`<button class="action" onclick="openEpisode(decodeURIComponent('${jsArg(meta.next.url)}'))">Berikutnya →</button>`:''}</div>`;
  window.scrollTo({top:0,behavior:'smooth'});
 }catch(e){showError(e);}
}
function switchPlayerByIndex(index){const links=currentResolutionGroup().links||[];const selected=links[Number(index)];if(selected)switchPlayer(selected.embed);}

function switchPlayer(u){currentPlayerUrl=u;const f=document.getElementById('episodeFrame');if(f)f.src=u;const st=document.getElementById('playerState');if(st)st.textContent='Server diganti. Jika embed ditolak, tekan PLAY untuk membuka pemutar sumber.';}
function openExternalPlayer(){if(currentPlayerUrl)window.open(currentPlayerUrl,'_blank','noopener,noreferrer');}
function reloadPlayer(){const f=document.getElementById('episodeFrame');if(f){f.src='about:blank';setTimeout(()=>f.src=currentPlayerUrl,120);}}
function goBack(){if(window.nocturneReaderObserver){window.nocturneReaderObserver.disconnect();window.nocturneReaderObserver=null;}const previous=backStack.pop();if(previous){currentTab=previous.tab||'home';main.innerHTML=previous.html;setTabActive(currentTab);statusText.textContent='SHADOW INDEX · '+currentTab.toUpperCase();}else{loadTab('home');}}
async function postApi(path,data){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','Accept':'application/json'},body:JSON.stringify(data)});const d=await r.json();if(!r.ok||!d.ok)throw new Error(d.error||'Request gagal');return d;}
async function loadMalTop(){setLoading('MEMUAT PERINGKAT MYANIMELIST');const d=await api('/api/mal/top');main.innerHTML=`${sectionHead('MAL Top Anime','COMMUNITY RANKING')}<div class="notice">Daftar populer/rating dari katalog publik MyAnimeList melalui Tenrai API. Peringkat dan skor dapat berubah.</div><div class="grid">${(d.items||[]).map(x=>`<article class="card" onclick="openMal(${Number(x.mal_id)})"><div class="poster">${x.images?.jpg?.image_url?`<img loading="lazy" src="${esc(x.images.jpg.image_url)}">`:'<div class="noimg">✧</div>'}</div><div class="cardbody"><div class="cardtitle">${esc(x.title)}</div><div class="cardsub">★ ${esc(x.score??'—')} · #${esc(x.rank??'—')}</div></div></article>`).join('')}</div>`;}
async function loadSeasonNow(){setLoading('MEMUAT ANIME MUSIM INI');const d=await api('/api/mal/seasons/now');main.innerHTML=`${sectionHead('Anime Musim Ini','CURRENT SEASON · MAL')}<div class="grid">${(d.items||[]).map(x=>`<article class="card" onclick="openMal(${Number(x.mal_id)})"><div class="poster">${x.images?.jpg?.image_url?`<img loading="lazy" src="${esc(x.images.jpg.image_url)}">`:'<div class="noimg">✧</div>'}</div><div class="cardbody"><div class="cardtitle">${esc(x.title)}</div><div class="cardsub">${esc(x.type||'Anime')} · ★ ${esc(x.score??'—')}</div></div></article>`).join('')}</div>`;}
async function addToLibrary(id,title,image,url){const status=prompt('Status: Watching / Completed / On-Hold / Dropped / Plan to Watch','Plan to Watch');if(status===null)return;try{await postApi('/api/library',{mal_id:id,title,image,mal_url:url,status});alert('Anime disimpan ke Koleksi Saya.');}catch(e){alert(e.message);}}
async function loadLibrary(){const d=await api('/api/library');const rows=d.items||[];main.innerHTML=`${sectionHead('Koleksi Saya','PERSONAL ANIME LIBRARY')}<div class="toolbar"><span class="pill">${rows.length} JUDUL TERSIMPAN</span><button class="action" onclick="exportBackup()">Ekspor Backup</button></div>${rows.length?`<div class="content-card">${rows.map(x=>`<div class="library-row">${x.image?`<img src="${esc(x.image)}" loading="lazy">`:''}<div class="grow"><b>${esc(x.title)}</b><div class="muted">MAL ID ${x.mal_id} · ${esc(x.status)} · Skor pribadi: ${esc(x.score??'—')}</div><div class="muted">${esc(x.notes||'')}</div></div><button class="action" onclick="openMal(${Number(x.mal_id)})">Detail</button><button class="action" onclick="removeLibrary(${Number(x.mal_id)})">Hapus</button></div>`).join('')}</div>`:'<div class="empty">Koleksi masih kosong. Buka dossier MAL lalu simpan anime ke koleksi.</div>'}`;}
async function removeLibrary(id){if(!confirm('Hapus anime dari koleksi?'))return;await fetch('/api/library/'+id,{method:'DELETE'});loadLibrary();}
async function loadHistory(){const d=await api('/api/history');const rows=d.items||[];main.innerHTML=`${sectionHead('Riwayat Tonton','LOCAL WATCH HISTORY')}<div class="toolbar"><span class="pill">${rows.length} RIWAYAT</span><button class="action" onclick="clearHistory()">Hapus semua</button></div>${rows.length?`<div class="content-card">${rows.map(x=>`<div class="library-row"><div class="grow"><b>${esc(x.anime_title||x.title)}</b><div class="muted">${esc(x.title)} · ${new Date(x.watched_at).toLocaleString()}</div></div><button class="action primary" onclick="openEpisode(decodeURIComponent('${jsArg(x.episode_url)}'))">Lanjutkan</button></div>`).join('')}</div>`:'<div class="empty">Belum ada episode yang dibuka.</div>'}`;}
async function clearHistory(){if(!confirm('Kosongkan seluruh riwayat tonton?'))return;await fetch('/api/history',{method:'DELETE'});loadHistory();}
function renderTools(){main.innerHTML=`${sectionHead('Tools & Backup','LOCAL DATA MANAGEMENT')}<div class="feature-grid"><div class="feature-tile"><h3>✧ Koleksi lokal</h3><p>Data koleksi disimpan dalam SQLite di perangkat Termux.</p><button class="action" onclick="loadLibrary()">Buka koleksi</button></div><div class="feature-tile"><h3>◷ Riwayat episode</h3><p>Catat episode yang pernah dibuka agar mudah dilanjutkan.</p><button class="action" onclick="loadHistory()">Buka riwayat</button></div><div class="feature-tile"><h3>⇩ Ekspor data</h3><p>Unduh backup JSON koleksi dan riwayat untuk dipindahkan.</p><button class="action primary" onclick="exportBackup()">Ekspor JSON</button></div><div class="feature-tile"><h3>⇧ Impor koleksi</h3><p>Masukkan file backup JSON yang sebelumnya diekspor dari NOCTURNE.</p><input class="field" type="file" id="importFile" accept="application/json"><button class="action" onclick="importBackup()">Impor JSON</button></div></div><div class="notice">Database tersimpan di file <b>nocturne.db</b> di folder project. Buat backup secara berkala sebelum memperbarui atau menghapus folder aplikasi.</div>`;}
async function exportBackup(){try{window.location.href='/api/export/download';}catch(e){alert(e.message);}}
async function importBackup(){const f=document.getElementById('importFile')?.files?.[0];if(!f){alert('Pilih file JSON terlebih dahulu.');return;}try{const d=JSON.parse(await f.text());const r=await postApi('/api/import',d);alert(`${r.imported} item koleksi diimpor.`);loadLibrary();}catch(e){alert('Impor gagal: '+e.message);}}
async function loadDiagnostics(){setLoading('MEMERIKSA JALUR PUBLIK');const d=await api('/api/diagnose');main.innerHTML=`${sectionHead('Pemeriksaan Endpoint','LIVE HTTP CHECK')}<div class="notice">Pemeriksaan dilakukan dari perangkatmu ke halaman publik. Situs ini terlihat memakai halaman HTML WordPress/custom, bukan API JSON publik yang terdokumentasi. HTTP 403 berarti server menolak request biasa; aplikasi tidak melakukan bypass.</div><div class="toolbar"><button class="action primary" onclick="loadDiagnostics()">⟳ Periksa ulang</button><span class="pill">${d.results.filter(x=>x.ok).length}/${d.results.length} OK</span></div><div class="content-card">${d.results.map(x=>`<div class="diagrow"><b>${esc(x.label)}</b><span class="${x.ok?'ok':'bad'}">${x.status??'ERR'}</span><div class="diag-note"><div>${esc(x.note)}</div><div class="muted">${esc(x.path)} · ${x.elapsed_ms} ms</div></div></div>`).join('')}</div>`;}
document.getElementById('searchForm').addEventListener('submit',async e=>{e.preventDefault();const q=document.getElementById('searchInput').value.trim();if(!q)return;currentTab='search';setTabActive('');setLoading('MENCARI JUDUL');statusText.textContent='SEARCH · '+q;try{const d=await api('/api/search?q='+encodeURIComponent(q));main.innerHTML=`${sectionHead('Hasil Pencarian','QUERY: '+d.query)}<div class="toolbar"><span class="pill">${d.items.length} HASIL</span><span class="muted">Metode: ${esc(d.method||'site search')}</span></div>${itemGrid(d.items)}`;}catch(err){showError(err);}});

const AUTH_USERS_KEY='nocturne.local.users.v1';
const AUTH_SESSION_KEY='nocturne.local.session.v1';
const PROFILE_PREFIX='nocturne.local.profile.';
let authMode='login';
let currentUser='';
function readUsers(){try{const v=JSON.parse(localStorage.getItem(AUTH_USERS_KEY)||'[]');return Array.isArray(v)?v:[];}catch(_){return [];}}
function writeUsers(users){localStorage.setItem(AUTH_USERS_KEY,JSON.stringify(users));}
function base64Bytes(bytes){let raw='';bytes.forEach(b=>raw+=String.fromCharCode(b));return btoa(raw);}
function bytesFromBase64(value){return Uint8Array.from(atob(value),c=>c.charCodeAt(0));}
async function makePasswordHash(password,salt){if(!window.crypto?.subtle)throw new Error('Perangkat tidak menyediakan Web Crypto untuk penyimpanan sandi lokal. Perbarui Android System WebView.');const key=await crypto.subtle.importKey('raw',new TextEncoder().encode(password),'PBKDF2',false,['deriveBits']);const bits=await crypto.subtle.deriveBits({name:'PBKDF2',salt:bytesFromBase64(salt),iterations:120000,hash:'SHA-256'},key,256);return base64Bytes(new Uint8Array(bits));}
function setAuthError(message){const el=document.getElementById('authError');if(!el)return;el.textContent=message||'';el.style.display=message?'block':'none';}
function renderAuthMode(){const isRegister=authMode==='register';document.getElementById('authTitle').textContent=isRegister?'Buat akun NOCTURNE':'Masuk ke NOCTURNE';document.getElementById('authDescription').textContent=isRegister?'Buat akun lokal untuk menyimpan profil di perangkat ini.':'Masuk menggunakan akun lokal yang sudah dibuat di perangkat ini.';document.getElementById('passwordConfirmWrap').style.display=isRegister?'block':'none';document.getElementById('authSubmit').textContent=isRegister?'BUAT AKUN':'MASUK';document.getElementById('authSwitch').textContent=isRegister?'Sudah punya akun? Masuk':'Belum punya akun? Buat akun';setAuthError('');}
function toggleAuthMode(){authMode=authMode==='login'?'register':'login';renderAuthMode();}
async function submitAuth(event){event.preventDefault();setAuthError('');const username=document.getElementById('authUsername').value.trim();const password=document.getElementById('authPassword').value;const confirmPassword=document.getElementById('authPasswordConfirm').value;const key=username.toLowerCase();if(!/^[a-z0-9_.-]{3,24}$/i.test(username)){setAuthError('Username 3–24 karakter. Gunakan huruf, angka, titik, garis bawah, atau tanda hubung.');return;}if(password.length<8){setAuthError('Password minimal 8 karakter.');return;}try{const users=readUsers();if(authMode==='register'){if(password!==confirmPassword){setAuthError('Konfirmasi password tidak sama.');return;}if(users.some(u=>u.username.toLowerCase()===key)){setAuthError('Username sudah digunakan di perangkat ini.');return;}const salt=base64Bytes(crypto.getRandomValues(new Uint8Array(16)));const passHash=await makePasswordHash(password,salt);users.push({username,salt,passHash,createdAt:new Date().toISOString()});writeUsers(users);localStorage.setItem(AUTH_SESSION_KEY,username);currentUser=username;getProfile();enterApp();}else{const user=users.find(u=>u.username.toLowerCase()===key);if(!user){setAuthError('Akun tidak ditemukan di perangkat ini.');return;}const passHash=await makePasswordHash(password,user.salt);if(passHash!==user.passHash){setAuthError('Password salah.');return;}localStorage.setItem(AUTH_SESSION_KEY,user.username);currentUser=user.username;getProfile();enterApp();}}catch(error){setAuthError(error.message||'Gagal menyimpan akun lokal. Periksa ruang penyimpanan perangkat.');}}
function bootAuth(){document.getElementById('authForm').addEventListener('submit',submitAuth);renderAuthMode();const session=localStorage.getItem(AUTH_SESSION_KEY);const user=readUsers().find(u=>u.username===session);if(user){currentUser=user.username;getProfile();enterApp();}else{document.getElementById('appRoot').style.display='none';document.getElementById('authGate').style.display='flex';}}
function enterApp(){document.getElementById('authGate').style.display='none';document.getElementById('appRoot').style.display='block';const account=document.getElementById('accountButton');const logoutButton=document.getElementById('logoutButton');account.style.display='inline-block';logoutButton.style.display='inline-block';account.textContent='♙ '+(getProfile().displayName||currentUser);loadTab('home');}
function logout(){if(!confirm('Keluar dari akun lokal NOCTURNE?'))return;localStorage.removeItem(AUTH_SESSION_KEY);currentUser='';document.getElementById('appRoot').style.display='none';document.getElementById('accountButton').style.display='none';document.getElementById('logoutButton').style.display='none';document.getElementById('authGate').style.display='flex';document.getElementById('authPassword').value='';document.getElementById('authPasswordConfirm').value='';authMode='login';renderAuthMode();}
function profileStorageKey(){return PROFILE_PREFIX+currentUser.toLowerCase();}
function getProfile(){if(!currentUser)return {};try{const profile=JSON.parse(localStorage.getItem(profileStorageKey())||'{}');return {username:currentUser,displayName:currentUser,bio:'',avatar:'',banner:'',...profile};}catch(_){return {username:currentUser,displayName:currentUser,bio:'',avatar:'',banner:''};}}
function saveProfileData(profile){try{localStorage.setItem(profileStorageKey(),JSON.stringify(profile));return true;}catch(_){alert('Penyimpanan lokal penuh. Kurangi ukuran foto atau hapus gambar lama.');return false;}}

// Beranda mempunyai media banner sendiri. Blob disimpan di IndexedDB agar MP4/GIF tidak
// dipaksa masuk ke localStorage (yang kapasitasnya jauh lebih kecil).
const HOME_MEDIA_DB='nocturne-home-media';
const HOME_MEDIA_STORE='assets';
const HOME_MEDIA_KEY='home-banner';
const HOME_MEDIA_MAX_BYTES=40*1024*1024;
let activeHomeBannerUrl='';
let appearancePreviewUrl='';
function openHomeMediaDB(){return new Promise((resolve,reject)=>{if(!('indexedDB' in window)){reject(new Error('Penyimpanan media lokal tidak didukung oleh WebView ini.'));return;}const req=indexedDB.open(HOME_MEDIA_DB,1);req.onupgradeneeded=()=>{const db=req.result;if(!db.objectStoreNames.contains(HOME_MEDIA_STORE))db.createObjectStore(HOME_MEDIA_STORE,{keyPath:'key'});};req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error||new Error('Gagal membuka penyimpanan banner.'));});}
async function saveHomeMedia(file,kind){const db=await openHomeMediaDB();try{await new Promise((resolve,reject)=>{const tx=db.transaction(HOME_MEDIA_STORE,'readwrite');tx.objectStore(HOME_MEDIA_STORE).put({key:HOME_MEDIA_KEY,blob:file,mime:kind,name:file.name||'home-banner',size:file.size,updatedAt:Date.now()});tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error||new Error('Gagal menyimpan banner.'));tx.onabort=()=>reject(tx.error||new Error('Penyimpanan banner dibatalkan.'));});}finally{db.close();}}
async function readHomeMedia(){const db=await openHomeMediaDB();try{return await new Promise((resolve,reject)=>{const tx=db.transaction(HOME_MEDIA_STORE,'readonly');const req=tx.objectStore(HOME_MEDIA_STORE).get(HOME_MEDIA_KEY);req.onsuccess=()=>resolve(req.result||null);req.onerror=()=>reject(req.error||new Error('Gagal membaca banner.'));});}finally{db.close();}}
async function deleteHomeMedia(){const db=await openHomeMediaDB();try{await new Promise((resolve,reject)=>{const tx=db.transaction(HOME_MEDIA_STORE,'readwrite');tx.objectStore(HOME_MEDIA_STORE).delete(HOME_MEDIA_KEY);tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error||new Error('Gagal menghapus banner.'));tx.onabort=()=>reject(tx.error||new Error('Penghapusan banner dibatalkan.'));});}finally{db.close();}}
function detectHomeMediaType(file){const name=(file.name||'').toLowerCase();const type=(file.type||'').toLowerCase();if(type==='video/mp4'||name.endsWith('.mp4'))return 'video/mp4';if(type==='image/gif'||name.endsWith('.gif'))return 'image/gif';return '';}
function createHomeMediaElement(asset){const mime=asset.mime||asset.blob?.type||'';const url=URL.createObjectURL(asset.blob);let node;if(mime==='video/mp4'||(asset.name||'').toLowerCase().endsWith('.mp4')){node=document.createElement('video');node.autoplay=true;node.loop=true;node.muted=true;node.playsInline=true;node.preload='metadata';node.setAttribute('aria-label','Banner video NOCTURNE');node.src=url;node.addEventListener('error',()=>{node.replaceWith(Object.assign(document.createElement('div'),{className:'preview-fallback',textContent:'MP4 TIDAK DAPAT DIPUTAR DI WEBVIEW INI'}));URL.revokeObjectURL(url);},{once:true});const play=node.play();if(play&&typeof play.catch==='function')play.catch(()=>{});}else{node=document.createElement('img');node.alt='Banner animasi NOCTURNE';node.src=url;node.addEventListener('error',()=>{node.replaceWith(Object.assign(document.createElement('div'),{className:'preview-fallback',textContent:'GIF TIDAK DAPAT DIBACA'}));URL.revokeObjectURL(url);},{once:true});}node.dataset.objectUrl=url;return node;}
async function applyHomeBanner(){const hero=document.getElementById('homeHero');const media=document.getElementById('homeHeroMedia');if(!hero||!media)return;try{const asset=await readHomeMedia();if(!hero.isConnected||document.getElementById('homeHero')!==hero)return;if(activeHomeBannerUrl){URL.revokeObjectURL(activeHomeBannerUrl);activeHomeBannerUrl='';}media.replaceChildren();if(asset&&asset.blob){const node=createHomeMediaElement(asset);activeHomeBannerUrl=node.dataset.objectUrl||'';media.appendChild(node);hero.classList.add('has-custom-media');}else{hero.classList.remove('has-custom-media');}}catch(error){console.warn('Home banner:',error);hero.classList.remove('has-custom-media');}}
async function renderHomeMediaPreview(){const holder=document.getElementById('appearancePreview');const info=document.getElementById('homeMediaInfo');if(!holder||!info)return;if(appearancePreviewUrl){URL.revokeObjectURL(appearancePreviewUrl);appearancePreviewUrl='';}holder.replaceChildren();try{const asset=await readHomeMedia();if(!holder.isConnected)return;if(asset&&asset.blob){holder.classList.remove('default-motion');const node=createHomeMediaElement(asset);appearancePreviewUrl=node.dataset.objectUrl||'';holder.appendChild(node);info.textContent=`${asset.name||'Banner tersimpan'} · ${(asset.size/1024/1024).toFixed(2)} MB · ${asset.mime||'media'}`;}else{holder.classList.add('default-motion');holder.innerHTML='<img class="default-banner" src="/assets/kolom/banner.svg" alt="Banner bawaan NOCTURNE"><div class="preview-fallback">NOCTURNE · BUILT-IN MOTION</div>';info.textContent='Banner animasi bawaan aplikasi aktif. Gerakan cahaya, sigil, dan lapisan atmosfer berjalan otomatis.';}}catch(error){holder.innerHTML='<div class="preview-fallback">PREVIEW TIDAK TERSEDIA</div>';info.textContent=error.message||'Penyimpanan media tidak tersedia.';}}
async function updateHomeBannerMedia(file){if(!file)return;const kind=detectHomeMediaType(file);if(!kind){alert('Format belum didukung. Pilih file .mp4 atau .gif.');return;}const sizeLimit=kind==='image/gif'?15*1024*1024:HOME_MEDIA_MAX_BYTES;if(file.size<=0||file.size>sizeLimit){alert(kind==='image/gif'?'Ukuran GIF maksimal 15 MB.':'Ukuran MP4 maksimal 40 MB.');return;}try{await saveHomeMedia(file,kind);if(activeHomeBannerUrl){URL.revokeObjectURL(activeHomeBannerUrl);activeHomeBannerUrl='';}if(appearancePreviewUrl){URL.revokeObjectURL(appearancePreviewUrl);appearancePreviewUrl='';}alert('Banner animasi beranda berhasil disimpan di perangkat ini.');await renderAppearance();}catch(error){alert(error.message||'Gagal menyimpan banner. Pastikan ruang penyimpanan WebView masih cukup.');}}
async function removeHomeBannerMedia(){if(!confirm('Hapus banner animasi beranda dan gunakan banner bawaan NOCTURNE?'))return;try{await deleteHomeMedia();if(activeHomeBannerUrl){URL.revokeObjectURL(activeHomeBannerUrl);activeHomeBannerUrl='';}if(appearancePreviewUrl){URL.revokeObjectURL(appearancePreviewUrl);appearancePreviewUrl='';}await renderAppearance();}catch(error){alert(error.message||'Gagal menghapus banner.');}}
async function renderAppearance(){main.innerHTML=`${sectionHead('Tampilan Beranda','HOME SCREEN PERSONALIZATION')}<section class="content-card"><div class="eyebrow">NOCTURNE · MOTION BACKGROUND</div><h2 style="font-weight:normal;margin:8px 0">Banner utama animasi</h2><p class="desc">Atur banner yang tampil di bagian sambutan pada UI utama. Banner ini terpisah dari foto dan banner profil.</p><div class="appearance-preview" id="appearancePreview"><div class="preview-fallback">MEMUAT PREVIEW...</div></div><div class="muted" id="homeMediaInfo">Memeriksa banner tersimpan...</div><div class="profile-upload" style="margin-top:15px"><label for="homeBannerUpload">Upload banner beranda</label><input id="homeBannerUpload" type="file" accept="video/mp4,image/gif,.mp4,.gif"><div class="muted" style="margin-top:8px">Format: MP4 (autoplay, loop, tanpa suara) maksimal 40 MB atau GIF animasi maksimal 15 MB. Disimpan lokal menggunakan IndexedDB.</div></div><div class="toolbar" style="margin-top:16px"><button class="action primary" type="button" onclick="document.getElementById('homeBannerUpload').click()">Pilih MP4 / GIF</button><button class="action" type="button" onclick="removeHomeBannerMedia()">Gunakan banner bawaan</button></div><div class="notice">Banner hanya mengubah bagian hero di halaman Beranda. Video diputar otomatis dalam keadaan mute dan berulang; GIF akan tetap bergerak. Data media tersimpan di perangkat ini dan tidak dikirim ke server. Menghapus data aplikasi/WebView dapat menghapus banner tersebut.</div></section>`;document.getElementById('homeBannerUpload').addEventListener('change',e=>updateHomeBannerMedia(e.target.files?.[0]));await renderHomeMediaPreview();}
function renderProfile(){const p=getProfile();const cover=p.banner?` style="background-image:url('${p.banner}')"`:'';const avatar=p.avatar?`<img src="${p.avatar}" alt="Foto profil">`:`<span>✠</span>`;main.innerHTML=`${sectionHead('Profil Saya','LOCAL ACCOUNT & PERSONALIZATION')}<section class="content-card"><div class="profile-cover" id="profileCover"${cover}></div><div class="profile-head"><div class="avatar-preview" id="profileAvatar">${avatar}</div><div class="profile-name"><h2>${esc(p.displayName||currentUser)}</h2><small>@${esc(currentUser)} · AKUN LOKAL</small></div></div><div class="profile-form-grid"><div class="profile-upload"><label for="avatarUpload">Foto profil</label><input id="avatarUpload" type="file" accept="image/*"><div class="muted">JPG/PNG/WebP · diperkecil otomatis</div></div><div class="profile-upload"><label for="bannerUpload">Banner profil</label><input id="bannerUpload" type="file" accept="image/*"><div class="muted">Hanya ditampilkan pada halaman profil; tidak mengubah banner beranda.</div></div></div><div style="margin-top:16px"><label class="muted" for="profileDisplayName">Nama tampilan</label><input class="field" id="profileDisplayName" maxlength="32" style="width:100%;margin-top:6px" value="${esc(p.displayName||currentUser)}"><label class="muted" for="profileBio" style="display:block;margin-top:13px">Bio</label><textarea class="field" id="profileBio" rows="3" maxlength="180" style="width:100%;margin-top:6px;resize:vertical">${esc(p.bio||'')}</textarea></div><div class="toolbar" style="margin-top:16px"><button class="action primary" onclick="saveProfileFields()">Simpan profil</button><button class="action" onclick="removeProfileAvatar()">Hapus foto</button><button class="action" onclick="removeProfileBanner()">Hapus banner profil</button></div></section><div class="notice">Akun ini hanya tersimpan di local storage perangkat ini. Tidak ada server login atau sinkronisasi akun. Hapus data aplikasi/browser dapat menghapus akun dan profil.</div>`;document.getElementById('avatarUpload').addEventListener('change',e=>updateProfileImage('avatar',e.target.files?.[0]));document.getElementById('bannerUpload').addEventListener('change',e=>updateProfileImage('banner',e.target.files?.[0]));}
async function optimizeImage(file,maxWidth,maxHeight,quality){if(!file)return '';if(!file.type.startsWith('image/'))throw new Error('File harus berupa gambar.');if(file.size>12*1024*1024)throw new Error('Ukuran gambar maksimal 12 MB.');const url=URL.createObjectURL(file);try{const img=new Image();await new Promise((resolve,reject)=>{img.onload=resolve;img.onerror=()=>reject(new Error('Gambar tidak bisa dibaca.'));img.src=url;});const ratio=Math.min(1,maxWidth/img.width,maxHeight/img.height);const canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(img.width*ratio));canvas.height=Math.max(1,Math.round(img.height*ratio));const ctx=canvas.getContext('2d');ctx.drawImage(img,0,0,canvas.width,canvas.height);return canvas.toDataURL('image/jpeg',quality);}finally{URL.revokeObjectURL(url);}}
async function updateProfileImage(kind,file){if(!file)return;try{const data=await optimizeImage(file,kind==='avatar'?640:1600,kind==='avatar'?640:700,kind==='avatar'?.82:.78);const p=getProfile();p[kind]=data;if(saveProfileData(p)){if(kind==='avatar'){const holder=document.getElementById('profileAvatar');holder.innerHTML=`<img src="${data}" alt="Foto profil">`;}else{const cover=document.getElementById('profileCover');cover.style.backgroundImage=`url("${data}")`;}alert(kind==='avatar'?'Foto profil diperbarui.':'Banner profil diperbarui.');}}catch(error){alert(error.message||'Gagal memproses gambar.');}}
function saveProfileFields(){const p=getProfile();p.displayName=document.getElementById('profileDisplayName').value.trim().slice(0,32)||currentUser;p.bio=document.getElementById('profileBio').value.trim().slice(0,180);if(saveProfileData(p)){document.getElementById('accountButton').textContent='♙ '+p.displayName;alert('Profil tersimpan di perangkat.');renderProfile();}}
function removeProfileAvatar(){const p=getProfile();p.avatar='';if(saveProfileData(p))renderProfile();}
function removeProfileBanner(){const p=getProfile();p.banner='';if(saveProfileData(p))renderProfile();}


bootAuth();
</script>
</body>
</html>'''


def main() -> None:
    parser = argparse.ArgumentParser(description="NOCTURNE — NOCTURNE streaming anime")
    parser.add_argument("--host", default="127.0.0.1", help="default 127.0.0.1 (local only)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--check", action="store_true", help="probe halaman publik OtakuDesu dan keluar")
    parser.add_argument("--mal-check", action="store_true", help="uji koneksi provider metadata MAL/Tenrai dan keluar")
    parser.add_argument("--providers-check", action="store_true", help="uji koneksi semua API provider dan keluar")
    args = parser.parse_args()
    if args.check:
        print(json.dumps(run_diagnostics(), ensure_ascii=False, indent=2))
        return
    if args.providers_check:
        print(json.dumps(provider_status(), ensure_ascii=False, indent=2))
        return
    if args.mal_check:
        started = time.monotonic()
        try:
            payload = mal_api_get("anime", {"q": "Cowboy Bebop", "limit": 1, "sfw": "true"})
            print(json.dumps({
                "ok": True,
                "provider": urlparse(MAL_API_BASE).hostname,
                "sample_count": len(payload.get("data", [])),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }, ensure_ascii=False, indent=2))
        except Exception as exc:
            print(json.dumps({
                "ok": False,
                "provider": urlparse(MAL_API_BASE).hostname,
                "error": str(exc),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }, ensure_ascii=False, indent=2))
        return
    print("\n  ✠ NOCTURNE / ANIME STREAMING ARCHIVE")
    print(f"  UI  : http://{args.host}:{args.port}")
    print("  Stop: Ctrl+C\n")
    # Localhost-only by default. Avoid exposing the scraper to a public network.
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
