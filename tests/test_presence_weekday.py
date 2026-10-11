"""Day-of-week weighting of arrival predictions: weighting, choosing it by testing, the Mon-Sun table, wording, settings."""

from __future__ import annotations

import random
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bt_presence as bp  # noqa: E402
from tests.test_presence import DAY, NOW, S, TZ, out, ts, weekday_outings  # noqa: E402


def routine(days: int, tuesday_back: float, other_back: float = 17.5, *, jitter=0.3, seed=3, end=NOW, leave_h=8.0) -> list[bp.Outing]:
    """One outing per weekday; Tuesdays are back at ``tuesday_back``, the other weekdays at ``other_back``."""
    rng = random.Random(seed)
    outings = []
    for k in range(days, 0, -1):
        day = (datetime.fromtimestamp(end, TZ) - timedelta(days=k)).date()
        if day.weekday() >= 5:
            continue
        back_h = tuesday_back if day.weekday() == 1 else other_back
        outings.append(out(bp.hours_to_ts(day, leave_h + rng.uniform(-jitter, jitter), TZ),
                           bp.hours_to_ts(day, back_h + rng.uniform(-jitter, jitter), TZ)))
    return outings


TUE_LEAVE, TUE_NOW = ts(2026, 10, 6, 8, 0), ts(2026, 10, 6, 10, 0)        # Tuesday 6 October 2026
WED_LEAVE, WED_NOW = ts(2026, 10, 7, 8, 0), ts(2026, 10, 7, 10, 0)


def predict(outings, leave, now, method="time_of_day", boost=1.0, settings=S):
    return bp.predict_return(outings, leave, now, TZ, settings, method, boost)


class NoChangeWithoutBoostTests(unittest.TestCase):
    def test_boost_one_is_exactly_the_old_prediction(self) -> None:
        history = routine(120, 19.0)
        for leave, now in ((TUE_LEAVE, TUE_NOW), (WED_LEAVE, WED_NOW), (WED_LEAVE, ts(2026, 10, 7, 17, 0))):
            for method in ("time_of_day", "duration"):
                a = predict(history, leave, now, method)
                b = predict(history, leave, now, method, 1.0)
                for key in ("status", "n", "median", "low", "high", "usual_high", "usual_median"):
                    self.assertEqual(a.get(key), b.get(key), (method, key))

    def test_the_default_call_has_no_boost(self) -> None:
        self.assertEqual(predict(routine(60, 19.0), TUE_LEAVE, TUE_NOW)["boost"], 1.0)


class WeightingTests(unittest.TestCase):
    def test_a_weekday_that_differs_pulls_the_prediction_towards_it(self) -> None:
        history = routine(160, 20.5)                                           # Tuesdays: 20:30, everything else 17:30
        plain = predict(history, TUE_LEAVE, TUE_NOW)
        boosted = predict(history, TUE_LEAVE, TUE_NOW, boost=8.0)
        self.assertEqual((plain["status"], boosted["status"]), ("ready", "ready"))
        plain_h, boosted_h = bp.shifted(plain["median"], TZ)[1], bp.shifted(boosted["median"], TZ)[1]
        self.assertLess(plain_h, 18.5)                                         # the pool of all weekdays is mostly 17:30
        self.assertGreater(boosted_h, 19.8)                                    # counting Tuesdays 8x gets close to 20:30
        self.assertGreater(boosted_h, plain_h + 1.5)

    def test_other_weekdays_are_hardly_affected_by_a_tuesday_pattern(self) -> None:
        history = routine(160, 20.5)
        wed = bp.shifted(predict(history, WED_LEAVE, WED_NOW, boost=8.0)["median"], TZ)[1]
        self.assertAlmostEqual(wed, 17.5, delta=0.6)

    def test_the_duration_method_is_weighted_too(self) -> None:
        history = routine(160, 20.5)
        a = bp.shifted(predict(history, TUE_LEAVE, TUE_NOW, "duration")["median"], TZ)[1]
        b = bp.shifted(predict(history, TUE_LEAVE, TUE_NOW, "duration", 8.0)["median"], TZ)[1]
        self.assertGreater(b, a + 1.0)

    def test_weekends_use_the_same_weekday_weighting_among_weekend_days(self) -> None:
        rng = random.Random(5)
        history = []
        for k in range(1, 170):
            day = (datetime.fromtimestamp(NOW, TZ) - timedelta(days=k)).date()
            if day.weekday() < 5:
                continue
            back = 21.0 if day.weekday() == 5 else 15.0                        # Saturdays late, Sundays early
            history.append(out(bp.hours_to_ts(day, 10.0, TZ), bp.hours_to_ts(day, back + rng.uniform(-0.2, 0.2), TZ)))
        sat = bp.shifted(predict(history, ts(2026, 10, 10, 10, 0), ts(2026, 10, 10, 11, 0), boost=8.0)["median"], TZ)[1]
        sun = bp.shifted(predict(history, ts(2026, 10, 11, 10, 0), ts(2026, 10, 11, 11, 0), boost=8.0)["median"], TZ)[1]
        self.assertGreater(sat, 19.5)
        self.assertLess(sun, 16.5)

    def test_the_weekday_is_the_leave_day_on_the_four_am_day(self) -> None:
        # an outing that leaves Monday 23:00 and returns Tuesday 01:00 belongs to Monday
        history = routine(120, 17.5)
        night = out(ts(2026, 9, 28, 23, 0), ts(2026, 9, 29, 1, 0))
        p = predict(history + [night], WED_LEAVE, WED_NOW)
        monday = predict(history + [night], ts(2026, 10, 5, 8, 0), ts(2026, 10, 5, 10, 0))
        self.assertEqual(p["weekday"], 2)                                      # Wednesday
        self.assertEqual(monday["weekday"], 0)
        self.assertEqual(shifted_weekday(night.leave), 0)

    def test_the_prediction_says_how_many_same_weekday_outings_it_used(self) -> None:
        history = routine(120, 19.0)
        p = predict(history, TUE_LEAVE, TUE_NOW, boost=4.0)
        expected = sum(1 for o in history if bp.shifted(o.leave, TZ)[0].weekday() == 1 and TUE_NOW - 180 * DAY <= o.back < TUE_LEAVE)
        self.assertEqual(p["n_same_day"], expected)
        self.assertEqual((p["boost"], p["weekday"]), (4.0, 1))

    def test_overdue_keeps_using_the_plain_pool_so_late_alerts_do_not_change(self) -> None:
        history = routine(160, 20.5)
        late = ts(2026, 10, 6, 23, 30)
        a = predict(history, TUE_LEAVE, late)
        b = predict(history, TUE_LEAVE, late, boost=8.0)
        self.assertEqual((a["status"], b["status"]), ("overdue", "overdue"))
        self.assertEqual((a["usual_high"], a["usual_median"]), (b["usual_high"], b["usual_median"]))

    def test_insufficient_history_is_still_insufficient(self) -> None:
        self.assertEqual(predict(weekday_outings(5), TUE_LEAVE, TUE_NOW, boost=8.0)["status"], "insufficient")


def shifted_weekday(ts_):
    return bp.shifted(ts_, TZ)[0].weekday()


class ChoosingTheWeightingTests(unittest.TestCase):
    def test_it_is_adopted_when_the_weekday_really_matters(self) -> None:
        history = routine(400, 20.5, seed=11, end=NOW)
        method, boost, bt, weighting = bp.choose_model(history, TZ, S)
        self.assertTrue(weighting["adopted"], weighting)
        self.assertGreater(boost, 1.0)
        self.assertGreaterEqual(weighting["agreeing"], 2)
        self.assertLess(bt["mean_error_min"], weighting["baseline"]["mean_error_min"] - 5)
        self.assertEqual(weighting["chosen"]["boost"], boost)

    def test_it_is_not_adopted_when_the_weekday_does_not_matter(self) -> None:
        history = weekday_outings(400, jitter=0.5, seed=4)
        method, boost, bt, weighting = bp.choose_model(history, TZ, S)
        self.assertEqual(boost, 1.0)
        self.assertFalse(weighting["adopted"])
        self.assertEqual(weighting["mode"], "auto")

    def test_too_few_tests_never_adopt_it(self) -> None:
        history = routine(25, 20.5)                                             # a few weeks only
        method, boost, bt, weighting = bp.choose_model(history, TZ, S)
        self.assertEqual(boost, 1.0)
        self.assertFalse(weighting["adopted"])

    def test_one_lucky_weighting_is_not_enough(self) -> None:
        history = routine(400, 20.5, seed=11)
        calls = {"n": 0}
        real = bp.backtest

        def fake(outings, tz, settings, method, min_history=None, boost=1.0):
            result = real(outings, tz, settings, method, min_history, boost)
            if boost != 16.0:                                                       # make all but the last weighting look no better than the baseline
                result = {**result, "mean_error_min": 10_000, "hit_rate": 0.0}
            return result
        bp.backtest = fake
        try:
            method, boost, bt, weighting = bp.choose_model(history, TZ, S)
        finally:
            bp.backtest = real
        self.assertEqual(boost, 1.0)
        self.assertEqual(weighting["agreeing"], 1)
        self.assertIn("best_alternative", weighting)

    def test_a_worse_hit_rate_blocks_it(self) -> None:
        history = routine(400, 20.5, seed=11)
        real = bp.backtest

        def fake(outings, tz, settings, method, min_history=None, boost=1.0):
            result = real(outings, tz, settings, method, min_history, boost)
            return {**result, "hit_rate": 0.1} if boost > 1 else result          # better error but a much worse window
        bp.backtest = fake
        try:
            _, boost, _, weighting = bp.choose_model(history, TZ, S)
        finally:
            bp.backtest = real
        self.assertEqual(boost, 1.0)
        self.assertFalse(weighting["adopted"])

    def test_forcing_and_turning_off(self) -> None:
        history = routine(200, 20.5)
        _, boost, _, w = bp.choose_model(history, TZ, bp.Settings(weekday_boost=3.0))
        self.assertEqual((boost, w["mode"], w["adopted"]), (3.0, "forced", True))
        _, boost, _, w = bp.choose_model(history, TZ, bp.Settings(weekday_boost=1.0))
        self.assertEqual((boost, w["mode"], w["adopted"]), (1.0, "off", False))

    def test_the_old_choose_method_still_works(self) -> None:
        method, bt = bp.choose_method(routine(200, 19.0), TZ, S)
        self.assertIn(method, ("time_of_day", "duration"))
        self.assertEqual(bt["boost"], 1.0)

    def test_the_backtest_with_a_boost_is_deterministic_and_reports_it(self) -> None:
        history = routine(200, 20.5)
        a = bp.backtest(history, TZ, S, "time_of_day", boost=4.0)
        self.assertEqual(a, bp.backtest(history, TZ, S, "time_of_day", boost=4.0))
        self.assertEqual(a["boost"], 4.0)


class SettingsTests(unittest.TestCase):
    def test_config_values(self) -> None:
        self.assertIsNone(bp.load_settings({}).weekday_boost)
        self.assertIsNone(bp.load_settings({"presence_weekday_boost": "auto"}).weekday_boost)
        self.assertEqual(bp.load_settings({"presence_weekday_boost": 3}).weekday_boost, 3.0)
        self.assertEqual(bp.load_settings({"presence_weekday_boost": 1}).weekday_boost, 1.0)
        for bad in (0, 0.5, 21, -2, True, "lots", None, [4]):
            self.assertIsNone(bp.load_settings({"presence_weekday_boost": bad}).weekday_boost, bad)

    def test_settings_are_part_of_the_cache_key(self) -> None:
        self.assertNotEqual(hash(bp.Settings(weekday_boost=2.0)), hash(bp.Settings(weekday_boost=4.0)))
        self.assertEqual(hash(bp.Settings()), hash(bp.Settings()))


class WeekdayTableTests(unittest.TestCase):
    def test_counts_and_typical_times_per_weekday(self) -> None:
        history = routine(120, 20.5)
        table = bp.weekday_table(history, TZ, NOW, S)
        self.assertEqual([r["name"] for r in table], ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])
        by = {r["name"]: r for r in table}
        self.assertEqual((by["Saturday"]["n"], by["Sunday"]["n"]), (0, 0))
        self.assertFalse(by["Saturday"]["return"]["enough"])
        self.assertGreaterEqual(by["Tuesday"]["n"], 10)
        self.assertAlmostEqual(by["Tuesday"]["return"]["median"], 20.5, delta=0.4)
        self.assertAlmostEqual(by["Wednesday"]["return"]["median"], 17.5, delta=0.4)
        self.assertAlmostEqual(by["Tuesday"]["leave"]["median"], 8.0, delta=0.4)
        self.assertEqual(sum(r["n"] for r in table), len(history) - sum(1 for o in history if o.back < NOW - 180 * DAY))

    def test_old_outings_are_left_out(self) -> None:
        ancient = routine(100, 20.5, end=NOW - 300 * DAY)
        self.assertEqual(sum(r["n"] for r in bp.weekday_table(ancient, TZ, NOW, S)), 0)

    def test_a_late_return_after_midnight_belongs_to_the_leave_day(self) -> None:
        history = [out(ts(2026, 10, 2, 20, 0), ts(2026, 10, 3, 1, 0)) for _ in range(1)]                  # Friday evening to Saturday 01:00
        row = {r["name"]: r for r in bp.weekday_table(history, TZ, NOW, S)}
        self.assertEqual((row["Friday"]["n"], row["Saturday"]["n"]), (1, 0))
        self.assertAlmostEqual(row["Friday"]["return"]["median"] if row["Friday"]["return"].get("enough") else 25.0, 25.0, delta=0.1)

    def test_empty_history(self) -> None:
        self.assertTrue(all(r["n"] == 0 for r in bp.weekday_table([], TZ, NOW, S)))


class WordingTests(unittest.TestCase):
    def pred(self, boost, n_same=4):
        return {"status": "ready", "kind": "return", "n": 30, "median": TUE_NOW + 4 * 3600, "low": TUE_NOW + 3 * 3600, "high": TUE_NOW + 6 * 3600,
                "method": "time_of_day", "boost": boost, "weekday": 1, "n_same_day": n_same, "leave_ts": TUE_LEAVE}

    def say(self, pred):
        return bp.describe_prediction("Lilou", pred, None, TUE_NOW, TZ)

    def test_a_weighted_prediction_says_so(self) -> None:
        text = self.say(self.pred(3.0))
        self.assertIn("from 30 similar weekday outings, counting the 4 past Tuesdays 3 times over.", text)

    def test_an_unweighted_one_says_the_weekday_is_not_counted_extra(self) -> None:
        text = self.say(self.pred(1.0))
        self.assertIn("(Tuesdays are not counted extra yet).", text)

    def test_one_outing_and_no_outings(self) -> None:
        self.assertIn("counting the 1 past Tuesday 3 times over", self.say(self.pred(3.0, 1)))
        self.assertNotIn("counted extra", self.say(self.pred(1.0, 0)))                                       # nothing to say about a weekday with no history
        self.assertNotIn("times over", self.say(self.pred(3.0, 0)))

    def test_old_predictions_without_the_new_fields_still_read_fine(self) -> None:
        old = {"status": "ready", "kind": "return", "n": 30, "median": TUE_NOW + 4 * 3600, "low": TUE_NOW + 3 * 3600, "high": TUE_NOW + 6 * 3600, "leave_ts": TUE_LEAVE}
        text = self.say(old)
        self.assertTrue(text.endswith("similar weekday outings."))


if __name__ == "__main__":
    unittest.main()
