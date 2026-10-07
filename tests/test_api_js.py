"""Tests for the dashboard's api() helper, run through node (skipped if node is absent)."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "static" / "app.js"

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const scenario = JSON.parse(process.argv[1]);
const noop = () => {};
const location = { pathname: scenario.path, search: scenario.search, href: 'unchanged' };
const fetch = async () => ({ status: scenario.status, ok: scenario.status >= 200 && scenario.status < 300,
                             json: async () => ({ hello: 'world' }) });
const doc = { getElementById: () => ({ value: '', addEventListener: noop }), querySelectorAll: () => [],
              addEventListener: noop, createElement: () => ({}) };
const ctx = { document: doc, window: { location }, localStorage: { getItem: () => null, setItem: noop },
              fetch, setInterval: noop, console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.env.APP_JS, 'utf8'), ctx);
(async () => {
  let result, error = null;
  try { result = await vm.runInContext('api', ctx)('/api/devices/AA/x', {method: 'PATCH'}); }
  catch (e) { error = e.message; }
  console.log(JSON.stringify({ result: result || null, error, href: location.href }));
})();
"""


def run(status: int, path: str = "/device/AA:BB", search: str = "") -> dict:
    result = subprocess.run(["node", "-e", HARNESS, json.dumps({"status": status, "path": path, "search": search})],
                            capture_output=True, text=True, timeout=20,
                            env={"APP_JS": str(APP_JS), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ApiHelperTests(unittest.TestCase):
    def test_success_returns_the_json_and_does_not_navigate(self) -> None:
        out = run(200)
        self.assertEqual((out["result"], out["error"], out["href"]), ({"hello": "world"}, None, "unchanged"))

    def test_login_required_goes_to_the_login_page_and_remembers_where_to_come_back_to(self) -> None:
        out = run(401, "/device/AA:BB:CC", "?tab=2")
        self.assertEqual(out["error"], "login required")
        self.assertEqual(out["href"], "/login?next=%2Fdevice%2FAA%3ABB%3ACC%3Ftab%3D2")

    def test_other_errors_still_just_throw(self) -> None:
        for status in (400, 404, 500):
            out = run(status)
            self.assertEqual((out["error"], out["href"]), (f"HTTP {status}", "unchanged"), status)

    def test_the_return_address_cannot_escape_the_site(self) -> None:
        # whatever is in the path, it is encoded into the query value, never into the address itself
        out = run(401, "//evil.com/x", "")
        self.assertTrue(out["href"].startswith("/login?next="))
        self.assertNotIn("//evil.com", out["href"])


if __name__ == "__main__":
    unittest.main()
