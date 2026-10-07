"""Tests for proximity alerts. Run from the project directory:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bt_alexa  # noqa: E402
import bt_db  # noqa: E402

CONFIG = {"departure_threshold_seconds": 60, "alexa_enabled": True}
BLE = "AA:00:00:00:00:01"


class FreshnessTests(unittest.TestCase):
    def test_recent_sighting_is_fresh(self) -> None:
        self.assertTrue(bt_alexa._ble_sighting_is_fresh({"last_seen": 990.0}, CONFIG, 1000.0))

    def test_boundary_is_fresh(self) -> None:
        self.assertTrue(bt_alexa._ble_sighting_is_fresh({"last_seen": 940.0}, CONFIG, 1000.0))

    def test_old_sighting_is_stale(self) -> None:
        self.assertFalse(bt_alexa._ble_sighting_is_fresh({"last_seen": 939.0}, CONFIG, 1000.0))

    def test_missing_last_seen_is_stale(self) -> None:
        self.assertFalse(bt_alexa._ble_sighting_is_fresh({"last_seen": None}, CONFIG, 1000.0))
        self.assertFalse(bt_alexa._ble_sighting_is_fresh({}, CONFIG, 1000.0))

    def test_uses_scanner_default_window_when_not_configured(self) -> None:
        self.assertTrue(bt_alexa._ble_sighting_is_fresh({"last_seen": 750.0}, {}, 1000.0))
        self.assertFalse(bt_alexa._ble_sighting_is_fresh({"last_seen": 600.0}, {}, 1000.0))


class ProximityAlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "bt_radar.db"
        bt_db.init_db(self.db)

    def make_phone(self, *, seen_ago: float, rssi: int = -45, linked_wifi: bool = False) -> None:
        conn = bt_db.get_connection(self.db)
        bt_db.upsert_device(conn, BLE, advertised_name="Phone", scan_type="BLE", rssi=rssi)
        bt_db.update_device(
            conn, BLE, friendly_name="Test Phone", state="DETECTED",
            last_seen=time.time() - seen_ago, proximity_enabled=True,
            proximity_rssi_threshold=-50, proximity_interval=60,
            proximity_prompt="Say hello", proximity_alexa_device="Office Echo",
        )
        if linked_wifi:
            wifi = "BB:00:00:00:00:02"
            bt_db.upsert_device(conn, wifi, scan_type="WiFi", ip_address="192.168.1.50")
            bt_db.link_device(conn, wifi, BLE)  # WiFi twin keeps the group "home"
        conn.close()

    async def run_check(self) -> mock.AsyncMock:
        speak = mock.AsyncMock(return_value=True)
        with mock.patch.object(bt_alexa, "speak", speak), \
             mock.patch.object(bt_alexa, "generate_encouragement", mock.AsyncMock(return_value="Hello")), \
             mock.patch.object(bt_alexa.bt_calendar, "get_device_calendar_context", mock.AsyncMock(return_value="")), \
             mock.patch.object(bt_alexa.bt_news, "get_device_news_suffix", mock.AsyncMock(return_value="")):
            await bt_alexa.check_proximity_devices(CONFIG, self.db)
        return speak

    async def test_speaks_when_close_and_recently_seen(self) -> None:
        self.make_phone(seen_ago=5)
        speak = await self.run_check()
        speak.assert_awaited_once()
        self.assertEqual(speak.await_args.kwargs["device"], "Office Echo")

    async def test_silent_when_signal_stale_even_if_state_is_detected(self) -> None:
        # State is DETECTED (kept alive by the linked WiFi twin) but Bluetooth
        # has not been seen for hours, so the old strong RSSI must be ignored.
        self.make_phone(seen_ago=3 * 3600, linked_wifi=True)
        speak = await self.run_check()
        speak.assert_not_awaited()

    async def test_silent_when_too_far_away(self) -> None:
        self.make_phone(seen_ago=5, rssi=-80)
        speak = await self.run_check()
        speak.assert_not_awaited()

    async def test_interval_still_respected(self) -> None:
        self.make_phone(seen_ago=5)
        conn = bt_db.get_connection(self.db)
        bt_db.update_device(conn, BLE, last_proximity_message=time.time() - 60)
        conn.close()
        speak = await self.run_check()
        speak.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
