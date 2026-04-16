"""Shared SQLite database module for Bluetooth Radar.

Provides the schema, connection helpers, and query functions used by both
the scanner and the web dashboard.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "bt_radar.db"


def get_connection(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and busy timeout."""
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _add_column(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    """Add a column to a table if it doesn't already exist."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        conn.commit()
    except sqlite3.OperationalError:
        pass


def _run_migration(conn: sqlite3.Connection, name: str, sql: str) -> None:
    """Run a SQL statement once, tracked by name in the migrations table."""
    if conn.execute("SELECT 1 FROM migrations WHERE name = ?", (name,)).fetchone():
        return
    conn.execute(sql)
    conn.execute("INSERT INTO migrations (name) VALUES (?)", (name,))


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    """Create tables if they don't exist."""
    conn = get_connection(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS devices (
            mac_address       TEXT PRIMARY KEY,
            advertised_name   TEXT,
            friendly_name     TEXT,
            device_type       TEXT DEFAULT 'Unknown',
            manufacturer      TEXT,
            scan_type         TEXT DEFAULT 'BLE',
            last_rssi         INTEGER,
            is_watchlisted    INTEGER DEFAULT 0,
            is_hidden         INTEGER DEFAULT 0,
            is_paired         INTEGER DEFAULT 0,
            is_notify         INTEGER DEFAULT 0,
            first_seen        REAL,
            last_seen         REAL,
            state             TEXT DEFAULT 'LOST',
            manufacturer_data TEXT,
            service_uuids     TEXT,
            ip_address        TEXT
        );

        CREATE TABLE IF NOT EXISTS events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            mac_address   TEXT NOT NULL,
            event_type    TEXT NOT NULL,
            timestamp     REAL NOT NULL,
            device_name   TEXT,
            device_type   TEXT,
            rssi          INTEGER,
            FOREIGN KEY (mac_address) REFERENCES devices(mac_address)
        );

        CREATE INDEX IF NOT EXISTS idx_events_mac ON events(mac_address);
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_devices_state ON devices(state);
        CREATE INDEX IF NOT EXISTS idx_devices_watchlisted ON devices(is_watchlisted);
    """)
    # Migrations for databases created before these columns existed
    _add_column(conn, "devices", "is_paired", "INTEGER DEFAULT 0")
    _add_column(conn, "devices", "is_notify", "INTEGER DEFAULT 0")
    _add_column(conn, "devices", "ip_address", "TEXT")
    _add_column(conn, "devices", "linked_to", "TEXT")
    _add_column(conn, "devices", "is_welcome", "INTEGER DEFAULT 0")

    # Proximity-triggered Alexa messages
    _add_column(conn, "devices", "proximity_enabled", "INTEGER DEFAULT 0")
    _add_column(conn, "devices", "proximity_rssi_threshold", "INTEGER DEFAULT -70")
    _add_column(conn, "devices", "proximity_interval", "INTEGER DEFAULT 30")
    _add_column(conn, "devices", "proximity_alexa_device", "TEXT")
    _add_column(conn, "devices", "proximity_prompt", "TEXT DEFAULT ''")
    _add_column(conn, "devices", "last_proximity_message", "REAL DEFAULT 0")

    # Calendar integration
    _add_column(conn, "devices", "calendar_calendars", "TEXT DEFAULT ''")

    # News feed integration
    _add_column(conn, "devices", "news_feeds", "TEXT DEFAULT ''")

    # Alexa voice (SSML Polly voice name)
    _add_column(conn, "devices", "alexa_voice", "TEXT DEFAULT ''")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_linked_to ON devices(linked_to)")

    # Chat history for Telegram bot conversations
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id   TEXT NOT NULL,
            role      TEXT NOT NULL,
            content   TEXT NOT NULL,
            timestamp REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chat_history_chat_id
            ON chat_history(chat_id, timestamp DESC);
    """)

    # Echo devices table for Alexa encourage mode
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS echo_devices (
            device_name           TEXT PRIMARY KEY,
            alias                 TEXT,
            encourage_enabled     INTEGER DEFAULT 0,
            encourage_interval    INTEGER DEFAULT 30,
            encourage_prompt      TEXT DEFAULT '',
            encourage_when_playing INTEGER DEFAULT 1,
            last_encouraged       REAL DEFAULT 0
        );
    """)
    _add_column(conn, "echo_devices", "tasks_enabled", "INTEGER DEFAULT 0")
    _add_column(conn, "echo_devices", "tasks_interval", "INTEGER DEFAULT 120")
    _add_column(conn, "echo_devices", "last_tasks_message", "REAL DEFAULT 0")

    # News headlines and per-device read tracking
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS news_headlines (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            guid       TEXT NOT NULL UNIQUE,
            feed_key   TEXT NOT NULL,
            title      TEXT NOT NULL,
            published  REAL NOT NULL,
            fetched_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_news_feed
            ON news_headlines(feed_key, published DESC);

        CREATE TABLE IF NOT EXISTS news_read (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            mac_address  TEXT NOT NULL,
            headline_id  INTEGER NOT NULL,
            read_at      REAL NOT NULL,
            UNIQUE(mac_address, headline_id)
        );
        CREATE INDEX IF NOT EXISTS idx_news_read_mac
            ON news_read(mac_address, headline_id);
    """)

    # Kitkat personal memory agent
    conn.executescript("""
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
    """)

    # One-time data migrations (tracked so they never re-run)
    conn.execute("CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY)")
    _run_migration(conn, "watchlisted_notify_backfill",
                   "UPDATE devices SET is_notify = 1 WHERE is_watchlisted = 1 AND is_notify = 0")
    _run_migration(conn, "rename_state_home_away",
                   "UPDATE devices SET state = CASE WHEN state = 'HOME' THEN 'DETECTED' WHEN state = 'AWAY' THEN 'LOST' ELSE state END")
    conn.commit()

    conn.close()


# ---------------------------------------------------------------------------
# Device operations
# ---------------------------------------------------------------------------

def upsert_device(
    conn: sqlite3.Connection,
    mac: str,
    *,
    advertised_name: str | None = None,
    device_type: str | None = None,
    manufacturer: str | None = None,
    scan_type: str = "BLE",
    rssi: int | None = None,
    state: str | None = None,
    manufacturer_data: dict | None = None,
    service_uuids: list[str] | None = None,
    ip_address: str | None = None,
) -> None:
    """Insert or update a device record."""
    now = time.time()
    mfr_json = json.dumps(manufacturer_data) if manufacturer_data else None
    uuid_json = json.dumps(service_uuids) if service_uuids else None

    conn.execute("""
        INSERT INTO devices (
            mac_address, advertised_name, device_type, manufacturer,
            scan_type, last_rssi, first_seen, last_seen, state,
            manufacturer_data, service_uuids, ip_address
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mac_address) DO UPDATE SET
            advertised_name   = COALESCE(excluded.advertised_name, devices.advertised_name),
            device_type       = CASE WHEN devices.device_type = 'Unknown'
                                          AND excluded.device_type IS NOT NULL
                                          AND excluded.device_type != 'Unknown'
                                     THEN excluded.device_type ELSE devices.device_type END,
            manufacturer      = COALESCE(excluded.manufacturer, devices.manufacturer),
            scan_type         = CASE
                                    WHEN devices.scan_type = excluded.scan_type THEN devices.scan_type
                                    WHEN devices.scan_type LIKE '%' || excluded.scan_type || '%' THEN devices.scan_type
                                    WHEN excluded.scan_type LIKE '%' || devices.scan_type || '%' THEN excluded.scan_type
                                    ELSE devices.scan_type || ', ' || excluded.scan_type
                                END,
            last_rssi         = COALESCE(excluded.last_rssi, devices.last_rssi),
            last_seen         = excluded.last_seen,
            state             = COALESCE(excluded.state, devices.state),
            manufacturer_data = COALESCE(excluded.manufacturer_data, devices.manufacturer_data),
            service_uuids     = COALESCE(excluded.service_uuids, devices.service_uuids),
            ip_address        = COALESCE(excluded.ip_address, devices.ip_address)
    """, (
        mac.upper(), advertised_name, device_type, manufacturer,
        scan_type, rssi, now, now, state or "DETECTED",
        mfr_json, uuid_json, ip_address,
    ))
    conn.commit()


def get_device(conn: sqlite3.Connection, mac: str) -> dict[str, Any] | None:
    """Fetch a single device by MAC address."""
    row = conn.execute(
        "SELECT * FROM devices WHERE mac_address = ?", (mac.upper(),)
    ).fetchone()
    return dict(row) if row else None


def get_all_devices(
    conn: sqlite3.Connection,
    *,
    include_hidden: bool = False,
    state: str | None = None,
    watchlisted_only: bool = False,
    scan_type: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch devices with optional filters."""
    clauses: list[str] = []
    params: list[Any] = []

    if not include_hidden:
        clauses.append("is_hidden = 0")
    if state:
        clauses.append("state = ?")
        params.append(state)
    if watchlisted_only:
        clauses.append("is_watchlisted = 1")
    if scan_type:
        clauses.append("scan_type LIKE ?")
        params.append(f"%{scan_type}%")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM devices {where} ORDER BY last_seen DESC", params
    ).fetchall()
    return [dict(r) for r in rows]


def update_device(
    conn: sqlite3.Connection,
    mac: str,
    *,
    friendly_name: str | None = None,
    device_type: str | None = None,
    is_watchlisted: bool | None = None,
    is_hidden: bool | None = None,
    is_paired: bool | None = None,
    is_notify: bool | None = None,
    is_welcome: bool | None = None,
    state: str | None = None,
    last_seen: float | None = None,
    proximity_enabled: bool | None = None,
    proximity_rssi_threshold: int | None = None,
    proximity_interval: int | None = None,
    proximity_alexa_device: str | None = None,
    proximity_prompt: str | None = None,
    last_proximity_message: float | None = None,
    calendar_calendars: str | None = None,
    news_feeds: str | None = None,
    alexa_voice: str | None = None,
) -> bool:
    """Update specific fields on a device. Returns True if a row was updated."""
    sets: list[str] = []
    params: list[Any] = []

    if friendly_name is not None:
        sets.append("friendly_name = ?")
        params.append(friendly_name)
    if device_type is not None:
        sets.append("device_type = ?")
        params.append(device_type)
    if is_watchlisted is not None:
        sets.append("is_watchlisted = ?")
        params.append(int(is_watchlisted))
    if is_hidden is not None:
        sets.append("is_hidden = ?")
        params.append(int(is_hidden))
    if is_paired is not None:
        sets.append("is_paired = ?")
        params.append(int(is_paired))
    if is_notify is not None:
        sets.append("is_notify = ?")
        params.append(int(is_notify))
    if is_welcome is not None:
        sets.append("is_welcome = ?")
        params.append(int(is_welcome))
    if state is not None:
        sets.append("state = ?")
        params.append(state)
    if last_seen is not None:
        sets.append("last_seen = ?")
        params.append(last_seen)
    if proximity_enabled is not None:
        sets.append("proximity_enabled = ?")
        params.append(int(proximity_enabled))
    if proximity_rssi_threshold is not None:
        sets.append("proximity_rssi_threshold = ?")
        params.append(proximity_rssi_threshold)
    if proximity_interval is not None:
        sets.append("proximity_interval = ?")
        params.append(proximity_interval)
    if proximity_alexa_device is not None:
        sets.append("proximity_alexa_device = ?")
        params.append(proximity_alexa_device or None)
    if proximity_prompt is not None:
        sets.append("proximity_prompt = ?")
        params.append(proximity_prompt)
    if last_proximity_message is not None:
        sets.append("last_proximity_message = ?")
        params.append(last_proximity_message)
    if calendar_calendars is not None:
        sets.append("calendar_calendars = ?")
        params.append(calendar_calendars)
    if news_feeds is not None:
        sets.append("news_feeds = ?")
        params.append(news_feeds)
    if alexa_voice is not None:
        sets.append("alexa_voice = ?")
        params.append(alexa_voice)

    if not sets:
        return False

    params.append(mac.upper())
    cur = conn.execute(
        f"UPDATE devices SET {', '.join(sets)} WHERE mac_address = ?", params
    )
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Device linking
# ---------------------------------------------------------------------------

def link_device(conn: sqlite3.Connection, secondary_mac: str, primary_mac: str) -> bool:
    """Link a secondary device to a primary device.

    Resolves chains: if the target is itself a secondary, resolves to the root.
    Re-points any existing dependents of the secondary to the resolved primary.
    Returns True if the link was created.
    """
    secondary_mac = secondary_mac.upper()
    primary_mac = primary_mac.upper()

    if secondary_mac == primary_mac:
        return False

    # Resolve the target to the root primary (follow linked_to chain)
    root = primary_mac
    seen: set[str] = {root}
    while True:
        row = conn.execute(
            "SELECT linked_to FROM devices WHERE mac_address = ?", (root,)
        ).fetchone()
        if not row or not row["linked_to"]:
            break
        root = row["linked_to"]
        if root in seen:
            break  # cycle guard
        seen.add(root)

    if secondary_mac == root:
        return False  # would create a self-link

    # Re-point any devices currently linked to secondary_mac to the root
    conn.execute(
        "UPDATE devices SET linked_to = ? WHERE linked_to = ?",
        (root, secondary_mac),
    )
    # Set the secondary's linked_to
    conn.execute(
        "UPDATE devices SET linked_to = ? WHERE mac_address = ?",
        (root, secondary_mac),
    )
    conn.commit()
    return True


def unlink_device(conn: sqlite3.Connection, mac: str) -> bool:
    """Remove the link for a device (set linked_to = NULL).

    Returns True if the device was updated.
    """
    mac = mac.upper()
    cur = conn.execute(
        "UPDATE devices SET linked_to = NULL WHERE mac_address = ? AND linked_to IS NOT NULL",
        (mac,),
    )
    conn.commit()
    return cur.rowcount > 0


def get_link_group(conn: sqlite3.Connection, mac: str) -> dict[str, Any]:
    """Return the link group for a given MAC.

    Returns dict with:
      - primary: the primary device dict
      - secondaries: list of secondary device dicts
    """
    mac = mac.upper()
    dev = get_device(conn, mac)
    if not dev:
        return {"primary": None, "secondaries": []}

    # Find the primary (root)
    primary_mac = mac
    if dev["linked_to"]:
        primary_mac = dev["linked_to"]

    primary = get_device(conn, primary_mac)
    if not primary:
        primary = dev
        primary_mac = mac

    # Find all secondaries
    rows = conn.execute(
        "SELECT * FROM devices WHERE linked_to = ?", (primary_mac,)
    ).fetchall()
    secondaries = [dict(r) for r in rows]

    return {"primary": primary, "secondaries": secondaries}


def get_all_devices_merged(
    conn: sqlite3.Connection,
    *,
    include_hidden: bool = False,
    state: str | None = None,
    watchlisted_only: bool = False,
    scan_type: str | None = None,
) -> list[dict[str, Any]]:
    """Like get_all_devices but merges linked secondaries into their primary.

    Each primary row gets:
      - state = DETECTED if any member is DETECTED
      - last_seen = max across group
      - scan_type = joined unique types (e.g. "BLE, WiFi")
      - linked_devices = list of secondary device dicts
    Secondary devices are excluded from the top-level list.
    """
    all_devs = get_all_devices(
        conn, include_hidden=include_hidden,
        state=None,  # fetch all states, filter after merging
        watchlisted_only=watchlisted_only,
        scan_type=None,  # filter after merging
    )

    # Index by MAC
    by_mac: dict[str, dict[str, Any]] = {d["mac_address"]: d for d in all_devs}

    # Group secondaries under their primary
    primaries: dict[str, list[dict[str, Any]]] = {}  # primary_mac -> [secondaries]
    secondary_macs: set[str] = set()

    for d in all_devs:
        linked = d.get("linked_to")
        if linked and linked in by_mac:
            secondary_macs.add(d["mac_address"])
            primaries.setdefault(linked, []).append(d)

    # Build the merged list
    result: list[dict[str, Any]] = []
    for d in all_devs:
        mac = d["mac_address"]
        if mac in secondary_macs:
            continue  # skip secondaries

        merged = dict(d)
        secs = primaries.get(mac, [])
        merged["linked_devices"] = secs

        if secs:
            # Merge state: DETECTED if any member is DETECTED
            all_members = [d] + secs
            if any(m["state"] == "DETECTED" for m in all_members):
                merged["state"] = "DETECTED"

            # Merge last_seen: max across group
            last_seens = [m["last_seen"] for m in all_members if m["last_seen"]]
            if last_seens:
                merged["last_seen"] = max(last_seens)

            # Merge scan_type: join unique types
            scan_types: list[str] = []
            for m in all_members:
                if m["scan_type"]:
                    for st in m["scan_type"].split(", "):
                        st = st.strip()
                        if st and st not in scan_types:
                            scan_types.append(st)
            merged["scan_type"] = ", ".join(scan_types) if scan_types else d["scan_type"]

        # Apply filters that couldn't be applied pre-merge
        if state and merged["state"] != state:
            continue
        if scan_type and scan_type not in (merged.get("scan_type") or ""):
            continue

        result.append(merged)

    return result


# ---------------------------------------------------------------------------
# Event operations
# ---------------------------------------------------------------------------

def record_event(
    conn: sqlite3.Connection,
    mac: str,
    event_type: str,
    *,
    device_name: str | None = None,
    device_type: str | None = None,
    rssi: int | None = None,
) -> None:
    """Insert an arrival or departure event."""
    conn.execute("""
        INSERT INTO events (mac_address, event_type, timestamp, device_name, device_type, rssi)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (mac.upper(), event_type, time.time(), device_name, device_type, rssi))
    conn.commit()


def get_events(
    conn: sqlite3.Connection,
    *,
    mac: str | None = None,
    event_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Fetch events with optional filters, newest first."""
    clauses: list[str] = []
    params: list[Any] = []

    if mac:
        clauses.append("e.mac_address = ?")
        params.append(mac.upper())
    if event_type:
        clauses.append("e.event_type = ?")
        params.append(event_type)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])

    rows = conn.execute(f"""
        SELECT e.*, d.friendly_name, d.advertised_name as d_adv_name
        FROM events e
        LEFT JOIN devices d ON e.mac_address = d.mac_address
        {where}
        ORDER BY e.timestamp DESC
        LIMIT ? OFFSET ?
    """, params).fetchall()
    return [dict(r) for r in rows]


def count_events(
    conn: sqlite3.Connection,
    *,
    mac: str | None = None,
    event_type: str | None = None,
) -> int:
    """Count events with optional filters."""
    clauses: list[str] = []
    params: list[Any] = []

    if mac:
        clauses.append("mac_address = ?")
        params.append(mac.upper())
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    row = conn.execute(
        f"SELECT COUNT(*) as cnt FROM events {where}", params
    ).fetchone()
    return row["cnt"]


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def get_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return summary statistics for the dashboard (excludes linked secondaries)."""
    total = conn.execute(
        "SELECT COUNT(*) as c FROM devices WHERE is_hidden = 0 AND linked_to IS NULL"
    ).fetchone()["c"]
    home = conn.execute(
        "SELECT COUNT(*) as c FROM devices WHERE state = 'DETECTED' AND is_hidden = 0 AND linked_to IS NULL"
    ).fetchone()["c"]
    watchlisted = conn.execute(
        "SELECT COUNT(*) as c FROM devices WHERE is_watchlisted = 1 AND linked_to IS NULL"
    ).fetchone()["c"]
    events_today_start = time.time() - (time.time() % 86400)  # midnight UTC approx
    events_today = conn.execute(
        "SELECT COUNT(*) as c FROM events WHERE timestamp >= ?", (events_today_start,)
    ).fetchone()["c"]
    return {
        "total_devices": total,
        "home_devices": home,
        "away_devices": total - home,
        "watchlisted_devices": watchlisted,
        "events_today": events_today,
    }


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def hide_stale_random_macs(conn: sqlite3.Connection, hours: int = 24) -> int:
    """Hide devices with random MACs not seen in the given hours.

    A random MAC has the locally administered bit set (second hex digit
    is one of 2, 3, 6, 7, A, B, E, F).
    """
    cutoff = time.time() - (hours * 3600)
    cur = conn.execute("""
        UPDATE devices SET is_hidden = 1
        WHERE is_hidden = 0
          AND is_watchlisted = 0
          AND last_seen < ?
          AND SUBSTR(mac_address, 2, 1) IN ('2','3','6','7','A','B','E','F','a','b','e','f')
    """, (cutoff,))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Chat history (Telegram bot)
# ---------------------------------------------------------------------------

def save_chat_message(
    conn: sqlite3.Connection, chat_id: str, role: str, content: str
) -> None:
    """Save a chat message to history."""
    conn.execute(
        "INSERT INTO chat_history (chat_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (chat_id, role, content, time.time()),
    )
    conn.commit()


def get_chat_history(
    conn: sqlite3.Connection, chat_id: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Return recent chat messages for a chat, oldest first."""
    rows = conn.execute(
        "SELECT role, content, timestamp FROM chat_history "
        "WHERE chat_id = ? ORDER BY timestamp DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def cleanup_chat_history(conn: sqlite3.Connection, max_age_days: int = 7) -> int:
    """Delete chat history entries older than max_age_days."""
    cutoff = time.time() - (max_age_days * 86400)
    cur = conn.execute("DELETE FROM chat_history WHERE timestamp < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


def clear_chat_history(conn: sqlite3.Connection, chat_id: str) -> int:
    """Delete all chat history for a specific chat_id. Returns count deleted."""
    cur = conn.execute("DELETE FROM chat_history WHERE chat_id = ?", (chat_id,))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Echo devices (Alexa encourage mode)
# ---------------------------------------------------------------------------

def upsert_echo_device(
    conn: sqlite3.Connection,
    device_name: str,
    *,
    alias: str | None = None,
    encourage_enabled: bool | None = None,
    encourage_interval: int | None = None,
    encourage_prompt: str | None = None,
    encourage_when_playing: bool | None = None,
    tasks_enabled: bool | None = None,
    tasks_interval: int | None = None,
) -> None:
    """Insert or update an Echo device record."""
    conn.execute("""
        INSERT INTO echo_devices (device_name, alias)
        VALUES (?, ?)
        ON CONFLICT(device_name) DO NOTHING
    """, (device_name, alias))

    sets: list[str] = []
    params: list[Any] = []
    if alias is not None:
        sets.append("alias = ?")
        params.append(alias)
    if encourage_enabled is not None:
        sets.append("encourage_enabled = ?")
        params.append(int(encourage_enabled))
    if encourage_interval is not None:
        sets.append("encourage_interval = ?")
        params.append(encourage_interval)
    if encourage_prompt is not None:
        sets.append("encourage_prompt = ?")
        params.append(encourage_prompt)
    if encourage_when_playing is not None:
        sets.append("encourage_when_playing = ?")
        params.append(int(encourage_when_playing))
    if tasks_enabled is not None:
        sets.append("tasks_enabled = ?")
        params.append(int(tasks_enabled))
    if tasks_interval is not None:
        sets.append("tasks_interval = ?")
        params.append(tasks_interval)

    if sets:
        params.append(device_name)
        conn.execute(
            f"UPDATE echo_devices SET {', '.join(sets)} WHERE device_name = ?",
            params,
        )
    conn.commit()


def get_echo_device(conn: sqlite3.Connection, device_name: str) -> dict[str, Any] | None:
    """Fetch a single Echo device by name."""
    row = conn.execute(
        "SELECT * FROM echo_devices WHERE device_name = ?", (device_name,)
    ).fetchone()
    return dict(row) if row else None


def get_all_echo_devices(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Fetch all Echo devices."""
    rows = conn.execute(
        "SELECT * FROM echo_devices ORDER BY device_name"
    ).fetchall()
    return [dict(r) for r in rows]


def get_enabled_echo_devices(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Fetch Echo devices with encourage mode enabled."""
    rows = conn.execute(
        "SELECT * FROM echo_devices WHERE encourage_enabled = 1"
    ).fetchall()
    return [dict(r) for r in rows]


def get_task_reminder_echo_devices(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Fetch Echo devices with task reminder mode enabled."""
    rows = conn.execute(
        "SELECT * FROM echo_devices WHERE tasks_enabled = 1"
    ).fetchall()
    return [dict(r) for r in rows]


def update_echo_last_encouraged(
    conn: sqlite3.Connection, device_name: str, timestamp: float,
) -> None:
    """Update the last_encouraged timestamp for an Echo device."""
    conn.execute(
        "UPDATE echo_devices SET last_encouraged = ? WHERE device_name = ?",
        (timestamp, device_name),
    )
    conn.commit()


def update_echo_last_tasks_message(
    conn: sqlite3.Connection, device_name: str, timestamp: float,
) -> None:
    """Update the last_tasks_message timestamp for an Echo device."""
    conn.execute(
        "UPDATE echo_devices SET last_tasks_message = ? WHERE device_name = ?",
        (timestamp, device_name),
    )
    conn.commit()


def delete_echo_device(conn: sqlite3.Connection, device_name: str) -> bool:
    """Delete an Echo device. Returns True if a row was deleted."""
    cur = conn.execute(
        "DELETE FROM echo_devices WHERE device_name = ?", (device_name,)
    )
    conn.commit()
    return cur.rowcount > 0


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
