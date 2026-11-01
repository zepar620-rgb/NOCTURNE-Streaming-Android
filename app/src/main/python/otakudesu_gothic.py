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
from pathlib import Path
from datetime import datetime, timezone
import json
import logging
import os
import re
import hashlib
import xml.etree.ElementTree as ET
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import quote, urljoin, urlparse, urlencode

import requests
from bs4 import BeautifulSoup, Tag
from flask import Flask, jsonify, render_template_string, request
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
_api_session.headers.update({"User-Agent": "NOCTURNE/4.0 (public anime metadata client)", "Accept": "application/json, application/xml;q=0.9, text/xml;q=0.8"})

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
    if not isinstance(img, Tag):
        return None
    for key in ("data-src", "data-lazy-src", "data-original", "src", "data-srcset", "srcset"):
        raw = img.get(key)
        if not raw:
            continue
        if "srcset" in key:
            raw = str(raw).split(",")[0].strip().split(" ")[0]
        candidate = safe_http_url(urljoin(BASE_URL + "/", str(raw)))
        if candidate and not candidate.startswith("data:"):
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
    parent: Tag = anchor
    for _ in range(4):
        if not isinstance(parent.parent, Tag):
            break
        parent = parent.parent
        if parent.name in ("article", "li") or any(
            token in " ".join(parent.get("class", []))
            for token in ("venz", "detpost", "thumb", "item", "card", "anime")
        ):
            return parent
    return anchor.parent if isinstance(anchor.parent, Tag) else anchor


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
        for img in container.find_all("img"):
            image = get_img_url(img)
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


def parse_catalog(section: str, page: int = 1) -> dict[str, Any]:
    page = max(1, min(int(page), 100))
    if section == "ongoing":
        path = "/ongoing-anime/" if page == 1 else f"/ongoing-anime/page/{page}/"
    elif section == "anime-list":
        path = "/anime-list/"
    else:
        raise ValueError("Katalog tidak dikenal")
    soup, final_url = get_soup(urljoin(BASE_URL, path))
    items = parse_anime_cards(soup, limit=500 if section == "anime-list" else 100)
    if section == "anime-list" and not items:
        # Anime list page may be plain alphabetic links without a card wrapper.
        for a in soup.select('a[href*="/anime/"]'):
            href = safe_http_url(urljoin(BASE_URL + "/", str(a.get("href", ""))))
            title = clean_text(a.get_text(" ", strip=True))
            if href and title:
                try:
                    site_url(href)
                    items.append({"title": title, "url": href, "image": None, "subtitle": ""})
                except ValueError:
                    pass
    return {"title": page_title(soup), "items": items, "page": page, "source": final_url}


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
    return {"title": page_title(soup), "genres": parse_genres(soup), "source": final_url}


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
                output.append({"name": name, "ok": True, "status": "ONLINE", "elapsed_ms": elapsed})
            except Exception as exc:
                output.append({"name": name, "ok": False, "status": "OFFLINE / ERROR", "error": str(exc)[:220], "elapsed_ms": elapsed})
    return sorted(output, key=lambda x: x["name"])


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
        ("WordPress REST root", "/wp-json/"),
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


@app.get("/api/providers/status")
def api_providers_status():
    return jsonify({"ok": True, "providers": provider_status()})


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
button,input{font:inherit}button{cursor:pointer}.app{position:relative;z-index:1;min-height:100vh}.topbar{position:sticky;top:0;z-index:20;background:#101113ed;backdrop-filter:blur(14px);border-bottom:1px solid var(--line);padding:12px clamp(14px,3vw,34px);display:flex;gap:18px;align-items:center;justify-content:space-between}.brand{display:flex;gap:12px;align-items:center;min-width:max-content}.sigil{width:40px;height:40px;border:1px solid var(--red);color:var(--red2);display:grid;place-items:center;font-size:24px;transform:rotate(45deg);box-shadow:0 0 18px #a52b4533}.sigil span{transform:rotate(-45deg)}.brandname{font-size:18px;letter-spacing:3px;font-weight:bold}.brandname small{display:block;color:var(--gold);font-size:9px;letter-spacing:4px;margin-top:3px}.searchbar{display:flex;max-width:580px;width:100%;border:1px solid var(--line);background:#1d1f23;border-radius:4px;overflow:hidden}.searchbar input{min-width:0;flex:1;background:transparent;color:var(--text);border:0;outline:none;padding:11px 14px;font-family:Arial,sans-serif}.searchbar button{border:0;background:var(--red);color:white;padding:0 17px;font-family:Arial,sans-serif;font-weight:bold;letter-spacing:1px}.status{font:11px Arial,sans-serif;color:var(--muted);white-space:nowrap}
.shell{display:grid;grid-template-columns:220px minmax(0,1fr);max-width:1600px;margin:0 auto;min-height:calc(100vh - 66px)}.sidebar{border-right:1px solid var(--line);padding:28px 16px;position:sticky;top:65px;align-self:start;height:calc(100vh - 65px);overflow-y:auto}.side-label{color:var(--gold);font-size:10px;letter-spacing:3px;margin:8px 10px 14px}.navbtn{width:100%;border:1px solid transparent;background:transparent;color:var(--muted);padding:12px 13px;text-align:left;margin:3px 0;border-radius:4px;font-size:14px;transition:.18s}.navbtn:hover,.navbtn.active{background:linear-gradient(90deg,#76253a44,transparent);border-color:#713045;color:var(--text)}.navbtn .ico{display:inline-block;width:25px;color:var(--red2)}.side-note{margin:35px 9px 0;padding:14px 12px;border-top:1px solid var(--line);color:#8d7883;font:11px/1.7 Arial,sans-serif}.main{padding:28px clamp(14px,3vw,38px) 70px;min-width:0;min-height:calc(100vh - 66px)}.hero{background-image:url('/assets/kolom/banner.svg');background-color:#202226;background-size:cover;background-position:center;border:1px solid #45484f;border-radius:7px;padding:28px clamp(18px,3vw,34px);margin-bottom:28px;position:relative;overflow:hidden;box-shadow:var(--shadow);isolation:isolate;min-height:170px}.hero:before{content:"";position:absolute;inset:-38%;z-index:1;pointer-events:none;opacity:.78;background:radial-gradient(ellipse at 18% 42%,#9e304c55 0,transparent 30%),radial-gradient(ellipse at 76% 58%,#b7a2ad24 0,transparent 26%),linear-gradient(112deg,transparent 35%,#d7a5b51c 49%,transparent 62%);background-size:155% 155%;animation:nocturneBannerDrift 13s ease-in-out infinite alternate,nocturneBannerPulse 7s ease-in-out infinite}
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
.eyebrow{font:10px Arial,sans-serif;letter-spacing:4px;color:var(--gold);text-transform:uppercase}.hero h1{font-size:clamp(26px,4vw,42px);font-weight:normal;letter-spacing:1px;margin:9px 0}.hero p{color:#c0adb7;max-width:660px;font-size:14px;line-height:1.65;margin:0}.section-head{display:flex;align-items:end;justify-content:space-between;gap:12px;margin:25px 0 16px;padding-bottom:10px;border-bottom:1px solid var(--line)}.section-head h2{font-size:21px;font-weight:normal;letter-spacing:1.5px;margin:0}.section-head small{font:10px Arial,sans-serif;color:var(--muted);letter-spacing:2px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:17px}.card{min-width:0;background:linear-gradient(160deg,#222429,#181a1e);border:1px solid #363940;border-radius:5px;overflow:hidden;cursor:pointer;transition:transform .18s,border-color .18s,box-shadow .18s}.card:hover{transform:translateY(-4px);border-color:#934157;box-shadow:0 12px 26px #0008}.poster{aspect-ratio:2/2.85;background:linear-gradient(145deg,#30202b,#100c10);position:relative;overflow:hidden}.poster img{display:block;width:100%;height:100%;object-fit:cover}.poster .noimg{height:100%;display:grid;place-items:center;color:#754052;font-size:38px}.cardbody{padding:10px 11px 12px}.cardtitle{font-size:13px;line-height:1.35;min-height:35px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;color:#f0e5e9}.cardsub{font:10px Arial,sans-serif;color:var(--gold);margin-top:7px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.pill{display:inline-block;border:1px solid #714052;color:#d7a2af;padding:4px 8px;font:10px Arial,sans-serif;letter-spacing:1px;border-radius:20px}.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 18px}.action{border:1px solid #633448;background:#241720;color:#eadde2;padding:9px 13px;border-radius:3px;font:12px Arial,sans-serif}.action:hover{background:#592338}.action.primary{background:var(--red);border-color:var(--red);color:white}.content-card{background:#1b1d21;border:1px solid var(--line);border-radius:6px;padding:19px;margin-bottom:18px}.detail-grid{display:grid;grid-template-columns:minmax(160px,230px) minmax(0,1fr);gap:23px}.detail-poster{width:100%;max-height:340px;object-fit:cover;border:1px solid #503344;border-radius:4px}.detail-title{font-size:clamp(22px,3vw,34px);font-weight:normal;line-height:1.2;margin:2px 0 13px}.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px;margin:14px 0}.meta div{background:#131518;border:1px solid #34373d;padding:9px 10px;font:12px/1.45 Arial,sans-serif;color:#d8cbd1}.meta b{display:block;color:var(--gold);font-size:10px;letter-spacing:1px;text-transform:uppercase;margin-bottom:3px}.desc{font:13px/1.7 Arial,sans-serif;color:#bcaeb6}.list{display:grid;gap:7px}.listbtn{width:100%;text-align:left;background:#1a1c20;border:1px solid #383b42;color:var(--text);padding:12px 13px;border-radius:3px;font-size:13px}.listbtn:hover{border-color:var(--red);background:#28151f}.daytitle{color:var(--gold);letter-spacing:2px;font-size:13px;margin:22px 0 10px}.playerbox{background:#08090b;border:1px solid #45484f;border-radius:5px;overflow:hidden}.playerbox iframe{display:block;width:100%;aspect-ratio:16/9;min-height:230px;border:0;background:#000}.playerbox{box-shadow:0 14px 32px #0009}.player-tools .action.primary{min-width:112px;font-weight:bold;letter-spacing:1px}.player-state{border-radius:0 3px 3px 0}.player-poster{padding:10px;border:1px solid var(--line);background:#15171b;border-radius:6px}.player-poster .ep-name{font-size:18px}.player-poster .eyebrow{margin-bottom:6px}.player-tools{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.server-select{background:#23252a;color:#eee5e8;border:1px solid #494c54;padding:9px 10px;border-radius:3px;max-width:100%;font:12px Arial,sans-serif}.player-state{font:12px/1.6 Arial,sans-serif;color:#cbb9c2;padding:10px 12px;background:#15171b;border-left:3px solid #9e304c;margin-top:10px}.player-poster{display:flex;gap:14px;align-items:center;margin-bottom:14px}.player-poster img{width:76px;height:108px;object-fit:cover;border:1px solid #704052;border-radius:3px}.player-poster .ep-name{font-size:17px;line-height:1.35}.server-chip{display:inline-block;padding:5px 9px;border:1px solid #5c3546;color:#d8a7b4;font:10px Arial,sans-serif;margin:3px;border-radius:20px}.player-placeholder{padding:45px 16px;text-align:center;color:var(--muted);font:13px/1.7 Arial,sans-serif}.source-link{display:inline-block;color:#e9b6c2;text-decoration:none;border-bottom:1px solid #8e4258;padding-bottom:2px;font:12px Arial,sans-serif;margin:12px 0}.source-link:hover{color:white}.notice{border:1px solid #795d3d;background:#281f17;color:#d6c09e;padding:12px 14px;border-radius:4px;font:12px/1.6 Arial,sans-serif;margin:12px 0}.error{border-color:#8c3347;background:#2b151d;color:#f0b5c1}.loading{padding:50px 15px;text-align:center;color:var(--gold);letter-spacing:3px}.loading:after{content:"";display:block;width:26px;height:26px;border:2px solid #663347;border-top-color:var(--red2);border-radius:50%;margin:15px auto 0;animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}.empty{padding:40px 15px;text-align:center;color:var(--muted);border:1px dashed #493340;border-radius:4px;font:13px Arial,sans-serif}.diagrow{display:grid;grid-template-columns:minmax(120px,1fr) 70px minmax(0,2fr);gap:10px;padding:10px;border-bottom:1px solid var(--line);font:12px Arial,sans-serif;align-items:center}.ok{color:#8ccea2}.bad{color:#e18b9b}.muted{color:var(--muted);font:11px Arial,sans-serif}.footer{margin-top:55px;padding:20px 0;border-top:1px solid var(--line);color:#7e6d77;font:10px/1.7 Arial,sans-serif;letter-spacing:.5px}
@media(max-width:800px){.shell{grid-template-columns:1fr}.sidebar{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line);padding:8px 12px;display:flex;gap:4px;overflow-x:auto}.side-label,.side-note{display:none}.navbtn{width:auto;min-width:max-content;padding:9px 12px;margin:0;font-size:12px}.navbtn .ico{width:auto;margin-right:5px}.main{padding-top:18px}.topbar{flex-wrap:wrap;gap:11px}.searchbar{order:3;max-width:none}.status{margin-left:auto}.hero{padding:21px 18px}.grid{grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:11px}.detail-grid{grid-template-columns:120px minmax(0,1fr);gap:14px}.detail-poster{max-height:220px}.content-card{padding:13px}}
@media(max-width:420px){.brandname{font-size:15px}.sigil{width:34px;height:34px}.detail-grid{grid-template-columns:1fr}.detail-poster{width:145px;max-height:220px}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.diagrow{grid-template-columns:1fr 55px}.diagrow .diag-note{grid-column:1/-1}}
.feature-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}.feature-tile{background:#24262b;border:1px solid #44474e;padding:16px;border-radius:5px}.feature-tile h3{margin:0 0 7px;font-weight:normal;color:#f0dce4}.feature-tile p{font:12px/1.55 Arial,sans-serif;color:var(--muted)}.field{background:#120e13;border:1px solid var(--line);color:var(--text);padding:10px;border-radius:3px;max-width:100%}.library-row{display:flex;gap:13px;align-items:center;padding:12px;border-bottom:1px solid var(--line)}.library-row img{width:58px;height:78px;object-fit:cover}.library-row .grow{flex:1;min-width:0}.library-row select,.library-row input{max-width:150px}.tool-panel{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.tool-panel textarea{width:100%;min-height:110px}.stat-number{font-size:25px;color:var(--gold)}
@media(max-width:800px){.shell{grid-template-columns:1fr}.sidebar{position:relative;top:0;height:auto;display:flex;overflow-x:auto;gap:4px;padding:8px;border-right:0;border-bottom:1px solid var(--line)}.side-label,.side-note{display:none}.navbtn{white-space:nowrap;width:auto;min-width:max-content;padding:9px}.main{padding-top:18px}.topbar{flex-wrap:wrap}.status{display:none}}

.auth-gate{position:fixed;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center;padding:22px;background:radial-gradient(ellipse at 50% 0%,#383a4055,transparent 55%),#0c0d0f;}
.auth-card{width:min(100%,440px);padding:28px;background:linear-gradient(155deg,#222429,#15171b);border:1px solid #474a51;border-radius:10px;box-shadow:0 26px 80px #000b;}
.auth-mark{width:58px;height:58px;margin:0 auto 18px;display:grid;place-items:center;border:1px solid var(--red);transform:rotate(45deg);color:var(--red2);font-size:28px}.auth-mark span{transform:rotate(-45deg)}
.auth-card .eyebrow{text-align:center}.auth-card h1{text-align:center;font-size:25px;font-weight:normal;margin:9px 0}.auth-card p{text-align:center;color:var(--muted);font:12px/1.7 Arial,sans-serif;margin:0 0 18px}.auth-card label{display:block;font:11px Arial,sans-serif;color:#c6c7cb;margin:12px 0 6px}.auth-card input{width:100%;background:#111317;border:1px solid #41444a;color:var(--text);border-radius:5px;padding:12px;outline:none}.auth-card input:focus,.field:focus{border-color:var(--red2)}.auth-card .auth-actions{display:flex;gap:10px;margin-top:18px}.auth-card .auth-actions button{flex:1}.auth-switch{display:block;margin:16px auto 0;background:none;border:0;color:#d38a9c;text-decoration:underline;font:12px Arial,sans-serif}.auth-error{display:none;color:#f0a6b3;background:#2a1b21;border:1px solid #623443;border-radius:4px;padding:9px 10px;font:12px/1.5 Arial,sans-serif;margin-top:12px}
.account-actions{display:flex;align-items:center;gap:7px}.account-actions button{white-space:nowrap}.profile-cover{height:170px;border-radius:7px;border:1px solid #45484f;background:linear-gradient(110deg,#34373d,#202226 60%,#17191d);background-size:cover;background-position:center;position:relative;overflow:hidden}.profile-cover:after{content:"";position:absolute;inset:0;background:linear-gradient(0deg,#0a0b0dcc,transparent 75%)}.profile-head{display:flex;align-items:end;gap:14px;margin:-46px 0 16px 18px;position:relative;z-index:1}.avatar-preview{width:90px;height:90px;flex:0 0 90px;border:3px solid #1a1c20;border-radius:50%;background:linear-gradient(145deg,#383a40,#17191d);display:grid;place-items:center;color:#d8d9dd;font-size:30px;overflow:hidden}.avatar-preview img{width:100%;height:100%;object-fit:cover}.profile-name{padding-bottom:7px}.profile-name h2{font-size:20px;font-weight:normal;margin:0 0 4px}.profile-name small{font:10px Arial,sans-serif;color:var(--muted);letter-spacing:1px}.profile-form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.profile-form-grid .field{width:100%}.profile-upload{padding:14px;background:#15171b;border:1px solid var(--line);border-radius:6px}.profile-upload label{display:block;color:#d6d7da;font:12px Arial,sans-serif;margin-bottom:8px}.profile-upload input{max-width:100%;font:11px Arial,sans-serif;color:var(--muted)}.hero.has-custom-media{border-color:#555960}.hero.has-custom-media h1,.hero.has-custom-media p,.hero.has-custom-media .eyebrow{text-shadow:0 2px 12px #000}
@media(max-width:620px){.profile-form-grid{grid-template-columns:1fr}.profile-cover{height:140px}.auth-card{padding:22px}.account-actions{width:100%;justify-content:flex-end}.topbar{align-items:flex-start}}

</style>
</head>
<body>
<div class="app" id="appRoot">
<header class="topbar">
  <div class="brand"><div class="sigil"><span>✠</span></div><div class="brandname">NOCTURNE<small>ANIME STREAMING ARCHIVE</small></div></div>
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
    <button class="navbtn" data-tab="discover" onclick="loadTab('discover',this)"><span class="ico">♛</span> Discovery Hub</button>
    <button class="navbtn" data-tab="news" onclick="loadTab('news',this)"><span class="ico">☷</span> Anime News</button>
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
    <div class="auth-mark"><span>✠</span></div>
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
async function loadTab(tab,btn=null){if(tab!=='home'&&activeHomeBannerUrl){URL.revokeObjectURL(activeHomeBannerUrl);activeHomeBannerUrl='';}if(tab!=='appearance'&&appearancePreviewUrl){URL.revokeObjectURL(appearancePreviewUrl);appearancePreviewUrl='';}currentTab=tab;backStack=[];setTabActive(tab);if(btn)setTabActive(tab);statusText.textContent='FETCHING · '+tab.toUpperCase();setLoading();try{
 if(tab==='home'){const d=await api('/api/home');main.innerHTML=`<div class="hero" id="homeHero"><div class="hero-media" id="homeHeroMedia" aria-hidden="true"></div><div class="hero-shade"></div><div class="hero-content"><div class="eyebrow">THE MIDNIGHT ANIME INDEX</div><h1>Selamat datang di NOCTURNE</h1><p>Metadata dibaca dari <b>${esc(d.source||'otakudesu.blog')}</b>. Pilih poster untuk membuka detail judul dan daftar episode.</p></div></div>${sectionHead('Rilisan dari Beranda','LATEST FROM SOURCE')}${itemGrid(d.items)}`;applyHomeBanner();}
 else if(tab==='ongoing'){ongoingPage=1;await loadOngoing(1,false);}
 else if(tab==='anime-list'){const d=await api('/api/catalog?section=anime-list');main.innerHTML=`${sectionHead('Daftar Anime','A—Z INDEX')}<div class="toolbar"><span class="pill">${d.items.length} JUDUL TERBACA</span></div>${itemGrid(d.items)}`;}
 else if(tab==='schedule'){const d=await api('/api/schedule');main.innerHTML=`${sectionHead('Jadwal Rilis','WEEKLY SCHEDULE')}${d.groups?.length?d.groups.map(g=>`<h3 class="daytitle">☾ ${esc(g.day)}</h3><div class="list">${g.items.map(x=>`<button class="listbtn" onclick="openAnime(decodeURIComponent('${jsArg(x.url)}'))">✧ &nbsp;${esc(x.title)}</button>`).join('')}</div>`).join(''):'<div class="empty">Jadwal tidak ditemukan.</div>'}`;}
 else if(tab==='genres'){const d=await api('/api/genres');main.innerHTML=`${sectionHead('Genre','CHOOSE YOUR MOOD')}<div class="grid">${d.genres.map(g=>`<article class="card" onclick="openGenre(decodeURIComponent('${jsArg(g.url)}'))"><div class="poster" style="aspect-ratio:1.35/1"><div class="noimg">❖</div></div><div class="cardbody"><div class="cardtitle">${esc(g.title)}</div><div class="cardsub">EXPLORE GENRE</div></div></article>`).join('')}</div>`;}
 else if(tab==='mal'){renderMalSearch();}
 else if(tab==='multiverse'){renderMultiverseSearch();}
 else if(tab==='discover'){await loadMultiverseDiscover();}
 else if(tab==='news'){await loadAnimeNews();}
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
async function loadProviderStatus(){
 setLoading('MENGUJI SEMUA PROVIDER');try{const d=await api('/api/providers/status',24000);main.innerHTML=`${sectionHead('API Monitor','MULTI-PROVIDER HEALTH CHECK')}<div class="toolbar"><span class="pill">${d.providers.filter(x=>x.ok).length}/${d.providers.length} ONLINE</span><button class="action primary" onclick="loadProviderStatus()">⟳ Tes Ulang</button></div><div class="content-card">${d.providers.map(x=>`<div class="diagrow"><b>${esc(x.name)}</b><span class="${x.ok?'ok':'bad'}">${x.ok?'ONLINE':'ERROR'}</span><div class="diag-note">${esc(x.error||'Provider merespons')}<div class="muted">${Number(x.elapsed_ms||0)} ms</div></div></div>`).join('')}</div><div class="notice">Status diuji dari perangkat ini. ONLINE berarti endpoint merespons pada saat pengecekan, bukan jaminan seluruh endpoint selalu tersedia. Rate limit masing-masing penyedia tetap berlaku.</div>`;}catch(e){showError(e);}}

async function loadOngoing(page,append){const d=await api(`/api/catalog?section=ongoing&page=${page}`);ongoingPage=page;if(!append)main.innerHTML=`${sectionHead('Anime Ongoing','CURRENTLY AIRING')}<div id="ongoingGrid">${itemGrid(d.items)}</div><div class="toolbar" style="margin-top:18px"><button class="action" onclick="loadOngoing(${Math.max(1,page-1)},false)" ${page<=1?'disabled':''}>← Sebelumnya</button><span class="pill">PAGE ${page}</span><button class="action" onclick="loadOngoing(${page+1},false)">Berikutnya →</button></div>`;else{} }
async function openGenre(url){rememberCurrent();setLoading('MEMBACA GENRE');try{const d=await api('/api/genre?url='+encodeURIComponent(url));main.innerHTML=`<div class="toolbar"><button class="action" onclick="goBack()">← Kembali</button></div>${sectionHead(d.title,'GENRE INDEX')}${itemGrid(d.items)}`;}catch(e){showError(e);}}
async function openAnime(url){rememberCurrent();setLoading('MEMBACA DETAIL ANIME');try{const d=await api('/api/anime?url='+encodeURIComponent(url));const meta=Object.entries(d.meta||{}).slice(0,18).map(([k,v])=>`<div><b>${esc(k)}</b>${esc(v)}</div>`).join('');main.innerHTML=`<div class="toolbar"><button class="action" onclick="goBack()">← Kembali</button><a class="source-link" target="_blank" rel="noopener noreferrer" href="${esc(d.url)}">Buka halaman asli ↗</a></div><section class="content-card"><div class="detail-grid"><div>${d.image?`<img class="detail-poster" loading="lazy" referrerpolicy="no-referrer" src="${esc(d.image)}" alt="${esc(d.title)}">`:'<div class="poster detail-poster noimg">✧</div>'}</div><div><div class="eyebrow">ANIME DOSSIER</div><h1 class="detail-title">${esc(d.title)}</h1><div class="toolbar">${(d.genres||[]).map(g=>`<span class="pill">${esc(g.title)}</span>`).join('')}</div><div class="meta">${meta||'<div>Detail belum tersedia di halaman ini.</div>'}</div><p class="desc">${esc(d.description||'')}</p><div class="toolbar" style="margin-top:14px"><button class="action primary" onclick="lookupMal('${jsArg(d.title)}')">✥ Cari informasi MyAnimeList</button></div><div id="malMatch"></div></div></div></section>${sectionHead('Daftar Episode','STREAMING EPISODES')}<div class="list">${(d.episodes||[]).length?d.episodes.map(ep=>`<button class="listbtn" onclick="openEpisode(decodeURIComponent('${jsArg(ep.url)}'))">▶ &nbsp;${esc(ep.title)}</button>`).join(''):'<div class="empty">Daftar episode tidak berhasil dibaca. Buka halaman asli untuk melihat struktur terbaru.</div>'}</div>`;}catch(e){showError(e);}}
function renderMalSearch(){main.innerHTML=`<div class="hero"><div class="eyebrow">MYANIMELIST · PUBLIC CATALOGUE</div><h1>Anime dossier & knowledge archive</h1><p>Cari judul untuk melihat sinopsis, skor komunitas, peringkat, studio, produser, tema, karakter, staf, rekomendasi, dan statistik dari katalog publik MyAnimeList.</p></div><div class="toolbar"><span class="pill">PROVIDER · TENRAI API</span><button class="action" onclick="checkMalStatus()">◈ Tes koneksi MAL</button></div><div id="malProviderStatus" class="notice">Provider metadata menggunakan Tenrai API. Pengecekan koneksi tersedia di tombol ini.</div><form class="toolbar" id="malSearchForm"><input class="server-select" id="malQuery" style="flex:1;min-width:180px" placeholder="Cari judul anime di MyAnimeList..." required><button class="action primary">Cari MAL</button></form><div id="malResults"><div class="empty">Masukkan judul anime untuk mencari di katalog MyAnimeList.</div></div><div class="notice">Metadata MyAnimeList publik disediakan melalui Tenrai API (kompatibel dengan format Jikan v4). Layanan pihak ketiga dapat berubah atau mengalami gangguan; setiap request memiliki batas waktu dan pesan error yang jelas.</div>`;document.getElementById('malSearchForm').addEventListener('submit',async e=>{e.preventDefault();await searchMal(document.getElementById('malQuery').value);});}
async function checkMalStatus(){const box=document.getElementById('malProviderStatus');if(!box)return;box.innerHTML='<span class="loading">MEMERIKSA KONEKSI TENRAI...</span>';try{const d=await api('/api/mal/status',14000);box.innerHTML=`<span class="pill">ONLINE</span> Provider ${esc(d.provider)} · ${Number(d.elapsed_ms)} ms · ${Number(d.sample_count)} contoh hasil.`;}catch(e){box.innerHTML=`<div class="notice error"><b>Provider belum dapat dihubungi.</b><br>${esc(e.message)}<br><small>Provider: Tenrai API</small></div>`;}}
async function searchMal(q){const box=document.getElementById('malResults');if(!box)return;box.innerHTML='<div class="loading">MENCARI DI MYANIMELIST</div>';try{const d=await api('/api/mal/search?q='+encodeURIComponent(q));box.innerHTML=sectionHead('Hasil MyAnimeList','TENRAI · MAL CATALOGUE')+(d.items.length?`<div class="grid">${d.items.map(x=>`<article class="card" onclick="openMal(${Number(x.mal_id)})"><div class="poster">${x.image?`<img loading="lazy" src="${esc(x.image)}" referrerpolicy="no-referrer" alt="${esc(x.title)}">`:'<div class="noimg">✧</div>'}</div><div class="cardbody"><div class="cardtitle">${esc(x.title)}</div><div class="cardsub">${x.score?'★ '+esc(x.score):'NO SCORE'} · ${esc(x.year||x.type||'MAL')}</div></div></article>`).join('')}</div>`:'<div class="empty">Tidak ada hasil.</div>');}catch(e){box.innerHTML=`<div class="notice error">${esc(e.message)}</div>`;}}
async function lookupMal(title){const target=document.getElementById('malMatch');if(!target)return;target.innerHTML='<div class="loading">MENCARI PADANAN DI TENRAI / MAL...</div>';try{const d=await api('/api/mal/search?q='+encodeURIComponent(title),14000);target.innerHTML=d.items.length?`<div class="notice">Pilih padanan MyAnimeList yang paling sesuai. Hasil dari provider Tenrai.</div><div class="grid">${d.items.slice(0,6).map(x=>`<article class="card" onclick="openMal(${Number(x.mal_id)})"><div class="poster">${x.image?`<img loading="lazy" src="${esc(x.image)}" referrerpolicy="no-referrer">`:'<div class="noimg">✧</div>'}</div><div class="cardbody"><div class="cardtitle">${esc(x.title)}</div><div class="cardsub">MAL ID ${Number(x.mal_id)} · ${x.score||'N/A'}</div></div></article>`).join('')}</div>`:'<div class="empty">Tidak ditemukan padanan MAL untuk judul ini. Coba kata kunci judul Inggris/Jepang atau gunakan menu MyAnimeList.</div>';}catch(e){target.innerHTML=`<div class="notice error"><b>Pencarian MAL gagal.</b><br>${esc(e.message)}<div class="toolbar" style="margin-top:10px"><button class="action" onclick="lookupMal(decodeURIComponent('${jsArg(title)}'))">Coba lagi</button><button class="action" onclick="loadTab('mal')">Buka pencarian MAL</button></div></div>`;}}
async function openMal(id){rememberCurrent();setLoading('MEMBUKA MYANIMELIST DOSSIER');try{const res=await api('/api/mal/anime/'+id);const d=res.data;const img=d.images?.jpg?.large_image_url||d.images?.webp?.large_image_url||'';const genres=(d.genres||[]).concat(d.themes||[],d.demographics||[]).map(x=>`<span class="pill">${esc(x.name)}</span>`).join('');const meta=[['Skor MAL',d.score?`★ ${d.score} / 10`:'Belum dinilai'],['Peringkat',d.rank?`#${d.rank}`:'—'],['Popularitas',d.popularity?`#${d.popularity}`:'—'],['Anggota',d.members?.toLocaleString('id-ID')||'—'],['Tipe',d.type||'—'],['Episode',d.episodes??'—'],['Status',d.status||'—'],['Musim',d.season&&d.year?`${d.season} ${d.year}`:d.year||'—'],['Durasi',d.duration||'—'],['Rating usia',d.rating||'—'],['Studio',(d.studios||[]).map(x=>x.name).join(', ')||'—'],['Produser',(d.producers||[]).map(x=>x.name).join(', ')||'—']].map(([k,v])=>`<div><b>${esc(k)}</b>${esc(v)}</div>`).join('');main.innerHTML=`<div class="toolbar"><button class="action" onclick="goBack()">← Kembali</button><a class="source-link" href="${esc(d.url||'https://myanimelist.net/anime/'+id)}" target="_blank" rel="noopener">Buka MyAnimeList ↗</a></div><section class="content-card"><div class="detail-grid"><div>${img?`<img class="detail-poster" src="${esc(img)}" referrerpolicy="no-referrer" alt="poster MAL">`:''}</div><div><div class="eyebrow">MYANIMELIST · ANIME ID ${Number(id)}</div><h1 class="detail-title">${esc(d.title||'Anime')}</h1><div class="desc">${esc(d.title_english||'')} ${d.title_japanese?' · '+esc(d.title_japanese):''}</div><div class="toolbar" style="margin-top:12px">${genres}</div><div class="meta">${meta}</div><h3>Sinopsis</h3><p class="desc">${esc(d.synopsis||'Sinopsis belum tersedia.')}</p>${d.background?`<h3>Latar Belakang</h3><p class="desc">${esc(d.background)}</p>`:''}<p class="muted">Sumber metadata: MyAnimeList · Tenrai API. Informasi dapat berubah mengikuti pembaruan MAL.</p></div></div></section><div class="toolbar"><button class="action primary" onclick="addToLibrary(${Number(id)},decodeURIComponent('${jsArg(d.title||'Anime')}'),decodeURIComponent('${jsArg(img)}'),decodeURIComponent('${jsArg(d.url||'')}'))">♡ Simpan ke Koleksi</button><button class="action" onclick="loadMalExtra(${Number(id)},'characters')">Karakter & VA</button><button class="action" onclick="loadMalExtra(${Number(id)},'staff')">Staf</button><button class="action" onclick="loadMalExtra(${Number(id)},'recommendations')">Rekomendasi</button><button class="action" onclick="loadMalExtra(${Number(id)},'reviews')">Review</button><button class="action" onclick="loadMalExtra(${Number(id)},'statistics')">Statistik</button><button class="action" onclick="loadMalExtra(${Number(id)},'relations')">Relasi</button></div><div id="malExtra"></div>${sectionHead('Tema Musik','OPENING & ENDING THEMES')}<div class="content-card">${[...(d.theme?.openings||[]),...(d.theme?.endings||[])].length?[...(d.theme?.openings||[]).map(x=>'Opening · '+x),...(d.theme?.endings||[]).map(x=>'Ending · '+x)].map(x=>`<div class="listbtn">${esc(x)}</div>`).join(''):'<div class="empty">Informasi lagu tema tidak tersedia.</div>'}</div>`;window.scrollTo({top:0,behavior:'smooth'});}catch(e){showError(e);}}
async function loadMalExtra(id,kind){const box=document.getElementById('malExtra');if(!box)return;box.innerHTML='<div class="loading">MEMUAT DATA KOMUNITAS MAL</div>';try{const d=await api(`/api/mal/extra/${id}/${kind}`);const data=d.data;let html='';if(kind==='statistics'){html=`<div class="content-card"><h3>Distribusi anggota</h3><div class="meta">${Object.entries(data||{}).filter(([k,v])=>typeof v!=='object').map(([k,v])=>`<div><b>${esc(k.replaceAll('_',' '))}</b>${esc(v)}</div>`).join('')}</div></div>`;}else if(kind==='characters'){html=`<div class="grid">${(data||[]).slice(0,30).map(x=>`<article class="card"><div class="poster">${x.character?.images?.jpg?.image_url?`<img loading="lazy" src="${esc(x.character.images.jpg.image_url)}">`:'<div class="noimg">✧</div>'}</div><div class="cardbody"><div class="cardtitle">${esc(x.character?.name||'Unknown')}</div><div class="cardsub">${esc(x.role||'Character')}</div><div class="desc">${esc((x.voice_actors||[]).slice(0,2).map(v=>v.person?.name+' · '+v.language).join(' / '))}</div></div></article>`).join('')}</div>`;}else if(kind==='staff'){html=`<div class="content-card"><div class="list">${(data||[]).slice(0,35).map(x=>`<div class="listbtn">${esc(x.person?.name||'Unknown')} <span class="muted">— ${esc((x.positions||[]).join(', '))}</span></div>`).join('')}</div></div>`;}else if(kind==='recommendations'){html=`<div class="grid">${(data||[]).slice(0,10).map(x=>`<article class="card" onclick="openMal(${Number(x.entry?.mal_id||0)})"><div class="poster">${x.entry?.images?.jpg?.image_url?`<img loading="lazy" src="${esc(x.entry.images.jpg.image_url)}">`:'<div class="noimg">✧</div>'}</div><div class="cardbody"><div class="cardtitle">${esc(x.entry?.title||'Recommendation')}</div><div class="cardsub">${esc(x.votes||'')} votes</div></div></article>`).join('')}</div>`;}else if(kind==='reviews'){html=`<div class="list">${(data||[]).slice(0,8).map(x=>`<article class="content-card"><b>${esc(x.user?.username||'MAL user')}</b><p class="desc">${esc((x.review||'').slice(0,900))}</p><span class="muted">${esc(x.score||'')} / 10 · ${esc(x.date||'')}</span></article>`).join('')}</div>`;}else{html=`<div class="list">${(data||[]).map(x=>`<div class="listbtn">${esc(x.relation||'Related')}: ${(x.entry||[]).map(e=>esc(e.name)).join(', ')}</div>`).join('')}</div>`;}box.innerHTML=sectionHead(kind.toUpperCase(),'MYANIMELIST COMMUNITY DATA')+(html||'<div class="empty">Data belum tersedia.</div>');}catch(e){box.innerHTML=`<div class="notice error">${esc(e.message)}</div>`;}}
async function openEpisode(url){rememberCurrent();setLoading('MENYIAPKAN SMART PLAYER');try{const d=await api('/api/episode?url='+encodeURIComponent(url));postApi('/api/history',{episode_url:d.url,title:d.title,anime_title:d.title.split(/\s+Episode\s+/i)[0],image:d.image}).catch(()=>{});const embeds=(d.embeds||[]).filter(Boolean);currentPlayerUrl=embeds[0]||d.url;const safePage=esc(d.url);let player='';if(embeds.length){const options=embeds.map((u,i)=>`<option value="${esc(u)}">Server ${i+1} · ${esc(new URL(u).hostname)}</option>`).join('');player=`<div class="player-tools"><select class="server-select" id="serverSelect" onchange="switchPlayer(this.value)">${options}</select><button class="action primary" onclick="openExternalPlayer()">▶ PLAY</button><button class="action" onclick="reloadPlayer()">⟳ Muat ulang</button></div><div class="playerbox"><iframe id="episodeFrame" src="${esc(embeds[0])}" title="${esc(d.title)}" allow="autoplay; fullscreen; picture-in-picture; encrypted-media" allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe></div><div class="player-state" id="playerState">✧ Player dimuat melalui embed. Jika area tetap putih/kosong atau muncul pesan Firefox, server kemungkinan menolak iframe. Tekan tombol <b>PLAY</b> untuk membuka pemutar.</div><div class="muted">Terdeteksi ${embeds.length} alamat embed dari markup halaman episode. Deteksi otomatis keberhasilan video lintas-domain dibatasi oleh keamanan browser.</div>`;}else{player=`<div class="playerbox"><div class="player-placeholder">Tidak ada URL embed publik yang terdeteksi pada halaman ini.<br><a class="source-link" target="_blank" rel="noopener noreferrer" href="${safePage}">Buka halaman episode ↗</a></div></div><div class="player-tools"><a class="action primary" target="_blank" rel="noopener noreferrer" href="${safePage}">▶ PLAY</a></div>`;}const poster=d.image?`<img src="${esc(d.image)}" loading="lazy" referrerpolicy="no-referrer" alt="poster">`:'';const nav=(d.episodes||[]).map(ep=>`<button class="listbtn" onclick="openEpisode(decodeURIComponent('${jsArg(ep.url)}'))">▶ &nbsp;${esc(ep.title)}</button>`).join('');main.innerHTML=`<div class="toolbar"><button class="action" onclick="goBack()">← Kembali</button><a class="source-link" target="_blank" rel="noopener noreferrer" href="${safePage}">Halaman sumber ↗</a></div>${sectionHead(d.title,'NOCTURNE · SMART PLAYER')}<div class="content-card"><div class="player-poster">${poster}<div><div class="eyebrow">NOW PLAYING</div><div class="ep-name">${esc(d.title)}</div><div class="muted">Charcoal Edition · Dual playback</div></div></div>${player}<div class="notice">Pemutar menampilkan embed langsung di aplikasi dan menyediakan mode PLAY untuk membuka sumber sebagai halaman utama di dalam aplikasi. Jika host menolak iframe melalui X-Frame-Options atau Content-Security-Policy, gunakan pemutar eksternal. Aplikasi tidak mengekstrak stream tersembunyi, mengunduh video, atau melewati proteksi.</div></div>${sectionHead('Navigasi Episode','EPISODE INDEX')}<div class="list">${nav||'<div class="empty">Navigasi episode tidak tersedia pada halaman ini.</div>'}</div><div class="toolbar" style="margin-top:14px">${d.previous?`<button class="action" onclick="openEpisode(decodeURIComponent('${jsArg(d.previous.url)}'))">← Sebelumnya</button>`:''}${d.next?`<button class="action" onclick="openEpisode(decodeURIComponent('${jsArg(d.next.url)}'))">Berikutnya →</button>`:''}</div>`;if(embeds.length){setTimeout(()=>{const st=document.getElementById('playerState');if(st&&st.textContent.startsWith('✧'))st.innerHTML='⚠ Jika player masih kosong, kemungkinan provider melarang iframe. Tekan PLAY untuk membuka pemutar sumber. Status pemuatan lintas-domain dibatasi oleh keamanan browser.';},7000);}window.scrollTo({top:0,behavior:'smooth'});}catch(e){showError(e);}}
function switchPlayer(u){currentPlayerUrl=u;const f=document.getElementById('episodeFrame');if(f)f.src=u;const st=document.getElementById('playerState');if(st)st.textContent='Server diganti. Jika embed ditolak, tekan PLAY untuk membuka pemutar sumber.';}
function openExternalPlayer(){if(currentPlayerUrl)window.open(currentPlayerUrl,'_blank','noopener,noreferrer');}
function reloadPlayer(){const f=document.getElementById('episodeFrame');if(f){f.src='about:blank';setTimeout(()=>f.src=currentPlayerUrl,120);}}
function goBack(){const previous=backStack.pop();if(previous){currentTab=previous.tab||'home';main.innerHTML=previous.html;setTabActive(currentTab);statusText.textContent='SHADOW INDEX · '+currentTab.toUpperCase();}else{loadTab('home');}}
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
