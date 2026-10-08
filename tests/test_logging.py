"""Tests for bt_logging: bot tokens must never reach a log."""

from __future__ import annotations

import io
import logging
import unittest

import bt_logging

# Built from pieces so that no complete token-shaped string sits in the source (secret scanners flag those)
TOKEN = "1234567890" + ":" + "AA" + "Eabcdefghijklmnopqrstuvwxyz-_0123"


class RedactTests(unittest.TestCase):
    def test_a_token_in_a_url_is_replaced(self) -> None:
        text = f"HTTP Request: POST https://api.telegram.org/bot{TOKEN}/getMe \"HTTP/1.1 200 OK\""
        out = bt_logging.redact(text)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn("Eabcdefghij", out)
        self.assertIn("api.telegram.org/bot<redacted>/getMe", out)

    def test_every_occurrence_goes_and_ordinary_text_is_untouched(self) -> None:
        out = bt_logging.redact(f"bot{TOKEN} then bot{TOKEN}; a robot: 12 and bot-farm")
        self.assertEqual(out.count("bot<redacted>"), 2)
        self.assertIn("a robot: 12 and bot-farm", out)

    def test_file_download_urls_are_covered_too(self) -> None:
        out = bt_logging.redact(f"https://api.telegram.org/file/bot{TOKEN}/photos/file_1.jpg")
        self.assertNotIn(TOKEN, out)


class SetupTests(unittest.TestCase):
    def setUp(self) -> None:
        root = logging.getLogger()
        saved = (root.handlers[:], root.level, {n: logging.getLogger(n).level for n in bt_logging.NOISY_LIBRARIES})
        root.handlers = []
        self.addCleanup(self.restore, saved)
        bt_logging.setup(logging.INFO, "%(name)s: %(message)s")
        self.stream = io.StringIO()
        root.handlers[0].setStream(self.stream)

    def restore(self, saved) -> None:
        root = logging.getLogger()
        root.handlers, root.level = saved[0], saved[1]
        for name, level in saved[2].items():
            logging.getLogger(name).setLevel(level)

    def test_http_libraries_stop_logging_every_request(self) -> None:
        logging.getLogger("httpx").info("HTTP Request: POST https://api.telegram.org/bot%s/getMe", TOKEN)
        logging.getLogger("httpcore.connection").debug("connect")
        self.assertEqual(self.stream.getvalue(), "")

    def test_a_token_in_a_warning_is_still_redacted(self) -> None:
        logging.getLogger("httpx").warning("failed: https://api.telegram.org/bot%s/sendMessage", TOKEN)
        out = self.stream.getvalue()
        self.assertIn("failed", out)
        self.assertNotIn(TOKEN, out)

    def test_a_token_in_a_traceback_is_redacted(self) -> None:
        try:
            raise RuntimeError(f"cannot reach https://api.telegram.org/bot{TOKEN}/getMe")
        except RuntimeError:
            logging.getLogger("bt_telegram").error("boom", exc_info=True)
        out = self.stream.getvalue()
        self.assertIn("boom", out)
        self.assertIn("RuntimeError", out)
        self.assertNotIn(TOKEN, out)

    def test_our_own_logging_is_unchanged(self) -> None:
        logging.getLogger("bt_scanner").info("Scan finished: %d devices", 12)
        self.assertEqual(self.stream.getvalue(), "bt_scanner: Scan finished: 12 devices\n")


if __name__ == "__main__":
    unittest.main()
