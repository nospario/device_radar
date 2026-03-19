#!/usr/bin/env python3
"""Device Radar Telegram Bot — interactive presence queries via Ollama
and proactive arrival/departure notifications."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

import bt_db
import bt_search

try:
    import bt_kitkat_telegram
    _HAS_KITKAT = True
except ImportError:
    _HAS_KITKAT = False

logger = logging.getLogger("bt_telegram")

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
BASE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Environment & configuration
# ---------------------------------------------------------------------------

def _load_env() -> None:
    """Load .env file as fallback for environment variables."""
    try:
        from dotenv import load_dotenv
        load_dotenv("/home/pi/.device-radar.env")
    except ImportError:
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

_cached_credentials: tuple[str, str] | None = None


def load_config() -> dict[str, Any]:
    """Load config.json."""
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open() as f:
            return json.load(f)
    return {}


def get_telegram_credentials(config: dict[str, Any] | None = None) -> tuple[str, str]:
    """Return (bot_token, chat_id) from environment variables."""
    global _cached_credentials
    if _cached_credentials is not None:
        return _cached_credentials
    if config is None:
        config = load_config()
    token_env = config.get("telegram_token_env", "TELEGRAM_BOT_TOKEN")
    chat_id_env = config.get("telegram_chat_id_env", "TELEGRAM_CHAT_ID")
    creds = (os.environ.get(token_env, ""), os.environ.get(chat_id_env, ""))
    if creds[0] and creds[1]:
        _cached_credentials = creds
    return creds


# ---------------------------------------------------------------------------
# Notification sender (standalone — used by bt_scanner.py)
# ---------------------------------------------------------------------------

async def send_notification(device_name: str, event: str) -> None:
    """Send an arrival/departure notification to Telegram.

    Can be called from bt_scanner.py without the full bot running.
    Only requires httpx (no python-telegram-bot dependency).
    """
    token, chat_id = get_telegram_credentials()
    if not token or not chat_id:
        return

    escaped = html.escape(device_name)
    if event == "arrived":
        text = f"\U0001f4e1 <b>{escaped}</b> detected"
    else:
        text = f"\U0001f44b <b>{escaped}</b> departed"

    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload, timeout=10,
            )
    except Exception as e:
        logger.error("Telegram notification failed: %s", e)


# ---------------------------------------------------------------------------
# Intent routing
# ---------------------------------------------------------------------------

_PRESENCE_PATTERNS = [
    re.compile(r"\b(who'?s|who\s+is|is\s+anyone|anyone)\s+(home|in|here|present|around)\b", re.I),
    re.compile(r"\bis\s+\w+\s+(home|in|here|present|around)\b", re.I),
    re.compile(r"\bwhere\s+is\s+\w+\b", re.I),
    re.compile(r"\bwhen\s+did\s+\w+\s+(arrive|leave|depart|get\s+home|come\s+home|go)\b", re.I),
    re.compile(r"\bhow\s+long\s+has\s+\w+\s+been\s+(home|away|out|gone|here)\b", re.I),
    re.compile(r"\bwhat\s+devices?\s+(are|is)\s+(home|present|connected|detected)\b", re.I),
    re.compile(r"\blast\s+seen\b", re.I),
    re.compile(r"\bdevice\s+status\b", re.I),
]


def is_presence_query(text: str) -> bool:
    """Check if text matches a presence query pattern."""
    return any(p.search(text) for p in _PRESENCE_PATTERNS)


def _extract_person(text: str) -> str | None:
    """Extract a person name from a presence query."""
    patterns = [
        re.compile(r"\bis\s+(\w+)\s+(home|in|here|present|around)\b", re.I),
        re.compile(r"\bwhere\s+is\s+(\w+)\b", re.I),
        re.compile(r"\bwhen\s+did\s+(\w+)\s+", re.I),
        re.compile(r"\bhow\s+long\s+has\s+(\w+)\s+been\b", re.I),
        re.compile(r"\blast\s+seen\s+(\w+)\b", re.I),
    ]
    skip = {"anyone", "someone", "everybody", "everyone", "any", "the", "a", "all"}
    for p in patterns:
        m = p.search(text)
        if m:
            name = m.group(1).lower()
            if name not in skip:
                return name
    return None


# ---------------------------------------------------------------------------
# Device Radar queries
# ---------------------------------------------------------------------------

async def _api_get(path: str, params: dict | None = None) -> Any:
    """Query the local Device Radar REST API."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://localhost:8080{path}", params=params, timeout=5,
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("Device Radar API error (%s): %s", path, e)
        return None


def _resolve_person(
    name: str, config: dict[str, Any], conn,
) -> dict[str, Any] | None:
    """Resolve a person name to a device via aliases then fuzzy match."""
    aliases = config.get("person_aliases", {})
    target = aliases.get(name.lower())
    if target:
        dev = bt_db.get_device(conn, target)
        if dev:
            return dev
        for d in bt_db.get_all_devices(conn, include_hidden=True):
            if (d.get("friendly_name") or "").lower() == target.lower():
                return d
    # Fuzzy match on friendly_name
    for d in bt_db.get_all_devices(conn, include_hidden=True):
        if name.lower() in (d.get("friendly_name") or "").lower():
            return d
    return None


def _time_ago(ts: float | None) -> str:
    """Format a Unix timestamp as a relative time string."""
    if not ts:
        return "unknown"
    diff = time.time() - ts
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{int(diff / 60)}m ago"
    if diff < 86400:
        return f"{int(diff / 3600)}h ago"
    return f"{int(diff / 86400)}d ago"


async def answer_presence(
    text: str, config: dict[str, Any], db_path: Path,
) -> str:
    """Answer a presence query with a factual response."""
    lower = text.lower()

    # "who's home" / "is anyone home" / "what devices are home"
    if re.search(
        r"\b(who'?s|who\s+is|is\s+anyone|anyone|what\s+devices?)"
        r"\s+(home|in|here|present|detected|connected)\b", lower,
    ):
        data = await _api_get("/api/devices/present")
        if data is None:
            return "Couldn't reach Device Radar."
        if not data:
            return "No devices currently detected."
        lines = []
        for d in data:
            name = d.get("friendly_name") or d.get("advertised_name") or d["mac_address"]
            lines.append(f"\U0001f7e2 {name} \u2014 {_time_ago(d.get('last_seen'))}")
        return f"{len(data)} device(s) detected:\n" + "\n".join(lines)

    # Person-specific queries
    person = _extract_person(text)
    if person:
        conn = bt_db.get_connection(db_path)
        try:
            dev = _resolve_person(person, config, conn)
            if not dev:
                return f"I don't know who \"{person}\" is. Add them to person_aliases in config."

            dev_name = dev.get("friendly_name") or dev.get("advertised_name") or dev["mac_address"]
            group = bt_db.get_link_group(conn, dev["mac_address"])
            members = ([group["primary"]] + group["secondaries"]) if group["primary"] else [dev]
            any_home = any(m["state"] == "DETECTED" for m in members if m)

            # "when did X arrive/leave"
            if re.search(r"\bwhen\s+did\b", lower):
                etype = "departed" if re.search(r"\b(leave|depart|go)\b", lower) else "arrived"
                evts = bt_db.get_events(conn, mac=dev["mac_address"], event_type=etype, limit=1)
                if not evts:
                    return f"No {etype} events recorded for {dev_name}."
                return f"{dev_name} last {etype} {_time_ago(evts[0]['timestamp'])}."

            # "how long has X been home/away"
            if re.search(r"\bhow\s+long\b", lower):
                etype = "arrived" if any_home else "departed"
                evts = bt_db.get_events(conn, mac=dev["mac_address"], event_type=etype, limit=1)
                status = "home" if any_home else "away"
                if evts:
                    return f"{dev_name} has been {status} since {_time_ago(evts[0]['timestamp'])}."
                return f"{dev_name} is {status} (exact time unknown)."

            # "is X home" / "where is X"
            status = "home \U0001f7e2" if any_home else "away \U0001f534"
            return f"{dev_name} is {status} (last seen {_time_ago(dev.get('last_seen'))})."
        finally:
            conn.close()

    # Fallback: general status
    data = await _api_get("/api/stats")
    if data:
        return (
            f"Device Radar: {data.get('home_devices', 0)} detected, "
            f"{data.get('away_devices', 0)} lost, "
            f"{data.get('events_today', 0)} events today."
        )
    return "Couldn't retrieve device status."


# ---------------------------------------------------------------------------
# Telegram bot handlers — helpers
# ---------------------------------------------------------------------------

try:
    from telegram import Update
    from telegram.constants import ChatAction
    from telegram.ext import Application, CommandHandler, MessageHandler, filters

    _HAS_TELEGRAM_LIB = True
except ImportError:
    _HAS_TELEGRAM_LIB = False


def _is_authorized(chat_id: int) -> bool:
    """Only allow messages from the configured chat ID."""
    _, authorized = get_telegram_credentials()
    return not authorized or str(chat_id) == authorized


def _parse_args(args: list[str] | None) -> tuple[list[str], bool]:
    """Split command args into (remaining_args, watchlist_only)."""
    if not args:
        return [], False
    remaining: list[str] = []
    wl = False
    for a in args:
        if a.lower() in ("watchlist", "wl"):
            wl = True
        else:
            remaining.append(a)
    return remaining, wl


def _device_name(dev: dict) -> str:
    """Get display name for a device."""
    return (
        dev.get("friendly_name")
        or dev.get("advertised_name")
        or dev.get("mac_address", "Unknown")
    )


def _format_event_time(ts: float) -> str:
    """Format a timestamp as HH:MM for event listings."""
    from datetime import datetime

    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _get_db_path() -> Path:
    """Get the database path from config."""
    config = load_config()
    return BASE_DIR / config.get("db_path", "bt_radar.db")


# ---------------------------------------------------------------------------
# Telegram bot handlers — commands
# ---------------------------------------------------------------------------

async def _cmd_home(update, context) -> None:
    """Handle /home [watchlist] — detected devices."""
    if not _is_authorized(update.effective_chat.id):
        return
    _, wl = _parse_args(context.args)
    params: dict[str, str] = {"state": "DETECTED"}
    if wl:
        params["watchlisted"] = "1"
    data = await _api_get("/api/devices", params)
    if data is None:
        await update.message.reply_text("Couldn't reach Device Radar.")
        return
    if not data:
        msg = "No watchlisted devices detected." if wl else "No devices currently detected."
        await update.message.reply_text(msg)
        return
    lines = []
    for d in data:
        lines.append(f"\U0001f7e2 {_device_name(d)} \u2014 {_time_ago(d.get('last_seen'))}")
    header = f"{len(data)} device(s) detected"
    if wl:
        header += " (watchlisted)"
    await update.message.reply_text(f"{header}:\n" + "\n".join(lines))


async def _cmd_away(update, context) -> None:
    """Handle /away [watchlist] — devices not currently detected."""
    if not _is_authorized(update.effective_chat.id):
        return
    _, wl = _parse_args(context.args)
    params: dict[str, str] = {"state": "LOST"}
    if wl:
        params["watchlisted"] = "1"
    data = await _api_get("/api/devices", params)
    if data is None:
        await update.message.reply_text("Couldn't reach Device Radar.")
        return
    if not data:
        msg = "All watchlisted devices are home!" if wl else "No lost devices."
        await update.message.reply_text(msg)
        return
    show = data[:20]
    lines = []
    for d in show:
        lines.append(f"\U0001f534 {_device_name(d)} \u2014 {_time_ago(d.get('last_seen'))}")
    header = f"{len(data)} device(s) away"
    if wl:
        header += " (watchlisted)"
    text = f"{header}:\n" + "\n".join(lines)
    if len(data) > 20:
        text += f"\n\u2026and {len(data) - 20} more"
    await update.message.reply_text(text)


async def _cmd_devices(update, context) -> None:
    """Handle /devices [watchlist] — list all devices with status."""
    if not _is_authorized(update.effective_chat.id):
        return
    _, wl = _parse_args(context.args)
    params: dict[str, str] = {}
    if wl:
        params["watchlisted"] = "1"
    data = await _api_get("/api/devices", params)
    if not data:
        await update.message.reply_text("No devices found.")
        return
    lines = []
    for d in data[:30]:
        icon = "\U0001f7e2" if d.get("state") == "DETECTED" else "\U0001f534"
        wl_mark = " \u2b50" if d.get("is_watchlisted") else ""
        lines.append(f"{icon} {_device_name(d)}{wl_mark} \u2014 {_time_ago(d.get('last_seen'))}")
    text = "\n".join(lines)
    if len(data) > 30:
        text += f"\n\u2026and {len(data) - 30} more"
    await update.message.reply_text(text)


async def _cmd_lastseen(update, context) -> None:
    """Handle /lastseen <name> — when a device was last detected."""
    if not _is_authorized(update.effective_chat.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /lastseen <name>")
        return
    name = " ".join(context.args)
    config = load_config()
    db_path = _get_db_path()
    conn = bt_db.get_connection(db_path)
    dev = _resolve_person(name, config, conn)
    conn.close()
    if not dev:
        await update.message.reply_text(f"Unknown: \"{name}\"")
        return
    state = "detected \U0001f7e2" if dev["state"] == "DETECTED" else "lost \U0001f534"
    await update.message.reply_text(
        f"{_device_name(dev)} \u2014 {state}, last seen {_time_ago(dev.get('last_seen'))}",
    )


async def _cmd_status(update, context) -> None:
    """Handle /status — system health overview."""
    if not _is_authorized(update.effective_chat.id):
        return
    data = await _api_get("/api/stats")
    if data is None:
        await update.message.reply_text("Couldn't reach Device Radar.")
        return

    import subprocess as sp

    def _svc_status(name: str) -> str:
        try:
            r = sp.run(
                ["systemctl", "is-active", name],
                capture_output=True, text=True, timeout=5,
            )
            return r.stdout.strip()
        except Exception:
            return "unknown"

    scanner = _svc_status("bt-scanner")
    bot = _svc_status("bt-telegram")
    web = _svc_status("bt-web")
    s_icon = "\U0001f7e2" if scanner == "active" else "\U0001f534"
    b_icon = "\U0001f7e2" if bot == "active" else "\U0001f534"
    w_icon = "\U0001f7e2" if web == "active" else "\U0001f534"

    lines = [
        "\U0001f4ca Device Radar Status",
        "",
        f"{data.get('home_devices', 0)} detected \u2022 "
        f"{data.get('away_devices', 0)} lost \u2022 "
        f"{data.get('watchlisted_devices', 0)} watchlisted",
        f"{data.get('events_today', 0)} events today",
        "",
        f"{s_icon} Scanner: {scanner}",
        f"{w_icon} Web: {web}",
        f"{b_icon} Bot: {bot}",
    ]
    await update.message.reply_text("\n".join(lines))


async def _cmd_history(update, context) -> None:
    """Handle /history [name] [watchlist] — recent events."""
    if not _is_authorized(update.effective_chat.id):
        return
    args, wl = _parse_args(context.args)
    config = load_config()
    db_path = _get_db_path()
    conn = bt_db.get_connection(db_path)

    try:
        mac_filter = None
        person_name = None
        if args:
            person_name = " ".join(args)
            dev = _resolve_person(person_name, config, conn)
            if not dev:
                await update.message.reply_text(f"Unknown: \"{person_name}\"")
                return
            mac_filter = dev["mac_address"]

        events = bt_db.get_events(conn, mac=mac_filter, limit=50)

        if wl:
            wl_macs = {
                d["mac_address"]
                for d in bt_db.get_all_devices(
                    conn, watchlisted_only=True, include_hidden=True,
                )
            }
            events = [e for e in events if e["mac_address"] in wl_macs]

        events = events[:10]
        if not events:
            await update.message.reply_text("No events found.")
            return

        lines = []
        for e in events:
            icon = "\U0001f7e2" if e["event_type"] == "arrived" else "\U0001f534"
            name = (
                e.get("friendly_name")
                or e.get("d_adv_name")
                or e.get("device_name")
                or e["mac_address"]
            )
            t = _format_event_time(e["timestamp"])
            ago = _time_ago(e["timestamp"])
            lines.append(f"{icon} {name} {e['event_type']} at {t} ({ago})")

        header = "Last 10 events"
        if person_name:
            header = f"Last events for {_device_name(dev)}"
        if wl:
            header += " (watchlisted)"
        await update.message.reply_text(f"{header}:\n" + "\n".join(lines))
    finally:
        conn.close()


async def _cmd_today(update, context) -> None:
    """Handle /today [watchlist] — today's arrivals and departures."""
    if not _is_authorized(update.effective_chat.id):
        return
    _, wl = _parse_args(context.args)
    db_path = _get_db_path()
    conn = bt_db.get_connection(db_path)

    try:
        from datetime import datetime

        midnight = (
            datetime.now()
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        rows = conn.execute(
            "SELECT e.*, d.friendly_name, d.is_watchlisted "
            "FROM events e "
            "LEFT JOIN devices d ON e.mac_address = d.mac_address "
            "WHERE e.timestamp >= ? ORDER BY e.timestamp DESC",
            (midnight,),
        ).fetchall()
        events = [dict(r) for r in rows]

        if wl:
            events = [e for e in events if e.get("is_watchlisted")]

        if not events:
            msg = "No events today"
            if wl:
                msg += " (watchlisted)"
            await update.message.reply_text(msg + ".")
            return

        lines = []
        for e in events[:20]:
            icon = "\U0001f7e2" if e["event_type"] == "arrived" else "\U0001f534"
            name = (
                e.get("friendly_name")
                or e.get("device_name")
                or e["mac_address"]
            )
            t = _format_event_time(e["timestamp"])
            lines.append(f"{icon} {t} \u2014 {name} {e['event_type']}")

        header = f"{len(events)} event(s) today"
        if wl:
            header += " (watchlisted)"
        text = f"{header}:\n" + "\n".join(lines)
        if len(events) > 20:
            text += f"\n\u2026and {len(events) - 20} more"
        await update.message.reply_text(text)
    finally:
        conn.close()


async def _cmd_notify_toggle(update, context) -> None:
    """Handle /notify on|off <name> — toggle notifications for a device."""
    if not _is_authorized(update.effective_chat.id):
        return
    args, _ = _parse_args(context.args)
    if len(args) < 2 or args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Usage: /notify on|off <name>")
        return

    action = args[0].lower()
    name = " ".join(args[1:])
    config = load_config()
    db_path = _get_db_path()
    conn = bt_db.get_connection(db_path)

    try:
        dev = _resolve_person(name, config, conn)
        if not dev:
            await update.message.reply_text(f"Unknown: \"{name}\"")
            return
        enabled = action == "on"
        bt_db.update_device(conn, dev["mac_address"], is_notify=enabled)
        status = "enabled \U0001f514" if enabled else "disabled \U0001f515"
        await update.message.reply_text(
            f"Notifications {status} for {_device_name(dev)}",
        )
    finally:
        conn.close()


async def _cmd_find(update, context) -> None:
    """Handle /find <name> — detailed device info."""
    if not _is_authorized(update.effective_chat.id):
        return
    args, _ = _parse_args(context.args)
    if not args:
        await update.message.reply_text("Usage: /find <name>")
        return

    name = " ".join(args)
    config = load_config()
    db_path = _get_db_path()
    conn = bt_db.get_connection(db_path)

    try:
        dev = _resolve_person(name, config, conn)
        if not dev:
            await update.message.reply_text(f"Unknown: \"{name}\"")
            return

        state_icon = "\U0001f7e2" if dev["state"] == "DETECTED" else "\U0001f534"
        wl_mark = " \u2b50" if dev.get("is_watchlisted") else ""
        notify_icon = "\U0001f514" if dev.get("is_notify") else "\U0001f515"

        lines = [
            f"{state_icon} {_device_name(dev)}{wl_mark}",
            "",
            f"MAC: {dev['mac_address']}",
            f"Type: {dev.get('device_type', 'Unknown')}",
            f"Scan: {dev.get('scan_type', 'Unknown')}",
        ]
        if dev.get("manufacturer"):
            lines.append(f"Manufacturer: {dev['manufacturer']}")
        if dev.get("ip_address"):
            lines.append(f"IP: {dev['ip_address']}")
        if dev.get("last_rssi"):
            lines.append(f"RSSI: {dev['last_rssi']} dBm")
        lines.extend([
            f"First seen: {_time_ago(dev.get('first_seen'))}",
            f"Last seen: {_time_ago(dev.get('last_seen'))}",
            f"Notifications: {notify_icon}",
            f"Watchlisted: {'yes' if dev.get('is_watchlisted') else 'no'}",
        ])

        group = bt_db.get_link_group(conn, dev["mac_address"])
        if group["secondaries"]:
            linked = ", ".join(_device_name(s) for s in group["secondaries"])
            lines.append(f"Linked: {linked}")
        elif (
            group["primary"]
            and group["primary"]["mac_address"] != dev["mac_address"]
        ):
            lines.append(f"Linked to: {_device_name(group['primary'])}")

        await update.message.reply_text("\n".join(lines))
    finally:
        conn.close()


async def _cmd_watchlist(update, context) -> None:
    """Handle /watchlist — show all watchlisted devices grouped by state."""
    if not _is_authorized(update.effective_chat.id):
        return
    data = await _api_get("/api/devices", {"watchlisted": "1"})
    if not data:
        await update.message.reply_text("No watchlisted devices.")
        return

    detected = [d for d in data if d.get("state") == "DETECTED"]
    lost = [d for d in data if d.get("state") != "DETECTED"]

    lines = [f"\u2b50 Watchlist ({len(data)} devices):"]
    if detected:
        lines.append("")
        lines.append("Detected:")
        for d in detected:
            n = "\U0001f514" if d.get("is_notify") else "\U0001f515"
            lines.append(
                f"  \U0001f7e2 {_device_name(d)} {n}"
                f" \u2014 {_time_ago(d.get('last_seen'))}",
            )
    if lost:
        lines.append("")
        lines.append("Lost:")
        for d in lost:
            n = "\U0001f514" if d.get("is_notify") else "\U0001f515"
            lines.append(
                f"  \U0001f534 {_device_name(d)} {n}"
                f" \u2014 {_time_ago(d.get('last_seen'))}",
            )
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Read-aloud state (in-memory, resets on restart — default off)
# ---------------------------------------------------------------------------

_readaloud_enabled: bool = False
_readaloud_device: str | None = None
_readaloud_voice: str | None = None

_VALID_VOICES = {"brian", "amy", "emma", "matthew", "joanna", "kendra"}


# ---------------------------------------------------------------------------
# Telegram bot handlers — Alexa commands
# ---------------------------------------------------------------------------

async def _cmd_say(update, context) -> None:
    """Handle /say [device] <message> — speak on an Echo device."""
    if not _is_authorized(update.effective_chat.id):
        return

    config = load_config()
    if not config.get("alexa_enabled"):
        await update.message.reply_text("Alexa integration is not enabled.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /say [device] <message>\n"
            "Examples:\n"
            "  /say Hello everyone\n"
            "  /say kitchen Dinner is ready\n"
            "  /say all Time for bed\n"
            "\nUse /echoes to see available devices."
        )
        return

    import bt_alexa

    first_word = context.args[0].lower()
    alias_keys = {k.lower() for k in config.get("alexa_devices", {})}

    if first_word == "all":
        message = " ".join(context.args[1:])
        if not message:
            await update.message.reply_text("Usage: /say all <message>")
            return
        target_display = "all devices"
        success = await bt_alexa.speak(message, config, device="ALL")
    elif first_word in alias_keys:
        device_name = bt_alexa.resolve_device_alias(first_word, config)
        message = " ".join(context.args[1:])
        if not message:
            await update.message.reply_text(f"Usage: /say {first_word} <message>")
            return
        target_display = f"{first_word} ({device_name})"
        success = await bt_alexa.speak(message, config, device=device_name)
    else:
        message = " ".join(context.args)
        default_device = config.get("alexa_device_name", "Laura's Echo")
        target_display = default_device
        success = await bt_alexa.speak(message, config)

    if success:
        await update.message.reply_text(f"Spoke on {target_display}: \"{message}\"")
    else:
        await update.message.reply_text(f"Failed to speak on {target_display}. Check logs.")


async def _cmd_echoes(update, context) -> None:
    """Handle /echoes — list available Echo devices and aliases."""
    if not _is_authorized(update.effective_chat.id):
        return

    config = load_config()
    if not config.get("alexa_enabled"):
        await update.message.reply_text("Alexa integration is not enabled.")
        return

    alexa_devices = config.get("alexa_devices", {})
    default_device = config.get("alexa_device_name", "Laura's Echo")

    lines = []
    if alexa_devices:
        lines.append("Available devices:")
        for alias, device in alexa_devices.items():
            marker = " (default)" if device == default_device else ""
            lines.append(f"  {alias} \u2192 {device}{marker}")
    else:
        lines.append("No device aliases configured.")

    lines.append(f"\nDefault: {default_device}")
    lines.append("\nUsage: /say <device> <message>")
    lines.append("Use /say all <message> to speak on all devices.")

    await update.message.reply_text("\n".join(lines))


async def _cmd_readaloud(update, context) -> None:
    """Handle /readaloud — toggle Alexa read-aloud for chat responses."""
    if not _is_authorized(update.effective_chat.id):
        return

    global _readaloud_enabled, _readaloud_device, _readaloud_voice

    args, _ = _parse_args(context.args)

    # No args — show status
    if not args:
        if not _readaloud_enabled:
            await update.message.reply_text(
                "\U0001f508 Read-aloud is off.\n\n"
                "Usage:\n"
                "  /readaloud on [device]\n"
                "  /readaloud off\n"
                "  /readaloud voice <name>\n"
                "  /readaloud voice off",
            )
        else:
            dev = _readaloud_device or "default"
            voice = _readaloud_voice or "default"
            await update.message.reply_text(
                f"\U0001f50a Read-aloud is on\n"
                f"Device: {dev}\n"
                f"Voice: {voice}",
            )
        return

    action = args[0].lower()

    # /readaloud on [device]
    if action == "on":
        _readaloud_enabled = True
        if len(args) > 1:
            _readaloud_device = args[1]
            await update.message.reply_text(
                f"\U0001f50a Read-aloud enabled on {args[1]}",
            )
        else:
            _readaloud_device = None
            await update.message.reply_text(
                "\U0001f50a Read-aloud enabled (default device)",
            )
        return

    # /readaloud off
    if action == "off":
        _readaloud_enabled = False
        await update.message.reply_text("\U0001f508 Read-aloud disabled")
        return

    # /readaloud voice <name|off>
    if action == "voice":
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /readaloud voice <name|off>\n"
                "Available: Brian, Amy, Emma, Matthew, Joanna, Kendra",
            )
            return
        voice_arg = args[1].lower()
        if voice_arg == "off":
            _readaloud_voice = None
            await update.message.reply_text("\U0001f508 Voice reset to default")
        elif voice_arg in _VALID_VOICES:
            _readaloud_voice = args[1].capitalize()
            await update.message.reply_text(
                f"\U0001f50a Voice set to {_readaloud_voice}",
            )
        else:
            await update.message.reply_text(
                f"Unknown voice \"{args[1]}\".\n"
                "Available: Brian, Amy, Emma, Matthew, Joanna, Kendra",
            )
        return

    await update.message.reply_text(
        "Usage: /readaloud on|off  or  /readaloud voice <name>",
    )


# ---------------------------------------------------------------------------
# Telegram bot handlers — Kitkat
# ---------------------------------------------------------------------------

async def _cmd_kitkat(update, context) -> None:
    """Handle /kitkat commands."""
    if not _is_authorized(update.effective_chat.id):
        return
    config = load_config()
    if not _HAS_KITKAT or not config.get("kitkat_enabled"):
        await update.message.reply_text("Kitkat is not enabled.")
        return
    chat_id = update.effective_chat.id
    args = context.args or []
    response = await bt_kitkat_telegram.handle_command(chat_id, args, config)
    await update.message.reply_text(response)


# ---------------------------------------------------------------------------
# Telegram bot handlers — message router
# ---------------------------------------------------------------------------

async def _handle_message(update, context) -> None:
    """Route incoming messages to presence handler or Ollama."""
    if not update.message or not update.message.text:
        return
    if not _is_authorized(update.effective_chat.id):
        return

    text = update.message.text
    config = load_config()
    db_path = _get_db_path()
    chat_id = str(update.effective_chat.id)

    # Presence query — answer directly
    if is_presence_query(text):
        await update.message.reply_text(
            await answer_presence(text, config, db_path),
        )
        return

    # Kitkat mode — route to personal memory agent
    if (
        _HAS_KITKAT
        and config.get("kitkat_enabled")
        and bt_kitkat_telegram.is_kitkat_mode(update.effective_chat.id)
    ):
        await update.effective_chat.send_action(ChatAction.TYPING)
        response = await bt_kitkat_telegram.handle_message(
            update.effective_chat.id, text, config,
        )
        await update.message.reply_text(response)
        return

    # General chat — forward to Ollama
    await update.effective_chat.send_action(ChatAction.TYPING)

    # Save user message and build history
    conn = bt_db.get_connection(db_path)
    bt_db.save_chat_message(conn, chat_id, "user", text)
    history = bt_db.get_chat_history(
        conn, chat_id, config.get("conversation_history_length", 10),
    )
    conn.close()

    system_prompt = config.get(
        "system_prompt",
        "You are a helpful assistant running locally on a Raspberry Pi at home. "
        "Keep responses concise and conversational.",
    )
    system_prompt += " Do not use emoji in your responses."
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend({"role": m["role"], "content": m["content"]} for m in history)

    response, searched = await bt_search.chat_with_search_async(messages, config)
    if response is None:
        response = "Sorry, I'm thinking too hard about that one. Try again in a moment."

    conn = bt_db.get_connection(db_path)
    bt_db.save_chat_message(conn, chat_id, "assistant", response)
    conn.close()

    prefix = "[searched the web]\n\n" if searched else ""
    await update.message.reply_text(f"{prefix}{response}")

    # Read aloud on Alexa if enabled
    if _readaloud_enabled:
        try:
            import bt_alexa

            device = _readaloud_device
            if device:
                device = bt_alexa.resolve_device_alias(device, config) or device
            await bt_alexa.speak(
                response, config, device=device, voice=_readaloud_voice,
            )
        except Exception:
            logger.debug("Read-aloud speak failed", exc_info=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the Telegram bot."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if not _HAS_TELEGRAM_LIB:
        logger.error(
            "python-telegram-bot not installed. "
            "Run: pip install python-telegram-bot --break-system-packages"
        )
        return

    config = load_config()
    if not config.get("telegram_bot_enabled", False):
        logger.info("Telegram bot disabled (telegram_bot_enabled = false)")
        return

    token, chat_id = get_telegram_credentials(config)
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        return
    if not chat_id:
        logger.warning("TELEGRAM_CHAT_ID not set — proactive notifications disabled")

    # Initialize DB and clean up old chat history
    db_path = BASE_DIR / config.get("db_path", "bt_radar.db")
    bt_db.init_db(db_path)
    conn = bt_db.get_connection(db_path)
    cleaned = bt_db.cleanup_chat_history(conn)
    if cleaned:
        logger.info("Cleaned up %d old chat history entries", cleaned)
    conn.close()

    logger.info("Starting Device Radar Telegram bot")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("home", _cmd_home))
    app.add_handler(CommandHandler("away", _cmd_away))
    app.add_handler(CommandHandler("devices", _cmd_devices))
    app.add_handler(CommandHandler("watchlist", _cmd_watchlist))
    app.add_handler(CommandHandler("lastseen", _cmd_lastseen))
    app.add_handler(CommandHandler("status", _cmd_status))
    app.add_handler(CommandHandler("history", _cmd_history))
    app.add_handler(CommandHandler("today", _cmd_today))
    app.add_handler(CommandHandler("notify", _cmd_notify_toggle))
    app.add_handler(CommandHandler("find", _cmd_find))
    app.add_handler(CommandHandler("say", _cmd_say))
    app.add_handler(CommandHandler("echoes", _cmd_echoes))
    app.add_handler(CommandHandler("readaloud", _cmd_readaloud))
    if _HAS_KITKAT and config.get("kitkat_enabled"):
        app.add_handler(CommandHandler("kitkat", _cmd_kitkat))
        logger.info("Kitkat Telegram integration enabled")
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))

    # Register bot commands with Telegram so they appear in the / menu
    async def _post_init(application) -> None:
        from telegram import BotCommand

        await application.bot.set_my_commands([
            BotCommand("home", "Who is home right now"),
            BotCommand("away", "Who is away"),
            BotCommand("devices", "List all devices with status"),
            BotCommand("watchlist", "Show watchlisted devices"),
            BotCommand("lastseen", "When a device was last seen"),
            BotCommand("status", "System health overview"),
            BotCommand("history", "Recent arrival/departure events"),
            BotCommand("today", "Today's events summary"),
            BotCommand("notify", "Toggle notifications (on/off name)"),
            BotCommand("find", "Detailed info about a device"),
            BotCommand("say", "Speak a message on an Echo device"),
            BotCommand("echoes", "List available Echo devices"),
            BotCommand("readaloud", "Toggle Alexa read-aloud for chat"),
            BotCommand("kitkat", "Personal memory agent (on/off/memories/stats)"),
        ])

    app.post_init = _post_init
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
