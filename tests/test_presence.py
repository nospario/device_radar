"""Tests for presence analytics (sessions, reports, predictions). Run: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import random
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bt_db  # noqa: E402
import bt_presence as bp  # noqa: E402

TZ = ZoneInfo("Europe/London")
S = bp.Settings()
HOUR, DAY = 3600, 86400


def ts(y, mo, d, h=0, mi=0) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp()


NOW = ts(2026, 10, 7, 10, 0)           # a Wednesday morning


def out(leave, back) -> bp.Outing:
    return bp.Outing(leave, back)


def weekday_outings(days: int, *, leave_h=8.0, back_h=17.5, jitter=0.4, seed=1, end=NOW) -> list[bp.Outing]:
    """One outing per weekday going back ``days`` days before ``end``, around the given times."""
    rng = random.Random(seed)
    outings = []
    for k in range(days, 0, -1):
        day = (datetime.fromtimestamp(end, TZ) - timedelta(days=k)).date()
        if day.weekday() >= 5:
            continue
        leave = bp.hours_to_ts(day, leave_h + rng.uniform(-jitter, jitter), TZ)
        back = bp.hours_to_ts(day, back_h + rng.uniform(-jitter, jitter), TZ)
        outings.append(out(leave, back))
    return outings


class TimeHelperTests(unittest.TestCase):
    def test_day_starts_at_four_am(self) -> None:
        self.assertEqual(bp.shifted(ts(2026, 10, 7, 0, 30), TZ), (date(2026, 10, 6), 24.5))
        self.assertEqual(bp.shifted(ts(2026, 10, 7, 3, 59), TZ)[0], date(2026, 10, 6))
        self.assertEqual(bp.shifted(ts(2026, 10, 7, 4, 0), TZ), (date(2026, 10, 7), 4.0))
        self.assertEqual(bp.shifted(ts(2026, 10, 7, 18, 40), TZ)[1], 18 + 40 / 60)

    def test_friday_night_belongs_to_friday(self) -> None:
        day, _ = bp.shifted(ts(2026, 10, 10, 0, 30), TZ)         # Saturday 00:30
        self.assertEqual((day, bp.day_type(day)), (date(2026, 10, 9), bp.WEEKDAY))

    def test_day_types(self) -> None:
        self.assertEqual(bp.day_type(date(2026, 10, 9)), bp.WEEKDAY)     # Friday
        self.assertEqual(bp.day_type(date(2026, 10, 10)), bp.WEEKEND)    # Saturday

    def test_fmt_hours(self) -> None:
        self.assertEqual(bp.fmt_hours(18 + 40 / 60), "18:40")
        self.assertEqual(bp.fmt_hours(24.5), "00:30")
        self.assertEqual(bp.fmt_hours(5.9999), "06:00")
        self.assertEqual(bp.fmt_hours(27.99), "03:59")           # 27h 59.4m
        self.assertEqual(bp.fmt_hours(27.999), "04:00")

    def test_round_trip(self) -> None:
        t = ts(2026, 10, 7, 18, 40)
        day, hours = bp.shifted(t, TZ)
        self.assertAlmostEqual(bp.hours_to_ts(day, hours, TZ), t, delta=1)
        late = ts(2026, 10, 8, 0, 30)
        day, hours = bp.shifted(late, TZ)
        self.assertAlmostEqual(bp.hours_to_ts(day, hours, TZ), late, delta=1)


class StatsTests(unittest.TestCase):
    def test_quantile(self) -> None:
        self.assertEqual(bp.quantile([5], 0.5), 5)
        self.assertEqual(bp.quantile([1, 2, 3, 4, 5], 0.5), 3)
        self.assertEqual(bp.quantile([1, 2, 3, 4], 0.5), 2.5)
        self.assertEqual(bp.quantile([4, 1, 3, 2, 5], 0.0), 1)
        self.assertEqual(bp.quantile([4, 1, 3, 2, 5], 1.0), 5)
        self.assertAlmostEqual(bp.quantile([0, 10], 0.25), 2.5)

    def test_weighted_quantile_with_equal_weights_matches_the_plain_median(self) -> None:
        pairs = [(v, 1.0) for v in (1, 2, 3, 4, 5)]
        self.assertAlmostEqual(bp.weighted_quantile(pairs, 0.5), 3)
        self.assertAlmostEqual(bp.weighted_quantile([(7, 2.0)], 0.9), 7)

    def test_weights_pull_the_answer_towards_heavy_points(self) -> None:
        light = bp.weighted_quantile([(10, 1), (20, 1), (30, 1)], 0.5)
        heavy_high = bp.weighted_quantile([(10, 1), (20, 1), (30, 8)], 0.5)
        heavy_low = bp.weighted_quantile([(10, 8), (20, 1), (30, 1)], 0.5)
        self.assertEqual(light, 20)
        self.assertGreater(heavy_high, 25)
        self.assertLess(heavy_low, 15)

    def test_weighted_quantiles_are_ordered_and_within_range(self) -> None:
        rng = random.Random(3)
        pairs = [(rng.uniform(0, 100), rng.uniform(0.1, 2)) for _ in range(50)]
        q = [bp.weighted_quantile(pairs, x) for x in (0.1, 0.5, 0.9)]
        self.assertEqual(q, sorted(q))
        self.assertTrue(min(v for v, _ in pairs) <= q[0] and q[2] <= max(v for v, _ in pairs))

    def test_summarise(self) -> None:
        self.assertEqual(bp.summarise([1, 2, 3], 8), {"n": 3, "enough": False})
        s = bp.summarise([float(v) for v in range(1, 11)], 8)
        self.assertTrue(s["enough"])
        self.assertEqual((s["n"], s["median"]), (10, 5.5))
        self.assertLess(s["p10"], s["q1"])
        self.assertLess(s["q3"], s["p90"])


class SessionTests(unittest.TestCase):
    def sessions(self, events, gaps=(), flap=20 * 60, now=NOW):
        return bp.build_sessions(events, list(gaps), flap, now)

    def test_arrive_depart_pairs(self) -> None:
        s = self.sessions([("A", "arrived", 1000), ("A", "departed", 5000)])
        self.assertEqual([(x.start, x.end) for x in s], [(1000, 5000)])

    def test_flicker_shorter_than_the_window_is_merged_but_longer_absences_are_kept(self) -> None:
        events = [("A", "arrived", 0), ("A", "departed", 3600), ("A", "arrived", 3600 + 600), ("A", "departed", 7200),
                  ("A", "arrived", 7200 + 3 * 3600), ("A", "departed", 7200 + 4 * 3600)]
        s = self.sessions(events)
        self.assertEqual([(x.start, x.end) for x in s], [(0, 7200), (7200 + 3 * 3600, 7200 + 4 * 3600)])

    def test_an_absence_exactly_at_the_window_is_kept(self) -> None:
        events = [("A", "arrived", 0), ("A", "departed", 100), ("A", "arrived", 100 + 20 * 60), ("A", "departed", 5000)]
        self.assertEqual(len(self.sessions(events)), 2)

    def test_several_devices_are_one_person(self) -> None:
        events = [("BLE", "arrived", 0), ("WIFI", "arrived", 50), ("BLE", "departed", 500), ("WIFI", "departed", 4000)]
        s = self.sessions(events)
        self.assertEqual([(x.start, x.end) for x in s], [(0, 4000)])

    def test_one_device_dropping_out_while_another_stays_is_not_an_absence(self) -> None:
        events = [("BLE", "arrived", 0), ("WIFI", "arrived", 10), ("BLE", "departed", 100), ("BLE", "arrived", 90000),
                  ("WIFI", "departed", 91000), ("BLE", "departed", 92000)]
        self.assertEqual(len(self.sessions(events)), 1)

    def test_ongoing_session_has_no_end(self) -> None:
        s = self.sessions([("A", "arrived", 1000)])
        self.assertIsNone(s[0].end)
        s = self.sessions([("A", "arrived", 0), ("A", "departed", 100), ("A", "arrived", 200)])
        self.assertEqual((s[0].start, s[0].end), (0, None))        # merged: 100s of flicker, still home

    def test_duplicate_and_orphan_events_are_ignored(self) -> None:
        events = [("A", "departed", 10), ("A", "arrived", 100), ("A", "arrived", 150), ("A", "departed", 900),
                  ("A", "departed", 950)]
        s = self.sessions(events)
        self.assertEqual([(x.start, x.end) for x in s], [(100, 900)])

    def test_unsorted_events_are_sorted(self) -> None:
        s = self.sessions([("A", "departed", 900), ("A", "arrived", 100)])
        self.assertEqual([(x.start, x.end) for x in s], [(100, 900)])

    def test_arrival_just_after_the_scanner_restarted_is_flagged(self) -> None:
        gaps = [(1000.0, 5000.0)]
        s = self.sessions([("A", "arrived", 5060), ("A", "departed", 9000)], gaps)
        self.assertFalse(s[0].start_ok)
        s = self.sessions([("A", "arrived", 5000 + 3600), ("A", "departed", 20000)], gaps)
        self.assertTrue(s[0].start_ok)

    def test_a_session_spanning_scanner_downtime_is_flagged(self) -> None:
        gaps = [(1000.0, 5000.0)]
        s = self.sessions([("A", "arrived", 100), ("A", "departed", 5100)], gaps)
        self.assertTrue(s[0].spans_gap)
        s = self.sessions([("A", "arrived", 100), ("A", "departed", 900)], gaps)
        self.assertFalse(s[0].spans_gap)

    def test_ongoing_session_across_downtime_is_flagged(self) -> None:
        s = self.sessions([("A", "arrived", NOW - 3 * DAY)], [(NOW - 2 * DAY, NOW - DAY)])
        self.assertTrue(s[0].spans_gap)


class OutingTests(unittest.TestCase):
    def test_only_long_reliable_absences_are_outings(self) -> None:
        sessions = [bp.Session(0, 1000), bp.Session(1000 + 30 * 60, 5000),            # 30 min: too short
                    bp.Session(5000 + 2 * HOUR, 9000), bp.Session(9000 + 5 * HOUR, None)]
        outings = bp.outings_from_sessions(sessions, [], 3600)
        self.assertEqual([(o.leave, o.back) for o in outings], [(5000, 5000 + 2 * HOUR), (9000, 9000 + 5 * HOUR)])

    def test_absences_touching_downtime_are_not_outings(self) -> None:
        sessions = [bp.Session(0, 1000), bp.Session(1000 + 3 * HOUR, 20000)]
        self.assertEqual(len(bp.outings_from_sessions(sessions, [], 3600)), 1)
        self.assertEqual(bp.outings_from_sessions(sessions, [(2000.0, 3000.0)], 3600), [])

    def test_unreliable_sessions_do_not_start_or_end_an_outing(self) -> None:
        a = bp.Session(0, 1000, spans_gap=True)                       # its end is not trustworthy
        b = bp.Session(1000 + 3 * HOUR, 30000, start_ok=False)        # arrival was only noticed after a restart
        c = bp.Session(30000 + 3 * HOUR, 50000)
        # a -> b is spoiled both ways (a's departure and b's arrival are unreliable); b -> c is fine,
        # because b's own departure and c's arrival are reliable
        self.assertEqual(bp.outings_from_sessions([a, b, c], [], 3600), [out(30000, 30000 + 3 * HOUR)])
        # an unreliable arrival spoils the outing *before* it
        self.assertEqual(bp.outings_from_sessions([bp.Session(0, 1000), b], [], 3600), [])

    def test_still_away_or_still_home_gives_no_outing(self) -> None:
        self.assertEqual(bp.outings_from_sessions([bp.Session(0, None)], [], 3600), [])
        self.assertEqual(bp.outings_from_sessions([], [], 3600), [])


class TypicalTimesTests(unittest.TestCase):
    def test_weekday_and_weekend_are_separate(self) -> None:
        wd = [out(ts(2026, 10, 5 + i, 8, 0), ts(2026, 10, 5 + i, 17, 30)) for i in range(5)] * 2        # Mon-Fri x2
        we = [out(ts(2026, 10, 10 + i, 11, 0), ts(2026, 10, 10 + i, 19, 0)) for i in range(2)] * 5       # Sat/Sun x5
        t = bp.typical_times(wd + we, TZ, 8)
        self.assertEqual(t["weekday"]["leave"]["median"], 8.0)
        self.assertEqual(t["weekday"]["return"]["median"], 17.5)
        self.assertEqual(t["weekend"]["leave"]["median"], 11.0)
        self.assertEqual(t["weekday"]["return"]["n"], 10)
        self.assertAlmostEqual(t["weekday"]["hours_out"]["median"], 9.5)

    def test_too_few_samples_say_so(self) -> None:
        t = bp.typical_times([out(ts(2026, 10, 5, 8), ts(2026, 10, 5, 17))], TZ, 8)
        self.assertEqual(t["weekday"]["return"], {"n": 1, "enough": False})

    def test_a_friday_night_return_after_midnight_is_a_weekday_return_after_24h(self) -> None:
        fri_night = [out(ts(2026, 10, 9, 19, 0), ts(2026, 10, 10, 0, 30)) for _ in range(8)]
        t = bp.typical_times(fri_night, TZ, 8)
        self.assertEqual(t["weekday"]["leave"]["median"], 19.0)
        self.assertEqual(t["weekday"]["return"]["median"], 24.5)
        self.assertEqual(t["weekend"]["return"]["n"], 0)


class DailyHoursAndTimelineTests(unittest.TestCase):
    def test_counts_only_observed_time_and_flags_partial_days(self) -> None:
        sessions = [bp.Session(ts(2026, 10, 5, 0, 0), ts(2026, 10, 5, 8, 0)), bp.Session(ts(2026, 10, 5, 18, 0), None)]
        gaps = [(ts(2026, 10, 6, 0, 0), ts(2026, 10, 6, 12, 0))]
        rows = {r["date"]: r for r in bp.daily_home_hours(sessions, gaps, TZ, ts(2026, 10, 7, 12, 0), days=4)}
        mon = rows["2026-10-05"]
        self.assertEqual((mon["home_h"], mon["observed_h"], mon["partial"]), (8 + 6, 24.0, False))
        tue = rows["2026-10-06"]
        self.assertEqual((tue["observed_h"], tue["partial"]), (12.0, True))          # half the day unobserved
        self.assertEqual(tue["home_h"], 12.0)                                          # at home for the observed half
        wed = rows["2026-10-07"]
        self.assertEqual((wed["observed_h"], wed["home_h"]), (12.0, 12.0))             # today only up to 'now'

    def test_returns_exactly_the_requested_number_of_days(self) -> None:
        self.assertEqual(len(bp.daily_home_hours([], [], TZ, NOW, days=28)), 28)

    def test_timeline_segments_are_hours_within_the_day(self) -> None:
        sessions = [bp.Session(ts(2026, 10, 5, 22, 0), ts(2026, 10, 6, 7, 30))]
        gaps = [(ts(2026, 10, 6, 12, 0), ts(2026, 10, 6, 14, 0))]
        rows = {r["date"]: r for r in bp.timeline(sessions, gaps, TZ, ts(2026, 10, 6, 20, 0), days=3)}
        self.assertEqual(rows["2026-10-05"]["home"], [[22.0, 24.0]])
        self.assertEqual(rows["2026-10-06"]["home"], [[0.0, 7.5]])
        self.assertEqual(rows["2026-10-06"]["unknown"], [[12.0, 14.0]])
        self.assertEqual(rows["2026-10-06"]["future_from"], 20.0)
        for row in rows.values():
            for a, b in row["home"] + row["unknown"]:
                self.assertTrue(0 <= a < b <= 24)


class HouseholdTests(unittest.TestCase):
    def test_empty_periods_and_typical_times(self) -> None:
        # two people with overlapping school/work days: nobody in 09:00-15:00 on weekdays
        a, b = [], []
        for d in range(5, 10):                                           # Mon 5 - Fri 9 Oct
            a += [bp.Session(ts(2026, 10, d - 1, 20, 0) if d > 5 else ts(2026, 10, 4, 6, 0), ts(2026, 10, d, 8, 30)),]
            b += [bp.Session(ts(2026, 10, d, 0, 0), ts(2026, 10, d, 9, 0))]
            a.append(bp.Session(ts(2026, 10, d, 15, 30), ts(2026, 10, d, 23, 0)))
            b.append(bp.Session(ts(2026, 10, d, 15, 0), ts(2026, 10, d, 23, 30)))
        sess = {"a": sorted(a, key=lambda s: s.start), "b": sorted(b, key=lambda s: s.start)}
        h = bp.household_empty(sess, [], TZ, ts(2026, 10, 12, 12, 0), 3600, 3, window_days=30)
        self.assertEqual(h["people"], ["a", "b"])
        self.assertGreaterEqual(h["periods"], 4)
        start = h["weekday"]["empty_from"]
        self.assertTrue(start["enough"])
        self.assertAlmostEqual(start["median"], 9.0, delta=0.6)
        self.assertAlmostEqual(h["weekday"]["empty_until"]["median"], 15.0, delta=0.6)

    def test_no_people_gives_an_empty_result(self) -> None:
        self.assertIsNone(bp.household_empty({}, [], TZ, NOW, 3600, 3)["empty_h_per_day"])

    def test_time_before_anyone_was_tracked_is_not_counted_as_empty(self) -> None:
        sess = {"a": [bp.Session(NOW - 3 * DAY, None)]}
        h = bp.household_empty(sess, [], TZ, NOW, 3600, 3, window_days=90)
        self.assertEqual(h["periods"], 0)
        self.assertAlmostEqual(h["observed_days"], 3.0, delta=0.1)

    def test_scanner_downtime_is_not_counted_as_empty(self) -> None:
        sess = {"a": [bp.Session(NOW - 10 * DAY, NOW - 9 * DAY), bp.Session(NOW - 2 * DAY, None)]}
        gaps = [(NOW - 9 * DAY + 600, NOW - 2 * DAY - 600)]
        h = bp.household_empty(sess, gaps, TZ, NOW, 3600, 3, window_days=30)
        self.assertEqual(h["periods"], 0)


class TrendTests(unittest.TestCase):
    def history(self, recent_back: float, before_back: float, n: int = 12) -> list[bp.Outing]:
        rng = random.Random(5)
        items = []
        for k in range(1, 56 if n >= 12 else 20):
            day = (datetime.fromtimestamp(NOW, TZ) - timedelta(days=k)).date()
            if day.weekday() >= 5:
                continue
            base = recent_back if k < 28 else before_back
            items.append(out(bp.hours_to_ts(day, 8.0, TZ), bp.hours_to_ts(day, base + rng.uniform(-0.2, 0.2), TZ)))
        return items

    def test_a_clear_shift_is_reported_as_clear(self) -> None:
        t = bp.trend(self.history(18.5, 17.5), TZ, NOW, 8)
        self.assertTrue(t["enough"])
        self.assertTrue(t["clear"])
        self.assertAlmostEqual(t["shift_min"], 60, delta=15)
        self.assertGreater(t["low_min"], 0)

    def test_no_shift_is_not_reported_as_a_trend(self) -> None:
        t = bp.trend(self.history(17.5, 17.5), TZ, NOW, 8)
        self.assertTrue(t["enough"])
        self.assertFalse(t["clear"])
        self.assertLess(abs(t["shift_min"]), 15)

    def test_needs_enough_data_in_both_periods(self) -> None:
        t = bp.trend(self.history(17.5, 17.5, n=3), TZ, NOW, 8)          # only the last 20 days exist
        self.assertFalse(t["enough"])
        self.assertEqual(bp.trend([], TZ, NOW, 8)["enough"], False)

    def test_deterministic(self) -> None:
        h = self.history(18.0, 17.5)
        self.assertEqual(bp.trend(h, TZ, NOW, 8), bp.trend(h, TZ, NOW, 8))


class PredictReturnTests(unittest.TestCase):
    def predict(self, outings, leave, now, method="time_of_day", settings=S):
        return bp.predict_return(outings, leave, now, TZ, settings, method)

    def test_not_enough_history(self) -> None:
        p = self.predict(weekday_outings(5), ts(2026, 10, 7, 8, 0), ts(2026, 10, 7, 9, 0))
        self.assertEqual((p["status"], p["needed"]), ("insufficient", 8))
        self.assertLess(p["n"], 8)

    def test_a_steady_routine_is_predicted_well(self) -> None:
        history = weekday_outings(60)
        p = self.predict(history, ts(2026, 10, 7, 8, 0), ts(2026, 10, 7, 10, 0))
        self.assertEqual(p["status"], "ready")
        self.assertTrue(p["low"] < p["median"] < p["high"])
        self.assertAlmostEqual(bp.shifted(p["median"], TZ)[1], 17.5, delta=0.5)
        self.assertTrue(17.0 <= bp.shifted(p["low"], TZ)[1] and bp.shifted(p["high"], TZ)[1] <= 18.1)
        self.assertGreaterEqual(p["n"], 30)

    def test_later_than_any_similar_day_is_overdue_with_the_usual_time(self) -> None:
        p = self.predict(weekday_outings(60), ts(2026, 10, 7, 8, 0), ts(2026, 10, 7, 20, 0))
        self.assertEqual(p["status"], "overdue")
        self.assertAlmostEqual(bp.shifted(p["usual_high"], TZ)[1], 17.9, delta=0.4)

    def test_prediction_only_looks_at_times_still_ahead(self) -> None:
        history = weekday_outings(60)
        early = self.predict(history, ts(2026, 10, 7, 8, 0), ts(2026, 10, 7, 9, 0))
        late = self.predict(history, ts(2026, 10, 7, 8, 0), ts(2026, 10, 7, 17, 30))
        self.assertEqual((early["status"], late["status"]), ("ready", "ready"))
        self.assertGreater(late["median"], early["median"])            # given they aren't back yet at 17:30
        self.assertGreaterEqual(late["low"], ts(2026, 10, 7, 17, 30))

    def test_weekdays_and_weekends_are_not_mixed(self) -> None:
        wd = weekday_outings(60, back_h=17.5)
        we = []
        for k in range(1, 70):
            day = (datetime.fromtimestamp(NOW, TZ) - timedelta(days=k)).date()
            if day.weekday() >= 5:
                we.append(out(bp.hours_to_ts(day, 11.0, TZ), bp.hours_to_ts(day, 22.0, TZ)))
        p = self.predict(wd + we * 2, ts(2026, 10, 7, 8, 0), ts(2026, 10, 7, 10, 0))              # Wednesday
        self.assertAlmostEqual(bp.shifted(p["median"], TZ)[1], 17.5, delta=0.6)
        sat = self.predict(wd + we * 2, ts(2026, 10, 10, 11, 0), ts(2026, 10, 10, 12, 0))
        self.assertEqual(sat["status"], "ready")
        self.assertAlmostEqual(bp.shifted(sat["median"], TZ)[1], 22.0, delta=0.6)

    def test_long_absences_are_not_predicted(self) -> None:
        p = self.predict(weekday_outings(60), NOW - 3 * DAY, NOW)
        self.assertEqual(p["status"], "long_away")

    def test_the_current_outing_is_not_part_of_its_own_history(self) -> None:
        history = weekday_outings(60) + [out(ts(2026, 10, 7, 8, 0), ts(2026, 10, 7, 23, 50))]       # 'today', already known
        p = self.predict(history, ts(2026, 10, 7, 8, 0), ts(2026, 10, 7, 10, 0))
        self.assertLess(bp.shifted(p["high"], TZ)[1], 19)

    def test_outings_that_happen_after_the_departure_cannot_influence_the_prediction(self) -> None:
        base = weekday_outings(60)
        leave = ts(2026, 10, 7, 8, 0)
        future = [out(bp.hours_to_ts((datetime.fromtimestamp(leave, TZ) + timedelta(days=k)).date(), 8.0, TZ),
                      bp.hours_to_ts((datetime.fromtimestamp(leave, TZ) + timedelta(days=k)).date(), 23.5, TZ))
                  for k in range(1, 16)]
        now = ts(2026, 10, 7, 10, 0)
        self.assertEqual(self.predict(base + future, leave, now), self.predict(base, leave, now))
        self.assertEqual(self.predict(base + future, leave, now, "duration"), self.predict(base, leave, now, "duration"))

    def test_recent_behaviour_counts_more_than_old_behaviour(self) -> None:
        old = weekday_outings(150, back_h=15.0, end=NOW - 100 * DAY)         # 100-180 days ago: back at 15:00
        new = weekday_outings(60, back_h=19.0)                                # the last two months: back at 19:00
        recent_weighted = self.predict(old + new, ts(2026, 10, 7, 8, 0), ts(2026, 10, 7, 9, 0))
        flat = self.predict(old + new, ts(2026, 10, 7, 8, 0), ts(2026, 10, 7, 9, 0),
                            settings=bp.Settings(halflife_days=365))
        weighted_h, flat_h = bp.shifted(recent_weighted["median"], TZ)[1], bp.shifted(flat["median"], TZ)[1]
        self.assertGreater(weighted_h, flat_h + 0.8)         # recent weeks pull the answer towards 19:00
        self.assertGreater(weighted_h, 18.0)
        self.assertLess(weighted_h, 19.6)

    def test_history_older_than_the_window_is_ignored(self) -> None:
        ancient = weekday_outings(400, back_h=12.0, end=NOW - 300 * DAY)
        p = self.predict(ancient, ts(2026, 10, 7, 8, 0), ts(2026, 10, 7, 9, 0))
        self.assertEqual(p["status"], "insufficient")

    def test_duration_method_follows_the_departure_time(self) -> None:
        rng = random.Random(9)
        history = []
        for k in range(1, 80):
            day = (datetime.fromtimestamp(NOW, TZ) - timedelta(days=k)).date()
            if day.weekday() >= 5:
                continue
            leave_h = rng.choice([7.0, 9.0, 11.0, 13.0])
            history.append(out(bp.hours_to_ts(day, leave_h, TZ), bp.hours_to_ts(day, leave_h + 6.0 + rng.uniform(-0.2, 0.2), TZ)))
        late_leaver = self.predict(history, ts(2026, 10, 7, 13, 0), ts(2026, 10, 7, 13, 10), "duration")
        early_leaver = self.predict(history, ts(2026, 10, 7, 7, 0), ts(2026, 10, 7, 7, 10), "duration")
        self.assertAlmostEqual(bp.shifted(late_leaver["median"], TZ)[1], 19.0, delta=0.5)
        self.assertAlmostEqual(bp.shifted(early_leaver["median"], TZ)[1], 13.0, delta=0.5)
        # the time-of-day method ignores when they left, so it is badly off for the early leaver
        tod = self.predict(history, ts(2026, 10, 7, 7, 0), ts(2026, 10, 7, 7, 10))
        self.assertGreater(abs(bp.shifted(tod["median"], TZ)[1] - 13.0), 1.5)

    def test_duration_method_needs_enough_similar_departures(self) -> None:
        history = weekday_outings(60, leave_h=8.0)
        p = self.predict(history, ts(2026, 10, 7, 16, 0), ts(2026, 10, 7, 16, 10), "duration")      # nobody leaves at 16:00
        self.assertEqual(p["status"], "insufficient")

    def test_after_midnight_returns(self) -> None:
        history = []
        for k in range(1, 60):
            day = (datetime.fromtimestamp(NOW, TZ) - timedelta(days=k)).date()
            if day.weekday() >= 5:
                continue
            history.append(out(bp.hours_to_ts(day, 19.0, TZ), bp.hours_to_ts(day, 24.5, TZ)))       # back 00:30
        p = self.predict(history, ts(2026, 10, 7, 19, 0), ts(2026, 10, 7, 21, 0))
        self.assertEqual(p["status"], "ready")
        self.assertAlmostEqual(p["median"], ts(2026, 10, 8, 0, 30), delta=HOUR)


class BacktestTests(unittest.TestCase):
    def test_a_regular_routine_backtests_well(self) -> None:
        r = bp.backtest(weekday_outings(120), TZ, S, "time_of_day")
        self.assertGreater(r["tests"], 30)
        self.assertGreaterEqual(r["hit_rate"], 0.7)
        self.assertLess(r["median_error_min"], 30)

    def test_too_little_data_means_no_tests_and_no_claims(self) -> None:
        r = bp.backtest(weekday_outings(14), TZ, S, "time_of_day")
        self.assertEqual(r["tests"], 0)
        self.assertIsNone(r["hit_rate"])
        self.assertIsNone(r["median_error_min"])

    def test_it_only_uses_the_past(self) -> None:
        """Changing the future must not change an earlier prediction."""
        base = weekday_outings(80)
        altered = base[:-1] + [out(base[-1].leave, base[-1].back + 6 * HOUR)]
        a, b = bp.backtest(base, TZ, S, "time_of_day"), bp.backtest(altered, TZ, S, "time_of_day")
        self.assertEqual(a["tests"], b["tests"])
        self.assertGreaterEqual(b["median_error_min"], a["median_error_min"])

    def test_choose_method_prefers_duration_when_return_depends_on_leave_time(self) -> None:
        rng = random.Random(4)
        history = []
        for k in range(1, 200):
            day = (datetime.fromtimestamp(NOW, TZ) - timedelta(days=k)).date()
            if day.weekday() >= 5:
                continue
            leave_h = rng.choice([7.0, 10.0, 13.0])
            history.append(out(bp.hours_to_ts(day, leave_h, TZ), bp.hours_to_ts(day, leave_h + 5 + rng.uniform(-0.2, 0.2), TZ)))
        method, result = bp.choose_method(history, TZ, S)
        self.assertEqual(method, "duration")
        self.assertLess(result["median_error_min"], 30)

    def test_choose_method_defaults_to_time_of_day(self) -> None:
        self.assertEqual(bp.choose_method(weekday_outings(120), TZ, S)[0], "time_of_day")
        self.assertEqual(bp.choose_method([], TZ, S)[0], "time_of_day")


class PredictLeaveTests(unittest.TestCase):
    def test_usual_leaving_time_still_ahead(self) -> None:
        p = bp.predict_leave(weekday_outings(60, leave_h=8.0), None, ts(2026, 10, 7, 6, 0), TZ, S)
        self.assertEqual(p["status"], "ready")
        self.assertAlmostEqual(bp.shifted(p["median"], TZ)[1], 8.0, delta=0.4)

    def test_nothing_expected_once_the_usual_time_has_passed(self) -> None:
        p = bp.predict_leave(weekday_outings(60, leave_h=8.0), None, ts(2026, 10, 7, 20, 0), TZ, S)
        self.assertEqual(p["status"], "none_expected")

    def test_not_enough_history(self) -> None:
        self.assertEqual(bp.predict_leave(weekday_outings(5), None, NOW, TZ, S)["status"], "insufficient")


class QualityTests(unittest.TestCase):
    def good_typical(self, spread=3.0):
        s = {"enough": True, "n": 40, "median": 18.0, "q1": 18.0 - spread / 2, "q3": 18.0 + spread / 2}
        return {bp.WEEKDAY: {"return": s}, bp.WEEKEND: {"return": {"enough": False, "n": 0}}}

    def sessions(self, n):
        return [bp.Session(i * 1000, i * 1000 + 500) for i in range(n)]

    def test_not_enough_outings(self) -> None:
        q = bp.quality(10, self.sessions(5), [out(0, 1)] * 3, 10, "WiFi", 8, self.good_typical())
        self.assertEqual(q["verdict"], "insufficient")
        self.assertIn("only 3 usable outings", q["reasons"][0])

    def test_good(self) -> None:
        q = bp.quality(150, self.sessions(80), [out(0, 1)] * 45, 60, "WiFi", 8, self.good_typical(3.0))
        self.assertEqual(q["verdict"], "good")

    def test_usable_when_times_vary_a_lot(self) -> None:
        q = bp.quality(150, self.sessions(80), [out(0, 1)] * 45, 60, "WiFi", 8, self.good_typical(6.5))
        self.assertEqual(q["verdict"], "usable")
        self.assertTrue(any("wide windows" in r for r in q["reasons"]))

    def test_noisy_for_each_reason(self) -> None:
        many = bp.quality(900, self.sessions(300), [out(0, 1)] * 45, 60, "WiFi", 8, self.good_typical())      # 5 a day
        self.assertEqual(many["verdict"], "noisy")
        flicker = bp.quality(900, self.sessions(100), [out(0, 1)] * 45, 60, "WiFi", 8, self.good_typical())   # 89% flicker
        self.assertEqual(flicker["verdict"], "noisy")
        spread = bp.quality(150, self.sessions(80), [out(0, 1)] * 45, 60, "WiFi", 8, self.good_typical(13.0))
        self.assertEqual(spread["verdict"], "noisy")
        self.assertIn("13 hours", " ".join(spread["reasons"]))

    def test_bluetooth_only_gets_advice_and_unreliable_sessions_are_counted(self) -> None:
        sessions = self.sessions(10) + [bp.Session(99999, 100000, spans_gap=True), bp.Session(200000, 200100, start_ok=False)]
        q = bp.quality(30, sessions, [out(0, 1)] * 20, 20, "Bluetooth", 8, self.good_typical())
        text = " ".join(q["reasons"])
        self.assertIn("link this phone's WiFi record", text)
        self.assertIn("2 home period(s) touched scanner downtime", text)

    def test_observed_days(self) -> None:
        gaps = [(ts(2026, 10, 3, 0, 0), ts(2026, 10, 5, 12, 0))]       # off 3rd, 4th and half of the 5th
        n = bp.observed_days(gaps, TZ, ts(2026, 10, 1, 9, 0), ts(2026, 10, 7, 12, 0))
        # 1st, 2nd and 6th. The 5th was only half watched, and today (the 7th) is still only half over.
        self.assertEqual(n, 3)


class SettingsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        s = bp.load_settings({})
        self.assertEqual((s.flap_minutes, s.flap_minutes_bluetooth, s.min_outing_minutes, s.min_samples), (20, 45, 60, 8))
        self.assertFalse(s.late_enabled)

    def test_overrides_and_bad_values(self) -> None:
        s = bp.load_settings({"presence_flap_minutes": 30, "presence_min_samples": 12, "late_alerts_enabled": True,
                              "late_alerts_people": ["Lilou", "  Sam "], "late_alerts_margin_minutes": 45})
        self.assertEqual((s.flap_minutes, s.min_samples, s.late_enabled, s.late_people, s.late_margin_minutes),
                         (30, 12, True, ("lilou", "sam"), 45))
        bad = bp.load_settings({"presence_flap_minutes": -3, "presence_min_samples": 1, "presence_halflife_days": "x",
                                "late_alerts_enabled": "yes", "late_alerts_people": "lilou"})
        d = bp.Settings()
        self.assertEqual((bad.flap_minutes, bad.min_samples, bad.halflife_days, bad.late_enabled, bad.late_people),
                         (d.flap_minutes, d.min_samples, d.halflife_days, False, ()))


class DescribeTests(unittest.TestCase):
    NOW = ts(2026, 10, 7, 15, 0)

    def say(self, pred, kind="return"):
        pred = {"kind": kind, "leave_ts": ts(2026, 10, 7, 8, 0), **pred}
        return bp.describe_prediction("Sam", pred, None, self.NOW, TZ)

    def test_each_status_has_a_plain_sentence(self) -> None:
        ready = self.say({"status": "ready", "median": ts(2026, 10, 7, 17, 40), "low": ts(2026, 10, 7, 16, 50),
                          "high": ts(2026, 10, 7, 19, 10), "n": 24})
        self.assertIn("expected home around 17:40", ready)
        self.assertIn("in about 3 h", ready)
        self.assertIn("between 16:50 and 19:10", ready)
        self.assertIn("24 similar weekday outings", ready)
        soon = self.say({"status": "ready", "median": ts(2026, 10, 7, 15, 35), "low": ts(2026, 10, 7, 15, 20),
                         "high": ts(2026, 10, 7, 16, 0), "n": 9})
        self.assertIn("in about 35 min", soon)
        self.assertIn("later than usual", self.say({"status": "overdue", "usual_high": ts(2026, 10, 7, 14, 0)}))
        self.assertIn("14:00", self.say({"status": "overdue", "usual_high": ts(2026, 10, 7, 14, 0)}))
        self.assertIn("Not enough history", self.say({"status": "insufficient", "n": 3, "needed": 8}))
        self.assertIn("3 of 8", self.say({"status": "insufficient", "n": 3, "needed": 8}))
        self.assertIn("long time", self.say({"status": "long_away"}))
        self.assertIn("scanner was off", self.say({"status": "unknown"}))
        self.assertIn("too noisy", self.say({"status": "unreliable"}))
        self.assertIn("too noisy", self.say({"status": "unreliable"}, kind="leave"))

    def test_leave_sentences(self) -> None:
        ready = self.say({"status": "ready", "median": ts(2026, 10, 7, 18, 5), "low": ts(2026, 10, 7, 17, 0),
                          "high": ts(2026, 10, 7, 19, 0), "n": 20}, kind="leave")
        self.assertIn("usually heads out around 18:05", ready)
        self.assertIn("doesn't usually go out again", self.say({"status": "none_expected"}, kind="leave"))
        self.assertIn("Not enough history", self.say({"status": "insufficient", "n": 2, "needed": 8}, kind="leave"))

    def test_typical_sentence(self) -> None:
        typical = bp.typical_times(weekday_outings(60), TZ, 8)
        text = bp.describe_typical("Sam", typical)
        self.assertRegex(text, r"Sam usually gets home around 17:[2-3]\d")
        self.assertIn("on weekdays", text)
        self.assertNotIn("weekends", text)                       # none recorded
        self.assertIsNone(bp.describe_typical("Sam", bp.typical_times([], TZ, 8)))

    def test_no_gendered_pronouns_anywhere(self) -> None:
        samples = [self.say({"status": s, "median": self.NOW + 3600, "low": self.NOW, "high": self.NOW + 7200,
                             "n": 12, "usual_high": self.NOW - 3600, "needed": 8}, kind=k)
                   for s in ("ready", "overdue", "insufficient", "long_away", "unknown", "unreliable", "none_expected")
                   for k in ("return", "leave")]
        for text in samples:
            for word in (" he ", " she ", " her ", " his ", " him "):
                self.assertNotIn(word, f" {text.lower()} ")


class EngineTests(unittest.TestCase):
    """analyse / build_report / eta_for_people against a real (temporary) database."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "bt_radar.db"
        bt_db.init_db(self.db)
        self.conn = bt_db.get_connection(self.db)
        self.addCleanup(self.conn.close)
        bp._session_cache.clear()
        bp._backtest_cache.clear()

    def phone(self, mac, name, scan="WiFi", state="DETECTED", last_seen=NOW - 60, **cols):
        bt_db.upsert_device(self.conn, mac, advertised_name=name, scan_type=scan, state=state)
        self.conn.execute("UPDATE devices SET friendly_name=?, device_type='Phone', state=?, last_seen=?, scan_type=? "
                          "WHERE mac_address=?", (name, state, last_seen, scan, mac))
        for c, v in cols.items():
            self.conn.execute(f"UPDATE devices SET {c}=? WHERE mac_address=?", (v, mac))
        self.conn.commit()

    def events(self, mac, outings, home_since=None):
        rows = [(mac, "arrived", home_since or (outings[0].leave - 12 * HOUR if outings else NOW - 60 * DAY))]
        for o in outings:
            rows += [(mac, "departed", o.leave), (mac, "arrived", o.back)]
        for m, kind, t in rows:
            self.conn.execute("INSERT INTO events (mac_address, event_type, timestamp) VALUES (?, ?, ?)", (m, kind, t))
        self.conn.commit()

    def report(self, now=NOW):
        return bp.build_report(self.conn, {}, now, TZ)

    def test_a_steady_household_produces_a_full_report(self) -> None:
        self.phone("AA:00:00:00:00:01", "Sam's iPhone", state="LOST", last_seen=ts(2026, 10, 7, 8, 0))
        outings = weekday_outings(90)
        self.events("AA:00:00:00:00:01", outings)
        self.conn.execute("INSERT INTO events (mac_address, event_type, timestamp) VALUES (?, 'departed', ?)",
                          ("AA:00:00:00:00:01", ts(2026, 10, 7, 8, 0)))        # left this morning (arrival was Tuesday evening)
        self.conn.commit()
        rep = self.report(ts(2026, 10, 7, 10, 0))
        (sam,) = rep["persons"]
        self.assertEqual((sam["person"], sam["state"]), ("sam", "away"))
        self.assertEqual(sam["quality"]["verdict"], "good")
        self.assertAlmostEqual(sam["typical"]["weekday"]["return"]["median"], 17.5, delta=0.3)
        self.assertAlmostEqual(sam["typical"]["weekday"]["leave"]["median"], 8.0, delta=0.3)
        self.assertEqual(sam["prediction"]["status"], "ready")
        self.assertEqual(sam["prediction"]["kind"], "return")
        self.assertEqual(len(sam["daily"]), 28)
        self.assertEqual(len(sam["timeline"]), 14)
        self.assertEqual(sam["method"], "time_of_day")
        self.assertGreater(sam["backtest"]["tests"], 20)
        self.assertGreaterEqual(sam["backtest"]["hit_rate"], 0.7)

    def test_people_without_a_phone_are_listed_but_not_analysed(self) -> None:
        self.conn.execute("INSERT INTO devices (mac_address, friendly_name, device_type, state, scan_type, first_seen, last_seen) "
                          "VALUES ('AA:00:00:00:00:09', 'Kim''s MacBook', 'Laptop', 'DETECTED', 'WiFi', 1, 1)")
        self.conn.commit()
        rep = self.report()
        self.assertEqual(rep["persons"], [])
        self.assertEqual(rep["untracked"], ["Kim"])

    def test_noisy_signal_gets_no_prediction(self) -> None:
        self.phone("AA:00:00:00:00:02", "Rae's iPhone", scan="BLE", state="LOST", last_seen=ts(2026, 10, 7, 8, 0))
        rng = random.Random(8)
        noisy = []
        t = NOW - 60 * DAY
        while t < NOW - 2 * DAY:
            leave = t + rng.uniform(0.5, 2) * HOUR
            back = leave + rng.uniform(1.5, 6) * HOUR            # an "outing" every few hours, at any time of day
            noisy.append(out(leave, back))
            t = back
        self.events("AA:00:00:00:00:02", noisy)
        self.conn.execute("INSERT INTO events (mac_address, event_type, timestamp) VALUES (?, 'departed', ?)",
                          ("AA:00:00:00:00:02", ts(2026, 10, 7, 8, 0)))
        self.conn.commit()
        (rae,) = self.report(ts(2026, 10, 7, 10, 0))["persons"]
        self.assertEqual(rae["quality"]["verdict"], "noisy")
        self.assertEqual(rae["prediction"]["status"], "unreliable")
        self.assertTrue(rae["quality"]["reasons"])

    def test_scanner_downtime_after_leaving_means_no_prediction(self) -> None:
        self.phone("AA:00:00:00:00:03", "Jo's iPhone", state="LOST", last_seen=ts(2026, 10, 7, 8, 0))
        self.events("AA:00:00:00:00:03", weekday_outings(90))
        self.conn.execute("INSERT INTO events (mac_address, event_type, timestamp) VALUES (?, 'departed', ?)",
                          ("AA:00:00:00:00:03", ts(2026, 10, 7, 8, 0)))
        self.conn.execute("INSERT INTO scanner_gaps (start, end) VALUES (?, ?)", (ts(2026, 10, 7, 8, 30), ts(2026, 10, 7, 9, 30)))
        self.conn.commit()
        (jo,) = self.report(ts(2026, 10, 7, 10, 0))["persons"]
        self.assertEqual((jo["prediction"]["status"], jo["prediction"].get("reason")), ("unknown", "downtime"))

    def test_home_people_get_a_usual_leaving_time(self) -> None:
        self.phone("AA:00:00:00:00:04", "Al's iPhone", state="DETECTED")
        self.events("AA:00:00:00:00:04", weekday_outings(90))
        eta = bp.eta_for_people(self.conn, {}, ts(2026, 10, 8, 6, 30), TZ)        # Thursday 06:30, at home
        self.assertEqual((eta["al"]["kind"], eta["al"]["status"]), ("leave", "ready"))
        self.assertAlmostEqual(bp.shifted(eta["al"]["median"], TZ)[1], 8.0, delta=0.5)

    def test_results_follow_new_events_and_are_cached_otherwise(self) -> None:
        self.phone("AA:00:00:00:00:05", "Bo's iPhone")
        self.events("AA:00:00:00:00:05", weekday_outings(60))
        first = bp.analyse(self.conn, {}, NOW, TZ)["bo"]
        again = bp.analyse(self.conn, {}, NOW, TZ)["bo"]
        self.assertEqual(len(first.outings), len(again.outings))
        self.assertEqual(len(bp._session_cache), 1)
        extra = out(NOW - 2 * HOUR - 20 * HOUR, NOW - 2 * HOUR - 10 * HOUR)
        self.events("AA:00:00:00:00:05", [extra])
        self.assertGreater(len(bp.analyse(self.conn, {}, NOW, TZ)["bo"].sessions) + len(bp._session_cache), 1)
        self.assertEqual(len(bp._session_cache), 2)             # recomputed because the events changed

    def test_two_databases_with_the_same_names_do_not_share_cached_results(self) -> None:
        self.phone("AA:00:00:00:00:06", "Cy's iPhone")
        self.events("AA:00:00:00:00:06", weekday_outings(60))
        mine = bp.analyse(self.conn, {}, NOW, TZ)["cy"]
        other_path = Path(self.tmp.name) / "other.db"
        bt_db.init_db(other_path)
        other = bt_db.get_connection(other_path)
        self.addCleanup(other.close)
        bt_db.upsert_device(other, "AA:00:00:00:00:06", advertised_name="Cy's iPhone", scan_type="WiFi")
        other.execute("UPDATE devices SET friendly_name=?, device_type='Phone' WHERE mac_address=?", ("Cy's iPhone", "AA:00:00:00:00:06"))
        other.commit()
        theirs = bp.analyse(other, {}, NOW, TZ)["cy"]
        self.assertGreater(len(mine.outings), 10)
        self.assertEqual(len(theirs.outings), 0)

    def test_downtime_in_the_middle_does_not_create_fake_outings(self) -> None:
        self.phone("AA:00:00:00:00:07", "Di's iPhone")
        outings = weekday_outings(60)
        self.events("AA:00:00:00:00:07", outings)
        gap = (NOW - 30 * DAY, NOW - 10 * DAY)
        self.conn.execute("INSERT INTO scanner_gaps (start, end) VALUES (?, ?)", gap)
        self.conn.commit()
        data = bp.analyse(self.conn, {}, NOW, TZ)["di"]
        for o in data.outings:
            self.assertFalse(o.leave < gap[1] and o.back > gap[0])


if __name__ == "__main__":
    unittest.main()
