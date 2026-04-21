"""Obsidian task list readers.

Two sources are parsed (Obsidian Tasks plugin emoji syntax):

* **Due today** — non-habit uncompleted tasks in the Master Task List whose
  `📅` due date is exactly today. Overdue and undated backlog are excluded.
* **Daily habits** — uncompleted tasks tagged ``#habit`` in today's Daily
  Note (one file per day, ``YYYY-MM-DD.md``, generated from the Daily
  Template by Obsidian's Daily Notes plugin). Each new day gets a fresh
  note, so we never have to worry about stale/future-dated instances.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from pathlib import Path

logger = logging.getLogger("bt_tasks")

DEFAULT_MASTER_PATH = (
    "/home/nospario/ObsidianVaults/Main/3. Todo Lists/MASTER TASK LIST.md"
)
DEFAULT_DAILY_NOTES_DIR = "/home/nospario/ObsidianVaults/Main/1. Journal"

_TASK_LINE_RE = re.compile(r"^\s*-\s*\[(?P<state>[ xX])\]\s*(?P<body>.*)$")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_HABIT_TAG_RE = re.compile(r"#habit(?![A-Za-z0-9_-])", re.IGNORECASE)

# Tasks plugin date emoji → field name
_DUE_EMOJI = "\U0001F4C5"       # 📅
_SCHEDULED_EMOJI = "\u23F3"     # ⏳
_START_EMOJI = "\U0001F6EB"     # 🛫
_DONE_EMOJI = "\u2705"          # ✅
_CREATED_EMOJI = "\u2795"       # ➕
_RECURRENCE_EMOJI = "\U0001F501"  # 🔁

# Priority emojis to strip from the spoken text
_PRIORITY_EMOJIS = ["\U0001F53A", "\u23EB", "\U0001F53C", "\U0001F53D", "\u23EC"]


def _extract_date_after(body: str, emoji: str) -> date | None:
    """Return the first YYYY-MM-DD date following ``emoji`` in ``body``."""
    idx = body.find(emoji)
    if idx == -1:
        return None
    m = _DATE_RE.search(body, idx)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _clean_description(body: str) -> str:
    """Strip date markers, recurrence, tags and priority emojis."""
    # Remove everything from the first date/recurrence emoji onwards
    cut = len(body)
    for emoji in (
        _CREATED_EMOJI, _SCHEDULED_EMOJI, _START_EMOJI, _DUE_EMOJI,
        _DONE_EMOJI, _RECURRENCE_EMOJI,
    ):
        idx = body.find(emoji)
        if idx != -1 and idx < cut:
            cut = idx
    body = body[:cut]
    # Strip #tags and priority markers
    body = re.sub(r"#\w+", "", body)
    for p in _PRIORITY_EMOJIS:
        body = body.replace(p, "")
    # Simplify Obsidian [[wikilinks|alias]] → alias (or target)
    body = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", body)
    body = re.sub(r"\[\[([^\]]+)\]\]", r"\1", body)
    return " ".join(body.split()).strip()


def _read_task_lines(path: Path) -> list[tuple[bool, str]]:
    """Read a markdown file and yield (done, body) for every `- [ ]` line."""
    if not path.exists():
        logger.info("Task file not found: %s", path)
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        logger.exception("Failed to read %s", path)
        return []

    out: list[tuple[bool, str]] = []
    for line in lines:
        m = _TASK_LINE_RE.match(line)
        if not m:
            continue
        out.append((m.group("state").lower() == "x", m.group("body")))
    return out


def _has_habit_tag(body: str) -> bool:
    """True if the raw task body contains the ``#habit`` tag as a whole word."""
    return bool(_HABIT_TAG_RE.search(body))


def get_todays_outstanding_tasks(
    path: str | Path = DEFAULT_MASTER_PATH,
    *,
    today: date | None = None,
    max_tasks: int = 10,
) -> list[str]:
    """Return uncompleted tasks due today, excluding ``#habit`` items.

    Overdue tasks and tasks with no due date are excluded. Habit-tagged
    tasks are returned by :func:`get_daily_recurring_tasks` instead.
    """
    today = today or date.today()
    tasks: list[str] = []
    for done, body in _read_task_lines(Path(path)):
        if done:
            continue
        if _has_habit_tag(body):
            continue
        due = _extract_date_after(body, _DUE_EMOJI)
        if due != today:
            continue
        desc = _clean_description(body)
        if desc:
            tasks.append(desc)
    return tasks[:max_tasks]


def _todays_daily_note(dir_path: str | Path, today: date) -> Path:
    """Path to today's Daily Note (``<dir>/YYYY-MM-DD.md``)."""
    return Path(dir_path) / f"{today.isoformat()}.md"


def summarize_habits(
    daily_notes_dir: str | Path = DEFAULT_DAILY_NOTES_DIR,
    *,
    for_date: date,
) -> tuple[int, int, list[str]] | None:
    """Summarise ``#habit`` completion for a specific day's Daily Note.

    Returns ``(total, completed, incomplete_descriptions)`` or ``None`` if
    the note doesn't exist. ``incomplete_descriptions`` is the cleaned
    spoken text for each uncompleted habit.
    """
    path = _todays_daily_note(daily_notes_dir, for_date)
    if not path.exists():
        return None

    total = 0
    completed = 0
    incomplete: list[str] = []
    for done, body in _read_task_lines(path):
        if not _has_habit_tag(body):
            continue
        total += 1
        if done:
            completed += 1
        else:
            desc = _clean_description(body)
            if desc:
                incomplete.append(desc)
    return total, completed, incomplete


def get_daily_recurring_tasks(
    daily_notes_dir: str | Path = DEFAULT_DAILY_NOTES_DIR,
    *,
    today: date | None = None,
    max_tasks: int = 15,
) -> list[str]:
    """Return uncompleted ``#habit``-tagged tasks from today's Daily Note.

    Returns an empty list if the note hasn't been created yet (Obsidian's
    Daily Notes plugin creates it on first open each day).
    """
    today = today or date.today()
    path = _todays_daily_note(daily_notes_dir, today)
    tasks: list[str] = []
    for done, body in _read_task_lines(path):
        if done:
            continue
        if not _has_habit_tag(body):
            continue
        desc = _clean_description(body)
        if desc:
            tasks.append(desc)
    return tasks[:max_tasks]


def complete_todays_habits(
    daily_notes_dir: str | Path = DEFAULT_DAILY_NOTES_DIR,
    *,
    today: date | None = None,
) -> int:
    """Mark today's uncompleted ``#habit`` tasks complete in the Daily Note.

    For each uncompleted ``- [ ]`` line tagged ``#habit`` the state is
    flipped to ``- [x]`` and ``✅ YYYY-MM-DD`` is appended (standard Tasks
    plugin format, skipped if already present). Tomorrow's Daily Note is
    created fresh from the Daily Template by the Daily Notes plugin, so we
    don't need to stamp a new instance here. Idempotent — completed lines
    are left alone. Returns the number of habit lines completed.
    """
    today = today or date.today()
    path = _todays_daily_note(daily_notes_dir, today)
    if not path.exists():
        logger.info("Daily note not found: %s", path)
        return 0

    try:
        original = path.read_text(encoding="utf-8")
    except Exception:
        logger.exception("Failed to read %s", path)
        return 0

    # splitlines(keepends=True) preserves \n / \r\n so we can reassemble
    # the file without changing line endings or the trailing newline state.
    lines = original.splitlines(keepends=True)
    new_lines: list[str] = []
    completed = 0

    for line in lines:
        stripped = line.rstrip("\r\n")
        ending = line[len(stripped):]
        m = _TASK_LINE_RE.match(stripped)
        if not m or m.group("state").lower() == "x":
            new_lines.append(line)
            continue
        body = m.group("body")
        if not _has_habit_tag(body):
            new_lines.append(line)
            continue

        # Everything before the `[ ]` marker (indent + `- `)
        marker_idx = stripped.index("[ ]")
        prefix = stripped[:marker_idx]
        completed_body = body
        if _DONE_EMOJI not in completed_body:
            completed_body = completed_body.rstrip() + f" {_DONE_EMOJI} {today.isoformat()}"
        new_lines.append(f"{prefix}[x] {completed_body}{ending}")
        completed += 1

    if completed == 0:
        return 0

    # Atomic replace via temp file in the same directory
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(new_lines), encoding="utf-8")
    tmp.replace(path)
    logger.info("Completed %d habit task(s) in %s", completed, path)
    return completed


def habit_hash(description: str) -> str:
    """Stable short hash of a habit description, for Telegram callback_data.

    Returns the first 16 hex chars of sha256 — short enough to fit inside
    the 64-byte callback_data limit when prefixed, and stable across
    processes so bt-scanner (sender) and bt-telegram (receiver) agree
    without sharing state.
    """
    return hashlib.sha256(description.encode("utf-8")).hexdigest()[:16]


def complete_habit_by_name(
    daily_notes_dir: str | Path = DEFAULT_DAILY_NOTES_DIR,
    *,
    description: str,
    today: date | None = None,
) -> bool:
    """Mark a single uncompleted ``#habit`` line matching ``description``
    complete in today's Daily Note.

    Match is against the cleaned description (the text shown in the bot's
    button). Returns True if a line was updated. First match wins if there
    are duplicates.
    """
    today = today or date.today()
    path = _todays_daily_note(daily_notes_dir, today)
    if not path.exists():
        logger.info("Daily note not found: %s", path)
        return False

    try:
        original = path.read_text(encoding="utf-8")
    except Exception:
        logger.exception("Failed to read %s", path)
        return False

    lines = original.splitlines(keepends=True)
    new_lines: list[str] = []
    done = False

    for line in lines:
        if done:
            new_lines.append(line)
            continue
        stripped = line.rstrip("\r\n")
        ending = line[len(stripped):]
        m = _TASK_LINE_RE.match(stripped)
        if not m or m.group("state").lower() == "x":
            new_lines.append(line)
            continue
        body = m.group("body")
        if not _has_habit_tag(body):
            new_lines.append(line)
            continue
        if _clean_description(body) != description:
            new_lines.append(line)
            continue

        marker_idx = stripped.index("[ ]")
        prefix = stripped[:marker_idx]
        completed_body = body
        if _DONE_EMOJI not in completed_body:
            completed_body = completed_body.rstrip() + f" {_DONE_EMOJI} {today.isoformat()}"
        new_lines.append(f"{prefix}[x] {completed_body}{ending}")
        done = True

    if not done:
        return False

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(new_lines), encoding="utf-8")
    tmp.replace(path)
    logger.info("Completed habit %r in %s", description, path)
    return True


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Obsidian task utilities")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_complete = sub.add_parser(
        "complete-habits",
        help="Mark today's uncompleted #habit tasks in the Daily Note as complete.",
    )
    p_complete.add_argument(
        "--daily-notes-dir", default=DEFAULT_DAILY_NOTES_DIR,
        help="Daily Notes directory (default: %(default)s)",
    )

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.cmd == "complete-habits":
        count = complete_todays_habits(args.daily_notes_dir)
        print(f"Completed {count} habit task(s)")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
