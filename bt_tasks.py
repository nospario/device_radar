"""Obsidian Master Task List reader.

Parses an Obsidian markdown note written with the Tasks plugin emoji syntax
and returns uncompleted tasks that are relevant for today (due on or before
today, or scheduled/started on or before today with no due date).
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

# Tasks plugin date emoji → field name
_DUE_EMOJI = "\U0001F4C5"       # 📅
_SCHEDULED_EMOJI = "\u23F3"     # ⏳
_START_EMOJI = "\U0001F6EB"     # 🛫
_DONE_EMOJI = "\u2705"          # ✅
_CREATED_EMOJI = "\u2795"       # ➕

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
    """Strip date markers, tags and priority emojis to leave the task text."""
    # Remove everything from the first date emoji onwards
    for emoji in (_CREATED_EMOJI, _SCHEDULED_EMOJI, _START_EMOJI, _DUE_EMOJI, _DONE_EMOJI):
        idx = body.find(emoji)
        if idx != -1:
            body = body[:idx]
    # Strip #tags and priority markers
    body = re.sub(r"#\w+", "", body)
    for p in _PRIORITY_EMOJIS:
        body = body.replace(p, "")
    # Simplify Obsidian [[wikilinks|alias]] → alias (or target)
    body = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", body)
    body = re.sub(r"\[\[([^\]]+)\]\]", r"\1", body)
    return " ".join(body.split()).strip()


def get_todays_outstanding_tasks(
    path: str | Path = DEFAULT_MASTER_PATH,
    *,
    today: date | None = None,
    max_tasks: int = 10,
) -> list[str]:
    """Return uncompleted task descriptions due/scheduled on or before today.

    Tasks with no date at all are excluded — they represent backlog, not
    today's work.
    """
    today = today or date.today()
    master = Path(path)
    if not master.exists():
        logger.warning("Master task list not found: %s", master)
        return []

    tasks: list[str] = []
    try:
        lines = master.read_text(encoding="utf-8").splitlines()
    except Exception:
        logger.exception("Failed to read %s", master)
        return []

    for line in lines:
        m = _TASK_LINE_RE.match(line)
        if not m:
            continue
        if m.group("state").lower() == "x":
            continue

        body = m.group("body")

        due = _extract_date_after(body, _DUE_EMOJI)
        scheduled = _extract_date_after(body, _SCHEDULED_EMOJI)
        start = _extract_date_after(body, _START_EMOJI)

        # Include if due today or overdue; or scheduled/started on or before today
        # when there is no due date set.
        relevant = False
        if due is not None and due <= today:
            relevant = True
        elif due is None and ((scheduled and scheduled <= today) or (start and start <= today)):
            relevant = True

        if not relevant:
            continue

        desc = _clean_description(body)
        if desc:
            tasks.append(desc)

    if len(tasks) > max_tasks:
        tasks = tasks[:max_tasks]
    return tasks
