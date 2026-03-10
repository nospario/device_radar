"""Weather integration for Device Radar.

Fetches current weather conditions from the Open-Meteo API (free, no API key)
and returns a formatted string suitable for Alexa TTS prepending.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("bt_weather")

# WMO Weather interpretation codes → spoken descriptions
_WMO_CODES: dict[int, str] = {
    0: "clear skies",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "foggy",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "heavy showers",
    85: "light snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}


@dataclass
class _CacheEntry:
    """Cached weather text with expiry."""
    text: str
    fetched_at: float
    ttl_seconds: float

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.fetched_at) > self.ttl_seconds


_cache: _CacheEntry | None = None


async def get_current_weather(config: dict[str, Any]) -> str:
    """Fetch current weather and return a formatted string.

    Returns a string like "10 degrees and partly cloudy" or empty string
    on any failure.
    """
    global _cache

    lat = config.get("weather_latitude")
    lng = config.get("weather_longitude")
    if lat is None or lng is None:
        return ""

    ttl = config.get("weather_cache_minutes", 30) * 60

    if _cache and not _cache.is_expired:
        return _cache.text

    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lng}"
        f"&current=temperature_2m,weather_code"
        f"&timezone=Europe%2FLondon"
    )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()

        current = data.get("current", {})
        temp = current.get("temperature_2m")
        code = current.get("weather_code")

        if temp is None:
            return ""

        temp_int = round(temp)
        description = _WMO_CODES.get(code, "")

        if description:
            text = f"{temp_int} degrees and {description}"
        else:
            text = f"{temp_int} degrees"

        _cache = _CacheEntry(text=text, fetched_at=time.time(), ttl_seconds=ttl)
        logger.info("Weather fetched: %s", text)
        return text

    except httpx.TimeoutException:
        logger.warning("Weather API timed out")
    except Exception as e:
        logger.error("Weather fetch error: %s", e)

    # Return stale cache if available
    if _cache:
        return _cache.text
    return ""
