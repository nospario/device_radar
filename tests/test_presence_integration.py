"""Telegram answers, late alerts and web endpoints built on presence analytics.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

os.environ["TZ"] = "Europe/London"          # the answers print local times; make them deterministic
time.tzset()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bt_db  # noqa: E402
import bt_presence as bp  # noqa: E402
import bt_telegram as tg  # noqa: E402
import bt_web  # noqa: E402
import test_presence as tp  # noqa: E402

TZ = ZoneInfo("Europe/London")
NOW = tp.NOW                                   # Wed 2026-10-07 10:00 local
HOUR, DAY = 3600, 86400
PHONE = "AA:00:00:00:00:01"


class Household(unittest.TestCase):
    """Sam: a steady weekday routine, left at 08:00 today. Kim: a laptop only."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "bt_radar.db"
        bt_db.init_db(self.db)
        self.conn = bt_db.get_connection(self.db)
        self.addCleanup(self.conn.close)
        bp._session_cache.clear()
        bp._backtest_cache.clear()
        self.add_phone("Sam's iPhone", state="LOST")
        self.add_events(tp.weekday_outings(90))
        self.add_event("departed", tp.ts(2026, 10, 7, 8, 0))
        bt_db.upsert_device(self.conn, "AA:00:00:00:00:09", advertised_name="Kim's MacBook", scan_type="WiFi", state="DETECTED")
        self.conn.execute("UPDATE devices SET friendly_name=\"Kim's MacBook\", device_type='Laptop' WHERE mac_address='AA:00:00:00:00:09'")
        self.conn.commit()

    def add_phone(self, name, state="DETECTED", mac=PHONE):
        bt_db.upsert_device(self.conn, mac, advertised_name=name, scan_type="WiFi", state=state)
        self.conn.execute("UPDATE devices SET friendly_name=?, device_type='Phone', state=?, last_seen=? WHERE mac_address=?",
                          (name, state, NOW - 60, mac))
        self.conn.commit()

    def add_event(self, kind, t, mac=PHONE):
        self.conn.execute("INSERT INTO events (mac_address, event_type, timestamp) VALUES (?, ?, ?)", (mac, kind, t))
        self.conn.commit()

    def add_events(self, outings, mac=PHONE):
        self.add_event("arrived", outings[0].leave - 12 * HOUR, mac)
        for o in outings:
            self.add_event("departed", o.leave, mac)
            self.add_event("arrived", o.back, mac)


class VisitorTests(Household):
    VISITOR = "AA:00:00:00:00:0F"

    def setUp(self) -> None:
        super().setUp()
        self.add_phone("Vic's iPhone", mac=self.VISITOR)
        self.add_events(tp.weekday_outings(90), mac=self.VISITOR)
        bp._session_cache.clear()

    def test_everyone_with_a_phone_is_reported_by_default(self) -> None:
        self.assertEqual(sorted(bp.build_report(self.conn, {}, NOW)["persons"][i]["person"] for i in (0, 1)), ["sam", "vic"])

    def test_a_visitor_is_left_out_of_reports_predictions_and_the_household(self) -> None:
        cfg = {"presence_visitors": ["Vic"]}
        report = bp.build_report(self.conn, cfg, NOW)
        self.assertEqual([p["person"] for p in report["persons"]], ["sam"])
        self.assertNotIn("vic", bp.analyse(self.conn, cfg, NOW))
        self.assertNotIn("vic", bp.eta_for_people(self.conn, cfg, NOW))
        self.assertEqual(report["household"]["people"], ["sam"])
        self.assertNotIn("Vic", report["untracked"])

    def test_a_visitor_still_shows_in_the_people_status_and_keeps_alerts(self) -> None:
        import bt_people
        cfg = {"presence_visitors": ["vic"]}
        self.assertIn("vic", [p["person"] for p in bt_people.people_status(self.conn, cfg, NOW)])
        self.conn.execute("UPDATE devices SET is_notify = 1 WHERE mac_address = ?", (self.VISITOR,))
        self.conn.commit()
        phone = dict(self.conn.execute("SELECT * FROM devices WHERE mac_address = ?", (self.VISITOR,)).fetchone())
        self.assertTrue(bt_people.notify_allowed(cfg, [phone]))

    def test_names_are_matched_loosely_and_bad_values_ignored(self) -> None:
        self.assertEqual(bp.visitors({"presence_visitors": [" VIC ", "", 5, None, "Ava"]}), {"vic", "ava"})
        for junk in (None, "vic", 5, {"vic": 1}):
            self.assertEqual(bp.visitors({"presence_visitors": junk}), set())
            self.assertIn("vic", bp.analyse(self.conn, {"presence_visitors": junk}, NOW))

    def test_changing_the_setting_takes_effect_at_once(self) -> None:
        self.assertIn("vic", bp.analyse(self.conn, {}, NOW))
        self.assertNotIn("vic", bp.analyse(self.conn, {"presence_visitors": ["vic"]}, NOW))
        self.assertIn("vic", bp.analyse(self.conn, {}, NOW))


class PredictiveAnswerTests(Household):
    def ask(self, text, now=NOW):
        return asyncio.run(tg.answer_presence(text, {}, self.db, now))

    def test_when_will_someone_be_home(self) -> None:
        answer = self.ask("when will Sam be home?")
        self.assertIn("Sam is expected home around 17:", answer)
        self.assertIn("80% chance between", answer)
        self.assertIn("usually gets home around 17:", answer)           # followed by the typical pattern

    def test_a_question_about_the_usual_time_leads_with_it(self) -> None:
        answer = self.ask("when does Sam usually get home?")
        self.assertTrue(answer.startswith("Sam usually gets home around"), answer)
        self.assertIn("expected home around", answer)

    def test_later_than_usual_is_said_plainly(self) -> None:
        answer = self.ask("when will Sam be back?", now=tp.ts(2026, 10, 7, 21, 0))
        self.assertIn("later than usual", answer)

    def test_leave_questions(self) -> None:
        answer = self.ask("when does Sam usually leave?")
        self.assertIn("Sam usually heads out around 08:", answer)
        self.assertIn("out at the moment", answer)

    def test_someone_who_is_already_home(self) -> None:
        self.add_event("arrived", tp.ts(2026, 10, 7, 9, 0))
        self.conn.execute("UPDATE devices SET state='DETECTED' WHERE mac_address=?", (PHONE,))
        self.conn.commit()
        answer = self.ask("when will Sam be home?")
        self.assertIn("Sam is already home", answer)

    def test_someone_with_no_phone_is_told_so(self) -> None:
        self.assertIn("No phone is tracked for Kim", self.ask("when will Kim be home?"))

    def test_a_visitor_is_not_predicted_in_chat_either(self) -> None:
        self.add_phone("Vic's iPhone", state="LOST", mac="AA:00:00:00:00:0F")
        answer = tg.predictive_answer(self.conn, {"presence_visitors": ["vic"]}, "vic", "when will they be home", NOW)
        self.assertEqual(answer, "Vic is a visitor, so I don't predict when they will be home.")

    def test_unknown_people_fall_through_to_the_old_answer(self) -> None:
        self.assertIn("I don't know who", self.ask("when will Zed be home?"))

    def test_little_history_is_admitted(self) -> None:
        self.conn.execute("DELETE FROM events")
        self.add_event("arrived", NOW - 5 * DAY)
        self.add_event("departed", tp.ts(2026, 10, 7, 8, 0))
        self.assertIn("Not enough history", self.ask("when will Sam be home?"))

    def test_questions_are_recognised_as_presence_queries(self) -> None:
        for q in ("when will Sam be home", "When is Sam usually home?", "when does Sam get home", "when will Sam leave"):
            self.assertTrue(tg.is_presence_query(q), q)
            self.assertEqual(tg._extract_person(q), "sam", q)
        self.assertEqual(tg._extract_person("when did Sam arrive"), "sam")           # the old question still works
        self.assertFalse(tg.is_presence_query("when will it rain"))

    def test_answers_never_use_gendered_pronouns(self) -> None:
        for q in ("when will Sam be home?", "when does Sam usually leave?", "when will Sam be back?", "when will Kim be home?"):
            for now in (NOW, tp.ts(2026, 10, 7, 21, 0)):
                text = f" {self.ask(q, now).lower()} "
                for word in (" he ", " she ", " his ", " her ", " him "):
                    self.assertNotIn(word, text)


class EtaCommandTests(Household):
    CONFIG: dict = {}

    def run_cmd(self, args):
        replies = []

        async def reply_text(text, **kw):
            replies.append(text)

        update = SimpleNamespace(message=SimpleNamespace(reply_text=reply_text), effective_chat=SimpleNamespace(id=1))
        patches = [mock.patch.object(tg, "_is_authorized", lambda _id: True), mock.patch.object(tg, "_get_db_path", lambda: self.db),
                   mock.patch.object(tg, "load_config", lambda: self.CONFIG), mock.patch("time.time", return_value=NOW)]
        for p in patches:
            p.start()
        try:
            asyncio.run(tg._cmd_eta(update, SimpleNamespace(args=args)))
        finally:
            for p in patches:
                p.stop()
        return replies

    def test_everyone_who_is_out(self) -> None:
        (text,) = self.run_cmd([])
        self.assertIn("Sam is expected home", text)

    def test_a_named_person(self) -> None:
        (text,) = self.run_cmd(["sam"])
        self.assertIn("Sam is expected home", text)

    def test_unknown_and_phoneless_names(self) -> None:
        self.assertIn("don't know who", self.run_cmd(["zed"])[0])
        self.assertIn("No phone is tracked for Kim", self.run_cmd(["kim"])[0])

    def test_visitors_are_left_out_of_the_list_and_not_predicted(self) -> None:
        self.add_phone("Vic's iPhone", state="LOST", mac="AA:00:00:00:00:0F")
        self.add_events(tp.weekday_outings(90), mac="AA:00:00:00:00:0F")
        self.add_event("departed", tp.ts(2026, 10, 7, 8, 0), mac="AA:00:00:00:00:0F")
        (everyone,) = self.run_cmd([])
        self.assertIn("Vic is expected home", everyone)
        type(self).CONFIG = {"presence_visitors": ["vic"]}
        try:
            (text,) = self.run_cmd([])
            self.assertIn("Sam is expected home", text)
            self.assertNotIn("Vic", text)
            self.assertIn("Vic is a visitor", self.run_cmd(["vic"])[0])
            self.assertNotIn("expected", self.run_cmd(["vic"])[0])
        finally:
            type(self).CONFIG = {}

    def test_when_only_a_visitor_is_out_nobody_is_listed(self) -> None:
        self.conn.execute("UPDATE devices SET state='DETECTED' WHERE mac_address=?", (PHONE,))
        self.conn.commit()
        self.add_phone("Vic's iPhone", state="LOST", mac="AA:00:00:00:00:0F")
        type(self).CONFIG = {"presence_visitors": ["vic"]}
        try:
            self.assertEqual(self.run_cmd([]), ["Everyone with a tracked phone is home."])
        finally:
            type(self).CONFIG = {}

    def test_when_everyone_is_home(self) -> None:
        self.conn.execute("UPDATE devices SET state='DETECTED' WHERE mac_address=?", (PHONE,))
        self.conn.commit()
        self.assertEqual(self.run_cmd([]), ["Everyone with a tracked phone is home."])


class LateAlertTests(Household):
    CONFIG = {"late_alerts_enabled": True, "late_alerts_people": ["sam"], "late_alerts_margin_minutes": 30}

    def run_once(self, now, config=None, send=None):
        send = send or mock.AsyncMock(return_value=True)
        lines = asyncio.run(bp.late_alerts_once(self.db, self.CONFIG if config is None else config, send, now, TZ))
        return lines, send

    def test_off_by_default(self) -> None:
        lines, send = self.run_once(tp.ts(2026, 10, 7, 23, 0), config={})
        self.assertEqual(lines, [])
        send.assert_not_awaited()

    def test_listing_people_alone_does_not_switch_it_on(self) -> None:
        for flag in ({}, {"late_alerts_enabled": False}, {"late_alerts_enabled": "yes"}):
            lines, send = self.run_once(tp.ts(2026, 10, 7, 23, 0), {"late_alerts_people": ["sam"], **flag})
            self.assertEqual(lines, [], flag)
            send.assert_not_awaited()

    def test_only_listed_people_are_watched(self) -> None:
        lines, send = self.run_once(tp.ts(2026, 10, 7, 23, 0), {**self.CONFIG, "late_alerts_people": ["someone-else"]})
        self.assertEqual(lines, [])

    def test_no_alert_while_the_usual_window_is_still_open(self) -> None:
        self.assertEqual(self.run_once(tp.ts(2026, 10, 7, 17, 30))[0], [])         # still within the usual range
        self.assertEqual(self.run_once(tp.ts(2026, 10, 7, 18, 10))[0], [])         # past the usual end, inside the 30 min margin

    def test_alerts_once_per_absence(self) -> None:
        lines, send = self.run_once(tp.ts(2026, 10, 7, 19, 0))
        self.assertEqual(len(lines), 1)
        self.assertIn("<b>Sam</b> is later than usual", lines[0])
        self.assertIn("normally home by 17:", lines[0])
        send.assert_awaited_once()
        again, send2 = self.run_once(tp.ts(2026, 10, 7, 20, 0))
        self.assertEqual(again, [])
        send2.assert_not_awaited()

    def test_a_new_absence_can_alert_again(self) -> None:
        self.run_once(tp.ts(2026, 10, 7, 19, 0))
        # home that evening, out again next day, late again
        self.add_event("arrived", tp.ts(2026, 10, 7, 19, 30))
        self.add_event("departed", tp.ts(2026, 10, 8, 8, 0))
        self.conn.execute("UPDATE devices SET state='LOST' WHERE mac_address=?", (PHONE,))
        self.conn.commit()
        bp._session_cache.clear()
        lines, _ = self.run_once(tp.ts(2026, 10, 8, 19, 0))
        self.assertEqual(len(lines), 1)

    def test_never_from_noisy_or_downtime_affected_data(self) -> None:
        self.conn.execute("INSERT INTO scanner_gaps (start, end) VALUES (?, ?)", (tp.ts(2026, 10, 7, 8, 30), tp.ts(2026, 10, 7, 9, 0)))
        self.conn.commit()
        self.assertEqual(self.run_once(tp.ts(2026, 10, 7, 23, 0))[0], [])         # the scanner was off after they left

    def test_dry_run_logs_and_sends_nothing(self) -> None:
        with self.assertLogs("bt_presence", level="INFO") as logs:
            lines, send = self.run_once(tp.ts(2026, 10, 7, 19, 0), {**self.CONFIG, "late_alerts_dry_run": True})
        self.assertEqual(len(lines), 1)
        send.assert_not_awaited()
        self.assertIn("[dry run]", logs.output[0])

    def test_a_failed_send_is_logged_but_not_repeated(self) -> None:
        with self.assertLogs("bt_presence", level="WARNING"):
            self.run_once(tp.ts(2026, 10, 7, 19, 0), send=mock.AsyncMock(return_value=False))
        self.assertEqual(self.run_once(tp.ts(2026, 10, 7, 20, 0))[0], [])


class WebEndpointTests(Household):
    def setUp(self) -> None:
        super().setUp()
        for name, value in (("get_db_path", lambda: self.db), ("load_config", lambda: {}),
                            ("AUTH_FILE", Path(self.tmp.name) / "none.json")):
            patcher = mock.patch.object(bt_web, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = bt_web.app.test_client()

    def test_reports_page_and_nav_link(self) -> None:
        html = self.client.get("/reports").get_data(as_text=True)
        self.assertIn('id="reports-root"', html)
        self.assertIn('href="/reports"', self.client.get("/").get_data(as_text=True))

    def test_reports_api_has_everything_the_page_draws(self) -> None:
        with mock.patch("time.time", return_value=NOW):
            report = self.client.get("/api/reports").get_json()
        (sam,) = report["persons"]
        for key in ("person", "display", "state", "quality", "typical", "daily", "timeline", "trend", "prediction",
                    "prediction_text", "typical_text", "backtest", "method"):
            self.assertIn(key, sam)
        self.assertEqual(len(sam["daily"]), 28)
        self.assertEqual(len(sam["timeline"]), 14)
        self.assertEqual(report["untracked"], ["Kim"])
        self.assertIn("household", report)

    def test_people_endpoint_now_carries_the_prediction(self) -> None:
        with mock.patch("time.time", return_value=NOW):
            people = {p["person"]: p for p in self.client.get("/api/people").get_json()}
        self.assertEqual(people["sam"]["prediction"]["status"], "ready")
        self.assertEqual(people["sam"]["prediction"]["kind"], "return")
        self.assertNotIn("prediction", people["kim"])               # no phone, nothing to predict

    def test_a_failure_in_the_analytics_does_not_break_the_people_strip(self) -> None:
        with mock.patch.object(bt_web.bt_presence, "eta_for_people", side_effect=RuntimeError("boom")), \
             self.assertLogs("bt_web", level="ERROR"):
            response = self.client.get("/api/people")
        self.assertEqual(response.status_code, 200)
        self.assertEqual({p["person"] for p in response.get_json()}, {"sam", "kim"})

    def test_reports_need_no_login_even_when_a_password_is_set(self) -> None:
        import bt_auth
        auth = Path(self.tmp.name) / "web_auth.json"
        bt_auth.set_password(auth, "correct horse battery")
        with mock.patch.object(bt_web, "AUTH_FILE", auth):
            self.assertEqual(self.client.get("/reports").status_code, 200)
            self.assertEqual(self.client.get("/api/reports").status_code, 200)


if __name__ == "__main__":
    unittest.main()
