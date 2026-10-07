#!/usr/bin/env python3
"""Housekeeping for the ``devices`` table.

BLE devices rotate their random addresses roughly every 15 minutes, and every
new address becomes a new row in ``devices``. Left alone the table grows by
thousands of rows a day, almost all of them one-off records that will never
be seen again.

Cleanup runs in two stages, and never touches a device a person has shown
interest in:

1. **Hide** unprotected devices that have not been seen for a couple of hours,
   so they drop off the dashboard.
2. **Delete** unprotected devices that have not been seen for a retention
   period: short-lived records (seen for under an hour in total, i.e. a
   rotated address) after a few days, anything else after a month.

A device is *protected* (never hidden or deleted) if a person has shown
interest in it: it has a friendly name, is watchlisted / notify / paired /
welcome / proximity / DNS-tracked, is linked to (or from) another device, or
has calendar / news / Alexa settings. Devices currently ``DETECTED`` are
never touched either. A device that only has an IP address (an unnamed WiFi
device) is not hidden, because hidden devices stay hidden when they come
back and WiFi devices often go quiet for hours, but it is deleted once it
has been unseen for the retention period. Event history alone does *not*
protect a device (early versions logged events for every device); a deleted
device's events are deleted with it.

Usage (from the project directory)::

    python3 bt_cleanup.py              # show what would happen
    python3 bt_cleanup.py --run        # hide + delete now (backs up first)
    python3 bt_cleanup.py --run --vacuum
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import bt_db

logger = logging.getLogger("bt_cleanup")

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

# Recorded in the ``migrations`` table once the pre-purge backup has been taken.
_BACKUP_MARKER = "cleanup_pre_purge_backup"

# Only vacuum when a run removed at least this many rows (it rewrites the file).
_VACUUM_MIN_DELETED = 1000

# Columns that mark a device as one a person cares about. Looked up at runtime
# because not every database has every column.
_FLAG_COLUMNS = (
    "is_watchlisted", "is_notify", "is_paired", "is_welcome",
    "proximity_enabled", "dns_tracking_enabled",
)
_TEXT_COLUMNS = (
    "calendar_calendars", "news_feeds", "alexa_voice",
    "proximity_alexa_device", "proximity_prompt",
)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    """Cleanup settings, read from ``cleanup_*`` keys in config.json."""

    enabled: bool = True
    dry_run: bool = False
    hide_after_hours: float = 2.0
    delete_short_lived_after_days: float = 3.0
    delete_other_after_days: float = 30.0
    short_lived_max_minutes: float = 60.0
    max_deletes_per_run: int = 5000
    batch_size: int = 500
    backup_before_first_purge: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _number(value: Any, default: float, minimum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number >= minimum else default


def _flag(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def load_settings(config: dict[str, Any]) -> Settings:
    """Build Settings from a config dict, falling back to defaults on bad values."""
    d = Settings()

    def get(name: str) -> Any:
        return config.get(f"cleanup_{name}")

    hide_hours = _number(get("hide_after_hours"), d.hide_after_hours, 0.1)
    # Never delete something before it would have been hidden.
    min_days = hide_hours / 24
    return Settings(
        enabled=_flag(get("enabled"), d.enabled),
        dry_run=_flag(get("dry_run"), d.dry_run),
        hide_after_hours=hide_hours,
        delete_short_lived_after_days=max(
            _number(get("delete_short_lived_after_days"), d.delete_short_lived_after_days, 0.0),
            min_days),
        delete_other_after_days=max(
            _number(get("delete_other_after_days"), d.delete_other_after_days, 0.0),
            min_days),
        short_lived_max_minutes=_number(
            get("short_lived_max_minutes"), d.short_lived_max_minutes, 1.0),
        max_deletes_per_run=int(_number(
            get("max_deletes_per_run"), d.max_deletes_per_run, 1)),
        batch_size=int(_number(get("batch_size"), d.batch_size, 1)),
        backup_before_first_purge=_flag(
            get("backup_before_first_purge"), d.backup_before_first_purge),
    )


# ---------------------------------------------------------------------------
# Selection (SQL)
# ---------------------------------------------------------------------------

def _protected_sql(conn: sqlite3.Connection, *, keep_visible: bool = False) -> str:
    """SQL condition (alias ``d``) that is true for devices that must be kept.

    With ``keep_visible`` it also covers devices that should not be *hidden*
    (anything with an IP address) but may still be deleted when stale.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    parts = [
        "COALESCE(d.friendly_name, '') != ''",
        "COALESCE(d.linked_to, '') != ''",
        "EXISTS (SELECT 1 FROM devices x WHERE x.linked_to = d.mac_address)",
    ]
    parts += [f"COALESCE(d.{c}, 0) != 0" for c in _FLAG_COLUMNS if c in columns]
    parts += [f"COALESCE(d.{c}, '') NOT IN ('', '[]')" for c in _TEXT_COLUMNS if c in columns]
    if keep_visible:
        parts.append("COALESCE(d.ip_address, '') != ''")
    return "(" + " OR ".join(parts) + ")"


def _where_delete(protected: str) -> str:
    return (
        "COALESCE(d.state, '') != 'DETECTED' AND d.last_seen IS NOT NULL "
        f"AND NOT {protected} "
        "AND ((d.last_seen - COALESCE(d.first_seen, d.last_seen) < :short_max "
        "      AND d.last_seen < :short_cutoff) "
        "     OR d.last_seen < :other_cutoff)"
    )


def _where_hide(protected: str) -> str:
    return (
        "d.is_hidden = 0 AND COALESCE(d.state, '') != 'DETECTED' "
        f"AND d.last_seen < :hide_cutoff AND NOT {protected}"
    )


def _params(settings: Settings, now: float) -> dict[str, float]:
    return {
        "hide_cutoff": now - settings.hide_after_hours * 3600,
        "short_max": settings.short_lived_max_minutes * 60,
        "short_cutoff": now - settings.delete_short_lived_after_days * 86400,
        "other_cutoff": now - settings.delete_other_after_days * 86400,
    }


def _count(conn: sqlite3.Connection, where: str, params: dict[str, Any] | None = None) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM devices d WHERE {where}", params or {}).fetchone()
    return int(row[0])


def _count_hide(conn: sqlite3.Connection, params: dict[str, Any]) -> int:
    """Devices that would be hidden (excluding ones about to be deleted anyway)."""
    where = (f"{_where_hide(_protected_sql(conn, keep_visible=True))} "
             f"AND NOT ({_where_delete(_protected_sql(conn))})")
    return _count(conn, where, params)


def preview(conn: sqlite3.Connection, settings: Settings, now: float | None = None) -> dict[str, Any]:
    """Return counts describing what a cleanup run would do. Changes nothing."""
    now = time.time() if now is None else now
    protected = _protected_sql(conn)
    params = _params(settings, now)
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    return {
        "total": _count(conn, "1 = 1"),
        "hidden": _count(conn, "d.is_hidden = 1"),
        "protected": _count(conn, protected),
        "to_delete": _count(conn, _where_delete(protected), params),
        "to_hide": _count_hide(conn, params),
        "db_bytes": int(page_count * page_size),
    }


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def hide_stale(conn: sqlite3.Connection, settings: Settings, now: float) -> int:
    """Hide unprotected devices not seen recently. Returns rows changed."""
    protected = _protected_sql(conn, keep_visible=True)
    cur = conn.execute(
        "UPDATE devices SET is_hidden = 1 WHERE mac_address IN "
        f"(SELECT d.mac_address FROM devices d WHERE {_where_hide(protected)})",
        _params(settings, now),
    )
    conn.commit()
    return cur.rowcount


def purge_stale(
    conn: sqlite3.Connection, settings: Settings, now: float, limit: int,
) -> int:
    """Delete stale unprotected devices in small batches. Returns rows deleted."""
    protected = _protected_sql(conn)
    where = _where_delete(protected)
    params = _params(settings, now)
    deleted = 0
    while deleted < limit:
        size = min(settings.batch_size, limit - deleted)
        if conn.in_transaction:
            conn.commit()
        try:
            # Hold the write lock from select to delete so a device cannot be
            # watchlisted/named in between and then deleted.
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"SELECT d.mac_address FROM devices d WHERE {where} LIMIT :n",
                {**params, "n": size},
            ).fetchall()
            if not rows:
                conn.rollback()
                break
            macs = [(r[0],) for r in rows]
            conn.executemany("DELETE FROM events WHERE mac_address = ?", macs)
            conn.executemany("DELETE FROM news_read WHERE mac_address = ?", macs)
            conn.executemany("DELETE FROM devices WHERE mac_address = ?", macs)
            conn.commit()
        except sqlite3.Error as exc:
            # e.g. database busy; the next scheduled run will pick up the rest.
            conn.rollback()
            logger.warning("Cleanup batch skipped: %s", exc)
            break
        deleted += len(macs)
    return deleted


def backup_database(conn: sqlite3.Connection) -> Path:
    """Copy the database to ``backups/`` beside it using SQLite's backup API."""
    # sqlite3's backup() retries forever if this connection holds an open
    # write transaction, so flush any pending changes first.
    if conn.in_transaction:
        conn.commit()
    db_file = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    dest_dir = db_file.parent / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{db_file.stem}.pre-cleanup-{time.strftime('%Y%m%d-%H%M%S')}.db"
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()
    return dest


def _backup_done(conn: sqlite3.Connection) -> bool:
    conn.execute("CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY)")
    return conn.execute(
        "SELECT 1 FROM migrations WHERE name = ?", (_BACKUP_MARKER,)
    ).fetchone() is not None


def _vacuum(conn: sqlite3.Connection) -> bool:
    try:
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        return True
    except sqlite3.OperationalError as exc:
        logger.warning("Vacuum skipped: %s", exc)
        return False


def run_cleanup(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    force: bool = False,
    dry_run: bool | None = None,
    unlimited: bool = False,
    vacuum: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Run a cleanup pass and return a summary.

    ``force`` runs even if cleanup is disabled in config (manual runs).
    ``dry_run`` overrides the config setting. ``unlimited`` ignores the
    per-run deletion cap. ``vacuum`` compacts the file after a large purge.
    """
    result: dict[str, Any] = {
        "dry_run": False, "hidden": 0, "deleted": 0, "would_hide": 0,
        "would_delete": 0, "backup": None, "vacuumed": False,
        "skipped": None, "error": None,
    }
    if not settings.enabled and not force:
        result["skipped"] = "disabled"
        return result

    now = time.time() if now is None else now
    dry = settings.dry_run if dry_run is None else bool(dry_run)
    result["dry_run"] = dry
    if conn.in_transaction:  # callers (the scanner) may have uncommitted writes
        conn.commit()

    if dry:
        counts = preview(conn, settings, now)
        result["would_delete"] = counts["to_delete"]
        result["would_hide"] = counts["to_hide"]
        return result

    limit = 2**31 if unlimited else settings.max_deletes_per_run

    if settings.backup_before_first_purge and not _backup_done(conn):
        protected = _protected_sql(conn)
        if _count(conn, _where_delete(protected), _params(settings, now)):
            try:
                path = backup_database(conn)
            except Exception as exc:  # noqa: BLE001 - never delete without a backup
                logger.error("Pre-cleanup backup failed, not deleting: %s", exc)
                result["error"] = f"backup failed: {exc}"
                return result
            conn.execute("INSERT OR IGNORE INTO migrations (name) VALUES (?)", (_BACKUP_MARKER,))
            conn.commit()
            result["backup"] = str(path)
            logger.info("Database backed up to %s before first cleanup", path)

    result["deleted"] = purge_stale(conn, settings, now, limit)
    result["hidden"] = hide_stale(conn, settings, now)

    if vacuum and result["deleted"] >= _VACUUM_MIN_DELETED:
        result["vacuumed"] = _vacuum(conn)
    return result


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open() as f:
            return json.load(f)
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean up stale device records.")
    parser.add_argument("--run", action="store_true", help="hide and delete now (default: preview only)")
    parser.add_argument("--vacuum", action="store_true", help="compact the database after a large purge")
    args = parser.parse_args()

    config = _load_config()
    settings = load_settings(config)
    db_path = CONFIG_PATH.parent / config.get("db_path", "bt_radar.db")
    conn = bt_db.get_connection(db_path)
    try:
        counts = preview(conn, settings)
        print(f"Records stored:        {counts['total']:>8,}")
        print(f"  protected (kept):    {counts['protected']:>8,}")
        print(f"  already hidden:      {counts['hidden']:>8,}")
        print(f"  to delete:           {counts['to_delete']:>8,}")
        print(f"  to hide:             {counts['to_hide']:>8,}")
        if args.run:
            result = run_cleanup(conn, settings, force=True, dry_run=False,
                                 unlimited=True, vacuum=args.vacuum)
            print(f"\nDeleted {result['deleted']:,}, hid {result['hidden']:,}"
                  + (f", backup: {result['backup']}" if result["backup"] else "")
                  + (", compacted" if result["vacuumed"] else "")
                  + (f", ERROR: {result['error']}" if result["error"] else ""))
        else:
            print("\nPreview only. Re-run with --run to apply.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
