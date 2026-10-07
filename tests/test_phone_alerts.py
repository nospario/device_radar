"""The scanner's arrival/departure alerts only come from phones. Run: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bt_db  # noqa: E402
import bt_scanner  # noqa: E402


class AlertCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "bt_radar.db"
        bt_db.init_db(self.db)
        self.conn = bt_db.get_connection(self.db)
        self.addCleanup(self.conn.close)
        self.config = {**bt_scanner.DEFAULT_CONFIG, "db_path": str(self.db), "arrival_cooldown_seconds": 0,
                       "departure_threshold_seconds": 60, "wifi_scan_enabled": False}
        send = mock.AsyncMock()
        patcher = mock.patch.object(bt_scanner.bt_telegram, "send_notification", send)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.send = send
        ping = mock.patch.object(bt_scanner.bt_wifi, "ping_host", mock.AsyncMock(return_value=False))
        ping.start()
        self.addCleanup(ping.stop)

    def scanner(self, **config) -> bt_scanner.BluetoothRadarScanner:
        return bt_scanner.BluetoothRadarScanner({**self.config, **config})

    def add(self, mac, name, dtype, *, notify=True, state="LOST", last_seen=None, **cols) -> str:
        bt_db.upsert_device(self.conn, mac, advertised_name=name, scan_type="BLE", state=state)
        self.conn.execute(
            "UPDATE devices SET friendly_name=?, device_type=?, is_watchlisted=1, is_notify=?, state=?, last_seen=? "
            "WHERE mac_address=?", (name, dtype, int(notify), state, last_seen or time.time(), mac))
        for c, v in cols.items():
            self.conn.execute(f"UPDATE devices SET {c} = ? WHERE mac_address = ?", (v, mac))
        self.conn.commit()
        return mac

    def arrive(self, scanner, mac) -> None:
        asyncio.run(scanner._check_arrivals(self.conn, {mac}, {mac: "LOST"}))

    def depart(self, scanner) -> None:
        asyncio.run(scanner._check_departures(self.conn, set(), time.time()))

    def sent(self) -> list[tuple]:
        return [c.args for c in self.send.await_args_list]


class StartupTests(AlertCase):
    def test_scanner_starts_with_no_startup_gap(self) -> None:
        self.assertIsNone(self.scanner().startup_gap)

    def test_run_announces_a_recorded_gap_once(self) -> None:
        scanner = self.scanner()
        scanner.startup_gap = (time.time() - 86400, time.time())
        announce = mock.AsyncMock(return_value=True)

        async def go():
            with mock.patch.object(bt_scanner.bt_health, "announce_restart", announce), \
                 mock.patch.object(scanner, "process_scan", side_effect=asyncio.CancelledError):
                try:
                    await scanner.run()
                except asyncio.CancelledError:
                    pass
                await asyncio.sleep(0)

        asyncio.run(go())
        announce.assert_awaited_once()
        self.assertEqual(announce.await_args.args[0], scanner.startup_gap)


class ArrivalTests(AlertCase):
    def test_phone_arrival_alerts(self) -> None:
        mac = self.add("AA:00:00:00:00:01", "Lilou's iPhone", "Phone")
        self.arrive(self.scanner(), mac)
        self.assertEqual(self.sent(), [("Lilou's iPhone", "arrived")])

    def test_laptop_with_notify_on_stays_silent(self) -> None:
        mac = self.add("AA:00:00:00:00:02", "Laura's MacBook Pro", "Laptop")
        self.arrive(self.scanner(), mac)
        self.assertEqual(self.sent(), [])

    def test_smart_home_device_with_notify_on_stays_silent(self) -> None:
        mac = self.add("AA:00:00:00:00:03", "192.168.1.2", "Network Device")
        self.arrive(self.scanner(), mac)
        self.assertEqual(self.sent(), [])

    def test_the_arrival_is_still_recorded_in_history_when_silenced(self) -> None:
        mac = self.add("AA:00:00:00:00:04", "Laura's MacBook Pro", "Laptop")
        self.arrive(self.scanner(), mac)
        self.assertEqual(len(bt_db.get_events(self.conn, mac=mac, event_type="arrived")), 1)

    def test_switching_the_rule_off_restores_laptop_alerts(self) -> None:
        mac = self.add("AA:00:00:00:00:05", "Laura's MacBook Pro", "Laptop")
        self.arrive(self.scanner(notify_phones_only=False), mac)
        self.assertEqual(self.sent(), [("Laura's MacBook Pro", "arrived")])

    def test_an_explicit_phone_role_overrides_the_device_type(self) -> None:
        mac = self.add("AA:00:00:00:00:06", "Pocket thing", "Network Device", role="phone")
        self.arrive(self.scanner(), mac)
        self.assertEqual(self.sent(), [("Pocket thing", "arrived")])

    def test_linked_group_alerts_once_under_the_phones_name(self) -> None:
        phone = self.add("AA:00:00:00:00:07", "Richard's iPhone", "iPhone", notify=True)
        wifi = self.add("AA:00:00:00:00:08", "Richard's iPhone (WiFi)", "Network Device", notify=False,
                        linked_to=phone)
        asyncio.run(self.scanner()._check_arrivals(self.conn, {phone, wifi}, {phone: "LOST", wifi: "LOST"}))
        self.assertEqual(self.sent(), [("Richard's iPhone", "arrived")])


class DepartureTests(AlertCase):
    GONE = time.time() - 1000

    def test_phone_departure_alerts(self) -> None:
        self.add("AA:00:00:00:00:11", "Lilou's iPhone", "Phone", state="DETECTED", last_seen=self.GONE)
        self.depart(self.scanner())
        self.assertEqual(self.sent(), [("Lilou's iPhone", "departed")])

    def test_laptop_departure_is_silent_but_recorded(self) -> None:
        mac = self.add("AA:00:00:00:00:12", "Laura's MacBook Pro", "Laptop", state="DETECTED", last_seen=self.GONE)
        self.depart(self.scanner())
        self.assertEqual(self.sent(), [])
        self.assertEqual(len(bt_db.get_events(self.conn, mac=mac, event_type="departed")), 1)

    def test_linked_group_departure_alerts_once_when_everything_is_gone(self) -> None:
        phone = self.add("AA:00:00:00:00:13", "Richard's iPhone", "iPhone", state="DETECTED", last_seen=self.GONE)
        self.add("AA:00:00:00:00:14", "Richard's iPhone (WiFi)", "Network Device", notify=False,
                 state="DETECTED", last_seen=self.GONE, linked_to=phone, scan_type="BLE")
        self.depart(self.scanner())
        self.assertEqual(self.sent(), [("Richard's iPhone", "departed")])


if __name__ == "__main__":
    unittest.main()
