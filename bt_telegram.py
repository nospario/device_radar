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
# Ollama
# ---------------------------------------------------------------------------

async def _ollama_chat(
    messages: list[dict[str, str]], config: dict[str, Any],
) -> str | None:
    """Send a chat request to Ollama via /api/generate.

    Converts the messages list into a single prompt string since
    /api/generate doesn't support multi-message chat format.
    """
    base_url = config.get("ollama_url", "http://localhost:11434")
    timeout = config.get("ollama_timeout_seconds", 15)
    model = config.get("ollama_model", "qwen2.5:1.5b")

    # Build a single prompt from the message history
    parts: list[str] = []
    system_prompt = ""
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if role == "system":
            system_prompt = content
        elif role == "user":
            parts.append(f"User: {content}")
        elif role == "assistant":
            parts.append(f"Assistant: {content}")
    parts.append("Assistant:")
    prompt = "\n".join(parts)

    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }
    if system_prompt:
        payload["system"] = system_prompt

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/api/generate", json=payload, timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json().get("response", "").strip()
    except httpx.TimeoutException:
        logger.warning("Ollama timed out after %ds", timeout)
        return None
    except Exception as e:
        logger.error("Ollama error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Telegram bot handlers
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


async def _cmd_home(update, context) -> None:
    """Handle /home — quick summary of detected devices."""
    if not _is_authorized(update.effective_chat.id):
        return
    config = load_config()
    db_path = BASE_DIR / config.get("db_path", "bt_radar.db")
    await update.message.reply_text(
        await answer_presence("who is home", config, db_path),
    )


async def _cmd_devices(update, context) -> None:
    """Handle /devices — list all watchlisted devices with status."""
    if not _is_authorized(update.effective_chat.id):
        return
    data = await _api_get("/api/devices", {"watchlisted": "1"})
    if not data:
        await update.message.reply_text("No watchlisted devices found.")
        return
    lines = []
    for d in data:
        name = d.get("friendly_name") or d.get("advertised_name") or d["mac_address"]
        icon = "\U0001f7e2" if d.get("state") == "DETECTED" else "\U0001f534"
        lines.append(f"{icon} {name} \u2014 {_time_ago(d.get('last_seen'))}")
    await update.message.reply_text("\n".join(lines))


async def _cmd_lastseen(update, context) -> None:
    """Handle /lastseen <name> — when a device was last detected."""
    if not _is_authorized(update.effective_chat.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /lastseen <name>")
        return
    name = " ".join(context.args)
    config = load_config()
    db_path = BASE_DIR / config.get("db_path", "bt_radar.db")
    conn = bt_db.get_connection(db_path)
    dev = _resolve_person(name, config, conn)
    conn.close()
    if not dev:
        await update.message.reply_text(f"Unknown: \"{name}\"")
        return
    dev_name = dev.get("friendly_name") or dev.get("advertised_name") or dev["mac_address"]
    state = "detected \U0001f7e2" if dev["state"] == "DETECTED" else "lost \U0001f534"
    await update.message.reply_text(
        f"{dev_name} \u2014 {state}, last seen {_time_ago(dev.get('last_seen'))}",
    )


async def _handle_message(update, context) -> None:
    """Route incoming messages to presence handler or Ollama."""
    if not update.message or not update.message.text:
        return
    if not _is_authorized(update.effective_chat.id):
        return

    text = update.message.text
    config = load_config()
    db_path = BASE_DIR / config.get("db_path", "bt_radar.db")
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
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend({"role": m["role"], "content": m["content"]} for m in history)

    response = await _ollama_chat(messages, config)
    if response is None:
        response = "Sorry, I'm thinking too hard about that one. Try again in a moment."

    conn = bt_db.get_connection(db_path)
    bt_db.save_chat_message(conn, chat_id, "assistant", response)
    conn.close()

    await update.message.reply_text(response)


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
    app.add_handler(CommandHandler("devices", _cmd_devices))
    app.add_handler(CommandHandler("lastseen", _cmd_lastseen))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))

    # Register bot commands with Telegram so they appear in the / menu
    async def _post_init(application) -> None:
        from telegram import BotCommand
        await application.bot.set_my_commands([
            BotCommand("home", "Who is home right now"),
            BotCommand("devices", "List all watched devices with status"),
            BotCommand("lastseen", "When a device was last seen"),
        ])

    app.post_init = _post_init
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
