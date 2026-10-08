#!/usr/bin/env python3
"""Logging setup shared by every Device Radar process.

Two protections, because the Telegram bot token is part of every Bot API URL
(``https://api.telegram.org/bot<id>:<secret>/getMe``) and the system journal is readable
by other users on the Pi:

* ``httpx`` / ``httpcore`` are held at WARNING, so they stop logging a line (with the full URL)
  for every request they make.
* Anything that still reaches a log, including exception text such as a failed connection,
  is passed through a formatter that replaces a bot token with ``bot<redacted>``.
"""

from __future__ import annotations

import logging
import re

_TOKEN = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{10,}")
REDACTED = "bot<redacted>"
NOISY_LIBRARIES = ("httpx", "httpcore")


def redact(text: str) -> str:
    """The text with any Telegram bot token replaced."""
    return _TOKEN.sub(REDACTED, text)


class RedactingFormatter(logging.Formatter):
    """A normal formatter whose output (message, arguments and traceback) never contains a bot token."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup(level: int, fmt: str, datefmt: str | None = None) -> None:
    """``logging.basicConfig`` plus the two protections above."""
    logging.basicConfig(level=level, format=fmt, datefmt=datefmt)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactingFormatter(fmt, datefmt))
    for name in NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
