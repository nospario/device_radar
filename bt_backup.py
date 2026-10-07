#!/usr/bin/env python3
"""Nightly backups of the Device Radar database (and config.json) to the external drive.

* Uses SQLite's online backup API, so the copy is consistent while the scanner
  keeps writing.
* The copy is written under a temporary name, verified (``PRAGMA integrity_check``
  and the ``devices`` table must exist), and only then renamed into place.
* Retention: the newest backup of each of the last 7 days that have one, plus the
  newest of each of the last 4 weeks. Everything else is deleted.
* Written to ``<external drive>/device-radar-backups`` only while the drive is
  mounted, one sequential write per night (suits a spinning disk).
* ``secrets`` (the ``.device-radar.env`` file with tokens and passwords) are never copied.

Runs as a loop in the Telegram bot process (next to the health watchdog) and from
the command line::

    python3 bt_backup.py            # back up now
    python3 bt_backup.py --list     # show the backups
    python3 bt_backup.py --verify   # check every backup opens and passes integrity_check

Restore: stop the services, copy a ``bt_radar-*.db`` over ``bt_radar.db`` (and delete any
``bt_radar.db-wal`` / ``-shm`` beside it), then start them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("bt_backup")

DIR_NAME = "device-radar-backups"
_DB_RE = re.compile(r"^bt_radar-(\d{8}-\d{6})\.db$")
_STAMP = "%Y%m%d-%H%M%S"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    hour: int = 3
    minute: int = 30
    keep_daily: int = 7
    keep_weekly: int = 4
    external_path: str = "/mnt/external"

    @property
    def directory(self) -> Path:
        return Path(self.external_path) / DIR_NAME


def load_settings(config: dict[str, Any]) -> Settings:
    d = Settings()

    def num(key: str, default: int, low: int, high: int) -> int:
        v = config.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) and low <= v <= high else default

    enabled = config.get("backup_enabled")
    return Settings(
        enabled=enabled if isinstance(enabled, bool) else d.enabled,
        hour=num("backup_hour", d.hour, 0, 23),
        minute=num("backup_minute", d.minute, 0, 59),
        keep_daily=num("backup_keep_daily", d.keep_daily, 1, 60),
        keep_weekly=num("backup_keep_weekly", d.keep_weekly, 0, 52),
        external_path=str(config.get("health_external_path") or d.external_path),
    )


@dataclass
class BackupResult:
    path: Path
    size: int
    config_copy: Path | None


# ---------------------------------------------------------------------------
# Making and verifying a backup
# ---------------------------------------------------------------------------

def verify_backup(path: Path) -> None:
    """Raise ``ValueError`` unless the file is a healthy Device Radar database."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise ValueError(f"integrity_check: {result}")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='devices'").fetchone() is None:
            raise ValueError("no devices table")
    except sqlite3.DatabaseError as exc:
        raise ValueError(str(exc)) from exc
    finally:
        conn.close()


def make_backup(db_path: Path, directory: Path, config_path: Path | None = None,
                now: float | None = None) -> BackupResult:
    """Back up the database (and config.json) into ``directory``. Raises on any failure."""
    stamp = time.strftime(_STAMP, time.localtime(time.time() if now is None else now))
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"bt_radar-{stamp}.db"
    partial = directory / f".bt_radar-{stamp}.db.partial"
    try:
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        dst = sqlite3.connect(str(partial))
        try:
            src.backup(dst)
            # The live database is in WAL mode, which the copy inherits. Make the backup a
            # plain single file (no -wal / -shm side files) so it is self-contained.
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
            src.close()
        verify_backup(partial)
        os.replace(partial, final)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    config_copy = None
    if config_path and config_path.exists():
        config_copy = directory / f"config-{stamp}.json"
        shutil.copy2(config_path, config_copy)
    return BackupResult(final, final.stat().st_size, config_copy)


def list_backups(directory: Path) -> list[tuple[datetime, Path]]:
    """Database backups in ``directory``, oldest first."""
    found = []
    if directory.is_dir():
        for path in directory.iterdir():
            match = _DB_RE.match(path.name)
            if match:
                found.append((datetime.strptime(match.group(1), _STAMP), path))
    return sorted(found)


def latest(directory: Path) -> tuple[datetime, Path, int] | None:
    backups = list_backups(directory)
    if not backups:
        return None
    when, path = backups[-1]
    return when, path, path.stat().st_size


def prune(directory: Path, keep_daily: int, keep_weekly: int) -> list[Path]:
    """Delete backups outside the retention rule. Returns what was deleted."""
    backups = list_backups(directory)
    newest_per_day: dict[Any, tuple[datetime, Path]] = {}
    newest_per_week: dict[Any, tuple[datetime, Path]] = {}
    for when, path in backups:                      # oldest first, so later ones overwrite
        newest_per_day[when.date()] = (when, path)
        iso = when.isocalendar()
        newest_per_week[(iso[0], iso[1])] = (when, path)
    keep: set[Path] = set()
    for day in sorted(newest_per_day)[-keep_daily:]:
        keep.add(newest_per_day[day][1])
    for week in sorted(newest_per_week)[-keep_weekly:] if keep_weekly else []:
        keep.add(newest_per_week[week][1])
    deleted = []
    for _, path in backups:
        if path not in keep:
            path.unlink(missing_ok=True)
            (directory / path.name.replace("bt_radar-", "config-").replace(".db", ".json")).unlink(missing_ok=True)
            deleted.append(path)
    return deleted


# ---------------------------------------------------------------------------
# When to run
# ---------------------------------------------------------------------------

def is_due(directory: Path, settings: Settings, now: float) -> bool:
    """True if a backup should run now: none yet, or none today and it is past the daily time."""
    if not settings.enabled:
        return False
    backups = list_backups(directory)
    if not backups:
        return True                                  # first ever backup: do it straight away
    local = time.localtime(now)
    if (local.tm_hour, local.tm_min) < (settings.hour, settings.minute):
        return False
    return backups[-1][0].date() < datetime.fromtimestamp(now).date()


def run_backup_now(db_path: Path, config_path: Path, settings: Settings,
                   ismount: Callable[[str], bool] = os.path.ismount,
                   now: float | None = None) -> BackupResult | None:
    """Back up and prune, if the external drive is mounted. Returns None if it was skipped."""
    if not ismount(settings.external_path):
        logger.warning("Backup skipped: %s is not mounted", settings.external_path)
        return None
    result = make_backup(db_path, settings.directory, config_path, now)
    deleted = prune(settings.directory, settings.keep_daily, settings.keep_weekly)
    logger.info("Backed up the database to %s (%.1f MB); removed %d old backup(s)",
                result.path.name, result.size / 1e6, len(deleted))
    return result


async def run_loop(db_path: Path, config_path: Path, load_config: Callable[[], dict[str, Any]],
                   ismount: Callable[[str], bool] = os.path.ismount) -> None:
    """Check every 10 minutes whether a backup is due. Never lets an error end the loop."""
    await asyncio.sleep(120)  # let the Pi settle after a boot
    loop = asyncio.get_running_loop()
    while True:
        try:
            settings = load_settings(load_config())
            if ismount(settings.external_path) and is_due(settings.directory, settings, time.time()):
                await loop.run_in_executor(None, run_backup_now, db_path, config_path, settings, ismount)
        except Exception:  # noqa: BLE001
            logger.error("Backup failed", exc_info=True)
        await asyncio.sleep(600)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up the Device Radar database.")
    parser.add_argument("--list", action="store_true", help="list backups")
    parser.add_argument("--verify", action="store_true", help="verify every backup")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = _load_config()
    settings = load_settings(config)
    db_path = BASE_DIR / config.get("db_path", "bt_radar.db")
    if args.list or args.verify:
        backups = list_backups(settings.directory)
        for when, path in backups:
            status = ""
            if args.verify:
                try:
                    verify_backup(path)
                    status = "  OK"
                except ValueError as exc:
                    status = f"  BAD ({exc})"
            print(f"{when:%Y-%m-%d %H:%M}  {path.stat().st_size / 1e6:6.1f} MB  {path.name}{status}")
        print(f"{len(backups)} backup(s) in {settings.directory}")
        return 0
    result = run_backup_now(db_path, CONFIG_PATH, settings)
    if result is None:
        print("Skipped: the external drive is not mounted.")
        return 1
    print(f"Backed up to {result.path} ({result.size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
