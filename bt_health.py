#!/usr/bin/env python3
"""Health watchdog: notices problems and tells you once, in Telegram.

It runs as a loop inside the Telegram bot process (so it keeps working, and can
report, when the scanner itself has died) and stores the latest result of every
check in ``health_results`` for the dashboard and ``/status``.

Checks: scanner heartbeat, systemd services, Ollama, calendar login (iCloud),
nightly backup freshness, disk space (SD card and external drive), CPU temperature and throttling,
pending updates and reboot, clock sync, database integrity, and **always-on
devices** (doorbell, camera, hub, ...) that have been offline too long.

Alerts are deliberately quiet:

* a problem must be seen on ``confirm`` consecutive runs before it is reported
* one message covers everything that changed in a run
* a recovery message follows ("back to normal")
* a problem that is still failing gets a reminder at most once a day

Every check function takes what it needs as arguments (clock, command runner,
HTTP getter) so it can be tested without a Pi.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import bt_backup
import bt_cleanup
import bt_db

logger = logging.getLogger("bt_health")

OK, WARN, FAIL = "ok", "warn", "fail"
_SEVERITY = {OK: 0, WARN: 1, FAIL: 2}

DEFAULT_SERVICES = ("bt-scanner", "bt-web", "bt-telegram", "pihole-FTL", "ollama",
                    "obsidian-sync", "nftables", "ssh")


@dataclass
class Check:
    """The result of one check."""

    key: str
    label: str
    status: str
    message: str
    confirm: int = 2          # consecutive bad runs before it is reported
    slow: str | None = None   # name of the cadence for slow checks (calendar, updates, database)


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    dry_run: bool = False
    interval_seconds: int = 300
    reminder_hours: float = 24.0
    services: tuple[str, ...] = DEFAULT_SERVICES
    disk_warn: int = 85
    disk_fail: int = 95
    temp_warn: float = 80.0
    temp_fail: float = 85.0
    updates_warn: int = 50
    offline_minutes: float = 20.0
    external_path: str = "/mnt/external"
    calendar_hours: float = 6.0
    updates_hours: float = 24.0
    database_hours: float = 24.0


def _int(value: Any, default: int, minimum: int = 1) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= minimum else default


def _num(value: Any, default: float, minimum: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= minimum else default


def load_settings(config: dict[str, Any]) -> Settings:
    d = Settings()
    services = config.get("health_services")
    if not (isinstance(services, list) and all(isinstance(s, str) and s for s in services)):
        services = list(d.services)
    enabled, dry = config.get("health_alerts_enabled"), config.get("health_alerts_dry_run")
    return Settings(
        enabled=enabled if isinstance(enabled, bool) else d.enabled,
        dry_run=dry if isinstance(dry, bool) else d.dry_run,
        interval_seconds=_int(config.get("health_interval_seconds"), d.interval_seconds, 30),
        reminder_hours=_num(config.get("health_reminder_hours"), d.reminder_hours, 1),
        services=tuple(services),
        disk_warn=_int(config.get("health_disk_warn_percent"), d.disk_warn),
        disk_fail=_int(config.get("health_disk_fail_percent"), d.disk_fail),
        temp_warn=_num(config.get("health_temp_warn_c"), d.temp_warn, 1),
        temp_fail=_num(config.get("health_temp_fail_c"), d.temp_fail, 1),
        updates_warn=_int(config.get("health_updates_warn"), d.updates_warn),
        offline_minutes=_num(config.get("health_offline_minutes"), d.offline_minutes, 1),
        external_path=str(config.get("health_external_path") or d.external_path),
    )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    if seconds < 172800:
        return f"{round(seconds / 3600)} h"
    return f"{round(seconds / 86400)} days"


def check_scanner(conn: sqlite3.Connection, now: float) -> Check:
    """The scanner writes a heartbeat every scan cycle (about every 25 s)."""
    bt_db.ensure_scanner_tables(conn)
    row = conn.execute("SELECT value FROM scanner_state WHERE key = 'heartbeat'").fetchone()
    if not row:
        return Check("scanner", "Scanner", WARN, "no heartbeat recorded yet")
    age = now - float(row[0])
    if age <= 180:
        return Check("scanner", "Scanner", OK, f"scanning (heartbeat {_ago(age)} ago)")
    status = FAIL if age > 600 else WARN
    return Check("scanner", "Scanner", status, f"no heartbeat for {_ago(age)}; the scanner may be stuck or stopped")


def check_services(names: tuple[str, ...], is_active: Callable[[str], str]) -> list[Check]:
    checks = []
    for name in names:
        state = is_active(name)
        if state == "active":
            checks.append(Check(f"service:{name}", f"Service {name}", OK, "running"))
        else:
            checks.append(Check(f"service:{name}", f"Service {name}", FAIL, f"is {state or 'unknown'}"))
    return checks


def check_ollama(http_status: Callable[[str], int], url: str) -> Check:
    try:
        code = http_status(f"{url.rstrip('/')}/api/tags")
    except Exception as exc:  # noqa: BLE001
        return Check("ollama", "Ollama (local AI)", FAIL, f"not answering ({type(exc).__name__})")
    return Check("ollama", "Ollama (local AI)", OK if code == 200 else FAIL,
                 "answering" if code == 200 else f"answered HTTP {code}")


def check_calendar(config: dict[str, Any], probe: Callable[[dict[str, Any]], tuple[str, str]]) -> Check | None:
    """iCloud login. A rejected login is a failure; being unreachable is only a warning."""
    state, message = probe(config)
    if state in ("disabled",):
        return None
    if state == "ok":
        return Check("calendar", "Calendar login", OK, message, slow="calendar")
    if state == "unreachable":
        return Check("calendar", "Calendar login", WARN, message, slow="calendar")
    return Check("calendar", "Calendar login", FAIL, message, confirm=1, slow="calendar")


def check_disk(label: str, key: str, path: str, warn: int, fail: int) -> Check | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    pct = round(usage.used * 100 / usage.total)
    free_gb = usage.free / 1e9
    status = FAIL if pct >= fail else WARN if pct >= warn else OK
    return Check(key, label, status, f"{pct}% full, {free_gb:.0f} GB free")


def check_external_mount(path: str, ismount: Callable[[str], bool] = os.path.ismount) -> Check:
    if ismount(path):
        return Check("external_mount", "External drive", OK, f"mounted at {path}")
    return Check("external_mount", "External drive", WARN, f"not mounted at {path}")


def check_temperature(read_millidegrees: Callable[[], int], warn: float, fail: float) -> Check | None:
    try:
        temp = read_millidegrees() / 1000
    except (OSError, ValueError):
        return None
    status = FAIL if temp >= fail else WARN if temp >= warn else OK
    return Check("temp", "CPU temperature", status, f"{temp:.0f} °C")


_THROTTLE_NOW = {0x1: "under-voltage", 0x2: "CPU speed capped", 0x4: "throttled", 0x8: "at its temperature limit"}


def check_throttling(run: Callable[[list[str]], str]) -> Check | None:
    try:
        match = re.search(r"0x[0-9a-fA-F]+", run(["vcgencmd", "get_throttled"]))
    except (OSError, subprocess.SubprocessError):
        return None
    if not match:
        return None
    bits = int(match.group(0), 16)
    now_flags = [text for bit, text in _THROTTLE_NOW.items() if bits & bit]
    if now_flags:
        return Check("throttle", "Power / throttling", WARN, "right now: " + ", ".join(now_flags))
    return Check("throttle", "Power / throttling", OK, "no throttling or under-voltage")


def check_time_sync(run: Callable[[list[str]], str]) -> Check | None:
    try:
        value = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if value == "yes":
        return Check("time_sync", "Clock", OK, "synchronised")
    return Check("time_sync", "Clock", WARN, "not synchronised with a time server")


def check_reboot(exists: Callable[[str], bool] = os.path.exists) -> Check:
    if exists("/var/run/reboot-required"):
        return Check("reboot", "Reboot", WARN, "a reboot is needed to finish installing updates", confirm=1)
    return Check("reboot", "Reboot", OK, "none needed")


def check_updates(run: Callable[[list[str]], str], warn_at: int) -> Check | None:
    try:
        out = run(["apt-get", "-s", "upgrade"])
    except (OSError, subprocess.SubprocessError):
        return None
    count = sum(1 for line in out.splitlines() if line.startswith("Inst "))
    status = WARN if count >= warn_at else OK
    return Check("updates", "Software updates", status, f"{count} pending", confirm=1, slow="updates")


def check_database(conn: sqlite3.Connection) -> Check:
    result = conn.execute("PRAGMA quick_check").fetchone()[0]
    if result == "ok":
        return Check("database", "Database", OK, "integrity check passed", slow="database")
    return Check("database", "Database", FAIL, f"integrity check failed: {result}", confirm=1, slow="database")


def check_backup(config: dict[str, Any], ismount: Callable[[str], bool], now: float) -> Check | None:
    """The nightly database backup should be recent (see bt_backup)."""
    settings = bt_backup.load_settings(config)
    if not settings.enabled:
        return None
    key, label = "backup", "Database backup"
    if not ismount(settings.external_path):
        return Check(key, label, WARN, "the external drive is not mounted, so no backups can be made", confirm=3)
    newest = bt_backup.latest(settings.directory)
    if newest is None:
        return Check(key, label, WARN, "no backup has been made yet", confirm=3)
    when, _path, size = newest
    age = now - when.timestamp()
    message = f"last backup {_ago(age)} ago ({size / 1e6:.1f} MB)"
    if age > 72 * 3600:
        return Check(key, label, FAIL, message, confirm=3)
    if age > 36 * 3600:
        return Check(key, label, WARN, message, confirm=3)
    return Check(key, label, OK, message)


def check_always_on(conn: sqlite3.Connection, now: float, minutes: float) -> list[Check]:
    """Devices flagged 'always on' that have been offline longer than ``minutes`` of running time.

    Measured in time the scanner was actually running (see bt_cleanup.running_cutoff), so a
    scanner restart, or the Pi being off, never makes everything look offline.
    """
    rows = conn.execute(
        "SELECT mac_address, friendly_name, advertised_name, ip_address, state, last_seen "
        "FROM devices WHERE always_on = 1 AND linked_to IS NULL ORDER BY friendly_name"
    ).fetchall()
    if not rows:
        return []
    cutoff = bt_cleanup.running_cutoff(bt_cleanup._downtime(conn, now), now, minutes * 60)
    checks = []
    for r in rows:
        name = r["friendly_name"] or r["advertised_name"] or r["mac_address"]
        key = f"offline:{r['mac_address']}"
        if r["state"] == "DETECTED" or (r["last_seen"] or 0) >= cutoff:
            checks.append(Check(key, name, OK, "online", confirm=1))
        else:
            ip = f" ({r['ip_address']})" if r["ip_address"] else ""
            checks.append(Check(key, name, FAIL,
                                f"offline for {_ago(now - (r['last_seen'] or now))}{ip}", confirm=1))
    return checks


# ---------------------------------------------------------------------------
# Alerting state machine
# ---------------------------------------------------------------------------

_ICON = {OK: "✅", WARN: "\U0001f7e1", FAIL: "\U0001f534"}


def process_results(
    conn: sqlite3.Connection, checks: list[Check], settings: Settings, now: float,
) -> list[str]:
    """Update stored state and return the alert lines to send (empty if nothing to say)."""
    bt_db.ensure_health_tables(conn)
    lines: list[str] = []
    for chk in checks:
        conn.execute(
            "INSERT INTO health_results (key, label, status, message, checked_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET label = excluded.label, status = excluded.status, "
            "message = excluded.message, checked_at = excluded.checked_at",
            (chk.key, chk.label, chk.status, chk.message, now),
        )
        row = conn.execute("SELECT * FROM health_state WHERE key = ?", (chk.key,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO health_state (key, status, since, bad_count) VALUES (?, ?, ?, 0)",
                         (chk.key, chk.status, now))
            row = conn.execute("SELECT * FROM health_state WHERE key = ?", (chk.key,)).fetchone()

        # `since` is when the current episode (ok, or a stretch of problems) began:
        # it only moves when we go between ok and not-ok, not between warn and fail.
        since = now if (row["status"] == OK) != (chk.status == OK) else row["since"]
        if chk.status == OK:
            if row["alerted_status"]:
                lines.append(f"{_ICON[OK]} {chk.label}: back to normal after {_ago(now - row['since'])}.")
            conn.execute("UPDATE health_state SET status = ?, since = ?, bad_count = 0, alerted_status = NULL "
                         "WHERE key = ?", (OK, since, chk.key))
            continue

        bad = row["bad_count"] + 1
        alerted, last_alert = row["alerted_status"], row["last_alert"]
        text = f"{_ICON[chk.status]} {chk.label}: {chk.message}"
        if bad >= chk.confirm:
            if alerted != chk.status and not (alerted == FAIL and chk.status == WARN):
                lines.append(text)
                alerted, last_alert = chk.status, now
            elif chk.status == FAIL and last_alert and now - last_alert >= settings.reminder_hours * 3600:
                lines.append(f"{text} (still failing)")
                last_alert = now
        conn.execute(
            "UPDATE health_state SET status = ?, since = ?, bad_count = ?, alerted_status = ?, last_alert = ? "
            "WHERE key = ?", (chk.status, since, bad, alerted, last_alert, chk.key))
    conn.commit()
    return lines


def format_alert(lines: list[str]) -> str:
    """HTML for Telegram. Device names and messages come from the network, so escape them."""
    return "⚠️ <b>Device Radar health</b>\n" + "\n".join(html.escape(line) for line in lines)


# ---------------------------------------------------------------------------
# Running everything
# ---------------------------------------------------------------------------

def _run_command(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return result.stdout


def _is_active(name: str) -> str:
    try:
        return subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _http_status(url: str) -> int:
    import httpx
    return httpx.get(url, timeout=5).status_code


def _read_temp() -> int:
    return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip())


@dataclass
class Environment:
    """Everything the checks touch outside the database; replaced in tests."""

    is_active: Callable[[str], str] = _is_active
    run: Callable[[list[str]], str] = _run_command
    http_status: Callable[[str], int] = _http_status
    read_temp: Callable[[], int] = _read_temp
    ismount: Callable[[str], bool] = os.path.ismount
    exists: Callable[[str], bool] = os.path.exists
    calendar_probe: Callable[[dict[str, Any]], tuple[str, str]] | None = None


def default_environment() -> Environment:
    """The real thing: includes the iCloud login probe from bt_calendar."""
    import bt_calendar
    return Environment(calendar_probe=bt_calendar.check_login)


def _due(conn: sqlite3.Connection, key: str, hours: float, now: float) -> bool:
    row = conn.execute("SELECT checked_at FROM health_results WHERE key = ?", (key,)).fetchone()
    return row is None or now - row[0] >= hours * 3600


def collect(conn: sqlite3.Connection, config: dict[str, Any], settings: Settings,
            env: Environment, now: float) -> list[Check]:
    """Run every check that is due and return the results."""
    bt_db.ensure_health_tables(conn)
    checks: list[Check] = [check_scanner(conn, now)]
    checks += check_services(settings.services, env.is_active)
    checks.append(check_ollama(env.http_status, config.get("ollama_url", "http://localhost:11434")))
    disk = check_disk("Disk (SD card)", "disk:root", "/", settings.disk_warn, settings.disk_fail)
    if disk:
        checks.append(disk)
    mount = check_external_mount(settings.external_path, env.ismount)
    checks.append(mount)
    if mount.status == OK:
        ext = check_disk("Disk (external drive)", "disk:external", settings.external_path,
                         settings.disk_warn, settings.disk_fail)
        if ext:
            checks.append(ext)
    for chk in (check_temperature(env.read_temp, settings.temp_warn, settings.temp_fail),
                check_throttling(env.run), check_time_sync(env.run), check_reboot(env.exists),
                check_backup(config, env.ismount, now)):
        if chk:
            checks.append(chk)
    checks += check_always_on(conn, now, settings.offline_minutes)

    if env.calendar_probe and _due(conn, "calendar", settings.calendar_hours, now):
        chk = check_calendar(config, env.calendar_probe)
        if chk:
            checks.append(chk)
    if _due(conn, "updates", settings.updates_hours, now):
        chk = check_updates(env.run, settings.updates_warn)
        if chk:
            checks.append(chk)
    if _due(conn, "database", settings.database_hours, now):
        checks.append(check_database(conn))
    return checks


def prune_removed_devices(conn: sqlite3.Connection) -> None:
    """Forget offline-alert rows for devices that were deleted or are no longer always-on."""
    live = {f"offline:{r[0]}" for r in conn.execute("SELECT mac_address FROM devices WHERE always_on = 1")}
    for table in ("health_results", "health_state"):
        for (key,) in conn.execute(f"SELECT key FROM {table} WHERE key LIKE 'offline:%'").fetchall():
            if key not in live:
                conn.execute(f"DELETE FROM {table} WHERE key = ?", (key,))
    conn.commit()


async def run_once(
    db_path: Path, config: dict[str, Any], send: Callable[..., Awaitable[bool]],
    env: Environment | None = None, now: float | None = None,
) -> list[str]:
    """One watchdog pass: run the checks, update state, send any alert. Returns the alert lines."""
    settings = load_settings(config)
    if not settings.enabled:
        return []
    env = env or default_environment()
    now = time.time() if now is None else now
    loop = asyncio.get_running_loop()

    def work() -> list[str]:
        conn = bt_db.get_connection(db_path)
        try:
            prune_removed_devices(conn)
            return process_results(conn, collect(conn, config, settings, env, now), settings, now)
        finally:
            conn.close()

    lines = await loop.run_in_executor(None, work)
    if lines:
        text = format_alert(lines)
        if settings.dry_run:
            logger.info("[dry run] health alert would be sent: %s", " | ".join(lines))
        elif not await send(text):
            logger.warning("Could not send the health alert to Telegram")
    return lines


async def run_loop(
    db_path: Path, load_config: Callable[[], dict[str, Any]], send: Callable[..., Awaitable[bool]],
    env: Environment | None = None,
) -> None:
    """Run the watchdog forever. Never lets an error end the loop."""
    await asyncio.sleep(45)  # let services settle after a boot before the first look
    while True:
        interval = Settings.interval_seconds
        try:
            config = load_config()
            interval = load_settings(config).interval_seconds
            await run_once(db_path, config, send, env)
        except Exception:  # noqa: BLE001
            logger.error("Health watchdog pass failed", exc_info=True)
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# "Back online" message after a long outage
# ---------------------------------------------------------------------------

RESTART_MIN_GAP = 1800  # only announce outages of 30 minutes or more


async def announce_restart(
    gap: tuple[float, float] | None, settings: Settings, send: Callable[..., Awaitable[bool]],
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, attempts: int = 8, delay: float = 30.0,
) -> bool:
    """Tell Telegram the scanner is back after being off. Retries while the network comes up.

    ``gap`` is the ``(start, end)`` recorded by ``bt_cleanup.note_scanner_start``.
    Returns True if a message was sent (or logged, in dry-run).
    """
    if not settings.enabled or not gap or gap[1] - gap[0] < RESTART_MIN_GAP:
        return False
    text = f"\U0001f501 <b>Device Radar is back online</b> after {_ago(gap[1] - gap[0])} offline."
    if settings.dry_run:
        logger.info("[dry run] would announce restart: %s", text)
        return True
    for attempt in range(attempts):
        if await send(text):
            return True
        if attempt < attempts - 1:
            await sleep(delay)
    logger.warning("Could not announce the restart to Telegram")
    return False


# ---------------------------------------------------------------------------
# Reading results (dashboard and /status)
# ---------------------------------------------------------------------------

def load_results(conn: sqlite3.Connection, now: float | None = None) -> dict[str, Any]:
    """Latest results for display: ``{"checks": [...], "stale": bool, "checked_at": ts|None, "summary": ...}``."""
    now = time.time() if now is None else now
    bt_db.ensure_health_tables(conn)
    rows = [dict(r) for r in conn.execute("SELECT key, label, status, message, checked_at FROM health_results")]
    rows.sort(key=lambda r: (-_SEVERITY[r["status"]], r["label"].lower()))
    checked = max((r["checked_at"] for r in rows), default=None)
    stale = checked is None or now - checked > 900
    problems = [r for r in rows if r["status"] != OK]
    if checked is None:
        summary = "no health checks have run yet"
    elif stale:
        summary = f"the health watchdog has not reported for {_ago(now - checked)}"
    elif problems:
        summary = f"{len(problems)} problem{'s' if len(problems) != 1 else ''}"
    else:
        summary = f"all {len(rows)} checks OK"
    return {"checks": rows, "stale": stale, "checked_at": checked, "summary": summary,
            "problems": len(problems), "worst": max((_SEVERITY[r["status"]] for r in rows), default=0)}
