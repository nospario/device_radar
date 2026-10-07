#!/usr/bin/env python3
"""New-device alerts: describe an unknown WiFi device and let you name it from Telegram.

When a WiFi/LAN device appears that Device Radar has never stored, the scanner
calls :func:`announce_new_wifi_device`, which sends one Telegram message with
what is known about it (hostname, IP, MAC, vendor, whether it uses a private
address) and buttons: Phone / Laptop / Smart home / Ignore. Choosing a role
makes the bot ask for a name (see ``bt_telegram``), then :func:`apply_role`
names the device and sets the right watch/notify flags:

* **Phone**: named, watched and notify on (phones drive home/away alerts)
* **Laptop** (laptops and tablets): named only
* **Smart home**: named only
* **Ignore**: hidden (the cleanup removes it later)

Each MAC is announced once. ``device_alerts`` remembers it, and the cleanup adds
the devices it deletes as ``forgotten`` so a device returning after a purge is
not announced as brand new. A per-hour cap stops a burst of alerts.

This module never imports ``bt_telegram``; the caller passes in ``send``.
"""

from __future__ import annotations

import html
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import bt_db
import bt_wifi

logger = logging.getLogger("bt_newdevice")

CALLBACK_PREFIX = "nd"
_MAC_RE = r"[0-9A-F]{2}(?::[0-9A-F]{2}){5}"
_CALLBACK_RE = re.compile(rf"^{CALLBACK_PREFIX}:(phone|laptop|home|ignore):({_MAC_RE})$")

# action -> what naming the device does
ROLES: dict[str, dict[str, Any]] = {
    "phone": {"label": "\U0001f4f1 Phone", "role": "phone", "device_type": "Phone",
              "watch": True, "notify": True},
    "laptop": {"label": "\U0001f4bb Laptop", "role": "laptop", "device_type": "Laptop",
               "watch": False, "notify": False},
    "home": {"label": "\U0001f3e0 Smart home", "role": "smart_home", "device_type": "IoT",
             "watch": False, "notify": False},
}
IGNORE = "ignore"

_PHONE_HOSTS = ("iphone", "android", "galaxy", "pixel", "oneplus", "redmi")
_LAPTOP_HOSTS = ("macbook", "mbp", "imac", "laptop", "desktop", "thinkpad", "surface", "ipad", "tablet")
_HOME_HOSTS = ("echo", "alexa", "cam", "plug", "bulb", "lamp", "hs100", "hs110", "ring", "nest",
               "chromecast", "firetv", "roku", "sonos", "hive", "doorbell", "printer", "tv")
_HOME_VENDORS = ("tp-link", "ring", "amazon", "google", "philips", "hive", "sonos", "tuya",
                 "espressif", "shelly", "belkin", "ikea", "garmin", "hp inc", "canon", "epson",
                 "brother")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    """From ``new_device_alerts_*`` keys in config.json."""

    enabled: bool = True
    dry_run: bool = False
    max_per_hour: int = 6


def load_settings(config: dict[str, Any]) -> Settings:
    d = Settings()
    enabled = config.get("new_device_alerts_enabled")
    dry = config.get("new_device_alerts_dry_run")
    cap = config.get("new_device_alerts_max_per_hour")
    return Settings(
        enabled=enabled if isinstance(enabled, bool) else d.enabled,
        dry_run=dry if isinstance(dry, bool) else d.dry_run,
        max_per_hour=cap if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1 else d.max_per_hour,
    )


# ---------------------------------------------------------------------------
# Describing a device
# ---------------------------------------------------------------------------

def guess_action(dev: dict[str, Any]) -> str | None:
    """Best guess at the button to tap ('phone', 'laptop', 'home') or None."""
    host = (dev.get("advertised_name") or "").lower()
    # Match whole words at the start of a hostname part ("RingDoorbell-9e.lan"
    # -> ringdoorbell, 9e, lan), so "Sterling-laptop" is not mistaken for Ring.
    tokens = [t for t in re.split(r"[^a-z0-9]+", host) if t]
    vendor = (dev.get("manufacturer") or "").lower()

    def hint(words: tuple[str, ...]) -> bool:
        return any(t.startswith(w) for t in tokens for w in words)

    if hint(_PHONE_HOSTS):
        return "phone"
    if hint(_LAPTOP_HOSTS) or (tokens and tokens[0] == "mac"):
        return "laptop"
    if hint(_HOME_HOSTS) or any(v in vendor for v in _HOME_VENDORS):
        return "home"
    return None


def _hostname(dev: dict[str, Any]) -> str | None:
    name = dev.get("advertised_name")
    if name and name != dev.get("ip_address"):
        return name
    return None


def describe(dev: dict[str, Any]) -> str:
    """One-line HTML summary used in alerts and in the 'name this' prompts."""
    bits = [html.escape(_hostname(dev) or dev.get("ip_address") or dev["mac_address"])]
    if dev.get("ip_address") and _hostname(dev):
        bits.append(html.escape(dev["ip_address"]))
    return " · ".join(bits)


def alert_text(dev: dict[str, Any]) -> str:
    """Full HTML message for a new device."""
    mac = dev["mac_address"]
    lines = ["\U0001f195 <b>New device on the WiFi</b>"]
    host = _hostname(dev)
    lines.append(f"Name: {html.escape(host)}" if host else "Name: (none announced)")
    if dev.get("ip_address"):
        lines.append(f"IP: {html.escape(dev['ip_address'])}")
    lines.append(f"MAC: <code>{html.escape(mac)}</code>")
    vendor = dev.get("manufacturer")
    if vendor:
        lines.append(f"Maker: {html.escape(vendor)}")
    elif bt_wifi.is_private_mac(mac):
        lines.append("Maker: unknown. It uses a private (randomised) address, which phones, "
                     "tablets and laptops do.")
    else:
        lines.append("Maker: not in the vendor list")
    guess = guess_action(dev)
    if guess:
        lines.append(f"Looks like: {ROLES[guess]['label']}")
    lines.append("\nTap what it is to name it:")
    return "\n".join(lines)


def callback_data(action: str, mac: str) -> str:
    return f"{CALLBACK_PREFIX}:{action}:{mac.upper()}"


def parse_callback(data: str | None) -> tuple[str, str] | None:
    """Return (action, mac) for a valid button payload, else None."""
    match = _CALLBACK_RE.match(data or "")
    return (match.group(1), match.group(2)) if match else None


def keyboard(dev: dict[str, Any]) -> dict[str, Any]:
    """Telegram inline keyboard (as a dict). A likely role is starred and listed first."""
    mac = dev["mac_address"]
    guess = guess_action(dev)
    order = list(ROLES)
    if guess:
        order.remove(guess)
        order.insert(0, guess)
    row = []
    for action in order:
        label = ROLES[action]["label"] + (" ⭐" if action == guess else "")
        row.append({"text": label, "callback_data": callback_data(action, mac)})
    row.append({"text": "\U0001f648 Ignore", "callback_data": callback_data(IGNORE, mac)})
    return {"inline_keyboard": [row[:2], row[2:]]}


# ---------------------------------------------------------------------------
# Bookkeeping (device_alerts)
# ---------------------------------------------------------------------------

def is_recorded(conn: Any, mac: str) -> bool:
    bt_db.ensure_alert_tables(conn)
    return conn.execute("SELECT 1 FROM device_alerts WHERE mac = ?", (mac.upper(),)).fetchone() is not None


def record(conn: Any, mac: str, kind: str, status: str, now: float | None = None) -> None:
    bt_db.ensure_alert_tables(conn)
    conn.execute(
        "INSERT INTO device_alerts (mac, kind, at, status) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(mac) DO UPDATE SET status = excluded.status, at = excluded.at",
        (mac.upper(), kind, time.time() if now is None else now, status),
    )
    conn.commit()


def forget(conn: Any, macs: list[str], now: float | None = None) -> None:
    """Mark devices the cleanup deleted, so they are not announced as new if they return.

    Runs inside the caller's transaction and does not commit: the cleanup calls
    it between selecting and deleting a batch while holding the write lock.
    The caller must have run ``bt_db.ensure_alert_tables`` beforehand.
    """
    stamp = time.time() if now is None else now
    conn.executemany(
        "INSERT OR IGNORE INTO device_alerts (mac, kind, at, status) VALUES (?, 'forgotten', ?, 'forgotten')",
        [(m.upper(), stamp) for m in macs],
    )


def _recent_alerts(conn: Any, now: float, window: float = 3600) -> int:
    bt_db.ensure_alert_tables(conn)
    return conn.execute(
        "SELECT COUNT(*) FROM device_alerts WHERE kind = 'new' AND status IN ('sent', 'dry_run') AND at > ?",
        (now - window,),
    ).fetchone()[0]


async def announce_new_wifi_device(
    conn: Any,
    dev: dict[str, Any],
    settings: Settings,
    send: Callable[..., Awaitable[bool]],
    now: float | None = None,
) -> str:
    """Send the "new device" alert if appropriate. Returns what happened.

    One of: ``disabled``, ``known``, ``rate_limited``, ``dry_run``, ``sent``, ``failed``.
    """
    now = time.time() if now is None else now
    mac = dev["mac_address"]
    if not settings.enabled:
        return "disabled"
    if is_recorded(conn, mac):
        return "known"
    if _recent_alerts(conn, now) >= settings.max_per_hour:
        record(conn, mac, "new", "suppressed", now)
        logger.info("New-device alert for %s suppressed (over %d/hour); see /unnamed", mac, settings.max_per_hour)
        return "rate_limited"
    if settings.dry_run:
        record(conn, mac, "new", "dry_run", now)
        logger.info("[dry run] would announce new device: %s", re.sub(r"<[^>]+>", "", describe(dev)))
        return "dry_run"
    ok = await send(alert_text(dev), reply_markup=keyboard(dev))
    record(conn, mac, "new", "sent" if ok else "failed", now)
    if not ok:
        logger.warning("Could not send new-device alert for %s", mac)
    return "sent" if ok else "failed"


# ---------------------------------------------------------------------------
# Acting on a button
# ---------------------------------------------------------------------------

def apply_role(conn: Any, mac: str, action: str, name: str | None = None) -> dict[str, Any]:
    """Name a device (or hide it for 'ignore'). Returns ``{"ok", "reason", "device"}``.

    ``reason`` is ``done``, ``gone`` (no such device), ``already_named`` or
    ``bad_request``. Never overwrites an existing friendly name.
    """
    dev = bt_db.get_device(conn, mac)
    if dev is None:
        return {"ok": False, "reason": "gone", "device": None}
    if (dev.get("friendly_name") or "").strip():
        return {"ok": False, "reason": "already_named", "device": dev}
    if action == IGNORE:
        bt_db.update_device(conn, mac, is_hidden=True)
        record(conn, mac, "new", "ignored")
        return {"ok": True, "reason": "done", "device": bt_db.get_device(conn, mac)}
    role = ROLES.get(action)
    clean = (name or "").strip()
    if role is None or not clean:
        return {"ok": False, "reason": "bad_request", "device": dev}
    bt_db.update_device(
        conn, mac, friendly_name=clean, device_type=role["device_type"], role=role["role"],
        is_watchlisted=role["watch"], is_notify=role["notify"],
    )
    record(conn, mac, "new", "named")
    return {"ok": True, "reason": "done", "device": bt_db.get_device(conn, mac)}


def list_unnamed_connected(conn: Any, limit: int = 10) -> list[dict[str, Any]]:
    """Connected, visible WiFi devices with no friendly name (excludes linked secondaries)."""
    rows = conn.execute(
        "SELECT * FROM devices WHERE state = 'DETECTED' AND scan_type LIKE '%WiFi%' "
        "AND is_hidden = 0 AND linked_to IS NULL AND COALESCE(friendly_name, '') = '' "
        "ORDER BY first_seen DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
