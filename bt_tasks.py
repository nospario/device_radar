"""Obsidian task list readers.

Parses the Master Task List (Obsidian Tasks plugin emoji syntax) into two
groups:

* **Due today** — uncompleted tasks whose `📅` due date is exactly today,
  excluding any tagged ``#habit``.
* **Daily habits** — uncompleted tasks tagged ``#habit``. Items with no
  date, due today, or overdue are included; items dated strictly in the
  future are skipped (those are tomorrow's recurrence instance that the
  Tasks plugin stamps when today's is ticked off).
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

logger = logging.getLogger("bt_tasks")

DEFAULT_MASTER_PATH = (
    "/home/nospario/ObsidianVaults/Main/3. Todo Lists/MASTER TASK LIST.md"
)

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


def get_daily_recurring_tasks(
    path: str | Path = DEFAULT_MASTER_PATH,
    *,
    today: date | None = None,
    max_tasks: int = 15,
) -> list[str]:
    """Return uncompleted ``#habit``-tagged tasks from the master list.

    Items with no date, due today, or overdue are all included. Items
    dated strictly in the future are skipped — the Obsidian Tasks plugin
    stamps tomorrow's recurrence when today's is ticked off, and we don't
    want that instance read back as still outstanding today.
    """
    today = today or date.today()
    tasks: list[str] = []
    for done, body in _read_task_lines(Path(path)):
        if done:
            continue
        if not _has_habit_tag(body):
            continue
        due = _extract_date_after(body, _DUE_EMOJI)
        if due is not None and due > today:
            continue
        desc = _clean_description(body)
        if desc:
            tasks.append(desc)
    return tasks[:max_tasks]
