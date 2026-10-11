"""Tests for the reports page code in static/app.js, run through node (skipped if node is absent)."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "static" / "app.js"

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const job = JSON.parse(process.argv[1]);     // {expr: "js expression using the page's functions", data: any}
const noop = () => {};
const els = {};
const el = (id) => els[id] || (els[id] = { id, innerHTML: '', textContent: '', hidden: false, value: '', addEventListener: noop });
const createElement = () => { let t = '';
  return { set textContent(v) { t = String(v); },
           get innerHTML() { return t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); } }; };
const doc = { getElementById: el, querySelectorAll: () => [], addEventListener: noop, createElement };
const ctx = { document: doc, window: {}, localStorage: { getItem: () => null, setItem: noop }, fetch: noop,
              setInterval: noop, console, Date, data: job.data };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.env.APP_JS, 'utf8'), ctx);
const result = vm.runInContext(job.expr, ctx);
console.log(JSON.stringify({ result, root: el('reports-root').innerHTML, meta: el('reports-meta').textContent }));
"""


def js(expr: str, data=None) -> dict:
    result = subprocess.run(["node", "-e", HARNESS, json.dumps({"expr": expr, "data": data})], capture_output=True,
                            text=True, timeout=20, env={"APP_JS": str(APP_JS), "PATH": "/usr/bin:/bin", "TZ": "Europe/London"})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.attrs: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        self.tags.append(tag)
        self.attrs += [name for name, _ in attrs]


def _parse(html: str) -> _Tags:
    parser = _Tags()
    parser.feed(html)
    return parser


def attribute_names(html: str) -> list[str]:
    return _parse(html).attrs


def tag_names(html: str) -> list[str]:
    return _parse(html).tags


def day(date="2026-10-07", weekday="Wed", home=None, unknown=None, future=None) -> dict:
    return {"date": date, "weekday": weekday, "home": home or [], "unknown": unknown or [], "future_from": future}


SUMMARY = {"n": 40, "enough": True, "median": 17.5, "q1": 17.0, "q3": 18.0, "p10": 16.5, "p90": 19.0}
NOT_ENOUGH = {"n": 3, "enough": False}


def person(**over) -> dict:
    base = {
        "person": "sam", "display": "Sam", "state": "away", "phones": ["Sam's iPhone"],
        "quality": {"verdict": "good", "signal": "WiFi", "observed_days": 40, "outings": 38, "home_periods": 50,
                    "home_periods_per_day": 1.2, "reasons": []},
        "typical": {"weekday": {"leave": SUMMARY, "return": SUMMARY, "hours_out": {**SUMMARY, "median": 9.5}},
                    "weekend": {"leave": NOT_ENOUGH, "return": NOT_ENOUGH, "hours_out": NOT_ENOUGH}},
        "daily": [{"date": "2026-10-06", "weekday": "Tue", "home_h": 14.0, "observed_h": 24, "coverage": 1, "partial": False},
                  {"date": "2026-10-07", "weekday": "Wed", "home_h": 5.0, "observed_h": 10, "coverage": 0.4, "partial": True}],
        "timeline": [day("2026-10-06", "Tue", home=[[0, 8], [17.5, 24]]), day("2026-10-07", "Wed", home=[[0, 8]], future=10)],
        "trend": {"enough": False, "n_recent": 2, "n_before": 3},
        "prediction": {"kind": "return", "status": "ready"},
        "prediction_text": "Sam is expected home around 17:40.", "typical_text": "Sam usually gets home around 17:30.",
        "backtest": {"tests": 30, "hit_rate": 0.8, "median_error_min": 25, "naive_error_min": 60}, "method": "time_of_day",
    }
    base.update(over)
    return base


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class FormattingTests(unittest.TestCase):
    def test_fmt_hours_matches_the_python_side(self) -> None:
        out = js("[fmtHours(18 + 40/60), fmtHours(24.5), fmtHours(5.9999), fmtHours(27.999), fmtHours(4)]")
        self.assertEqual(out["result"], ["18:40", "00:30", "06:00", "04:00", "04:00"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TimelineTests(unittest.TestCase):
    def svg(self, rows) -> str:
        return js("timelineSvg(data)", rows)["result"]

    def test_one_track_per_day_and_segments_as_rectangles(self) -> None:
        svg = self.svg([day(home=[[0, 8], [17.5, 24]], unknown=[[10, 12]], future=20), day("2026-10-08", "Thu")])
        self.assertEqual(svg.count('class="track"'), 2)
        self.assertEqual(svg.count('class="home"'), 2)
        self.assertEqual(svg.count('class="unknown"'), 1)
        self.assertEqual(svg.count('class="future"'), 1)

    def test_geometry_stays_inside_the_chart(self) -> None:
        import re
        svg = self.svg([day(home=[[-3, 8], [17.5, 30]], future=24)])
        for x, w in re.findall(r'<rect class="(?:home|unknown|future)" x="([\d.]+)" y="[\d.]+" width="([\d.]+)"', svg):
            self.assertGreaterEqual(float(x), 54)
            self.assertLessEqual(float(x) + float(w), 54 + 720 + 0.01)
        self.assertNotIn("NaN", svg)
        self.assertNotIn("undefined", svg)

    def test_labels_are_escaped(self) -> None:
        svg = self.svg([day(weekday="<b>x</b>")])
        self.assertNotIn("<b>x", svg)
        self.assertIn("&lt;b&gt;x", svg)

    def test_empty_input_is_still_valid_svg(self) -> None:
        svg = self.svg([])
        self.assertTrue(svg.startswith("<svg") and svg.endswith("</svg>"))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ChartWidthTests(unittest.TestCase):
    def viewbox_widths(self, client_width) -> list[int]:
        import re
        set_width = "" if client_width is None else f"document.getElementById('reports-root').clientWidth = {client_width}; "
        root = js(set_width + "renderReports(data)", {"persons": [person()], "untracked": [], "generated_at": 1790000000})["root"]
        return [int(w) for w in re.findall(r'viewBox="0 0 (\d+) \d+"', root)]

    def test_charts_are_drawn_at_the_width_of_the_card(self) -> None:
        # a 1000px container less the card's padding and border: both charts fit it exactly
        self.assertEqual(self.viewbox_widths(1000), [962, 962])

    def test_unknown_width_falls_back_to_a_default_size(self) -> None:
        self.assertEqual(self.viewbox_widths(None), [782, 782])

    def test_extreme_widths_are_clamped(self) -> None:
        self.assertEqual(self.viewbox_widths(100), [300, 300])
        self.assertEqual(self.viewbox_widths(9000), [1800, 1800])

    def test_timeline_geometry_follows_the_width(self) -> None:
        import re
        svg = js("timelineSvg(data, 1000)", [day(home=[[0, 24]])])["result"]
        self.assertIn('viewBox="0 0 1000 ', svg)
        x, w = re.search(r'<rect class="home" x="([\d.]+)" y="[\d.]+" width="([\d.]+)"', svg).groups()
        self.assertAlmostEqual(float(x) + float(w), 1000 - 8, places=1)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DailyBarsTests(unittest.TestCase):
    def test_bars_scale_with_hours_and_partial_days_are_marked(self) -> None:
        import re
        rows = [{"date": "2026-10-05", "weekday": "Mon", "home_h": 24, "observed_h": 24, "partial": False},
                {"date": "2026-10-06", "weekday": "Tue", "home_h": 12, "observed_h": 24, "partial": False},
                {"date": "2026-10-07", "weekday": "Wed", "home_h": 3, "observed_h": 6, "partial": True}]
        svg = js("dailyBarsSvg(data)", rows)["result"]
        heights = [float(h) for h in re.findall(r'<rect class="bar[^"]*" x="[\d.]+" y="[\d.]+" width="[\d.]+" height="([\d.]+)"', svg)]
        self.assertEqual(len(heights), 3)
        self.assertAlmostEqual(heights[0], 66.0, places=1)
        self.assertAlmostEqual(heights[1], 33.0, places=1)
        self.assertEqual(svg.count("bar partial"), 1)
        self.assertIn("scanner watched 6 h", svg)

    def test_out_of_range_values_are_clamped(self) -> None:
        import re
        svg = js("dailyBarsSvg(data)", [{"date": "2026-10-05", "weekday": "Mon", "home_h": 99, "observed_h": 24, "partial": False},
                                        {"date": "2026-10-06", "weekday": "Tue", "home_h": -5, "observed_h": 24, "partial": False}])["result"]
        heights = [float(h) for h in re.findall(r'height="([\d.]+)"><title>', svg)]
        self.assertEqual(heights, [66.0, 0.0])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TextBlockTests(unittest.TestCase):
    def test_trend_variants(self) -> None:
        few = js("trendHtml(data)", {"enough": False, "n_recent": 2, "n_before": 3})["result"]
        self.assertIn("Not enough recent data", few)
        steady = js("trendHtml(data)", {"enough": True, "clear": False, "shift_min": 12, "low_min": -20, "high_min": 40})["result"]
        self.assertIn("steady", steady)
        self.assertIn("12 min later", steady)
        later = js("trendHtml(data)", {"enough": True, "clear": True, "shift_min": 55, "low_min": 30, "high_min": 80})["result"]
        self.assertIn("moved 55 min later", later)
        earlier = js("trendHtml(data)", {"enough": True, "clear": True, "shift_min": -40, "low_min": -70, "high_min": -10})["result"]
        self.assertIn("moved 40 min earlier", earlier)

    def test_accuracy_variants(self) -> None:
        none = js("accuracyHtml(data)", {"backtest": {"tests": 0}})["result"]
        self.assertIn("Not enough history", none)
        full = js("accuracyHtml(data)", {"backtest": {"tests": 30, "hit_rate": 0.8, "median_error_min": 25, "naive_error_min": 60}})["result"]
        self.assertIn("30 past outings", full)
        self.assertIn("80% of the time", full)
        self.assertIn("off by 25 min", full)
        self.assertIn("off by 60 min", full)
        no_naive = js("accuracyHtml(data)", {"backtest": {"tests": 12, "hit_rate": 0.5, "median_error_min": 70, "naive_error_min": None}})["result"]
        self.assertNotIn("guessing the usual time", no_naive)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class RenderReportsTests(unittest.TestCase):
    def render(self, report) -> str:
        return js("renderReports(data)", report)["root"]

    def test_empty_state_explains_how_to_get_started(self) -> None:
        html = self.render({"persons": [], "untracked": ["Laura"]})
        self.assertIn("No phones are being tracked yet", html)
        self.assertIn("Laura", html)

    def test_a_person_card_has_every_section(self) -> None:
        html = self.render({"persons": [person()], "untracked": [], "household": {"people": []}, "generated_at": 1790000000})
        for needle in ("Sam", "q-good", "Sam is expected home around 17:40.", "Typical times", "17:30", "middle half 17:00",
                       "not enough data yet (3 so far)", "Last 14 days at home", "Hours at home per day", "Trend",
                       "How reliable are the predictions?", "Data quality: good", "timeline-svg", "bars-svg"):
            self.assertIn(needle, html, needle)

    def test_average_hours_use_only_fully_watched_days(self) -> None:
        html = self.render({"persons": [person()], "untracked": []})
        self.assertIn("weekdays 14.0 h", html)           # the partial Wednesday (5 h) is not averaged in
        self.assertIn("weekends \u2014", html)

    def test_untrusted_text_is_escaped_everywhere(self) -> None:
        evil = '<img src=x onerror=alert(1)>'
        card = person(display=evil, prediction_text=evil, typical_text=evil,
                      quality={"verdict": 'good" onmouseover="alert(1)', "signal": evil, "observed_days": 1, "outings": 1,
                               "home_periods": 1, "home_periods_per_day": 1, "reasons": [evil]})
        html = self.render({"persons": [card], "untracked": [evil], "household": {"people": [evil], "weekday": {}, "weekend": {},
                                                                                    "empty_h_per_day": 1, "observed_days": 3}})
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)
        # parse it like a browser would: no tag may have gained an event-handler attribute or a stray tag
        self.assertEqual([a for a in attribute_names(html) if a.startswith("on")], [])
        self.assertNotIn("img", tag_names(html))
        self.assertIn("q-good&quot; onmouseover=&quot;alert(1)", html)      # kept inside the class value, as text

    def test_noisy_people_show_the_warning_badge_and_reasons(self) -> None:
        card = person(quality={"verdict": "noisy", "signal": "Bluetooth", "observed_days": 20, "outings": 50, "home_periods": 100,
                               "home_periods_per_day": 5.0, "reasons": ["5.0 separate home periods a day"]},
                      prediction_text="The phone signal for Sam is too noisy to predict from yet.")
        html = self.render({"persons": [card], "untracked": []})
        self.assertIn("q-noisy", html)
        self.assertIn("5.0 separate home periods a day", html)
        self.assertIn("too noisy", html)

    def test_household_section(self) -> None:
        hh = {"people": ["lilou", "sam"], "periods": 12, "empty_h_per_day": 6.5, "observed_days": 30,
              "weekday": {"empty_from": {**SUMMARY, "median": 8.5}, "empty_until": {**SUMMARY, "median": 15.25}},
              "weekend": {"empty_from": NOT_ENOUGH, "empty_until": NOT_ENOUGH}}
        html = self.render({"persons": [person()], "untracked": ["Laura"], "household": hh})
        self.assertIn("The house", html)
        self.assertIn("6.5 h a day", html)
        self.assertIn("08:30", html)
        self.assertIn("15:15", html)
        self.assertIn("Weekends: <span class=\"text-dim\">not enough data yet", html)
        self.assertIn("Laura has no phone tracked", html)
        two = self.render({"persons": [person()], "untracked": ["Laura", "Kim"], "household": hh})
        self.assertIn("Laura, Kim have no phone tracked", two)

    def test_generated_time_is_shown(self) -> None:
        out = js("renderReports(data)", {"persons": [person()], "untracked": [], "generated_at": 1790000000})
        self.assertRegex(out["meta"], r"^Updated \d\d:\d\d$")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DashboardChipTests(unittest.TestCase):
    def chip(self, p) -> str:
        return js("peopleChipText(data)", p)["result"]

    def test_away_person_with_a_prediction_shows_the_expected_time(self) -> None:
        text = self.chip({"state": "away", "since": None, "last_seen": None,
                          "prediction": {"kind": "return", "status": "ready", "median": 1790000000}})
        self.assertRegex(text, r"expected ~\d\d:\d\d")

    def test_a_phone_gone_quiet_overnight_is_not_shown_as_a_departure_with_an_eta(self) -> None:
        text = self.chip({"state": "away", "since": None, "last_seen": None, "prediction": {"kind": "return", "status": "phone_quiet", "usual_wake": 10.4}})
        self.assertIn("phone quiet, probably asleep at home", text)
        self.assertNotIn("expected", text)

    def test_overdue_says_later_than_usual(self) -> None:
        self.assertIn("later than usual", self.chip({"state": "away", "prediction": {"kind": "return", "status": "overdue"}}))

    def test_no_clutter_when_there_is_nothing_useful_to_say(self) -> None:
        for pred in ({"kind": "return", "status": "insufficient"}, {"kind": "return", "status": "unreliable"},
                     {"kind": "return", "status": "unknown"}, {"kind": "return", "status": "long_away"}, None):
            text = self.chip({"state": "away", "since": None, "last_seen": None, "prediction": pred})
            self.assertEqual(text, "away")

    def test_people_who_are_home_do_not_show_an_eta(self) -> None:
        text = self.chip({"state": "home", "since": None, "prediction": {"kind": "leave", "status": "ready", "median": 1790000000}})
        self.assertEqual(text, "home")


WEEKDAYS = [{"weekday": i, "name": name, "n": n, "leave": SUMMARY if n >= 3 else NOT_ENOUGH, "return": SUMMARY if n >= 3 else NOT_ENOUGH}
            for i, (name, n) in enumerate([("Monday", 14), ("Tuesday", 12), ("Wednesday", 9), ("Thursday", 5), ("Friday", 11), ("Saturday", 2), ("Sunday", 0)])]
BASE = {"method": "time_of_day", "boost": 1.0, "tests": 74, "hit_rate": 0.69, "median_error_min": 197, "mean_error_min": 262}


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class WeekdayReportTests(unittest.TestCase):
    def test_the_table_lists_monday_to_sunday_with_counts(self) -> None:
        html = js("weekdayTableHtml(data)", WEEKDAYS)["result"]
        for name in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
            self.assertIn(f"<th>{name}</th>", html)
        self.assertLess(html.index("Monday"), html.index("Sunday"))
        self.assertIn("<th>Monday</th><td>14</td>", html)
        self.assertIn("<th>Sunday</th><td>0</td>", html)
        self.assertIn("Gets home", html)

    def test_days_with_few_outings_are_faded_and_explained(self) -> None:
        html = js("weekdayTableHtml(data)", WEEKDAYS)["result"]
        self.assertEqual(html.count('class="few-outings"'), 3)                                      # Thursday (5), Saturday (2), Sunday (0)
        self.assertRegex(html, r'<tr class="few-outings"><th>Thursday</th>')
        self.assertRegex(html, r'<tr class=""><th>Monday</th>')
        self.assertIn("fewer than 8 outings", html)
        self.assertIn("not enough data to be precise yet", html)

    def test_times_and_the_empty_cells(self) -> None:
        html = js("weekdayTableHtml(data)", WEEKDAYS)["result"]
        self.assertIn("<strong>17:30</strong>", html)
        self.assertRegex(html, r"<th>Sunday</th><td>0</td><td><span class=\"text-dim\">\u2014</span></td>")

    def test_no_table_without_rows(self) -> None:
        self.assertEqual(js("weekdayTableHtml(data)", [])["result"], "")
        self.assertEqual(js("weekdayTableHtml(data)", None)["result"], "")

    def test_the_weighting_in_use_is_explained_with_the_test_result(self) -> None:
        w = {"mode": "auto", "adopted": True, "boost": 4, "baseline": {**BASE, "mean_error_min": 65}, "chosen": {**BASE, "boost": 4.0, "tests": 37, "mean_error_min": 46}, "agreeing": 4, "needed_tests": 15}
        text = js("weightingHtml(data)", {"weighting": w})["result"]
        self.assertIn("The same day of the week counts 4\u00d7 extra.", text)
        self.assertIn("cut the average error from 65 min to 46 min", text)
        self.assertIn("37 past outings", text)
        self.assertIn("4 of the weightings tried agreed", text)

    def test_not_enough_data_to_test_yet(self) -> None:
        w = {"mode": "auto", "adopted": False, "boost": 1.0, "baseline": {**BASE, "tests": 6}, "tried": [], "needed_tests": 15}
        text = js("weightingHtml(data)", {"weighting": w})["result"]
        self.assertIn("Not enough past outings yet", text)
        self.assertIn("6 of 15 test predictions needed", text)
        self.assertIn("switches on by itself", text)
        none = js("weightingHtml(data)", {"weighting": {"mode": "auto", "adopted": False, "baseline": {"tests": 0}, "needed_tests": 15}})["result"]
        self.assertIn("0 of 15", none)

    def test_tested_but_not_shown_to_help(self) -> None:
        w = {"mode": "auto", "adopted": False, "boost": 1.0, "baseline": BASE, "best_alternative": {**BASE, "boost": 16.0, "mean_error_min": 237}, "needed_tests": 15}
        text = js("weightingHtml(data)", {"weighting": w})["result"]
        self.assertIn("has not been shown to help yet", text)
        self.assertIn("16\u00d7 would give 237 min against 262 min now, from 74 test predictions", text)

    def test_forced_and_off(self) -> None:
        self.assertIn("<strong>3\u00d7</strong> as a setting", js("weightingHtml(data)", {"weighting": {"mode": "forced", "boost": 3}})["result"])
        self.assertIn("switched off in the settings", js("weightingHtml(data)", {"weighting": {"mode": "off", "boost": 1}})["result"])

    def test_old_reports_without_the_new_fields_still_render(self) -> None:
        self.assertEqual(js("weightingHtml(data)", {})["result"], "")
        html = js("personReportHtml(data, 600)", person())["result"]
        self.assertIn("How reliable are the predictions?", html)
        self.assertIn("By day of the week", html)                                                  # heading is there, the table just empty

    def test_the_person_card_shows_both(self) -> None:
        w = {"mode": "auto", "adopted": False, "boost": 1.0, "baseline": {**BASE, "tests": 6}, "needed_tests": 15}
        html = js("personReportHtml(data, 600)", person(weekdays=WEEKDAYS, weighting=w))["result"]
        self.assertIn('class="report-table weekday-table"', html)
        self.assertIn('class="report-line weighting-line"', html)
        self.assertLess(html.index("By day of the week"), html.index("Last 14 days at home"))

    def test_nothing_from_the_server_can_inject_html(self) -> None:
        evil = '<img src=x onerror=alert(1)>'
        rows = [{**WEEKDAYS[0], "name": evil}]
        html = js("weekdayTableHtml(data)", rows)["result"]
        self.assertNotIn("<img", html)


if __name__ == "__main__":
    unittest.main()
