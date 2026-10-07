"""Tests for new-device alerts. Run: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bt_cleanup  # noqa: E402
import bt_db  # noqa: E402
import bt_newdevice as nd  # noqa: E402

NOW = 1_800_000_000.0
MAC = "AA:BB:CC:00:00:01"


def device(**kw) -> dict:
    base = {"mac_address": MAC, "advertised_name": "iPhone.lan", "ip_address": "192.168.1.158",
            "manufacturer": None, "first_seen": NOW}
    base.update(kw)
    return base


class DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "bt_radar.db"
        bt_db.init_db(self.db)
        self.conn = bt_db.get_connection(self.db)
        self.addCleanup(self.conn.close)

    def add(self, mac: str = MAC, *, scan_type: str = "WiFi", state: str = "DETECTED", **cols) -> str:
        bt_db.upsert_device(self.conn, mac, advertised_name="host.lan", scan_type=scan_type,
                            state=state, ip_address="192.168.1.50")
        for c, v in cols.items():
            self.conn.execute(f"UPDATE devices SET {c} = ? WHERE mac_address = ?", (v, mac))
        self.conn.commit()
        return mac

    def status(self, mac: str = MAC):
        row = self.conn.execute("SELECT kind, status FROM device_alerts WHERE mac = ?", (mac,)).fetchone()
        return tuple(row) if row else None


class SettingsAndParsingTests(unittest.TestCase):
    def test_defaults(self) -> None:
        self.assertEqual(nd.load_settings({}), nd.Settings(True, False, 6))

    def test_overrides_and_bad_values(self) -> None:
        s = nd.load_settings({"new_device_alerts_enabled": False, "new_device_alerts_dry_run": True,
                              "new_device_alerts_max_per_hour": 2})
        self.assertEqual(s, nd.Settings(False, True, 2))
        bad = nd.load_settings({"new_device_alerts_enabled": "no", "new_device_alerts_max_per_hour": 0,
                                "new_device_alerts_dry_run": 1})
        self.assertEqual(bad, nd.Settings(True, False, 6))
        self.assertEqual(nd.load_settings({"new_device_alerts_max_per_hour": True}).max_per_hour, 6)

    def test_callback_round_trip(self) -> None:
        for action in ("phone", "laptop", "home", "ignore"):
            data = nd.callback_data(action, MAC.lower())
            self.assertLessEqual(len(data.encode()), 64)  # Telegram's limit
            self.assertEqual(nd.parse_callback(data), (action, MAC))

    def test_callback_rejects_anything_else(self) -> None:
        for bad in (None, "", "habit:abc", "nd:phone:", "nd:phone:AA:BB", "nd:root:AA:BB:CC:00:00:01",
                    "nd:phone:AA:BB:CC:00:00:0G", "nd:phone:AA:BB:CC:00:00:01; DROP TABLE devices",
                    "xnd:phone:AA:BB:CC:00:00:01"):
            self.assertIsNone(nd.parse_callback(bad), bad)


class GuessTests(unittest.TestCase):
    def guess(self, host, vendor=None):
        return nd.guess_action({"advertised_name": host, "manufacturer": vendor})

    def test_phones(self) -> None:
        for host in ("iPhone.lan", "Richards-iPhone.lan", "android-1a2b.lan", "Galaxy-S21", "Pixel-7"):
            self.assertEqual(self.guess(host), "phone", host)

    def test_laptops(self) -> None:
        for host in ("Mac.lan", "Lauras-MBP.lan", "MacBook-Pro.local", "Sterling-laptop.lan", "DESKTOP-ABC", "iPad.lan"):
            self.assertEqual(self.guess(host), "laptop", host)

    def test_smart_home_by_hostname_or_vendor(self) -> None:
        self.assertEqual(self.guess("RingDoorbell-9e.lan"), "home")
        self.assertEqual(self.guess("HS100.lan"), "home")
        self.assertEqual(self.guess("192.168.1.9", "TP-LINK TECHNOLOGIES CO.,LTD."), "home")
        self.assertEqual(self.guess("amazon-29a76c36b.lan", "Amazon Technologies Inc."), "home")

    def test_whole_word_matching_avoids_false_hits(self) -> None:
        # "Sterling" contains "ring"; "Campbell" starts with "cam"; neither is a smart-home device
        self.assertIsNone(self.guess("Sterling.lan"))
        self.assertIsNone(self.guess("Marina.lan"))
        self.assertEqual(self.guess("Campbells-iPhone"), "phone")  # phone check wins over "cam"

    def test_no_clue_is_none(self) -> None:
        self.assertIsNone(self.guess("192.168.1.5"))
        self.assertIsNone(self.guess(None))


class TextAndKeyboardTests(unittest.TestCase):
    def test_alert_text_escapes_html_from_the_network(self) -> None:
        text = nd.alert_text(device(advertised_name="<b>evil</b>&co.lan", manufacturer="A<i>B"))
        self.assertNotIn("<b>evil", text)
        self.assertIn("&lt;b&gt;evil&lt;/b&gt;&amp;co.lan", text)
        self.assertIn("A&lt;i&gt;B", text)

    def test_private_address_without_vendor_is_explained(self) -> None:
        text = nd.alert_text(device(mac_address="8E:44:51:E7:87:9D"))
        self.assertIn("private (randomised) address", text)

    def test_public_address_without_vendor_is_not_called_private(self) -> None:
        text = nd.alert_text(device(mac_address="C0:C7:DB:02:98:70", advertised_name="Lauras-MBP.lan"))
        self.assertIn("not in the vendor list", text)
        self.assertNotIn("private", text)

    def test_vendor_ip_and_guess_shown(self) -> None:
        text = nd.alert_text(device(advertised_name="HS100.lan", manufacturer="TP-LINK TECHNOLOGIES CO.,LTD."))
        self.assertIn("Maker: TP-LINK", text)
        self.assertIn("IP: 192.168.1.158", text)
        self.assertIn("Looks like: " + nd.ROLES["home"]["label"], text)

    def test_ip_only_device_has_no_name_line_claim(self) -> None:
        text = nd.alert_text(device(advertised_name="192.168.1.158"))
        self.assertIn("(none announced)", text)

    def test_keyboard_puts_the_guess_first_with_a_star_and_always_offers_ignore(self) -> None:
        kb = nd.keyboard(device(advertised_name="Mac.lan"))["inline_keyboard"]
        buttons = [b for row in kb for b in row]
        self.assertEqual(len(buttons), 4)
        self.assertIn("⭐", buttons[0]["text"])
        self.assertEqual(nd.parse_callback(buttons[0]["callback_data"]), ("laptop", MAC))
        self.assertEqual(nd.parse_callback(buttons[-1]["callback_data"]), ("ignore", MAC))

    def test_keyboard_without_a_guess_keeps_default_order(self) -> None:
        buttons = [b for row in nd.keyboard(device(advertised_name="192.168.1.9"))["inline_keyboard"] for b in row]
        self.assertEqual([nd.parse_callback(b["callback_data"])[0] for b in buttons],
                         ["phone", "laptop", "home", "ignore"])
        self.assertFalse(any("⭐" in b["text"] for b in buttons))


class AnnounceTests(DbCase):
    def run_announce(self, send, settings=nd.Settings(), dev=None, now=NOW):
        import asyncio
        return asyncio.run(nd.announce_new_wifi_device(self.conn, dev or device(), settings, send, now))

    def test_sends_once_with_buttons_then_remembers(self) -> None:
        send = mock.AsyncMock(return_value=True)
        self.assertEqual(self.run_announce(send), "sent")
        text = send.await_args.args[0]
        self.assertIn("New device on the WiFi", text)
        self.assertIn("inline_keyboard", send.await_args.kwargs["reply_markup"])
        self.assertEqual(self.status(), ("new", "sent"))
        self.assertEqual(self.run_announce(send), "known")
        self.assertEqual(send.await_count, 1)

    def test_disabled_sends_nothing_and_records_nothing(self) -> None:
        send = mock.AsyncMock(return_value=True)
        self.assertEqual(self.run_announce(send, nd.Settings(enabled=False)), "disabled")
        send.assert_not_awaited()
        self.assertIsNone(self.status())

    def test_dry_run_logs_but_does_not_send(self) -> None:
        send = mock.AsyncMock(return_value=True)
        with self.assertLogs("bt_newdevice", level="INFO") as logs:
            self.assertEqual(self.run_announce(send, nd.Settings(dry_run=True)), "dry_run")
        send.assert_not_awaited()
        self.assertIn("would announce", logs.output[0])
        self.assertEqual(self.status(), ("new", "dry_run"))

    def test_hourly_cap_suppresses_and_suppressed_ones_do_not_count(self) -> None:
        send = mock.AsyncMock(return_value=True)
        s = nd.Settings(max_per_hour=2)
        results = [self.run_announce(send, s, device(mac_address=f"AA:BB:CC:00:00:{i:02X}")) for i in range(5)]
        self.assertEqual(results, ["sent", "sent", "rate_limited", "rate_limited", "rate_limited"])
        self.assertEqual(send.await_count, 2)
        self.assertEqual(self.status("AA:BB:CC:00:00:02"), ("new", "suppressed"))
        # an hour later the cap has room again
        later = self.run_announce(send, s, device(mac_address="AA:BB:CC:00:00:09"), now=NOW + 3601)
        self.assertEqual(later, "sent")

    def test_failed_send_is_recorded_and_not_retried(self) -> None:
        send = mock.AsyncMock(return_value=False)
        with self.assertLogs("bt_newdevice", level="WARNING"):
            self.assertEqual(self.run_announce(send), "failed")
        self.assertEqual(self.status(), ("new", "failed"))
        self.assertEqual(self.run_announce(send), "known")

    def test_forgotten_device_is_not_announced_as_new(self) -> None:
        nd.forget(self.conn, [MAC], NOW)
        self.conn.commit()
        send = mock.AsyncMock(return_value=True)
        self.assertEqual(self.run_announce(send), "known")
        send.assert_not_awaited()


class ApplyRoleTests(DbCase):
    def get(self):
        return bt_db.get_device(self.conn, MAC)

    def test_phone_is_named_watched_and_notifying(self) -> None:
        self.add()
        r = nd.apply_role(self.conn, MAC, "phone", "  Mathilde's iPhone ")
        self.assertTrue(r["ok"])
        d = self.get()
        self.assertEqual((d["friendly_name"], d["device_type"], d["role"], d["is_watchlisted"], d["is_notify"]),
                         ("Mathilde's iPhone", "Phone", "phone", 1, 1))
        self.assertEqual(self.status(), ("new", "named"))

    def test_laptop_and_smart_home_are_named_only(self) -> None:
        for action, mac, dtype, role in (("laptop", "AA:BB:CC:00:00:02", "Laptop", "laptop"),
                                         ("home", "AA:BB:CC:00:00:03", "IoT", "smart_home")):
            self.add(mac)
            nd.apply_role(self.conn, mac, action, "Thing")
            d = bt_db.get_device(self.conn, mac)
            self.assertEqual((d["device_type"], d["role"], d["is_watchlisted"], d["is_notify"]), (dtype, role, 0, 0))

    def test_ignore_hides_without_naming(self) -> None:
        self.add()
        r = nd.apply_role(self.conn, MAC, "ignore")
        self.assertTrue(r["ok"])
        d = self.get()
        self.assertEqual((d["is_hidden"], d["friendly_name"]), (1, None))
        self.assertEqual(self.status(), ("new", "ignored"))

    def test_never_overwrites_an_existing_name(self) -> None:
        self.add(friendly_name="Dryer")
        r = nd.apply_role(self.conn, MAC, "phone", "Oops")
        self.assertEqual((r["ok"], r["reason"]), (False, "already_named"))
        self.assertEqual(self.get()["friendly_name"], "Dryer")

    def test_missing_device_and_bad_requests(self) -> None:
        self.assertEqual(nd.apply_role(self.conn, MAC, "phone", "X")["reason"], "gone")
        self.add()
        self.assertEqual(nd.apply_role(self.conn, MAC, "phone", "   ")["reason"], "bad_request")
        self.assertEqual(nd.apply_role(self.conn, MAC, "phone")["reason"], "bad_request")
        self.assertEqual(nd.apply_role(self.conn, MAC, "wizard", "X")["reason"], "bad_request")
        self.assertIsNone(self.get()["friendly_name"])

    def test_list_unnamed_connected(self) -> None:
        self.add("AA:BB:CC:00:00:01")                                       # listed
        self.add("AA:BB:CC:00:00:02", friendly_name="Named")                # named
        self.add("AA:BB:CC:00:00:03", state="LOST")                         # not connected
        self.add("AA:BB:CC:00:00:04", scan_type="BLE")                      # not WiFi
        self.add("AA:BB:CC:00:00:05", is_hidden=1)                          # hidden
        self.add("AA:BB:CC:00:00:06", linked_to="AA:BB:CC:00:00:02")        # linked secondary
        self.add("AA:BB:CC:00:00:07", friendly_name="")                     # empty name counts as unnamed
        macs = {d["mac_address"] for d in nd.list_unnamed_connected(self.conn)}
        self.assertEqual(macs, {"AA:BB:CC:00:00:01", "AA:BB:CC:00:00:07"})


class CleanupInteractionTests(DbCase):
    OLD = 400 * 86400

    def old_wifi(self, mac: str, scan_type: str = "WiFi") -> None:
        self.add(mac, scan_type=scan_type, state="LOST")
        self.conn.execute("UPDATE devices SET first_seen = ?, last_seen = ? WHERE mac_address = ?",
                          (time.time() - self.OLD, time.time() - self.OLD, mac))
        self.conn.commit()

    def purge(self):
        return bt_cleanup.run_cleanup(self.conn, bt_cleanup.Settings(backup_before_first_purge=False), force=True)

    def test_deleted_wifi_devices_are_remembered_so_they_are_not_new_when_they_return(self) -> None:
        self.old_wifi("AA:BB:CC:00:00:01")
        self.old_wifi("AA:BB:CC:00:00:02", scan_type="BLE")
        self.assertEqual(self.purge()["deleted"], 2)
        self.assertEqual(self.status("AA:BB:CC:00:00:01"), ("forgotten", "forgotten"))
        self.assertIsNone(self.status("AA:BB:CC:00:00:02"))   # BLE devices are never announced

    def test_the_delete_batch_is_still_one_transaction(self) -> None:
        """forget() runs between select and delete; it must not commit the batch early.

        Watches the real SQL: the batch's DELETE must happen after BEGIN IMMEDIATE
        and before the first COMMIT. (Checking conn.in_transaction is not enough:
        an early commit is followed by an INSERT that quietly opens a new one.)
        """
        self.old_wifi("AA:BB:CC:00:00:01")
        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)
        self.purge()
        self.conn.set_trace_callback(None)
        begin = next(i for i, sql in enumerate(statements) if sql.startswith("BEGIN IMMEDIATE"))
        delete = next(i for i, sql in enumerate(statements) if sql.startswith("DELETE FROM devices"))
        forgot = next(i for i, sql in enumerate(statements) if "'forgotten'" in sql)
        commit = next(i for i, sql in enumerate(statements) if i > begin and sql.startswith("COMMIT"))
        self.assertTrue(begin < forgot < delete < commit, statements[begin:commit + 1])

    def test_a_failure_while_remembering_rolls_the_whole_batch_back(self) -> None:
        self.old_wifi("AA:BB:CC:00:00:01")
        with mock.patch.object(nd, "forget", side_effect=sqlite3.OperationalError("disk I/O error")), \
             self.assertLogs("bt_cleanup", level="WARNING"):
            result = self.purge()
        self.assertEqual(result["deleted"], 0)
        self.assertIsNotNone(bt_db.get_device(self.conn, "AA:BB:CC:00:00:01"))


if __name__ == "__main__":
    unittest.main()
