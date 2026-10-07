#!/usr/bin/env python3
"""Dashboard login: protects everything that *changes* data (edit, pair, link, cleanup...).

Reading the dashboard stays open to your network. Changing anything needs the
password. Until a password is set the dashboard behaves as it always did (open);
the health watchdog warns that none is set.

The password is stored only as a salted scrypt hash, together with the secret that
signs login cookies, in ``web_auth.json`` beside the code (mode 0600, git-ignored).
Setting a new password also makes a new secret, which logs everyone out.

Set or change it (run as root, from the project directory, in a terminal)::

    sudo python3 bt_auth.py set-password
    sudo python3 bt_auth.py status
    sudo python3 bt_auth.py remove        # go back to an open dashboard
"""

from __future__ import annotations

import getpass
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
AUTH_FILE = BASE_DIR / "web_auth.json"

MIN_PASSWORD_LENGTH = 8


def load(path: Path) -> dict[str, Any]:
    """The stored auth data, or ``{}`` if there is none or it is unreadable."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_configured(auth: dict[str, Any]) -> bool:
    return bool(auth.get("password_hash")) and bool(auth.get("secret"))


def set_password(path: Path, password: str) -> None:
    """Store a new password hash and a fresh cookie-signing secret (0600, written atomically)."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"the password must be at least {MIN_PASSWORD_LENGTH} characters")
    path = Path(path)
    data = {"password_hash": generate_password_hash(password), "secret": secrets.token_hex(32)}
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def remove(path: Path) -> bool:
    try:
        Path(path).unlink()
        return True
    except FileNotFoundError:
        return False


def check_password(auth: dict[str, Any], password: str) -> bool:
    if not is_configured(auth):
        return False
    return check_password_hash(auth["password_hash"], password or "")


def safe_next(target: str | None) -> str:
    """Only allow redirecting to a path on this site (never to another host)."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    if "\\" in target or any(ch in target for ch in ("\r", "\n", "\t")) or "://" in target.split("?", 1)[0]:
        return "/"
    return target


class LoginThrottle:
    """Allow only a few wrong passwords per client within a time window (in memory)."""

    def __init__(self, max_failures: int = 5, window: float = 900.0) -> None:
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, list[float]] = {}

    def _recent(self, client: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(client, []) if now - t < self.window]
        if recent:
            self._failures[client] = recent
        else:
            self._failures.pop(client, None)
        return recent

    def allowed(self, client: str, now: float | None = None) -> bool:
        return len(self._recent(client, time.time() if now is None else now)) < self.max_failures

    def retry_after(self, client: str, now: float | None = None) -> int:
        """Seconds until the oldest counted failure expires (0 if not blocked)."""
        now = time.time() if now is None else now
        recent = self._recent(client, now)
        if len(recent) < self.max_failures:
            return 0
        return max(1, int(self.window - (now - min(recent))))

    def failure(self, client: str, now: float | None = None) -> None:
        self._failures.setdefault(client, []).append(time.time() if now is None else now)

    def success(self, client: str) -> None:
        self._failures.pop(client, None)


def main(argv: list[str] | None = None) -> int:
    args = (argv if argv is not None else sys.argv[1:]) or ["status"]
    command = args[0]
    if command == "set-password":
        first = getpass.getpass("New dashboard password: ")
        if getpass.getpass("Repeat it: ") != first:
            print("The two entries did not match; nothing was changed.")
            return 1
        try:
            set_password(AUTH_FILE, first)
        except ValueError as exc:
            print(f"Not changed: {exc}.")
            return 1
        print(f"Password set ({AUTH_FILE}). Everyone has been logged out; the dashboard now asks for it before changes.")
        return 0
    if command == "status":
        print("A dashboard password is set." if is_configured(load(AUTH_FILE)) else
              "No dashboard password is set: anyone on the network can change devices.")
        return 0
    if command == "remove":
        print("Password removed; the dashboard is open again." if remove(AUTH_FILE) else "There was no password to remove.")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
