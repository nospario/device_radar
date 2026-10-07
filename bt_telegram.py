#!/usr/bin/env python3
"""Device Radar Telegram Bot — interactive presence queries via Ollama
and proactive arrival/departure notifications."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

import bt_backup
import bt_db
import bt_health
import bt_newdevice
import bt_people
import bt_presence
import bt_search
import bt_tasks

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

async def send_message(
    text: str,
    parse_mode: str = "HTML",
    reply_markup: dict | None = None,
) -> bool:
    """Send a free-form message to the configured Telegram chat.

    ``reply_markup`` should be a JSON-serialisable dict (e.g.
    ``{"inline_keyboard": [[{"text": "X", "callback_data": "y"}]]}``);
    Telegram expects it as a JSON-encoded string inside the request body.
    Returns True if the API accepted it. Safe to call without the full bot
    running — only requires httpx.
    """
    token, chat_id = get_telegram_credentials()
    if not token or not chat_id:
        return False

    payload: dict[str, Any] = {
        "chat_id": chat_id, "text": text, "parse_mode": parse_mode,
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload, timeout=10,
            )
            return resp.status_code == 200
    except Exception as e:
        logger.error("Telegram send_message failed: %s", e)
        return False


async def send_notification(device_name: str, event: str) -> None:
    """Send an arrival/departure notification to Telegram.

    Can be called from bt_scanner.py without the full bot running.
    """
    escaped = html.escape(device_name)
    if event == "arrived":
        text = f"\U0001f4e1 <b>{escaped}</b> detected"
    else:
        text = f"\U0001f44b <b>{escaped}</b> departed"
    await send_message(text)


# ---------------------------------------------------------------------------
# Intent routing
# ---------------------------------------------------------------------------

_PRESENCE_PATTERNS = [
    re.compile(r"\b(who'?s|who\s+is|is\s+anyone|anyone)\s+(home|in|here|present|around)\b", re.I),
    re.compile(r"\bis\s+\w+\s+(home|in|here|present|around)\b", re.I),
    re.compile(r"\bwhere\s+is\s+\w+\b", re.I),
    re.compile(r"\bwhen\s+did\s+\w+\s+(arrive|leave|depart|get\s+home|come\s+home|go)\b", re.I),
    re.compile(r"\bwhen\s+(will|is|does|would|should)\s+\w+\s+(be\s+)?(usually\s+|normally\s+|typically\s+)?"
               r"(home|back|arrive|get\s+home|come\s+home|leave|go\s+out|head\s+out)\b", re.I),
    re.compile(r"\bhow\s+long\s+has\s+\w+\s+been\s+(home|away|out|gone|here)\b", re.I),
    re.compile(r"\bwhat\s+devices?\s+(are|is)\s+(home|present|connected|detected)\b", re.I),
    re.compile(r"\blast\s+seen\b", re.I),
    re.compile(r"\bdevice\s+status\b", re.I),
]


def is_presence_query(text: str) -> bool:
    """Check if text matches a presence query pattern."""
    return any(p.search(text) for p in _PRESENCE_PATTERNS)


_PEOPLE_ICON = {"home": "\U0001f7e2", "away": "\U0001f534", "no_phone": "\u26aa"}


def _people_detail(p: dict[str, Any]) -> str:
    if p["state"] == "home":
        return f"arrived {_time_ago(p['since'])}" if p["since"] else "home"
    if p["state"] == "away":
        if p["since"]:
            return f"left {_time_ago(p['since'])}"
        return f"last seen {_time_ago(p['last_seen'])}" if p["last_seen"] else "away"
    return "no phone tracked"


def format_people_summary(people: list[dict[str, Any]], connected_devices: int | None = None) -> str:
    """Who is home, one line per person (decided by phones, see bt_people)."""
    # Plain text (these replies are not sent with a parse mode). Person keys are
    # already limited to letters, digits, spaces and hyphens by normalise_person.
    lines = [f"{_PEOPLE_ICON[p['state']]} {p['display']} \u2014 {_people_detail(p)}" for p in people]
    if connected_devices is not None:
        lines.append(f"\n{connected_devices} device(s) connected in total. /devices lists them all.")
    return "\n".join(lines)


def _people_summary_text(db_path: Path, config: dict[str, Any]) -> str | None:
    """The who's-home summary, or None if no people are set up yet (callers then list devices)."""
    conn = bt_db.get_connection(db_path)
    try:
        people = bt_people.people_status(conn, config)
        if not people:
            return None
        connected = conn.execute(
            "SELECT COUNT(*) FROM devices WHERE state = 'DETECTED' AND linked_to IS NULL AND is_hidden = 0",
        ).fetchone()[0]
    finally:
        conn.close()
    return format_people_summary(people, connected)


def no_phone_message(entry: dict[str, Any]) -> str:
    """Reply when someone is known but has no phone tracked."""
    name = entry["display"]
    msg = f"No phone is tracked for {name}, so I can't tell whether they're home."
    if entry["others"]:
        shown = ", ".join(entry["others"][:3])
        msg += f" ({shown} {'is' if len(entry['others']) == 1 else 'are'} known, but only phones count.)"
    return msg + " Send /unnamed to name a new phone, or set a device's Role to Phone in the dashboard."


def _extract_person(text: str) -> str | None:
    """Extract a person name from a presence query."""
    patterns = [
        re.compile(r"\bis\s+(\w+)\s+(home|in|here|present|around)\b", re.I),
        re.compile(r"\bwhere\s+is\s+(\w+)\b", re.I),
        re.compile(r"\bwhen\s+(?:will|is|does|would|should)\s+(\w+)\b", re.I),
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
    # The person's phone (people and roles, see bt_people)
    phone = bt_people.best_phone(conn, config, name)
    if phone:
        return phone
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


def predictive_answer(conn, config: dict[str, Any], person: str, question: str = "",
                      now: float | None = None) -> str:
    """Answer "when will X be home / when does X usually leave?" from the presence statistics."""
    now = time.time() if now is None else now
    data = bt_presence.analyse(conn, config, now).get(person)
    if data is None:
        return f"No phone is tracked for {person.title()}, so I can't say."
    lower = question.lower()
    wants_leave = bool(re.search(r"\b(leave|go\s+out|head\s+out)\b", lower))
    asks_usual = bool(re.search(r"\b(usually|typically|normally|does)\b", lower))
    pred = bt_presence.eta_for_people(conn, config, now)[person]
    name = data.display
    if wants_leave:
        typical = bt_presence.describe_typical(name, data.typical, "leave")
        if data.state != "home":
            live = f"{name} is out at the moment."
        else:
            live = bt_presence.describe_prediction(name, pred, data.typical, now, None)
        parts = [typical, live] if asks_usual and typical else [live, typical]
        return " ".join(p for p in parts if p)
    typical = bt_presence.describe_typical(name, data.typical, "return")
    if data.state == "home":
        since = f" (arrived {_time_ago(data.sessions[-1].start)})" if data.sessions else ""
        live = f"{name} is already home{since}."
    else:
        live = bt_presence.describe_prediction(name, pred, data.typical, now, None)
    parts = [typical, live] if asks_usual and typical else [live, typical]
    return " ".join(p for p in parts if p)


async def answer_presence(
    text: str, config: dict[str, Any], db_path: Path, now: float | None = None,
) -> str:
    """Answer a presence query with a factual response."""
    lower = text.lower()

    # "who's home" / "is anyone home" / "what devices are home"
    if re.search(
        r"\b(who'?s|who\s+is|is\s+anyone|anyone|what\s+devices?)"
        r"\s+(home|in|here|present|detected|connected)\b", lower,
    ):
        summary = _people_summary_text(db_path, config)
        if summary:
            return summary
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
            entry = bt_people.person_entry(conn, config, person)
            if entry and entry["state"] == "no_phone":
                return no_phone_message(entry)
            if entry and re.search(r"\bwhen\s+(?:will|is|does|would|should)\b", lower):
                return predictive_answer(conn, config, entry["person"], text, now)
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
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.constants import ChatAction
    from telegram.ext import (
        Application, CallbackQueryHandler, CommandHandler, MessageHandler,
        filters,
    )

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
    if not wl:
        summary = _people_summary_text(_get_db_path(), load_config())
        if summary:
            await update.message.reply_text(summary)
            return
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


def _health_lines(db_path: Path) -> list[str]:
    """Health summary for /status: the headline, then any problems (see bt_health)."""
    conn = bt_db.get_connection(db_path)
    try:
        health = bt_health.load_results(conn)
    finally:
        conn.close()
    icon = {0: "\U0001f7e2", 1: "\U0001f7e1", 2: "\U0001f534"}
    head = "\u26a0\ufe0f" if health["stale"] and health["checked_at"] else icon.get(health["worst"], "\U0001f7e2")
    lines = ["", f"{head} Health: {health['summary']}"]
    for chk in [c for c in health["checks"] if c["status"] != "ok"][:8]:
        lines.append(f"  {icon[1 if chk['status'] == 'warn' else 2]} {chk['label']}: {chk['message']}")
    return lines


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
    lines += _health_lines(_get_db_path())
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


# ---------------------------------------------------------------------------
# New-device naming (buttons on the "New device on the WiFi" alert, and /unnamed)
# ---------------------------------------------------------------------------

_NAMING_TIMEOUT = 900  # seconds the bot waits for a name after a role button is tapped


def _markup_from_dict(kb: dict) -> "InlineKeyboardMarkup":
    """Turn the plain-dict keyboard from bt_newdevice into python-telegram-bot objects."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(b["text"], callback_data=b["callback_data"]) for b in row]
        for row in kb["inline_keyboard"]
    ])


def _pending_names(context) -> dict[str, dict[str, Any]]:
    return context.bot_data.setdefault("nd_pending", {})


async def _cmd_eta(update, context) -> None:
    """Handle /eta [name]: when people who are out are expected home (see bt_presence)."""
    if not _is_authorized(update.effective_chat.id):
        return
    conn = bt_db.get_connection(_get_db_path())
    try:
        config = load_config()
        if context.args:
            entry = bt_people.person_entry(conn, config, " ".join(context.args))
            if entry is None:
                await update.message.reply_text(f"I don't know who \"{' '.join(context.args)}\" is.")
            elif entry["state"] == "no_phone":
                await update.message.reply_text(no_phone_message(entry))
            else:
                await update.message.reply_text(predictive_answer(conn, config, entry["person"], "when will they be home"))
            return
        away = [p for p in bt_people.people_status(conn, config) if p["state"] == "away"]
        if not away:
            await update.message.reply_text("Everyone with a tracked phone is home.")
            return
        lines = [predictive_answer(conn, config, p["person"], "when will they be home") for p in away]
    finally:
        conn.close()
    await update.message.reply_text("\n\n".join(lines))


async def _cmd_unnamed(update, context) -> None:
    """Handle /unnamed: list connected WiFi devices that have no name, each with naming buttons."""
    if not _is_authorized(update.effective_chat.id):
        return
    conn = bt_db.get_connection(_get_db_path())
    try:
        limit = 8
        devices = bt_newdevice.list_unnamed_connected(conn, limit=limit + 1)
    finally:
        conn.close()
    if not devices:
        await update.message.reply_text("Every connected WiFi device has a name.")
        return
    shown = devices[:limit]
    await update.message.reply_text(
        f"{len(shown)}{'+' if len(devices) > limit else ''} connected device(s) without a name:",
    )
    for dev in shown:
        await update.message.reply_text(
            bt_newdevice.alert_text(dev), parse_mode="HTML",
            reply_markup=_markup_from_dict(bt_newdevice.keyboard(dev)),
        )


async def _on_newdevice_callback(update, context) -> None:
    """Handle a role button on a new-device message."""
    query = update.callback_query
    if not _is_authorized(query.from_user.id if query.from_user else 0) and not _is_authorized(
        query.message.chat.id if query.message else 0,
    ):
        await query.answer()
        return
    await query.answer()

    parsed = bt_newdevice.parse_callback(query.data)
    if not parsed or not query.message:
        return
    action, mac = parsed
    chat_id = str(query.message.chat.id)

    conn = bt_db.get_connection(_get_db_path())
    try:
        dev = bt_db.get_device(conn, mac)
        if dev is None:
            await query.edit_message_text("That device is no longer in the list.")
            return
        existing = (dev.get("friendly_name") or "").strip()
        if existing:
            await query.edit_message_text(
                f"Already named <b>{html.escape(existing)}</b>.", parse_mode="HTML",
            )
            return
        what = bt_newdevice.describe(dev)
        if action == bt_newdevice.IGNORE:
            bt_newdevice.apply_role(conn, mac, action)
            await query.edit_message_text(
                f"\U0001f648 Ignored: {what}. It will be hidden and cleaned up later.",
                parse_mode="HTML",
            )
            return
    finally:
        conn.close()

    _pending_names(context)[chat_id] = {
        "mac": mac, "action": action, "message_id": query.message.message_id, "at": time.time(),
    }
    await query.edit_message_text(
        f"{bt_newdevice.ROLES[action]['label']}: {what}\n\n"
        "Reply with a name for it (for example <i>Mathilde's iPhone</i>), or send <b>cancel</b>.",
        parse_mode="HTML",
    )


async def _maybe_handle_naming(update, context) -> bool:
    """If a role button is waiting for a name, treat this message as the name.

    Returns True if the message was consumed.
    """
    chat_id = str(update.effective_chat.id)
    pending = _pending_names(context)
    entry = pending.get(chat_id)
    if not entry:
        return False
    if time.time() - entry["at"] > _NAMING_TIMEOUT:
        pending.pop(chat_id, None)
        return False
    text = (update.message.text or "").strip()
    if text.lower() in ("cancel", "/cancel"):
        pending.pop(chat_id, None)
        await update.message.reply_text("Cancelled. Send /unnamed to see it again.")
        return True
    if len(text) > 60:
        await update.message.reply_text("That name is too long (60 characters max). Try a shorter one, or send cancel.")
        return True
    pending.pop(chat_id, None)
    conn = bt_db.get_connection(_get_db_path())
    try:
        result = bt_newdevice.apply_role(conn, entry["mac"], entry["action"], text)
    finally:
        conn.close()
    if result["ok"]:
        role = bt_newdevice.ROLES[entry["action"]]
        extra = " Watched, with notifications on." if role["watch"] else ""
        await update.message.reply_text(
            f"\u2705 Saved as <b>{html.escape(text)}</b> ({html.escape(role['device_type'])}).{extra}",
            parse_mode="HTML",
        )
    elif result["reason"] == "already_named":
        await update.message.reply_text("That device already has a name, so I left it alone.")
    else:
        await update.message.reply_text("That device is no longer in the list.")
    return True


def _habits_daily_notes_dir() -> str:
    return load_config().get(
        "obsidian_daily_notes_dir", bt_tasks.DEFAULT_DAILY_NOTES_DIR,
    )


def _build_habit_keyboard(
    habits: list[tuple[str, bool]],
) -> "InlineKeyboardMarkup | None":
    """Build a vertical toggle keyboard — one button per habit, with a ✓
    prefix on completed ones. Tapping any button toggles state."""
    if not habits:
        return None
    rows = []
    for desc, done in habits:
        label = f"✓ {desc}" if done else desc
        rows.append([InlineKeyboardButton(
            label, callback_data=f"habit:{bt_tasks.habit_hash(desc)}",
        )])
    return InlineKeyboardMarkup(rows)


def _render_habit_message(habits: list[tuple[str, bool]]) -> str:
    if not habits:
        return "<b>No habits in today's Daily Note.</b>"
    total = len(habits)
    done_count = sum(1 for _, done in habits if done)
    if done_count == total:
        return (
            f"<b>All {total} habits complete for today.</b>\n"
            f"Tap any to un-tick."
        )
    return (
        f"<b>Habits: {done_count}/{total} complete</b>\n"
        f"Tap to toggle."
    )


async def _cmd_habits(update, context) -> None:
    """Handle /habits — list all of today's #habit items with tap-to-toggle buttons."""
    if not _is_authorized(update.effective_chat.id):
        return
    habits = bt_tasks.get_all_habits(_habits_daily_notes_dir())
    await update.message.reply_text(
        _render_habit_message(habits),
        parse_mode="HTML",
        reply_markup=_build_habit_keyboard(habits),
    )


async def _on_habit_callback(update, context) -> None:
    """Handle a habit-button tap: toggle state in the Daily Note and
    refresh the message in place."""
    query = update.callback_query
    if not _is_authorized(query.from_user.id if query.from_user else 0) and not _is_authorized(
        query.message.chat.id if query.message else 0,
    ):
        await query.answer()
        return

    await query.answer()  # dismiss the loading spinner

    data = query.data or ""
    if not data.startswith("habit:"):
        return
    target_hash = data.split(":", 1)[1]

    daily_notes_dir = _habits_daily_notes_dir()
    current = bt_tasks.get_all_habits(daily_notes_dir)
    matched = next(
        ((desc, done) for desc, done in current
         if bt_tasks.habit_hash(desc) == target_hash),
        None,
    )

    if matched is not None:
        desc, currently_done = matched
        bt_tasks.set_habit_done_state(
            daily_notes_dir, description=desc, done=not currently_done,
        )

    refreshed = bt_tasks.get_all_habits(daily_notes_dir)
    try:
        await query.edit_message_text(
            _render_habit_message(refreshed),
            parse_mode="HTML",
            reply_markup=_build_habit_keyboard(refreshed),
        )
    except Exception as e:
        # Most common: "message is not modified" when nothing changed
        logger.debug("Habit message edit skipped: %s", e)


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
# Telegram bot handlers — message router
# ---------------------------------------------------------------------------

async def _handle_message(update, context) -> None:
    """Route incoming messages to presence handler or Ollama."""
    if not update.message or not update.message.text:
        return
    if not _is_authorized(update.effective_chat.id):
        return

    # A role button is waiting for a name: this message is the name
    if await _maybe_handle_naming(update, context):
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
    app.add_handler(CommandHandler("habits", _cmd_habits))
    app.add_handler(CommandHandler("unnamed", _cmd_unnamed))
    app.add_handler(CommandHandler("eta", _cmd_eta))
    app.add_handler(CallbackQueryHandler(_on_habit_callback, pattern=r"^habit:"))
    app.add_handler(CallbackQueryHandler(_on_newdevice_callback, pattern=r"^nd:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))

    # Register bot commands with Telegram so they appear in the / menu
    async def _post_init(application) -> None:
        from telegram import BotCommand

        # The health watchdog lives in this process so it can still report if the scanner dies
        application.bot_data["health_task"] = asyncio.create_task(
            bt_health.run_loop(_get_db_path(), load_config, send_message),
        )
        # ...and so does the nightly database backup to the external drive
        application.bot_data["backup_task"] = asyncio.create_task(
            bt_backup.run_loop(_get_db_path(), bt_backup.CONFIG_PATH, load_config),
        )
        # ...and the opt-in "later than usual" check (does nothing unless late_alerts_enabled)
        application.bot_data["late_task"] = asyncio.create_task(
            bt_presence.run_late_loop(_get_db_path(), load_config, send_message),
        )

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
            BotCommand("habits", "List outstanding habits (tap to complete)"),
            BotCommand("unnamed", "Name connected devices that have no name"),
            BotCommand("eta", "When people who are out will be home"),
        ])

    app.post_init = _post_init
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
