"""The phone going quiet overnight is time at home, not an outing (and not a 02:33 "leave")."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bt_presence as bp  # noqa: E402
from test_presence import DAY, NOW, S, TZ, out, ts, weekday_outings  # noqa: E402
from test_presence_integration import Household, HOUR  # noqa: E402


def sess(start, end, ok=True, gap=False):
    return bp.Session(start, end, ok, gap)


class MergeTests(unittest.TestCase):
    def merge(self, sessions, gaps=()):
        return bp.merge_night_quiet(sessions, list(gaps), TZ)

    def test_a_gap_in_the_small_hours_that_ends_in_the_morning_is_bridged(self) -> None:
        a = sess(ts(2026, 10, 10, 13, 56), ts(2026, 10, 11, 2, 34))            # arrived 02:30 .. departed 02:34 (the evening before's session ends 02:34)
        b = sess(ts(2026, 10, 11, 10, 25), ts(2026, 10, 11, 10, 35))
        merged, wakes = self.merge([a, b])
        self.assertEqual(len(merged), 1)
        self.assertEqual((merged[0].start, merged[0].end), (a.start, b.end))
        self.assertAlmostEqual(wakes[0], 10 + 25 / 60, places=3)

    def test_the_window_is_midnight_to_four_and_ends_before_noon(self) -> None:
        def result(h1, m1, h2, m2, d2=11):
            first = sess(ts(2026, 10, 10, 12), ts(2026, 10, 11, h1, m1))
            second = sess(ts(2026, 10, d2, h2, m2), ts(2026, 10, d2, 23, 0))
            return len(self.merge([first, second])[0])
        self.assertEqual(result(0, 0, 9, 0), 1)               # exactly midnight counts
        self.assertEqual(result(3, 59, 9, 0), 1)
        self.assertEqual(result(4, 0, 9, 0), 2)               # an early start (04:00) is a real departure
        self.assertEqual(result(23, 50, 9, 0, d2=12), 2)      # before midnight: the evening before
        self.assertEqual(result(2, 0, 11, 59), 1)
        self.assertEqual(result(2, 0, 12, 0), 2)              # back at noon or later: a real outing
        self.assertEqual(result(0, 10, 21, 35), 2)            # Mathilde's Thursday: out until 21:35

    def test_a_gap_that_runs_into_the_next_day_is_a_real_absence(self) -> None:
        first = sess(ts(2026, 10, 10, 12), ts(2026, 10, 11, 3, 0))
        second = sess(ts(2026, 10, 12, 8, 0), ts(2026, 10, 12, 20, 0))
        self.assertEqual(len(self.merge([first, second])[0]), 2)

    def test_scanner_downtime_and_untrusted_arrivals_are_never_bridged(self) -> None:
        first = sess(ts(2026, 10, 10, 12), ts(2026, 10, 11, 2, 34))
        second = sess(ts(2026, 10, 11, 10, 25), ts(2026, 10, 11, 18, 0))
        self.assertEqual(len(self.merge([first, second], gaps=[(ts(2026, 10, 11, 5), ts(2026, 10, 11, 6))])[0]), 2)     # the scanner was off in between
        self.assertEqual(len(self.merge([first, sess(second.start, second.end, ok=False)])[0]), 2)                          # arrival learned after a restart

    def test_open_sessions_and_the_spans_gap_flag(self) -> None:
        first = sess(ts(2026, 10, 10, 12), None)
        self.assertEqual(len(self.merge([first, sess(ts(2026, 10, 11, 10), None)])[0]), 2)                                   # still at home: no end to bridge from
        a = sess(ts(2026, 10, 10, 12), ts(2026, 10, 11, 2, 0), gap=False)
        b = sess(ts(2026, 10, 11, 9, 0), ts(2026, 10, 11, 9, 30), gap=True)
        merged, _ = self.merge([a, b])
        self.assertTrue(merged[0].spans_gap)                                                                                 # untrustworthy stays untrustworthy

    def test_two_quiet_nights_in_a_row_and_a_real_outing_between(self) -> None:
        sessions = [sess(ts(2026, 10, 3, 18), ts(2026, 10, 4, 1, 0)), sess(ts(2026, 10, 4, 9, 0), ts(2026, 10, 4, 12, 0)),
                    sess(ts(2026, 10, 4, 19, 0), ts(2026, 10, 5, 2, 0)), sess(ts(2026, 10, 5, 8, 0), ts(2026, 10, 5, 9, 0))]
        merged, wakes = self.merge(sessions)
        self.assertEqual(len(merged), 2)                                                                                     # (Sat 18:00 - Sun 12:00) and (Sun 19:00 - Mon 09:00); the afternoon in between is a real outing
        self.assertEqual(len(wakes), 2)
        self.assertEqual((merged[0].end, merged[1].start), (ts(2026, 10, 4, 12, 0), ts(2026, 10, 4, 19, 0)))

    def test_it_uses_local_time_across_the_clock_change(self) -> None:
        # the night the clocks go back (Sunday 25 October 2026, 02:00 BST -> 01:00 GMT)
        first = sess(ts(2026, 10, 24, 12), ts(2026, 10, 25, 3, 0))
        second = sess(ts(2026, 10, 25, 9, 0), ts(2026, 10, 25, 20, 0))
        self.assertEqual(len(self.merge([first, second])[0]), 1)

    def test_nothing_to_merge(self) -> None:
        self.assertEqual(self.merge([]), ([], []))
        one = [sess(ts(2026, 10, 10, 12), ts(2026, 10, 10, 20))]
        self.assertEqual(self.merge(one)[0], one)


class OutingTests(unittest.TestCase):
    def test_a_bridged_gap_is_not_an_outing_but_a_real_one_is(self) -> None:
        sessions = [sess(ts(2026, 10, 10, 8), ts(2026, 10, 10, 13, 56)),                     # home, then a real outing until 02:30
                    sess(ts(2026, 10, 11, 2, 30), ts(2026, 10, 11, 2, 34)),                    # got home, phone went quiet
                    sess(ts(2026, 10, 11, 10, 25), ts(2026, 10, 11, 17, 0)),
                    sess(ts(2026, 10, 11, 20, 0), None)]
        merged, _ = bp.merge_night_quiet(sessions, [], TZ)
        outings = bp.outings_from_sessions(merged, [], 3600)
        self.assertEqual([(o.leave, o.back) for o in outings], [(ts(2026, 10, 10, 13, 56), ts(2026, 10, 11, 2, 30)), (ts(2026, 10, 11, 17, 0), ts(2026, 10, 11, 20, 0))])
        unmerged = bp.outings_from_sessions(sessions, [], 3600)
        self.assertEqual(len(unmerged), 3)                                                       # without the bridge the quiet night is a 7.8 hour outing


class SettingsTests(unittest.TestCase):
    def test_config(self) -> None:
        self.assertTrue(bp.load_settings({}).night_quiet)
        self.assertFalse(bp.load_settings({"presence_night_quiet": False}).night_quiet)
        self.assertTrue(bp.load_settings({"presence_night_quiet": "no"}).night_quiet)          # not a boolean: ignored
        self.assertNotEqual(hash(bp.Settings(night_quiet=True)), hash(bp.Settings(night_quiet=False)))


class QuietHousehold(Household):
    """Sam: weekday outings, plus every few nights a late return after which the phone goes quiet until morning."""

    def setUp(self) -> None:
        super().setUp()
        for k in range(1, 40, 3):
            day = datetime.fromtimestamp(NOW, TZ).date() - timedelta(days=k)
            if day.weekday() >= 5:
                continue
            arrive = bp.hours_to_ts(day + timedelta(days=1), 2.5, TZ)                        # 02:30 the next morning
            self.add_event("arrived", arrive)
            self.add_event("departed", arrive + 240)                                          # phone quiet four minutes later
            self.add_event("arrived", bp.hours_to_ts(day + timedelta(days=1), 10.4, TZ))
            self.add_event("departed", bp.hours_to_ts(day + timedelta(days=1), 10.6, TZ) + 4 * HOUR)

    def analyse(self, cfg=None):
        bp._session_cache.clear()
        bp._backtest_cache.clear()
        return bp.analyse(self.conn, cfg or {}, NOW, TZ)["sam"]


class IntegrationTests(QuietHousehold):
    def test_quiet_nights_are_bridged_and_reported(self) -> None:
        on, off = self.analyse(), self.analyse({"presence_night_quiet": False})
        self.assertGreater(len(on.quiet_wakes), 3)
        self.assertEqual(len(off.quiet_wakes), 0)
        self.assertLess(len(on.outings), len(off.outings))
        self.assertTrue(any("overnight gap" in r for r in on.quality["reasons"]), on.quality["reasons"])
        self.assertEqual(on.quality["night_quiet"], len(on.quiet_wakes))
        self.assertFalse(any("overnight gap" in r for r in off.quality["reasons"]))

    def test_no_outing_starts_in_the_small_hours_any_more(self) -> None:
        night = lambda d: [o for o in d.outings if 24 <= bp.shifted(o.leave, TZ)[1] < 28]
        self.assertGreater(len(night(self.analyse({"presence_night_quiet": False}))), 0)
        self.assertEqual(len(night(self.analyse())), 0)

    def test_the_setting_takes_effect_without_clearing_caches(self) -> None:
        bp._session_cache.clear()
        a = bp.analyse(self.conn, {}, NOW, TZ)["sam"]
        b = bp.analyse(self.conn, {"presence_night_quiet": False}, NOW, TZ)["sam"]
        self.assertNotEqual(len(a.outings), len(b.outings))

    def test_the_report_shows_the_reason(self) -> None:
        bp._session_cache.clear()
        report = bp.build_report(self.conn, {}, NOW, TZ)
        (sam,) = report["persons"]
        self.assertGreater(sam["quality"]["night_quiet"], 3)


class LeavePredictionTests(unittest.TestCase):
    def test_in_the_evening_the_quiet_nights_are_no_longer_a_leave_at_2_33(self) -> None:
        # a weekend routine: out in the afternoon, home by 21:00; and quiet nights that used to look like "leaving at 02:33"
        outings = []
        for k in range(1, 120):
            day = datetime.fromtimestamp(NOW, TZ).date() - timedelta(days=k)
            if day.weekday() < 5:
                continue
            outings.append(out(bp.hours_to_ts(day, 14.0, TZ), bp.hours_to_ts(day, 18.0, TZ)))
            if k % 2:
                outings.append(out(bp.hours_to_ts(day + timedelta(days=1), 2.55, TZ), bp.hours_to_ts(day + timedelta(days=1), 10.4, TZ)))   # the old artefact
        sunday_evening = ts(2026, 10, 11, 19, 20)
        old = bp.predict_leave(outings, sunday_evening - 3600, sunday_evening, TZ, S)
        self.assertEqual(old["status"], "ready")
        self.assertAlmostEqual(bp.shifted(old["median"], TZ)[1], 26.6, delta=0.5)                  # "heads out around 02:33"
        cleaned = [o for o in outings if not 24 <= bp.shifted(o.leave, TZ)[1] < 28]
        self.assertEqual(bp.predict_leave(cleaned, sunday_evening - 3600, sunday_evening, TZ, S)["status"], "none_expected")
        text = bp.describe_prediction("Mathilde", {"kind": "leave", "status": "none_expected", "n": 20}, None, sunday_evening, TZ)
        self.assertEqual(text, "Mathilde doesn't usually go out again at this time of day.")


class LivePhoneQuietTests(unittest.TestCase):
    def data(self, wakes, leave_ts, sessions=None):
        s = sessions if sessions is not None else [sess(ts(2026, 10, 10, 8), leave_ts)]
        return bp.PersonData("sam", "Sam", [], "WiFi", 5, s, weekday_outings(60), "away", quiet_wakes=wakes, quality={"verdict": "usable"})

    def predict(self, data, now, gaps=()):
        return bp.person_prediction(data, list(gaps), now, TZ, S)

    def test_just_after_the_phone_went_quiet_in_the_small_hours(self) -> None:
        data = self.data([10.4, 10.5, 9.9, 10.6], ts(2026, 10, 11, 2, 34))
        pred = self.predict(data, ts(2026, 10, 11, 3, 10))
        self.assertEqual((pred["kind"], pred["status"], pred["n"]), ("return", "phone_quiet", 4))
        self.assertAlmostEqual(pred["usual_wake"], 10.45, delta=0.1)
        text = bp.describe_prediction("Sam", pred, None, ts(2026, 10, 11, 3, 10), TZ)
        self.assertIn("Sam's phone went quiet at 02:34", text)
        self.assertIn("usually been the phone asleep at home", text)
        self.assertIn("around 10:27 (from 4 nights)", text)

    def test_it_needs_a_few_quiet_nights_in_the_history(self) -> None:
        pred = self.predict(self.data([10.4, 10.5], ts(2026, 10, 11, 2, 34)), ts(2026, 10, 11, 3, 10))
        self.assertNotEqual(pred["status"], "phone_quiet")

    def test_not_for_an_evening_departure_or_when_the_morning_has_passed(self) -> None:
        evening = self.data([10.4] * 4, ts(2026, 10, 10, 22, 0), [sess(ts(2026, 10, 10, 8), ts(2026, 10, 10, 22, 0))])
        self.assertNotEqual(self.predict(evening, ts(2026, 10, 10, 23, 0))["status"], "phone_quiet")
        quiet = self.data([10.4] * 4, ts(2026, 10, 11, 2, 34))
        self.assertNotEqual(self.predict(quiet, ts(2026, 10, 11, 12, 30))["status"], "phone_quiet")                    # gone past noon: a real absence
        self.assertEqual(self.predict(quiet, ts(2026, 10, 11, 11, 59))["status"], "phone_quiet")

    def test_downtime_and_noisy_data_still_come_first(self) -> None:
        quiet = self.data([10.4] * 4, ts(2026, 10, 11, 2, 34))
        self.assertEqual(self.predict(quiet, ts(2026, 10, 11, 4, 0), gaps=[(ts(2026, 10, 11, 3, 0), ts(2026, 10, 11, 3, 30))])["status"], "unknown")
        noisy = self.data([10.4] * 4, ts(2026, 10, 11, 2, 34))
        noisy.quality = {"verdict": "noisy", "reasons": []}
        self.assertEqual(self.predict(noisy, ts(2026, 10, 11, 4, 0))["status"], "unreliable")

    def test_the_setting_turns_it_off(self) -> None:
        data = self.data([10.4] * 4, ts(2026, 10, 11, 2, 34))
        pred = bp.person_prediction(data, [], ts(2026, 10, 11, 3, 10), TZ, bp.Settings(night_quiet=False))
        self.assertNotEqual(pred["status"], "phone_quiet")


class MethodChoiceTests(unittest.TestCase):
    def choose(self, tod, dur):
        real = bp.backtest
        bp.backtest = lambda o, tz, s, method, min_history=None, boost=1.0: tod if method == "time_of_day" else dur
        try:
            return bp.choose_method([], TZ, S)[0]
        finally:
            bp.backtest = real

    def test_duration_must_cover_at_least_half_as_many_outings(self) -> None:
        tod = {"tests": 68, "median_error_min": 203, "hit_rate": 0.69}
        self.assertEqual(self.choose(tod, {"tests": 16, "median_error_min": 197, "hit_rate": 0.56}), "time_of_day")      # a lucky small sample
        self.assertEqual(self.choose(tod, {"tests": 40, "median_error_min": 150, "hit_rate": 0.7}), "duration")
        self.assertEqual(self.choose(tod, {"tests": 34, "median_error_min": 150, "hit_rate": 0.7}), "duration")           # exactly half
        self.assertEqual(self.choose(tod, {"tests": 33, "median_error_min": 150, "hit_rate": 0.7}), "time_of_day")
        self.assertEqual(self.choose(tod, {"tests": 40, "median_error_min": 210, "hit_rate": 0.7}), "time_of_day")        # not better


if __name__ == "__main__":
    unittest.main()
