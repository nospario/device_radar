"""Tests for scanner-downtime handling in bt_cleanup. Run from the project directory:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bt_cleanup  # noqa: E402
import bt_db  # noqa: E402

HOUR = 3600
DAY = 86400
NOW = 1_800_000_000.0


class RunningCutoffTests(unittest.TestCase):
    def test_no_gaps_is_plain_subtraction(self) -> None:
        self.assertEqual(bt_cleanup.running_cutoff([], NOW, 30 * DAY), NOW - 30 * DAY)

    def test_gap_inside_the_window_extends_it(self) -> None:
        gap = (NOW - 10 * DAY, NOW - 4 * DAY)  # 6 days off
        self.assertEqual(bt_cleanup.running_cutoff([gap], NOW, 30 * DAY), NOW - 36 * DAY)

    def test_gap_older_than_the_window_changes_nothing(self) -> None:
        gap = (NOW - 90 * DAY, NOW - 80 * DAY)
        self.assertEqual(bt_cleanup.running_cutoff([gap], NOW, 30 * DAY), NOW - 30 * DAY)

    def test_gap_ending_now_is_skipped_entirely(self) -> None:
        # scanner has been down for 5 days and is just coming back
        gap = (NOW - 5 * DAY, NOW)
        self.assertEqual(bt_cleanup.running_cutoff([gap], NOW, 30 * DAY), NOW - 35 * DAY)

    def test_gap_straddling_the_window_edge(self) -> None:
        gap = (NOW - 32 * DAY, NOW - 28 * DAY)  # 4 days off, starts before the naive cutoff
        self.assertEqual(bt_cleanup.running_cutoff([gap], NOW, 30 * DAY), NOW - 34 * DAY)

    def test_several_gaps_accumulate(self) -> None:
        gaps = [(NOW - 20 * DAY, NOW - 18 * DAY), (NOW - 10 * DAY, NOW - 7 * DAY)]  # 2 + 3 days
        self.assertEqual(bt_cleanup.running_cutoff(gaps, NOW, 30 * DAY), NOW - 35 * DAY)

    def test_overlapping_gaps_are_not_double_counted(self) -> None:
        gaps = [(NOW - 12 * DAY, NOW - 8 * DAY), (NOW - 10 * DAY, NOW - 6 * DAY)]  # union = 6 days
        self.assertEqual(bt_cleanup.running_cutoff(gaps, NOW, 30 * DAY), NOW - 36 * DAY)

    def test_unsorted_input(self) -> None:
        gaps = [(NOW - 10 * DAY, NOW - 7 * DAY), (NOW - 20 * DAY, NOW - 18 * DAY)]
        self.assertEqual(bt_cleanup.running_cutoff(gaps, NOW, 30 * DAY), NOW - 35 * DAY)


class DowntimeTrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "bt_radar.db"
        bt_db.init_db(self.db)
        self.conn = bt_db.get_connection(self.db)
        self.addCleanup(self.conn.close)

    def gaps(self) -> list[tuple[float, float]]:
        return [(r[0], r[1]) for r in self.conn.execute("SELECT start, end FROM scanner_gaps")]

    def test_first_start_records_no_gap(self) -> None:
        self.assertIsNone(bt_cleanup.note_scanner_start(self.conn, NOW))
        self.assertEqual(self.gaps(), [])

    def test_start_after_long_downtime_records_the_gap(self) -> None:
        bt_cleanup.heartbeat(self.conn, NOW - 30 * DAY)
        gap = bt_cleanup.note_scanner_start(self.conn, NOW)
        self.assertEqual(gap, (NOW - 30 * DAY, NOW))
        self.assertEqual(self.gaps(), [(NOW - 30 * DAY, NOW)])

    def test_quick_restart_is_not_a_gap(self) -> None:
        bt_cleanup.heartbeat(self.conn, NOW - 45)
        self.assertIsNone(bt_cleanup.note_scanner_start(self.conn, NOW))
        self.assertEqual(self.gaps(), [])

    def test_heartbeat_moves_forward(self) -> None:
        bt_cleanup.heartbeat(self.conn, NOW - 100)
        bt_cleanup.heartbeat(self.conn, NOW)
        self.assertEqual(bt_cleanup._last_heartbeat(self.conn), NOW)

    def test_current_outage_counts_even_before_the_scanner_restarts(self) -> None:
        # e.g. someone runs "Clean up now" from the web while the scanner is down
        bt_cleanup.heartbeat(self.conn, NOW - 5 * DAY)
        self.assertIn((NOW - 5 * DAY, NOW), bt_cleanup._downtime(self.conn, NOW))

    def test_running_scanner_has_no_current_outage(self) -> None:
        bt_cleanup.heartbeat(self.conn, NOW - 20)
        self.assertEqual(bt_cleanup._downtime(self.conn, NOW), [])

    def test_backfill_finds_multi_day_event_gaps_once(self) -> None:
        self.conn.execute("INSERT INTO devices (mac_address, first_seen, last_seen) VALUES ('AA', 1, 1)")
        for t in (NOW - 40 * DAY, NOW - 39.9 * DAY, NOW - 5 * DAY, NOW - 4.9 * DAY, NOW - 4.8 * DAY):
            self.conn.execute("INSERT INTO events (mac_address, event_type, timestamp) VALUES ('AA','arrived',?)", (t,))
        self.conn.commit()
        self.assertEqual(bt_cleanup.backfill_gaps(self.conn), 1)
        self.assertEqual(self.gaps(), [(NOW - 39.9 * DAY, NOW - 5 * DAY)])
        self.assertEqual(bt_cleanup.backfill_gaps(self.conn), 0)  # only once
        self.assertEqual(len(self.gaps()), 1)

    def test_backfill_ignores_overnight_quiet(self) -> None:
        self.conn.execute("INSERT INTO devices (mac_address, first_seen, last_seen) VALUES ('AA', 1, 1)")
        for t in (NOW - 2 * DAY, NOW - 2 * DAY + 10 * HOUR, NOW - 1 * DAY):
            self.conn.execute("INSERT INTO events (mac_address, event_type, timestamp) VALUES ('AA','arrived',?)", (t,))
        self.conn.commit()
        self.assertEqual(bt_cleanup.backfill_gaps(self.conn), 0)


class CleanupAfterDowntimeTests(unittest.TestCase):
    """The original bug: a month with the Pi switched off made everything look stale."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "bt_radar.db"
        bt_db.init_db(self.db)
        self.conn = bt_db.get_connection(self.db)
        self.addCleanup(self.conn.close)
        self.settings = bt_cleanup.Settings()

    def add(self, mac: str, *, age: float, lifespan: float = 20 * DAY, **cols: object) -> str:
        last = NOW - age
        self.conn.execute(
            "INSERT INTO devices (mac_address, first_seen, last_seen, state, scan_type, ip_address) "
            "VALUES (?, ?, ?, 'LOST', 'WiFi', ?)",
            (mac, last - lifespan, last, "192.168.1.9"),
        )
        for c, v in cols.items():
            self.conn.execute(f"UPDATE devices SET {c} = ? WHERE mac_address = ?", (v, mac))
        self.conn.commit()
        return mac

    def exists(self, mac: str) -> bool:
        return self.conn.execute("SELECT 1 FROM devices WHERE mac_address = ?", (mac,)).fetchone() is not None

    def run_cleanup(self) -> dict:
        return bt_cleanup.run_cleanup(self.conn, self.settings, now=NOW)

    def test_month_of_downtime_does_not_delete_devices_seen_before_it(self) -> None:
        # last seen 31 days ago (2 days before the Pi went off for 29 days):
        # without downtime handling this is "unseen for 31 days" and deleted
        mac = self.add("AA:00:00:00:00:01", age=31 * DAY)
        self.conn.execute("INSERT INTO scanner_gaps (start, end) VALUES (?, ?)", (NOW - 29 * DAY, NOW))
        self.conn.commit()
        self.run_cleanup()
        self.assertTrue(self.exists(mac))

    def test_same_device_is_deleted_when_there_was_no_downtime(self) -> None:
        mac = self.add("AA:00:00:00:00:02", age=31 * DAY)
        self.run_cleanup()
        self.assertFalse(self.exists(mac))

    def test_device_unseen_for_30_running_days_is_still_deleted_after_downtime(self) -> None:
        # off for 20 days, and the device had already been unseen for 20 running days before that
        mac = self.add("AA:00:00:00:00:03", age=45 * DAY)
        self.conn.execute("INSERT INTO scanner_gaps (start, end) VALUES (?, ?)", (NOW - 25 * DAY, NOW - 5 * DAY))
        self.conn.commit()
        self.run_cleanup()  # unseen 45 days, 20 of them off => 25 running days: kept
        self.assertTrue(self.exists(mac))
        late = self.add("AA:00:00:00:00:04", age=55 * DAY)  # 35 running days: deleted
        self.run_cleanup()
        self.assertFalse(self.exists(late))

    def test_hide_stage_also_skips_downtime(self) -> None:
        mac = self.add("AA:00:00:00:00:05", age=3 * DAY, ip_address=None)
        self.conn.execute("UPDATE devices SET ip_address = NULL WHERE mac_address = ?", (mac,))
        self.conn.execute("INSERT INTO scanner_gaps (start, end) VALUES (?, ?)", (NOW - 3 * DAY, NOW - 600))
        self.conn.commit()
        self.run_cleanup()  # only ~10 minutes of running time since it was last seen
        self.assertFalse(bool(self.conn.execute(
            "SELECT is_hidden FROM devices WHERE mac_address = ?", (mac,)).fetchone()[0]))

    def test_preview_matches_what_run_would_do_during_current_outage(self) -> None:
        mac = self.add("AA:00:00:00:00:06", age=40 * DAY)
        bt_cleanup.heartbeat(self.conn, NOW - 20 * DAY)   # scanner went quiet 20 days ago
        counts = bt_cleanup.preview(self.conn, self.settings, NOW)
        self.assertEqual(counts["to_delete"], 0)           # 40 days - 20 off = 20 running days
        self.run_cleanup()
        self.assertTrue(self.exists(mac))


if __name__ == "__main__":
    unittest.main()
