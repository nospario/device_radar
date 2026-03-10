"""BBC News RSS headline integration for Device Radar.

Fetches headlines from selected BBC RSS feeds, stores them in SQLite,
tracks per-device read state, and provides formatted spoken suffixes
for Alexa TTS messages.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

import bt_db

logger = logging.getLogger("bt_news")


# ---------------------------------------------------------------------------
# BBC RSS Feed Registry
# ---------------------------------------------------------------------------

BBC_FEEDS: dict[str, dict[str, str]] = {
    # News
    "top_stories":         {"name": "Top Stories",           "category": "news",  "url": "https://feeds.bbci.co.uk/news/rss.xml"},
    "uk":                  {"name": "UK",                    "category": "news",  "url": "https://feeds.bbci.co.uk/news/uk/rss.xml"},
    "world":               {"name": "World",                 "category": "news",  "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    "business":            {"name": "Business",              "category": "news",  "url": "https://feeds.bbci.co.uk/news/business/rss.xml"},
    "politics":            {"name": "Politics",              "category": "news",  "url": "https://feeds.bbci.co.uk/news/politics/rss.xml"},
    "technology":          {"name": "Technology",            "category": "news",  "url": "https://feeds.bbci.co.uk/news/technology/rss.xml"},
    "science_environment": {"name": "Science & Environment", "category": "news",  "url": "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"},
    "health":              {"name": "Health",                "category": "news",  "url": "https://feeds.bbci.co.uk/news/health/rss.xml"},
    "education":           {"name": "Education",             "category": "news",  "url": "https://feeds.bbci.co.uk/news/education/rss.xml"},
    "entertainment_arts":  {"name": "Entertainment & Arts",  "category": "news",  "url": "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml"},
    "england":             {"name": "England",               "category": "news",  "url": "https://feeds.bbci.co.uk/news/england/rss.xml"},
    # Sport
    "sport":               {"name": "Sport",                 "category": "sport", "url": "https://feeds.bbci.co.uk/sport/rss.xml"},
    "football":            {"name": "Football",              "category": "sport", "url": "https://feeds.bbci.co.uk/sport/football/rss.xml"},
    "cricket":             {"name": "Cricket",               "category": "sport", "url": "https://feeds.bbci.co.uk/sport/cricket/rss.xml"},
    "formula1":            {"name": "Formula 1",             "category": "sport", "url": "https://feeds.bbci.co.uk/sport/formula1/rss.xml"},
    "rugby_union":         {"name": "Rugby Union",           "category": "sport", "url": "https://feeds.bbci.co.uk/sport/rugby-union/rss.xml"},
    "tennis":              {"name": "Tennis",                "category": "sport", "url": "https://feeds.bbci.co.uk/sport/tennis/rss.xml"},
    "golf":                {"name": "Golf",                  "category": "sport", "url": "https://feeds.bbci.co.uk/sport/golf/rss.xml"},
    "nottm_forest":        {"name": "Nottm Forest",          "category": "sport", "url": "https://feeds.bbci.co.uk/sport/football/teams/nottingham-forest/rss.xml"},
}

# Prefixes to strip from headlines for cleaner TTS
_TITLE_PREFIXES = re.compile(r"^(Watch:|Live:|Video:|Listen:|In pictures:)\s*", re.IGNORECASE)

# In-memory cache: feed_key → last_fetched_timestamp
_feed_last_fetched: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_available_feeds() -> list[dict[str, str]]:
    """Return list of {key, name, category} for all BBC feeds (for the UI)."""
    return [
        {"key": k, "name": v["name"], "category": v["category"]}
        for k, v in BBC_FEEDS.items()
    ]


# ---------------------------------------------------------------------------
# RSS fetching and DB storage
# ---------------------------------------------------------------------------

def _clean_title(title: str) -> str:
    """Clean an RSS title for spoken TTS output."""
    title = html.unescape(title).strip()
    title = _TITLE_PREFIXES.sub("", title)
    return title


async def refresh_feeds(
    feed_keys: list[str],
    db_path: Path,
    config: dict[str, Any] | None = None,
) -> None:
    """Fetch RSS for selected feeds, parse headlines, upsert into DB.

    Skips feeds that were fetched within ``news_cache_minutes``.
    """
    cache_minutes = 15
    if config:
        cache_minutes = config.get("news_cache_minutes", 15)
    cache_ttl = cache_minutes * 60
    now = time.time()

    conn = bt_db.get_connection(db_path)

    try:
        async with httpx.AsyncClient() as client:
            for key in feed_keys:
                feed = BBC_FEEDS.get(key)
                if not feed:
                    continue

                # Check in-memory cache
                last = _feed_last_fetched.get(key, 0)
                if now - last < cache_ttl:
                    continue

                try:
                    resp = await client.get(feed["url"], timeout=10, follow_redirects=True)
                    resp.raise_for_status()
                except Exception as e:
                    logger.warning("Failed to fetch RSS feed %s: %s", key, e)
                    continue

                try:
                    root = ET.fromstring(resp.content)
                except ET.ParseError as e:
                    logger.warning("Failed to parse RSS feed %s: %s", key, e)
                    continue

                count = 0
                for item in root.findall(".//item"):
                    title_el = item.find("title")
                    guid_el = item.find("guid")
                    link_el = item.find("link")
                    pub_el = item.find("pubDate")

                    if title_el is None or title_el.text is None:
                        continue

                    title = _clean_title(title_el.text)
                    guid = (guid_el.text if guid_el is not None and guid_el.text else
                            link_el.text if link_el is not None and link_el.text else
                            None)
                    if not guid:
                        continue

                    published = now
                    if pub_el is not None and pub_el.text:
                        try:
                            published = parsedate_to_datetime(pub_el.text).timestamp()
                        except Exception:
                            pass

                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO news_headlines "
                            "(guid, feed_key, title, published, fetched_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (guid, key, title, published, now),
                        )
                        count += 1
                    except Exception:
                        pass

                conn.commit()
                _feed_last_fetched[key] = now
                logger.info("Refreshed feed %s: %d new headlines", key, count)

        # Prune old headlines (> 7 days) — runs on every refresh
        cutoff = now - (7 * 86400)
        conn.execute(
            "DELETE FROM news_read WHERE headline_id IN "
            "(SELECT id FROM news_headlines WHERE published < ?)",
            (cutoff,),
        )
        conn.execute("DELETE FROM news_headlines WHERE published < ?", (cutoff,))
        conn.commit()

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Unread headline retrieval and read marking
# ---------------------------------------------------------------------------

def get_unread_headlines(
    mac: str,
    feed_keys: list[str],
    db_path: Path,
    count: int = 3,
) -> list[dict[str, Any]]:
    """Return up to ``count`` unread headlines for this device from selected feeds."""
    if not feed_keys:
        return []

    placeholders = ",".join("?" for _ in feed_keys)
    conn = bt_db.get_connection(db_path)

    try:
        rows = conn.execute(
            f"""
            SELECT h.id, h.title, h.feed_key, h.published
            FROM news_headlines h
            WHERE h.feed_key IN ({placeholders})
              AND h.id NOT IN (
                  SELECT nr.headline_id FROM news_read nr
                  WHERE nr.mac_address = ?
              )
            ORDER BY h.published DESC
            LIMIT ?
            """,
            [*feed_keys, mac.upper(), count],
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_headlines_read(
    mac: str,
    headline_ids: list[int],
    db_path: Path,
) -> None:
    """Mark headlines as read for this device."""
    if not headline_ids:
        return

    now = time.time()
    conn = bt_db.get_connection(db_path)
    try:
        for hid in headline_ids:
            conn.execute(
                "INSERT OR IGNORE INTO news_read "
                "(mac_address, headline_id, read_at) VALUES (?, ?, ?)",
                (mac.upper(), hid, now),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_news_suffix(headlines: list[dict[str, Any]]) -> str:
    """Format headlines into a spoken suffix for Alexa TTS.

    Returns something like:
    "In the news: PM announces energy plan. England win test. Forest sign striker."
    """
    if not headlines:
        return ""

    titles = [h["title"] for h in headlines]
    # Ensure each title ends with a period for natural TTS pausing
    cleaned = []
    for t in titles:
        t = t.rstrip(".")
        cleaned.append(f"{t}.")

    return "In the news: " + " ".join(cleaned)


# ---------------------------------------------------------------------------
# Main entry point (called from bt_alexa.py)
# ---------------------------------------------------------------------------

async def get_device_news_suffix(
    device: dict[str, Any],
    config: dict[str, Any],
    db_path: Path,
) -> str:
    """Get formatted news suffix for a device's Alexa message.

    1. Parse device's selected feeds from ``news_feeds`` JSON column
    2. Refresh those feeds (fetches new headlines if cache expired)
    3. Get unread headlines for this device
    4. Mark them as read
    5. Return formatted suffix string
    """
    if not config.get("news_enabled", True):
        return ""

    # Parse device's selected feeds
    feeds_json = device.get("news_feeds") or ""
    try:
        feed_keys = json.loads(feeds_json) if feeds_json else []
    except (json.JSONDecodeError, TypeError):
        feed_keys = []

    if not feed_keys:
        return ""

    # Filter to valid feed keys
    feed_keys = [k for k in feed_keys if k in BBC_FEEDS]
    if not feed_keys:
        return ""

    mac = device.get("mac_address", "")
    if not mac:
        return ""

    headline_count = config.get("news_headline_count", 3)

    # Refresh feeds (fetches new headlines if cache expired)
    try:
        await refresh_feeds(feed_keys, db_path, config)
    except Exception:
        logger.error("Failed to refresh news feeds", exc_info=True)

    # Get unread headlines
    headlines = get_unread_headlines(mac, feed_keys, db_path, count=headline_count)
    if not headlines:
        return ""

    # Mark as read
    headline_ids = [h["id"] for h in headlines]
    try:
        mark_headlines_read(mac, headline_ids, db_path)
    except Exception:
        logger.error("Failed to mark headlines as read", exc_info=True)

    suffix = format_news_suffix(headlines)
    logger.info("News suffix for %s: %s", mac, suffix)
    return suffix
