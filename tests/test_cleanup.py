"""Tests for bt_cleanup. Run from the project directory:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bt_cleanup  # noqa: E402
import bt_db  # noqa: E402

HOUR = 3600
DAY = 86400
NOW = 1_800_000_000.0


class CleanupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "bt_radar.db"
        bt_db.init_db(self.db_path)
        self.conn = bt_db.get_connection(self.db_path)
        self.addCleanup(self.conn.close)
        self.settings = bt_cleanup.Settings()

    # -- helpers --

    def add(self, mac: str, *, age: float, lifespan: float = 60, state: str = "LOST",
            **columns: object) -> str:
        """Add a device last seen ``age`` seconds ago, seen for ``lifespan`` seconds."""
        last = NOW - age
        self.conn.execute(
            "INSERT INTO devices (mac_address, first_seen, last_seen, state) VALUES (?, ?, ?, ?)",
            (mac, last - lifespan, last, state),
        )
        for column, value in columns.items():
            self.conn.execute(f"UPDATE devices SET {column} = ? WHERE mac_address = ?", (value, mac))
        self.conn.commit()
        return mac

    def exists(self, mac: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM devices WHERE mac_address = ?", (mac,)).fetchone() is not None

    def hidden(self, mac: str) -> bool:
        return bool(self.conn.execute(
            "SELECT is_hidden FROM devices WHERE mac_address = ?", (mac,)).fetchone()[0])

    def run_cleanup(self, **kwargs: object) -> dict:
        kwargs.setdefault("now", NOW)
        return bt_cleanup.run_cleanup(self.conn, self.settings, **kwargs)


class RetentionTests(CleanupTestCase):
    def test_short_lived_deleted_after_three_days_not_before(self) -> None:
        old = self.add("AA:00:00:00:00:01", age=3 * DAY + HOUR)
        recent = self.add("AA:00:00:00:00:02", age=2 * DAY)
        self.run_cleanup()
        self.assertFalse(self.exists(old))
        self.assertTrue(self.exists(recent))

    def test_longer_lived_kept_until_thirty_days(self) -> None:
        week = self.add("AA:00:00:00:00:03", age=10 * DAY, lifespan=3 * DAY)
        month = self.add("AA:00:00:00:00:04", age=31 * DAY, lifespan=3 * DAY)
        self.run_cleanup()
        self.assertTrue(self.exists(week))
        self.assertFalse(self.exists(month))

    def test_hide_stage(self) -> None:
        stale = self.add("AA:00:00:00:00:05", age=3 * HOUR)
        fresh = self.add("AA:00:00:00:00:06", age=30 * 60)
        result = self.run_cleanup()
        self.assertTrue(self.hidden(stale))
        self.assertFalse(self.hidden(fresh))
        self.assertEqual(result["hidden"], 1)

    def test_hidden_flag_is_not_required_to_delete(self) -> None:
        mac = self.add("AA:00:00:00:00:07", age=5 * DAY, is_hidden=0)
        self.run_cleanup()
        self.assertFalse(self.exists(mac))


class ProtectionTests(CleanupTestCase):
    """Everything here is ancient and would otherwise be deleted."""

    OLD = 400 * DAY

    def assert_survives(self, mac: str) -> None:
        self.run_cleanup()
        self.assertTrue(self.exists(mac), f"{mac} should have been protected")
        self.assertFalse(self.hidden(mac), f"{mac} should not have been hidden")

    def test_flag_columns_protect(self) -> None:
        for i, column in enumerate(("is_watchlisted", "is_notify", "is_paired",
                                    "is_welcome", "proximity_enabled")):
            mac = self.add(f"BB:00:00:00:00:{i:02X}", age=self.OLD, **{column: 1})
            self.assert_survives(mac)

    def test_friendly_name_protects(self) -> None:
        self.assert_survives(self.add("BB:00:00:00:01:01", age=self.OLD, friendly_name="Richard's iPhone"))

    def test_advertised_name_alone_does_not_protect(self) -> None:
        mac = self.add("BB:00:00:00:01:02", age=self.OLD, advertised_name="JBL Flip")
        self.run_cleanup()
        self.assertFalse(self.exists(mac))

    def test_ip_address_protects(self) -> None:
        self.assert_survives(self.add("BB:00:00:00:01:03", age=self.OLD, ip_address="192.168.1.6"))

    def test_linked_secondary_and_primary_protect(self) -> None:
        primary = self.add("BB:00:00:00:02:01", age=self.OLD)
        secondary = self.add("BB:00:00:00:02:02", age=self.OLD, linked_to=primary)
        self.run_cleanup()
        self.assertTrue(self.exists(primary))
        self.assertTrue(self.exists(secondary))

    def test_event_history_protects(self) -> None:
        mac = self.add("BB:00:00:00:03:01", age=self.OLD)
        self.conn.execute(
            "INSERT INTO events (mac_address, event_type, timestamp) VALUES (?, 'arrived', ?)",
            (mac, NOW - self.OLD))
        self.conn.commit()
        self.assert_survives(mac)

    def test_calendar_news_voice_settings_protect(self) -> None:
        self.assert_survives(self.add("BB:00:00:00:04:01", age=self.OLD, calendar_calendars='["Work"]'))
        self.assert_survives(self.add("BB:00:00:00:04:02", age=self.OLD, news_feeds='["uk"]'))
        self.assert_survives(self.add("BB:00:00:00:04:03", age=self.OLD, alexa_voice="Amy"))

    def test_empty_json_settings_do_not_protect(self) -> None:
        mac = self.add("BB:00:00:00:04:04", age=self.OLD, calendar_calendars="[]", news_feeds="")
        self.run_cleanup()
        self.assertFalse(self.exists(mac))

    def test_dns_tracking_column_protects_when_present(self) -> None:
        # Present in some deployed databases but not defined in init_db.
        self.conn.execute("ALTER TABLE devices ADD COLUMN dns_tracking_enabled INTEGER DEFAULT 0")
        mac = self.add("BB:00:00:00:05:01", age=self.OLD, dns_tracking_enabled=1)
        self.assert_survives(mac)

    def test_detected_devices_are_never_touched(self) -> None:
        self.assert_survives(self.add("BB:00:00:00:06:01", age=self.OLD, state="DETECTED"))


class ModeTests(CleanupTestCase):
    def test_dry_run_changes_nothing_and_reports_counts(self) -> None:
        doomed = self.add("CC:00:00:00:00:01", age=10 * DAY)
        stale = self.add("CC:00:00:00:00:02", age=5 * HOUR, lifespan=2 * DAY)
        result = self.run_cleanup(dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual((result["would_delete"], result["would_hide"]), (1, 1))
        self.assertEqual((result["deleted"], result["hidden"]), (0, 0))
        self.assertTrue(self.exists(doomed) and self.exists(stale))
        self.assertFalse(self.hidden(stale))
        self.assertIsNone(result["backup"])
        self.assertFalse((Path(self.tmp.name) / "backups").exists())

    def test_disabled_skips_unless_forced(self) -> None:
        self.settings = bt_cleanup.Settings(enabled=False)
        mac = self.add("CC:00:00:00:01:01", age=10 * DAY)
        self.assertEqual(self.run_cleanup()["skipped"], "disabled")
        self.assertTrue(self.exists(mac))
        self.run_cleanup(force=True)
        self.assertFalse(self.exists(mac))

    def test_per_run_cap_then_unlimited(self) -> None:
        self.settings = bt_cleanup.Settings(max_deletes_per_run=3, batch_size=2)
        for i in range(8):
            self.add(f"CC:00:00:00:02:{i:02X}", age=10 * DAY)
        self.assertEqual(self.run_cleanup()["deleted"], 3)
        self.assertEqual(self.run_cleanup()["deleted"], 3)
        self.assertEqual(self.run_cleanup(unlimited=True)["deleted"], 2)
        self.assertEqual(preview_total(self.conn), 0)

    def test_news_read_rows_removed_with_device(self) -> None:
        mac = self.add("CC:00:00:00:03:01", age=10 * DAY)
        self.conn.execute(
            "INSERT INTO news_read (mac_address, headline_id, read_at) VALUES (?, 1, 1)", (mac,))
        self.conn.commit()
        self.run_cleanup()
        self.assertFalse(self.exists(mac))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM news_read").fetchone()[0], 0)

    def test_preview_matches_dry_run_and_changes_nothing(self) -> None:
        self.add("CC:00:00:00:04:01", age=10 * DAY)
        self.add("CC:00:00:00:04:02", age=5 * HOUR, lifespan=2 * DAY)
        self.add("CC:00:00:00:04:03", age=10 * DAY, friendly_name="Kept")
        counts = bt_cleanup.preview(self.conn, self.settings, NOW)
        self.assertEqual(counts["total"], 3)
        self.assertEqual(counts["protected"], 1)
        self.assertEqual(counts["to_delete"], 1)
        self.assertEqual(counts["to_hide"], 1)
        self.assertEqual(preview_total(self.conn), 3)


class BackupTests(CleanupTestCase):
    def backups(self) -> list[Path]:
        return sorted((Path(self.tmp.name) / "backups").glob("*.db"))

    def test_backup_taken_once_before_first_real_purge(self) -> None:
        self.add("DD:00:00:00:00:01", age=10 * DAY)
        first = self.run_cleanup()
        self.assertIsNotNone(first["backup"])
        self.assertEqual(len(self.backups()), 1)
        self.add("DD:00:00:00:00:02", age=10 * DAY)
        self.assertIsNone(self.run_cleanup()["backup"])
        self.assertEqual(len(self.backups()), 1)

    def test_backup_contains_the_deleted_rows(self) -> None:
        import sqlite3
        self.add("DD:00:00:00:01:01", age=10 * DAY)
        self.run_cleanup()
        backup = sqlite3.connect(str(self.backups()[0]))
        self.addCleanup(backup.close)
        self.assertEqual(backup.execute("SELECT COUNT(*) FROM devices").fetchone()[0], 1)

    def test_no_backup_when_nothing_to_delete(self) -> None:
        self.add("DD:00:00:00:02:01", age=60)
        self.assertIsNone(self.run_cleanup()["backup"])
        self.assertEqual(self.backups(), [])

    def test_backup_can_be_disabled(self) -> None:
        self.settings = bt_cleanup.Settings(backup_before_first_purge=False)
        self.add("DD:00:00:00:03:01", age=10 * DAY)
        self.assertIsNone(self.run_cleanup()["backup"])
        self.assertEqual(self.backups(), [])


class SettingsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        s = bt_cleanup.load_settings({})
        self.assertEqual((s.hide_after_hours, s.delete_short_lived_after_days,
                          s.delete_other_after_days), (2.0, 3.0, 30.0))
        self.assertTrue(s.enabled)
        self.assertFalse(s.dry_run)

    def test_overrides_and_bad_values(self) -> None:
        s = bt_cleanup.load_settings({
            "cleanup_enabled": False, "cleanup_dry_run": True,
            "cleanup_delete_other_after_days": 60,
            "cleanup_hide_after_hours": "banana",
            "cleanup_batch_size": -5,
            "cleanup_max_deletes_per_run": "x",
        })
        self.assertFalse(s.enabled)
        self.assertTrue(s.dry_run)
        self.assertEqual(s.delete_other_after_days, 60.0)
        self.assertEqual(s.hide_after_hours, 2.0)
        self.assertEqual(s.batch_size, 500)
        self.assertEqual(s.max_deletes_per_run, 5000)

    def test_delete_never_earlier_than_hide(self) -> None:
        s = bt_cleanup.load_settings({
            "cleanup_hide_after_hours": 72, "cleanup_delete_short_lived_after_days": 1})
        self.assertGreaterEqual(s.delete_short_lived_after_days, 3.0)

    def test_non_bool_flags_fall_back(self) -> None:
        s = bt_cleanup.load_settings({"cleanup_enabled": "no", "cleanup_dry_run": 1})
        self.assertTrue(s.enabled)
        self.assertFalse(s.dry_run)


def preview_total(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]


if __name__ == "__main__":
    unittest.main()
