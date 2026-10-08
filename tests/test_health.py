"""Tests for the health watchdog. Run: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import time
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bt_backup  # noqa: E402
import bt_cleanup  # noqa: E402
import bt_db  # noqa: E402
import bt_health as bh  # noqa: E402

NOW = 1_800_000_000.0
HOUR, DAY = 3600, 86400
Usage = namedtuple("Usage", "total used free")


def env(**kw) -> bh.Environment:
    def run(cmd):
        return {"vcgencmd": "throttled=0x0\n", "timedatectl": "yes\n", "apt-get": "Inst a\nInst b\nConf a\n"}[cmd[0]]
    defaults = dict(is_active=lambda n: "active", run=run, http_status=lambda url: 200,
                    read_temp=lambda: 45000, ismount=lambda p: True, exists=lambda p: False,
                    calendar_probe=lambda c: ("ok", "login works"),
                    sync_status=lambda: bh.SyncStatus(NOW - 20, NOW - 20, "Fully synced"),       # never read the real log in tests
                    restart_service=lambda name: True)
    defaults.update(kw)
    return bh.Environment(**defaults)


class Db(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "bt_radar.db"
        bt_db.init_db(self.db)
        self.conn = bt_db.get_connection(self.db)
        self.addCleanup(self.conn.close)
        # a temporary "external drive": tests must never look at the real /mnt/external
        self.external = Path(self.tmp.name) / "external"
        self.config = {"health_external_path": str(self.external), "health_check_web_password": False,
                       "health_check_sync": False}                # tests that exercise the sync check switch it on

    def fresh_backup(self, now=NOW):
        bt_backup.make_backup(self.db, self.external / bt_backup.DIR_NAME, None, now - 3600)

    def device(self, mac, name, *, state="DETECTED", last_seen=NOW - 30, always_on=1, ip="192.168.1.9", **cols):
        bt_db.upsert_device(self.conn, mac, advertised_name=name, scan_type="WiFi", state=state, ip_address=ip)
        self.conn.execute("UPDATE devices SET friendly_name=?, state=?, last_seen=?, always_on=? WHERE mac_address=?",
                          (name, state, last_seen, always_on, mac))
        for c, v in cols.items():
            self.conn.execute(f"UPDATE devices SET {c}=? WHERE mac_address=?", (v, mac))
        self.conn.commit()

    def heartbeat(self, ts):
        bt_cleanup.heartbeat(self.conn, ts)


class SettingsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        s = bh.load_settings({})
        self.assertEqual((s.enabled, s.dry_run, s.interval_seconds, s.offline_minutes), (True, False, 300, 20.0))
        self.assertIn("bt-scanner", s.services)

    def test_overrides_and_bad_values(self) -> None:
        s = bh.load_settings({"health_alerts_enabled": False, "health_interval_seconds": 60,
                              "health_services": ["a", "b"], "health_disk_warn_percent": 70})
        self.assertEqual((s.enabled, s.interval_seconds, s.services, s.disk_warn), (False, 60, ("a", "b"), 70))
        bad = bh.load_settings({"health_alerts_enabled": "no", "health_interval_seconds": 5,
                                "health_services": ["ok", 3], "health_temp_warn_c": -1, "health_offline_minutes": True})
        d = bh.Settings()
        self.assertEqual((bad.enabled, bad.interval_seconds, bad.services, bad.temp_warn, bad.offline_minutes),
                         (d.enabled, d.interval_seconds, d.services, d.temp_warn, d.offline_minutes))


class SimpleCheckTests(Db):
    def test_scanner_heartbeat(self) -> None:
        self.assertEqual(bh.check_scanner(self.conn, NOW).status, bh.WARN)           # none recorded yet
        self.heartbeat(NOW - 20)
        self.assertEqual(bh.check_scanner(self.conn, NOW).status, bh.OK)
        self.heartbeat(NOW - 300)
        self.assertEqual(bh.check_scanner(self.conn, NOW).status, bh.WARN)
        self.heartbeat(NOW - 700)
        c = bh.check_scanner(self.conn, NOW)
        self.assertEqual(c.status, bh.FAIL)
        self.assertIn("12 min", c.message)

    def test_services(self) -> None:
        states = {"a": "active", "b": "failed", "c": "inactive", "d": ""}
        out = {c.key: c for c in bh.check_services(("a", "b", "c", "d"), states.get)}
        self.assertEqual(out["service:a"].status, bh.OK)
        self.assertEqual((out["service:b"].status, out["service:b"].message), (bh.FAIL, "is failed"))
        self.assertEqual(out["service:c"].message, "is inactive")
        self.assertEqual(out["service:d"].message, "is unknown")

    def test_ollama(self) -> None:
        self.assertEqual(bh.check_ollama(lambda u: 200, "http://x:1/").status, bh.OK)
        self.assertEqual(bh.check_ollama(lambda u: 503, "http://x:1").status, bh.FAIL)
        seen = []

        def boom(url):
            seen.append(url)
            raise ConnectionError("refused")
        c = bh.check_ollama(boom, "http://x:1/")
        self.assertEqual((c.status, seen), (bh.FAIL, ["http://x:1/api/tags"]))
        self.assertIn("ConnectionError", c.message)

    def test_calendar(self) -> None:
        cfg = {}
        self.assertEqual(bh.check_calendar(cfg, lambda c: ("ok", "login works")).status, bh.OK)
        self.assertIsNone(bh.check_calendar(cfg, lambda c: ("disabled", "off")))
        auth = bh.check_calendar(cfg, lambda c: ("auth", "rejected"))
        self.assertEqual((auth.status, auth.confirm), (bh.FAIL, 1))            # a revoked password is reported at once
        self.assertEqual(bh.check_calendar(cfg, lambda c: ("unreachable", "x")).status, bh.WARN)
        self.assertEqual(bh.check_calendar(cfg, lambda c: ("no_credentials", "x")).status, bh.FAIL)

    def test_disk_thresholds(self) -> None:
        for used, status in ((50, bh.OK), (84, bh.OK), (85, bh.WARN), (94, bh.WARN), (95, bh.FAIL)):
            with mock.patch.object(bh.shutil, "disk_usage", return_value=Usage(100e9, used * 1e9, (100 - used) * 1e9)):
                c = bh.check_disk("Disk", "disk:root", "/", 85, 95)
            self.assertEqual(c.status, status, used)
        with mock.patch.object(bh.shutil, "disk_usage", side_effect=OSError):
            self.assertIsNone(bh.check_disk("Disk", "disk:root", "/", 85, 95))

    def test_external_mount(self) -> None:
        self.assertEqual(bh.check_external_mount("/mnt/x", lambda p: True).status, bh.OK)
        c = bh.check_external_mount("/mnt/x", lambda p: False)
        self.assertEqual((c.status, c.message), (bh.WARN, "not mounted at /mnt/x"))

    def test_temperature(self) -> None:
        for milli, status in ((45000, bh.OK), (79900, bh.OK), (80000, bh.WARN), (85000, bh.FAIL)):
            self.assertEqual(bh.check_temperature(lambda m=milli: m, 80, 85).status, status, milli)
        self.assertIsNone(bh.check_temperature(lambda: (_ for _ in ()).throw(OSError()), 80, 85))

    def test_throttling_reports_only_what_is_happening_now(self) -> None:
        past_only = bh.check_throttling(lambda cmd: "throttled=0x50000")      # happened since boot, not now
        self.assertEqual(past_only.status, bh.OK)
        now = bh.check_throttling(lambda cmd: "throttled=0x5")                # under-voltage + throttled now
        self.assertEqual(now.status, bh.WARN)
        self.assertIn("under-voltage", now.message)
        self.assertIn("throttled", now.message)
        self.assertIsNone(bh.check_throttling(lambda cmd: "garbage"))
        self.assertIsNone(bh.check_throttling(lambda cmd: (_ for _ in ()).throw(OSError())))

    def test_time_sync_and_reboot(self) -> None:
        self.assertEqual(bh.check_time_sync(lambda cmd: "yes\n").status, bh.OK)
        self.assertEqual(bh.check_time_sync(lambda cmd: "no\n").status, bh.WARN)
        self.assertEqual(bh.check_reboot(lambda p: False).status, bh.OK)
        c = bh.check_reboot(lambda p: True)
        self.assertEqual((c.status, c.confirm), (bh.WARN, 1))

    def test_updates(self) -> None:
        lines = "".join(f"Inst pkg{i}\n" for i in range(60)) + "Conf pkg0\nRemv old\n"
        c = bh.check_updates(lambda cmd: lines, 50)
        self.assertEqual((c.status, c.message), (bh.WARN, "60 pending"))
        self.assertEqual(bh.check_updates(lambda cmd: "Inst a\n", 50).status, bh.OK)
        self.assertIsNone(bh.check_updates(lambda cmd: (_ for _ in ()).throw(subprocess.TimeoutExpired("apt", 1)), 50))

    def test_database(self) -> None:
        self.assertEqual(bh.check_database(self.conn).status, bh.OK)

        class Broken:
            def execute(self, sql):
                return mock.Mock(fetchone=lambda: ("*** in database main ***",))
        c = bh.check_database(Broken())
        self.assertEqual((c.status, c.confirm), (bh.FAIL, 1))


class BackupCheckTests(Db):
    def check(self, now=NOW, mounted=True, config=None):
        return bh.check_backup({**self.config, **(config or {})}, lambda p: mounted, now)

    def test_disabled_means_no_check(self) -> None:
        self.assertIsNone(self.check(config={"backup_enabled": False}))

    def test_drive_not_mounted(self) -> None:
        c = self.check(mounted=False)
        self.assertEqual((c.status, c.confirm), (bh.WARN, 3))
        self.assertIn("not mounted", c.message)

    def test_no_backup_yet(self) -> None:
        self.assertEqual(self.check().message, "no backup has been made yet")

    def test_age_thresholds(self) -> None:
        self.fresh_backup(NOW)                      # made one hour before NOW
        c = self.check(NOW)
        self.assertEqual(c.status, bh.OK)
        self.assertIn("60 min ago", c.message)
        self.assertEqual(self.check(NOW + 30 * HOUR).status, bh.OK)       # 31 h old
        self.assertEqual(self.check(NOW + 40 * HOUR).status, bh.WARN)     # 41 h old
        self.assertEqual(self.check(NOW + 80 * HOUR).status, bh.FAIL)     # 81 h old


class WebPasswordCheckTests(Db):
    def test_warns_until_a_password_is_set(self) -> None:
        import bt_auth
        auth = Path(self.tmp.name) / "web_auth.json"
        c = bh.check_web_password({}, auth)
        self.assertEqual((c.status, c.key), (bh.WARN, "web_password"))
        self.assertIn("bt_auth.py set-password", c.message)
        bt_auth.set_password(auth, "correct horse battery")
        c = bh.check_web_password({}, auth)
        self.assertEqual(c.status, bh.OK)
        self.assertNotIn("correct horse", c.message)

    def test_can_be_switched_off(self) -> None:
        self.assertIsNone(bh.check_web_password({"health_check_web_password": False}, Path(self.tmp.name) / "none.json"))

    def test_a_corrupt_auth_file_counts_as_no_password(self) -> None:
        auth = Path(self.tmp.name) / "web_auth.json"
        auth.write_text("not json")
        self.assertEqual(bh.check_web_password({}, auth).status, bh.WARN)

    def test_it_is_part_of_a_normal_pass(self) -> None:
        auth = Path(self.tmp.name) / "web_auth.json"
        self.heartbeat(NOW - 10)
        self.fresh_backup()
        conn_cfg = {"health_external_path": str(self.external)}
        environment = env(); environment.auth_file = auth
        asyncio.run(bh.run_once(self.db, conn_cfg, mock.AsyncMock(return_value=True), environment, NOW))
        row = self.conn.execute("SELECT status FROM health_results WHERE key = 'web_password'").fetchone()
        self.assertEqual(row[0], "warn")


class SyncLogTests(unittest.TestCase):
    def write(self, text: str) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name) / "abc"
        folder.mkdir()
        (folder / "sync.log").write_text(text)
        return str(Path(tmp.name) / "*" / "sync.log")

    def test_reads_the_newest_fully_synced_and_the_newest_line(self) -> None:
        pattern = self.write("[2026-10-08T11:14:55.666Z] Fully synced\n[2026-10-08T11:15:25.666Z] Fully synced\n"
                             "                                        [2026-10-08T11:17:18.611Z] Starting sync:\n"
                             "[2026-10-08T11:17:18.881Z] Connecting...\n")
        status = bh.read_sync_status(pattern)
        self.assertEqual(status.last_synced, bh.datetime.fromisoformat("2026-10-08T11:15:25.666+00:00").timestamp())
        self.assertEqual(status.last_line, bh.datetime.fromisoformat("2026-10-08T11:17:18.881+00:00").timestamp())
        self.assertEqual(status.last_text, "Connecting...")

    def test_a_healthy_log_ends_on_fully_synced(self) -> None:
        status = bh.read_sync_status(self.write("[2026-10-08T11:15:25.666Z] Uploading file x\n[2026-10-08T11:15:55.666Z] Fully synced\n"))
        self.assertEqual(status.last_synced, status.last_line)

    def test_never_synced_no_log_and_junk(self) -> None:
        self.assertIsNone(bh.read_sync_status(self.write("[2026-10-08T11:17:18.881Z] Connecting...\n")).last_synced)
        self.assertIsNone(bh.read_sync_status("/nonexistent/*/sync.log"))
        status = bh.read_sync_status(self.write("garbage\n[not a time] Fully synced\n[2026-13-45T99:99:99Z] x\n"))
        self.assertIsNone(status.last_line)

    def test_only_the_tail_of_a_huge_log_is_read(self) -> None:
        old = "[2026-10-01T00:00:00.000Z] Fully synced\n" * 20000
        pattern = self.write(old + "[2026-10-08T11:15:25.666Z] Fully synced\n")
        status = bh.read_sync_status(pattern, tail_bytes=4096)
        self.assertEqual(status.last_synced, bh.datetime.fromisoformat("2026-10-08T11:15:25.666+00:00").timestamp())


class ObsidianSyncCheckTests(Db):
    def check(self, status, *, config=None, active="active", restart=None, now=NOW, **setting_kw):
        calls = restart if restart is not None else []
        environment = env(is_active=lambda n: active, sync_status=lambda: status,
                          restart_service=lambda name: (calls.append(name), True)[1])
        settings = bh.load_settings({**(config or {}), **setting_kw})
        return bh.check_obsidian_sync(self.conn, {**(config or {})}, settings, environment, now), calls

    def test_a_recent_fully_synced_is_ok_and_never_restarts(self) -> None:
        chk, calls = self.check(bh.SyncStatus(NOW - 40, NOW - 40, "Fully synced"))
        self.assertEqual((chk.status, calls), (bh.OK, []))
        self.assertIn("in sync", chk.message)

    def test_a_clock_slightly_behind_is_not_a_problem(self) -> None:
        chk, calls = self.check(bh.SyncStatus(NOW + 30, NOW + 30, "Fully synced"))
        self.assertEqual((chk.status, calls), (bh.OK, []))

    def test_busy_uploading_is_not_a_problem(self) -> None:
        chk, calls = self.check(bh.SyncStatus(NOW - 30 * 60, NOW - 20, "Uploading file big.png"))
        self.assertEqual((chk.status, chk.message, calls), (bh.OK, "busy syncing", []))

    def test_quiet_for_too_long_restarts_the_service_once_and_says_so(self) -> None:
        stuck = bh.SyncStatus(NOW - 20 * 60, NOW - 3 * 60, "Connecting...")
        chk, calls = self.check(stuck)
        self.assertEqual(calls, ["obsidian-sync"])
        self.assertEqual(chk.status, bh.WARN)
        self.assertIn("20 min", chk.message)
        self.assertIn("Connecting...", chk.message)
        self.assertIn("restarted the service", chk.message)
        self.assertEqual(chk.confirm, 1)                               # already time-based: no second look needed

    def test_not_restarted_again_during_the_cooldown(self) -> None:
        stuck = bh.SyncStatus(NOW - 20 * 60, NOW - 10 * 60, "Connecting...")
        self.check(stuck)
        chk, calls = self.check(stuck, now=NOW + 10 * 60)
        self.assertEqual(calls, [])
        self.assertEqual(chk.status, bh.WARN)
        self.assertIn("giving it time", chk.message)

    def test_restarts_again_after_the_cooldown_but_gives_up_after_the_limit(self) -> None:
        stuck = lambda t: bh.SyncStatus(t - 60 * 60, t - 20 * 60, "Connecting...")
        total = []
        for i in range(3):
            now = NOW + i * 31 * 60
            chk, calls = self.check(stuck(now), now=now)
            total += calls
        self.assertEqual(len(total), 3)
        now = NOW + 3 * 31 * 60
        chk, calls = self.check(stuck(now), now=now)
        self.assertEqual((calls, chk.status), ([], bh.FAIL))
        self.assertIn("restarted 3 times", chk.message)

    def test_recovery_resets_the_restart_count(self) -> None:
        stuck = bh.SyncStatus(NOW - 20 * 60, NOW - 10 * 60, "Connecting...")
        self.check(stuck)
        later = NOW + 2 * HOUR
        chk, _ = self.check(bh.SyncStatus(later - 10, later - 10, "Fully synced"), now=later)
        self.assertEqual(chk.status, bh.OK)
        self.assertEqual(bh._state_get(self.conn, "sync_restarts"), 0)
        chk, calls = self.check(bh.SyncStatus(later + 3 * HOUR - 20 * 60, later + 3 * HOUR - 10 * 60, "x"), now=later + 3 * HOUR)
        self.assertEqual(calls, ["obsidian-sync"])                      # a fresh problem gets a fresh restart

    def test_auto_restart_can_be_turned_off(self) -> None:
        chk, calls = self.check(bh.SyncStatus(NOW - 20 * 60, NOW - 10 * 60, "x"), config={"health_sync_auto_restart": False})
        self.assertEqual((chk.status, calls), (bh.FAIL, []))
        self.assertIn("automatic restart is off", chk.message)

    def test_dry_run_never_restarts(self) -> None:
        chk, calls = self.check(bh.SyncStatus(NOW - 20 * 60, NOW - 10 * 60, "x"), config={"health_alerts_dry_run": True})
        self.assertEqual(calls, [])
        self.assertIn("dry run", chk.message)

    def test_a_failed_restart_is_reported_as_a_failure(self) -> None:
        environment = env(sync_status=lambda: bh.SyncStatus(NOW - 20 * 60, NOW - 10 * 60, "x"), restart_service=lambda n: False)
        chk = bh.check_obsidian_sync(self.conn, {}, bh.load_settings({}), environment, NOW)
        self.assertEqual(chk.status, bh.FAIL)
        self.assertIn("failed", chk.message)

    def test_a_stopped_service_is_left_to_the_service_check(self) -> None:
        chk, calls = self.check(bh.SyncStatus(NOW - 3600, NOW - 3600, "x"), active="failed")
        self.assertEqual((chk, calls), (None, []))

    def test_can_be_switched_off_and_a_missing_log_only_warns(self) -> None:
        self.assertIsNone(self.check(bh.SyncStatus(NOW - 3600, NOW - 3600, "x"), config={"health_check_sync": False})[0])
        chk, calls = self.check(None)
        self.assertEqual((chk.status, calls), (bh.WARN, []))

    def test_never_synced_since_start_counts_as_quiet(self) -> None:
        chk, calls = self.check(bh.SyncStatus(None, NOW - 10 * 60, "Connecting..."))
        self.assertEqual(calls, ["obsidian-sync"])
        self.assertIn("never reported", chk.message)

    def test_settings_are_read_and_bad_values_ignored(self) -> None:
        s = bh.load_settings({"health_sync_stale_minutes": 20, "health_sync_max_restarts": 5, "health_sync_auto_restart": False})
        self.assertEqual((s.sync_stale_minutes, s.sync_max_restarts, s.sync_auto_restart), (20.0, 5, False))
        d = bh.load_settings({"health_sync_stale_minutes": "x", "health_sync_max_restarts": -2, "health_sync_auto_restart": "yes"})
        self.assertEqual((d.sync_stale_minutes, d.sync_max_restarts, d.sync_auto_restart), (10.0, 3, True))


class AlwaysOnTests(Db):
    def test_online_and_recently_seen_devices_are_ok(self) -> None:
        self.device("AA:00:00:00:00:01", "Doorbell")
        self.device("AA:00:00:00:00:02", "Camera", state="LOST", last_seen=NOW - 5 * 60)   # lost 5 min: within 20
        self.heartbeat(NOW - 10)
        out = {c.label: c for c in bh.check_always_on(self.conn, NOW, 20)}
        self.assertEqual((out["Doorbell"].status, out["Camera"].status), (bh.OK, bh.OK))

    def test_offline_too_long_fails_with_age_and_ip(self) -> None:
        self.device("AA:00:00:00:00:03", "Hive Hub", state="LOST", last_seen=NOW - 3 * HOUR, ip="192.168.1.168")
        self.heartbeat(NOW - 10)
        c = bh.check_always_on(self.conn, NOW, 20)[0]
        self.assertEqual((c.status, c.confirm, c.key), (bh.FAIL, 1, "offline:AA:00:00:00:00:03"))
        self.assertEqual(c.message, "offline for 3 h (192.168.1.168)")

    def test_only_always_on_unlinked_devices_are_checked(self) -> None:
        self.device("AA:00:00:00:00:04", "Plain", always_on=0, state="LOST", last_seen=NOW - DAY)
        self.device("AA:00:00:00:00:05", "Linked", state="LOST", last_seen=NOW - DAY, linked_to="AA:00:00:00:00:04")
        self.heartbeat(NOW - 10)
        self.assertEqual(bh.check_always_on(self.conn, NOW, 20), [])

    def test_time_the_scanner_was_off_does_not_count(self) -> None:
        """The scanner restarted 2 minutes ago after a long outage: nothing can be called offline yet."""
        self.device("AA:00:00:00:00:06", "Doorbell", state="LOST", last_seen=NOW - 3 * HOUR)
        self.conn.execute("INSERT INTO scanner_gaps (start, end) VALUES (?, ?)", (NOW - 3 * HOUR, NOW - 120))
        self.conn.commit()
        self.heartbeat(NOW - 5)
        self.assertEqual(bh.check_always_on(self.conn, NOW, 20)[0].status, bh.OK)
        # ...but it is offline once 20 minutes of running time have passed since the restart
        later = NOW + 25 * 60
        self.heartbeat(later - 5)
        self.assertEqual(bh.check_always_on(self.conn, later, 20)[0].status, bh.FAIL)

    def test_a_dead_scanner_does_not_make_every_device_look_offline(self) -> None:
        self.device("AA:00:00:00:00:07", "Doorbell", state="LOST", last_seen=NOW - 2 * HOUR)
        self.heartbeat(NOW - 2 * HOUR)            # scanner went quiet at the same moment
        self.assertEqual(bh.check_always_on(self.conn, NOW, 20)[0].status, bh.OK)


class StateMachineTests(Db):
    def run_checks(self, checks, now, **settings):
        return bh.process_results(self.conn, checks, bh.Settings(**settings), now)

    def bad(self, status=bh.FAIL, confirm=2, key="x", label="Thing", msg="broken"):
        return bh.Check(key, label, status, msg, confirm=confirm)

    def good(self, key="x", label="Thing"):
        return bh.Check(key, label, bh.OK, "fine")

    def test_confirmation_then_one_alert_then_recovery(self) -> None:
        self.assertEqual(self.run_checks([self.bad()], NOW), [])                           # 1st bad: wait
        first = self.run_checks([self.bad()], NOW + 300)                                   # 2nd bad: alert
        self.assertEqual(first, [f"{bh._ICON[bh.FAIL]} Thing: broken"])
        self.assertEqual(self.run_checks([self.bad()], NOW + 600), [])                     # no repeat
        self.assertEqual(self.run_checks([self.bad()], NOW + 900), [])
        rec = self.run_checks([self.good()], NOW + 3 * HOUR)
        self.assertEqual(rec, [f"{bh._ICON[bh.OK]} Thing: back to normal after 3 h."])
        self.assertEqual(self.run_checks([self.good()], NOW + 3 * HOUR + 300), [])         # recovery said once

    def test_a_blip_that_clears_before_confirmation_says_nothing(self) -> None:
        self.assertEqual(self.run_checks([self.bad()], NOW), [])
        self.assertEqual(self.run_checks([self.good()], NOW + 300), [])
        self.assertEqual(self.run_checks([self.bad()], NOW + 600), [])                      # count restarted
        self.assertEqual(self.run_checks([self.good()], NOW + 900), [])

    def test_confirm_one_alerts_immediately(self) -> None:
        self.assertEqual(len(self.run_checks([self.bad(confirm=1)], NOW)), 1)

    def test_escalation_alerts_but_de_escalation_does_not(self) -> None:
        self.run_checks([self.bad(bh.WARN, confirm=1)], NOW)
        up = self.run_checks([self.bad(bh.FAIL, confirm=1)], NOW + 300)
        self.assertEqual(len(up), 1)
        self.assertIn(bh._ICON[bh.FAIL], up[0])
        self.assertEqual(self.run_checks([self.bad(bh.WARN, confirm=1)], NOW + 600), [])    # fail -> warn: quiet
        self.assertEqual(self.run_checks([self.bad(bh.FAIL, confirm=1)], NOW + 900), [])    # back to fail: already told

    def test_recovery_time_runs_from_the_start_of_the_whole_episode(self) -> None:
        self.run_checks([self.bad(bh.WARN, confirm=1)], NOW)
        self.run_checks([self.bad(bh.FAIL, confirm=1)], NOW + HOUR)
        rec = self.run_checks([self.good()], NOW + 3 * HOUR)
        self.assertIn("after 3 h", rec[0])

    def test_reminder_only_for_failures_and_only_after_a_day(self) -> None:
        self.run_checks([self.bad(confirm=1)], NOW)
        self.assertEqual(self.run_checks([self.bad(confirm=1)], NOW + 23 * HOUR), [])
        again = self.run_checks([self.bad(confirm=1)], NOW + 25 * HOUR)
        self.assertEqual(len(again), 1)
        self.assertIn("(still failing)", again[0])
        self.assertEqual(self.run_checks([self.bad(confirm=1)], NOW + 26 * HOUR), [])
        self.run_checks([self.bad(bh.WARN, key="w", confirm=1)], NOW)
        self.assertEqual(self.run_checks([self.bad(bh.WARN, key="w", confirm=1)], NOW + 3 * DAY), [])

    def test_many_checks_in_one_pass_and_results_are_stored(self) -> None:
        lines = self.run_checks([self.bad(key="a", label="A", confirm=1), self.bad(key="b", label="B", confirm=1),
                                 self.good(key="c", label="C")], NOW)
        self.assertEqual(len(lines), 2)
        rows = {r["key"]: r for r in self.conn.execute("SELECT * FROM health_results")}
        self.assertEqual({k: r["status"] for k, r in rows.items()}, {"a": "fail", "b": "fail", "c": "ok"})
        self.assertEqual(rows["a"]["message"], "broken")

    def test_alert_text_is_html_escaped(self) -> None:
        text = bh.format_alert(["\U0001f534 <b>Bad</b> & co: offline"])
        self.assertIn("&lt;b&gt;Bad&lt;/b&gt; &amp; co", text)
        self.assertTrue(text.startswith("⚠️ <b>Device Radar health</b>"))


class RunOnceTests(Db):
    def setUp(self) -> None:
        super().setUp()
        self.fresh_backup()

    def run_once(self, config=None, send=None, environment=None, now=NOW, scanner_alive=True):
        if scanner_alive:
            self.heartbeat(now - 10)   # a running scanner keeps touching its heartbeat as simulated time moves on
        send = send or mock.AsyncMock(return_value=True)
        lines = asyncio.run(bh.run_once(self.db, {**self.config, **(config or {})}, send, environment or env(), now))
        return lines, send

    def test_a_healthy_pi_sends_nothing_and_records_results(self) -> None:
        self.heartbeat(NOW - 10)
        lines, send = self.run_once()
        self.assertEqual(lines, [])
        send.assert_not_awaited()
        keys = {r[0] for r in self.conn.execute("SELECT key FROM health_results")}
        for expected in ("scanner", "service:bt-web", "ollama", "disk:root", "external_mount", "temp", "throttle",
                         "time_sync", "reboot", "calendar", "updates", "database"):
            self.assertIn(expected, keys)

    def test_problems_are_sent_once_in_a_single_message(self) -> None:
        self.heartbeat(NOW - 10)
        bad = env(is_active=lambda n: "failed" if n in ("ollama", "bt-web") else "active",
                  calendar_probe=lambda c: ("auth", "iCloud rejected the login"))
        lines, send = self.run_once(environment=bad)           # calendar fails at once; services need a 2nd run
        self.assertEqual(len(lines), 1)
        lines2, send2 = self.run_once(environment=bad, now=NOW + 300)
        self.assertEqual(len(lines2), 2)                       # the two services, confirmed now
        self.assertEqual(send2.await_count, 1)
        self.assertIn("ollama", send2.await_args.args[0])

    def test_a_stuck_sync_is_restarted_and_reported_in_one_run_then_recovery_is_announced(self) -> None:
        self.heartbeat(NOW - 10)
        restarts = []
        stuck = env(sync_status=lambda: bh.SyncStatus(NOW - 20 * 60, NOW - 10 * 60, "Connecting..."),
                    restart_service=lambda n: (restarts.append(n), True)[1])
        lines, send = self.run_once({"health_check_sync": True}, environment=stuck)
        self.assertEqual(restarts, ["obsidian-sync"])
        self.assertEqual(len(lines), 1)
        self.assertIn("Obsidian sync", lines[0])
        self.assertIn("restarted the service", send.await_args.args[0])
        later = NOW + 300
        healthy = env(sync_status=lambda: bh.SyncStatus(later - 20, later - 20, "Fully synced"))
        lines, send = self.run_once({"health_check_sync": True}, environment=healthy, now=later)
        self.assertEqual(len(lines), 1)
        self.assertIn("back to normal", lines[0])
        keys = {r[0] for r in self.conn.execute("SELECT key FROM health_results")}
        self.assertIn("sync", keys)

    def test_disabled_does_nothing(self) -> None:
        lines, send = self.run_once({"health_alerts_enabled": False})
        self.assertEqual(lines, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM health_results").fetchone()[0], 0)

    def test_dry_run_logs_and_does_not_send(self) -> None:
        bad = env(calendar_probe=lambda c: ("auth", "rejected"))
        with self.assertLogs("bt_health", level="INFO") as logs:
            lines, send = self.run_once({"health_alerts_dry_run": True}, environment=bad)
        self.assertEqual(len(lines), 1)
        send.assert_not_awaited()
        self.assertIn("[dry run]", logs.output[0])

    def test_a_failed_send_is_logged(self) -> None:
        bad = env(calendar_probe=lambda c: ("auth", "rejected"))
        with self.assertLogs("bt_health", level="WARNING"):
            self.run_once(send=mock.AsyncMock(return_value=False), environment=bad)

    def test_slow_checks_are_not_repeated_every_pass(self) -> None:
        calls = []
        environment = env(calendar_probe=lambda c: calls.append(1) or ("ok", "fine"))
        self.run_once(environment=environment, now=NOW)
        self.run_once(environment=environment, now=NOW + 300)
        self.run_once(environment=environment, now=NOW + 5 * HOUR)
        self.assertEqual(len(calls), 1)
        self.run_once(environment=environment, now=NOW + 7 * HOUR)
        self.assertEqual(len(calls), 2)

    def test_external_drive_missing_is_a_warning_and_skips_its_disk_check(self) -> None:
        self.run_once(environment=env(ismount=lambda p: False))
        keys = {r[0]: r[1] for r in self.conn.execute("SELECT key, status FROM health_results")}
        self.assertEqual(keys["external_mount"], "warn")
        self.assertNotIn("disk:external", keys)

    def test_removing_the_always_on_flag_removes_its_old_alert_state(self) -> None:
        self.heartbeat(NOW - 10)
        self.device("AA:00:00:00:00:21", "Doorbell", state="LOST", last_seen=NOW - 3 * HOUR)
        self.run_once()
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM health_state WHERE key LIKE 'offline:%'").fetchone())
        self.conn.execute("UPDATE devices SET always_on = 0")
        self.conn.commit()
        self.run_once(now=NOW + 300)
        for table in ("health_state", "health_results"):
            self.assertIsNone(self.conn.execute(f"SELECT 1 FROM {table} WHERE key LIKE 'offline:%'").fetchone())

    def test_an_offline_always_on_device_is_reported_once_then_recovery(self) -> None:
        self.heartbeat(NOW - 10)
        self.device("AA:00:00:00:00:22", "Doorbell", state="LOST", last_seen=NOW - 3 * HOUR, ip="192.168.1.15")
        lines, _ = self.run_once()
        self.assertEqual(len(lines), 1)
        self.assertIn("Doorbell: offline for 3 h (192.168.1.15)", lines[0])
        self.assertEqual(self.run_once(now=NOW + 300)[0], [])
        self.conn.execute("UPDATE devices SET state='DETECTED', last_seen=? WHERE mac_address=?", (NOW + 600, "AA:00:00:00:00:22"))
        self.conn.commit()
        rec, _ = self.run_once(now=NOW + 700)
        self.assertEqual(len(rec), 1)
        self.assertIn("Doorbell: back to normal", rec[0])


class RestartAnnouncementTests(unittest.TestCase):
    def go(self, gap, send, settings=bh.Settings(), **kw):
        sleep = mock.AsyncMock()
        result = asyncio.run(bh.announce_restart(gap, settings, send, sleep=sleep, **kw))
        return result, sleep

    def test_long_outage_is_announced_with_its_length(self) -> None:
        send = mock.AsyncMock(return_value=True)
        ok, _ = self.go((NOW - 29 * DAY, NOW), send)
        self.assertTrue(ok)
        self.assertIn("back online", send.await_args.args[0])
        self.assertIn("29 days offline", send.await_args.args[0])

    def test_short_outages_and_no_gap_are_silent(self) -> None:
        send = mock.AsyncMock(return_value=True)
        self.assertFalse(self.go((NOW - 600, NOW), send)[0])
        self.assertFalse(self.go(None, send)[0])
        self.assertFalse(self.go((NOW - DAY, NOW), send, bh.Settings(enabled=False))[0])
        send.assert_not_awaited()

    def test_retries_while_the_network_comes_up(self) -> None:
        send = mock.AsyncMock(side_effect=[False, False, True])
        ok, sleep = self.go((NOW - DAY, NOW), send)
        self.assertTrue(ok)
        self.assertEqual((send.await_count, sleep.await_count), (3, 2))

    def test_gives_up_after_the_attempt_limit(self) -> None:
        send = mock.AsyncMock(return_value=False)
        with self.assertLogs("bt_health", level="WARNING"):
            ok, _ = self.go((NOW - DAY, NOW), send, attempts=3)
        self.assertFalse(ok)
        self.assertEqual(send.await_count, 3)

    def test_dry_run_only_logs(self) -> None:
        send = mock.AsyncMock(return_value=True)
        with self.assertLogs("bt_health", level="INFO"):
            ok, _ = self.go((NOW - DAY, NOW), send, bh.Settings(dry_run=True))
        self.assertTrue(ok)
        send.assert_not_awaited()


class LoadResultsTests(Db):
    def test_nothing_has_run_yet(self) -> None:
        r = bh.load_results(self.conn, NOW)
        self.assertEqual((r["summary"], r["stale"], r["checks"], r["worst"]), ("no health checks have run yet", True, [], 0))

    def test_all_ok_problems_and_ordering(self) -> None:
        bh.process_results(self.conn, [bh.Check("a", "Alpha", bh.OK, "fine")], bh.Settings(), NOW)
        r = bh.load_results(self.conn, NOW + 60)
        self.assertEqual((r["summary"], r["stale"], r["worst"]), ("all 1 checks OK", False, 0))
        bh.process_results(self.conn, [bh.Check("b", "Beta", bh.WARN, "meh"), bh.Check("c", "Gamma", bh.FAIL, "bad"),
                                       bh.Check("a", "Alpha", bh.OK, "fine")], bh.Settings(), NOW + 60)
        r = bh.load_results(self.conn, NOW + 120)
        self.assertEqual([c["label"] for c in r["checks"]], ["Gamma", "Beta", "Alpha"])   # worst first
        self.assertEqual((r["summary"], r["problems"], r["worst"]), ("2 problems", 2, 2))

    def test_results_go_stale_if_the_watchdog_stops(self) -> None:
        bh.process_results(self.conn, [bh.Check("a", "Alpha", bh.OK, "fine")], bh.Settings(), NOW)
        r = bh.load_results(self.conn, NOW + 2 * HOUR)
        self.assertTrue(r["stale"])
        self.assertIn("has not reported for 2 h", r["summary"])


if __name__ == "__main__":
    unittest.main()
