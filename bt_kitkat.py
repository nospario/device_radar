"""Kitkat — personal memory RAG engine for Device Radar.

Core module handling ChromaDB initialisation, context retrieval,
chat orchestration via bt_search, and background memory extraction.
"""
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

import bt_db
import bt_search

log = logging.getLogger("kitkat")

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
BASE_DIR = Path(__file__).resolve().parent

_DEFAULT_GDRIVE_EXTENSIONS = (
    ".md", ".txt", ".pdf", ".docx", ".csv", ".json",
    ".py", ".sh", ".yml", ".yaml", ".toml", ".cfg",
    ".ini", ".html", ".xml",
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict[str, Any]:
    """Load config.json from the script directory."""
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# ChromaDB setup (lazy init)
# ---------------------------------------------------------------------------

_chroma_client = None
_collections: dict[str, Any] = {}
_write_lock = threading.Lock()

# Single-thread executor for background memory extraction
_bg_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)


def _get_chroma(config: dict[str, Any]) -> dict[str, Any]:
    """Lazy-init ChromaDB client and collections."""
    global _chroma_client, _collections
    if _chroma_client is not None:
        return _collections

    import chromadb

    data_dir = config.get("kitkat_data_dir", "/mnt/external/kitkat")
    chroma_path = os.path.join(data_dir, "chroma_db")
    os.makedirs(chroma_path, exist_ok=True)

    _chroma_client = chromadb.PersistentClient(path=chroma_path)

    ef = _get_embedding_function(config)

    for name in (
        "kitkat_conversations",
        "kitkat_obsidian",
        "kitkat_gdrive",
        "kitkat_calendar",
    ):
        _collections[name] = _chroma_client.get_or_create_collection(
            name=name,
            embedding_function=ef,
            metadata={"hnsw:space": "cosine"},
        )

    return _collections


def _get_embedding_function(config: dict[str, Any]):
    """Return an embedding function — prefer ChromaDB built-in, fallback to custom."""
    ollama_url = config.get("ollama_url", "http://localhost:11434")
    model = config.get("kitkat_embedding_model", "nomic-embed-text:latest")

    # Use our custom embedding function — the built-in ChromaDB one doesn't
    # handle errors gracefully (crashes on texts exceeding context length)
    return _make_embedding_function(ollama_url, model)


def _make_embedding_function(ollama_url: str, model: str):
    """Create a ChromaDB-compatible embedding function.

    Uses chromadb.api.types.EmbeddingFunction as base class so ChromaDB's
    query() and add() methods work correctly (they call embed_query/embed).
    """
    try:
        from chromadb.api.types import EmbeddingFunction
    except ImportError:
        EmbeddingFunction = object

    _url = ollama_url.rstrip("/")
    _model = model

    class _OllamaEF(EmbeddingFunction):

        @staticmethod
        def name() -> str:
            return "kitkat_ollama"

        def __call__(self, input: list[str]) -> list:
            if not input:
                return []
            # Truncate texts exceeding embedding model context
            max_chars = 6000
            input = [t[:max_chars] if len(t) > max_chars else t for t in input]
            # Try batch embedding
            try:
                resp = httpx.post(
                    f"{_url}/api/embed",
                    json={"model": _model, "input": input},
                    timeout=120.0,
                )
                resp.raise_for_status()
                data = resp.json()
                embeddings = data["embeddings"]
                if len(embeddings) == len(input):
                    return embeddings
                log.warning(
                    "Batch embed returned %d vectors for %d inputs",
                    len(embeddings), len(input),
                )
            except Exception as e:
                log.warning("Batch embedding failed: %s — per-item fallback", e)

            # Fallback: embed one at a time
            results = []
            for text in input:
                try:
                    resp = httpx.post(
                        f"{_url}/api/embed",
                        json={"model": _model, "input": text},
                        timeout=60.0,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    results.append(data["embeddings"][0])
                except Exception as e:
                    log.error("Embedding failed (len=%d): %s", len(text), e)
                    results.append([0.0] * 768)
            return results

    return _OllamaEF()


# ---------------------------------------------------------------------------
# Storage availability
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Context retrieval (score-based merging across all collections)
# ---------------------------------------------------------------------------

def _retrieve_context(
    query: str, config: dict[str, Any],
) -> tuple[str, dict[str, int]]:
    """Query all ChromaDB collections and build a context block.

    Returns (context_string, counts_dict).
    counts_dict has keys: memories, obsidian, gdrive, calendar.
    Uses score-based merging — best results across all collections compete
    for the top N slots regardless of source.
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
    all_results: list[tuple[float, str, str, dict]] = []

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

    context_block = (
        "[Retrieved Context]\n"
        + "\n\n".join(context_parts)
        + "\n[End Context]"
    )
    return context_block, counts


# ---------------------------------------------------------------------------
# Memory extraction (runs synchronously in background thread)
# ---------------------------------------------------------------------------

def _extract_memories_sync(
    user_message: str,
    assistant_response: str,
    chat_source: str,
    config: dict[str, Any],
) -> None:
    """Extract facts from a conversation exchange and store in ChromaDB + SQLite.

    Synchronous — dispatched to _bg_executor so it doesn't block the response.
    """
    try:
        import ollama as ollama_lib

        extraction_response = ollama_lib.chat(
            model=config.get("ollama_model", "llama3.2:3b"),
            messages=[
                {
                    "role": "system",
                    "content": config.get(
                        "kitkat_memory_extraction_prompt",
                        "Extract facts as a JSON array of strings. Return [] if none.",
                    ),
                },
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
                        # Similar fact already stored — update timestamp
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


# ---------------------------------------------------------------------------
# Main chat functions
# ---------------------------------------------------------------------------

def chat_sync(
    message: str,
    config: dict[str, Any],
    chat_source: str = "web",
) -> tuple[str, dict[str, int], bool]:
    """Send a message to Kitkat and get a response with RAG context.

    Synchronous — Flask calls directly, Telegram wraps in run_in_executor.

    Returns (response_text, context_counts_dict, searched_web).
    """
    chat_id = f"kitkat_{chat_source}"
    db_path = BASE_DIR / config.get("db_path", "bt_radar.db")

    # 1. Retrieve RAG context
    context_block = ""
    context_counts: dict[str, int] = {
        "memories": 0, "obsidian": 0, "gdrive": 0, "calendar": 0,
    }
    if _is_storage_available(config):
        try:
            context_block, context_counts = _retrieve_context(message, config)
        except Exception as e:
            log.error("Context retrieval failed: %s", e)

    # 2. Build system prompt with RAG context
    system_prompt = config.get(
        "kitkat_system_prompt",
        "You are Kitkat, a personal AI assistant running locally on a "
        "Raspberry Pi. Be conversational, warm, and concise.",
    )
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

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    messages.extend(
        {"role": row["role"], "content": row["content"]} for row in history_rows
    )
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

    # 6. Extract memories in background thread
    if _is_storage_available(config):
        _bg_executor.submit(
            _extract_memories_sync, message, response_text, chat_source, config,
        )

    return response_text, context_counts, searched


async def chat_async(
    message: str,
    config: dict[str, Any],
    chat_source: str = "web",
) -> tuple[str, dict[str, int], bool]:
    """Async wrapper around chat_sync for the Telegram bot."""
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, chat_sync, message, config, chat_source,
    )


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def get_memories(config: dict[str, Any], limit: int = 50) -> list[dict]:
    """Return active memories from kitkat_memories table, most recent first."""
    db_path = BASE_DIR / config.get("db_path", "bt_radar.db")
    conn = bt_db.get_connection(db_path)
    try:
        return bt_db.get_kitkat_memories(conn, limit=limit, active_only=True)
    finally:
        conn.close()


def delete_memory(config: dict[str, Any], memory_id: int) -> bool:
    """Deactivate a memory in SQLite and remove its vector from ChromaDB."""
    db_path = BASE_DIR / config.get("db_path", "bt_radar.db")
    conn = bt_db.get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT chroma_id FROM kitkat_memories WHERE id = ?", (memory_id,),
        ).fetchone()
        bt_db.deactivate_kitkat_memory(conn, memory_id)
    finally:
        conn.close()

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

    db_path = BASE_DIR / config.get("db_path", "bt_radar.db")
    conn = bt_db.get_connection(db_path)
    try:
        return bt_db.save_kitkat_memory(conn, fact, "manual", chat_source, doc_id)
    finally:
        conn.close()


def get_index_stats(config: dict[str, Any]) -> dict[str, int]:
    """Return document counts from each ChromaDB collection."""
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
    """Full memory reset: clear conversations collection, deactivate all SQLite memories."""
    if _is_storage_available(config):
        collections = _get_chroma(config)
        with _write_lock:
            conv = collections["kitkat_conversations"]
            if conv.count() > 0:
                all_ids = conv.get()["ids"]
                conv.delete(ids=all_ids)

    db_path = BASE_DIR / config.get("db_path", "bt_radar.db")
    conn = bt_db.get_connection(db_path)
    try:
        conn.execute("UPDATE kitkat_memories SET is_active = 0")
        conn.commit()
        conn.execute("DELETE FROM chat_history WHERE chat_id LIKE 'kitkat_%'")
        conn.commit()
    finally:
        conn.close()
