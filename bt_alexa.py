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

import bt_db

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

def _get_time_of_day() -> str:
    """Return morning/afternoon/evening based on current hour."""
    hour = datetime.now().hour
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


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


async def _generate_greeting(
    person_name: str, time_away: str | None, config: dict[str, Any],
) -> str | None:
    """Generate a welcome greeting via Ollama."""
    base_url = config.get("ollama_url", "http://localhost:11434")
    timeout = config.get("ollama_timeout_seconds", 15)
    model = config.get("alexa_ollama_model") or config.get("ollama_model", "qwen2.5:1.5b")

    time_of_day = _get_time_of_day()
    day_of_week = datetime.now().strftime("%A")

    away_context = ""
    if time_away:
        away_context = f" They have been away for {time_away}."

    prompt = (
        f"Generate a single sentence welcome home greeting for {person_name}. "
        f"It is {time_of_day} on {day_of_week}.{away_context} "
        f"Keep it casual, warm, and under 30 words. "
        f"Do not use emoji, hashtags, special characters, or quotation marks. "
        f"Just output the greeting, nothing else."
    )

    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/api/generate", json=payload, timeout=timeout,
            )
            resp.raise_for_status()
            greeting = resp.json().get("response", "").strip()
            # Strip any quotes the model may have wrapped around the greeting
            greeting = greeting.strip('"').strip("'")
            if greeting:
                return greeting
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


async def speak(message: str, config: dict[str, Any], device: str | None = None) -> bool:
    """Execute alexa_remote_control.sh to speak a message on an Echo device."""
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

    # Generate greeting via Ollama
    greeting = await _generate_greeting(person_name, time_away, config)
    if not greeting:
        greeting = f"Welcome home {person_name}"
        logger.info("Using fallback greeting for %s", person_name)

    logger.info("Alexa announcement for %s: %s", person_name, greeting)

    # Speak on Echo
    success = await speak(greeting, config)
    if success:
        logger.info("Alexa spoke greeting for %s", person_name)
    else:
        logger.error("Failed to speak greeting for %s", person_name)


# ---------------------------------------------------------------------------
# Player state query
# ---------------------------------------------------------------------------

async def query_player_state(
    device_name: str, config: dict[str, Any],
) -> str | None:
    """Query the current player state of an Echo device.

    Returns 'PLAYING', 'PAUSED', 'IDLE', or None on failure.
    """
    script_path = config.get("alexa_script_path", "/opt/bt-monitor/alexa_remote_control.sh")
    env_file = config.get("alexa_env_file", "/home/pi/.alexa-env")

    if not Path(script_path).exists():
        return None

    env = _parse_env_file(env_file)
    if not env.get("REFRESH_TOKEN"):
        return None

    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                script_path, "-d", device_name, "-q",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**dict(__import__("os").environ), **env},
            ),
            timeout=30,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = stdout.decode()

        # Parse currentState from the media/state JSON block
        import json as _json
        for line in output.split("\n{"):
            block = "{" + line if not line.startswith("{") else line
            try:
                data = _json.loads(block.strip())
                if "currentState" in data:
                    return data["currentState"]
            except _json.JSONDecodeError:
                continue
        return "IDLE"
    except Exception as e:
        logger.debug("Failed to query player state for %s: %s", device_name, e)
        return None


# ---------------------------------------------------------------------------
# Encourage mode
# ---------------------------------------------------------------------------

async def generate_encouragement(prompt: str, config: dict[str, Any]) -> str | None:
    """Generate an encouraging message via Ollama."""
    base_url = config.get("ollama_url", "http://localhost:11434")
    timeout = config.get("ollama_timeout_seconds", 15)
    model = config.get("alexa_ollama_model") or config.get("ollama_model", "qwen2.5:1.5b")

    time_of_day = _get_time_of_day()
    day_of_week = datetime.now().strftime("%A")

    full_prompt = (
        f"{prompt} "
        f"It is {time_of_day} on {day_of_week}. "
        f"Keep it to a single sentence, casual and friendly, under 30 words. "
        f"Do not use emoji, hashtags, special characters, or quotation marks. "
        f"Vary the message each time. Just output the message, nothing else."
    )

    payload: dict[str, Any] = {
        "model": model,
        "prompt": full_prompt,
        "stream": False,
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/api/generate", json=payload, timeout=timeout,
            )
            resp.raise_for_status()
            message = resp.json().get("response", "").strip()
            message = message.strip('"').strip("'")
            if message:
                return message
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

                # Check if music is playing (if required)
                if dev["encourage_when_playing"]:
                    state = await query_player_state(device_name, config)
                    if state != "PLAYING":
                        logger.debug(
                            "Skipping encourage for %s — state is %s",
                            device_name, state,
                        )
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
