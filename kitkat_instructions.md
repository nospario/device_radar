# Kitkat — Personal Memory Agent for Device Radar

## Overview

Kitkat is a new subsystem within Device Radar that adds a personal memory/RAG (Retrieval-Augmented Generation) agent. It learns about the user through conversation, indexes personal knowledge sources (Obsidian vault, Google Drive, Apple Calendar), and provides an intelligent conversational interface via both a web frontend and Telegram.

Kitkat runs entirely locally on the Raspberry Pi 5 using Ollama for inference and embeddings, and ChromaDB for vector storage on the external hard drive.

**Name origin**: Kitkat. The name follows Device Radar's existing Red Dwarf theme (legion, krytie, cat, starbug) — "Kit Kat" is a nod to Cat's love of looking good and knowing everything about himself. Kitkat learns everything about its owner.

## Target Environment

Same Raspberry Pi 5 as Device Radar. All existing Device Radar services and infrastructure remain unchanged.

- **Production directory**: `/opt/bt-monitor/` (alongside existing Device Radar files)
- **Development directory**: `/var/www/bluetooth/`
- **External hard drive**: `/mnt/external` (458GB, mounted at `/dev/sda1`)
- **Kitkat persistent data**: `/mnt/external/kitkat/` (ChromaDB storage, index state)
- **Obsidian vault**: `/home/nospario/ObsidianVaults/Main`
- **Google Drive mount**: `/home/nospario/gdrive`
- **Ollama**: already running at `http://localhost:11434`
- **Existing CalDAV credentials**: already in `/home/pi/.device-radar.env` as `APPLE_ID_EMAIL` and `APPLE_ID_APP_PASSWORD`
- **Python**: 3.11+
- **Pi 5 RAM**: 4–8GB. ChromaDB with hnswlib typically uses 200–500MB depending on collection size. Monitor with `htop` after first index.

## Architecture

Kitkat adds four new Python modules and one new systemd service to Device Radar:

| New Module | Purpose |
|---|---|
| `bt_kitkat.py` | Core RAG engine — memory extraction, embedding, retrieval, context-augmented chat |
| `bt_kitkat_index.py` | Background indexer — watches and indexes Obsidian vault, Google Drive, and calendar events into ChromaDB |
| `bt_kitkat_web.py` | Flask blueprint — web chat UI and API endpoints, registered into existing `bt_web.py` |
| `bt_kitkat_telegram.py` | Telegram integration — hooks into existing `bt_telegram.py` to route Kitkat commands and conversations |

New systemd service:

| Service | Unit File | Description |
|---|---|---|
| `bt-kitkat-indexer` | `bt-kitkat-indexer.service` | Background indexer that periodically re-indexes knowledge sources |

**Critical constraint**: Do NOT modify the behaviour of any existing Device Radar module unless explicitly stated. Kitkat integrates via clearly defined hook points in `bt_web.py` (blueprint registration) and `bt_telegram.py` (command routing).

## Storage Layout

All persistent Kitkat data lives on the external hard drive to protect the SD card from write wear:

```
/mnt/external/kitkat/
├── chroma_db/              # ChromaDB persistent storage (all collections)
├── index_state.json        # Indexer state: file hashes, last-indexed timestamps
└── logs/                   # Indexer log files (rotated)
```

ChromaDB stores four collections internally within `chroma_db/`:
- `kitkat_conversations` — extracted facts and memories from chat
- `kitkat_obsidian` — indexed Obsidian vault content chunks
- `kitkat_gdrive` — indexed Google Drive file content chunks
- `kitkat_calendar` — indexed calendar events

Collection names are prefixed with `kitkat_` to avoid any future conflicts.

## New Dependencies

Add to `requirements.txt`:

```
chromadb>=0.5.0
```

Install: `pip install chromadb --break-system-packages`

Do NOT install `chromadb-client` — use the full `chromadb` package for local persistent storage.

The `ollama` Python package is already installed (used by `bt_search.py`). Reuse it for both chat and embedding calls.

No other new dependencies are needed. File watching for the indexer uses the standard library (`os.walk`, hash comparison) rather than `watchdog`, to keep things simple and avoid inotify limitations on the Pi.

## Ollama Models Required

Before first run, pull the embedding model:

```bash
ollama pull nomic-embed-text:latest
```

This model is ~270MB and runs fast on CPU (~200ms per embedding). It produces 768-dimension vectors.

The chat model is whatever is already configured in Device Radar's `config.json` (`ollama_model` field). Kitkat reuses this setting — do not add a separate chat model config key.

## Configuration

Add these keys to the existing `config.json` (alongside all existing keys — do not remove or modify any existing keys):

```json
{
  "kitkat_enabled": true,
  "kitkat_data_dir": "/mnt/external/kitkat",
  "kitkat_embedding_model": "nomic-embed-text:latest",
  "kitkat_obsidian_path": "/home/nospario/ObsidianVaults/Main",
  "kitkat_gdrive_path": "/home/nospario/gdrive",
  "kitkat_index_interval_minutes": 30,
  "kitkat_max_context_chunks": 8,
  "kitkat_conversation_history_length": 20,
  "kitkat_calendar_enabled": true,
  "kitkat_system_prompt": "You are Kitkat, a personal AI assistant running locally on a Raspberry Pi. You have access to memories from past conversations, notes from an Obsidian vault, files from Google Drive, and calendar events. Use retrieved context naturally — reference it when relevant but don't dump everything you know. Be conversational, warm, and concise. If you learn something new about the user, acknowledge it naturally. Never use emoji.",
  "kitkat_memory_extraction_prompt": "Extract discrete facts about the user from this conversation exchange. Return ONLY a JSON array of strings, each being one fact. Focus on: personal preferences, biographical details, work information, relationships, goals, opinions, routines, and anything the user would expect to be remembered. If no facts are extractable, return []. Do not include facts about yourself (the assistant). Do not include trivial conversational filler. Example output: [\"User's name is Richard\", \"User works at Loughborough University\", \"User prefers Python over JavaScript\"]"
}
```

**Note on `kitkat_gdrive_extensions`**: This key is optional. If omitted, the code defaults to: `[".md", ".txt", ".pdf", ".docx", ".csv", ".json", ".py", ".sh", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".html", ".xml"]`. Only add it to `config.json` if you need to override the default list.

The existing `calendar_url`, `calendar_username_env`, `calendar_password_env`, `calendar_cache_minutes` keys are reused by Kitkat for CalDAV access — no duplication needed.

## Core Module: `bt_kitkat.py`

This is the RAG engine. It owns ChromaDB initialisation, context retrieval, chat orchestration, and memory extraction.

### Module Initialisation

```python
"""Kitkat — personal memory RAG engine for Device Radar."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chromadb
import httpx

import bt_db
import bt_search

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
BASE_DIR = Path(__file__).resolve().parent

_DEFAULT_GDRIVE_EXTENSIONS = (
    ".md", ".txt", ".pdf", ".docx", ".csv", ".json",
    ".py", ".sh", ".yml", ".yaml", ".toml", ".cfg",
    ".ini", ".html", ".xml",
)
```

Config loading follows the existing Device Radar pattern — load fresh from disk when needed, pass as parameter to functions:

```python
def load_config() -> dict[str, Any]:
    """Load config.json from the script directory."""
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
```

### ChromaDB Setup

Initialise on first use (lazy init with module-level variables). Config is passed in on first call and cached with the client:

```python
_chroma_client: chromadb.PersistentClient | None = None
_collections: dict[str, chromadb.Collection] = {}
_write_lock = threading.Lock()


def _get_chroma(config: dict[str, Any]) -> dict[str, chromadb.Collection]:
    """Lazy-init ChromaDB client and collections."""
    global _chroma_client, _collections
    if _chroma_client is not None:
        return _collections

    data_dir = config.get("kitkat_data_dir", "/mnt/external/kitkat")
    chroma_path = os.path.join(data_dir, "chroma_db")
    os.makedirs(chroma_path, exist_ok=True)

    _chroma_client = chromadb.PersistentClient(path=chroma_path)

    ef = _OllamaEmbeddingFunction(
        config.get("ollama_url", "http://localhost:11434"),
        config.get("kitkat_embedding_model", "nomic-embed-text:latest"),
    )

    for name in ("kitkat_conversations", "kitkat_obsidian", "kitkat_gdrive", "kitkat_calendar"):
        _collections[name] = _chroma_client.get_or_create_collection(
            name=name,
            embedding_function=ef,
            metadata={"hnsw:space": "cosine"},
        )

    return _collections
```

**Important**: `_write_lock` is a `threading.Lock`, not `asyncio.Lock`. ChromaDB operations are synchronous and memory extraction runs in a thread pool — `asyncio.Lock` does not work across threads.

### Custom Embedding Function

ChromaDB requires an embedding function that conforms to its protocol. Implement one that calls Ollama's embedding endpoint with **batch support**:

```python
class _OllamaEmbeddingFunction:
    """ChromaDB-compatible embedding function using Ollama."""

    def __init__(self, ollama_url: str, model: str):
        self.ollama_url = ollama_url.rstrip("/")
        self.model = model

    def __call__(self, input: list[str]) -> list[list[float]]:
        """Embed a list of texts. Called by ChromaDB internally."""
        if not input:
            return []
        try:
            # Ollama /api/embed accepts a list of inputs in one call
            resp = httpx.post(
                f"{self.ollama_url}/api/embed",
                json={"model": self.model, "input": input},
                timeout=120.0,
            )
            resp.raise_for_status()
            data = resp.json()
            embeddings = data["embeddings"]
            if len(embeddings) == len(input):
                return embeddings
            # Unexpected count — fall through to per-item
            log.warning(
                "Batch embed returned %d vectors for %d inputs — falling back",
                len(embeddings), len(input),
            )
        except Exception as e:
            log.warning("Batch embedding failed: %s — falling back to per-item", e)

        # Fallback: embed one at a time
        results = []
        for text in input:
            try:
                resp = httpx.post(
                    f"{self.ollama_url}/api/embed",
                    json={"model": self.model, "input": text},
                    timeout=60.0,
                )
                resp.raise_for_status()
                data = resp.json()
                results.append(data["embeddings"][0])
            except Exception as e:
                log.error("Embedding failed for text (len=%d): %s", len(text), e)
                results.append([0.0] * 768)  # nomic-embed-text dimension
        return results
```

Batch embedding sends all texts in a single Ollama call, which is significantly faster during indexing. Falls back to per-item on failure.

**Important**: if `chromadb` ships with `OllamaEmbeddingFunction` in `chromadb.utils.embedding_functions`, use that instead and delete the custom class. Check at import time and fall back to the custom one.

### Memory Extraction

After every chat exchange (user message + assistant response), extract facts **synchronously in a background thread**:

```python
def _extract_memories_sync(
    user_message: str,
    assistant_response: str,
    chat_source: str,
    config: dict[str, Any],
) -> None:
    """Extract facts from a conversation exchange and store in ChromaDB + SQLite.

    This is a synchronous function — run it in a thread executor so it
    doesn't block the response.
    """
    try:
        import ollama as ollama_lib

        extraction_response = ollama_lib.chat(
            model=config.get("ollama_model", "llama3.2:3b"),
            messages=[
                {"role": "system", "content": config.get("kitkat_memory_extraction_prompt", "")},
                {
                    "role": "user",
                    "content": (
                        f"User said: {user_message}\n\n"
                        f"Assistant replied: {assistant_response}"
                    ),
                },
            ],
            options={"temperature": 0.1, "num_predict": 500},
        )

        raw = extraction_response["message"]["content"].strip()
        # Handle markdown code blocks around JSON
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        facts = json.loads(raw)
        if not isinstance(facts, list):
            return

        collections = _get_chroma(config)
        conv_collection = collections["kitkat_conversations"]
        now = datetime.now(timezone.utc).isoformat()

        db_path = BASE_DIR / config.get("db_path", "bt_radar.db")
        conn = bt_db.get_connection(db_path)

        try:
            for fact in facts:
                if not isinstance(fact, str) or len(fact.strip()) < 5:
                    continue
                fact = fact.strip()

                # Deduplication: check for semantically similar existing memories
                if conv_collection.count() > 0:
                    existing = conv_collection.query(
                        query_texts=[fact],
                        n_results=1,
                    )
                    if (
                        existing["distances"]
                        and existing["distances"][0]
                        and existing["distances"][0][0] < 0.25
                    ):
                        # Similar fact already stored — update timestamp only
                        doc_id = existing["ids"][0][0]
                        conv_collection.update(
                            ids=[doc_id],
                            metadatas=[{
                                "type": "memory",
                                "source": "conversation",
                                "chat_source": chat_source,
                                "timestamp": now,
                            }],
                        )
                        log.debug("Updated existing memory: %s", doc_id)
                        continue

                # New fact — insert into ChromaDB and SQLite
                fact_hash = hashlib.sha256(fact.encode()).hexdigest()[:12]
                doc_id = f"mem_{int(time.time())}_{fact_hash}"

                with _write_lock:
                    conv_collection.add(
                        ids=[doc_id],
                        documents=[fact],
                        metadatas=[{
                            "type": "memory",
                            "source": "conversation",
                            "chat_source": chat_source,
                            "timestamp": now,
                            "chroma_id": doc_id,
                        }],
                    )

                bt_db.save_kitkat_memory(conn, fact, "conversation", chat_source, doc_id)
                log.info("Extracted memory: %s", fact[:80])
        finally:
            conn.close()

    except json.JSONDecodeError:
        log.warning("Memory extraction returned non-JSON — skipping")
    except Exception as e:
        log.error("Memory extraction failed: %s", e)
```

**Key changes from original spec**:
- **Synchronous**, not async — runs in `run_in_executor` thread pool
- Uses `threading.Lock` (`_write_lock`) which works correctly across threads
- Accepts `config` as a parameter instead of reading a module-level global
- Opens its own `conn` for SQLite writes (thread-safe with WAL mode)
- Adds `num_predict: 500` to limit Ollama output length for faster extraction
- Uses `datetime.now(timezone.utc)` instead of deprecated `datetime.utcnow()`
- Deduplication threshold raised to `0.25` (cosine distance) to catch semantic equivalents like "User's name is Richard" / "The user is called Richard"
- Stores `chroma_id` in SQLite metadata for cross-reference when deleting

### Context Retrieval

When the user sends a message, retrieve relevant context from all four collections. Uses **score-based merging** instead of rigid per-collection allocation:

```python
def _retrieve_context(query: str, config: dict[str, Any]) -> tuple[str, dict[str, int]]:
    """Query all ChromaDB collections and build a context block.

    Returns (context_string, counts_dict).
    counts_dict has keys: memories, obsidian, gdrive, calendar.
    """
    collections = _get_chroma(config)
    max_chunks = config.get("kitkat_max_context_chunks", 8)

    count_keys = {
        "kitkat_conversations": "memories",
        "kitkat_obsidian": "obsidian",
        "kitkat_gdrive": "gdrive",
        "kitkat_calendar": "calendar",
    }
    section_names = {
        "kitkat_conversations": "Memories",
        "kitkat_obsidian": "Obsidian Notes",
        "kitkat_gdrive": "Google Drive",
        "kitkat_calendar": "Calendar",
    }

    # Query each collection for up to max_chunks candidates
    all_results: list[tuple[float, str, str, dict]] = []  # (distance, coll_name, doc, meta)

    for coll_name in count_keys:
        coll = collections.get(coll_name)
        if coll is None or coll.count() == 0:
            continue

        try:
            results = coll.query(
                query_texts=[query],
                n_results=min(max_chunks, coll.count()),
            )
        except Exception as e:
            log.warning("ChromaDB query failed for %s: %s", coll_name, e)
            continue

        if not results["documents"] or not results["documents"][0]:
            continue

        for doc, dist, meta in zip(
            results["documents"][0],
            results["distances"][0],
            results["metadatas"][0],
        ):
            if dist > 0.8:
                continue
            all_results.append((dist, coll_name, doc, meta))

    # Sort by relevance (lowest distance first) and take top N
    all_results.sort(key=lambda r: r[0])
    selected = all_results[:max_chunks]

    # Group selected results by collection for display
    grouped: dict[str, list[str]] = {}
    counts: dict[str, int] = {"memories": 0, "obsidian": 0, "gdrive": 0, "calendar": 0}

    for _dist, coll_name, doc, meta in selected:
        key = count_keys[coll_name]
        counts[key] += 1

        if coll_name == "kitkat_obsidian":
            title = meta.get("title", "Unknown")
            heading = meta.get("heading", "")
            prefix = f'From "{title}"'
            if heading:
                prefix += f" > {heading}"
            item = f"- {prefix}: {doc}"
        elif coll_name == "kitkat_gdrive":
            filename = meta.get("filename", "Unknown")
            item = f'- From "{filename}": {doc}'
        else:
            item = f"- {doc}"

        section = section_names[coll_name]
        grouped.setdefault(section, []).append(item)

    if not grouped:
        return "", counts

    # Build context block with sections in a stable order
    context_parts = []
    for section in ("Memories", "Obsidian Notes", "Google Drive", "Calendar"):
        if section in grouped:
            context_parts.append(f"{section}:\n" + "\n".join(grouped[section]))

    context_block = "[Retrieved Context]\n" + "\n\n".join(context_parts) + "\n[End Context]"
    return context_block, counts
```

**Key change**: Instead of a fixed allocation (3 memories, 2 obsidian, 2 gdrive, 1 calendar), all collections compete on relevance score. If you have no gdrive content, those slots go to better-ranked results from other sources. The total is still capped at `kitkat_max_context_chunks`.

### Main Chat Function

Primary entry point — both web UI and Telegram call this. Routes chat through `bt_search` so Kitkat inherits web search, tool calling, error handling, and model fallback:

```python
def chat_sync(
    message: str,
    config: dict[str, Any],
    chat_source: str = "web",
) -> tuple[str, dict[str, int], bool]:
    """Send a message to Kitkat and get a response with RAG context.

    Synchronous — suitable for Flask routes (called directly) and Telegram
    (called via run_in_executor or from sync context).

    Args:
        message: The user's message text.
        config: Loaded config dict.
        chat_source: "web" or "telegram_{chat_id}".

    Returns:
        Tuple of (response_text, context_counts_dict, searched_web).
    """
    chat_id = f"kitkat_{chat_source}"
    db_path = BASE_DIR / config.get("db_path", "bt_radar.db")

    # 1. Retrieve RAG context (returns empty string if storage unavailable)
    context_block = ""
    context_counts = {"memories": 0, "obsidian": 0, "gdrive": 0, "calendar": 0}
    if _is_storage_available(config):
        try:
            context_block, context_counts = _retrieve_context(message, config)
        except Exception as e:
            log.error("Context retrieval failed: %s", e)

    # 2. Build system prompt with RAG context
    system_prompt = config.get("kitkat_system_prompt", "You are Kitkat, a personal AI assistant.")
    system_prompt += " Do not use emoji in your responses."
    if context_block:
        system_prompt += f"\n\n{context_block}"

    # 3. Load conversation history
    history_length = config.get("kitkat_conversation_history_length", 20)
    conn = bt_db.get_connection(db_path)
    try:
        history_rows = bt_db.get_chat_history(conn, chat_id, limit=history_length)
    finally:
        conn.close()

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend({"role": row["role"], "content": row["content"]} for row in history_rows)
    messages.append({"role": "user", "content": message})

    # 4. Call Ollama via bt_search (inherits web search, tool calling, fallback)
    response_text, searched = bt_search.chat_with_search_sync(messages, config)
    if response_text is None:
        response_text = (
            "Sorry, I'm having trouble thinking right now. "
            "Ollama might be busy or unavailable."
        )
        searched = False

    # 5. Save to conversation history
    conn = bt_db.get_connection(db_path)
    try:
        bt_db.save_chat_message(conn, chat_id, "user", message)
        bt_db.save_chat_message(conn, chat_id, "assistant", response_text)
    finally:
        conn.close()

    # 6. Extract memories in background thread (don't block the response)
    if _is_storage_available(config):
        import concurrent.futures
        _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        _executor.submit(
            _extract_memories_sync, message, response_text, chat_source, config,
        )

    return response_text, context_counts, searched


async def chat_async(
    message: str,
    config: dict[str, Any],
    chat_source: str = "web",
) -> tuple[str, dict[str, int], bool]:
    """Async wrapper around chat_sync for use in Telegram bot.

    Runs the synchronous chat in a thread executor to avoid blocking
    the Telegram event loop.
    """
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, chat_sync, message, config, chat_source,
    )
```

**Key changes from original spec**:
- **`chat_sync`** is the primary entry point (synchronous) — Flask calls it directly, matching how `bt_web.py` calls `bt_search.chat_with_search_sync()`
- **`chat_async`** wraps it for the Telegram bot via `run_in_executor`
- Routes through **`bt_search.chat_with_search_sync()`** instead of calling `ollama_lib.chat()` directly — Kitkat inherits web search, tool calling, error handling, timeout, and model fallback for free
- Returns a third value `searched` (bool) so the UI can show a "searched the web" badge
- Config and conn passed as parameters, not read from globals
- Memory extraction dispatched to a `ThreadPoolExecutor` (not `asyncio.run()` in an executor)

### Storage Availability Check

```python
def _is_storage_available(config: dict[str, Any]) -> bool:
    """Check if the external drive is mounted and the data dir is writable."""
    data_dir = config.get("kitkat_data_dir", "/mnt/external/kitkat")
    mount_point = os.path.dirname(data_dir)  # /mnt/external
    if not os.path.ismount(mount_point):
        return False
    try:
        os.makedirs(data_dir, exist_ok=True)
        return True
    except OSError:
        return False
```

When storage is unavailable:
- `chat_sync()` proceeds without RAG context (plain Ollama conversation via bt_search)
- Memory extraction is skipped
- Web UI shows: "External storage unavailable — Kitkat running in limited mode"
- Indexer logs error and retries next cycle

### Helper Functions

All helper functions accept `config` as a parameter and open their own database connections:

```python
def get_memories(config: dict[str, Any], limit: int = 50) -> list[dict]:
    """Return active memories from kitkat_memories SQLite table, most recent first."""
    db_path = BASE_DIR / config.get("db_path", "bt_radar.db")
    conn = bt_db.get_connection(db_path)
    try:
        return bt_db.get_kitkat_memories(conn, limit=limit, active_only=True)
    finally:
        conn.close()


def delete_memory(config: dict[str, Any], memory_id: int) -> bool:
    """Deactivate a memory in SQLite and remove its vector from ChromaDB conversations collection."""
    db_path = BASE_DIR / config.get("db_path", "bt_radar.db")
    conn = bt_db.get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT chroma_id FROM kitkat_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        bt_db.deactivate_kitkat_memory(conn, memory_id)
    finally:
        conn.close()

    # Remove from ChromaDB if we have the chroma_id
    if row and row["chroma_id"] and _is_storage_available(config):
        try:
            collections = _get_chroma(config)
            with _write_lock:
                collections["kitkat_conversations"].delete(ids=[row["chroma_id"]])
        except Exception as e:
            log.warning("Failed to delete ChromaDB vector %s: %s", row["chroma_id"], e)
            return False
    return True


def add_manual_memory(
    config: dict[str, Any], fact: str, chat_source: str = "manual",
) -> int:
    """Insert a memory into both ChromaDB and SQLite. Returns SQLite row id."""
    now = datetime.now(timezone.utc).isoformat()
    fact_hash = hashlib.sha256(fact.encode()).hexdigest()[:12]
    doc_id = f"mem_{int(time.time())}_{fact_hash}"

    # ChromaDB
    if _is_storage_available(config):
        collections = _get_chroma(config)
        with _write_lock:
            collections["kitkat_conversations"].add(
                ids=[doc_id],
                documents=[fact],
                metadatas=[{
                    "type": "memory",
                    "source": "manual",
                    "chat_source": chat_source,
                    "timestamp": now,
                    "chroma_id": doc_id,
                }],
            )

    # SQLite
    db_path = BASE_DIR / config.get("db_path", "bt_radar.db")
    conn = bt_db.get_connection(db_path)
    try:
        return bt_db.save_kitkat_memory(conn, fact, "manual", chat_source, doc_id)
    finally:
        conn.close()


def get_index_stats(config: dict[str, Any]) -> dict[str, int]:
    """Return document counts from each ChromaDB collection.
    Returns: {"conversations": N, "obsidian": N, "gdrive": N, "calendar": N}
    """
    if not _is_storage_available(config):
        return {"conversations": 0, "obsidian": 0, "gdrive": 0, "calendar": 0}
    collections = _get_chroma(config)
    return {
        "conversations": collections["kitkat_conversations"].count(),
        "obsidian": collections["kitkat_obsidian"].count(),
        "gdrive": collections["kitkat_gdrive"].count(),
        "calendar": collections["kitkat_calendar"].count(),
    }


def clear_conversation(config: dict[str, Any], chat_source: str = "web") -> None:
    """Clear Kitkat conversation history for the given source."""
    chat_id = f"kitkat_{chat_source}"
    db_path = BASE_DIR / config.get("db_path", "bt_radar.db")
    conn = bt_db.get_connection(db_path)
    try:
        bt_db.clear_chat_history(conn, chat_id)
    finally:
        conn.close()


def reset_memories(config: dict[str, Any]) -> None:
    """Full memory reset: clear conversations collection, deactivate all SQLite memories, clear chat history."""
    # ChromaDB
    if _is_storage_available(config):
        collections = _get_chroma(config)
        with _write_lock:
            # Delete all documents from conversations collection
            conv = collections["kitkat_conversations"]
            if conv.count() > 0:
                all_ids = conv.get()["ids"]
                conv.delete(ids=all_ids)

    # SQLite
    db_path = BASE_DIR / config.get("db_path", "bt_radar.db")
    conn = bt_db.get_connection(db_path)
    try:
        conn.execute("UPDATE kitkat_memories SET is_active = 0")
        conn.commit()
        # Clear all Kitkat chat histories
        conn.execute("DELETE FROM chat_history WHERE chat_id LIKE 'kitkat_%'")
        conn.commit()
    finally:
        conn.close()
```

### Database Additions

Add to `bt_db.py` in the `init_db()` function (after existing table creation, inside the `executescript` block):

```sql
CREATE TABLE IF NOT EXISTS kitkat_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'conversation',
    chat_source TEXT NOT NULL DEFAULT 'web',
    chroma_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_by INTEGER REFERENCES kitkat_memories(id),
    is_active INTEGER NOT NULL DEFAULT 1
);
```

The `chroma_id` column stores the ChromaDB document ID for cross-reference when deleting memories.

Also add these helper functions to `bt_db.py`, following the existing pattern (all take `conn` as first parameter):

```python
# ---------------------------------------------------------------------------
# Kitkat memories
# ---------------------------------------------------------------------------

def save_kitkat_memory(
    conn: sqlite3.Connection,
    fact: str,
    source: str,
    chat_source: str,
    chroma_id: str | None = None,
) -> int:
    """Insert a memory row and return its id."""
    cur = conn.execute(
        "INSERT INTO kitkat_memories (fact, source, chat_source, chroma_id) "
        "VALUES (?, ?, ?, ?)",
        (fact, source, chat_source, chroma_id),
    )
    conn.commit()
    return cur.lastrowid


def get_kitkat_memories(
    conn: sqlite3.Connection, limit: int = 50, active_only: bool = True,
) -> list[dict[str, Any]]:
    """Fetch memories ordered by created_at DESC."""
    where = "WHERE is_active = 1" if active_only else ""
    rows = conn.execute(
        f"SELECT id, fact, source, chat_source, chroma_id, created_at, is_active "
        f"FROM kitkat_memories {where} ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def deactivate_kitkat_memory(conn: sqlite3.Connection, memory_id: int) -> None:
    """Set is_active = 0."""
    conn.execute(
        "UPDATE kitkat_memories SET is_active = 0 WHERE id = ?", (memory_id,),
    )
    conn.commit()
```

The existing `chat_history` table is reused with Kitkat-specific chat_id values:
- Web: `"kitkat_web"`
- Telegram: `"kitkat_telegram_{numeric_chat_id}"`

Kitkat conversations are completely separate from existing Device Radar assistant conversations.

## Indexer Module: `bt_kitkat_index.py`

Runs as a standalone service or CLI tool. Indexes three knowledge sources into ChromaDB.

### Obsidian Vault Indexer

1. Recursively walk `kitkat_obsidian_path` for `.md` files
2. Skip directories starting with `.` (especially `.obsidian/`, `.trash/`)
3. For each markdown file:
   - Compute SHA-256 hash of file content
   - Check `index_state.json` — skip if hash unchanged since last index
   - Read the file (UTF-8, fall back to latin-1, skip on failure with warning)
   - **Chunking**: split on `## ` and `### ` headings, keeping each section as one chunk. If a section exceeds 1000 characters, split further at paragraph boundaries (`\n\n`). Minimum chunk size: 100 characters (skip tiny fragments). Prepend note title and section heading to each chunk for retrieval context
   - Metadata per chunk: `{"source": "obsidian", "file_path": relative_path, "title": note_title_from_filename, "heading": section_heading_or_empty, "indexed_at": iso_timestamp}`
   - **Delete-then-upsert**: remove all existing ChromaDB documents where metadata `file_path` matches this file, then insert new chunks. Handles edits cleanly
4. After all files, check for orphans: any `file_path` in ChromaDB that no longer exists on disk → delete those documents. Use `collection.get(where={"source": "obsidian"})` to list all obsidian documents, then check each `file_path` against the filesystem
5. Update `index_state.json` with new hashes

### Google Drive Indexer

1. Recursively walk `kitkat_gdrive_path` for files matching extensions from `config.get("kitkat_gdrive_extensions", _DEFAULT_GDRIVE_EXTENSIONS)`
2. Skip hidden directories and files (starting with `.`)
3. Same hash-based change detection as Obsidian
4. File reading by type:
   - `.md`, `.txt`, `.csv`, `.json`, `.py`, `.sh`, `.yml`, `.yaml`, `.toml`, `.cfg`, `.ini`, `.html`, `.xml` — read as plain text (UTF-8, fallback latin-1)
   - `.pdf` — `subprocess.run(["pdftotext", filepath, "-"], capture_output=True)`. If `pdftotext` not installed, skip PDFs with warning
   - `.docx` — `subprocess.run(["pandoc", filepath, "-t", "plain"], capture_output=True)`. If `pandoc` not installed, skip docx with warning
5. Same chunking strategy (heading-aware for markdown, paragraph-based for others, 1000-char max)
6. Metadata: `{"source": "gdrive", "file_path": relative_path, "filename": basename, "indexed_at": iso_timestamp}`
7. Same delete-then-upsert and orphan cleanup

### Calendar Indexer

1. Use `bt_calendar.get_events(config, calendar_names)` to fetch events via CalDAV. This is the existing async function — wrap with `asyncio.run()` in the indexer. Pass `None` for `calendar_names` to fetch all calendars, or use `bt_calendar.get_available_calendars(config)` to discover them first
2. Fetch events for the next 14 days (modify the `get_events` call or filter results by date)
3. Document string per event: `"{summary} on {date} from {start_time} to {end_time} [{calendar_name}]"` (append `" at {location}"` if location exists). Use the `CalendarEvent` fields: `event.summary`, `event.start`, `event.end`, `event.location`, `event.calendar_name`
4. Metadata: `{"source": "calendar", "calendar_name": cal_name, "event_date": date_str, "indexed_at": iso_timestamp}`
5. **Full refresh**: clear entire `kitkat_calendar` collection on each run, re-insert all events. Calendar data is small and changes frequently — full refresh is simpler than diffing

### Index State File

`/mnt/external/kitkat/index_state.json`:

```json
{
  "obsidian": {
    "files": {
      "relative/path/note.md": {
        "hash": "sha256hex",
        "indexed_at": "2025-01-01T00:00:00Z",
        "chunk_count": 5
      }
    },
    "last_full_index": "2025-01-01T00:00:00Z"
  },
  "gdrive": {
    "files": {},
    "last_full_index": null
  },
  "calendar": {
    "last_full_index": null
  }
}
```

Load/save with `json.load`/`json.dump`. If corrupt or missing, start fresh.

### Top-level `index_all` Function

```python
def index_all(config: dict[str, Any]) -> dict[str, int]:
    """Index all sources once and return stats.

    Called by CLI (no args), Telegram /kitkat reindex, and web reindex button.
    """
    index_obsidian(config)
    index_gdrive(config)
    index_calendar(config)
    import bt_kitkat
    return bt_kitkat.get_index_stats(config)
```

### Service Loop

```python
def run_service() -> None:
    """Main loop when running as systemd service. Blocking."""
    config = load_config()
    _ensure_data_dir(config)

    while True:
        config = load_config()  # Reload each cycle to pick up changes
        try:
            log.info("Starting index cycle")
            stats = index_all(config)
            log.info("Index complete. %s", stats)
        except Exception as e:
            log.error("Index cycle error: %s", e)

        interval = config.get("kitkat_index_interval_minutes", 30)
        time.sleep(interval * 60)
```

**Note**: The service loop is synchronous (no asyncio) — it calls synchronous ChromaDB operations directly. The only async call is `bt_calendar.get_events()` for the calendar indexer, which is wrapped with `asyncio.run()` inside `index_calendar()`.

### CLI Interface

```bash
python3 bt_kitkat_index.py                    # index all sources once, exit
python3 bt_kitkat_index.py --source obsidian  # index only Obsidian
python3 bt_kitkat_index.py --source gdrive    # index only Google Drive
python3 bt_kitkat_index.py --source calendar  # index only calendar
python3 bt_kitkat_index.py --stats            # print collection counts
python3 bt_kitkat_index.py --service          # run as continuous service
```

Use `argparse`. The `--service` flag is what the systemd unit invokes.

### Logging

Logger name: `kitkat.indexer`. When running as a service, log to both stdout (journald picks this up) and `/mnt/external/kitkat/logs/indexer.log` (RotatingFileHandler, 5MB max, 3 backups).

## Web Interface: `bt_kitkat_web.py`

Flask Blueprint registered into the existing `bt_web.py`.

### Registration in `bt_web.py`

Add these lines inside the `main()` function of `bt_web.py`, after `bt_db.init_db(get_db_path())` and before `app.run(...)`:

```python
if config.get("kitkat_enabled"):
    from bt_kitkat_web import kitkat_bp
    app.register_blueprint(kitkat_bp)
```

Also add "Kitkat" to the nav bar in `templates/base.html` (see Navigation section below).

These are the **ONLY** changes to `bt_web.py` and `templates/base.html`.

### Routes

| Route | Method | Description |
|---|---|---|
| `/kitkat` | GET | Chat interface page |
| `/api/kitkat/chat` | POST | Send message, get response |
| `/api/kitkat/history` | GET | Conversation history |
| `/api/kitkat/history` | DELETE | Clear conversation history |
| `/api/kitkat/memories` | GET | List active memories |
| `/api/kitkat/memories` | POST | Add manual memory (`{"fact": "text"}`) |
| `/api/kitkat/memories/<id>` | DELETE | Delete/deactivate a memory |
| `/api/kitkat/stats` | GET | Index stats (collection counts) |
| `/api/kitkat/reindex` | POST | Trigger reindex in background |
| `/api/kitkat/reset` | POST | Full memory reset (requires `{"confirm": true}`) |

### Chat API

**POST `/api/kitkat/chat`**

Request: `{"message": "What's on my calendar tomorrow?"}`

Response:
```json
{
  "response": "You've got a team standup at 9:30 and a dentist appointment at 2pm.",
  "context_used": {
    "memories": 0,
    "obsidian": 0,
    "gdrive": 0,
    "calendar": 2
  },
  "searched": false
}
```

The `searched` field indicates whether web search was invoked (inherited from `bt_search`).

### Route Implementations

All routes follow the existing Flask pattern — load config, call sync functions directly:

```python
@kitkat_bp.route("/api/kitkat/chat", methods=["POST"])
def api_kitkat_chat():
    data = request.get_json()
    if not data or "message" not in data:
        return jsonify({"error": "message required"}), 400

    config = load_config()
    response_text, context_counts, searched = bt_kitkat.chat_sync(
        data["message"], config, chat_source="web",
    )
    return jsonify({
        "response": response_text,
        "context_used": context_counts,
        "searched": searched,
    })
```

### Chat Page: `templates/kitkat.html`

Follow existing Device Radar dark theme. Reference `templates/assistant.html` for layout, CSS, and JS patterns.

**Structure** (matches assistant.html pattern — full-width chat with toolbar):
- **Toolbar row** (above chat, same pattern as assistant.html):
  - Left: "Kitkat" title with "Personal Memory Agent" subtitle
  - Right: action buttons — "Memories" (opens modal), "Stats" (opens modal), "Reindex", "Clear Chat"
- **Chat area**: scrollable message list, auto-scroll on new messages
  - Full-width message rows (same `.chat-row` pattern as assistant.html)
  - Each message: avatar, sender ("You" / "Kitkat"), text, timestamp (hover-visible)
  - Kitkat messages show context badges when RAG was used (e.g., "2 memories, 1 note, 1 calendar")
  - Show "Searched the web" badge when `searched` is true (same pattern as assistant.html)
- **Input**: text field + send button at bottom (same pattern as assistant page)
- **Loading indicator**: dot-pulse animation (same as assistant.html)

**Modals** (simple overlay divs, no framework needed):
- **Memories modal**: list of active memories with delete (x) buttons; "Add memory" text input + button at top; fetches from `/api/kitkat/memories`
- **Stats modal**: collection counts from `/api/kitkat/stats`, storage status
- **Reset confirmation**: JS `confirm()` dialog before calling `/api/kitkat/reset`

**JavaScript**: Same IIFE pattern as assistant.html. Fetch API for all HTTP calls. localStorage for any persistent UI state.

### Navigation

Add "Kitkat" to the nav bar in `templates/base.html`, after the "Assistant" link:

```html
<a href="/kitkat" class="{% if active == 'kitkat' %}active{% endif %}">Kitkat</a>
```

Match the existing link pattern exactly. The template passes `active="kitkat"` from `bt_kitkat_web.py`.

## Telegram Integration: `bt_kitkat_telegram.py`

Provides functions called from `bt_telegram.py`. Does NOT run its own bot or polling loop.

### Kitkat Mode

Per-chat toggle stored in memory (dict keyed by chat_id, default OFF). When ON, non-command non-presence messages route to Kitkat instead of the regular assistant.

```python
"""Kitkat Telegram integration for Device Radar."""
from __future__ import annotations

import logging
from typing import Any

import bt_kitkat

log = logging.getLogger(__name__)

_kitkat_mode: dict[int, bool] = {}


def is_kitkat_mode(chat_id: int) -> bool:
    return _kitkat_mode.get(chat_id, False)


def set_kitkat_mode(chat_id: int, enabled: bool):
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
        return "Kitkat mode ON — I'll remember what you tell me."

    elif cmd == "off":
        set_kitkat_mode(chat_id, False)
        return "Kitkat mode OFF — back to regular assistant."

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
        import bt_kitkat_index
        import asyncio
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, bt_kitkat_index.index_all, config)
        return "Reindexing started in background."

    else:
        return ("Kitkat commands:\n"
                "  /kitkat — toggle on/off\n"
                "  /kitkat on|off\n"
                "  /kitkat memories — list recent\n"
                "  /kitkat stats — index stats\n"
                "  /kitkat remember <fact>\n"
                "  /kitkat forget <text>\n"
                "  /kitkat clear — clear history\n"
                "  /kitkat reindex")
```

**Changes from original spec**:
- All functions accept `config` parameter (passed from `bt_telegram.py`)
- No emoji in responses (consistent with Device Radar system prompt directive)
- `handle_message` uses `chat_async` (the async wrapper)
- `handle_command` reindex uses `run_in_executor` with the sync `index_all` (not `asyncio.create_task` which would require an async function)
- Shows "searched the web" prefix when web search was used

### Hook Points in `bt_telegram.py`

Three surgical additions to the existing file:

**1. Import** (at top of file, after existing imports):
```python
try:
    import bt_kitkat_telegram
    _HAS_KITKAT = True
except ImportError:
    _HAS_KITKAT = False
```

Use try/except rather than config check at import time — config isn't loaded yet at module level, and this prevents import errors from crashing the bot.

**2. Command handler** (in `main()`, alongside other `CommandHandler` registrations):
```python
if _HAS_KITKAT and config.get("kitkat_enabled"):
    app.add_handler(CommandHandler("kitkat", _cmd_kitkat))
```

And the handler function (alongside other `_cmd_*` functions):
```python
async def _cmd_kitkat(update, context) -> None:
    """Handle /kitkat commands."""
    if not _is_authorized(update.effective_chat.id):
        return
    config = load_config()
    if not config.get("kitkat_enabled"):
        await update.message.reply_text("Kitkat is not enabled.")
        return
    chat_id = update.effective_chat.id
    args = context.args or []
    response = await bt_kitkat_telegram.handle_command(chat_id, args, config)
    await update.message.reply_text(response)
```

**3. Message routing** (in `_handle_message`, after the presence query check and BEFORE the "General chat" section):
```python
    # Kitkat mode — route to personal memory agent
    if (
        _HAS_KITKAT
        and config.get("kitkat_enabled")
        and bt_kitkat_telegram.is_kitkat_mode(update.effective_chat.id)
    ):
        await update.effective_chat.send_action(ChatAction.TYPING)
        response = await bt_kitkat_telegram.handle_message(
            update.effective_chat.id, text, config,
        )
        await update.message.reply_text(response)
        return
```

This goes at approximately line 912 of `bt_telegram.py`, after the `is_presence_query` check returns and before the existing `# General chat` comment.

**4. Register /kitkat in the bot command menu** (in `_post_init`):
```python
BotCommand("kitkat", "Personal memory agent (on/off/memories/stats)"),
```

These are the **ONLY** changes to `bt_telegram.py`. Using `CommandHandler` for `/kitkat` is cleaner than parsing it manually in the message handler — it follows the existing pattern for all other commands.

## Systemd Service

Create `bt-kitkat-indexer.service`:

```ini
[Unit]
Description=Kitkat Knowledge Indexer
After=network.target
Wants=bt-scanner.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/bt-monitor/bt_kitkat_index.py --service
WorkingDirectory=/opt/bt-monitor
EnvironmentFile=/home/pi/.device-radar.env
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo cp bt-kitkat-indexer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bt-kitkat-indexer
sudo systemctl start bt-kitkat-indexer
```

## External Hard Drive Setup

### Create data directories

```bash
sudo mkdir -p /mnt/external/kitkat/chroma_db
sudo mkdir -p /mnt/external/kitkat/logs
```

### Ensure mount on boot

If `/mnt/external` is not in `/etc/fstab`:

```bash
echo "UUID=$(blkid -s UUID -o value /dev/sda1) /mnt/external ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
```

The `nofail` flag prevents boot failure if the drive is disconnected.

## File Structure (New Files)

```
bt-monitor/
├── bt_kitkat.py                  # Core RAG engine
├── bt_kitkat_index.py            # Background indexer + CLI
├── bt_kitkat_web.py              # Flask blueprint
├── bt_kitkat_telegram.py         # Telegram integration
├── bt-kitkat-indexer.service     # Systemd unit
└── templates/
    └── kitkat.html               # Web chat interface
```

Modifications to existing files (minimal):
- `bt_db.py` — add `kitkat_memories` table + 3 helper functions
- `bt_web.py` — add 3 lines for blueprint registration (inside `main()`)
- `bt_telegram.py` — add import, command handler, message routing, bot command menu entry
- `templates/base.html` — add "Kitkat" nav link
- `requirements.txt` — add `chromadb>=0.5.0`

## Implementation Order

1. **`bt_db.py`** — add table + helpers. Test: `python3 -c "import bt_db; bt_db.init_db()"`
2. **`bt_kitkat.py`** — core engine. Build incrementally: ChromaDB init → `chat_sync` without RAG → context retrieval → memory extraction. Test via Python script
3. **`bt_kitkat_index.py`** — Obsidian first, then GDrive, then calendar. Test with `--stats`
4. **`bt_kitkat_web.py` + `kitkat.html`** — blueprint + UI. Register in `bt_web.py`. Add nav link to `base.html`. Test in browser
5. **`bt_kitkat_telegram.py`** — add hooks to `bt_telegram.py`. Test via Telegram
6. **Systemd service** — create, enable, start. Verify with `journalctl -u bt-kitkat-indexer -f`
7. **End-to-end**: tell Kitkat your name via web → ask via Telegram → verify memory persists; ask about Obsidian notes → verify RAG; ask about calendar → verify; delete memory → verify gone; unmount drive → verify degradation; remount → verify recovery

## Performance Notes

- **nomic-embed-text**: ~200ms per embedding on Pi 5 CPU, ~270MB model. Batch embedding reduces per-item overhead during indexing
- **Memory extraction**: ~5–10s per exchange (limited by `num_predict: 500`), runs in background thread after response is sent
- **ChromaDB on USB SSD**: negligible latency. Spinning HDD: ~50–100ms per query (fine)
- **ChromaDB RAM**: ~200–500MB depending on collection size. Monitor on first deploy
- **Initial indexing**: ~5 mins for 500-note vault. Subsequent runs fast (hash-skip)
- **Concurrency**: `_write_lock` (`threading.Lock`) around all ChromaDB writes protects against concurrent background memory extraction and indexer writes
- **Web search**: Kitkat inherits web search from `bt_search` — keyword-gated so regular chat stays fast (~8s), search queries take ~2 min

## Code Style

Follow existing Device Radar conventions:
- Type hints, `from __future__ import annotations`
- Config loaded fresh from disk, passed as `config: dict[str, Any]` parameter — no module-level config globals
- Database functions take `conn: sqlite3.Connection` as first parameter
- `logging` module, format: `%(asctime)s [%(levelname)s] %(message)s`
- No global mutable state except lazy-init caches (ChromaDB client, collections)
- Single-file modules
- `datetime.now(timezone.utc)` instead of deprecated `datetime.utcnow()`
