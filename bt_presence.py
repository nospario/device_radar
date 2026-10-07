#!/usr/bin/env python3
"""Presence analytics: clean home/away sessions, reports, trends and arrival predictions.

Built from the phone events Device Radar already records (see ``bt_people``), with
the flicker removed and the time the scanner was switched off left out:

1. **Sessions**: periods a person is at home. Arrive/depart events of all the
   person's phone records are merged; absences shorter than ``flap_minutes`` are
   treated as flicker. Sessions touching scanner downtime are flagged unreliable
   (the scanner learned of an arrival or departure only when it restarted).
2. **Outings**: reliable absences of at least ``min_outing_minutes`` between two
   sessions. All typical-time statistics use outings only.
3. **Reports**: typical leave/return times (weekday / weekend, with ranges), time at
   home per day, a day-by-day timeline, when the house is empty, and whether
   return times are drifting.
4. **Predictions**: "expected home around 18:40 (80% between 17:50 and 20:10)",
   from similar past outings, weighted towards recent weeks. Every prediction
   carries its sample size, and a walk-forward backtest measures how often the
   80% window was right on past data.

Plain explainable statistics (medians, weighted quantiles, a bootstrap for trends)
rather than machine learning: a few dozen samples per person do not support more.

Time-of-day is measured on a "day" that starts at 04:00 local time, so a 00:30
return belongs to the evening before.
"""

from __future__ import annotations

import asyncio
import html
import logging
import random
import sqlite3
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Awaitable, Callable

import bt_cleanup
import bt_db
import bt_people

logger = logging.getLogger("bt_presence")

DAY_START_HOUR = 4
SETTLE_SECONDS = 15 * 60       # an arrival/departure this soon after the scanner restarted is not a real one
WEEKDAY, WEEKEND = "weekday", "weekend"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    flap_minutes: float = 20.0
    flap_minutes_bluetooth: float = 45.0
    min_outing_minutes: float = 60.0
    min_samples: int = 8
    halflife_days: float = 42.0
    history_days: int = 180
    long_away_hours: float = 18.0
    late_enabled: bool = False
    late_dry_run: bool = False
    late_people: tuple[str, ...] = ()
    late_margin_minutes: float = 30.0


def _num(value: Any, default: float, low: float, high: float) -> float:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high
    return float(value) if ok else default


def load_settings(config: dict[str, Any]) -> Settings:
    d = Settings()
    people = config.get("late_alerts_people")
    if not (isinstance(people, list) and all(isinstance(p, str) for p in people)):
        people = []
    enabled, dry = config.get("late_alerts_enabled"), config.get("late_alerts_dry_run")
    samples = config.get("presence_min_samples")
    return Settings(
        flap_minutes=_num(config.get("presence_flap_minutes"), d.flap_minutes, 1, 240),
        flap_minutes_bluetooth=_num(config.get("presence_flap_minutes_bluetooth"), d.flap_minutes_bluetooth, 1, 240),
        min_outing_minutes=_num(config.get("presence_min_outing_minutes"), d.min_outing_minutes, 5, 720),
        min_samples=int(samples) if isinstance(samples, int) and not isinstance(samples, bool) and 3 <= samples <= 100 else d.min_samples,
        halflife_days=_num(config.get("presence_halflife_days"), d.halflife_days, 7, 365),
        history_days=int(_num(config.get("presence_history_days"), d.history_days, 14, 730)),
        long_away_hours=_num(config.get("presence_long_away_hours"), d.long_away_hours, 2, 720),
        late_enabled=enabled if isinstance(enabled, bool) else d.late_enabled,
        late_dry_run=dry if isinstance(dry, bool) else d.late_dry_run,
        late_people=tuple(bt_people.normalise_person(p) or "" for p in people if bt_people.normalise_person(p)),
        late_margin_minutes=_num(config.get("late_alerts_margin_minutes"), d.late_margin_minutes, 0, 600),
    )


# ---------------------------------------------------------------------------
# Time helpers (local time; ``tz=None`` means the system's timezone)
# ---------------------------------------------------------------------------

def _local(ts: float, tz: tzinfo | None) -> datetime:
    return datetime.fromtimestamp(ts, tz) if tz else datetime.fromtimestamp(ts)


def _midnight_ts(day: date, tz: tzinfo | None) -> float:
    naive = datetime(day.year, day.month, day.day)
    return (naive.replace(tzinfo=tz) if tz else naive).timestamp()


def shifted(ts: float, tz: tzinfo | None = None) -> tuple[date, float]:
    """``(day, hours)`` where the day starts at 04:00; hours is in [4, 28)."""
    local = _local(ts, tz)
    hours = local.hour + local.minute / 60 + local.second / 3600
    day = local.date()
    if hours < DAY_START_HOUR:
        hours += 24
        day -= timedelta(days=1)
    return day, hours


def day_type(day: date) -> str:
    return WEEKDAY if day.weekday() < 5 else WEEKEND


def fmt_hours(hours: float) -> str:
    """'18:40' for 18.67 (hours may exceed 24 for after-midnight times)."""
    total = int(round(hours * 60)) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def hours_to_ts(day: date, hours: float, tz: tzinfo | None = None) -> float:
    return _midnight_ts(day, tz) + hours * 3600


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def quantile(values: list[float], q: float) -> float:
    """Linear-interpolated quantile of a non-empty list."""
    data = sorted(values)
    if len(data) == 1:
        return data[0]
    pos = q * (len(data) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(data) - 1)
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


def weighted_quantile(pairs: list[tuple[float, float]], q: float) -> float:
    """Weighted quantile of ``(value, weight)`` pairs (weights > 0); interpolates between points."""
    data = sorted(pairs)
    total = sum(w for _, w in data)
    if len(data) == 1 or total <= 0:
        return data[0][0]
    target = q * total
    cumulative = 0.0
    previous_mid = None
    previous_value = data[0][0]
    for value, weight in data:
        mid = cumulative + weight / 2
        if mid >= target:
            if previous_mid is None:
                return value
            frac = (target - previous_mid) / (mid - previous_mid) if mid > previous_mid else 0
            return previous_value + (value - previous_value) * frac
        cumulative += weight
        previous_mid, previous_value = mid, value
    return data[-1][0]


def summarise(values: list[float], min_n: int) -> dict[str, Any]:
    """n, median, middle half (q1-q3) and 10-90% range, or just n if there is not enough data."""
    n = len(values)
    if n < min_n:
        return {"n": n, "enough": False}
    return {"n": n, "enough": True, "median": quantile(values, 0.5), "q1": quantile(values, 0.25),
            "q3": quantile(values, 0.75), "p10": quantile(values, 0.10), "p90": quantile(values, 0.90)}


# ---------------------------------------------------------------------------
# Sessions and outings
# ---------------------------------------------------------------------------

@dataclass
class Session:
    start: float
    end: float | None            # None: still at home
    start_ok: bool = True        # False if it began just after the scanner restarted
    spans_gap: bool = False      # the scanner was off during it, so its end is not trustworthy


@dataclass(frozen=True)
class Outing:
    leave: float
    back: float

    @property
    def duration(self) -> float:
        return self.back - self.leave


Gap = tuple[float, float]


def _overlaps(gaps: list[Gap], start: float, end: float) -> bool:
    return any(g0 < end and g1 > start for g0, g1 in gaps)


def build_sessions(events: list[tuple[str, str, float]], gaps: list[Gap], flap_seconds: float,
                   now: float) -> list[Session]:
    """Merge per-device arrive/depart events into a person's home sessions (see module docstring)."""
    open_since: dict[str, float] = {}
    spans: list[list[float | None]] = []
    for mac, kind, ts in sorted(events, key=lambda e: e[2]):
        if kind == "arrived":
            open_since.setdefault(mac, ts)
        elif kind == "departed" and mac in open_since:
            spans.append([open_since.pop(mac), ts])
    spans.extend([start, None] for start in open_since.values())
    spans.sort(key=lambda s: s[0])

    merged: list[list[float | None]] = []
    for start, end in spans:
        if merged:
            last = merged[-1]
            if last[1] is None or start - last[1] < flap_seconds:
                last[1] = None if (last[1] is None or end is None) else max(last[1], end)
                continue
        merged.append([start, end])

    sessions = []
    for start, end in merged:
        effective_end = now if end is None else end
        sessions.append(Session(
            start=start, end=end,
            start_ok=not any(0 <= start - g1 <= SETTLE_SECONDS for _, g1 in gaps),
            spans_gap=_overlaps(gaps, start, effective_end),
        ))
    return sessions


def outings_from_sessions(sessions: list[Session], gaps: list[Gap], min_outing_seconds: float) -> list[Outing]:
    """Reliable absences long enough to count as an outing."""
    outings = []
    for first, second in zip(sessions, sessions[1:]):
        if first.end is None or first.spans_gap or not second.start_ok:
            continue
        if second.start - first.end < min_outing_seconds or _overlaps(gaps, first.end, second.start):
            continue
        outings.append(Outing(first.end, second.start))
    return outings


# ---------------------------------------------------------------------------
# Typical times, daily hours, timeline, household, trend
# ---------------------------------------------------------------------------

def typical_times(outings: list[Outing], tz: tzinfo | None, min_n: int) -> dict[str, Any]:
    """Leave / return time-of-day and outing length, split into weekdays and weekends."""
    result: dict[str, Any] = {}
    for kind in (WEEKDAY, WEEKEND):
        leaves, returns, durations = [], [], []
        for o in outings:
            day, leave_h = shifted(o.leave, tz)
            if day_type(day) != kind:
                continue
            back_day, back_h = shifted(o.back, tz)
            leaves.append(leave_h)
            returns.append(back_h + (24 if back_day > day else 0))
            durations.append(o.duration / 3600)
        result[kind] = {"leave": summarise(leaves, min_n), "return": summarise(returns, min_n),
                        "hours_out": summarise(durations, min_n)}
    return result


def _subtract(interval: tuple[float, float], gaps: list[Gap]) -> list[tuple[float, float]]:
    """The parts of ``interval`` not covered by any gap."""
    pieces = [interval]
    for g0, g1 in sorted(gaps):
        nxt = []
        for a, b in pieces:
            if g1 <= a or g0 >= b:
                nxt.append((a, b))
                continue
            if g0 > a:
                nxt.append((a, g0))
            if g1 < b:
                nxt.append((g1, b))
        pieces = nxt
    return [p for p in pieces if p[1] > p[0]]


def _intersect(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = []
    for a0, a1 in a:
        for b0, b1 in b:
            lo, hi = max(a0, b0), min(a1, b1)
            if hi > lo:
                out.append((lo, hi))
    return out


def _length(intervals: list[tuple[float, float]]) -> float:
    return sum(b - a for a, b in intervals)


def _session_spans(sessions: list[Session], now: float) -> list[tuple[float, float]]:
    return [(s.start, now if s.end is None else s.end) for s in sessions]


def daily_home_hours(sessions: list[Session], gaps: list[Gap], tz: tzinfo | None, now: float,
                     days: int = 28) -> list[dict[str, Any]]:
    """Hours at home per calendar day, counting only time the scanner was actually watching."""
    today = _local(now, tz).date()
    spans = _session_spans(sessions, now)
    rows = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        start = _midnight_ts(day, tz)
        end = min(_midnight_ts(day + timedelta(days=1), tz), now)
        observed = _subtract((start, end), gaps) if end > start else []
        home = _intersect(spans, observed)
        full_day = (_midnight_ts(day + timedelta(days=1), tz) - start)
        rows.append({
            "date": day.isoformat(), "weekday": day.strftime("%a"),
            "home_h": round(_length(home) / 3600, 2), "observed_h": round(_length(observed) / 3600, 2),
            "coverage": round(_length(observed) / full_day, 3),
            "partial": _length(observed) / full_day < 0.8,
        })
    return rows


def timeline(sessions: list[Session], gaps: list[Gap], tz: tzinfo | None, now: float,
             days: int = 14) -> list[dict[str, Any]]:
    """Per day, the hours (0-24) spent at home and the hours the scanner was off, for drawing."""
    today = _local(now, tz).date()
    spans = _session_spans(sessions, now)
    rows = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        start = _midnight_ts(day, tz)
        end = min(_midnight_ts(day + timedelta(days=1), tz), now)

        def to_hours(parts: list[tuple[float, float]]) -> list[list[float]]:
            return [[round((a - start) / 3600, 3), round((b - start) / 3600, 3)] for a, b in parts]

        window = [(start, end)] if end > start else []
        unknown = _intersect(window, sorted(gaps))
        known = _subtract((start, end), gaps) if end > start else []
        rows.append({"date": day.isoformat(), "weekday": day.strftime("%a"),
                     "home": to_hours(_intersect(spans, known)), "unknown": to_hours(unknown),
                     "future_from": round((end - start) / 3600, 3) if end < _midnight_ts(day + timedelta(days=1), tz) else None})
    return rows


def household_empty(people_sessions: dict[str, list[Session]], gaps: list[Gap], tz: tzinfo | None,
                    now: float, min_empty_seconds: float, min_n: int, window_days: int = 90) -> dict[str, Any]:
    """When nobody (among tracked phones) is home: typical start/end and hours per day."""
    if not people_sessions:
        return {"people": [], "empty_h_per_day": None, "weekday": {}, "weekend": {}}
    home = []
    for sessions in people_sessions.values():
        home.extend(_session_spans(sessions, now))
    first_tracked = min((a for a, _ in home), default=now)
    since = max(now - window_days * 86400, first_tracked)
    home.sort()
    union: list[list[float]] = []
    for a, b in home:
        if union and a <= union[-1][1]:
            union[-1][1] = max(union[-1][1], b)
        else:
            union.append([a, b])
    observed = _subtract((since, now), gaps)
    empty = []
    for o0, o1 in observed:
        cursor = o0
        for a, b in union:
            if b <= o0 or a >= o1:
                continue
            if a > cursor:
                empty.append((cursor, a))
            cursor = max(cursor, b)
        if cursor < o1:
            empty.append((cursor, o1))
    empty = [e for e in empty if e[1] - e[0] >= min_empty_seconds and e[1] < now]   # finished periods only
    starts: dict[str, list[float]] = {WEEKDAY: [], WEEKEND: []}
    ends: dict[str, list[float]] = {WEEKDAY: [], WEEKEND: []}
    for a, b in empty:
        day, hour = shifted(a, tz)
        kind = day_type(day)
        starts[kind].append(hour)
        end_day, end_hour = shifted(b, tz)
        ends[kind].append(end_hour + (24 if end_day > day else 0))
    observed_days = _length(observed) / 86400
    return {
        "people": sorted(people_sessions), "periods": len(empty),
        "empty_h_per_day": round(sum(b - a for a, b in empty) / 3600 / observed_days, 2) if observed_days >= 1 else None,
        "observed_days": round(observed_days, 1),
        WEEKDAY: {"empty_from": summarise(starts[WEEKDAY], min_n), "empty_until": summarise(ends[WEEKDAY], min_n)},
        WEEKEND: {"empty_from": summarise(starts[WEEKEND], min_n), "empty_until": summarise(ends[WEEKEND], min_n)},
    }


def trend(outings: list[Outing], tz: tzinfo | None, now: float, min_n: int,
          window_days: int = 28, resamples: int = 1000) -> dict[str, Any]:
    """Are weekday return times drifting? Last ``window_days`` against the ``window_days`` before.

    The shift in the median (minutes) comes with an 80% bootstrap interval; ``clear`` is true only
    if that interval excludes zero, so ordinary week-to-week variation is not reported as a trend.
    """
    recent, before = [], []
    for o in outings:
        day, _ = shifted(o.back, tz)
        if day_type(shifted(o.leave, tz)[0]) != WEEKDAY:
            continue
        hour = shifted(o.back, tz)[1] + (24 if day > shifted(o.leave, tz)[0] else 0)
        age = (now - o.back) / 86400
        if age < window_days:
            recent.append(hour)
        elif age < 2 * window_days:
            before.append(hour)
    out: dict[str, Any] = {"n_recent": len(recent), "n_before": len(before), "enough": False}
    if len(recent) < min_n or len(before) < min_n:
        return out
    rng = random.Random(42)
    diffs = []
    for _ in range(resamples):
        a = statistics.median(rng.choices(recent, k=len(recent)))
        b = statistics.median(rng.choices(before, k=len(before)))
        diffs.append(a - b)
    low, high = quantile(diffs, 0.10), quantile(diffs, 0.90)
    shift = statistics.median(recent) - statistics.median(before)
    out.update({"enough": True, "shift_min": round(shift * 60), "low_min": round(low * 60),
                "high_min": round(high * 60), "clear": low > 0 or high < 0})
    return out


# ---------------------------------------------------------------------------
# Predictions
# ---------------------------------------------------------------------------

def _history(outings: list[Outing], before: float, now: float, kind: str, tz: tzinfo | None,
             settings: Settings) -> list[Outing]:
    cutoff = now - settings.history_days * 86400
    return [o for o in outings if o.back < before and o.back >= cutoff and day_type(shifted(o.leave, tz)[0]) == kind]


def predict_return(outings: list[Outing], leave_ts: float, now: float, tz: tzinfo | None,
                   settings: Settings, method: str = "time_of_day") -> dict[str, Any]:
    """When will someone who left at ``leave_ts`` be back? See the module docstring.

    ``status`` is ``ready`` (with median/low/high timestamps), ``overdue`` (later than any similar
    past outing; ``usual_high`` says when they are normally back by), ``insufficient`` (too little
    similar history) or ``long_away`` (gone more than ``long_away_hours``).
    """
    if now - leave_ts > settings.long_away_hours * 3600:
        return {"status": "long_away"}
    leave_day, leave_h = shifted(leave_ts, tz)
    kind = day_type(leave_day)
    history = _history(outings, leave_ts, now, kind, tz, settings)
    if len(history) < settings.min_samples:
        return {"status": "insufficient", "n": len(history), "needed": settings.min_samples}

    pairs: list[tuple[float, float]] = []
    for o in history:
        weight = 0.5 ** ((now - o.back) / 86400 / settings.halflife_days)
        if method == "duration":
            if abs(shifted(o.leave, tz)[1] - leave_h) > 1.5:
                continue
            pairs.append((leave_ts + o.duration, weight))
        else:
            back_day, back_h = shifted(o.back, tz)
            hours = back_h + (24 if back_day > shifted(o.leave, tz)[0] else 0)
            pairs.append((hours_to_ts(leave_day, hours, tz), weight))
    if len(pairs) < settings.min_samples:
        return {"status": "insufficient", "n": len(pairs), "needed": settings.min_samples, "method": method}

    all_high = weighted_quantile(pairs, 0.9)
    remaining = [(t, w) for t, w in pairs if t >= now]
    if len(remaining) < 3:
        return {"status": "overdue", "n": len(pairs), "method": method,
                "usual_median": weighted_quantile(pairs, 0.5), "usual_high": all_high}
    return {"status": "ready", "n": len(pairs), "method": method,
            "median": weighted_quantile(remaining, 0.5), "low": weighted_quantile(remaining, 0.1),
            "high": weighted_quantile(remaining, 0.9)}


def backtest(outings: list[Outing], tz: tzinfo | None, settings: Settings, method: str,
             min_history: int | None = None) -> dict[str, Any]:
    """Walk forward through past outings: predict each from only the outings before it.

    Returns how often the 80% window contained the real return time, the typical error of the
    median (minutes), and the typical error of the naive guess "the usual return time".
    """
    min_history = settings.min_samples + 2 if min_history is None else min_history
    ordered = sorted(outings, key=lambda o: o.back)
    hits = tests = skipped = 0
    errors: list[float] = []
    naive_errors: list[float] = []
    for k in range(min_history, len(ordered)):
        target = ordered[k]
        simulated_now = target.leave + 600
        pred = predict_return(ordered[:k], target.leave, simulated_now, tz, settings, method)
        if pred["status"] != "ready":
            skipped += 1
            continue
        tests += 1
        hits += pred["low"] <= target.back <= pred["high"]
        errors.append(abs(pred["median"] - target.back) / 60)
        day, _ = shifted(target.leave, tz)
        same = [shifted(o.back, tz)[1] + (24 if shifted(o.back, tz)[0] > shifted(o.leave, tz)[0] else 0)
                for o in _history(ordered[:k], target.leave, simulated_now, day_type(day), tz, settings)]
        if same:
            guess = hours_to_ts(day, statistics.median(same), tz)
            naive_errors.append(abs(guess - target.back) / 60)
    return {"method": method, "tests": tests, "skipped": skipped,
            "hit_rate": round(hits / tests, 2) if tests else None,
            "median_error_min": round(statistics.median(errors)) if errors else None,
            "naive_error_min": round(statistics.median(naive_errors)) if naive_errors else None}


def choose_method(outings: list[Outing], tz: tzinfo | None, settings: Settings) -> tuple[str, dict[str, Any]]:
    """Pick whichever method had the smaller typical error in the backtest (time-of-day on a tie)."""
    tod = backtest(outings, tz, settings, "time_of_day")
    dur = backtest(outings, tz, settings, "duration")
    if (dur["tests"] >= 10 and tod["tests"] >= 10 and dur["median_error_min"] is not None
            and tod["median_error_min"] is not None and dur["median_error_min"] < tod["median_error_min"]):
        return "duration", dur
    return "time_of_day", tod


def predict_leave(outings: list[Outing], home_since: float | None, now: float, tz: tzinfo | None,
                  settings: Settings) -> dict[str, Any]:
    """When does someone who is at home usually next head out? (weekday/weekend, still ahead of now)."""
    day, _ = shifted(now, tz)
    kind = day_type(day)
    cutoff = now - settings.history_days * 86400
    pairs = []
    for o in outings:
        if o.leave < cutoff or day_type(shifted(o.leave, tz)[0]) != kind:
            continue
        leave_day, leave_h = shifted(o.leave, tz)
        pairs.append((hours_to_ts(day, leave_h, tz), 0.5 ** ((now - o.leave) / 86400 / settings.halflife_days)))
    if len(pairs) < settings.min_samples:
        return {"status": "insufficient", "n": len(pairs), "needed": settings.min_samples}
    remaining = [(t, w) for t, w in pairs if t >= now]
    if len(remaining) < 3:
        return {"status": "none_expected", "n": len(pairs)}
    return {"status": "ready", "n": len(pairs), "median": weighted_quantile(remaining, 0.5),
            "low": weighted_quantile(remaining, 0.1), "high": weighted_quantile(remaining, 0.9)}


# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------

def observed_days(gaps: list[Gap], tz: tzinfo | None, first_ts: float, now: float, min_coverage: float = 0.8) -> int:
    """Calendar days since ``first_ts`` on which the scanner watched for at least 80% of the day."""
    day = _local(first_ts, tz).date()
    last = _local(now, tz).date()
    count = 0
    while day <= last:
        start = _midnight_ts(day, tz)
        end = min(_midnight_ts(day + timedelta(days=1), tz), now)
        if end > start and _length(_subtract((start, end), gaps)) / (_midnight_ts(day + timedelta(days=1), tz) - start) >= min_coverage:
            count += 1
        day += timedelta(days=1)
    return count


def _spread(summary: dict[str, Any]) -> float | None:
    """Width in hours of the middle half of a time-of-day summary (None if there is not enough data)."""
    return round(summary["q3"] - summary["q1"], 1) if summary.get("enough") else None


def quality(raw_sessions: int, sessions: list[Session], outings: list[Outing], days: int, signal: str,
            min_n: int, typical: dict[str, Any] | None = None) -> dict[str, Any]:
    """How trustworthy this person's data is, in plain terms.

    ``noisy`` (too unreliable to predict from) if the signal flickers hugely, there are implausibly
    many separate home periods a day, or weekday return times are spread over more than 8 hours.
    ``good`` needs 30+ usable outings, at most 3 home periods a day and tidy return times.
    """
    merged = len(sessions)
    flicker = round(1 - merged / raw_sessions, 2) if raw_sessions else 0.0
    per_day = round(merged / days, 1) if days else None
    ret_spread = _spread(((typical or {}).get(WEEKDAY, {}) or {}).get("return", {}))
    reasons = []
    noisy = False
    if len(outings) < min_n:
        verdict = "insufficient"
        reasons.append(f"only {len(outings)} usable outings so far (need {min_n})")
    else:
        if per_day is not None and per_day > 4:
            noisy = True
            reasons.append(f"{per_day} separate home periods a day: the signal flickers a lot")
        if flicker >= 0.85:
            noisy = True
            reasons.append(f"{round(flicker * 100)}% of raw arrivals/departures were flicker")
        if ret_spread is not None and ret_spread > 8:
            noisy = True
            reasons.append(f"weekday return times are spread over {ret_spread:.0f} hours, which is more likely signal "
                           "dropouts (e.g. the phone going quiet overnight) than a real routine")
        if noisy:
            verdict = "noisy"
        elif len(outings) >= 30 and (per_day or 0) <= 3 and (ret_spread is None or ret_spread <= 5):
            verdict = "good"
        else:
            verdict = "usable"
            if ret_spread is not None and ret_spread > 5:
                reasons.append(f"weekday return times vary by {ret_spread:.0f} hours, so predictions will have wide windows")
    if 0.6 <= flicker < 0.85:
        reasons.append(f"{round(flicker * 100)}% of raw arrivals/departures were flicker and were merged away")
    if signal == "Bluetooth":
        reasons.append("Bluetooth only: it flickers more than WiFi, so link this phone's WiFi record")
    unreliable = sum(1 for s in sessions if s.spans_gap or not s.start_ok)
    if unreliable:
        reasons.append(f"{unreliable} home period(s) touched scanner downtime and are left out of the averages")
    return {"verdict": verdict, "signal": signal, "observed_days": days, "outings": len(outings),
            "home_periods": merged, "home_periods_per_day": per_day, "flicker_share": flicker,
            "return_spread_h": ret_spread, "reasons": reasons}


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------

@dataclass
class PersonData:
    person: str
    display: str
    macs: list[str]
    signal: str
    raw_sessions: int
    sessions: list[Session]
    outings: list[Outing]
    state: str                                  # home / away (from bt_people)
    method: str = "time_of_day"
    backtest: dict[str, Any] = field(default_factory=dict)
    typical: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)


def ignore_before(config: dict[str, Any], person: str, tz: tzinfo | None = None) -> float | None:
    """Start of the history to use for ``person``, from ``presence_ignore_before`` ({"richard": "2026-10-07"}).

    Lets you discard a period you know was bad (e.g. a Bluetooth-only phone that flickered) so the
    reports and predictions start again from clean data. Events before that date stay in the
    database; they are only left out of the analysis. Unknown names and unreadable dates are ignored.
    """
    raw = config.get("presence_ignore_before")
    if not isinstance(raw, dict):
        return None
    for name, value in raw.items():
        if bt_people.normalise_person(name) == person:
            try:
                return _midnight_ts(date.fromisoformat(str(value)), tz)
            except ValueError:
                logger.warning("presence_ignore_before for %s is not a YYYY-MM-DD date: %r", person, value)
    return None


def _signal(devices: list[dict[str, Any]]) -> str:
    kinds = {("WiFi" if "wifi" in (d.get("scan_type") or "").lower() else "Bluetooth") for d in devices}
    return "WiFi + Bluetooth" if len(kinds) == 2 else (kinds.pop() if kinds else "none")


_session_cache: dict[tuple, Any] = {}
_backtest_cache: dict[tuple, tuple[str, dict[str, Any]]] = {}
_cache_lock = threading.Lock()
_CACHE_LIMIT = 64


def _remember(cache: dict, key: tuple, value: Any) -> None:
    with _cache_lock:
        if len(cache) >= _CACHE_LIMIT:
            cache.pop(next(iter(cache)))
        cache[key] = value


def _db_path(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    return row[2] if row and row[2] else ""


def analyse(conn: sqlite3.Connection, config: dict[str, Any], now: float | None = None,
            tz: tzinfo | None = None) -> dict[str, PersonData]:
    """Sessions, outings and backtests for every person who has a tracked phone.

    Sessions are rebuilt only when that person's events (or the scanner downtime) change. The
    backtest, the slow part, is cached by the outings themselves, so Bluetooth flicker that merges
    away without changing any outing does not trigger it again.
    """
    now = time.time() if now is None else now
    settings = load_settings(config)
    gaps = [(float(s), float(e)) for s, e in conn.execute("SELECT start, end FROM scanner_gaps")] \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'scanner_gaps'").fetchone() else []
    all_gaps = gaps + [g for g in bt_cleanup._downtime(conn, now) if g not in gaps]
    states = {p["person"]: p for p in bt_people.people_status(conn, config, now)}
    db = _db_path(conn)

    result: dict[str, PersonData] = {}
    for person, devices in bt_people.phone_groups(conn, config).items():
        macs = [d["mac_address"] for d in devices]
        marks = ",".join("?" * len(macs))
        count, newest = conn.execute(
            f"SELECT COUNT(*), COALESCE(MAX(id), 0) FROM events WHERE mac_address IN ({marks})", macs).fetchone()[:2]
        cutoff = ignore_before(config, person, tz)
        key = (db, person, tuple(macs), count, newest, tuple(all_gaps), settings.flap_minutes,
               settings.flap_minutes_bluetooth, settings.min_outing_minutes, int(now // 3600), cutoff)
        with _cache_lock:
            cached = _session_cache.get(key)
        if cached:
            signal, raw, sessions, outings = cached
        else:
            events = [(r[0], r[1], r[2]) for r in conn.execute(
                f"SELECT mac_address, event_type, timestamp FROM events WHERE mac_address IN ({marks}) AND timestamp >= ?",
                [*macs, cutoff or 0])]
            signal = _signal(devices)
            flap = (settings.flap_minutes_bluetooth if "Bluetooth" in signal else settings.flap_minutes) * 60
            sessions = build_sessions(events, all_gaps, flap, now)
            raw = len(build_sessions(events, all_gaps, 0.0, now))
            outings = outings_from_sessions(sessions, all_gaps, settings.min_outing_minutes * 60)
            _remember(_session_cache, key, (signal, raw, sessions, outings))

        bkey = (tuple(outings), settings, tz)
        with _cache_lock:
            chosen = _backtest_cache.get(bkey)
        if chosen is None:
            chosen = choose_method(outings, tz, settings)
            _remember(_backtest_cache, bkey, chosen)
        method, bt = chosen
        typical = typical_times(outings, tz, settings.min_samples)
        first = min((s.start for s in sessions), default=None)
        days = observed_days(all_gaps, tz, first, now) if first else 0
        result[person] = PersonData(person, person.title(), macs, signal, raw, sessions, outings,
                                    states.get(person, {}).get("state", "away"), method, bt, typical,
                                    quality(raw, sessions, outings, days, signal, settings.min_samples, typical))
    return result


def person_prediction(data: PersonData, gaps: list[Gap], now: float, tz: tzinfo | None,
                      settings: Settings) -> dict[str, Any]:
    """The live prediction for one person: when they will be home, or when they usually leave."""
    if data.quality.get("verdict") == "noisy":
        return {"kind": "leave" if data.state == "home" else "return", "status": "unreliable",
                "reasons": data.quality.get("reasons", [])}
    if data.state == "home":
        home_since = data.sessions[-1].start if data.sessions else None
        out = predict_leave(data.outings, home_since, now, tz, settings)
        out["kind"] = "leave"
        return out
    if not data.sessions or data.sessions[-1].end is None:
        return {"kind": "return", "status": "insufficient", "n": 0}
    last = data.sessions[-1]
    leave_ts = last.end
    if last.spans_gap or _overlaps(gaps, leave_ts, now):
        return {"kind": "return", "status": "unknown", "reason": "downtime", "leave_ts": leave_ts}
    out = predict_return(data.outings, leave_ts, now, tz, settings, data.method)
    out["kind"], out["leave_ts"] = "return", leave_ts
    return out


def build_report(conn: sqlite3.Connection, config: dict[str, Any], now: float | None = None,
                 tz: tzinfo | None = None) -> dict[str, Any]:
    """Everything the reports page shows, as plain JSON-able data."""
    now = time.time() if now is None else now
    settings = load_settings(config)
    gaps = bt_cleanup._downtime(conn, now)
    people = analyse(conn, config, now, tz)
    status = {p["person"]: p for p in bt_people.people_status(conn, config, now)}
    persons = []
    for person, data in sorted(people.items()):
        prediction = person_prediction(data, gaps, now, tz, settings)
        persons.append({
            "person": person, "display": data.display, "state": data.state, "phones": status.get(person, {}).get("phones", []),
            "quality": data.quality,
            "typical": data.typical,
            "daily": daily_home_hours(data.sessions, gaps, tz, now),
            "timeline": timeline(data.sessions, gaps, tz, now),
            "trend": trend(data.outings, tz, now, settings.min_samples),
            "prediction": prediction,
            "prediction_text": describe_prediction(data.display, prediction, data.typical, now, tz),
            "typical_text": describe_typical(data.display, data.typical),
            "backtest": data.backtest, "method": data.method,
        })
    tracked = {p: d.sessions for p, d in people.items() if d.outings}
    untracked = sorted(p["display"] for p in status.values() if p["state"] == "no_phone")
    return {"generated_at": now, "persons": persons, "untracked": untracked,
            "household": household_empty(tracked, gaps, tz, now, settings.min_outing_minutes * 60, settings.min_samples),
            "settings": {"flap_minutes": settings.flap_minutes, "flap_minutes_bluetooth": settings.flap_minutes_bluetooth,
                         "min_outing_minutes": settings.min_outing_minutes, "min_samples": settings.min_samples,
                         "halflife_days": settings.halflife_days}}


def eta_for_people(conn: sqlite3.Connection, config: dict[str, Any], now: float | None = None,
                   tz: tzinfo | None = None) -> dict[str, dict[str, Any]]:
    """Live predictions per person (for the dashboard chips and Telegram)."""
    now = time.time() if now is None else now
    settings = load_settings(config)
    gaps = bt_cleanup._downtime(conn, now)
    return {person: person_prediction(data, gaps, now, tz, settings)
            for person, data in analyse(conn, config, now, tz).items()}


# ---------------------------------------------------------------------------
# Words (Telegram and the dashboard)
# ---------------------------------------------------------------------------

def describe_prediction(display: str, pred: dict[str, Any], typical: dict[str, Any] | None,
                        now: float, tz: tzinfo | None) -> str:
    """One plain-English sentence about when someone will be home (or leave)."""
    def clock(ts: float) -> str:
        return _local(ts, tz).strftime("%H:%M")

    status = pred.get("status")
    if status == "unreliable":
        return (f"The phone signal for {display} is too noisy to predict from yet; see the data-quality notes on the "
                "reports page.")
    if pred.get("kind") == "leave":
        if status == "ready":
            return f"{display} usually heads out around {clock(pred['median'])} (80% between {clock(pred['low'])} and {clock(pred['high'])}), from {pred['n']} similar days."
        if status == "none_expected":
            return f"{display} doesn't usually go out again at this time of day."
        return f"Not enough history yet to say when {display} usually leaves ({pred.get('n', 0)} of {pred.get('needed', '?')} similar days)."
    if status == "ready":
        mins = max(0, round((pred["median"] - now) / 60))
        when = f"in about {mins} min" if mins < 90 else f"in about {round(mins / 60)} h"
        return (f"{display} is expected home around {clock(pred['median'])} ({when}); 80% chance between "
                f"{clock(pred['low'])} and {clock(pred['high'])}, from {pred['n']} similar {('weekday' if day_type(shifted(pred['leave_ts'], tz)[0]) == WEEKDAY else 'weekend')} outings.")
    if status == "overdue":
        return f"{display} is later than usual: normally back by {clock(pred['usual_high'])} (80% of similar days) and not home yet."
    if status == "long_away":
        return f"{display} has been away for a long time, so there is no usual return time to predict."
    if status == "unknown":
        return f"The scanner was off after {display} left, so I can't predict their return until it sees them arrive again."
    return f"Not enough history yet to predict when {display} will be home ({pred.get('n', 0)} of {pred.get('needed', '?')} similar outings)."


def describe_typical(display: str, typical: dict[str, Any], which: str = "return") -> str | None:
    """'Lilou usually gets home around 18:45 on weekdays (middle half 16:25 to 20:40)...' (or leaves)."""
    verb = "gets home" if which == "return" else "heads out"
    parts = []
    for kind, label in ((WEEKDAY, "weekdays"), (WEEKEND, "weekends")):
        summary = typical.get(kind, {}).get(which, {})
        if summary.get("enough"):
            parts.append(f"around {fmt_hours(summary['median'])} on {label} (middle half {fmt_hours(summary['q1'])} "
                         f"to {fmt_hours(summary['q3'])}, from {summary['n']} outings)")
    return f"{display} usually {verb} " + "; ".join(parts) + "." if parts else None


# ---------------------------------------------------------------------------
# Opt-in "later than usual" alerts
# ---------------------------------------------------------------------------

async def late_alerts_once(db_path: Any, config: dict[str, Any], send: Callable[..., Awaitable[bool]],
                           now: float | None = None, tz: tzinfo | None = None) -> list[str]:
    """Tell Telegram when a listed person is back later than any similar day. Once per absence.

    Off unless ``late_alerts_enabled`` is true; only people in ``late_alerts_people`` are watched,
    and only once the prediction has enough history and a trustworthy signal (never from noisy
    data, never while the scanner was off). The alert waits ``late_alerts_margin_minutes`` past the
    point where the usual window ends.
    """
    settings = load_settings(config)
    if not settings.late_enabled or not settings.late_people:
        return []
    now = time.time() if now is None else now
    loop = asyncio.get_running_loop()

    def work() -> list[tuple[str, str, float]]:
        conn = bt_db.get_connection(db_path)
        try:
            bt_db.ensure_presence_tables(conn)
            found = []
            for person, pred in eta_for_people(conn, config, now, tz).items():
                if person not in settings.late_people or pred.get("kind") != "return" or pred.get("status") != "overdue":
                    continue
                if now < pred["usual_high"] + settings.late_margin_minutes * 60:
                    continue
                row = conn.execute("SELECT leave_ts FROM late_alerts WHERE person = ?", (person,)).fetchone()
                if row and abs(row[0] - pred["leave_ts"]) < 1:
                    continue                                  # already told about this absence
                conn.execute("INSERT INTO late_alerts (person, leave_ts, alerted_at) VALUES (?, ?, ?) "
                             "ON CONFLICT(person) DO UPDATE SET leave_ts = excluded.leave_ts, alerted_at = excluded.alerted_at",
                             (person, pred["leave_ts"], now))
                conn.commit()
                found.append((person, person.title(), pred["usual_high"]))
            return found
        finally:
            conn.close()

    lines = []
    for _person, display, usual_high in await loop.run_in_executor(None, work):
        clock = _local(usual_high, tz).strftime("%H:%M")
        text = (f"\u23f0 <b>{html.escape(display)}</b> is later than usual: normally home by {clock} "
                "(80% of similar days) and not back yet.")
        lines.append(text)
        if settings.late_dry_run:
            logger.info("[dry run] late alert would be sent: %s", text)
        elif not await send(text):
            logger.warning("Could not send the late alert for %s", display)
    return lines


async def run_late_loop(db_path: Any, load_config: Callable[[], dict[str, Any]],
                        send: Callable[..., Awaitable[bool]]) -> None:
    """Check every 5 minutes. Never lets an error end the loop."""
    await asyncio.sleep(180)
    while True:
        try:
            await late_alerts_once(db_path, load_config(), send)
        except Exception:  # noqa: BLE001
            logger.error("Late-arrival check failed", exc_info=True)
        await asyncio.sleep(300)
