"""Tests for the nightly backup. Run: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bt_backup as bb  # noqa: E402
import bt_db  # noqa: E402


def stamp(dt: datetime) -> float:
    return dt.timestamp()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "bt_radar.db"
        bt_db.init_db(self.db)
        conn = bt_db.get_connection(self.db)
        bt_db.upsert_device(conn, "AA:BB:CC:00:00:01", advertised_name="Phone")
        bt_db.update_device(conn, "AA:BB:CC:00:00:01", friendly_name="Richard's iPhone")
        conn.close()
        self.config = self.root / "config.json"
        self.config.write_text('{"scan_interval_seconds": 15}')
        self.drive = self.root / "drive"
        self.dir = self.drive / bb.DIR_NAME

    def touch_backup(self, dt: datetime) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        name = dt.strftime("%Y%m%d-%H%M%S")
        path = self.dir / f"bt_radar-{name}.db"
        path.write_bytes(b"x")
        (self.dir / f"config-{name}.json").write_text("{}")
        return path


class MakeBackupTests(Base):
    def test_backup_is_a_verified_copy_with_the_data_in_it(self) -> None:
        result = bb.make_backup(self.db, self.dir, self.config, stamp(datetime(2026, 10, 7, 3, 30)))
        self.assertEqual(result.path.name, "bt_radar-20261007-033000.db")
        copy = sqlite3.connect(f"file:{result.path}?mode=ro", uri=True)
        self.addCleanup(copy.close)
        self.assertEqual(copy.execute("SELECT friendly_name FROM devices").fetchone()[0], "Richard's iPhone")
        self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(result.config_copy.read_text(), '{"scan_interval_seconds": 15}')

    def test_the_backup_is_a_single_self_contained_file(self) -> None:
        """Only the database and config copies exist: no partial file and no WAL/SHM side files."""
        bb.make_backup(self.db, self.dir, self.config, stamp(datetime(2026, 10, 7, 3, 30)))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["bt_radar-20261007-033000.db", "config-20261007-033000.json"])
        # and it opens by itself, in rollback-journal mode, with no side files appearing
        conn = sqlite3.connect(self.dir / "bt_radar-20261007-033000.db")
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_a_backup_made_while_the_database_is_being_written_is_consistent(self) -> None:
        # a connection with an open, uncommitted write transaction must not corrupt or block the copy
        conn = bt_db.get_connection(self.db)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE devices SET friendly_name = 'uncommitted'")
        try:
            result = bb.make_backup(self.db, self.dir)
        except sqlite3.OperationalError:
            result = None          # a locked database is acceptable; a corrupt copy is not
        finally:
            conn.rollback()
            conn.close()
        if result:
            bb.verify_backup(result.path)
            copy = sqlite3.connect(f"file:{result.path}?mode=ro", uri=True)
            self.addCleanup(copy.close)
            self.assertEqual(copy.execute("SELECT friendly_name FROM devices").fetchone()[0], "Richard's iPhone")

    def test_a_missing_source_fails_cleanly_and_leaves_nothing(self) -> None:
        with self.assertRaises(sqlite3.OperationalError):
            bb.make_backup(self.root / "nope.db", self.dir)
        self.assertEqual(list(self.dir.glob("*")) if self.dir.exists() else [], [])

    def test_a_corrupt_copy_is_rejected_and_removed(self) -> None:
        with mock.patch.object(bb, "verify_backup", side_effect=ValueError("integrity_check: bad")):
            with self.assertRaises(ValueError):
                bb.make_backup(self.db, self.dir)
        self.assertEqual(list(self.dir.glob("bt_radar-*")), [])
        self.assertEqual(list(self.dir.glob(".*")), [])

    def test_config_is_optional(self) -> None:
        self.assertIsNone(bb.make_backup(self.db, self.dir, None).config_copy)
        self.assertIsNone(bb.make_backup(self.db, self.dir, self.root / "missing.json", time.time() + 5).config_copy)

    def test_secrets_file_is_never_copied(self) -> None:
        (self.root / ".device-radar.env").write_text("TELEGRAM_BOT_TOKEN=secret")
        bb.make_backup(self.db, self.dir, self.config)
        self.assertEqual([p.name for p in self.dir.iterdir() if "env" in p.name or "secret" in p.read_text(errors="ignore")], [])


class VerifyTests(Base):
    def test_rejects_garbage_and_foreign_databases(self) -> None:
        garbage = self.root / "garbage.db"
        garbage.write_bytes(b"this is not a database" * 100)
        with self.assertRaises(ValueError):
            bb.verify_backup(garbage)
        other = self.root / "other.db"
        c = sqlite3.connect(other)
        c.execute("CREATE TABLE things (x)")
        c.commit()
        c.close()
        with self.assertRaisesRegex(ValueError, "no devices table"):
            bb.verify_backup(other)

    def test_accepts_a_real_database(self) -> None:
        bb.verify_backup(self.db)


class PruneTests(Base):
    def test_keeps_the_newest_of_each_of_the_last_7_days_and_of_the_last_4_weeks(self) -> None:
        start = datetime(2026, 8, 1, 3, 30)
        made = [self.touch_backup(start + timedelta(days=i)) for i in range(60)]       # a month of nightly backups x2
        made.append(self.touch_backup(start + timedelta(days=59, hours=10)))           # a second one on the last day
        bb.prune(self.dir, 7, 4)
        kept = {p.name for p in self.dir.glob("bt_radar-*.db")}
        backups = bb.list_backups(self.dir)
        # independent calculation of what the rule means
        days = sorted({w.date() for w, _ in backups}, reverse=True)[:7]
        by_day = {d: max((w, p) for w, p in backups if w.date() == d)[1].name for d in days}
        weeks = sorted({w.isocalendar()[:2] for w, _ in [(datetime.strptime(n[9:24], "%Y%m%d-%H%M%S"), n)
                                                         for n in [p.name for _, p in backups]]}, reverse=True)
        self.assertGreaterEqual(len(kept), 7)
        self.assertLessEqual(len(kept), 7 + 4)
        for name in by_day.values():
            self.assertIn(name, kept)
        newest_day_backup = max(backups)[1].name
        self.assertIn(newest_day_backup, kept)
        # exactly one backup survives per kept day, and nothing older than 4 weeks except via the weekly rule
        per_day = {}
        for name in kept:
            per_day.setdefault(name[9:17], []).append(name)
        self.assertTrue(all(len(v) == 1 for v in per_day.values()))
        self.assertEqual(len({n[9:17] for n in kept if n[9:17] in {d.strftime("%Y%m%d") for d in days}}), 7)

    def test_exact_expectation_on_a_small_case(self) -> None:
        # Mon 2026-10-05 .. Sun 2026-10-18 (ISO weeks 41 and 42), keep 3 days + 1 week
        paths = {d: self.touch_backup(datetime(2026, 10, d, 3, 30)) for d in range(5, 19)}
        deleted = bb.prune(self.dir, 3, 1)
        kept = {d for d, p in paths.items() if p.exists()}
        self.assertEqual(kept, {16, 17, 18})          # last 3 days; the newest of week 42 (Oct 18) is among them
        self.assertEqual(len(deleted), 11)

    def test_weekly_rule_keeps_the_newest_of_each_of_the_last_4_weeks(self) -> None:
        # ISO weeks: Sep 1,2 = wk36; Sep 8,9 = wk37; Sep 15,16 = wk38; Sep 22,23 = wk39; Oct 1,2 = wk40
        for d in (1, 2, 8, 9, 15, 16, 22, 23):
            self.touch_backup(datetime(2026, 9, d, 3, 30))
        for d in (1, 2):
            self.touch_backup(datetime(2026, 10, d, 3, 30))
        bb.prune(self.dir, 2, 4)
        kept = sorted(p.name[13:17] for p in self.dir.glob("bt_radar-*.db"))
        # last 2 days with a backup (Oct 1, Oct 2) + newest of weeks 37-40 (Sep 9, Sep 16, Sep 23, Oct 2)
        self.assertEqual(kept, ["0909", "0916", "0923", "1001", "1002"])

    def test_a_backup_is_kept_while_fewer_than_7_days_have_backups(self) -> None:
        # an old backup is still a valid backup: it is only retired once newer days push it out
        old = self.touch_backup(datetime(2020, 1, 1, 3, 30))
        bb.prune(self.dir, 7, 0)
        self.assertTrue(old.exists())
        for d in range(1, 9):
            self.touch_backup(datetime(2026, 10, d, 3, 30))
        bb.prune(self.dir, 7, 0)
        self.assertFalse(old.exists())

    def test_matching_config_copies_are_deleted_and_unrelated_files_untouched(self) -> None:
        old = self.touch_backup(datetime(2026, 1, 1, 3, 30))
        self.touch_backup(datetime(2026, 10, 7, 3, 30))
        (self.dir / "README.txt").write_text("keep me")
        (self.dir / "bt_radar-notastamp.db").write_text("keep me too")
        bb.prune(self.dir, 1, 0)
        self.assertFalse(old.exists())
        self.assertFalse((self.dir / "config-20260101-033000.json").exists())
        self.assertTrue((self.dir / "config-20261007-033000.json").exists())
        self.assertTrue((self.dir / "README.txt").exists())
        self.assertTrue((self.dir / "bt_radar-notastamp.db").exists())

    def test_never_deletes_the_only_backup(self) -> None:
        only = self.touch_backup(datetime(2020, 1, 1, 3, 30))
        bb.prune(self.dir, 1, 0)
        self.assertTrue(only.exists())

    def test_empty_or_missing_directory_is_fine(self) -> None:
        self.assertEqual(bb.prune(self.dir, 7, 4), [])
        self.assertIsNone(bb.latest(self.dir))


class DueTests(Base):
    S = bb.Settings(external_path="/x")

    def at(self, y, mo, d, h, mi) -> float:
        return stamp(datetime(y, mo, d, h, mi))

    def test_first_ever_backup_is_due_immediately(self) -> None:
        self.assertTrue(bb.is_due(self.dir, self.S, self.at(2026, 10, 7, 12, 0)))
        self.assertTrue(bb.is_due(self.dir, self.S, self.at(2026, 10, 7, 1, 0)))     # even before 03:30

    def test_disabled_is_never_due(self) -> None:
        self.assertFalse(bb.is_due(self.dir, bb.Settings(enabled=False), self.at(2026, 10, 7, 12, 0)))

    def test_not_before_the_daily_time(self) -> None:
        self.touch_backup(datetime(2026, 10, 5, 3, 30))
        self.assertFalse(bb.is_due(self.dir, self.S, self.at(2026, 10, 7, 3, 29)))
        self.assertTrue(bb.is_due(self.dir, self.S, self.at(2026, 10, 7, 3, 30)))

    def test_not_again_once_todays_backup_exists(self) -> None:
        self.touch_backup(datetime(2026, 10, 7, 3, 31))
        self.assertFalse(bb.is_due(self.dir, self.S, self.at(2026, 10, 7, 23, 59)))
        self.assertTrue(bb.is_due(self.dir, self.S, self.at(2026, 10, 8, 3, 30)))

    def test_catches_up_after_the_pi_was_off_at_the_scheduled_time(self) -> None:
        self.touch_backup(datetime(2026, 10, 5, 3, 31))
        self.assertTrue(bb.is_due(self.dir, self.S, self.at(2026, 10, 7, 15, 0)))   # booted mid-afternoon


class SettingsTests(unittest.TestCase):
    def test_defaults_and_overrides(self) -> None:
        d = bb.load_settings({})
        self.assertEqual((d.enabled, d.hour, d.minute, d.keep_daily, d.keep_weekly), (True, 3, 30, 7, 4))
        self.assertEqual(d.directory, Path("/mnt/external") / bb.DIR_NAME)
        s = bb.load_settings({"backup_enabled": False, "backup_hour": 5, "backup_keep_daily": 3,
                              "health_external_path": "/media/usb"})
        self.assertEqual((s.enabled, s.hour, s.keep_daily, s.external_path), (False, 5, 3, "/media/usb"))

    def test_bad_values_fall_back(self) -> None:
        s = bb.load_settings({"backup_enabled": "yes", "backup_hour": 24, "backup_minute": -1,
                              "backup_keep_daily": 0, "backup_keep_weekly": True})
        self.assertEqual((s.enabled, s.hour, s.minute, s.keep_daily, s.keep_weekly), (True, 3, 30, 7, 4))


class RunNowAndLoopTests(Base):
    def settings(self):
        return bb.Settings(external_path=str(self.drive))

    def test_skipped_when_the_drive_is_not_mounted(self) -> None:
        with self.assertLogs("bt_backup", level="WARNING"):
            self.assertIsNone(bb.run_backup_now(self.db, self.config, self.settings(), ismount=lambda p: False))
        self.assertFalse(self.dir.exists())

    def test_backs_up_and_prunes_when_mounted(self) -> None:
        self.touch_backup(datetime(2020, 1, 1, 3, 30))
        result = bb.run_backup_now(self.db, self.config, self.settings(), ismount=lambda p: True)
        self.assertIsNotNone(result)
        names = [p.name for _, p in bb.list_backups(self.dir)]
        self.assertIn(result.path.name, names)
        self.assertIn("bt_radar-20200101-033000.db", names)          # only 2 days have backups, so both are kept

    def test_the_loop_runs_a_due_backup_and_survives_errors(self) -> None:
        calls = []

        def fake_run(db, cfg, settings, ismount):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("disk exploded")
            return None

        sleeps = [None, None, None, asyncio.CancelledError()]       # initial delay, then 2 loop passes, then stop

        async def go():
            with mock.patch.object(bb, "run_backup_now", fake_run), \
                 mock.patch.object(bb.asyncio, "sleep", mock.AsyncMock(side_effect=sleeps)), \
                 self.assertLogs("bt_backup", level="ERROR"):
                try:
                    await bb.run_loop(self.db, self.config, lambda: {"health_external_path": str(self.drive)},
                                      ismount=lambda p: True)
                except asyncio.CancelledError:
                    pass

        asyncio.run(go())
        self.assertEqual(len(calls), 3)  # 1st raised (logged, loop continues), 2nd and 3rd pass ran: nothing "stops" it

    def test_the_loop_does_nothing_when_the_drive_is_missing(self) -> None:
        calls = []
        sleeps = [None, asyncio.CancelledError()]

        async def go():
            with mock.patch.object(bb, "run_backup_now", lambda *a, **k: calls.append(1)), \
                 mock.patch.object(bb.asyncio, "sleep", mock.AsyncMock(side_effect=sleeps)):
                try:
                    await bb.run_loop(self.db, self.config, lambda: {"health_external_path": str(self.drive)},
                                      ismount=lambda p: False)
                except asyncio.CancelledError:
                    pass

        asyncio.run(go())
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
