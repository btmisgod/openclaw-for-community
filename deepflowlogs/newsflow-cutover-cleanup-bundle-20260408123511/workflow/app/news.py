from __future__ import annotations

import html
import re
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus, urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from pytz import timezone, UTC

from .config import load_settings


SETTINGS = load_settings()
TZ = timezone(SETTINGS.timezone)
HTTP = requests.Session()
HTTP.headers.update(
    {
        "User-Agent": "newsflow-mvp/0.1 (+https://example.invalid)",
        "Accept-Language": "en-US,en;q=0.8",
    }
)


SECTION_FEEDS = {
    "政治经济": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
    ],
    "科技": [
        "https://feeds.arstechnica.com/arstechnica/index",
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://blog.google/rss/",
        "https://openai.com/news/rss.xml",
    ],
    "体育娱乐": [
        "https://www.espn.com/espn/rss/news",
        "https://www.hollywoodreporter.com/feed/",
        "https://variety.com/feed/",
        "https://www.billboard.com/feed/",
        "https://www.si.com/rss/si_topstories.rss",
    ],
    "其他": [
        "https://www.nasa.gov/rss/dyn/breaking_news.rss",
        "https://www.who.int/rss-feeds/news-english.xml",
        "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Health.xml",
    ],
}


def _parse_dt(value: str | None):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except Exception:
        dt = dtparser.parse(value)
    if dt.tzinfo is None:
        dt = UTC.localize(dt)
    return dt.astimezone(TZ)


def _clean_title(title: str) -> str:
    title = html.unescape(title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def _extract_images(link: str) -> list[str]:
    images: list[str] = []
    try:
        resp = HTTP.get(link, timeout=5)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for selector in [
            ('meta[property="og:image"]', "content"),
            ('meta[name="twitter:image"]', "content"),
        ]:
            for tag in soup.select(selector[0]):
                val = tag.get(selector[1])
                if val:
                    images.append(urljoin(link, val))
        for img in soup.select("img[src]"):
            src = img.get("src")
            if src:
                images.append(urljoin(link, src))
        dedup: list[str] = []
        for item in images:
            if item not in dedup and item.startswith("http"):
                dedup.append(item)
        return dedup[:5]
    except Exception:
        return []


def _append_image(images: list[str], link: str, value: str | None) -> None:
    if not value:
        return
    resolved = urljoin(link, value)
    if resolved.startswith("http") and resolved not in images:
        images.append(resolved)


def _entry_images(entry: Any, link: str) -> list[str]:
    images: list[str] = []
    for media in entry.get("media_content") or []:
        if isinstance(media, dict):
            _append_image(images, link, media.get("url"))
    for media in entry.get("media_thumbnail") or []:
        if isinstance(media, dict):
            _append_image(images, link, media.get("url"))
    for content in entry.get("content") or []:
        if not isinstance(content, dict):
            continue
        raw_value = content.get("value")
        if not raw_value:
            continue
        soup = BeautifulSoup(raw_value, "html.parser")
        for tag in soup.select("img[src]"):
            _append_image(images, link, tag.get("src"))
    summary_html = entry.get("summary") or ""
    if summary_html:
        soup = BeautifulSoup(summary_html, "html.parser")
        for tag in soup.select("img[src]"):
            _append_image(images, link, tag.get("src"))
    return images[:5]


def collect_news(section: str, limit: int) -> list[dict[str, Any]]:
    now = datetime.now(TZ)
    cutoff = now - timedelta(hours=24)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for feed_url in SECTION_FEEDS[section]:
        try:
            feed_resp = HTTP.get(feed_url, timeout=10)
            feed_resp.raise_for_status()
            parsed = feedparser.parse(feed_resp.content)
        except Exception:
            continue
        for entry in parsed.entries:
            published = _parse_dt(entry.get("published") or entry.get("updated"))
            if not published or published < cutoff:
                continue
            title = _clean_title(entry.get("title", ""))
            if not title:
                continue
            norm = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", title.lower())
            if norm in seen:
                continue
            seen.add(norm)
            link = entry.get("link", "")
            source_media = None
            if entry.get("source"):
                source_media = entry["source"].get("title")
            if not source_media:
                source_media = parsed.feed.get("title", "Unknown Source")
            summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)
            items.append(
                {
                    "title": title,
                    "source_media": source_media,
                    "published_at": published.isoformat(),
                    "link": link,
                    "summary_en": summary[:800],
                    "images": _entry_images(entry, link),
                }
            )
    items.sort(key=lambda x: x["published_at"], reverse=True)
    selected = items[:limit]
    for item in selected:
        if item.get("images") or not item.get("link"):
            continue
        item["images"] = _extract_images(item["link"])
    return selected


def search_benchmark_samples(query: str, limit: int = 4) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    seen: set[str] = set()
    url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        resp = HTTP.get(url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.select(".result"):
            link_tag = card.select_one(".result__a")
            snippet_tag = card.select_one(".result__snippet")
            if not link_tag:
                continue
            href = link_tag.get("href") or ""
            title = _clean_title(link_tag.get_text(" ", strip=True))
            snippet = snippet_tag.get_text(" ", strip=True) if snippet_tag else ""
            source_media = ""
            source_tag = card.select_one(".result__url")
            if source_tag:
                source_media = source_tag.get_text(" ", strip=True)
            if not href or not title or href in seen:
                continue
            seen.add(href)
            samples.append(
                {
                    "title": title,
                    "source_media": source_media or "DuckDuckGo Search",
                    "link": href,
                    "snippet": snippet[:280],
                    "search_query": query,
                    "search_mode": "open_search",
                }
            )
            if len(samples) >= limit:
                return samples[:limit]
    except Exception:
        pass

    rss_url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    try:
        resp = HTTP.get(rss_url, timeout=15)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        for entry in parsed.entries:
            href = entry.get("link", "")
            title = _clean_title(entry.get("title", ""))
            summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)
            source_media = parsed.feed.get("title", "Google News Search")
            if not href or not title or href in seen:
                continue
            seen.add(href)
            samples.append(
                {
                    "title": title,
                    "source_media": source_media,
                    "link": href,
                    "snippet": summary[:280],
                    "search_query": query,
                    "search_mode": "open_search",
                }
            )
            if len(samples) >= limit:
                break
    except Exception:
        return []
    return samples[:limit]
