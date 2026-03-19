#!/usr/bin/env python3
"""Kitkat Knowledge Indexer — indexes Obsidian vault, Google Drive, and
calendar events into ChromaDB for RAG retrieval."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

log = logging.getLogger("kitkat.indexer")

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
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Index state persistence
# ---------------------------------------------------------------------------

def _state_path(config: dict[str, Any]) -> Path:
    data_dir = config.get("kitkat_data_dir", "/mnt/external/kitkat")
    return Path(data_dir) / "index_state.json"


def _load_state(config: dict[str, Any]) -> dict:
    path = _state_path(config)
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "obsidian": {"files": {}, "last_full_index": None},
            "gdrive": {"files": {}, "last_full_index": None},
            "calendar": {"last_full_index": None},
        }


def _save_state(config: dict[str, Any], state: dict) -> None:
    path = _state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Chunking utilities
# ---------------------------------------------------------------------------

def _chunk_markdown(text: str, title: str, max_size: int = 1000, min_size: int = 100) -> list[dict]:
    """Split markdown into chunks by headings, then by paragraphs if too large.

    Returns list of {"text": str, "heading": str} dicts.
    """
    # Split on ## and ### headings
    sections = re.split(r"(?m)^(#{2,3}\s+.+)$", text)

    chunks = []
    current_heading = ""

    i = 0
    while i < len(sections):
        part = sections[i]
        if re.match(r"^#{2,3}\s+", part):
            current_heading = part.lstrip("#").strip()
            i += 1
            continue

        # Prepend title context
        prefix = title
        if current_heading:
            prefix += f" > {current_heading}"

        if len(part.strip()) < min_size:
            i += 1
            continue

        if len(part) <= max_size:
            chunks.append({"text": f"{prefix}: {part.strip()}", "heading": current_heading})
        else:
            # Split on paragraph boundaries
            paragraphs = part.split("\n\n")
            buffer = ""
            for para in paragraphs:
                if buffer and len(buffer) + len(para) > max_size:
                    if len(buffer.strip()) >= min_size:
                        chunks.append({"text": f"{prefix}: {buffer.strip()}", "heading": current_heading})
                    buffer = para
                else:
                    buffer = f"{buffer}\n\n{para}" if buffer else para
            if buffer.strip() and len(buffer.strip()) >= min_size:
                chunks.append({"text": f"{prefix}: {buffer.strip()}", "heading": current_heading})

        i += 1

    # If no sections were found (no headings), chunk the whole thing
    if not chunks and len(text.strip()) >= min_size:
        if len(text) <= max_size:
            chunks.append({"text": f"{title}: {text.strip()}", "heading": ""})
        else:
            paragraphs = text.split("\n\n")
            buffer = ""
            for para in paragraphs:
                if buffer and len(buffer) + len(para) > max_size:
                    if len(buffer.strip()) >= min_size:
                        chunks.append({"text": f"{title}: {buffer.strip()}", "heading": ""})
                    buffer = para
                else:
                    buffer = f"{buffer}\n\n{para}" if buffer else para
            if buffer.strip() and len(buffer.strip()) >= min_size:
                chunks.append({"text": f"{title}: {buffer.strip()}", "heading": ""})

    return chunks


def _chunk_text(text: str, filename: str, max_size: int = 1000, min_size: int = 100) -> list[dict]:
    """Split plain text into paragraph-based chunks."""
    if len(text.strip()) < min_size:
        return []

    if len(text) <= max_size:
        return [{"text": f"{filename}: {text.strip()}", "heading": ""}]

    chunks = []
    paragraphs = text.split("\n\n")
    buffer = ""
    for para in paragraphs:
        if buffer and len(buffer) + len(para) > max_size:
            if len(buffer.strip()) >= min_size:
                chunks.append({"text": f"{filename}: {buffer.strip()}", "heading": ""})
            buffer = para
        else:
            buffer = f"{buffer}\n\n{para}" if buffer else para
    if buffer.strip() and len(buffer.strip()) >= min_size:
        chunks.append({"text": f"{filename}: {buffer.strip()}", "heading": ""})

    return chunks


def _file_hash(filepath: str) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_file(filepath: str) -> str | None:
    """Read a text file with encoding fallback. Returns None on failure."""
    for encoding in ("utf-8", "latin-1"):
        try:
            return Path(filepath).read_text(encoding=encoding)
        except (UnicodeDecodeError, ValueError):
            continue
        except Exception as e:
            log.warning("Failed to read %s: %s", filepath, e)
            return None
    return None


# ---------------------------------------------------------------------------
# Obsidian vault indexer
# ---------------------------------------------------------------------------

def index_obsidian(config: dict[str, Any]) -> int:
    """Index the Obsidian vault into ChromaDB. Returns number of files indexed."""
    import bt_kitkat

    vault_path = config.get("kitkat_obsidian_path", "")
    if not vault_path or not os.path.isdir(vault_path):
        log.warning("Obsidian vault path not configured or not found: %s", vault_path)
        return 0

    collections = bt_kitkat._get_chroma(config)
    coll = collections["kitkat_obsidian"]
    state = _load_state(config)
    obsidian_state = state.setdefault("obsidian", {"files": {}, "last_full_index": None})
    file_states = obsidian_state.setdefault("files", {})
    now = datetime.now(timezone.utc).isoformat()

    indexed_count = 0
    seen_paths: set[str] = set()

    for root, dirs, files in os.walk(vault_path):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith(".")]

        for fname in files:
            if not fname.endswith(".md"):
                continue
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, vault_path)
            seen_paths.add(rel_path)

            # Hash-based change detection
            try:
                current_hash = _file_hash(full_path)
            except Exception as e:
                log.warning("Cannot hash %s: %s", rel_path, e)
                continue

            existing = file_states.get(rel_path)
            if existing and existing.get("hash") == current_hash:
                continue

            # Read and chunk
            content = _read_file(full_path)
            if content is None:
                continue

            title = Path(fname).stem
            chunks = _chunk_markdown(content, title)
            if not chunks:
                continue

            # Delete-then-upsert
            try:
                existing_docs = coll.get(where={"file_path": rel_path})
                if existing_docs["ids"]:
                    coll.delete(ids=existing_docs["ids"])
            except Exception:
                pass

            ids = []
            documents = []
            metadatas = []
            for i, chunk in enumerate(chunks):
                chunk_id = f"obs_{hashlib.sha256(f'{rel_path}_{i}'.encode()).hexdigest()[:16]}"
                ids.append(chunk_id)
                documents.append(chunk["text"])
                metadatas.append({
                    "source": "obsidian",
                    "file_path": rel_path,
                    "title": title,
                    "heading": chunk.get("heading", ""),
                    "indexed_at": now,
                })

            with bt_kitkat._write_lock:
                coll.add(ids=ids, documents=documents, metadatas=metadatas)

            file_states[rel_path] = {
                "hash": current_hash,
                "indexed_at": now,
                "chunk_count": len(chunks),
            }
            indexed_count += 1
            log.info("Indexed Obsidian note: %s (%d chunks)", rel_path, len(chunks))

    # Orphan cleanup — remove docs for files that no longer exist
    try:
        all_docs = coll.get(where={"source": "obsidian"})
        orphan_ids = []
        for doc_id, meta in zip(all_docs["ids"], all_docs["metadatas"]):
            fp = meta.get("file_path", "")
            if fp and fp not in seen_paths:
                orphan_ids.append(doc_id)
        if orphan_ids:
            coll.delete(ids=orphan_ids)
            log.info("Removed %d orphaned Obsidian documents", len(orphan_ids))
    except Exception as e:
        log.warning("Orphan cleanup failed: %s", e)

    # Remove stale entries from state
    for fp in list(file_states.keys()):
        if fp not in seen_paths:
            del file_states[fp]

    obsidian_state["last_full_index"] = now
    _save_state(config, state)
    return indexed_count


# ---------------------------------------------------------------------------
# Google Drive indexer
# ---------------------------------------------------------------------------

def _read_pdf(filepath: str) -> str | None:
    """Extract text from PDF using pdftotext."""
    try:
        result = subprocess.run(
            ["pdftotext", filepath, "-"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except FileNotFoundError:
        log.warning("pdftotext not installed — skipping PDF files")
    except Exception as e:
        log.warning("PDF extraction failed for %s: %s", filepath, e)
    return None


def _read_docx(filepath: str) -> str | None:
    """Extract text from DOCX using pandoc."""
    try:
        result = subprocess.run(
            ["pandoc", filepath, "-t", "plain"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except FileNotFoundError:
        log.warning("pandoc not installed — skipping DOCX files")
    except Exception as e:
        log.warning("DOCX extraction failed for %s: %s", filepath, e)
    return None


def index_gdrive(config: dict[str, Any]) -> int:
    """Index Google Drive files into ChromaDB. Returns number of files indexed."""
    import bt_kitkat

    gdrive_path = config.get("kitkat_gdrive_path", "")
    if not gdrive_path or not os.path.isdir(gdrive_path):
        log.warning("Google Drive path not configured or not found: %s", gdrive_path)
        return 0

    extensions = tuple(
        config.get("kitkat_gdrive_extensions", _DEFAULT_GDRIVE_EXTENSIONS)
    )
    collections = bt_kitkat._get_chroma(config)
    coll = collections["kitkat_gdrive"]
    state = _load_state(config)
    gdrive_state = state.setdefault("gdrive", {"files": {}, "last_full_index": None})
    file_states = gdrive_state.setdefault("files", {})
    now = datetime.now(timezone.utc).isoformat()

    indexed_count = 0
    seen_paths: set[str] = set()

    for root, dirs, files in os.walk(gdrive_path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]

        for fname in files:
            if fname.startswith("."):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in extensions:
                continue

            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, gdrive_path)
            seen_paths.add(rel_path)

            try:
                current_hash = _file_hash(full_path)
            except Exception:
                continue

            existing = file_states.get(rel_path)
            if existing and existing.get("hash") == current_hash:
                continue

            # Read by type
            if ext == ".pdf":
                content = _read_pdf(full_path)
            elif ext == ".docx":
                content = _read_docx(full_path)
            else:
                content = _read_file(full_path)

            if not content:
                continue

            # Chunk
            if ext == ".md":
                chunks = _chunk_markdown(content, Path(fname).stem)
            else:
                chunks = _chunk_text(content, fname)

            if not chunks:
                continue

            # Delete-then-upsert
            try:
                existing_docs = coll.get(where={"file_path": rel_path})
                if existing_docs["ids"]:
                    coll.delete(ids=existing_docs["ids"])
            except Exception:
                pass

            ids = []
            documents = []
            metadatas = []
            for i, chunk in enumerate(chunks):
                chunk_id = f"gd_{hashlib.sha256(f'{rel_path}_{i}'.encode()).hexdigest()[:16]}"
                ids.append(chunk_id)
                documents.append(chunk["text"])
                metadatas.append({
                    "source": "gdrive",
                    "file_path": rel_path,
                    "filename": fname,
                    "indexed_at": now,
                })

            with bt_kitkat._write_lock:
                coll.add(ids=ids, documents=documents, metadatas=metadatas)

            file_states[rel_path] = {
                "hash": current_hash,
                "indexed_at": now,
                "chunk_count": len(chunks),
            }
            indexed_count += 1
            log.info("Indexed GDrive file: %s (%d chunks)", rel_path, len(chunks))

    # Orphan cleanup
    try:
        all_docs = coll.get(where={"source": "gdrive"})
        orphan_ids = []
        for doc_id, meta in zip(all_docs["ids"], all_docs["metadatas"]):
            fp = meta.get("file_path", "")
            if fp and fp not in seen_paths:
                orphan_ids.append(doc_id)
        if orphan_ids:
            coll.delete(ids=orphan_ids)
            log.info("Removed %d orphaned GDrive documents", len(orphan_ids))
    except Exception as e:
        log.warning("GDrive orphan cleanup failed: %s", e)

    for fp in list(file_states.keys()):
        if fp not in seen_paths:
            del file_states[fp]

    gdrive_state["last_full_index"] = now
    _save_state(config, state)
    return indexed_count


# ---------------------------------------------------------------------------
# Calendar indexer
# ---------------------------------------------------------------------------

def index_calendar(config: dict[str, Any]) -> int:
    """Index calendar events into ChromaDB. Returns number of events indexed."""
    import bt_kitkat

    if not config.get("kitkat_calendar_enabled", True):
        return 0
    if not config.get("calendar_enabled", False):
        log.info("Calendar integration disabled — skipping calendar indexing")
        return 0

    collections = bt_kitkat._get_chroma(config)
    coll = collections["kitkat_calendar"]
    now = datetime.now(timezone.utc).isoformat()

    # Fetch events via bt_calendar
    try:
        import bt_calendar
        calendar_names = bt_calendar.get_available_calendars(config)
        if not calendar_names:
            log.info("No calendars discovered — skipping")
            return 0

        # bt_calendar.get_events is async — run in a fresh event loop
        events = asyncio.run(bt_calendar.get_events(config, calendar_names))
    except Exception as e:
        log.error("Failed to fetch calendar events: %s", e)
        return 0

    # Filter to next 14 days
    today = datetime.now().date()
    cutoff = today + timedelta(days=14)
    upcoming = [
        e for e in events
        if hasattr(e, "start") and e.start.date() <= cutoff
    ]

    # Full refresh: clear collection, re-insert
    try:
        if coll.count() > 0:
            all_ids = coll.get()["ids"]
            coll.delete(ids=all_ids)
    except Exception as e:
        log.warning("Failed to clear calendar collection: %s", e)

    if not upcoming:
        state = _load_state(config)
        state.setdefault("calendar", {})["last_full_index"] = now
        _save_state(config, state)
        return 0

    ids = []
    documents = []
    metadatas = []

    for i, event in enumerate(upcoming):
        date_str = event.start.strftime("%Y-%m-%d")
        start_time = event.start.strftime("%H:%M") if not event.all_day else "all day"
        end_time = event.end.strftime("%H:%M") if not event.all_day else ""

        if event.all_day:
            doc = f"{event.summary} on {date_str} (all day) [{event.calendar_name}]"
        else:
            doc = f"{event.summary} on {date_str} from {start_time} to {end_time} [{event.calendar_name}]"

        # Append location if available
        if hasattr(event, "location") and event.location:
            doc += f" at {event.location}"

        event_id = f"cal_{hashlib.sha256(f'{event.summary}_{date_str}_{i}'.encode()).hexdigest()[:16]}"
        ids.append(event_id)
        documents.append(doc)
        metadatas.append({
            "source": "calendar",
            "calendar_name": event.calendar_name,
            "event_date": date_str,
            "indexed_at": now,
        })

    with bt_kitkat._write_lock:
        coll.add(ids=ids, documents=documents, metadatas=metadatas)

    log.info("Indexed %d calendar events", len(ids))

    state = _load_state(config)
    state.setdefault("calendar", {})["last_full_index"] = now
    _save_state(config, state)
    return len(ids)


# ---------------------------------------------------------------------------
# Top-level functions
# ---------------------------------------------------------------------------

def index_all(config: dict[str, Any]) -> dict[str, int]:
    """Index all sources once and return stats."""
    import bt_kitkat

    log.info("Starting full index cycle")
    obsidian_count = index_obsidian(config)
    gdrive_count = index_gdrive(config)
    calendar_count = index_calendar(config)
    stats = bt_kitkat.get_index_stats(config)
    log.info(
        "Index complete: %d obsidian files, %d gdrive files, %d calendar events. "
        "Total chunks: %s",
        obsidian_count, gdrive_count, calendar_count, stats,
    )
    return stats


def _ensure_data_dir(config: dict[str, Any]) -> None:
    """Create the data directory structure if needed."""
    data_dir = config.get("kitkat_data_dir", "/mnt/external/kitkat")
    os.makedirs(os.path.join(data_dir, "chroma_db"), exist_ok=True)
    os.makedirs(os.path.join(data_dir, "logs"), exist_ok=True)


def run_service() -> None:
    """Main loop when running as systemd service. Blocking."""
    config = load_config()
    _ensure_data_dir(config)

    while True:
        config = load_config()  # Reload each cycle to pick up changes
        try:
            index_all(config)
        except Exception as e:
            log.error("Index cycle error: %s", e)

        interval = config.get("kitkat_index_interval_minutes", 30)
        time.sleep(interval * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _setup_logging(config: dict[str, Any], service_mode: bool = False) -> None:
    """Configure logging for the indexer."""
    log.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
    )

    # Always log to stdout
    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    log.addHandler(sh)

    # In service mode, also log to file
    if service_mode:
        data_dir = config.get("kitkat_data_dir", "/mnt/external/kitkat")
        log_dir = os.path.join(data_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        fh = RotatingFileHandler(
            os.path.join(log_dir, "indexer.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
        )
        fh.setFormatter(formatter)
        log.addHandler(fh)


def main() -> None:
    parser = argparse.ArgumentParser(description="Kitkat Knowledge Indexer")
    parser.add_argument("--source", choices=["obsidian", "gdrive", "calendar"],
                        help="Index only this source")
    parser.add_argument("--stats", action="store_true", help="Print collection counts")
    parser.add_argument("--service", action="store_true", help="Run as continuous service")
    args = parser.parse_args()

    config = load_config()
    _setup_logging(config, service_mode=args.service)

    if not config.get("kitkat_enabled"):
        log.error("Kitkat is not enabled in config.json (kitkat_enabled = false)")
        return

    if args.stats:
        import bt_kitkat
        stats = bt_kitkat.get_index_stats(config)
        for source, count in stats.items():
            print(f"  {source}: {count} chunks")
        return

    if args.service:
        run_service()
        return

    # One-shot index
    _ensure_data_dir(config)
    if args.source == "obsidian":
        index_obsidian(config)
    elif args.source == "gdrive":
        index_gdrive(config)
    elif args.source == "calendar":
        index_calendar(config)
    else:
        index_all(config)


if __name__ == "__main__":
    main()
