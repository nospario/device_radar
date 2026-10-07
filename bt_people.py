#!/usr/bin/env python3
"""People and device roles.

Home/away for a *person* is decided by their **phone**, not by laptops or smart
home gear that sit on the WiFi all day. Every device has a ``role``:

* ``phone``: drives home/away and arrival/departure alerts
* ``laptop``: laptops, desktops, tablets (informational)
* ``smart_home``: speakers, plugs, cameras, router... (infrastructure)
* ``other``

A role can be set explicitly (``devices.role``) or is worked out from the
device type (``effective_role``), so devices named before roles existed keep
working with no data migration. Likewise a device belongs to a person through
``devices.person`` or, if that is empty, through a friendly name such as
"Laura's MacBook" (``effective_person``).

A person is **home** if any phone in their group (the phone plus its linked
WiFi/Bluetooth records) is detected, **away** if they have a phone but none is
detected, and **no_phone** if no phone is tracked for them.
"""

from __future__ import annotations

import re
import time
from typing import Any

PHONE = "phone"
LAPTOP = "laptop"
SMART_HOME = "smart_home"
OTHER = "other"
ROLES = (PHONE, LAPTOP, SMART_HOME, OTHER)

# Device types (as set in the dashboard or by the scanner) -> role
_TYPE_ROLE = {
    "phone": PHONE, "iphone": PHONE,
    "laptop": LAPTOP, "desktop": LAPTOP, "tablet": LAPTOP, "computer": LAPTOP,
    "watch": OTHER, "headphones": OTHER,
    "smart speaker": SMART_HOME, "speaker": SMART_HOME, "smart plug": SMART_HOME, "iot": SMART_HOME,
    "wifi router": SMART_HOME, "printer": SMART_HOME, "tv": SMART_HOME, "gaming console": SMART_HOME,
    "tooth brush": SMART_HOME,
}

_PERSON_RE = re.compile(r"^\s*([A-Za-z][A-Za-z\-]{0,29})['’]s\b")


def role_from_type(device_type: str | None) -> str | None:
    """Role implied by a device type, or None if the type says nothing (e.g. 'Network Device')."""
    return _TYPE_ROLE.get((device_type or "").strip().lower())


def effective_role(dev: dict[str, Any]) -> str | None:
    """The explicit role if valid, otherwise the one implied by the device type."""
    role = (dev.get("role") or "").strip().lower()
    return role if role in ROLES else role_from_type(dev.get("device_type"))


def person_from_name(name: str | None) -> str | None:
    """'Laura's MacBook' -> 'laura'. None if the name is not of the form "<Name>'s ..."."""
    match = _PERSON_RE.match(name or "")
    return match.group(1).lower() if match else None


def normalise_person(value: str | None) -> str | None:
    """Clean a person key for storing: lowercase letters/digits/space/hyphen, max 30 chars."""
    clean = re.sub(r"[^a-z0-9 \-]", "", (value or "").strip().lower())[:30].strip()
    return clean or None


def effective_person(dev: dict[str, Any]) -> str | None:
    """The explicit person if set, otherwise taken from the friendly name."""
    explicit = normalise_person(dev.get("person"))
    return explicit or person_from_name(dev.get("friendly_name"))


def notify_allowed(config: dict[str, Any], members: list[dict[str, Any]]) -> bool:
    """May this device (or link group) send an arrival/departure Telegram alert?

    ``members`` is the device alone, or the whole link group. The existing rule
    (some member has ``is_notify``) still applies; on top of it, with
    ``notify_phones_only`` (default true) some member must be a phone.
    """
    if not any(m.get("is_notify") for m in members):
        return False
    if config.get("notify_phones_only", True) is False:
        return True
    return any(effective_role(m) == PHONE for m in members)


# ---------------------------------------------------------------------------
# Who is home
# ---------------------------------------------------------------------------

def _rows(conn: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM devices")]


def _display(person: str) -> str:
    return person.title()


def _person_keys(config: dict[str, Any], devices: list[dict[str, Any]]) -> list[str]:
    keys: dict[str, None] = {}
    for key in (config.get("person_aliases") or {}):
        k = normalise_person(key)
        if k:
            keys[k] = None
    for dev in devices:
        k = effective_person(dev)
        if k:
            keys[k] = None
    return sorted(keys)


def _label(dev: dict[str, Any]) -> str:
    return dev.get("friendly_name") or dev.get("advertised_name") or dev["mac_address"]


def people_status(conn: Any, config: dict[str, Any], now: float | None = None) -> list[dict[str, Any]]:
    """Home/away for every known person, home first. See the module docstring."""
    now = time.time() if now is None else now
    devices = _rows(conn)
    by_link: dict[str, list[dict[str, Any]]] = {}
    for dev in devices:
        if dev.get("linked_to"):
            by_link.setdefault(dev["linked_to"], []).append(dev)

    result = []
    for person in _person_keys(config, devices):
        mine = [d for d in devices if effective_person(d) == person]
        phone_roots = [d for d in mine if effective_role(d) == PHONE and not d.get("linked_to")]
        members: list[dict[str, Any]] = []
        for root in phone_roots:
            members.append(root)
            members.extend(by_link.get(root["mac_address"], []))
        others = [_label(d) for d in mine if d not in members and not d.get("linked_to")]

        entry: dict[str, Any] = {
            "person": person, "display": _display(person), "state": "no_phone",
            "since": None, "last_seen": None, "phones": [_label(d) for d in phone_roots], "others": others,
        }
        if phone_roots:
            macs = [m["mac_address"] for m in members]
            home = any(m["state"] == "DETECTED" for m in members)
            entry["state"] = "home" if home else "away"
            entry["last_seen"] = max((m.get("last_seen") or 0) for m in members) or None
            marks = ",".join("?" * len(macs))
            event = "arrived" if home else "departed"
            row = conn.execute(
                f"SELECT MAX(timestamp) FROM events WHERE event_type = ? AND mac_address IN ({marks})",
                [event, *macs],
            ).fetchone()
            entry["since"] = row[0] if row and row[0] else None
        result.append(entry)

    order = {"home": 0, "away": 1, "no_phone": 2}
    result.sort(key=lambda e: (order[e["state"]], e["person"]))
    return result


def person_entry(conn: Any, config: dict[str, Any], name: str) -> dict[str, Any] | None:
    """The people_status entry for a typed name like 'laura', or None if not a known person."""
    key = normalise_person(name)
    return next((e for e in people_status(conn, config) if e["person"] == key), None)


def best_phone(conn: Any, config: dict[str, Any], name: str) -> dict[str, Any] | None:
    """The phone device to answer for a person: a detected one first, else the most recently seen."""
    key = normalise_person(name)
    phones = [d for d in _rows(conn)
              if effective_person(d) == key and effective_role(d) == PHONE and not d.get("linked_to")]
    if not phones:
        return None
    phones.sort(key=lambda d: (d["state"] != "DETECTED", -(d.get("last_seen") or 0)))
    return phones[0]
