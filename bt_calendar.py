"""Apple Calendar (iCloud CalDAV) integration for Device Radar.

Fetches today's and tomorrow's events from configured iCloud calendars,
caches them in memory with a configurable TTL, and provides formatted
event context strings for Ollama proximity prompts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("bt_calendar")


# ---------------------------------------------------------------------------
# Environment loading
# ---------------------------------------------------------------------------

def _load_env() -> None:
    """Load .device-radar.env if env vars aren't already set."""
    env_path = Path("/home/pi/.device-radar.env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if key and key not in os.environ:
            os.environ[key] = val


_load_env()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CalendarEvent:
    """A single calendar event."""
    summary: str
    start: datetime
    end: datetime
    all_day: bool = False
    calendar_name: str = ""


@dataclass
class _CacheEntry:
    """Cached calendar events with expiry."""
    events: list[CalendarEvent]
    fetched_at: float
    ttl_seconds: float

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.fetched_at) > self.ttl_seconds


# Cache keyed by frozenset of calendar names.
_cache: dict[frozenset[str], _CacheEntry] = {}


# ---------------------------------------------------------------------------
# CalDAV fetching (synchronous — run in executor)
# ---------------------------------------------------------------------------

def _fetch_events_sync(
    url: str,
    username: str,
    password: str,
    calendar_names: list[str],
) -> list[CalendarEvent]:
    """Fetch today's and tomorrow's events from CalDAV (synchronous)."""
    import caldav

    events: list[CalendarEvent] = []
    today = date.today()
    tomorrow = today + timedelta(days=1)
    start_dt = datetime.combine(today, datetime.min.time())
    end_dt = datetime.combine(tomorrow + timedelta(days=1), datetime.min.time())

    try:
        client = caldav.DAVClient(url=url, username=username, password=password)
        principal = client.principal()
        calendars = principal.calendars()
    except Exception:
        logger.error("Failed to connect to CalDAV server at %s", url, exc_info=True)
        return events

    name_set = {n.lower() for n in calendar_names}
    matched = [c for c in calendars if (c.name or "").lower() in name_set]

    if not matched:
        available = [c.name for c in calendars]
        logger.warning(
            "No matching calendars found. Requested: %s, Available: %s",
            calendar_names, available,
        )
        return events

    for cal in matched:
        cal_name = cal.name or ""
        try:
            results = cal.date_search(start=start_dt, end=end_dt, expand=True)
            for event_obj in results:
                try:
                    vevent = event_obj.vobject_instance.vevent
                    summary = (
                        str(vevent.summary.value)
                        if hasattr(vevent, "summary")
                        else "(no title)"
                    )
                    dtstart = vevent.dtstart.value
                    dtend = (
                        vevent.dtend.value
                        if hasattr(vevent, "dtend")
                        else dtstart
                    )

                    all_day = isinstance(dtstart, date) and not isinstance(
                        dtstart, datetime
                    )

                    if all_day:
                        start_datetime = datetime.combine(
                            dtstart, datetime.min.time()
                        )
                        end_datetime = datetime.combine(
                            dtend, datetime.min.time()
                        )
                    else:
                        start_datetime = (
                            dtstart
                            if isinstance(dtstart, datetime)
                            else datetime.combine(dtstart, datetime.min.time())
                        )
                        end_datetime = (
                            dtend
                            if isinstance(dtend, datetime)
                            else datetime.combine(dtend, datetime.min.time())
                        )

                    events.append(
                        CalendarEvent(
                            summary=summary,
                            start=start_datetime,
                            end=end_datetime,
                            all_day=all_day,
                            calendar_name=cal_name,
                        )
                    )
                except Exception:
                    logger.debug(
                        "Failed to parse event in %s", cal_name, exc_info=True
                    )
        except Exception:
            logger.error(
                "Failed to fetch events from calendar '%s'",
                cal_name,
                exc_info=True,
            )

    events.sort(key=lambda e: e.start)
    return events


# ---------------------------------------------------------------------------
# Async wrapper with caching
# ---------------------------------------------------------------------------

async def get_events(
    config: dict[str, Any],
    calendar_names: list[str] | None = None,
) -> list[CalendarEvent]:
    """Get calendar events for the given calendars, using cache.

    Returns an empty list if calendar integration is disabled or credentials
    are missing.
    """
    if not config.get("calendar_enabled", False):
        return []

    if calendar_names is None:
        calendar_names = config.get("calendar_names", [])
    if not calendar_names:
        return []

    cache_key = frozenset(calendar_names)
    ttl = config.get("calendar_cache_minutes", 15) * 60

    cached = _cache.get(cache_key)
    if cached and not cached.is_expired:
        logger.debug("Calendar cache hit for %s", calendar_names)
        return cached.events

    url = config.get("calendar_url", "https://caldav.icloud.com")
    username_env = config.get("calendar_username_env", "APPLE_ID_EMAIL")
    password_env = config.get("calendar_password_env", "APPLE_ID_APP_PASSWORD")
    username = os.environ.get(username_env, "")
    password = os.environ.get(password_env, "")

    if not username or not password:
        logger.warning(
            "Calendar credentials not set (need %s and %s in environment)",
            username_env, password_env,
        )
        return []

    loop = asyncio.get_running_loop()
    try:
        events = await asyncio.wait_for(
            loop.run_in_executor(
                None, _fetch_events_sync, url, username, password, calendar_names,
            ),
            timeout=30,
        )
    except asyncio.TimeoutError:
        logger.warning("Calendar fetch timed out after 30s")
        if cached:
            return cached.events
        return []
    except Exception:
        logger.error("Calendar fetch failed", exc_info=True)
        if cached:
            return cached.events
        return []

    _cache[cache_key] = _CacheEntry(
        events=events,
        fetched_at=time.time(),
        ttl_seconds=ttl,
    )
    logger.info("Fetched %d calendar events for %s", len(events), calendar_names)
    return events


# ---------------------------------------------------------------------------
# Context formatting
# ---------------------------------------------------------------------------

def format_event_context(
    events: list[CalendarEvent],
    person_name: str,
) -> str:
    """Format calendar events into a context string for Ollama prompts.

    Returns a string like:
    "Richard has the following events today: Dentist at 2:00 PM. "

    Returns an empty string if there are no events.
    """
    if not events:
        return ""

    today = date.today()
    tomorrow = today + timedelta(days=1)

    today_events: list[str] = []
    tomorrow_events: list[str] = []

    for ev in events:
        event_date = ev.start.date()
        if ev.all_day:
            desc = f"{ev.summary} (all day)"
        else:
            desc = f"{ev.summary} at {ev.start.strftime('%-I:%M %p')}"

        if event_date == today:
            today_events.append(desc)
        elif event_date == tomorrow:
            tomorrow_events.append(desc)

    parts: list[str] = []
    if today_events:
        parts.append(
            f"{person_name} has the following events today: "
            f"{', '.join(today_events)}."
        )
    if tomorrow_events:
        parts.append(
            f"{person_name} has these events tomorrow: "
            f"{', '.join(tomorrow_events)}."
        )

    if not parts:
        return ""
    return " ".join(parts) + " "


# ---------------------------------------------------------------------------
# Device helpers
# ---------------------------------------------------------------------------

def resolve_device_to_person_key(
    device_name: str,
    config: dict[str, Any],
) -> str | None:
    """Reverse-lookup a device friendly_name to its person_aliases key.

    E.g., "Richard's iPhone" -> "richard"
    Returns None if no match found.
    """
    aliases = config.get("person_aliases", {})
    for person_key, dev_name in aliases.items():
        if dev_name.lower() == device_name.lower():
            return person_key.lower()
    return None


def _resolve_person_name(device_name: str, config: dict[str, Any]) -> str:
    """Resolve a device name to a capitalised person name."""
    key = resolve_device_to_person_key(device_name, config)
    if key:
        return key.capitalize()
    if "\u2019s " in device_name or "'s " in device_name:
        return device_name.replace("\u2019s ", "'s ").split("'s ")[0]
    return device_name


async def get_device_calendar_context(
    device: dict[str, Any],
    config: dict[str, Any],
) -> str:
    """Get formatted calendar context for a device.

    Reads the device's calendar_calendars JSON field to determine which
    calendars to query.  Resolves the person name from person_aliases.

    Returns an empty string if no calendars configured or calendar disabled.
    """
    if not config.get("calendar_enabled", False):
        return ""

    raw = device.get("calendar_calendars") or ""
    if not raw:
        return ""

    try:
        calendar_names = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""

    if not calendar_names or not isinstance(calendar_names, list):
        return ""

    dev_name = device.get("friendly_name") or device.get("advertised_name") or ""
    person_name = _resolve_person_name(dev_name, config)

    events = await get_events(config, calendar_names)
    return format_event_context(events, person_name)
