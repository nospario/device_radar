"""Tests for the dashboard login. Run: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bt_auth  # noqa: E402
import bt_db  # noqa: E402
import bt_web  # noqa: E402

MAC = "AA:BB:CC:00:00:01"
PASSWORD = "correct horse battery"


class AuthModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "web_auth.json"

    def test_nothing_stored_means_not_configured(self) -> None:
        self.assertEqual(bt_auth.load(self.path), {})
        self.assertFalse(bt_auth.is_configured({}))
        self.assertFalse(bt_auth.check_password({}, "anything"))

    def test_corrupt_or_foreign_files_are_treated_as_not_configured(self) -> None:
        for content in ("not json", "[1, 2]", '"text"', "null"):
            self.path.write_text(content)
            self.assertEqual(bt_auth.load(self.path), {}, content)

    def test_set_password_stores_a_hash_not_the_password(self) -> None:
        bt_auth.set_password(self.path, PASSWORD)
        raw = self.path.read_text()
        self.assertNotIn(PASSWORD, raw)
        data = json.loads(raw)
        self.assertTrue(data["password_hash"].startswith("scrypt:"))
        self.assertEqual(len(data["secret"]), 64)
        self.assertTrue(bt_auth.check_password(data, PASSWORD))
        self.assertFalse(bt_auth.check_password(data, "wrong password"))
        self.assertFalse(bt_auth.check_password(data, ""))
        self.assertFalse(bt_auth.check_password(data, None))

    def test_the_file_is_private_and_no_temp_file_is_left(self) -> None:
        bt_auth.set_password(self.path, PASSWORD)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["web_auth.json"])

    def test_short_passwords_are_refused_and_change_nothing(self) -> None:
        with self.assertRaises(ValueError):
            bt_auth.set_password(self.path, "short")
        self.assertFalse(self.path.exists())
        bt_auth.set_password(self.path, PASSWORD)
        before = self.path.read_text()
        with self.assertRaises(ValueError):
            bt_auth.set_password(self.path, "1234567")
        self.assertEqual(self.path.read_text(), before)

    def test_changing_the_password_rotates_the_secret_and_salt(self) -> None:
        bt_auth.set_password(self.path, PASSWORD)
        first = bt_auth.load(self.path)
        bt_auth.set_password(self.path, PASSWORD)
        second = bt_auth.load(self.path)
        self.assertNotEqual(first["secret"], second["secret"])           # logs everyone out
        self.assertNotEqual(first["password_hash"], second["password_hash"])   # fresh salt

    def test_remove(self) -> None:
        bt_auth.set_password(self.path, PASSWORD)
        self.assertTrue(bt_auth.remove(self.path))
        self.assertFalse(bt_auth.remove(self.path))
        self.assertFalse(bt_auth.is_configured(bt_auth.load(self.path)))

    def test_safe_next_only_allows_paths_on_this_site(self) -> None:
        good = ["/", "/device/AA:BB:CC:00:00:01", "/history?page=2", "/alexa#top"]
        for target in good:
            self.assertEqual(bt_auth.safe_next(target), target, target)
        bad = [None, "", "device", "//evil.com", "//evil.com/x", "https://evil.com", "http://evil.com/",
               "/\\evil.com", "/ok\r\nSet-Cookie: x=1", "/ok\nHost: evil", "javascript:alert(1)", "/redir?u=a://b"]
        for target in bad[:-1]:
            self.assertEqual(bt_auth.safe_next(target), "/", repr(target))
        # a "://" only in the query string is harmless
        self.assertEqual(bt_auth.safe_next("/redir?u=a://b"), "/redir?u=a://b")

    @mock.patch("builtins.print")
    def test_cli_status_set_and_remove(self, printed) -> None:
        with mock.patch.object(bt_auth, "AUTH_FILE", self.path):
            self.assertEqual(bt_auth.main(["status"]), 0)
            self.assertIn("No dashboard password", printed.call_args.args[0])
            with mock.patch("getpass.getpass", side_effect=[PASSWORD, PASSWORD]):
                self.assertEqual(bt_auth.main(["set-password"]), 0)
            self.assertTrue(bt_auth.is_configured(bt_auth.load(self.path)))
            self.assertEqual(bt_auth.main(["status"]), 0)
            self.assertIn("password is set", printed.call_args.args[0])
            with mock.patch("getpass.getpass", side_effect=["another password", "different password"]):
                self.assertEqual(bt_auth.main(["set-password"]), 1)         # entries differ: unchanged
            self.assertTrue(bt_auth.check_password(bt_auth.load(self.path), PASSWORD))
            with mock.patch("getpass.getpass", side_effect=["short", "short"]):
                self.assertEqual(bt_auth.main(["set-password"]), 1)         # too short: unchanged
            self.assertTrue(bt_auth.check_password(bt_auth.load(self.path), PASSWORD))
            self.assertEqual(bt_auth.main(["remove"]), 0)
            self.assertFalse(self.path.exists())
            self.assertEqual(bt_auth.main(["bogus"]), 2)


class ThrottleTests(unittest.TestCase):
    def test_blocks_after_five_failures_per_client_and_recovers(self) -> None:
        t = bt_auth.LoginThrottle(5, 900)
        for i in range(5):
            self.assertTrue(t.allowed("a", 1000 + i))
            t.failure("a", 1000 + i)
        self.assertFalse(t.allowed("a", 1010))
        self.assertTrue(t.allowed("b", 1010))                    # other clients unaffected
        self.assertGreater(t.retry_after("a", 1010), 800)
        self.assertTrue(t.allowed("a", 1000 + 901))              # window passed
        self.assertEqual(t.retry_after("a", 1000 + 901), 0)

    def test_success_clears_the_count(self) -> None:
        t = bt_auth.LoginThrottle(3, 900)
        for _ in range(2):
            t.failure("a", 1000)
        t.success("a")
        for _ in range(2):
            t.failure("a", 1001)
        self.assertTrue(t.allowed("a", 1002))


class WebLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "bt_radar.db"
        bt_db.init_db(self.db)
        conn = bt_db.get_connection(self.db)
        bt_db.upsert_device(conn, MAC, advertised_name="Test Device")
        conn.close()
        self.auth = root / "web_auth.json"
        for name, value in (("get_db_path", lambda: self.db), ("load_config", lambda: {}), ("AUTH_FILE", self.auth)):
            patcher = mock.patch.object(bt_web, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        throttle = mock.patch.object(bt_web, "_login_throttle", bt_auth.LoginThrottle())
        throttle.start()
        self.addCleanup(throttle.stop)
        self.client = bt_web.app.test_client()

    def protect(self, password: str = PASSWORD) -> None:
        bt_auth.set_password(self.auth, password)

    def login(self, password: str = PASSWORD, **extra):
        return self.client.post("/login", data={"password": password, **extra})

    def name(self) -> str | None:
        conn = bt_db.get_connection(self.db)
        try:
            return bt_db.get_device(conn, MAC)["friendly_name"]
        finally:
            conn.close()

    def rename(self, value: str):
        return self.client.patch(f"/api/devices/{MAC}", json={"friendly_name": value})

    # -- no password: exactly as before --

    def test_without_a_password_everything_is_open(self) -> None:
        self.assertEqual(self.rename("Open").status_code, 200)
        self.assertEqual(self.name(), "Open")
        self.assertEqual(self.client.post("/api/cleanup/run", json={"dry_run": True}).status_code, 200)

    def test_login_page_explains_how_to_set_a_password_when_none_exists(self) -> None:
        html = self.client.get("/login").get_data(as_text=True)
        self.assertIn("bt_auth.py set-password", html)
        self.assertNotIn('name="password"', html)

    # -- with a password --

    def test_changes_are_refused_without_a_login(self) -> None:
        self.protect()
        r = self.rename("Hacked")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json(), {"error": "login required", "login": "/login"})
        self.assertIsNone(self.name())

    def test_every_write_endpoint_is_protected(self) -> None:
        self.protect()
        calls = [("patch", f"/api/devices/{MAC}"), ("post", "/api/device/x/notifications"),
                 ("post", "/api/cleanup/run"), ("post", f"/api/devices/{MAC}/pair"),
                 ("post", f"/api/devices/{MAC}/unpair"), ("post", f"/api/devices/{MAC}/link"),
                 ("post", f"/api/devices/{MAC}/unlink"), ("post", "/api/echo-devices"),
                 ("patch", "/api/echo-devices/Kitchen"), ("delete", "/api/echo-devices/Kitchen")]
        for method, url in calls:
            with self.subTest(method=method, url=url):
                self.assertEqual(getattr(self.client, method)(url, json={}).status_code, 401)

    def test_every_non_get_route_in_the_app_is_covered(self) -> None:
        """Guards the future: a new write endpoint added without thinking must still need the login."""
        self.protect()
        for rule in bt_web.app.url_map.iter_rules():
            for method in rule.methods - {"GET", "HEAD", "OPTIONS"}:
                if rule.rule in ("/login", "/logout"):
                    continue
                url = rule.rule
                for arg in rule.arguments:
                    url = url.replace(f"<path:{arg}>", "x").replace(f"<{arg}>", "x")
                with self.subTest(method=method, url=url):
                    self.assertEqual(self.client.open(url, method=method, json={}).status_code, 401)

    def test_reading_stays_open(self) -> None:
        self.protect()
        for url in ("/", "/history", "/pairing", "/alexa", "/api/devices", "/api/stats", "/api/people",
                    "/api/health", "/api/cleanup/preview", f"/device/{MAC}", "/login"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_wrong_password_does_not_log_in(self) -> None:
        self.protect()
        r = self.login("not the password")
        self.assertEqual(r.status_code, 401)
        self.assertIn("not right", r.get_data(as_text=True))
        self.assertEqual(self.rename("x").status_code, 401)

    def test_right_password_allows_changes_and_logout_takes_them_away(self) -> None:
        self.protect()
        r = self.login(next="/device/" + MAC)
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/device/" + MAC))
        self.assertEqual(self.rename("Changed").status_code, 200)
        self.assertEqual(self.name(), "Changed")
        self.assertEqual(self.client.post("/logout").status_code, 302)
        self.assertEqual(self.rename("Again").status_code, 401)
        self.assertEqual(self.name(), "Changed")

    def test_login_cannot_redirect_to_another_site(self) -> None:
        self.protect()
        for evil in ("//evil.com", "https://evil.com/x", "/\\evil.com"):
            self.client = bt_web.app.test_client()
            self.assertEqual(self.login(next=evil).headers["Location"], "/", evil)

    def test_too_many_wrong_passwords_lock_the_client_out_even_for_the_right_one(self) -> None:
        self.protect()
        for _ in range(5):
            self.assertEqual(self.login("wrong").status_code, 401)
        r = self.login()                       # correct, but throttled
        self.assertEqual(r.status_code, 429)
        self.assertIn("Too many", r.get_data(as_text=True))
        self.assertEqual(self.rename("x").status_code, 401)

    def test_cookie_is_httponly_and_strict(self) -> None:
        self.protect()
        cookie = self.login().headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_changing_the_password_logs_everyone_out(self) -> None:
        self.protect()
        self.login()
        self.assertEqual(self.rename("ok").status_code, 200)
        self.protect("a brand new password")           # rotates the signing secret
        self.assertEqual(self.rename("after").status_code, 401)
        self.assertEqual(self.login("a brand new password").status_code, 302)
        self.assertEqual(self.rename("after").status_code, 200)

    def test_removing_the_password_opens_the_dashboard_again(self) -> None:
        self.protect()
        self.assertEqual(self.rename("x").status_code, 401)
        bt_auth.remove(self.auth)
        self.assertEqual(self.rename("open again").status_code, 200)

    def signed_cookie(self, key: str, payload: dict) -> str:
        """A session cookie signed exactly the way Flask does it, with the given key."""
        from flask.sessions import SecureCookieSessionInterface
        from itsdangerous import URLSafeTimedSerializer
        iface = SecureCookieSessionInterface()
        return URLSafeTimedSerializer(
            key, salt=iface.salt, serializer=iface.serializer,
            signer_kwargs={"key_derivation": iface.key_derivation, "digest_method": iface.digest_method},
        ).dumps(payload)

    def test_a_forged_session_cookie_does_not_work(self) -> None:
        self.protect()
        # control: a cookie signed with the real secret is accepted, so this test can tell the difference
        real = bt_auth.load(self.auth)["secret"]
        self.client.set_cookie("session", self.signed_cookie(real, {"auth": True}))
        self.assertEqual(self.rename("legit").status_code, 200)
        # the same cookie signed with any other key is not
        self.client = bt_web.app.test_client()
        for wrong in ("not the secret", "0" * 64, ""):
            self.client.set_cookie("session", self.signed_cookie(wrong or "x", {"auth": True}))
            self.assertEqual(self.rename("forged").status_code, 401, wrong)
        self.assertEqual(self.name(), "legit")

    def test_an_unsigned_or_tampered_cookie_does_not_work(self) -> None:
        self.protect()
        for value in ("eyJhdXRoIjp0cnVlfQ", "eyJhdXRoIjp0cnVlfQ.aaaa.bbbb", "garbage", "True"):
            self.client = bt_web.app.test_client()
            self.client.set_cookie("session", value)
            self.assertEqual(self.rename("x").status_code, 401, value)

    def test_a_cookie_from_while_no_password_was_set_cannot_become_a_login_later(self) -> None:
        # while the dashboard is open the app signs with a throwaway per-process key
        cookie = self.signed_cookie(bt_web._FALLBACK_KEY, {"auth": True})
        self.protect()
        self.client.set_cookie("session", cookie)
        self.assertEqual(self.rename("sneaky").status_code, 401)

    def test_passwords_and_hashes_never_appear_in_responses(self) -> None:
        self.protect()
        bodies = [self.client.get("/login").get_data(as_text=True), self.login("wrong").get_data(as_text=True),
                  self.login().get_data(as_text=True), self.client.get("/").get_data(as_text=True)]
        for body in bodies:
            self.assertNotIn(PASSWORD, body)
            self.assertNotIn("scrypt:", body)

    def test_security_headers(self) -> None:
        r = self.client.get("/")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["Referrer-Policy"], "same-origin")

    def test_navigation_shows_log_in_or_log_out_only_when_relevant(self) -> None:
        self.assertNotIn("Log in", self.client.get("/").get_data(as_text=True))
        self.assertNotIn("Log out", self.client.get("/").get_data(as_text=True))
        self.protect()
        self.assertIn("Log in", self.client.get("/").get_data(as_text=True))
        self.login()
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Log out", page)
        self.assertNotIn(">Log in<", page)

    def test_login_when_already_logged_in_just_redirects(self) -> None:
        self.protect()
        self.login()
        self.assertEqual(self.client.get("/login?next=/history").headers["Location"], "/history")


if __name__ == "__main__":
    unittest.main()
