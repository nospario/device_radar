"""Tests for the dashboard health panel, run through node (skipped if node is absent)."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "static" / "app.js"

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const calls = JSON.parse(process.argv[1]);          // list of health payloads, rendered one after another
const noop = () => {};
// A <details> that remembers whether the user opened or closed it, like a browser does
const details = { open: false };
const el = { innerHTML: '', hidden: true, className: '',
  querySelector(sel) { return sel === 'details' && this.innerHTML.includes('<details') ? details : null; } };
const createElement = () => { let t = '';
  return { set textContent(v) { t = String(v); },
           get innerHTML() { return t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); } }; };
const doc = { getElementById: (id) => id === 'health-strip' ? el : { value: '', addEventListener: noop },
              querySelectorAll: () => [], addEventListener: noop, createElement };
const ctx = { document: doc, window: {}, localStorage: { getItem: () => null, setItem: noop },
              fetch: noop, setInterval: noop, console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.env.APP_JS, 'utf8'), ctx);
const render = vm.runInContext('renderHealth', ctx);
const out = [];
for (const call of calls) {
  if (call.userOpen !== undefined) details.open = call.userOpen;     // the user toggled it
  render(call.health);
  out.push({ html: el.innerHTML, hidden: el.hidden, cls: el.className });
}
console.log(JSON.stringify(out));
"""


def render(*calls: dict) -> list[dict]:
    result = subprocess.run(["node", "-e", HARNESS, json.dumps(list(calls))], capture_output=True, text=True,
                            timeout=20, env={"APP_JS": str(APP_JS), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def health(checks=(), worst=0, stale=False, summary="all 2 checks OK") -> dict:
    checks = list(checks)
    return {"checks": checks, "worst": worst, "stale": stale, "summary": summary,
            "problems": sum(1 for c in checks if c["status"] != "ok")}


def chk(label, status="ok", message="fine") -> dict:
    return {"key": label.lower(), "label": label, "status": status, "message": message}


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class HealthPanelTests(unittest.TestCase):
    def test_all_ok_is_green_and_collapsed(self) -> None:
        out = render({"health": health([chk("A"), chk("B")])})[0]
        self.assertFalse(out["hidden"])
        self.assertIn("health-strip-ok", out["cls"])
        self.assertNotIn("<details open", out["html"])
        self.assertIn("Health: all 2 checks OK", out["html"])

    def test_problems_open_the_panel_and_colour_it_by_the_worst_one(self) -> None:
        out = render({"health": health([chk("A", "warn", "meh"), chk("B", "fail", "bad")], worst=2, summary="2 problems")})[0]
        self.assertIn("health-strip-fail", out["cls"])
        self.assertIn("<details open", out["html"])
        self.assertIn("health-fail", out["html"])
        self.assertIn("meh", out["html"])

    def test_a_warning_only_is_amber(self) -> None:
        out = render({"health": health([chk("A", "warn")], worst=1, summary="1 problem")})[0]
        self.assertIn("health-strip-warn", out["cls"])

    def test_stale_results_are_amber_even_if_every_check_was_ok(self) -> None:
        out = render({"health": health([chk("A")], stale=True, summary="the health watchdog has not reported for 2 h")})[0]
        self.assertIn("health-strip-warn", out["cls"])
        self.assertIn("has not reported for 2 h", out["html"])

    def test_the_users_open_or_closed_choice_survives_a_refresh(self) -> None:
        ok = health([chk("A")])
        bad = health([chk("A", "fail")], worst=2, summary="1 problem")
        # user opens the panel while everything is fine: it stays open on the next refresh
        out = render({"health": ok}, {"health": ok, "userOpen": True}, {"health": ok})
        self.assertIn("<details open", out[2]["html"])
        # user closes it while there is a problem: a refresh does not pop it open again
        out = render({"health": bad}, {"health": bad, "userOpen": False}, {"health": bad})
        self.assertNotIn("<details open", out[2]["html"])

    def test_labels_and_messages_are_escaped(self) -> None:
        out = render({"health": health([chk("<img src=x onerror=1>", "fail", '"><script>1</script>')], worst=2,
                                       summary="<b>x</b>")})[0]
        for raw in ("<img", "<script", "<b>x"):
            self.assertNotIn(raw, out["html"])
        self.assertIn("&lt;img", out["html"])

    def test_a_status_with_a_quote_cannot_break_the_class_attribute(self) -> None:
        out = render({"health": health([chk("A", 'x" onmouseover="alert(1)')], worst=2)})[0]
        self.assertNotIn('onmouseover="alert(1)', out["html"])


if __name__ == "__main__":
    unittest.main()
