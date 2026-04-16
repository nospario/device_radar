#!/usr/bin/env python3
"""Alexa Welcome Home Announcements — generates personalised greetings via
Ollama and speaks them through an Amazon Echo using alexa_remote_control.sh."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

import bt_calendar
import bt_db
import bt_news
import bt_tasks
import bt_weather

logger = logging.getLogger("bt_alexa")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

# In-memory cooldown tracker: {mac_address: last_announcement_timestamp}
_cooldowns: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config() -> dict[str, Any]:
    """Load config.json."""
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open() as f:
            return json.load(f)
    return {}


def _parse_env_file(path: str) -> dict[str, str]:
    """Parse a KEY=VALUE env file into a dict."""
    env: dict[str, str] = {}
    env_path = Path(path)
    if not env_path.exists():
        logger.warning("Alexa env file not found: %s", path)
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if key:
            env[key] = val
    return env


# ---------------------------------------------------------------------------
# Ollama greeting generation
# ---------------------------------------------------------------------------

def _format_duration(seconds: float) -> str:
    """Format a duration in seconds as a human-readable string."""
    if seconds < 60:
        return "less than a minute"
    if seconds < 3600:
        mins = int(seconds / 60)
        return f"{mins} minute{'s' if mins != 1 else ''}"
    if seconds < 86400:
        hours = int(seconds / 3600)
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = int(seconds / 86400)
    return f"{days} day{'s' if days != 1 else ''}"


def _resolve_person_name(device_name: str, config: dict[str, Any]) -> str:
    """Resolve a device name to a person name via person_aliases (reverse lookup)."""
    aliases = config.get("person_aliases", {})
    for person, dev in aliases.items():
        if dev.lower() == device_name.lower():
            return person.capitalize()
    # Fall back to extracting name from device name like "Richard's iPhone"
    if "'s " in device_name:
        return device_name.split("'s ")[0]
    return device_name


def _get_time_away(mac: str, db_path: Path) -> str | None:
    """Get how long a device has been away by checking last departure event."""
    try:
        conn = bt_db.get_connection(db_path)
        events = bt_db.get_events(conn, mac=mac, event_type="departed", limit=1)
        conn.close()
        if events:
            departed_at = events[0]["timestamp"]
            away_seconds = time.time() - departed_at
            if away_seconds > 0:
                return _format_duration(away_seconds)
    except Exception:
        logger.debug("Could not determine time away for %s", mac, exc_info=True)
    return None


async def _build_prefix(config: dict[str, Any]) -> str:
    """Build the spoken prefix with current time and weather."""
    current_time = datetime.now().strftime("%-I:%M %p")
    weather = await bt_weather.get_current_weather(config)
    if weather:
        return f"It's {current_time}, {weather}."
    return f"It's {current_time}."


async def _generate_greeting(
    person_name: str, time_away: str | None, config: dict[str, Any],
    calendar_context: str = "",
) -> str | None:
    """Generate a welcome greeting via Ollama."""
    base_url = config.get("ollama_url", "http://localhost:11434")
    timeout = config.get("ollama_timeout_seconds", 15)
    model = config.get("alexa_ollama_model") or config.get("ollama_model", "qwen2.5:1.5b")

    away_context = ""
    if time_away:
        away_context = f" They have been away for {time_away}."

    now = datetime.now()
    today_str = now.strftime("%A %-d %B %Y")

    prompt = (
        f"Generate a single sentence welcome home greeting for {person_name}. "
        f"Today is {today_str}.{away_context} "
        f"{calendar_context}"
        f"Keep it casual, warm, and under 30 words. "
        f"If calendar events are listed, mention at least one by name exactly as given. "
        f"Do not use emoji, hashtags, special characters, or quotation marks. "
        f"Just output the greeting, nothing else."
    )

    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }

    try:
        # Fetch weather in parallel with Ollama call
        async with httpx.AsyncClient() as client:
            ollama_task = client.post(
                f"{base_url}/api/generate", json=payload, timeout=timeout,
            )
            prefix_task = _build_prefix(config)
            resp, prefix = await asyncio.gather(ollama_task, prefix_task)

            resp.raise_for_status()
            greeting = resp.json().get("response", "").strip()
            greeting = greeting.strip('"').strip("'")
            if greeting:
                return f"{prefix} {greeting}"
    except httpx.TimeoutException:
        logger.warning("Ollama timed out generating greeting after %ds", timeout)
    except Exception as e:
        logger.error("Ollama greeting error: %s", e)
    return None


# ---------------------------------------------------------------------------
# Alexa script execution
# ---------------------------------------------------------------------------

def resolve_device_alias(alias: str, config: dict[str, Any]) -> str | None:
    """Resolve a short alias (e.g. 'kitchen') to an Echo device name."""
    devices = config.get("alexa_devices", {})
    for key, value in devices.items():
        if key.lower() == alias.lower():
            return value
    return None


async def speak(
    message: str, config: dict[str, Any],
    device: str | None = None, voice: str | None = None,
) -> bool:
    """Execute alexa_remote_control.sh to speak a message on an Echo device.

    If ``voice`` is set to an Amazon Polly voice name (e.g. "Brian"),
    the message is wrapped in SSML ``<voice>`` tags.
    """
    script_path = config.get("alexa_script_path", "/opt/bt-monitor/alexa_remote_control.sh")
    device_name = device or config.get("alexa_device_name", "Laura's Echo")
    env_file = config.get("alexa_env_file", "/home/pi/.alexa-env")

    if not Path(script_path).exists():
        logger.error("Alexa script not found: %s", script_path)
        return False

    # Build environment from env file
    env = _parse_env_file(env_file)
    if not env.get("REFRESH_TOKEN"):
        logger.error("REFRESH_TOKEN not found in %s", env_file)
        return False

    # Wrap in SSML voice tags if a Polly voice is specified
    if voice:
        message = f"<speak><voice name='{voice}'>{message}</voice></speak>"

    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                script_path, "-d", device_name, "-e", f"speak:{message}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**dict(__import__("os").environ), **env},
            ),
            timeout=30,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            logger.error(
                "Alexa script failed (rc=%d): stdout=%s stderr=%s",
                proc.returncode,
                stdout.decode().strip(),
                stderr.decode().strip(),
            )
            return False

        logger.debug("Alexa script output: %s", stdout.decode().strip())
        return True

    except asyncio.TimeoutError:
        logger.error("Alexa script timed out after 30s")
        return False
    except Exception as e:
        logger.error("Alexa script error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def announce_arrival(
    device_name: str, mac: str, config: dict[str, Any], db_path: Path,
) -> None:
    """Generate and speak a welcome home greeting for an arriving device.

    Called from bt_scanner.py when a welcome-enabled device arrives.
    Handles cooldown, Ollama greeting generation, and Alexa TTS.
    """
    cooldown = config.get("alexa_cooldown_seconds", 300)
    now = time.time()

    # Check cooldown
    last = _cooldowns.get(mac, 0)
    if now - last < cooldown:
        logger.debug(
            "Alexa cooldown active for %s (%ds remaining)",
            device_name, int(cooldown - (now - last)),
        )
        return

    _cooldowns[mac] = now

    # Resolve person name
    person_name = _resolve_person_name(device_name, config)

    # Get time away
    time_away = _get_time_away(mac, db_path)

    # Get calendar context for this device
    calendar_context = ""
    try:
        conn = bt_db.get_connection(db_path)
        device = bt_db.get_device(conn, mac)
        conn.close()
        if device:
            calendar_context = await bt_calendar.get_device_calendar_context(
                device, config,
            )
    except Exception:
        logger.debug("Calendar context fetch failed for greeting", exc_info=True)

    # Generate greeting via Ollama
    greeting = await _generate_greeting(person_name, time_away, config, calendar_context=calendar_context)
    if not greeting:
        greeting = f"Welcome home {person_name}"
        logger.info("Using fallback greeting for %s", person_name)

    # Append news headlines
    if device:
        try:
            news_suffix = await bt_news.get_device_news_suffix(device, config, db_path)
            if news_suffix:
                greeting = f"{greeting} {news_suffix}"
        except Exception:
            logger.debug("News suffix failed for greeting", exc_info=True)

    logger.info("Alexa announcement for %s: %s", person_name, greeting)

    # Speak on Echo (use device's configured voice if set)
    voice = device.get("alexa_voice") if device else None
    success = await speak(greeting, config, voice=voice or None)
    if success:
        logger.info("Alexa spoke greeting for %s", person_name)
    else:
        logger.error("Failed to speak greeting for %s", person_name)


# ---------------------------------------------------------------------------
# Player state query
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Encourage mode
# ---------------------------------------------------------------------------

async def generate_encouragement(
    prompt: str, config: dict[str, Any], calendar_context: str = "",
) -> str | None:
    """Generate an encouraging message via Ollama."""
    base_url = config.get("ollama_url", "http://localhost:11434")
    timeout = config.get("ollama_timeout_seconds", 15)
    model = config.get("alexa_ollama_model") or config.get("ollama_model", "qwen2.5:1.5b")

    now = datetime.now()
    today_str = now.strftime("%A %-d %B %Y")

    full_prompt = (
        f"{prompt} "
        f"Today is {today_str}. "
        f"{calendar_context}"
        f"Keep it to a single sentence, casual and friendly, under 30 words. "
        f"If calendar events are listed, mention at least one by name exactly as given. "
        f"Do not use emoji, hashtags, special characters, or quotation marks. "
        f"Vary the message each time. Just output the message, nothing else."
    )

    payload: dict[str, Any] = {
        "model": model,
        "prompt": full_prompt,
        "stream": False,
    }

    try:
        # Fetch weather in parallel with Ollama call
        async with httpx.AsyncClient() as client:
            ollama_task = client.post(
                f"{base_url}/api/generate", json=payload, timeout=timeout,
            )
            prefix_task = _build_prefix(config)
            resp, prefix = await asyncio.gather(ollama_task, prefix_task)

            resp.raise_for_status()
            message = resp.json().get("response", "").strip()
            message = message.strip('"').strip("'")
            if message:
                return f"{prefix} {message}"
    except httpx.TimeoutException:
        logger.warning("Ollama timed out generating encouragement after %ds", timeout)
    except Exception as e:
        logger.error("Ollama encouragement error: %s", e)
    return None


async def run_encourage_loop(config: dict[str, Any], db_path: Path) -> None:
    """Background loop that sends encouragement messages to enabled Echo devices."""
    logger.info("Encourage loop started")

    while True:
        try:
            await asyncio.sleep(60)  # check every 60 seconds

            if not config.get("alexa_enabled"):
                continue

            conn = bt_db.get_connection(db_path)
            try:
                devices = bt_db.get_enabled_echo_devices(conn)
            finally:
                conn.close()

            if not devices:
                continue

            now = time.time()

            for dev in devices:
                device_name = dev["device_name"]
                interval = dev["encourage_interval"] * 60  # convert to seconds
                last = dev["last_encouraged"] or 0
                prompt = dev["encourage_prompt"]

                if not prompt:
                    continue

                if now - last < interval:
                    continue

                # Generate and speak the message
                message = await generate_encouragement(prompt, config)
                if not message:
                    logger.warning("Failed to generate encouragement for %s", device_name)
                    continue

                logger.info("Encourage %s: %s", device_name, message)
                success = await speak(message, config, device=device_name)

                if success:
                    conn = bt_db.get_connection(db_path)
                    bt_db.update_echo_last_encouraged(conn, device_name, now)
                    conn.close()
                    logger.info("Encouragement sent to %s", device_name)
                else:
                    logger.error("Failed to speak encouragement on %s", device_name)

        except Exception:
            logger.error("Error in encourage loop", exc_info=True)


# ---------------------------------------------------------------------------
# Task reminders (Obsidian Master Task List)
# ---------------------------------------------------------------------------

def _covers_all_tasks(message: str, tasks: list[str], min_ratio: float = 0.8) -> bool:
    """Check that a generated message mentions every task (by a distinctive word).

    A task "counts" as mentioned if at least one of its words longer than 4
    characters appears verbatim in the message (case-insensitive). We require
    ``min_ratio`` of tasks to pass before accepting the LLM output.
    """
    lower = message.lower()
    covered = 0
    for t in tasks:
        words = [w.strip(".,;:!?") for w in t.split() if len(w) > 4]
        if not words:
            covered += 1
            continue
        if any(w.lower() in lower for w in words):
            covered += 1
    return covered >= max(1, int(len(tasks) * min_ratio))


async def _generate_task_reminder(
    due_today: list[str], daily: list[str], config: dict[str, Any],
) -> str | None:
    """Generate an encouraging reminder message listing today's tasks."""
    base_url = config.get("ollama_url", "http://localhost:11434")
    timeout = config.get("ollama_timeout_seconds", 30)
    model = config.get("alexa_ollama_model") or config.get("ollama_model", "qwen2.5:1.5b")

    today_str = datetime.now().strftime("%A %-d %B %Y")

    sections: list[str] = []
    mentions: list[str] = []
    if due_today:
        sections.append(
            "Tasks due today:\n" + "\n".join(f"- {t}" for t in due_today)
        )
        mentions.append("the tasks due today")
    if daily:
        sections.append(
            "Daily reoccurring tasks:\n" + "\n".join(f"- {t}" for t in daily)
        )
        mentions.append("the daily reoccurring tasks")
    body = "\n\n".join(sections)
    mention_clause = (
        f" Cover {' and '.join(mentions)}." if len(mentions) > 1 else ""
    )

    prompt = (
        f"Today is {today_str}. Richard has the following outstanding items:\n\n"
        f"{body}\n\n"
        f"Write a warm, encouraging spoken reminder that names EVERY SINGLE "
        f"item from the list(s) above — do not skip, merge, omit, or invent "
        f"any.{mention_clause} Only mention items that appear above; if a "
        f"group is absent, do not refer to it. It is fine to be several "
        f"sentences long. Keep the tone friendly and motivating. Do not use "
        f"emoji, hashtags, special characters, bullet points, quotation marks, "
        f"or numbered lists. Output only the spoken message."
    )

    all_tasks = due_today + daily

    # num_predict large enough that a list of up to 10 tasks is not truncated.
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": 512, "temperature": 0.6},
    }

    try:
        async with httpx.AsyncClient() as client:
            ollama_task = client.post(
                f"{base_url}/api/generate", json=payload, timeout=timeout,
            )
            prefix_task = _build_prefix(config)
            resp, prefix = await asyncio.gather(ollama_task, prefix_task)

            resp.raise_for_status()
            message = resp.json().get("response", "").strip()
            message = message.strip('"').strip("'")
            if not message:
                return None
            if not _covers_all_tasks(message, all_tasks):
                logger.warning(
                    "Task reminder dropped tasks — using plain-read fallback"
                )
                return None
            return f"{prefix} {message}"
    except httpx.TimeoutException:
        logger.warning("Ollama timed out generating task reminder after %ds", timeout)
    except Exception as e:
        logger.error("Ollama task reminder error: %s", e)
    return None


async def run_task_reminder_loop(config: dict[str, Any], db_path: Path) -> None:
    """Background loop that reads uncompleted Obsidian tasks and speaks a
    reminder on each Echo device that has the task-reminder toggle enabled."""
    logger.info("Task reminder loop started")

    while True:
        try:
            await asyncio.sleep(60)

            if not config.get("alexa_enabled"):
                continue

            conn = bt_db.get_connection(db_path)
            try:
                devices = bt_db.get_task_reminder_echo_devices(conn)
            finally:
                conn.close()

            if not devices:
                continue

            now = time.time()
            due_devices = [
                d for d in devices
                if now - (d.get("last_tasks_message") or 0)
                >= (d.get("tasks_interval") or 120) * 60
            ]
            if not due_devices:
                continue

            # Read tasks once per loop pass — all due devices share the same list
            master_path = config.get(
                "obsidian_master_task_path", bt_tasks.DEFAULT_MASTER_PATH,
            )
            daily_path = config.get(
                "obsidian_daily_tasks_path", bt_tasks.DEFAULT_DAILY_PATH,
            )
            due_today = bt_tasks.get_todays_outstanding_tasks(master_path)
            daily = bt_tasks.get_daily_recurring_tasks(daily_path)

            if not due_today and not daily:
                logger.debug("No outstanding tasks for today — skipping reminders")
                # Still update the timestamp so we don't re-check every 60s
                conn = bt_db.get_connection(db_path)
                for dev in due_devices:
                    bt_db.update_echo_last_tasks_message(conn, dev["device_name"], now)
                conn.close()
                continue

            message = await _generate_task_reminder(due_today, daily, config)
            if not message:
                # Fallback: flat read of both lists
                prefix = await _build_prefix(config)
                parts: list[str] = [prefix]
                if due_today:
                    parts.append(
                        "Richard, tasks due today: " + "; ".join(due_today) + "."
                    )
                if daily:
                    parts.append(
                        "Daily tasks: " + "; ".join(daily) + "."
                    )
                parts.append("Give them a go when you can.")
                message = " ".join(parts)

            for dev in due_devices:
                device_name = dev["device_name"]
                logger.info("Task reminder %s: %s", device_name, message)
                success = await speak(message, config, device=device_name)
                if success:
                    conn = bt_db.get_connection(db_path)
                    bt_db.update_echo_last_tasks_message(conn, device_name, now)
                    conn.close()
                else:
                    logger.error("Failed to speak task reminder on %s", device_name)

        except Exception:
            logger.error("Error in task reminder loop", exc_info=True)


# ---------------------------------------------------------------------------
# Proximity-triggered messages
# ---------------------------------------------------------------------------

async def check_proximity_devices(config: dict[str, Any], db_path: Path) -> None:
    """Check proximity-enabled devices and speak messages when RSSI conditions are met."""
    conn = bt_db.get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM devices WHERE proximity_enabled = 1 AND state = 'DETECTED'"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return

    now = time.time()

    for row in rows:
        dev = dict(row)
        mac = dev["mac_address"]
        scan_type = (dev.get("scan_type") or "").lower()

        # Only BLE devices have meaningful RSSI
        if "ble" not in scan_type:
            continue

        rssi = dev.get("last_rssi")
        if rssi is None:
            continue

        threshold = dev.get("proximity_rssi_threshold") or -70
        if rssi < threshold:
            continue

        interval = (dev.get("proximity_interval") or 30) * 60  # minutes → seconds
        last = dev.get("last_proximity_message") or 0
        if now - last < interval:
            continue

        prompt = dev.get("proximity_prompt") or ""
        if not prompt:
            continue

        # Resolve calendar context for this device
        calendar_context = ""
        try:
            calendar_context = await bt_calendar.get_device_calendar_context(
                dev, config,
            )
        except Exception:
            logger.debug("Calendar context fetch failed for %s", mac, exc_info=True)

        # Generate message via Ollama (reuse encouragement generator)
        message = await generate_encouragement(prompt, config, calendar_context=calendar_context)
        if not message:
            dev_name = dev["friendly_name"] or dev["advertised_name"] or mac
            logger.warning("Failed to generate proximity message for %s", dev_name)
            continue

        # Append news headlines
        try:
            news_suffix = await bt_news.get_device_news_suffix(dev, config, db_path)
            if news_suffix:
                message = f"{message} {news_suffix}"
        except Exception:
            logger.debug("News suffix failed for proximity %s", mac, exc_info=True)

        # Determine which Echo to speak through
        echo_device = dev.get("proximity_alexa_device") or config.get("alexa_device_name")

        dev_name = dev["friendly_name"] or dev["advertised_name"] or mac
        voice = dev.get("alexa_voice") or None
        logger.info("Proximity message for %s (RSSI %s): %s", dev_name, rssi, message)
        success = await speak(message, config, device=echo_device, voice=voice)

        if success:
            conn = bt_db.get_connection(db_path)
            bt_db.update_device(conn, mac, last_proximity_message=now)
            conn.close()
        else:
            logger.error("Failed to speak proximity message for %s", dev_name)


# ---------------------------------------------------------------------------
# Test mode
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Test Alexa welcome announcement")
    parser.add_argument("--test", metavar="NAME", help="Person name to test with")
    args = parser.parse_args()

    if not args.test:
        parser.print_help()
        sys.exit(1)

    config = load_config()
    if not config.get("alexa_enabled"):
        logger.warning("alexa_enabled is not true in config — running anyway for test")

    db_path = BASE_DIR / config.get("db_path", "bt_radar.db")
    person = args.test

    async def _test() -> None:
        greeting = await _generate_greeting(person, None, config)
        if not greeting:
            greeting = f"Welcome home {person}"
            logger.info("Ollama unavailable — using fallback greeting")

        logger.info("Generated greeting: %s", greeting)

        success = await speak(greeting, config)
        if success:
            logger.info("Test complete — greeting spoken on Alexa")
        else:
            logger.error("Test failed — could not speak on Alexa")

    asyncio.run(_test())
