"""Due-today task reminders (Alexa): the Master Task List reader and the reminder loop. (The Daily Habits reminders were removed on 13 Oct 2026.)"""

from __future__ import annotations

import asyncio
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bt_alexa  # noqa: E402
import bt_tasks  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 10, 13)

MASTER = """# Master task list
- [ ] Ring the dentist 📅 2026-10-13
- [ ] #task Look into splitting the PayPal payments ➕ 2026-10-12 📅 2026-10-13
- [x] Already done 📅 2026-10-13 ✅ 2026-10-13
- [ ] Overdue one 📅 2026-10-10
- [ ] Next week 📅 2026-10-20
- [ ] No date at all
- [ ] #habit Take vitamins 📅 2026-10-13
- [ ] [[Project X|Book the van]] ⏫ 📅 2026-10-13 🔁 every week
  - [ ] indented subtask 📅 2026-10-13
* not a task 📅 2026-10-13
"""


class ReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "MASTER TASK LIST.md"
        self.path.write_text(MASTER, encoding="utf-8")

    def due(self, **kw):
        return bt_tasks.get_todays_outstanding_tasks(self.path, today=TODAY, **kw)

    def test_only_unfinished_tasks_due_exactly_today(self) -> None:
        self.assertEqual(self.due(), ["Ring the dentist", "Look into splitting the PayPal payments", "Book the van", "indented subtask"])

    def test_the_text_is_cleaned_for_speaking(self) -> None:
        for task in self.due():
            self.assertNotRegex(task, r"[#\[\]📅➕⏫🔁✅]")

    def test_habit_tagged_lines_are_still_left_out(self) -> None:
        self.assertNotIn("Take vitamins", self.due())

    def test_the_tasks_the_pebble_sorter_adds_are_read(self) -> None:
        self.path.write_text("- [ ] #task Ring the garage about the MOT ➕ 2026-10-13 📅 2026-10-13\n", encoding="utf-8")
        self.assertEqual(self.due(), ["Ring the garage about the MOT"])

    def test_the_number_is_capped(self) -> None:
        self.path.write_text("".join(f"- [ ] Task {i} 📅 2026-10-13\n" for i in range(25)), encoding="utf-8")
        self.assertEqual(len(self.due()), 10)
        self.assertEqual(len(self.due(max_tasks=3)), 3)

    def test_a_missing_or_empty_file_is_no_tasks(self) -> None:
        self.assertEqual(bt_tasks.get_todays_outstanding_tasks(Path(self.tmp.name) / "nope.md", today=TODAY), [])
        self.path.write_text("", encoding="utf-8")
        self.assertEqual(self.due(), [])

    def test_windows_line_endings_and_a_missing_final_newline(self) -> None:
        self.path.write_bytes("- [ ] Ring the dentist 📅 2026-10-13\r\n- [ ] Pay the bill 📅 2026-10-13".encode())
        self.assertEqual(self.due(), ["Ring the dentist", "Pay the bill"])


class BundleTests(unittest.TestCase):
    def test_bundles_hold_at_most_the_given_number_in_order(self) -> None:
        tasks = [f"task {i}" for i in range(10)]
        bundles = bt_alexa._bundle_tasks(tasks, 4)
        self.assertEqual([len(b) for b in bundles], [4, 4, 2])
        self.assertEqual([t for b in bundles for t in b], tasks)
        self.assertEqual(bt_alexa._bundle_tasks([], 4), [])
        self.assertEqual(bt_alexa._bundle_tasks(["one"], 4), [["one"]])

    def test_the_plain_fallback_reads_every_task(self) -> None:
        self.assertEqual(bt_alexa._bundle_fallback(["Ring the dentist"]), "Richard, tasks due today: Ring the dentist.")
        text = bt_alexa._bundle_fallback(["Ring the dentist", "Book the van", "Pay the bill"])
        self.assertTrue(text.startswith("Richard, tasks due today: "))
        for task in ("Ring the dentist", "Book the van", "Pay the bill"):
            self.assertIn(task, text)

    def test_a_message_must_mention_the_tasks(self) -> None:
        bundle = ["Ring the dentist", "Book the van", "Renew the passport", "Pay the council tax"]
        self.assertTrue(bt_alexa._bundle_covered("Richard, ring the dentist, book the van, renew the passport and pay the council tax", bundle))
        self.assertTrue(bt_alexa._bundle_covered("Please ring the dentist, book the van and renew the passport", bundle))                 # 3 of 4
        self.assertFalse(bt_alexa._bundle_covered("You have a busy day, good luck", bundle))
        self.assertTrue(bt_alexa._bundle_covered("anything", ["Go"]))                                                                  # nothing long enough to check


class FakeClient:
    """httpx.AsyncClient stand-in answering with ``reply`` (or raising ``error``)."""
    reply = ""
    error: Exception | None = None
    last_json: dict = {}

    def __init__(self, *a, **k) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, timeout=None):
        FakeClient.last_json = json
        if FakeClient.error:
            raise FakeClient.error

        class R:
            def raise_for_status(self): pass
            def json(self_inner): return {"response": FakeClient.reply}
        return R()


class MessageTests(unittest.TestCase):
    def make(self, bundle, reply="", error=None, part=1, total=1):
        FakeClient.reply, FakeClient.error = reply, error
        with mock.patch.object(bt_alexa.httpx, "AsyncClient", FakeClient):
            return asyncio.run(bt_alexa._generate_bundle_message(bundle, part, total, {}))

    def test_a_good_model_message_is_used(self) -> None:
        msg = self.make(["Ring the dentist"], "Good morning Richard, do remember to ring the dentist today.")
        self.assertIn("ring the dentist", msg)

    def test_a_message_that_drops_the_task_is_replaced_by_the_plain_one(self) -> None:
        self.assertEqual(self.make(["Ring the dentist"], "Have a lovely day!"), "Richard, tasks due today: Ring the dentist.")

    def test_a_model_that_fails_gives_the_plain_message(self) -> None:
        self.assertEqual(self.make(["Ring the dentist"], error=RuntimeError("boom")), "Richard, tasks due today: Ring the dentist.")
        self.assertTrue(self.make(["Book the van"], error=bt_alexa.httpx.TimeoutException("slow")).endswith("Book the van."))

    def test_the_prompt_lists_the_tasks_and_mentions_no_habits(self) -> None:
        self.make(["Ring the dentist", "Book the van"], "ring the dentist and book the van", part=2, total=3)
        prompt = FakeClient.last_json["prompt"]
        self.assertIn("- Ring the dentist", prompt)
        self.assertIn("- Book the van", prompt)
        self.assertIn("message 2 of 3", prompt)
        self.assertNotIn("habit", prompt.lower())
        self.assertNotIn("(due today)", prompt)


class LoopTests(unittest.TestCase):
    """One cycle of ``run_task_reminder_loop`` with the database, the clock, the task file and Alexa faked."""

    def cycle(self, tasks_per_read, devices=None):
        spoken, stamped = [], []
        reads = iter(tasks_per_read)
        last = {"value": []}

        def read(path):
            try:
                last["value"] = next(reads)
            except StopIteration:
                pass
            return list(last["value"])

        sleeps = {"loop": 0}

        async def fake_sleep(delay):
            if delay == 60:
                sleeps["loop"] += 1
                if sleeps["loop"] > 1:
                    raise asyncio.CancelledError

        async def fake_speak(msg, config, device=None):
            spoken.append((device, msg))
            return True

        async def fake_message(bundle, i, n, config):
            return "SAY " + " | ".join(bundle)

        devices = devices if devices is not None else [{"device_name": "Office Echo", "last_tasks_message": 0, "tasks_interval": 90}]
        conn = mock.Mock()
        with mock.patch.object(bt_alexa.asyncio, "sleep", fake_sleep), mock.patch.object(bt_alexa.bt_db, "get_connection", return_value=conn), \
                mock.patch.object(bt_alexa.bt_db, "get_task_reminder_echo_devices", return_value=devices), \
                mock.patch.object(bt_alexa.bt_db, "update_echo_last_tasks_message", lambda c, name, ts: stamped.append(name)), \
                mock.patch.object(bt_alexa.bt_tasks, "get_todays_outstanding_tasks", read), mock.patch.object(bt_alexa, "speak", fake_speak), \
                mock.patch.object(bt_alexa, "_generate_bundle_message", fake_message):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(bt_alexa.run_task_reminder_loop({"alexa_enabled": True, "tasks_bundle_minutes": 0, "tasks_min_bundle_gap_seconds": 0}, Path("x.db")))
        return spoken, stamped

    def test_due_today_tasks_are_spoken_in_bundles_of_four(self) -> None:
        tasks = [f"task {i}" for i in range(6)]
        spoken, stamped = self.cycle([tasks, tasks, tasks])
        self.assertEqual([m for _, m in spoken], ["SAY task 0 | task 1 | task 2 | task 3", "SAY task 4 | task 5"])
        self.assertEqual({d for d, _ in spoken}, {"Office Echo"})
        self.assertEqual(stamped, ["Office Echo"])                                                  # stamped once, after the last bundle

    def test_a_task_ticked_off_mid_cycle_is_not_announced(self) -> None:
        # reads: the list at the start of the cycle, then a fresh read before each bundle (b was ticked off in Obsidian before the first)
        spoken, _ = self.cycle([["a", "b", "c", "d", "e"], ["a", "c", "d", "e"], ["a", "c", "d", "e"]])
        self.assertEqual([m for _, m in spoken], ["SAY a | c | d", "SAY e"])

    def test_a_whole_bundle_ticked_off_is_skipped(self) -> None:
        spoken, stamped = self.cycle([["a", "b", "c", "d", "e"], ["e"], ["e"]])
        self.assertEqual([m for _, m in spoken], ["SAY e"])
        self.assertEqual(stamped, ["Office Echo"])

    def test_no_tasks_means_silence_but_the_timer_is_reset(self) -> None:
        spoken, stamped = self.cycle([[]])
        self.assertEqual(spoken, [])
        self.assertEqual(stamped, ["Office Echo"])

    def test_a_device_that_was_reminded_recently_is_left_alone(self) -> None:
        import time
        spoken, stamped = self.cycle([["task 0"]], devices=[{"device_name": "Office Echo", "last_tasks_message": time.time() - 60, "tasks_interval": 90}])
        self.assertEqual((spoken, stamped), ([], []))

    def test_nothing_happens_when_alexa_is_off(self) -> None:
        sleeps = {"n": 0}

        async def fake_sleep(delay):
            sleeps["n"] += 1
            if sleeps["n"] > 2:
                raise asyncio.CancelledError
        with mock.patch.object(bt_alexa.asyncio, "sleep", fake_sleep), mock.patch.object(bt_alexa.bt_db, "get_connection") as conn:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(bt_alexa.run_task_reminder_loop({"alexa_enabled": False}, Path("x.db")))
        conn.assert_not_called()


class HabitsAreGoneTests(unittest.TestCase):
    def test_the_habit_code_no_longer_exists(self) -> None:
        for name in ("run_telegram_habit_reminder_loop", "run_telegram_habit_summary_loop", "_generate_habit_summary_message"):
            self.assertFalse(hasattr(bt_alexa, name), name)
        for name in ("get_daily_recurring_tasks", "complete_todays_habits", "get_all_habits", "set_habit_done_state", "habit_hash", "summarize_habits", "DEFAULT_DAILY_NOTES_DIR"):
            self.assertFalse(hasattr(bt_tasks, name), name)

    def test_nothing_else_in_the_code_mentions_habits(self) -> None:
        for path in sorted(ROOT.glob("bt_*.py")) + [ROOT / "templates" / "alexa.html", ROOT / "static" / "app.js"]:
            if path.name == "bt_tasks.py":
                continue                                                                              # keeps the #habit exclusion, explained there
            self.assertNotRegex(path.read_text(encoding="utf-8"), r"(?i)habit", path.name)

    def test_the_bot_has_no_habits_command(self) -> None:
        source = (ROOT / "bt_telegram.py").read_text(encoding="utf-8")
        self.assertNotIn('"habits"', source)
        self.assertNotIn("habit:", source)

    def test_the_scanner_starts_no_habit_loops(self) -> None:
        self.assertNotIn("habit", (ROOT / "bt_scanner.py").read_text(encoding="utf-8").lower())
        self.assertIn("run_task_reminder_loop", (ROOT / "bt_scanner.py").read_text(encoding="utf-8"))           # the due-today reminders are still started


if __name__ == "__main__":
    unittest.main()
