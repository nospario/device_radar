"""Tests for dashboard JavaScript helpers, run through node (skipped if node is absent).

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "static" / "app.js"

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const filters = JSON.parse(process.argv[1]);
const devices = JSON.parse(process.argv[2]);
const noop = () => {};
const doc = {
  getElementById: (id) => ({ value: filters[id] || '', addEventListener: noop, checked: false }),
  querySelectorAll: () => [],
  addEventListener: noop,
};
const ctx = { document: doc, window: {}, localStorage: { getItem: () => null, setItem: noop },
              fetch: noop, setInterval: noop, console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.env.APP_JS, 'utf8'), ctx);
const out = vm.runInContext('applyColumnFilters', ctx)(devices).map(d => d.mac_address);
console.log(JSON.stringify(out));
"""

DEVICES = [
    {"mac_address": "A", "advertised_name": "iPhone.lan", "ip_address": "192.168.1.158", "manufacturer": None},
    {"mac_address": "B", "friendly_name": "Dryer", "advertised_name": "HS100.lan",
     "ip_address": "192.168.1.198", "manufacturer": "TP-Link"},
    {"mac_address": "C", "advertised_name": None, "ip_address": None, "manufacturer": "Apple"},
]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class NameFilterTests(unittest.TestCase):
    def search(self, text: str) -> list[str]:
        result = subprocess.run(
            ["node", "-e", HARNESS, json.dumps({"col-filter-name": text}), json.dumps(DEVICES)],
            capture_output=True, text=True, timeout=20, env={"APP_JS": str(APP_JS), "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_empty_filter_matches_everything(self) -> None:
        self.assertEqual(self.search(""), ["A", "B", "C"])

    def test_matches_displayed_name(self) -> None:
        self.assertEqual(self.search("iphone"), ["A"])
        self.assertEqual(self.search("dryer"), ["B"])

    def test_matches_ip_address(self) -> None:
        self.assertEqual(self.search("192.168.1.158"), ["A"])
        self.assertEqual(self.search("192.168.1.19"), ["B"])

    def test_matches_manufacturer(self) -> None:
        self.assertEqual(self.search("tp-link"), ["B"])
        self.assertEqual(self.search("apple"), ["C"])

    def test_unknown_placeholder_still_matches_unnamed(self) -> None:
        self.assertEqual(self.search("(unknown)"), ["C"])

    def test_friendly_name_hides_advertised_name_from_search(self) -> None:
        # B shows as "Dryer", so its hostname must not match (unchanged behaviour).
        self.assertEqual(self.search("hs100"), [])

    def test_no_match(self) -> None:
        self.assertEqual(self.search("zzz"), [])


if __name__ == "__main__":
    unittest.main()
