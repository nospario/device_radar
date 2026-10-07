"""bt_calendar.check_login: the cheap iCloud login probe used by the health watchdog."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import caldav  # noqa: E402
from caldav.lib import error as caldav_error  # noqa: E402

import bt_calendar  # noqa: E402

CONFIG = {"calendar_enabled": True, "calendar_username_env": "T_USER", "calendar_password_env": "T_PASS"}


class LoginProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {"T_USER": "someone@example.com", "T_PASS": "app-password"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_disabled(self) -> None:
        self.assertEqual(bt_calendar.check_login({"calendar_enabled": False})[0], "disabled")
        self.assertEqual(bt_calendar.check_login({})[0], "disabled")

    def test_missing_credentials(self) -> None:
        with mock.patch.dict(os.environ, {"T_PASS": ""}):
            self.assertEqual(bt_calendar.check_login(CONFIG)[0], "no_credentials")

    def test_working_login(self) -> None:
        with mock.patch.object(caldav, "DAVClient") as client:
            self.assertEqual(bt_calendar.check_login(CONFIG), ("ok", "login works"))
        client.assert_called_once()
        self.assertEqual(client.call_args.kwargs["username"], "someone@example.com")
        client.return_value.principal.assert_called_once()

    def test_rejected_login_is_reported_as_auth(self) -> None:
        with mock.patch.object(caldav, "DAVClient") as client:
            client.return_value.principal.side_effect = caldav_error.AuthorizationError(
                url="https://caldav.icloud.com", reason="Unauthorized")
            state, message = bt_calendar.check_login(CONFIG)
        self.assertEqual(state, "auth")
        self.assertIn("app password", message)

    def test_network_trouble_is_reported_as_unreachable_not_auth(self) -> None:
        with mock.patch.object(caldav, "DAVClient") as client:
            client.return_value.principal.side_effect = ConnectionError("no route")
            state, message = bt_calendar.check_login(CONFIG)
        self.assertEqual(state, "unreachable")
        self.assertIn("ConnectionError", message)

    def test_the_password_never_appears_in_any_message(self) -> None:
        with mock.patch.object(caldav, "DAVClient") as client:
            client.return_value.principal.side_effect = RuntimeError("boom")
            self.assertNotIn("app-password", bt_calendar.check_login(CONFIG)[1])


if __name__ == "__main__":
    unittest.main()
