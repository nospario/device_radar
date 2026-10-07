"""Telegram answers by person (phones only). Run: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bt_db  # noqa: E402
import bt_newdevice as nd  # noqa: E402
import bt_telegram as tg  # noqa: E402

CONFIG = {"person_aliases": {"richard": "Richard's iPhone", "laura": "Laura's iPhone"}}


class Case(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "bt_radar.db"
        bt_db.init_db(self.db)
        self.conn = bt_db.get_connection(self.db)
        self.addCleanup(self.conn.close)

    def add(self, mac, name, dtype, state="DETECTED", **cols):
        bt_db.upsert_device(self.conn, mac, advertised_name=name, scan_type="BLE", state=state)
        self.conn.execute("UPDATE devices SET friendly_name=?, device_type=?, state=?, last_seen=? WHERE mac_address=?",
                          (name, dtype, state, time.time() - 120, mac))
        for c, v in cols.items():
            self.conn.execute(f"UPDATE devices SET {c} = ? WHERE mac_address = ?", (v, mac))
        self.conn.commit()

    def household(self) -> None:
        self.add("AA:00:00:00:00:01", "Richard's iPhone", "iPhone")
        self.add("AA:00:00:00:00:02", "Mathilde's iPhone", "Phone", state="LOST")
        self.add("AA:00:00:00:00:03", "Laura's MacBook Pro", "Laptop")
        self.add("AA:00:00:00:00:04", "Dryer", "Smart Plug")

    async def ask(self, text: str) -> str:
        return await tg.answer_presence(text, CONFIG, self.db)


class WhoIsHomeTests(Case):
    async def test_answers_by_person_not_by_device(self) -> None:
        self.household()
        answer = await self.ask("who's home?")
        self.assertIn("Richard", answer)
        self.assertIn("\U0001f7e2 Richard", answer)
        self.assertIn("\U0001f534 Mathilde", answer)
        self.assertIn("⚪ Laura — no phone tracked", answer)
        self.assertNotIn("Dryer", answer)
        self.assertNotIn("MacBook", answer)
        self.assertIn("/devices", answer)

    async def test_falls_back_to_the_device_list_when_nobody_is_set_up(self) -> None:
        self.add("AA:00:00:00:00:09", "Dryer", "Smart Plug")
        with mock.patch.object(tg, "_api_get", mock.AsyncMock(return_value=[
                {"friendly_name": "Dryer", "mac_address": "AA:00:00:00:00:09", "last_seen": time.time()}])):
            answer = await tg.answer_presence("who's home", {}, self.db)
        self.assertIn("1 device(s) detected", answer)

    async def test_home_command_uses_the_people_summary(self) -> None:
        self.household()
        replies = []

        async def reply_text(text, **kw):
            replies.append(text)

        update = SimpleNamespace(message=SimpleNamespace(reply_text=reply_text), effective_chat=SimpleNamespace(id=1))
        with mock.patch.object(tg, "_is_authorized", lambda _id: True), \
             mock.patch.object(tg, "_get_db_path", lambda: self.db), \
             mock.patch.object(tg, "load_config", lambda: CONFIG):
            await tg._cmd_home(update, SimpleNamespace(args=[]))
        self.assertIn("Richard", replies[0])
        self.assertIn("no phone tracked", replies[0])


class PersonQuestionTests(Case):
    async def test_person_with_a_phone_is_answered_from_the_phone(self) -> None:
        self.household()
        answer = await self.ask("is Richard home?")
        self.assertIn("Richard's iPhone is home", answer)

    async def test_person_who_is_away(self) -> None:
        self.household()
        self.assertIn("Mathilde's iPhone is away", await self.ask("is Mathilde home?"))

    async def test_person_without_a_phone_is_not_answered_from_a_laptop(self) -> None:
        self.household()
        answer = await self.ask("is Laura home?")
        self.assertIn("No phone is tracked for Laura", answer)
        self.assertNotIn(" is home", answer)
        self.assertIn("Laura's MacBook Pro", answer)          # mentioned as known, but only phones count
        self.assertIn("/unnamed", answer)

    async def test_the_no_phone_message_is_neutral_about_pronouns(self) -> None:
        self.household()
        answer = (await self.ask("is Laura home?")).lower()
        for word in (" she ", " he ", " her ", " his ", " him "):
            self.assertNotIn(word, f" {answer} ")

    async def test_when_did_and_how_long_also_report_the_missing_phone(self) -> None:
        self.household()
        self.assertIn("No phone is tracked", await self.ask("when did Laura arrive?"))
        self.assertIn("No phone is tracked", await self.ask("how long has Laura been home?"))

    async def test_unknown_names_still_get_the_old_message(self) -> None:
        self.household()
        self.assertIn("I don't know who", await self.ask("is Zoe home?"))

    async def test_resolve_person_picks_the_phone_before_fuzzy_name_matching(self) -> None:
        self.add("AA:00:00:00:00:05", "Sam's MacBook", "Laptop")          # would win a fuzzy match
        self.add("AA:00:00:00:00:06", "Sam's iPhone", "Phone")
        dev = tg._resolve_person("sam", {}, self.conn)
        self.assertEqual(dev["friendly_name"], "Sam's iPhone")

    async def test_naming_a_phone_from_telegram_sets_the_person(self) -> None:
        bt_db.upsert_device(self.conn, "AA:00:00:00:00:07", advertised_name="iPhone.lan", scan_type="WiFi",
                            state="DETECTED", ip_address="192.168.1.70")
        nd.apply_role(self.conn, "AA:00:00:00:00:07", "phone", "Ava's iPhone")
        dev = bt_db.get_device(self.conn, "AA:00:00:00:00:07")
        self.assertEqual((dev["person"], dev["role"]), ("ava", "phone"))
        people = {p["person"]: p for p in __import__("bt_people").people_status(self.conn, {}, time.time())}
        self.assertEqual(people["ava"]["state"], "home")

    async def test_naming_something_that_is_not_possessive_leaves_person_empty(self) -> None:
        bt_db.upsert_device(self.conn, "AA:00:00:00:00:08", advertised_name="x", scan_type="WiFi", state="DETECTED")
        nd.apply_role(self.conn, "AA:00:00:00:00:08", "home", "Garden Camera")
        self.assertIsNone(bt_db.get_device(self.conn, "AA:00:00:00:00:08")["person"])


class FormatTests(unittest.TestCase):
    def test_summary_text(self) -> None:
        people = [
            {"person": "a", "display": "Amy", "state": "home", "since": time.time() - 3600, "last_seen": None},
            {"person": "b", "display": "Bob", "state": "away", "since": None, "last_seen": time.time() - 86400},
            {"person": "c", "display": "Cat", "state": "away", "since": time.time() - 7200, "last_seen": None},
            {"person": "d", "display": "Dan", "state": "no_phone", "since": None, "last_seen": None},
        ]
        text = tg.format_people_summary(people, 7)
        self.assertIn("Amy \u2014 arrived 1h ago", text)
        self.assertIn("Bob \u2014 last seen 1d ago", text)
        self.assertIn("Cat \u2014 left 2h ago", text)
        self.assertIn("Dan \u2014 no phone tracked", text)
        self.assertIn("7 device(s) connected in total", text)
        self.assertNotIn("&", text)  # plain text: nothing HTML-escaped


if __name__ == "__main__":
    unittest.main()
