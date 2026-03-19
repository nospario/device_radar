"""Kitkat web interface — Flask blueprint for chat UI and API endpoints."""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, render_template, request

import bt_db
import bt_kitkat

log = logging.getLogger("kitkat.web")

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
BASE_DIR = Path(__file__).resolve().parent

kitkat_bp = Blueprint("kitkat", __name__)


def _load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _get_db_path(config: dict[str, Any] | None = None) -> Path:
    if config is None:
        config = _load_config()
    return BASE_DIR / config.get("db_path", "bt_radar.db")


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

@kitkat_bp.route("/kitkat")
def kitkat_page():
    config = _load_config()
    storage_ok = bt_kitkat._is_storage_available(config)
    return render_template("kitkat.html", active="kitkat", storage_ok=storage_ok)


# ---------------------------------------------------------------------------
# Chat API
# ---------------------------------------------------------------------------

@kitkat_bp.route("/api/kitkat/chat", methods=["POST"])
def api_kitkat_chat():
    data = request.get_json()
    if not data or "message" not in data:
        return jsonify({"error": "message required"}), 400

    config = _load_config()
    response_text, context_counts, searched = bt_kitkat.chat_sync(
        data["message"], config, chat_source="web",
    )
    return jsonify({
        "response": response_text,
        "context_used": context_counts,
        "searched": searched,
    })


# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------

@kitkat_bp.route("/api/kitkat/history")
def api_kitkat_history():
    config = _load_config()
    conn = bt_db.get_connection(_get_db_path(config))
    try:
        history = bt_db.get_chat_history(conn, "kitkat_web", limit=100)
    finally:
        conn.close()
    return jsonify({"messages": history})


@kitkat_bp.route("/api/kitkat/history", methods=["DELETE"])
def api_kitkat_history_clear():
    config = _load_config()
    bt_kitkat.clear_conversation(config, chat_source="web")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------

@kitkat_bp.route("/api/kitkat/memories")
def api_kitkat_memories():
    config = _load_config()
    memories = bt_kitkat.get_memories(config, limit=100)
    return jsonify({"memories": memories})


@kitkat_bp.route("/api/kitkat/memories", methods=["POST"])
def api_kitkat_memories_add():
    data = request.get_json()
    if not data or "fact" not in data:
        return jsonify({"error": "fact required"}), 400

    config = _load_config()
    memory_id = bt_kitkat.add_manual_memory(config, data["fact"], chat_source="manual")
    return jsonify({"ok": True, "id": memory_id})


@kitkat_bp.route("/api/kitkat/memories/<int:memory_id>", methods=["DELETE"])
def api_kitkat_memories_delete(memory_id: int):
    config = _load_config()
    bt_kitkat.delete_memory(config, memory_id)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Stats and management
# ---------------------------------------------------------------------------

@kitkat_bp.route("/api/kitkat/stats")
def api_kitkat_stats():
    config = _load_config()
    stats = bt_kitkat.get_index_stats(config)
    storage_ok = bt_kitkat._is_storage_available(config)
    return jsonify({"stats": stats, "storage_available": storage_ok})


@kitkat_bp.route("/api/kitkat/reindex", methods=["POST"])
def api_kitkat_reindex():
    config = _load_config()
    # Run in background thread
    def _do_reindex():
        try:
            import bt_kitkat_index
            bt_kitkat_index.index_all(config)
        except Exception as e:
            log.error("Reindex failed: %s", e)

    threading.Thread(target=_do_reindex, daemon=True).start()
    return jsonify({"ok": True, "message": "Reindexing started"})


@kitkat_bp.route("/api/kitkat/reset", methods=["POST"])
def api_kitkat_reset():
    data = request.get_json()
    if not data or not data.get("confirm"):
        return jsonify({"error": "confirm required"}), 400

    config = _load_config()
    bt_kitkat.reset_memories(config)
    return jsonify({"ok": True, "message": "All memories and history cleared"})
