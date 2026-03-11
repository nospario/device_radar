"""DNS Traffic Monitoring via Pi-hole — ingestion, alerting, and query functions.

Polls Pi-hole for DNS queries, maps client IPs to Device Radar devices via ARP,
normalises domains, and provides dwell-time-based browsing alerts.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

try:
    import tldextract
except ImportError:
    tldextract = None  # type: ignore[assignment]

import bt_alexa
import bt_db

logger = logging.getLogger("bt_dns")

# In-memory ARP cache: {ip: mac}
_arp_cache: dict[str, str] = {}
_arp_cache_time: float = 0.0
_ARP_CACHE_TTL = 300  # seconds

# Track last ingested Pi-hole timestamp to avoid re-processing
_last_pihole_ts: float = 0.0

# tldextract instance (cached to avoid repeated public suffix list downloads)
_tld_extractor: Any = None


# ---------------------------------------------------------------------------
# ARP-based IP → MAC resolution
# ---------------------------------------------------------------------------

def _refresh_arp_cache() -> dict[str, str]:
    """Read /proc/net/arp and build an IP→MAC mapping."""
    global _arp_cache, _arp_cache_time
    now = time.time()
    if _arp_cache and (now - _arp_cache_time) < _ARP_CACHE_TTL:
        return _arp_cache

    result: dict[str, str] = {}
    try:
        arp_path = Path("/proc/net/arp")
        if arp_path.exists():
            for line in arp_path.read_text().splitlines()[1:]:  # skip header
                parts = line.split()
                if len(parts) >= 4:
                    ip = parts[0]
                    flags = parts[2]
                    mac = parts[3].upper()
                    # flags 0x2 = complete entry
                    if flags != "0x0" and mac != "00:00:00:00:00:00":
                        result[ip] = mac
    except Exception:
        logger.error("Failed to read ARP table", exc_info=True)

    _arp_cache = result
    _arp_cache_time = now
    return result


def _resolve_ip_to_mac(ip: str) -> str | None:
    """Resolve a Pi-hole client IP to a MAC address via ARP."""
    cache = _refresh_arp_cache()
    return cache.get(ip)


def _resolve_mac_to_device(
    conn: sqlite3.Connection, mac: str,
) -> str | None:
    """Resolve a MAC to a device, following link groups to the primary.

    Returns the primary device's mac_address, or the mac itself if it's
    standalone. Returns None if the device is not found or not opted in.
    """
    dev = bt_db.get_device(conn, mac)
    if not dev:
        return None
    # If this is a secondary, resolve to primary first
    if dev.get("linked_to"):
        primary = bt_db.get_device(conn, dev["linked_to"])
        if primary and primary.get("dns_tracking_enabled"):
            return primary["mac_address"]
        # Primary not tracked — fall through to check this device
    if not dev.get("dns_tracking_enabled"):
        return None
    return mac


# ---------------------------------------------------------------------------
# Domain normalisation & categories
# ---------------------------------------------------------------------------

def _normalise_domain(full_domain: str) -> str:
    """Extract the root/registered domain from a full domain."""
    global _tld_extractor
    if tldextract is None:
        # Fallback: simple split (less accurate for .co.uk etc.)
        parts = full_domain.rstrip(".").split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return full_domain

    if _tld_extractor is None:
        _tld_extractor = tldextract.TLDExtract(
            cache_dir="/tmp/tldextract_cache",
            include_psl_private_domains=False,
        )
    ext = _tld_extractor(full_domain)
    if ext.registered_domain:
        return ext.registered_domain
    return full_domain


def get_category(conn: sqlite3.Connection, root_domain: str) -> str | None:
    """Look up a domain's category from the domain_categories table."""
    row = conn.execute(
        "SELECT category FROM domain_categories WHERE root_domain = ?",
        (root_domain,),
    ).fetchone()
    return row["category"] if row else None


# ---------------------------------------------------------------------------
# Pi-hole data fetching
# ---------------------------------------------------------------------------

def _poll_pihole_api(api_url: str, since_timestamp: float) -> list[dict[str, Any]]:
    """Fetch recent queries from Pi-hole's API."""
    queries: list[dict[str, Any]] = []
    try:
        url = f"{api_url}?getAllQueries&from={int(since_timestamp)}"
        resp = httpx.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("data", [])
        for row in raw:
            if len(row) >= 6:
                queries.append({
                    "timestamp": float(row[0]),
                    "query_type": row[1],
                    "domain": row[2],
                    "client": row[3],
                    "status": _map_pihole_status(row[4]) if len(row) > 4 else "unknown",
                    "upstream": row[5] if len(row) > 5 else None,
                })
    except Exception:
        logger.error("Failed to poll Pi-hole API", exc_info=True)
    return queries


def _poll_pihole_db(db_path: str, since_timestamp: float) -> list[dict[str, Any]]:
    """Fetch recent queries directly from Pi-hole's FTL database."""
    queries: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT timestamp, type, domain, client, status, forward "
            "FROM queries WHERE timestamp > ? ORDER BY timestamp ASC",
            (int(since_timestamp),),
        ).fetchall()
        for row in rows:
            queries.append({
                "timestamp": float(row["timestamp"]),
                "query_type": _map_pihole_query_type(row["type"]),
                "domain": row["domain"],
                "client": row["client"],
                "status": _map_pihole_status(row["status"]),
                "upstream": row["forward"],
            })
        conn.close()
    except Exception:
        logger.error("Failed to poll Pi-hole FTL database", exc_info=True)
    return queries


def _map_pihole_status(status: Any) -> str:
    """Map Pi-hole numeric status to a readable string."""
    status_map = {
        "1": "blocked", "2": "forwarded", "3": "cached",
        "4": "blocked", "5": "blocked",
        1: "blocked", 2: "forwarded", 3: "cached",
        4: "blocked", 5: "blocked",
    }
    return status_map.get(status, str(status))


def _map_pihole_query_type(qtype: Any) -> str:
    """Map Pi-hole numeric query type to a string."""
    type_map = {1: "A", 2: "AAAA", 3: "ANY", 4: "SRV", 5: "SOA",
                6: "PTR", 7: "TXT", 8: "NAPTR", 9: "MX",
                10: "DS", 11: "RRSIG", 12: "DNSKEY", 13: "NS",
                14: "OTHER", 15: "SVCB", 16: "HTTPS"}
    return type_map.get(qtype, str(qtype))


# ---------------------------------------------------------------------------
# Ingestion loop
# ---------------------------------------------------------------------------

async def dns_ingestion_loop(config: dict[str, Any], db_path: Path) -> None:
    """Background loop that polls Pi-hole and stores DNS queries."""
    global _last_pihole_ts

    interval = config.get("dns_poll_interval_seconds", 30)
    api_url = config.get("pihole_api_url", "http://localhost/admin/api.php")
    ftl_path = config.get("pihole_ftl_db_path", "/etc/pihole/pihole-FTL.db")

    logger.info("DNS ingestion loop started (poll every %ds)", interval)

    # Seed last timestamp from most recent stored query
    try:
        conn = bt_db.get_connection(db_path)
        row = conn.execute(
            "SELECT MAX(timestamp) as max_ts FROM dns_queries"
        ).fetchone()
        if row and row["max_ts"]:
            _last_pihole_ts = row["max_ts"]
        conn.close()
    except Exception:
        pass

    if _last_pihole_ts == 0:
        _last_pihole_ts = time.time() - 60  # start from 1 minute ago

    while True:
        try:
            await asyncio.sleep(interval)

            # Try API first, fall back to direct DB
            queries = _poll_pihole_api(api_url, _last_pihole_ts)
            if not queries:
                queries = _poll_pihole_db(ftl_path, _last_pihole_ts)

            if not queries:
                continue

            conn = bt_db.get_connection(db_path)
            try:
                inserted = _ingest_queries(conn, queries)
                if inserted:
                    logger.debug("Ingested %d DNS queries", inserted)
            finally:
                conn.close()

            # Update last timestamp
            max_ts = max(q["timestamp"] for q in queries)
            if max_ts > _last_pihole_ts:
                _last_pihole_ts = max_ts

        except Exception:
            logger.error("Error in DNS ingestion loop", exc_info=True)


def _ingest_queries(
    conn: sqlite3.Connection, queries: list[dict[str, Any]],
) -> int:
    """Process and store a batch of Pi-hole queries. Returns count inserted."""
    count = 0
    for q in queries:
        client_ip = q["client"]
        mac = _resolve_ip_to_mac(client_ip)
        if not mac:
            continue

        device_mac = _resolve_mac_to_device(conn, mac)
        if not device_mac:
            continue

        domain = q["domain"]
        root_domain = _normalise_domain(domain)
        category = get_category(conn, root_domain)

        try:
            conn.execute("""
                INSERT INTO dns_queries
                    (timestamp, device_mac, client_ip, full_domain, root_domain,
                     query_type, status, category, upstream)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                q["timestamp"], device_mac, client_ip, domain, root_domain,
                q.get("query_type", "A"), q.get("status", "forwarded"),
                category, q.get("upstream"),
            ))
            count += 1

            # Update daily stats incrementally
            _update_daily_stats(conn, q["timestamp"], device_mac, root_domain, category)
        except sqlite3.IntegrityError:
            pass  # duplicate

    if count:
        conn.commit()
    return count


def _update_daily_stats(
    conn: sqlite3.Connection, timestamp: float,
    device_mac: str, root_domain: str, category: str | None,
) -> None:
    """Incrementally update the dns_daily_stats aggregation table."""
    date_str = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
    conn.execute("""
        INSERT INTO dns_daily_stats (date, device_mac, root_domain, category,
                                     query_count, first_seen, last_seen)
        VALUES (?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(date, device_mac, root_domain) DO UPDATE SET
            query_count = query_count + 1,
            first_seen = MIN(first_seen, excluded.first_seen),
            last_seen = MAX(last_seen, excluded.last_seen),
            category = COALESCE(excluded.category, dns_daily_stats.category)
    """, (date_str, device_mac, root_domain, category, timestamp, timestamp))


# ---------------------------------------------------------------------------
# Alert engine
# ---------------------------------------------------------------------------

async def dns_alert_loop(config: dict[str, Any], db_path: Path) -> None:
    """Background loop that checks browsing alerts and fires notifications."""
    interval = config.get("alert_check_interval_seconds", 60)
    gap_minutes = config.get("alert_session_gap_minutes", 3)

    logger.info("DNS alert loop started (check every %ds)", interval)

    while True:
        try:
            await asyncio.sleep(interval)

            conn = bt_db.get_connection(db_path)
            try:
                alerts = conn.execute(
                    "SELECT * FROM website_alerts WHERE is_active = 1"
                ).fetchall()
                alerts = [dict(a) for a in alerts]
            finally:
                conn.close()

            if not alerts:
                continue

            now = time.time()

            for alert in alerts:
                try:
                    conn = bt_db.get_connection(db_path)
                    try:
                        session_mins = _detect_active_session(
                            conn, alert["device_mac"], alert["domain"],
                            gap_minutes, alert["threshold_minutes"],
                        )
                    finally:
                        conn.close()

                    if session_mins is None:
                        continue

                    # Check cooldown
                    last_triggered = alert.get("last_triggered") or 0
                    cooldown_secs = alert["cooldown_minutes"] * 60
                    if now - last_triggered < cooldown_secs:
                        continue

                    # Fire the alert
                    await _fire_alert(alert, session_mins, config, db_path)

                except Exception:
                    logger.error(
                        "Error checking alert %d for %s",
                        alert["id"], alert["domain"], exc_info=True,
                    )

        except Exception:
            logger.error("Error in DNS alert loop", exc_info=True)


def _detect_active_session(
    conn: sqlite3.Connection,
    device_mac: str,
    domain: str,
    gap_minutes: int,
    threshold_minutes: int,
) -> float | None:
    """Check for an active browsing session meeting the threshold.

    Returns the session duration in minutes if threshold is met, or None.
    """
    window_minutes = threshold_minutes + gap_minutes
    cutoff = time.time() - (window_minutes * 60)

    rows = conn.execute("""
        SELECT timestamp FROM dns_queries
        WHERE device_mac = ? AND root_domain = ? AND timestamp > ?
        ORDER BY timestamp ASC
    """, (device_mac, domain, cutoff)).fetchall()

    if not rows:
        return None

    # Walk through queries tracking the most recent session
    session_start = rows[0]["timestamp"]
    prev_ts = session_start

    for row in rows[1:]:
        ts = row["timestamp"]
        if ts - prev_ts > gap_minutes * 60:
            # Gap too large — new session starts
            session_start = ts
        prev_ts = ts

    # Only care about the most recent session (the one that ends at prev_ts)
    session_duration_mins = (prev_ts - session_start) / 60

    if session_duration_mins >= threshold_minutes:
        return session_duration_mins
    return None


async def _fire_alert(
    alert: dict[str, Any],
    session_mins: float,
    config: dict[str, Any],
    db_path: Path,
) -> None:
    """Fire a browsing alert — generate message and deliver via configured channel."""
    domain = alert["domain"]
    device_mac = alert["device_mac"]
    minutes = int(session_mins)
    now = time.time()

    # Resolve person name
    conn = bt_db.get_connection(db_path)
    try:
        device = bt_db.get_device(conn, device_mac)
    finally:
        conn.close()

    device_name = ""
    if device:
        device_name = device.get("friendly_name") or device.get("advertised_name") or device_mac
    person_name = bt_alexa._resolve_person_name(device_name, config) if device_name else "Someone"

    # Generate message
    if alert.get("use_ollama"):
        message = await _generate_alert_message(person_name, domain, minutes, config)
    else:
        message = (alert.get("custom_message") or "Stop browsing {domain}!").format(
            domain=domain, minutes=minutes,
        )

    if not message:
        message = f"{person_name}, you've been on {domain} for {minutes} minutes. Time to refocus!"

    logger.info("Firing alert for %s on %s (%d mins): %s", device_mac, domain, minutes, message)

    # Deliver via configured channel
    alert_type = alert.get("alert_type", "alexa")
    delivered = False

    if alert_type in ("alexa", "all"):
        echo_device = None
        voice = None
        if device:
            echo_device = device.get("proximity_alexa_device") or config.get("alexa_device_name")
            voice = device.get("alexa_voice") or None
        try:
            success = await bt_alexa.speak(message, config, device=echo_device, voice=voice)
            if success:
                delivered = True
        except Exception:
            logger.error("Failed to speak alert on Alexa", exc_info=True)

    if alert_type in ("telegram", "all"):
        try:
            import bt_telegram
            await bt_telegram.send_notification(
                f"{person_name}: {domain}", f"browsing alert — {message}",
            )
            delivered = True
        except Exception:
            logger.error("Failed to send alert via Telegram", exc_info=True)

    if alert_type in ("ntfy", "all"):
        try:
            ntfy_url = config.get("ntfy_url")
            if ntfy_url:
                async with httpx.AsyncClient() as client:
                    await client.post(ntfy_url, content=message.encode(), timeout=10)
                delivered = True
        except Exception:
            logger.error("Failed to send alert via ntfy", exc_info=True)

    # Log to alert_history and update last_triggered
    conn = bt_db.get_connection(db_path)
    try:
        conn.execute("""
            INSERT INTO alert_history
                (alert_id, device_mac, domain, triggered_at, message, alert_type, browsing_duration_mins)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (alert["id"], device_mac, domain, now, message, alert_type, session_mins))

        conn.execute(
            "UPDATE website_alerts SET last_triggered = ? WHERE id = ?",
            (now, alert["id"]),
        )
        conn.commit()
    finally:
        conn.close()


async def _generate_alert_message(
    person_name: str, domain: str, minutes: int, config: dict[str, Any],
) -> str | None:
    """Generate a browsing alert message via Ollama."""
    base_url = config.get("ollama_url", "http://localhost:11434")
    timeout = config.get("ollama_timeout_seconds", 15)
    model = config.get("ollama_model", "qwen2.5:1.5b")

    prompt = (
        f"Generate a short, witty, slightly sarcastic message (1-2 sentences max) "
        f"telling {person_name} to stop browsing {domain} after {minutes} minutes. "
        f"Be creative and vary the tone — sometimes motivational, sometimes funny, "
        f"sometimes gently mocking. Don't be mean. Keep it under 30 words. "
        f"Do not use emoji, hashtags, or quotation marks. "
        f"Just output the message, nothing else."
    )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
                timeout=timeout,
            )
            resp.raise_for_status()
            msg = resp.json().get("response", "").strip()
            return msg.strip('"').strip("'") if msg else None
    except Exception:
        logger.error("Ollama alert message generation failed", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Query functions (for API endpoints)
# ---------------------------------------------------------------------------

def get_dns_queries(
    conn: sqlite3.Connection,
    *,
    device_mac: str | None = None,
    domain: str | None = None,
    category: str | None = None,
    status: str | None = None,
    from_ts: float | None = None,
    to_ts: float | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch paginated DNS queries with filters. Returns (queries, total_count)."""
    clauses: list[str] = []
    params: list[Any] = []

    if device_mac:
        clauses.append("q.device_mac = ?")
        params.append(device_mac.upper())
    if domain:
        clauses.append("(q.root_domain LIKE ? OR q.full_domain LIKE ?)")
        params.extend([f"%{domain}%", f"%{domain}%"])
    if category:
        clauses.append("q.category = ?")
        params.append(category)
    if status:
        clauses.append("q.status = ?")
        params.append(status)
    if from_ts:
        clauses.append("q.timestamp >= ?")
        params.append(from_ts)
    if to_ts:
        clauses.append("q.timestamp <= ?")
        params.append(to_ts)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    # Count total
    count_params = list(params)
    total = conn.execute(
        f"SELECT COUNT(*) as cnt FROM dns_queries q {where}", count_params,
    ).fetchone()["cnt"]

    # Fetch page
    params.extend([limit, offset])
    rows = conn.execute(f"""
        SELECT q.*, d.friendly_name, d.advertised_name
        FROM dns_queries q
        LEFT JOIN devices d ON q.device_mac = d.mac_address
        {where}
        ORDER BY q.timestamp DESC
        LIMIT ? OFFSET ?
    """, params).fetchall()

    return [dict(r) for r in rows], total


def get_traffic_stats(
    conn: sqlite3.Connection,
    *,
    device_mac: str | None = None,
    from_ts: float | None = None,
    to_ts: float | None = None,
) -> dict[str, Any]:
    """Get summary statistics for the traffic page."""
    clauses: list[str] = []
    params: list[Any] = []

    if device_mac:
        clauses.append("device_mac = ?")
        params.append(device_mac.upper())
    if from_ts:
        clauses.append("timestamp >= ?")
        params.append(from_ts)
    if to_ts:
        clauses.append("timestamp <= ?")
        params.append(to_ts)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    # Total queries
    total = conn.execute(
        f"SELECT COUNT(*) as cnt FROM dns_queries {where}", params,
    ).fetchone()["cnt"]

    # Unique domains
    unique = conn.execute(
        f"SELECT COUNT(DISTINCT root_domain) as cnt FROM dns_queries {where}", list(params),
    ).fetchone()["cnt"]

    # Most active device
    top_device = conn.execute(f"""
        SELECT device_mac, COUNT(*) as cnt FROM dns_queries {where}
        GROUP BY device_mac ORDER BY cnt DESC LIMIT 1
    """, list(params)).fetchone()

    top_device_name = None
    top_device_mac = None
    if top_device:
        top_device_mac = top_device["device_mac"]
        dev = bt_db.get_device(conn, top_device_mac)
        if dev:
            top_device_name = dev.get("friendly_name") or dev.get("advertised_name") or top_device_mac

    # Top domain
    top_domain = conn.execute(f"""
        SELECT root_domain, COUNT(*) as cnt FROM dns_queries {where}
        GROUP BY root_domain ORDER BY cnt DESC LIMIT 1
    """, list(params)).fetchone()

    # Blocked count
    blocked_params = list(params)
    blocked_where = f"WHERE status = 'blocked'"
    if clauses:
        blocked_where = f"WHERE {' AND '.join(clauses)} AND status = 'blocked'"
    blocked = conn.execute(
        f"SELECT COUNT(*) as cnt FROM dns_queries {blocked_where}", blocked_params,
    ).fetchone()["cnt"]

    # Active alerts
    active_alerts = conn.execute(
        "SELECT COUNT(*) as cnt FROM website_alerts WHERE is_active = 1"
    ).fetchone()["cnt"]

    return {
        "total_queries": total,
        "unique_domains": unique,
        "most_active_device": top_device_name,
        "most_active_device_mac": top_device_mac,
        "top_domain": top_domain["root_domain"] if top_domain else None,
        "top_domain_count": top_domain["cnt"] if top_domain else 0,
        "blocked_count": blocked,
        "active_alerts": active_alerts,
    }


def get_top_domains(
    conn: sqlite3.Connection,
    *,
    device_mac: str | None = None,
    from_ts: float | None = None,
    to_ts: float | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Get top N domains by query count for chart data."""
    clauses: list[str] = []
    params: list[Any] = []

    if device_mac:
        clauses.append("device_mac = ?")
        params.append(device_mac.upper())
    if from_ts:
        clauses.append("timestamp >= ?")
        params.append(from_ts)
    if to_ts:
        clauses.append("timestamp <= ?")
        params.append(to_ts)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    rows = conn.execute(f"""
        SELECT root_domain, category, COUNT(*) as count
        FROM dns_queries {where}
        GROUP BY root_domain
        ORDER BY count DESC
        LIMIT ?
    """, params).fetchall()

    return [dict(r) for r in rows]


def export_queries_csv(
    conn: sqlite3.Connection,
    *,
    device_mac: str | None = None,
    domain: str | None = None,
    category: str | None = None,
    status: str | None = None,
    from_ts: float | None = None,
    to_ts: float | None = None,
) -> str:
    """Export filtered queries as CSV string."""
    queries, _ = get_dns_queries(
        conn, device_mac=device_mac, domain=domain, category=category,
        status=status, from_ts=from_ts, to_ts=to_ts,
        limit=10000, offset=0,
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Time", "Device", "Domain", "Root Domain", "Category",
                     "Type", "Status", "Upstream"])
    for q in queries:
        writer.writerow([
            datetime.fromtimestamp(q["timestamp"]).strftime("%Y-%m-%d %H:%M:%S"),
            q.get("friendly_name") or q.get("advertised_name") or q.get("device_mac") or "Unknown",
            q["full_domain"],
            q["root_domain"],
            q.get("category") or "Uncategorised",
            q.get("query_type") or "",
            q.get("status") or "",
            q.get("upstream") or "",
        ])
    return output.getvalue()


# ---------------------------------------------------------------------------
# Alert CRUD
# ---------------------------------------------------------------------------

def get_device_alerts(
    conn: sqlite3.Connection, device_mac: str,
) -> list[dict[str, Any]]:
    """Get all alerts for a device."""
    rows = conn.execute(
        "SELECT * FROM website_alerts WHERE device_mac = ? ORDER BY created_at DESC",
        (device_mac.upper(),),
    ).fetchall()
    return [dict(r) for r in rows]


def create_alert(
    conn: sqlite3.Connection, device_mac: str, **kwargs: Any,
) -> int:
    """Create a new website alert. Returns the new alert ID."""
    cur = conn.execute("""
        INSERT INTO website_alerts
            (device_mac, domain, threshold_minutes, cooldown_minutes,
             alert_type, use_ollama, custom_message, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
    """, (
        device_mac.upper(),
        kwargs.get("domain", ""),
        kwargs.get("threshold_minutes", 5),
        kwargs.get("cooldown_minutes", 30),
        kwargs.get("alert_type", "alexa"),
        int(kwargs.get("use_ollama", True)),
        kwargs.get("custom_message"),
        time.time(),
    ))
    conn.commit()
    return cur.lastrowid


def update_alert(
    conn: sqlite3.Connection, alert_id: int, **kwargs: Any,
) -> bool:
    """Update an alert. Returns True if updated."""
    sets: list[str] = []
    params: list[Any] = []

    for field in ("domain", "threshold_minutes", "cooldown_minutes",
                  "alert_type", "custom_message"):
        if field in kwargs:
            sets.append(f"{field} = ?")
            params.append(kwargs[field])

    if "use_ollama" in kwargs:
        sets.append("use_ollama = ?")
        params.append(int(kwargs["use_ollama"]))
    if "is_active" in kwargs:
        sets.append("is_active = ?")
        params.append(int(kwargs["is_active"]))

    if not sets:
        return False

    params.append(alert_id)
    cur = conn.execute(
        f"UPDATE website_alerts SET {', '.join(sets)} WHERE id = ?", params,
    )
    conn.commit()
    return cur.rowcount > 0


def delete_alert(conn: sqlite3.Connection, alert_id: int) -> bool:
    """Delete an alert. Returns True if deleted."""
    cur = conn.execute("DELETE FROM website_alerts WHERE id = ?", (alert_id,))
    conn.commit()
    return cur.rowcount > 0


def get_alert_history(
    conn: sqlite3.Connection,
    device_mac: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Get recent alert history."""
    if device_mac:
        rows = conn.execute(
            "SELECT * FROM alert_history WHERE device_mac = ? "
            "ORDER BY triggered_at DESC LIMIT ?",
            (device_mac.upper(), limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM alert_history ORDER BY triggered_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Domain categories CRUD
# ---------------------------------------------------------------------------

def get_all_categories(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Get all domain category mappings."""
    rows = conn.execute(
        "SELECT * FROM domain_categories ORDER BY category, root_domain"
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_category(
    conn: sqlite3.Connection, root_domain: str, category: str,
) -> None:
    """Add or update a domain category."""
    conn.execute("""
        INSERT INTO domain_categories (root_domain, category) VALUES (?, ?)
        ON CONFLICT(root_domain) DO UPDATE SET category = excluded.category
    """, (root_domain, category))
    conn.commit()


def delete_category(conn: sqlite3.Connection, root_domain: str) -> bool:
    """Delete a domain category. Returns True if deleted."""
    cur = conn.execute(
        "DELETE FROM domain_categories WHERE root_domain = ?", (root_domain,),
    )
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Data maintenance
# ---------------------------------------------------------------------------

def purge_old_queries(conn: sqlite3.Connection, retention_days: int = 14) -> int:
    """Delete raw DNS queries older than retention_days. Returns count deleted."""
    cutoff = time.time() - (retention_days * 86400)
    cur = conn.execute("DELETE FROM dns_queries WHERE timestamp < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Tracked devices helper
# ---------------------------------------------------------------------------

def get_tracked_devices(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Get all devices with dns_tracking_enabled = 1."""
    rows = conn.execute(
        "SELECT * FROM devices WHERE dns_tracking_enabled = 1 ORDER BY friendly_name"
    ).fetchall()
    return [dict(r) for r in rows]
