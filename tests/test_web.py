"""Smoke tests for the Flask dashboard. Run from the project directory:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bt_db  # noqa: E402
import bt_web  # noqa: E402


class WebSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name) / "bt_radar.db"
        bt_db.init_db(db)
        conn = bt_db.get_connection(db)
        bt_db.upsert_device(conn, "AA:BB:CC:00:00:01", advertised_name="Test Device")
        bt_db.upsert_echo_device(conn, "Kitchen Echo")
        conn.close()
        original = bt_web.get_db_path
        bt_web.get_db_path = lambda: db
        self.addCleanup(setattr, bt_web, "get_db_path", original)
        # Never read the real config.json: it enables calendar/Alexa and the
        # device page would then call iCloud with real credentials.
        original_config = bt_web.load_config
        bt_web.load_config = lambda: {}
        self.addCleanup(setattr, bt_web, "load_config", original_config)
        self.client = bt_web.app.test_client()

    def test_pages_load(self) -> None:
        for url in ("/", "/history", "/pairing", "/alexa", "/device/AA:BB:CC:00:00:01"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_core_api_loads(self) -> None:
        for url in ("/api/devices", "/api/stats", "/api/events", "/api/echo-devices",
                    "/api/cleanup/preview"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_assistant_page_and_api_are_gone(self) -> None:
        self.assertEqual(self.client.get("/assistant").status_code, 404)
        for method in ("get", "post", "delete"):
            for url in ("/api/assistant/history", "/api/assistant/chat", "/api/assistant/speak"):
                with self.subTest(method=method, url=url):
                    self.assertEqual(getattr(self.client, method)(url).status_code, 404)

    def test_no_assistant_link_in_navigation(self) -> None:
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("/assistant", html)
        self.assertIn('href="/alexa"', html)

    def test_echo_device_api_still_works(self) -> None:
        devices = self.client.get("/api/echo-devices").get_json()
        self.assertEqual([d["device_name"] for d in devices], ["Kitchen Echo"])

    def device(self, mac: str) -> dict:
        conn = bt_db.get_connection(bt_web.get_db_path())
        try:
            return bt_db.get_device(conn, mac)
        finally:
            conn.close()

    def patch(self, mac: str, body: dict):
        return self.client.patch(f"/api/devices/{mac}", json=body)

    def test_people_endpoint_reports_home_by_phone(self) -> None:
        conn = bt_db.get_connection(bt_web.get_db_path())
        bt_db.upsert_device(conn, "AA:BB:CC:00:00:02", advertised_name="x", scan_type="BLE")
        bt_db.update_device(conn, "AA:BB:CC:00:00:02", friendly_name="Lilou's iPhone", device_type="Phone")
        bt_db.upsert_device(conn, "AA:BB:CC:00:00:03", advertised_name="y", scan_type="WiFi")
        bt_db.update_device(conn, "AA:BB:CC:00:00:03", friendly_name="Laura's MacBook", device_type="Laptop")
        conn.close()
        people = {p["person"]: p for p in self.client.get("/api/people").get_json()}
        self.assertEqual(people["lilou"]["state"], "home")
        self.assertEqual(people["laura"]["state"], "no_phone")
        self.assertEqual(people["laura"]["others"], ["Laura's MacBook"])

    def test_device_list_includes_effective_role_and_person(self) -> None:
        self.patch("AA:BB:CC:00:00:01", {"friendly_name": "Ava's iPad", "device_type": "Tablet"})
        dev = next(d for d in self.client.get("/api/devices").get_json() if d["mac_address"] == "AA:BB:CC:00:00:01")
        self.assertEqual((dev["effective_role"], dev["effective_person"]), ("laptop", "ava"))

    def test_role_and_person_can_be_set_and_cleared(self) -> None:
        mac = "AA:BB:CC:00:00:01"
        self.assertEqual(self.patch(mac, {"role": "phone", "person": "  Sam  "}).status_code, 200)
        dev = self.device(mac)
        self.assertEqual((dev["role"], dev["person"]), ("phone", "sam"))
        self.assertEqual(self.patch(mac, {"role": "", "person": ""}).status_code, 200)
        dev = self.device(mac)
        self.assertEqual((dev["role"], dev["person"]), (None, None))

    def test_bad_role_is_rejected_and_nothing_changes(self) -> None:
        mac = "AA:BB:CC:00:00:01"
        r = self.patch(mac, {"role": "wizard", "friendly_name": "Changed"})
        self.assertEqual(r.status_code, 400)
        dev = self.device(mac)
        self.assertIsNone(dev["role"])
        self.assertNotEqual(dev["friendly_name"], "Changed")

    def test_person_is_sanitised(self) -> None:
        mac = "AA:BB:CC:00:00:01"
        self.patch(mac, {"person": "<script>Anne-Marie</script>"})
        dev = self.device(mac)
        self.assertNotIn("<", dev["person"])
        self.assertEqual(dev["person"], "scriptanne-mariescript")

    def test_device_page_offers_role_and_person(self) -> None:
        html = self.client.get("/device/AA:BB:CC:00:00:01").get_data(as_text=True)
        self.assertIn('id="device-role"', html)
        self.assertIn('id="device-person"', html)

    def test_dashboard_has_the_people_strip(self) -> None:
        self.assertIn('id="people-strip"', self.client.get("/").get_data(as_text=True))

    def test_telegram_chat_helpers_still_exist(self) -> None:
        # The Telegram bot shares the chat history table and the async search chat.
        import bt_search
        conn = bt_db.get_connection(bt_web.get_db_path())
        bt_db.save_chat_message(conn, "12345", "user", "hello")
        history = bt_db.get_chat_history(conn, "12345", 10)
        self.assertEqual([m["content"] for m in history], ["hello"])
        self.assertEqual(bt_db.cleanup_chat_history(conn, max_age_days=7), 0)
        conn.close()
        self.assertTrue(callable(bt_search.chat_with_search_async))
        self.assertFalse(hasattr(bt_search, "chat_with_search_sync"))
        self.assertFalse(hasattr(bt_db, "clear_chat_history"))


if __name__ == "__main__":
    unittest.main()
