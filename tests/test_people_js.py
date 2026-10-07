"""Tests for the dashboard's "who's home" strip, run through node (skipped if node is absent)."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "static" / "app.js"

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const people = JSON.parse(process.argv[1]);
const strip = { innerHTML: '', hidden: false };
const noop = () => {};
// Emulates the DOM trick escapeHtml() relies on: textContent in, escaped innerHTML out
// (a real browser escapes & < > in text but not quotes).
const createElement = () => { let t = '';
  return { set textContent(v) { t = String(v); },
           get innerHTML() { return t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); } }; };
const doc = { getElementById: (id) => id === 'people-strip' ? strip : { value: '', addEventListener: noop },
              querySelectorAll: () => [], addEventListener: noop, createElement };
const NOW = Date.now() / 1000;
const ctx = { document: doc, window: {}, localStorage: { getItem: () => null, setItem: noop },
              fetch: noop, setInterval: noop, console, Date };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.env.APP_JS, 'utf8'), ctx);
// timestamps in the test data are offsets from now, in seconds
const fixed = people.map(p => ({...p,
  since: p.since == null ? null : NOW - p.since, last_seen: p.last_seen == null ? null : NOW - p.last_seen}));
vm.runInContext('renderPeople', ctx)(fixed);
const texts = fixed.map(p => vm.runInContext('peopleChipText', ctx)(p));
console.log(JSON.stringify({ html: strip.innerHTML, hidden: strip.hidden, texts }));
"""


def run(people: list[dict]) -> dict:
    result = subprocess.run(["node", "-e", HARNESS, json.dumps(people)], capture_output=True, text=True,
                            timeout=20, env={"APP_JS": str(APP_JS), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def person(state, display="Amy", **kw) -> dict:
    return {"person": display.lower(), "display": display, "state": state, "since": None, "last_seen": None,
            "phones": ["Amy's iPhone"], "others": [], **kw}


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PeopleStripTests(unittest.TestCase):
    def test_no_people_hides_the_strip(self) -> None:
        out = run([])
        self.assertTrue(out["hidden"])

    def test_chip_wording_for_each_state(self) -> None:
        out = run([person("home", since=3 * 3600), person("home", "Bob"), person("away", "Cat", since=2 * 86400),
                   person("away", "Dan", last_seen=3600), person("away", "Eve"), person("no_phone", "Fay", phones=[])])
        self.assertEqual(out["texts"], ["arrived 3h ago", "home", "left 2d ago", "last seen 1h ago", "away",
                                        "no phone tracked"])
        self.assertFalse(out["hidden"])

    def test_chip_classes_and_names(self) -> None:
        html = run([person("home"), person("away", "Bob"), person("no_phone", "Cat", phones=[])])["html"]
        for cls in ("person-home", "person-away", "person-no_phone"):
            self.assertIn(cls, html)
        for name in ("Amy", "Bob", "Cat"):
            self.assertIn(f"<strong>{name}</strong>", html)

    def test_names_are_html_escaped(self) -> None:
        html = run([person("home", "<img src=x onerror=alert(1)>")])["html"]
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_a_quote_in_a_device_name_cannot_break_out_of_the_tooltip_attribute(self) -> None:
        evil = 'x" onmouseover="alert(1)'
        html = run([person("home", phones=[evil])])["html"]
        self.assertNotIn('onmouseover="alert(1)', html)       # the injected attribute must not exist
        self.assertIn("&quot; onmouseover=&quot;alert(1)", html)  # it is just escaped text inside title
        self.assertEqual(html.count('title="'), 1)

    def test_no_phone_tooltip_explains_what_to_do(self) -> None:
        html = run([person("no_phone", "Cat", phones=[])])["html"]
        self.assertIn("Only phones count as presence", html)


if __name__ == "__main__":
    unittest.main()
