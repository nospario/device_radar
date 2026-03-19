"""Kitkat Telegram integration for Device Radar.

Provides functions called from bt_telegram.py. Does NOT run its own bot
or polling loop.
"""
from __future__ import annotations

import logging
from typing import Any

import bt_kitkat

log = logging.getLogger("kitkat.telegram")

_kitkat_mode: dict[int, bool] = {}


def is_kitkat_mode(chat_id: int) -> bool:
    return _kitkat_mode.get(chat_id, False)


def set_kitkat_mode(chat_id: int, enabled: bool) -> None:
    _kitkat_mode[chat_id] = enabled


async def handle_message(chat_id: int, text: str, config: dict[str, Any]) -> str:
    """Process a message through Kitkat. Returns formatted response."""
    response, counts, searched = await bt_kitkat.chat_async(
        text, config, chat_source=f"telegram_{chat_id}",
    )
    parts = []
    if searched:
        parts.append("[searched the web]")
    used = [f"{v} {k}" for k, v in counts.items() if v > 0]
    if used:
        parts.append(f"[{', '.join(used)}]")
    prefix = " ".join(parts)
    if prefix:
        return f"{prefix}\n\n{response}"
    return response


async def handle_command(chat_id: int, args: list[str], config: dict[str, Any]) -> str:
    """Handle /kitkat subcommands."""
    if not args:
        current = is_kitkat_mode(chat_id)
        set_kitkat_mode(chat_id, not current)
        state = "ON" if not current else "OFF"
        return f"Kitkat mode {state}"

    cmd = args[0].lower()

    if cmd == "on":
        set_kitkat_mode(chat_id, True)
        return "Kitkat mode ON -- I'll remember what you tell me."

    elif cmd == "off":
        set_kitkat_mode(chat_id, False)
        return "Kitkat mode OFF -- back to regular assistant."

    elif cmd == "memories":
        memories = bt_kitkat.get_memories(config, limit=10)
        if not memories:
            return "No memories stored yet."
        lines = ["Recent memories:"]
        for m in memories:
            lines.append(f"  - {m['fact']}")
        return "\n".join(lines)

    elif cmd == "stats":
        stats = bt_kitkat.get_index_stats(config)
        lines = ["Kitkat index stats:"]
        for source, count in stats.items():
            lines.append(f"  {source}: {count} chunks")
        return "\n".join(lines)

    elif cmd == "remember" and len(args) > 1:
        fact = " ".join(args[1:])
        bt_kitkat.add_manual_memory(config, fact, chat_source=f"telegram_{chat_id}")
        return f"Remembered: {fact}"

    elif cmd == "forget" and len(args) > 1:
        search_text = " ".join(args[1:])
        memories = bt_kitkat.get_memories(config, limit=50)
        matches = [m for m in memories if search_text.lower() in m["fact"].lower()]
        if not matches:
            return f"No memories matching '{search_text}'"
        for m in matches:
            bt_kitkat.delete_memory(config, m["id"])
        return f"Forgot {len(matches)} memory/memories matching '{search_text}'"

    elif cmd == "clear":
        bt_kitkat.clear_conversation(config, chat_source=f"telegram_{chat_id}")
        return "Conversation history cleared."

    elif cmd == "reindex":
        import asyncio
        import bt_kitkat_index
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, bt_kitkat_index.index_all, config)
        return "Reindexing started in background."

    else:
        return (
            "Kitkat commands:\n"
            "  /kitkat -- toggle on/off\n"
            "  /kitkat on|off\n"
            "  /kitkat memories -- list recent\n"
            "  /kitkat stats -- index stats\n"
            "  /kitkat remember <fact>\n"
            "  /kitkat forget <text>\n"
            "  /kitkat clear -- clear history\n"
            "  /kitkat reindex"
        )
