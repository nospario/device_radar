"""Tests for people and device roles. Run: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bt_db  # noqa: E402
import bt_people as bp  # noqa: E402

NOW = 1_800_000_000.0


class RoleTests(unittest.TestCase):
    def test_role_from_type(self) -> None:
        cases = {"Phone": "phone", "iPhone": "phone", "Laptop": "laptop", "Tablet": "laptop",
                 "Smart Speaker": "smart_home", "Smart Plug": "smart_home", "WiFi Router": "smart_home",
                 "IoT": "smart_home", "Printer": "smart_home", "Watch": "other"}
        for dtype, role in cases.items():
            self.assertEqual(bp.role_from_type(dtype), role, dtype)

    def test_unknown_types_have_no_role(self) -> None:
        for dtype in ("Network Device", "Unknown", "", None, "Beacon"):
            self.assertIsNone(bp.role_from_type(dtype), dtype)

    def test_explicit_role_wins_and_junk_is_ignored(self) -> None:
        self.assertEqual(bp.effective_role({"role": "laptop", "device_type": "Phone"}), "laptop")
        self.assertEqual(bp.effective_role({"role": " PHONE ", "device_type": "IoT"}), "phone")
        self.assertEqual(bp.effective_role({"role": "wizard", "device_type": "Phone"}), "phone")
        self.assertEqual(bp.effective_role({"role": "", "device_type": "Laptop"}), "laptop")
        self.assertIsNone(bp.effective_role({"device_type": "Network Device"}))


class PersonTests(unittest.TestCase):
    def test_person_from_name(self) -> None:
        self.assertEqual(bp.person_from_name("Laura's MacBook"), "laura")
        self.assertEqual(bp.person_from_name("Richard’s iPhone (WiFi)"), "richard")
        self.assertEqual(bp.person_from_name("Anne-Marie's iPad"), "anne-marie")
        for name in ("Richard Home Mouse", "Dryer", "", None, "Lauras-MBP.lan", "'s thing"):
            self.assertIsNone(bp.person_from_name(name), name)

    def test_normalise_person(self) -> None:
        self.assertEqual(bp.normalise_person("  Laura "), "laura")
        self.assertEqual(bp.normalise_person("Anne-Marie!"), "anne-marie")
        self.assertEqual(bp.normalise_person("<b>x</b>"), "bxb")  # no markup characters survive
        self.assertIsNone(bp.normalise_person("   "))
        self.assertIsNone(bp.normalise_person(None))
        self.assertEqual(len(bp.normalise_person("a" * 80)), 30)

    def test_explicit_person_overrides_the_name(self) -> None:
        self.assertEqual(bp.effective_person({"friendly_name": "Laura's iPad", "person": "Ava"}), "ava")
        self.assertEqual(bp.effective_person({"friendly_name": "Laura's iPad"}), "laura")
        self.assertEqual(bp.effective_person({"friendly_name": "Dryer", "person": "richard"}), "richard")
        self.assertIsNone(bp.effective_person({"friendly_name": "Dryer"}))


class NotifyAllowedTests(unittest.TestCase):
    phone = {"is_notify": 1, "device_type": "Phone"}
    laptop = {"is_notify": 1, "device_type": "Laptop"}

    def test_phone_with_notify_alerts(self) -> None:
        self.assertTrue(bp.notify_allowed({}, [self.phone]))

    def test_laptop_with_notify_is_silenced_by_default(self) -> None:
        self.assertFalse(bp.notify_allowed({}, [self.laptop]))

    def test_unknown_type_is_not_a_phone(self) -> None:
        self.assertFalse(bp.notify_allowed({}, [{"is_notify": 1, "device_type": "Network Device"}]))

    def test_notify_flag_is_still_required(self) -> None:
        self.assertFalse(bp.notify_allowed({}, [{"is_notify": 0, "device_type": "Phone"}]))

    def test_group_needs_a_phone_member_and_a_notify_member(self) -> None:
        group = [{"is_notify": 0, "device_type": "Phone"}, {"is_notify": 1, "device_type": "Network Device"}]
        self.assertTrue(bp.notify_allowed({}, group))
        self.assertFalse(bp.notify_allowed({}, [self.laptop, {"is_notify": 1, "device_type": "Network Device"}]))

    def test_can_be_switched_off_in_config(self) -> None:
        self.assertTrue(bp.notify_allowed({"notify_phones_only": False}, [self.laptop]))
        self.assertFalse(bp.notify_allowed({"notify_phones_only": False}, [{"is_notify": 0}]))
        self.assertFalse(bp.notify_allowed({"notify_phones_only": True}, [self.laptop]))


class PeopleStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "bt_radar.db"
        bt_db.init_db(self.db)
        self.conn = bt_db.get_connection(self.db)
        self.addCleanup(self.conn.close)

    def add(self, mac, name, dtype, state="DETECTED", last_seen=NOW - 60, **cols):
        bt_db.upsert_device(self.conn, mac, advertised_name=name, scan_type="BLE", state=state)
        self.conn.execute(
            "UPDATE devices SET friendly_name = ?, device_type = ?, state = ?, last_seen = ? WHERE mac_address = ?",
            (name, dtype, state, last_seen, mac))
        for c, v in cols.items():
            self.conn.execute(f"UPDATE devices SET {c} = ? WHERE mac_address = ?", (v, mac))
        self.conn.commit()

    def event(self, mac, kind, ts):
        self.conn.execute("INSERT INTO events (mac_address, event_type, timestamp) VALUES (?, ?, ?)", (mac, kind, ts))
        self.conn.commit()

    def status(self, config=None):
        return {p["person"]: p for p in bp.people_status(self.conn, config or {}, NOW)}

    def test_home_when_the_phone_is_detected_with_arrival_time(self) -> None:
        self.add("AA:00:00:00:00:01", "Richard's iPhone", "Phone")
        self.event("AA:00:00:00:00:01", "arrived", NOW - 7200)
        self.event("AA:00:00:00:00:01", "departed", NOW - 20000)
        p = self.status()["richard"]
        self.assertEqual((p["state"], p["since"], p["phones"]), ("home", NOW - 7200, ["Richard's iPhone"]))

    def test_away_when_the_phone_is_lost_with_departure_time(self) -> None:
        self.add("AA:00:00:00:00:02", "Mathilde's iPhone", "Phone", state="LOST", last_seen=NOW - 5000)
        self.event("AA:00:00:00:00:02", "departed", NOW - 4000)
        p = self.status()["mathilde"]
        self.assertEqual((p["state"], p["since"], p["last_seen"]), ("away", NOW - 4000, NOW - 5000))

    def test_laptops_never_make_someone_home(self) -> None:
        self.add("AA:00:00:00:00:03", "Laura's MacBook", "Laptop")          # connected
        self.add("AA:00:00:00:00:04", "Laura's iPad", "Tablet")             # connected
        p = self.status()["laura"]
        self.assertEqual((p["state"], p["phones"]), ("no_phone", []))
        self.assertEqual(sorted(p["others"]), ["Laura's MacBook", "Laura's iPad"])

    def test_a_linked_wifi_record_keeps_the_phone_home(self) -> None:
        self.add("AA:00:00:00:00:05", "Lilou's iPhone", "Phone", state="LOST", last_seen=NOW - 9000)
        self.add("AA:00:00:00:00:06", "Lilou's iPhone (WiFi)", "Network Device", linked_to="AA:00:00:00:00:05")
        p = self.status()["lilou"]
        self.assertEqual(p["state"], "home")
        self.assertEqual(p["phones"], ["Lilou's iPhone"])   # the WiFi record is not listed as a separate phone
        self.assertEqual(p["others"], [])

    def test_person_in_config_without_devices_shows_as_no_phone(self) -> None:
        p = self.status({"person_aliases": {"Ava": "Ava's iPhone"}})["ava"]
        self.assertEqual((p["state"], p["phones"], p["others"]), ("no_phone", [], []))

    def test_explicit_person_and_role_override_the_name_and_type(self) -> None:
        self.add("AA:00:00:00:00:07", "Pocket Thing", "Network Device", person="Ava", role="phone")
        self.add("AA:00:00:00:00:08", "Dryer", "Smart Plug", person="ava")
        p = self.status()["ava"]
        self.assertEqual((p["state"], p["phones"], p["others"]), ("home", ["Pocket Thing"], ["Dryer"]))

    def test_unnamed_devices_are_not_people(self) -> None:
        bt_db.upsert_device(self.conn, "AA:00:00:00:00:09", advertised_name="Pixel.lan", scan_type="WiFi")
        self.assertEqual(bp.people_status(self.conn, {}, NOW), [])

    def test_ordering_home_then_away_then_no_phone(self) -> None:
        self.add("AA:00:00:00:00:10", "Zed's iPhone", "Phone")
        self.add("AA:00:00:00:00:11", "Amy's iPhone", "Phone", state="LOST")
        self.add("AA:00:00:00:00:12", "Bob's MacBook", "Laptop")
        self.assertEqual([p["person"] for p in bp.people_status(self.conn, {}, NOW)], ["zed", "amy", "bob"])

    def test_person_entry_and_unknown_names(self) -> None:
        self.add("AA:00:00:00:00:13", "Laura's MacBook", "Laptop")
        self.assertEqual(bp.person_entry(self.conn, {}, "Laura")["state"], "no_phone")
        self.assertIsNone(bp.person_entry(self.conn, {}, "nobody"))

    def test_best_phone_prefers_a_connected_one(self) -> None:
        self.add("AA:00:00:00:00:14", "Old iPhone", "Phone", state="LOST", last_seen=NOW - 100, person="sam")
        self.add("AA:00:00:00:00:15", "New iPhone", "Phone", state="DETECTED", last_seen=NOW - 500, person="sam")
        self.assertEqual(bp.best_phone(self.conn, {}, "Sam")["friendly_name"], "New iPhone")

    def test_best_phone_is_none_without_a_phone(self) -> None:
        self.add("AA:00:00:00:00:16", "Laura's MacBook", "Laptop")
        self.assertIsNone(bp.best_phone(self.conn, {}, "laura"))


if __name__ == "__main__":
    unittest.main()
