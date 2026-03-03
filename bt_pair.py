"""Bluetooth pairing helper — wraps bluetoothctl for pair/trust/remove operations."""

from __future__ import annotations

import logging
import re
import sqlite3
import subprocess
from dataclasses import dataclass

import bt_db

logger = logging.getLogger("bt_pair")

TIMEOUT = 30


@dataclass
class PairResult:
    success: bool
    message: str


@dataclass
class DeviceInfo:
    paired: bool
    trusted: bool
    connected: bool
    name: str | None


def _run(args: list[str], timeout: int = TIMEOUT) -> subprocess.CompletedProcess[str]:
    """Run a bluetoothctl command and return the result."""
    return subprocess.run(
        ["bluetoothctl"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def get_paired_devices() -> dict[str, str]:
    """Return {mac: name} of all paired devices from bluetoothctl."""
    result: dict[str, str] = {}
    try:
        proc = _run(["devices", "Paired"])
        for line in proc.stdout.splitlines():
            # Format: "Device AA:BB:CC:DD:EE:FF DeviceName"
            m = re.match(r"Device\s+([0-9A-Fa-f:]{17})\s+(.*)", line.strip())
            if m:
                result[m.group(1).upper()] = m.group(2).strip()
    except (subprocess.TimeoutExpired, OSError):
        logger.error("Failed to list paired devices", exc_info=True)
    return result


def get_device_info(mac: str) -> DeviceInfo | None:
    """Get pairing/trust/connection status for a specific device."""
    try:
        proc = _run(["info", mac.upper()])
        output = proc.stdout

        if "not available" in output.lower():
            return None

        paired = bool(re.search(r"Paired:\s*yes", output, re.IGNORECASE))
        trusted = bool(re.search(r"Trusted:\s*yes", output, re.IGNORECASE))
        connected = bool(re.search(r"Connected:\s*yes", output, re.IGNORECASE))
        name_match = re.search(r"Name:\s*(.*)", output)
        name = name_match.group(1).strip() if name_match else None

        return DeviceInfo(paired=paired, trusted=trusted, connected=connected, name=name)
    except (subprocess.TimeoutExpired, OSError):
        logger.error("Failed to get info for %s", mac, exc_info=True)
        return None


def pair_device(mac: str) -> PairResult:
    """Pair with a device. The user must confirm on their phone/device."""
    mac = mac.upper()
    try:
        proc = _run(["pair", mac], timeout=TIMEOUT)
        output = proc.stdout + proc.stderr

        if "Pairing successful" in output:
            # Also trust it so it auto-reconnects
            trust_device(mac)
            return PairResult(success=True, message="Pairing successful")

        if "AlreadyExists" in output or "Already Exists" in output:
            return PairResult(success=True, message="Device already paired")

        # Extract error message
        error = output.strip().split("\n")[-1] if output.strip() else "Unknown error"
        return PairResult(success=False, message=f"Pairing failed: {error}")
    except subprocess.TimeoutExpired:
        return PairResult(success=False, message="Pairing timed out — ensure the device is in pairing mode")
    except OSError as e:
        return PairResult(success=False, message=f"Failed to run bluetoothctl: {e}")


def trust_device(mac: str) -> PairResult:
    """Trust a device so it auto-reconnects."""
    mac = mac.upper()
    try:
        proc = _run(["trust", mac])
        output = proc.stdout + proc.stderr

        if "trust succeeded" in output.lower() or "Changing" in output:
            return PairResult(success=True, message="Device trusted")
        return PairResult(success=False, message=output.strip())
    except (subprocess.TimeoutExpired, OSError) as e:
        return PairResult(success=False, message=str(e))


def unpair_device(mac: str) -> PairResult:
    """Remove a device (unpair + untrust)."""
    mac = mac.upper()
    try:
        proc = _run(["remove", mac])
        output = proc.stdout + proc.stderr

        if "Device has been removed" in output or "removed" in output.lower():
            return PairResult(success=True, message="Device unpaired and removed")

        if "not available" in output.lower():
            return PairResult(success=True, message="Device was not paired")

        error = output.strip().split("\n")[-1] if output.strip() else "Unknown error"
        return PairResult(success=False, message=f"Unpair failed: {error}")
    except subprocess.TimeoutExpired:
        return PairResult(success=False, message="Unpair timed out")
    except OSError as e:
        return PairResult(success=False, message=f"Failed to run bluetoothctl: {e}")


def sync_paired_status(conn: sqlite3.Connection) -> int:
    """Query bluetoothctl for paired devices and update is_paired in DB.

    Returns the number of devices updated.
    """
    paired_macs = set(get_paired_devices().keys())
    updated = 0

    rows = conn.execute("SELECT mac_address, is_paired FROM devices").fetchall()
    for row in rows:
        mac = row["mac_address"]
        currently_paired = mac in paired_macs
        db_paired = bool(row["is_paired"])

        if currently_paired != db_paired:
            bt_db.update_device(conn, mac, is_paired=currently_paired)
            updated += 1

    if updated:
        logger.info("Synced paired status for %d device(s)", updated)
    return updated
