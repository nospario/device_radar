# Kitkat Standalone — Decoupled Personal Memory Agent

## Why Decouple

Kitkat is a personal AI knowledge agent. Device Radar is an IoT presence tracker. They share a Pi but serve fundamentally different purposes. The current coupling forces Kitkat into Device Radar's sync Flask architecture, which prevents the single most impactful feature: **streaming responses**.

With streaming, the first token appears in ~1 second (warm model) instead of the user waiting 90+ seconds for a complete response. On CPU-only hardware this is the difference between usable and painful.

Secondary benefits: independent restarts, resource isolation, cleaner Telegram integration, proper async throughout, faster iteration without risking Device Radar stability.

## Key Improvements Over Coupled Version

| Change | Impact |
|---|---|
| **sqlite-vec replaces ChromaDB** | Saves 200-500MB RAM, eliminates ~30 transitive dependencies, single database file for everything |
| **FTS5 hybrid retrieval** | Catches keyword matches that embeddings miss (names, project names, dates). Zero extra dependencies — built into SQLite |
| **SSE streaming** | First token in ~1s instead of 90s+ wait. Same hardware, same model, transformative UX |
| **Ollama keep_alive + model preload** | Ensures the chat model is always warm. Eliminates 17s cold-start penalty |
| **Markdown rendering** | Responses with code blocks, lists, and formatting render properly instead of showing raw markdown |
| **Smaller embedding model option** | `snowflake-arctic-embed:xs` (22M params, 90MB) is 3x smaller and 4x faster than nomic-embed-text. Configurable |
| **FastAPI + uvicorn** | Async-native, doesn't block Device Radar during long Kitkat chats |
| **Standalone Telegram bot** | Own identity, no `/kitkat` prefix, direct conversation |

## Architecture

Kitkat becomes a fully standalone application with its own web server, Telegram bot, database, and config file. It shares the Ollama server, external drive, and calendar credentials with Device Radar via environment variables.

```
┌─────────────────┐     ┌─────────────────┐
│  Device Radar   │     │     Kitkat       │
│  Flask :8080    │     │  FastAPI :8081   │
│  bt_telegram    │     │  kitkat_bot      │
│  bt_scanner     │     │  kitkat_indexer  │
└────────┬────────┘     └────────┬────────┘
         │                       │
         └───────┬───────────────┘
                 │
         ┌───────▼───────┐
         │    Ollama      │
         │  :11434        │
         └───────┬───────┘
                 │
         ┌───────▼───────┐
         │  /mnt/external │
         │  kitkat.db     │
         │  (sqlite-vec   │
         │   + FTS5)      │
         └───────────────┘
```

### Services

| Service | Port/Socket | Description |
|---|---|---|
| `kitkat-web` | `:8081` | FastAPI + uvicorn — web UI and streaming API |
| `kitkat-bot` | — | Standalone Telegram bot (own token) |
| `kitkat-indexer` | — | Background indexer (Obsidian, Google Drive, calendar) |

### Directory Structure

```
/var/www/kitkat/                    # Development
/opt/kitkat/                        # Production
├── kitkat_core.py                  # RAG engine (vector search, context retrieval, memory extraction)
├── kitkat_chat.py                  # Ollama chat with streaming + optional web search
├── kitkat_db.py                    # SQLite + sqlite-vec + FTS5 (unified storage)
├── kitkat_index.py                 # Background indexer (Obsidian, GDrive, calendar)
├── kitkat_calendar.py              # Calendar fetching (extracted from bt_calendar)
├── kitkat_web.py                   # FastAPI app — streaming API + web UI
├── kitkat_bot.py                   # Standalone Telegram bot
├── kitkat.json                     # Configuration (own file)
├── requirements.txt                # Own dependencies
├── deploy.sh                       # Deploy script
├── kitkat-web.service              # Systemd: FastAPI web server
├── kitkat-bot.service              # Systemd: Telegram bot
├── kitkat-indexer.service          # Systemd: background indexer
├── templates/
│   └── index.html                  # Web chat UI with SSE streaming + markdown
└── static/
    ├── style.css                   # Standalone dark theme
    └── marked.min.js               # Markdown rendering (40KB)
```

### Data Layout

All persistent data on the external drive in a **single SQLite database** (no separate ChromaDB directory):

```
/mnt/external/kitkat/
├── kitkat.db           # SQLite — vectors (sqlite-vec), FTS5 index, memories, chat history
├── index_state.json    # Indexer state (file hashes, timestamps)
└── logs/               # Service logs (rotated)
```

## Shared Resources

Kitkat and Device Radar share these via environment variables (already in `/home/pi/.device-radar.env`):

| Resource | Env Var(s) | Notes |
|---|---|---|
| Ollama | `OLLAMA_URL` (new, default `http://localhost:11434`) | Shared server, no conflict |
| Calendar creds | `APPLE_ID_EMAIL`, `APPLE_ID_APP_PASSWORD` | Read-only, no conflict |
| Web search | `OLLAMA_API_KEY` | Shared Ollama cloud key |
| Kitkat Telegram | `KITKAT_TELEGRAM_TOKEN`, `KITKAT_TELEGRAM_CHAT_ID` | **New** bot token from BotFather |

Add to `/home/pi/.device-radar.env`:
```
KITKAT_TELEGRAM_TOKEN=<new token from BotFather>
KITKAT_TELEGRAM_CHAT_ID=<same chat ID as Device Radar>
```

## Configuration

`kitkat.json` — standalone config, no shared keys with Device Radar:

```json
{
  "ollama_url": "http://localhost:11434",
  "ollama_model": "llama3.2:3b",
  "ollama_timeout_seconds": 300,
  "ollama_keep_alive": -1,
  "embedding_model": "nomic-embed-text:latest",
  "embedding_dimensions": 768,
  "web_port": 8081,
  "data_dir": "/mnt/external/kitkat",
  "obsidian_path": "/home/nospario/ObsidianVaults/Main",
  "gdrive_path": "/home/nospario/gdrive",
  "gdrive_extensions": [".md", ".txt", ".pdf", ".docx", ".csv", ".json", ".py", ".sh", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".html", ".xml"],
  "index_interval_minutes": 30,
  "max_context_chunks": 5,
  "fts_boost": 0.3,
  "conversation_history_length": 20,
  "memory_dedup_threshold": 0.25,
  "calendar_enabled": true,
  "calendar_url": "https://caldav.icloud.com",
  "calendar_username_env": "APPLE_ID_EMAIL",
  "calendar_password_env": "APPLE_ID_APP_PASSWORD",
  "calendar_cache_minutes": 15,
  "web_search_enabled": true,
  "telegram_enabled": true,
  "telegram_token_env": "KITKAT_TELEGRAM_TOKEN",
  "telegram_chat_id_env": "KITKAT_TELEGRAM_CHAT_ID",
  "system_prompt": "You are Kitkat, a personal AI assistant running locally on a Raspberry Pi. You have access to memories from past conversations, notes from an Obsidian vault, files from Google Drive, and calendar events. Use retrieved context naturally — reference it when relevant but don't dump everything you know. Be conversational, warm, and concise. If you learn something new about the user, acknowledge it naturally. Never use emoji.",
  "memory_extraction_prompt": "Extract discrete facts about the user from this conversation exchange. Return ONLY a JSON array of strings, each being one fact. Focus on: personal preferences, biographical details, work information, relationships, goals, opinions, routines, and anything the user would expect to be remembered. If no facts are extractable, return []. Do not include facts about yourself (the assistant). Do not include trivial conversational filler."
}
```

**Key config notes:**
- `ollama_keep_alive: -1` — keeps the chat model permanently loaded in RAM. Eliminates the 17-second cold-start penalty. The model stays warm between requests.
- `embedding_dimensions` — must match the model (768 for nomic-embed-text, 384 for snowflake-arctic-embed:xs). Used to create the sqlite-vec virtual table.
- `fts_boost: 0.3` — weight given to FTS5 keyword matches during hybrid retrieval (0.0 = vector only, 1.0 = equal weight). 0.3 means vector similarity dominates but keyword hits get a meaningful boost.
- To switch to the smaller/faster embedding model, change `embedding_model` to `snowflake-arctic-embed:xs` and `embedding_dimensions` to `384`, then reindex.

## Module Specifications

### kitkat_db.py — Unified SQLite Storage

Replaces ChromaDB entirely. Single database with sqlite-vec for vectors, FTS5 for keyword search, and regular tables for memories and chat history.

```python
"""Kitkat database — sqlite-vec vectors, FTS5 full-text, memories, chat history.

All storage in one SQLite database on the external drive.
"""
from __future__ import annotations

import sqlite3
import struct
import time
from pathlib import Path
from typing import Any

import sqlite_vec


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with WAL mode, busy timeout, and sqlite-vec loaded."""
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | Path, embedding_dim: int = 768) -> None:
    """Create all tables if they don't exist."""
    conn = get_connection(db_path)
    conn.executescript(f"""
        -- Chat history
        CREATE TABLE IF NOT EXISTS chat_history (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id   TEXT NOT NULL,
            role      TEXT NOT NULL,
            content   TEXT NOT NULL,
            timestamp REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chat_ts
            ON chat_history(chat_id, timestamp DESC);

        -- Memories (extracted facts)
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fact TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'conversation',
            chat_source TEXT NOT NULL DEFAULT 'web',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            is_active INTEGER NOT NULL DEFAULT 1
        );

        -- Document chunks (metadata for vectors and FTS)
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            indexed_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_collection
            ON chunks(collection);
    """)

    # sqlite-vec virtual table for vector similarity search
    # Uses the chunks table rowid for joining
    conn.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
            embedding float[{embedding_dim}]
        )
    """)

    # FTS5 virtual table for keyword search
    # content= links to chunks table so FTS stays in sync
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            content,
            collection,
            content=chunks,
            content_rowid=id,
            tokenize='porter unicode61'
        )
    """)

    # FTS triggers to keep in sync with chunks table
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, content, collection)
            VALUES (new.id, new.content, new.collection);
        END;
        CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, content, collection)
            VALUES ('delete', old.id, old.content, old.collection);
        END;
        CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, content, collection)
            VALUES ('delete', old.id, old.content, old.collection);
            INSERT INTO chunks_fts(rowid, content, collection)
            VALUES (new.id, new.content, new.collection);
        END;
    """)

    conn.commit()
    conn.close()


def _serialize_vec(vec: list[float]) -> bytes:
    """Serialize a float vector to bytes for sqlite-vec."""
    return struct.pack(f"{len(vec)}f", *vec)


# ---------------------------------------------------------------------------
# Vector + FTS operations
# ---------------------------------------------------------------------------

def add_chunks(
    db_path: str | Path,
    collection: str,
    documents: list[str],
    embeddings: list[list[float]],
    metadatas: list[dict] | None = None,
) -> list[int]:
    """Insert document chunks with vectors and FTS index. Returns row IDs."""
    import json
    conn = get_connection(db_path)
    row_ids = []
    try:
        for i, (doc, emb) in enumerate(zip(documents, embeddings)):
            meta = json.dumps(metadatas[i]) if metadatas and i < len(metadatas) else "{}"
            cur = conn.execute(
                "INSERT INTO chunks (collection, content, metadata_json) VALUES (?, ?, ?)",
                (collection, doc, meta),
            )
            row_id = cur.lastrowid
            row_ids.append(row_id)
            conn.execute(
                "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                (row_id, _serialize_vec(emb)),
            )
        conn.commit()
    finally:
        conn.close()
    return row_ids


def delete_chunks_by_metadata(
    db_path: str | Path, collection: str, key: str, value: str,
) -> int:
    """Delete chunks where metadata[key] == value. Returns count deleted."""
    import json
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM chunks WHERE collection = ? AND json_extract(metadata_json, ?) = ?",
            (collection, f"$.{key}", value),
        ).fetchall()
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM chunks WHERE id IN ({placeholders})", ids)
        conn.commit()
        return len(ids)
    finally:
        conn.close()


def delete_chunks_by_collection(db_path: str | Path, collection: str) -> int:
    """Delete all chunks in a collection. Returns count deleted."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM chunks WHERE collection = ?", (collection,),
        ).fetchall()
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM chunks WHERE id IN ({placeholders})", ids)
        conn.commit()
        return len(ids)
    finally:
        conn.close()


def count_chunks(db_path: str | Path, collection: str) -> int:
    """Count chunks in a collection."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM chunks WHERE collection = ?", (collection,),
        ).fetchone()
        return row["c"]
    finally:
        conn.close()


def hybrid_search(
    db_path: str | Path,
    query_text: str,
    query_embedding: list[float],
    collections: list[str],
    n_results: int = 8,
    fts_boost: float = 0.3,
    max_distance: float = 0.8,
) -> list[dict[str, Any]]:
    """Hybrid search combining sqlite-vec cosine similarity and FTS5 BM25.

    Returns list of dicts: {id, collection, content, metadata, distance, score}.
    Sorted by combined score (lower = better).

    The algorithm:
    1. Vector search: query sqlite-vec for top N*2 nearest neighbors
    2. FTS search: query FTS5 for top N*2 keyword matches
    3. Reciprocal rank fusion: combine rankings with fts_boost weight
    4. Return top N results
    """
    import json
    conn = get_connection(db_path)
    try:
        coll_placeholders = ",".join("?" * len(collections))
        n_candidates = n_results * 3  # Over-fetch for better fusion

        # --- Vector search ---
        vec_results = {}
        vec_rows = conn.execute(f"""
            SELECT v.rowid, v.distance, c.collection, c.content, c.metadata_json
            FROM chunks_vec v
            JOIN chunks c ON c.id = v.rowid
            WHERE c.collection IN ({coll_placeholders})
              AND v.embedding MATCH ?
              AND k = ?
            ORDER BY v.distance
        """, (*collections, _serialize_vec(query_embedding), n_candidates)).fetchall()

        for rank, row in enumerate(vec_rows):
            if row["distance"] > max_distance:
                continue
            vec_results[row["rowid"]] = {
                "id": row["rowid"],
                "collection": row["collection"],
                "content": row["content"],
                "metadata": json.loads(row["metadata_json"]),
                "distance": row["distance"],
                "vec_rank": rank,
            }

        # --- FTS5 search ---
        fts_results = {}
        # Escape FTS5 special characters in query
        fts_query = " OR ".join(
            w for w in query_text.split() if len(w) > 2
        )
        if fts_query:
            try:
                fts_rows = conn.execute(f"""
                    SELECT f.rowid, rank as bm25_rank,
                           c.collection, c.content, c.metadata_json
                    FROM chunks_fts f
                    JOIN chunks c ON c.id = f.rowid
                    WHERE chunks_fts MATCH ?
                      AND c.collection IN ({coll_placeholders})
                    ORDER BY rank
                    LIMIT ?
                """, (fts_query, *collections, n_candidates)).fetchall()

                for rank, row in enumerate(fts_rows):
                    fts_results[row["rowid"]] = {
                        "id": row["rowid"],
                        "collection": row["collection"],
                        "content": row["content"],
                        "metadata": json.loads(row["metadata_json"]),
                        "fts_rank": rank,
                    }
            except sqlite3.OperationalError:
                pass  # FTS query syntax error — skip keyword search

        # --- Reciprocal rank fusion ---
        all_ids = set(vec_results.keys()) | set(fts_results.keys())
        scored = []
        k = 60  # RRF constant

        for doc_id in all_ids:
            vec_score = 1.0 / (k + vec_results[doc_id]["vec_rank"]) if doc_id in vec_results else 0
            fts_score = (fts_boost / (k + fts_results[doc_id]["fts_rank"])) if doc_id in fts_results else 0
            combined = vec_score + fts_score

            # Use vector result as base (has distance), fall back to FTS
            entry = vec_results.get(doc_id) or fts_results.get(doc_id)
            entry["rrf_score"] = combined
            if "distance" not in entry:
                entry["distance"] = 0.5  # FTS-only results get default distance
            scored.append(entry)

        scored.sort(key=lambda x: -x["rrf_score"])  # Higher RRF score = better
        return scored[:n_results]

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Chat history
# ---------------------------------------------------------------------------

def save_message(db_path: str | Path, chat_id: str, role: str, content: str) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO chat_history (chat_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def get_history(
    db_path: str | Path, chat_id: str, limit: int = 20,
) -> list[dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT role, content, timestamp FROM chat_history "
            "WHERE chat_id = ? ORDER BY timestamp DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


def clear_history(db_path: str | Path, chat_id: str) -> int:
    conn = get_connection(db_path)
    try:
        cur = conn.execute("DELETE FROM chat_history WHERE chat_id = ?", (chat_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------

def save_memory(
    db_path: str | Path, fact: str, source: str, chat_source: str,
) -> int:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO memories (fact, source, chat_source) VALUES (?, ?, ?)",
            (fact, source, chat_source),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_memories(
    db_path: str | Path, limit: int = 50, active_only: bool = True,
) -> list[dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        where = "WHERE is_active = 1" if active_only else ""
        rows = conn.execute(
            f"SELECT id, fact, source, chat_source, created_at, is_active "
            f"FROM memories {where} ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def deactivate_memory(db_path: str | Path, memory_id: int) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute("UPDATE memories SET is_active = 0 WHERE id = ?", (memory_id,))
        conn.commit()
    finally:
        conn.close()
```

**Key design decisions:**
- **Every function opens/closes its own connection**. SQLite WAL mode handles concurrent readers/writers. This is simpler than threading connections and safe with the background memory extraction thread.
- **Chunks table** is the single source of truth for all indexed content (obsidian, gdrive, calendar, conversation memories). The `collection` column distinguishes them.
- **chunks_vec** is a sqlite-vec virtual table linked by rowid to chunks. Stores float vectors.
- **chunks_fts** is an FTS5 content-sync table — automatically kept in sync with chunks via triggers. Uses Porter stemming and Unicode tokenization.
- **hybrid_search** uses reciprocal rank fusion to merge vector and keyword results. The `fts_boost` parameter controls how much weight keyword matches get relative to semantic similarity.
- **No ChromaDB dependency**. The entire database is ~160KB of additional code (sqlite-vec wheel) instead of ChromaDB's 200-500MB RAM footprint and 30+ transitive packages.

### kitkat_chat.py — Ollama Chat with Streaming

This replaces the bt_search dependency. Implements streaming natively and manages model keep_alive.

```python
"""Ollama chat with streaming and optional web search."""
from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import ollama

log = logging.getLogger("kitkat.chat")

_SEARCH_KEYWORDS = [
    "search", "look up", "google", "find out", "latest", "current",
    "recent", "today", "right now", "breaking", "news", "update",
    "what's happening", "who won", "score", "weather in", "price of",
]


def _needs_search(user_message: str) -> bool:
    text = user_message.lower()
    return any(kw in text for kw in _SEARCH_KEYWORDS)


async def preload_model(config: dict[str, Any]) -> None:
    """Warm up the chat model by loading it into memory.

    Called on service startup to ensure the first real request is fast.
    Sets keep_alive from config (default -1 = permanent).
    """
    host = config.get("ollama_url", "http://localhost:11434")
    model = config.get("ollama_model", "llama3.2:3b")
    keep_alive = config.get("ollama_keep_alive", -1)

    try:
        client = ollama.AsyncClient(host=host)
        # A minimal chat call with keep_alive loads the model
        await client.chat(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            keep_alive=keep_alive,
            options={"num_predict": 1},  # Generate just 1 token
        )
        log.info("Model %s preloaded with keep_alive=%s", model, keep_alive)
    except Exception as e:
        log.warning("Model preload failed (will load on first request): %s", e)


async def chat_stream(
    messages: list[dict[str, str]],
    config: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    """Stream chat response tokens. Yields dicts with keys:
    - {"token": str}             — a content token
    - {"searched": bool}         — emitted once before first token if search was used
    - {"done": True, "full_response": str}  — final message

    If web search is triggered, runs the tool loop first (non-streaming),
    then streams the final answer.
    """
    host = config.get("ollama_url", "http://localhost:11434")
    model = config.get("ollama_model", "llama3.2:3b")
    timeout = config.get("ollama_timeout_seconds", 300)
    keep_alive = config.get("ollama_keep_alive", -1)

    client = ollama.AsyncClient(host=host, timeout=timeout)
    use_search = (
        config.get("web_search_enabled", False)
        and bool(os.environ.get("OLLAMA_API_KEY"))
    )
    searched = False

    # Check if this query needs web search
    user_msg = ""
    for msg in reversed(messages):
        if msg["role"] == "user":
            user_msg = msg["content"]
            break

    if use_search and _needs_search(user_msg):
        # Run tool loop non-streaming to completion, then stream final answer
        searched, messages = await _run_search_loop(client, model, messages, config)

    yield {"searched": searched}

    # Stream the final response
    full_response = ""
    try:
        stream = await client.chat(
            model=model,
            messages=messages,
            stream=True,
            think=False,
            keep_alive=keep_alive,
            options={"temperature": 0.7},
        )
        async for chunk in stream:
            token = chunk.message.content or ""
            if token:
                full_response += token
                yield {"token": token}
    except Exception as e:
        log.error("Streaming chat error: %s", e)
        if not full_response:
            error_msg = "Sorry, I'm having trouble thinking right now."
            full_response = error_msg
            yield {"token": error_msg}

    yield {"done": True, "full_response": full_response}


async def chat_batch(
    messages: list[dict[str, str]],
    config: dict[str, Any],
) -> tuple[str | None, bool]:
    """Non-streaming chat. Returns (response_text, searched).
    Used by Telegram bot where streaming isn't practical.
    """
    full_response = ""
    searched = False
    async for event in chat_stream(messages, config):
        if "searched" in event:
            searched = event["searched"]
        elif "token" in event:
            full_response += event["token"]
        elif "done" in event:
            break
    return full_response or None, searched


async def _run_search_loop(client, model, messages, config):
    """Run the tool-calling loop for web search. Non-streaming.
    Returns (searched: bool, updated_messages: list).
    """
    try:
        from ollama import web_search, web_fetch
    except (ImportError, AttributeError):
        return False, messages

    tools = [web_search, web_fetch]
    chat_messages = list(messages)
    searched = False

    # Inject search instruction into system prompt
    for i, msg in enumerate(chat_messages):
        if msg["role"] == "system":
            chat_messages[i] = {
                **msg,
                "content": msg["content"] + (
                    " You have access to a web_search tool. Use it to answer "
                    "questions about current events or things you're unsure about."
                ),
            }
            break

    for _ in range(5):
        try:
            response = await client.chat(
                model=model, messages=chat_messages, tools=tools, think=None,
            )
        except Exception as exc:
            # Model doesn't support tools — fall back to plain chat
            if "does not support tools" in str(exc).lower():
                log.warning("Model %s doesn't support tools — skipping search", model)
                return False, messages
            raise

        chat_messages.append(response.message)

        if not response.message.tool_calls:
            # Final response reached — pop it so we can re-stream
            chat_messages.pop()
            return searched, chat_messages

        searched = True
        for tc in response.message.tool_calls:
            log.info("Tool call: %s(%s)", tc.function.name, tc.function.arguments)
            tool_fn = {"web_search": web_search, "web_fetch": web_fetch}.get(
                tc.function.name
            )
            if tool_fn:
                try:
                    result = tool_fn(**tc.function.arguments)
                    chat_messages.append({"role": "tool", "content": str(result)[:2000]})
                except Exception as e:
                    log.error("Tool %s failed: %s", tc.function.name, e)
                    chat_messages.append({"role": "tool", "content": f"Error: {e}"})

    return searched, chat_messages
```

**Key features:**
- **`preload_model()`** — called once on service startup. Sends a minimal 1-token request with `keep_alive=-1` to load the model permanently into Ollama's memory. The first real user request then gets ~1s first-token latency instead of ~18s.
- **`keep_alive`** parameter passed on every chat request to maintain the permanent keep-alive.
- **`chat_stream()`** is the primary interface — yields tokens as an async iterator.
- **`chat_batch()`** consumes the stream for Telegram (where streaming isn't practical).
- Tool-calling model fallback — if the model doesn't support tools, gracefully skips search instead of crashing.

### kitkat_core.py — RAG Engine

Replaces ChromaDB calls with sqlite-vec + FTS5 hybrid search. Manages embeddings explicitly.

```python
"""Kitkat RAG engine — vector search, context retrieval, memory extraction."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

import kitkat_chat
import kitkat_db

log = logging.getLogger("kitkat.core")

BASE_DIR = Path(__file__).resolve().parent

_write_lock = threading.Lock()
_bg_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)


def load_config() -> dict[str, Any]:
    try:
        return json.loads((BASE_DIR / "kitkat.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_db_path(config: dict[str, Any]) -> Path:
    return Path(config.get("data_dir", "/mnt/external/kitkat")) / "kitkat.db"


def is_storage_available(config: dict[str, Any]) -> bool:
    data_dir = config.get("data_dir", "/mnt/external/kitkat")
    mount_point = os.path.dirname(data_dir)
    if not os.path.ismount(mount_point):
        return False
    try:
        os.makedirs(data_dir, exist_ok=True)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def embed_texts(texts: list[str], config: dict[str, Any]) -> list[list[float]]:
    """Embed texts via Ollama. Batch request with per-item fallback."""
    if not texts:
        return []

    ollama_url = config.get("ollama_url", "http://localhost:11434").rstrip("/")
    model = config.get("embedding_model", "nomic-embed-text:latest")
    dim = config.get("embedding_dimensions", 768)
    max_chars = 6000

    # Truncate overly long texts
    texts = [t[:max_chars] if len(t) > max_chars else t for t in texts]

    # Try batch
    try:
        resp = httpx.post(
            f"{ollama_url}/api/embed",
            json={"model": model, "input": texts},
            timeout=120.0,
        )
        resp.raise_for_status()
        embeddings = resp.json()["embeddings"]
        if len(embeddings) == len(texts):
            return embeddings
    except Exception as e:
        log.warning("Batch embedding failed: %s — per-item fallback", e)

    # Per-item fallback
    results = []
    for text in texts:
        try:
            resp = httpx.post(
                f"{ollama_url}/api/embed",
                json={"model": model, "input": text},
                timeout=60.0,
            )
            resp.raise_for_status()
            results.append(resp.json()["embeddings"][0])
        except Exception as e:
            log.error("Embedding failed (len=%d): %s", len(text), e)
            results.append([0.0] * dim)
    return results


def embed_single(text: str, config: dict[str, Any]) -> list[float]:
    """Embed a single text. Convenience wrapper."""
    return embed_texts([text], config)[0]


# ---------------------------------------------------------------------------
# Context retrieval (hybrid vector + FTS5)
# ---------------------------------------------------------------------------

def retrieve_context(
    query: str, config: dict[str, Any],
) -> tuple[str, dict[str, int]]:
    """Retrieve relevant context using hybrid search.

    Returns (context_block_string, counts_dict).
    """
    if not is_storage_available(config):
        return "", {"memories": 0, "obsidian": 0, "gdrive": 0, "calendar": 0}

    db_path = get_db_path(config)
    max_chunks = config.get("max_context_chunks", 5)
    fts_boost = config.get("fts_boost", 0.3)

    # Embed the query
    query_vec = embed_single(query, config)

    # Hybrid search across all collections
    results = kitkat_db.hybrid_search(
        db_path,
        query_text=query,
        query_embedding=query_vec,
        collections=["conversations", "obsidian", "gdrive", "calendar"],
        n_results=max_chunks,
        fts_boost=fts_boost,
    )

    # Group by collection for display
    collection_map = {
        "conversations": "Memories",
        "obsidian": "Obsidian Notes",
        "gdrive": "Google Drive",
        "calendar": "Calendar",
    }
    counts = {"memories": 0, "obsidian": 0, "gdrive": 0, "calendar": 0}
    count_keys = {
        "conversations": "memories",
        "obsidian": "obsidian",
        "gdrive": "gdrive",
        "calendar": "calendar",
    }
    grouped: dict[str, list[str]] = {}

    for result in results:
        coll = result["collection"]
        meta = result["metadata"]
        doc = result["content"]
        key = count_keys.get(coll, coll)
        counts[key] = counts.get(key, 0) + 1

        if coll == "obsidian":
            title = meta.get("title", "Unknown")
            heading = meta.get("heading", "")
            prefix = f'From "{title}"'
            if heading:
                prefix += f" > {heading}"
            item = f"- {prefix}: {doc}"
        elif coll == "gdrive":
            filename = meta.get("filename", "Unknown")
            item = f'- From "{filename}": {doc}'
        else:
            item = f"- {doc}"

        section = collection_map.get(coll, coll)
        grouped.setdefault(section, []).append(item)

    if not grouped:
        return "", counts

    context_parts = []
    for section in ("Memories", "Obsidian Notes", "Google Drive", "Calendar"):
        if section in grouped:
            context_parts.append(f"{section}:\n" + "\n".join(grouped[section]))

    context_block = (
        "[Retrieved Context]\n"
        + "\n\n".join(context_parts)
        + "\n[End Context]"
    )
    return context_block, counts


# ---------------------------------------------------------------------------
# Memory extraction (background thread)
# ---------------------------------------------------------------------------

def _extract_memories_sync(
    user_message: str,
    assistant_response: str,
    chat_source: str,
    config: dict[str, Any],
) -> None:
    """Extract facts and store in vector DB + memories table. Runs in bg thread."""
    try:
        import ollama as ollama_lib

        extraction_response = ollama_lib.chat(
            model=config.get("ollama_model", "llama3.2:3b"),
            messages=[
                {"role": "system", "content": config.get("memory_extraction_prompt", "")},
                {
                    "role": "user",
                    "content": f"User said: {user_message}\n\nAssistant replied: {assistant_response}",
                },
            ],
            options={"temperature": 0.1, "num_predict": 500},
        )

        raw = extraction_response["message"]["content"].strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        facts = json.loads(raw)
        if not isinstance(facts, list):
            return

        db_path = get_db_path(config)
        threshold = config.get("memory_dedup_threshold", 0.25)

        for fact in facts:
            if not isinstance(fact, str) or len(fact.strip()) < 5:
                continue
            fact = fact.strip()

            # Embed the fact
            fact_vec = embed_single(fact, config)

            # Dedup: check for similar existing memories
            existing = kitkat_db.hybrid_search(
                db_path,
                query_text=fact,
                query_embedding=fact_vec,
                collections=["conversations"],
                n_results=1,
                fts_boost=0,
            )
            if existing and existing[0]["distance"] < threshold:
                log.debug("Duplicate memory skipped: %s", fact[:60])
                continue

            # Store vector
            with _write_lock:
                kitkat_db.add_chunks(
                    db_path,
                    collection="conversations",
                    documents=[fact],
                    embeddings=[fact_vec],
                    metadatas=[{
                        "type": "memory",
                        "source": "conversation",
                        "chat_source": chat_source,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }],
                )

            # Store in memories table for audit/management
            kitkat_db.save_memory(db_path, fact, "conversation", chat_source)
            log.info("Extracted memory: %s", fact[:80])

    except json.JSONDecodeError:
        log.warning("Memory extraction returned non-JSON — skipping")
    except Exception as e:
        log.error("Memory extraction failed: %s", e)


# ---------------------------------------------------------------------------
# Chat entry points
# ---------------------------------------------------------------------------

async def chat_stream(
    message: str,
    config: dict[str, Any],
    chat_source: str = "web",
):
    """Primary entry point — streams response with RAG context.

    Yields:
    - {"context": dict}       — context counts, emitted first
    - {"searched": bool}       — web search indicator
    - {"token": str}           — response tokens
    - {"done": True, ...}      — completion signal
    """
    db_path = get_db_path(config)
    chat_id = f"kitkat_{chat_source}"

    # 1. Retrieve RAG context
    context_block, context_counts = retrieve_context(message, config)
    yield {"context": context_counts}

    # 2. Build system prompt
    system_prompt = config.get("system_prompt", "You are Kitkat.")
    system_prompt += " Do not use emoji in your responses."
    if context_block:
        system_prompt += f"\n\n{context_block}"

    # 3. Load history
    history = kitkat_db.get_history(
        db_path, chat_id, config.get("conversation_history_length", 20),
    )

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend({"role": h["role"], "content": h["content"]} for h in history)
    messages.append({"role": "user", "content": message})

    # 4. Save user message
    kitkat_db.save_message(db_path, chat_id, "user", message)

    # 5. Stream response
    full_response = ""
    async for event in kitkat_chat.chat_stream(messages, config):
        if "token" in event:
            full_response += event["token"]
        yield event

    # 6. Save assistant response
    if full_response:
        kitkat_db.save_message(db_path, chat_id, "assistant", full_response)

    # 7. Background memory extraction
    if is_storage_available(config) and full_response:
        _bg_executor.submit(
            _extract_memories_sync, message, full_response, chat_source, config,
        )


async def chat_for_telegram(
    message: str,
    config: dict[str, Any],
    chat_source: str = "web",
) -> tuple[str, dict[str, int], bool]:
    """Non-streaming chat for Telegram. Returns (response, counts, searched)."""
    full_response = ""
    context_counts = {}
    searched = False

    async for event in chat_stream(message, config, chat_source):
        if "context" in event:
            context_counts = event["context"]
        elif "searched" in event:
            searched = event["searched"]
        elif "token" in event:
            full_response += event["token"]

    return full_response, context_counts, searched


# ---------------------------------------------------------------------------
# Helper functions (called from web/telegram/indexer)
# ---------------------------------------------------------------------------

def get_memories(config: dict[str, Any], limit: int = 50) -> list[dict]:
    return kitkat_db.get_memories(get_db_path(config), limit=limit)


def delete_memory(config: dict[str, Any], memory_id: int) -> bool:
    db_path = get_db_path(config)
    kitkat_db.deactivate_memory(db_path, memory_id)
    # Note: the vector chunk stays in the DB but the memory is soft-deleted.
    # A periodic cleanup could remove orphaned vectors.
    return True


def add_manual_memory(
    config: dict[str, Any], fact: str, chat_source: str = "manual",
) -> int:
    db_path = get_db_path(config)

    # Store vector
    if is_storage_available(config):
        fact_vec = embed_single(fact, config)
        with _write_lock:
            kitkat_db.add_chunks(
                db_path,
                collection="conversations",
                documents=[fact],
                embeddings=[fact_vec],
                metadatas=[{
                    "type": "memory",
                    "source": "manual",
                    "chat_source": chat_source,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }],
            )

    return kitkat_db.save_memory(db_path, fact, "manual", chat_source)


def get_index_stats(config: dict[str, Any]) -> dict[str, int]:
    if not is_storage_available(config):
        return {"conversations": 0, "obsidian": 0, "gdrive": 0, "calendar": 0}
    db_path = get_db_path(config)
    return {
        "conversations": kitkat_db.count_chunks(db_path, "conversations"),
        "obsidian": kitkat_db.count_chunks(db_path, "obsidian"),
        "gdrive": kitkat_db.count_chunks(db_path, "gdrive"),
        "calendar": kitkat_db.count_chunks(db_path, "calendar"),
    }


def clear_conversation(config: dict[str, Any], chat_source: str = "web") -> None:
    kitkat_db.clear_history(get_db_path(config), f"kitkat_{chat_source}")


def reset_memories(config: dict[str, Any]) -> None:
    db_path = get_db_path(config)
    with _write_lock:
        kitkat_db.delete_chunks_by_collection(db_path, "conversations")
    conn = kitkat_db.get_connection(db_path)
    try:
        conn.execute("UPDATE memories SET is_active = 0")
        conn.commit()
        conn.execute("DELETE FROM chat_history WHERE chat_id LIKE 'kitkat_%'")
        conn.commit()
    finally:
        conn.close()


def reindex_all(config: dict[str, Any]) -> dict[str, int]:
    """Trigger full reindex. Called from web/telegram."""
    import kitkat_index
    return kitkat_index.index_all(config)
```

### kitkat_web.py — FastAPI Web Server with Streaming and Markdown

```python
"""Kitkat web server — FastAPI with SSE streaming."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import kitkat_chat
import kitkat_core
import kitkat_db

log = logging.getLogger("kitkat.web")
BASE_DIR = Path(__file__).resolve().parent


def _load_config() -> dict[str, Any]:
    try:
        return json.loads((BASE_DIR / "kitkat.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB and preload Ollama model for fast first response."""
    config = _load_config()
    db_path = kitkat_core.get_db_path(config)
    dim = config.get("embedding_dimensions", 768)
    kitkat_db.init_db(db_path, embedding_dim=dim)
    log.info("Database initialized at %s", db_path)

    # Preload model — ensures first user request gets ~1s TTFT
    await kitkat_chat.preload_model(config)

    yield  # App runs here

    log.info("Shutting down")


app = FastAPI(title="Kitkat", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ---- HTML page ----

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    config = _load_config()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "storage_ok": kitkat_core.is_storage_available(config),
    })


# ---- Streaming chat (SSE) ----

class ChatRequest(BaseModel):
    message: str


@app.post("/api/chat/stream")
async def api_chat_stream(body: ChatRequest):
    """Server-Sent Events endpoint for streaming chat responses."""
    config = _load_config()

    async def event_generator():
        async for event in kitkat_core.chat_stream(body.message, config, "web"):
            if "context" in event:
                yield f"data: {json.dumps({'type': 'context', 'counts': event['context']})}\n\n"
            elif "searched" in event:
                yield f"data: {json.dumps({'type': 'searched', 'value': event['searched']})}\n\n"
            elif "token" in event:
                yield f"data: {json.dumps({'type': 'token', 'content': event['token']})}\n\n"
            elif "done" in event:
                yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---- Non-streaming chat (fallback) ----

@app.post("/api/chat")
async def api_chat(body: ChatRequest):
    config = _load_config()
    full_response = ""
    context_counts = {}
    searched = False

    async for event in kitkat_core.chat_stream(body.message, config, "web"):
        if "context" in event:
            context_counts = event["context"]
        elif "searched" in event:
            searched = event["searched"]
        elif "token" in event:
            full_response += event["token"]

    return {
        "response": full_response,
        "context_used": context_counts,
        "searched": searched,
    }


# ---- History ----

@app.get("/api/history")
async def get_history():
    config = _load_config()
    db_path = kitkat_core.get_db_path(config)
    messages = kitkat_db.get_history(db_path, "kitkat_web", limit=100)
    return {"messages": messages}


@app.delete("/api/history")
async def clear_history():
    config = _load_config()
    kitkat_core.clear_conversation(config, "web")
    return {"ok": True}


# ---- Memories ----

@app.get("/api/memories")
async def get_memories():
    config = _load_config()
    return {"memories": kitkat_core.get_memories(config)}


class MemoryRequest(BaseModel):
    fact: str


@app.post("/api/memories")
async def add_memory(body: MemoryRequest):
    config = _load_config()
    mid = kitkat_core.add_manual_memory(config, body.fact)
    return {"ok": True, "id": mid}


@app.delete("/api/memories/{memory_id}")
async def delete_memory(memory_id: int):
    config = _load_config()
    kitkat_core.delete_memory(config, memory_id)
    return {"ok": True}


# ---- Stats & management ----

@app.get("/api/stats")
async def get_stats():
    config = _load_config()
    return {
        "stats": kitkat_core.get_index_stats(config),
        "storage_available": kitkat_core.is_storage_available(config),
    }


@app.post("/api/reindex")
async def reindex():
    import asyncio
    config = _load_config()
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, kitkat_core.reindex_all, config)
    return {"ok": True, "message": "Reindexing started"}


class ResetRequest(BaseModel):
    confirm: bool


@app.post("/api/reset")
async def reset(body: ResetRequest):
    if not body.confirm:
        return {"error": "confirm required"}, 400
    config = _load_config()
    kitkat_core.reset_memories(config)
    return {"ok": True}
```

**Key additions vs original spec:**
- **`lifespan` context manager** — runs `kitkat_db.init_db()` and `kitkat_chat.preload_model()` on startup. The model preload ensures the very first user request gets ~1s TTFT instead of 18s.
- No ChromaDB imports anywhere.

### Web UI — SSE Streaming with Markdown Rendering

The frontend uses **marked.js** for markdown rendering and **ReadableStream** for SSE consumption:

```html
<!-- In <head> -->
<script src="/static/marked.min.js"></script>
```

```javascript
// Markdown rendering with sanitization
function renderMarkdown(text) {
    // marked.js converts markdown to HTML
    return marked.parse(text, {
        breaks: true,       // GFM line breaks
        gfm: true,          // GitHub Flavored Markdown
        headerIds: false,    // Don't generate IDs
        mangle: false,       // Don't mangle email addresses
    });
}

async function sendMessage(text) {
    appendMessage('user', text);
    showLoading();

    const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text }),
    });

    hideLoading();

    // Create empty assistant message row
    const msgEl = appendMessage('assistant', '', null, true, {});
    const textEl = msgEl.querySelector('.chat-text');

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let fullText = '';

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();

        for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            const data = JSON.parse(line.slice(6));

            if (data.type === 'context') {
                updateContextBadges(msgEl, data.counts);
            } else if (data.type === 'searched' && data.value) {
                showSearchBadge(msgEl);
            } else if (data.type === 'token') {
                fullText += data.content;
                // Render as markdown on each token for live preview
                textEl.innerHTML = renderMarkdown(fullText);
                scrollToBottom();
            } else if (data.type === 'done') {
                // Final render with complete text
                textEl.innerHTML = renderMarkdown(fullText);
            }
        }
    }
}
```

**Markdown rendering notes:**
- `marked.parse()` is called on every token for live streaming preview. This is fast (~0.1ms for typical text lengths).
- Code blocks render with `<pre><code>` tags. The CSS applies dark-theme styling with monospace font and background.
- Lists, bold, italic, headers, links all render properly.
- User messages remain plain text (no markdown rendering) — only assistant responses get markdown.

### kitkat_calendar.py — Calendar Fetching

Extracted from bt_calendar.py — only the parts Kitkat needs. ~120 lines instead of ~400.

```python
"""Calendar integration for Kitkat — fetches events from iCloud CalDAV."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

log = logging.getLogger("kitkat.calendar")


@dataclass
class CalendarEvent:
    summary: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str = ""
    calendar_name: str = ""


# Simple cache
_cache: dict[str, tuple[list[CalendarEvent], float]] = {}
_calendars_cache: tuple[list[str], float] = ([], 0)


def _load_env() -> None:
    """Load .device-radar.env if env vars aren't already set."""
    from pathlib import Path
    env_path = Path("/home/pi/.device-radar.env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if key and key not in os.environ:
            os.environ[key] = val


_load_env()


def get_available_calendars(config: dict[str, Any]) -> list[str]:
    """Return list of calendar names from CalDAV (cached)."""
    global _calendars_cache

    if not config.get("calendar_enabled", False):
        return []

    ttl = config.get("calendar_cache_minutes", 15) * 60
    names, fetched_at = _calendars_cache
    if names and (time.time() - fetched_at) < ttl:
        return names

    url = config.get("calendar_url", "https://caldav.icloud.com")
    username = os.environ.get(config.get("calendar_username_env", "APPLE_ID_EMAIL"), "")
    password = os.environ.get(config.get("calendar_password_env", "APPLE_ID_APP_PASSWORD"), "")

    if not username or not password:
        return names

    try:
        import caldav
        client = caldav.DAVClient(url=url, username=username, password=password)
        principal = client.principal()
        calendars = principal.calendars()
        result = sorted({c.name for c in calendars if c.name})
        _calendars_cache = (result, time.time())
        return result
    except Exception:
        log.error("Failed to discover calendars", exc_info=True)
        return names


def _fetch_events_sync(config: dict[str, Any], calendar_names: list[str]) -> list[CalendarEvent]:
    """Fetch events for the next 14 days (synchronous)."""
    import caldav

    url = config.get("calendar_url", "https://caldav.icloud.com")
    username = os.environ.get(config.get("calendar_username_env", "APPLE_ID_EMAIL"), "")
    password = os.environ.get(config.get("calendar_password_env", "APPLE_ID_APP_PASSWORD"), "")

    if not username or not password:
        return []

    today = date.today()
    start_dt = datetime.combine(today, datetime.min.time())
    end_dt = datetime.combine(today + timedelta(days=14), datetime.min.time())
    events: list[CalendarEvent] = []

    try:
        client = caldav.DAVClient(url=url, username=username, password=password)
        principal = client.principal()
        calendars = principal.calendars()

        for cal in calendars:
            if cal.name not in calendar_names:
                continue
            try:
                results = cal.date_search(start=start_dt, end=end_dt, expand=True)
            except Exception:
                continue

            for item in results:
                try:
                    import vobject
                    vobj = vobject.readOne(item.data)
                    vevent = vobj.vevent
                    summary = str(vevent.summary.value) if hasattr(vevent, "summary") else "Untitled"
                    dtstart = vevent.dtstart.value
                    dtend = vevent.dtend.value if hasattr(vevent, "dtend") else dtstart
                    location = str(vevent.location.value) if hasattr(vevent, "location") else ""

                    all_day = isinstance(dtstart, date) and not isinstance(dtstart, datetime)
                    if all_day:
                        dtstart = datetime.combine(dtstart, datetime.min.time())
                        dtend = datetime.combine(dtend, datetime.min.time())
                    elif hasattr(dtstart, "tzinfo") and dtstart.tzinfo:
                        dtstart = dtstart.replace(tzinfo=None)
                        dtend = dtend.replace(tzinfo=None) if hasattr(dtend, "replace") else dtend

                    events.append(CalendarEvent(
                        summary=summary,
                        start=dtstart,
                        end=dtend,
                        all_day=all_day,
                        location=location,
                        calendar_name=cal.name,
                    ))
                except Exception:
                    continue
    except Exception:
        log.error("Calendar fetch failed", exc_info=True)

    return events


async def get_events(config: dict[str, Any], calendar_names: list[str]) -> list[CalendarEvent]:
    """Async wrapper — runs sync CalDAV fetch in executor."""
    cache_key = ",".join(sorted(calendar_names))
    ttl = config.get("calendar_cache_minutes", 15) * 60

    if cache_key in _cache:
        events, fetched_at = _cache[cache_key]
        if (time.time() - fetched_at) < ttl:
            return events

    loop = asyncio.get_running_loop()
    events = await asyncio.wait_for(
        loop.run_in_executor(None, _fetch_events_sync, config, calendar_names),
        timeout=30,
    )
    _cache[cache_key] = (events, time.time())
    return events
```

### kitkat_index.py — Background Indexer

Nearly identical to current `bt_kitkat_index.py` with these changes:
- Imports `kitkat_core` and `kitkat_db` instead of `bt_kitkat` and `bt_db`
- Imports `kitkat_calendar` instead of `bt_calendar`
- Config from `kitkat.json` instead of `config.json`
- Calls `kitkat_core.embed_texts()` explicitly for batch embedding before `kitkat_db.add_chunks()`
- Uses `kitkat_db.delete_chunks_by_metadata()` for delete-then-upsert
- Uses `kitkat_db.delete_chunks_by_collection()` for calendar full refresh

The chunking logic (_chunk_markdown, _chunk_text, _file_hash, _read_file, _read_pdf, _read_docx) is copied unchanged.

**Key indexing pattern:**

```python
# Example: indexing an Obsidian note
chunks = _chunk_markdown(content, title)
documents = [c["text"] for c in chunks]
metadatas = [{"source": "obsidian", "file_path": rel_path, "title": title, "heading": c["heading"]} for c in chunks]

# Batch embed all chunks at once
embeddings = kitkat_core.embed_texts(documents, config)

# Delete old chunks for this file
kitkat_db.delete_chunks_by_metadata(db_path, "obsidian", "file_path", rel_path)

# Insert new chunks with vectors
with kitkat_core._write_lock:
    kitkat_db.add_chunks(db_path, "obsidian", documents, embeddings, metadatas)
```

### kitkat_bot.py — Standalone Telegram Bot

Own bot, own identity. No `/kitkat` prefix, no mode toggle. Just message the bot and it's Kitkat.

```python
"""Kitkat Telegram bot — standalone personal memory agent."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from telegram import Update, BotCommand
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters

import kitkat_core
import kitkat_db

log = logging.getLogger("kitkat.bot")
BASE_DIR = Path(__file__).resolve().parent


def _load_config() -> dict[str, Any]:
    try:
        import json
        return json.loads((BASE_DIR / "kitkat.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _is_authorized(chat_id: int, config: dict[str, Any]) -> bool:
    authorized = os.environ.get(
        config.get("telegram_chat_id_env", "KITKAT_TELEGRAM_CHAT_ID"), ""
    )
    return not authorized or str(chat_id) == authorized


async def _handle_message(update, context) -> None:
    """All non-command messages go to Kitkat chat."""
    if not update.message or not update.message.text:
        return
    config = _load_config()
    if not _is_authorized(update.effective_chat.id, config):
        return

    await update.effective_chat.send_action(ChatAction.TYPING)

    response, counts, searched = await kitkat_core.chat_for_telegram(
        update.message.text, config,
        chat_source=f"telegram_{update.effective_chat.id}",
    )

    parts = []
    if searched:
        parts.append("[searched the web]")
    used = [f"{v} {k}" for k, v in counts.items() if v > 0]
    if used:
        parts.append(f"[{', '.join(used)}]")
    prefix = " ".join(parts)
    text = f"{prefix}\n\n{response}" if prefix else response

    await update.message.reply_text(text)


async def _cmd_memories(update, context) -> None:
    config = _load_config()
    memories = kitkat_core.get_memories(config, limit=10)
    if not memories:
        await update.message.reply_text("No memories stored yet.")
        return
    lines = ["Recent memories:"]
    for m in memories:
        lines.append(f"  - {m['fact']}")
    await update.message.reply_text("\n".join(lines))


async def _cmd_remember(update, context) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /remember <fact>")
        return
    config = _load_config()
    fact = " ".join(context.args)
    kitkat_core.add_manual_memory(config, fact,
        chat_source=f"telegram_{update.effective_chat.id}")
    await update.message.reply_text(f"Remembered: {fact}")


async def _cmd_forget(update, context) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /forget <text>")
        return
    config = _load_config()
    search = " ".join(context.args)
    memories = kitkat_core.get_memories(config, limit=50)
    matches = [m for m in memories if search.lower() in m["fact"].lower()]
    if not matches:
        await update.message.reply_text(f"No memories matching '{search}'")
        return
    for m in matches:
        kitkat_core.delete_memory(config, m["id"])
    await update.message.reply_text(f"Forgot {len(matches)} matching memories")


async def _cmd_clear(update, context) -> None:
    config = _load_config()
    kitkat_core.clear_conversation(config,
        chat_source=f"telegram_{update.effective_chat.id}")
    await update.message.reply_text("Conversation history cleared.")


async def _cmd_stats(update, context) -> None:
    config = _load_config()
    stats = kitkat_core.get_index_stats(config)
    lines = ["Index stats:"]
    for source, count in stats.items():
        lines.append(f"  {source}: {count} chunks")
    await update.message.reply_text("\n".join(lines))


async def _cmd_reindex(update, context) -> None:
    import asyncio
    config = _load_config()
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, kitkat_core.reindex_all, config)
    await update.message.reply_text("Reindexing started.")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = _load_config()
    if not config.get("telegram_enabled", True):
        log.info("Telegram bot disabled")
        return

    token = os.environ.get(
        config.get("telegram_token_env", "KITKAT_TELEGRAM_TOKEN"), ""
    )
    if not token:
        log.error("Telegram token not set")
        return

    db_path = kitkat_core.get_db_path(config)
    dim = config.get("embedding_dimensions", 768)
    kitkat_db.init_db(db_path, embedding_dim=dim)

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("memories", _cmd_memories))
    app.add_handler(CommandHandler("remember", _cmd_remember))
    app.add_handler(CommandHandler("forget", _cmd_forget))
    app.add_handler(CommandHandler("clear", _cmd_clear))
    app.add_handler(CommandHandler("stats", _cmd_stats))
    app.add_handler(CommandHandler("reindex", _cmd_reindex))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))

    async def _post_init(application) -> None:
        await application.bot.set_my_commands([
            BotCommand("memories", "List recent memories"),
            BotCommand("remember", "Manually add a memory"),
            BotCommand("forget", "Delete memories matching text"),
            BotCommand("clear", "Clear conversation history"),
            BotCommand("stats", "Index statistics"),
            BotCommand("reindex", "Trigger knowledge reindex"),
        ])

    app.post_init = _post_init
    log.info("Starting Kitkat Telegram bot")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
```

## Dependencies

`requirements.txt`:

```
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
jinja2>=3.1.0
sqlite-vec>=0.1.6
httpx>=0.25.0
ollama>=0.4.0
python-telegram-bot>=21.0
caldav>=1.3.0
vobject>=0.9.6
```

**What's gone vs the coupled version:**
- No `chromadb` (replaced by sqlite-vec — saves 200-500MB RAM, ~30 transitive deps)
- No `flask` (replaced by FastAPI)
- No `sse-starlette` (FastAPI's StreamingResponse handles SSE natively)
- No `python-dotenv` (manual env loading, same as bt_calendar pattern)
- `sqlite-vec` is 160KB with zero transitive dependencies

**Static assets (vendored, not pip):**
- `static/marked.min.js` — markdown renderer (~40KB, MIT license, download from https://cdn.jsdelivr.net/npm/marked/marked.min.js)

## Ollama Model Setup

```bash
# Chat model (already pulled)
ollama pull llama3.2:3b

# Embedding model — choose one:
ollama pull nomic-embed-text:latest       # 274MB, 768-dim, ~200ms/embed (default)
# OR
ollama pull snowflake-arctic-embed:xs     # 90MB, 384-dim, ~50ms/embed (faster, smaller)

# Set keep_alive permanently (alternative to per-request keep_alive)
# This keeps the model loaded in memory across all Ollama clients
curl http://localhost:11434/api/generate -d '{"model": "llama3.2:3b", "keep_alive": -1}'
```

**Embedding model choice:**
- `nomic-embed-text:latest` — 137M params, 768 dimensions, F16 quantization, ~200ms per embedding on Pi 5. Higher quality, more RAM.
- `snowflake-arctic-embed:xs` — 22M params, 384 dimensions, ~50ms per embedding. 4x faster indexing, half the vector storage, slightly lower retrieval quality. Good enough for personal notes.

To switch models: change `embedding_model` and `embedding_dimensions` in `kitkat.json`, delete `kitkat.db`, and reindex.

## Systemd Services

### kitkat-web.service
```ini
[Unit]
Description=Kitkat Web Server
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m uvicorn kitkat_web:app --host 0.0.0.0 --port 8081 --workers 1
WorkingDirectory=/opt/kitkat
EnvironmentFile=/home/pi/.device-radar.env
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### kitkat-bot.service
```ini
[Unit]
Description=Kitkat Telegram Bot
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/kitkat/kitkat_bot.py
WorkingDirectory=/opt/kitkat
EnvironmentFile=/home/pi/.device-radar.env
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### kitkat-indexer.service
```ini
[Unit]
Description=Kitkat Knowledge Indexer
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/kitkat/kitkat_index.py --service
WorkingDirectory=/opt/kitkat
EnvironmentFile=/home/pi/.device-radar.env
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

## Device Radar Changes

After Kitkat is standalone, remove the coupled integration from Device Radar:

1. **bt_web.py** — remove blueprint registration (3 lines)
2. **bt_telegram.py** — remove kitkat import, command handler, message routing (~25 lines)
3. **templates/base.html** — change Kitkat nav link from `/kitkat` to `http://{host}:8081/` (external link)
4. **Delete** — `bt_kitkat.py`, `bt_kitkat_web.py`, `bt_kitkat_telegram.py`, `templates/kitkat.html`
5. **bt_db.py** — `kitkat_memories` table and helpers can be left (harmless) or removed
6. **bt-kitkat-indexer.service** — replaced by standalone `kitkat-indexer.service`

## Migration Plan

### Data migration

The ChromaDB data cannot be directly migrated to sqlite-vec — vectors need to be re-embedded. The simplest approach is a full reindex:

1. Memories: copy from `bt_radar.db` to `kitkat.db` (SQL), then re-embed each fact
2. Obsidian/GDrive/Calendar: full reindex (the indexer handles this)
3. Chat history: copy from `bt_radar.db`

```python
# Migration script — run once
import sqlite3
import kitkat_db
import kitkat_core

config = kitkat_core.load_config()
db_path = kitkat_core.get_db_path(config)
kitkat_db.init_db(db_path, config.get("embedding_dimensions", 768))

# Migrate chat history
old = sqlite3.connect("/var/www/bluetooth/bt_radar.db")
old.row_factory = sqlite3.Row
rows = old.execute("SELECT * FROM chat_history WHERE chat_id LIKE 'kitkat_%'").fetchall()
for r in rows:
    kitkat_db.save_message(db_path, r["chat_id"], r["role"], r["content"])

# Migrate memories (re-embed for vector search)
mem_rows = old.execute("SELECT * FROM kitkat_memories WHERE is_active = 1").fetchall()
for r in mem_rows:
    kitkat_core.add_manual_memory(config, r["fact"], r["chat_source"])

old.close()
print(f"Migrated {len(rows)} messages and {len(mem_rows)} memories")
# Then run: python3 kitkat_index.py  (full reindex of obsidian/gdrive/calendar)
```

### Rollout order
1. Create new Telegram bot via BotFather, get token, add to `.device-radar.env`
2. Build and test standalone Kitkat in `/var/www/kitkat/`
3. Stop the old kitkat indexer: `sudo systemctl stop bt-kitkat-indexer`
4. Run migration script
5. Run full reindex: `python3 kitkat_index.py`
6. Deploy standalone Kitkat to `/opt/kitkat/`
7. Install and start new services: `kitkat-web`, `kitkat-bot`, `kitkat-indexer`
8. Verify streaming works in browser and Telegram
9. Remove Kitkat coupling from Device Radar, deploy Device Radar
10. Update Device Radar nav link to point to `:8081`

## Performance Expectations

| Metric | Current (coupled) | Standalone |
|---|---|---|
| Time to first token (web) | 90-120s (batch) | **~1s** (streaming, warm model) |
| Time to first token (cold) | 90-120s | **~18s** (cold) → 1s after preload |
| Full response time | 90-120s | 90-120s (same model, same hardware) |
| Perceived responsiveness | Poor | **Good** (streaming + markdown) |
| Context retrieval | Vector only | **Hybrid** (vector + keyword FTS5) |
| Web server during chat | **Blocked** (Flask sync) | Responsive (async uvicorn) |
| Device Radar during chat | **Blocked** (shared Flask) | Unaffected (separate process) |
| RAM: vector DB | ~300-500MB (ChromaDB) | **~5-10MB** (sqlite-vec) |
| RAM: total Kitkat | ~500MB | **~150-200MB** |
| Dependencies | ~40 transitive packages | **~12 transitive packages** |
| Indexing speed | ~200ms/chunk | ~50ms/chunk (with arctic-embed:xs) |

## Code Reuse Summary

| Component | Reuse | Notes |
|---|---|---|
| Chunking logic (markdown, text, PDF, DOCX) | 100% copy | Unchanged |
| Context retrieval formatting | 90% copy | Same display logic, new search backend |
| Memory extraction (background thread) | 95% copy | embed_texts() instead of ChromaDB add |
| Calendar integration | 90% extract | Simplified from bt_calendar.py |
| Indexer (Obsidian, GDrive, calendar) | 85% copy | New DB calls, explicit embedding |
| Embedding function | Simplified | httpx calls unchanged, no ChromaDB wrapper |
| Chat function | **Rewrite** | New streaming architecture |
| Web server | **Rewrite** | Flask → FastAPI + SSE + model preload |
| Telegram bot | **Rewrite** | Piggyback → standalone |
| Database layer | **Rewrite** | ChromaDB → sqlite-vec + FTS5 |
| Web UI template | 70% copy | Add SSE + markdown.js, keep structure/CSS |
