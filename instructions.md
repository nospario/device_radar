# Device Radar — Network Monitor Integration
### Instructions for Claude Code

---

## Project Overview

**Device Radar** is an existing Python application running on a Raspberry Pi 5 that monitors device presence via BLE, Classic Bluetooth, and WiFi/LAN. It stores data in SQLite and serves a Flask web dashboard on port 8080.

The goal of this integration is to add a **network traffic monitoring module** that tracks DNS queries, website visits, and app usage patterns for a specific watched device (the owner's personal iPhone). This should feel like a natural extension of the existing codebase — same architecture, same patterns, same database.

The project lives at `/opt/bt-monitor/` (production) and `/var/www/bluetooth/` (development). All changes should be made in the development directory.

---

## What to Build

### New module: `bt_netmon.py`

A background network monitoring service that:

1. **Sniffs DNS queries** originating from a specified device IP and logs which domains are queried and when
2. **Maps domains to human-readable app/service names** using a lookup table (e.g. `spclient.wg.spotify.com` → `Spotify`)
3. **Tracks connection events** (destination IPs, ports, data volume) for the watched device
4. **Persists all data to the existing SQLite database** (`bt_radar.db`) using new tables
5. **Runs as a background thread or separate service**, consistent with how `bt_scanner.py` and `bt_web.py` are structured

---

## Database Changes (`bt_db.py`)

Add the following new tables to the existing schema. Follow the existing migration pattern in `bt_db.py` — use `CREATE TABLE IF NOT EXISTS` and add a schema version migration if one exists.

### Column: `traffic_monitor` on existing `devices` table

Add a new boolean column to the existing `devices` table via a schema migration:

```sql
ALTER TABLE devices ADD COLUMN traffic_monitor INTEGER NOT NULL DEFAULT 0;
```

This flag controls whether DNS and connection traffic is captured for a given device. It must be added via the migration system — check how existing migrations are handled in `bt_db.py` and follow the same pattern (e.g. a `schema_version` check with `ALTER TABLE` inside a version guard).

Add a helper function:

- `get_traffic_monitored_devices()` — returns all devices where `traffic_monitor = 1`, including their MAC and last known IP

### Table: `dns_log`

```sql
CREATE TABLE IF NOT EXISTS dns_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,           -- ISO8601 datetime
    device_mac  TEXT,                    -- MAC address of querying device (from ARP cache)
    device_ip   TEXT NOT NULL,           -- Source IP of the DNS query
    domain      TEXT NOT NULL,           -- Queried domain name (stripped of trailing dot)
    app_name    TEXT,                    -- Human-readable app/service name (nullable)
    query_type  TEXT DEFAULT 'A'         -- DNS record type (A, AAAA, CNAME, etc.)
);
```

### Table: `connection_log`

```sql
CREATE TABLE IF NOT EXISTS connection_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT NOT NULL,
    device_mac   TEXT,
    device_ip    TEXT NOT NULL,
    dest_ip      TEXT NOT NULL,
    dest_port    INTEGER,
    protocol     TEXT,                   -- TCP or UDP
    bytes_sent   INTEGER DEFAULT 0,
    session_key  TEXT                    -- Hash of src_ip+dst_ip+dst_port for grouping
);
```

Add query helper functions to `bt_db.py`:

- `get_dns_log(device_ip=None, limit=100, since=None)` — returns recent DNS entries, optionally filtered by IP
- `get_top_domains(device_ip=None, hours=24)` — returns domain + count, sorted by frequency
- `get_hourly_activity(device_ip=None, days=7)` — returns activity counts grouped by hour-of-day
- `get_app_usage(device_ip=None, hours=24)` — returns app_name + hit count for named apps only

---

## New File: `bt_netmon.py`

### Dependencies

Uses `scapy` for packet capture. Add `scapy` to `requirements.txt`.

> **Note:** Scapy requires root privileges. The existing scanner already runs as root via systemd, so this is consistent.

### Core responsibilities

#### 1. DNS Sniffer

```python
from scapy.all import DNS, DNSQR, IP, sniff

def start_dns_sniffer(interface, watched_ips, db_path):
    """
    Sniff UDP port 53 traffic. For each DNS query from a watched IP,
    log the domain to dns_log. Resolve app_name via domain_to_app().
    """
```

- Filter: `udp port 53`
- Only log queries **from** IPs in `watched_ips` (not responses)
- Strip the trailing `.` from FQDN before storing
- Run `domain_to_app(domain)` to populate `app_name`
- Deduplicate: don't log the same domain more than once per 60-second window (to avoid spam from keep-alives)

#### 2. Domain-to-App Lookup

```python
def domain_to_app(domain: str) -> str | None:
    """
    Return a human-readable app/service name for a known domain,
    or None if unknown.
    """
```

Build a lookup table covering at minimum:

| Domain pattern | App name |
|---|---|
| `instagram.com`, `cdninstagram.com` | Instagram |
| `youtube.com`, `googlevideo.com`, `ytimg.com` | YouTube |
| `spotify.com`, `spclient.wg.spotify.com` | Spotify |
| `twitter.com`, `twimg.com`, `t.co` | X / Twitter |
| `bbc.co.uk`, `ibl.api.bbc.co.uk` | BBC iPlayer |
| `whatsapp.net`, `whatsapp.com` | WhatsApp |
| `maps.googleapis.com` | Google Maps |
| `apple.com`, `icloud.com`, `mzstatic.com` | Apple / iCloud |
| `facebook.com`, `fbcdn.net` | Facebook |
| `netflix.com`, `nflxvideo.net` | Netflix |
| `tiktok.com`, `tiktokv.com` | TikTok |
| `amazon.co.uk`, `amazon.com` | Amazon |
| `reddit.com`, `redd.it` | Reddit |
| `linkedin.com` | LinkedIn |
| `snapchat.com` | Snapchat |

Use suffix matching (i.e. check if the domain **ends with** a known pattern) rather than exact match.

#### 3. Connection Tracker

```python
def start_connection_tracker(interface, watched_ips, db_path):
    """
    Track TCP/UDP connections from watched IPs.
    Log destination IP, port, protocol, and running byte count.
    """
```

- Use a short-lived session window (e.g. 30 seconds) to aggregate packets before writing to DB
- Avoid logging DNS traffic (port 53) — that's handled separately

#### 4. Watched IP Resolution

Rather than reading from `config.json`, the module should dynamically build its `watched_ips` set from the database:

- Call `get_traffic_monitored_devices()` from `bt_db.py` to get all devices with `traffic_monitor = 1`
- Resolve each device's current IP via ARP cache or the existing `bt_wifi.py` scan results
- Re-check the database periodically (e.g. every 60 seconds) so that toggling a device on/off in the dashboard takes effect without restarting the service
- Log a warning if a watched MAC cannot be resolved to a current IP

#### 5. Threading model

Follow the pattern of `bt_scanner.py`. Use `threading.Thread(daemon=True)` for both sniffer threads. Provide a clean `stop()` mechanism.

---

## Configuration Changes (`config.json`)

Add the following fields:

```json
{
  "netmon_enabled": false,
  "netmon_interface": "eth0",
  "netmon_dedup_window_seconds": 60,
  "netmon_max_log_days": 30
}
```

- `netmon_enabled` — master switch; defaults to `false` so it doesn't break existing setups. When `false`, no sniffing occurs regardless of per-device settings.
- `netmon_interface` — the network interface to sniff on (e.g. `eth0`, `wlan0`)
- `netmon_dedup_window_seconds` — suppress duplicate DNS log entries for the same domain within this window
- `netmon_max_log_days` — auto-purge DNS/connection log entries older than this many days (add a cleanup call to the existing cleanup routine in `bt_db.py`)

> **Note:** The list of devices to monitor is no longer set in `config.json`. It is managed per-device via the web dashboard (see Device Detail page changes below).

---

## Web Dashboard Changes (`bt_web.py` + templates)

### New route: `/netmon`

A new Flask route and template `templates/netmon.html` showing:

1. **Top Domains (last 24h)** — table of domain, app name, hit count
2. **App Usage Summary** — chart or table of named apps and their query counts
3. **Hourly Activity Heatmap** — table or chart showing DNS query volume by hour of day (last 7 days), to reveal usage patterns
4. **Recent DNS Log** — paginated table of recent entries (timestamp, device IP, domain, app name)

### Navigation

Add a **"Network"** nav item to `templates/base.html` linking to `/netmon`, consistent with existing nav items.

### Device Detail page (`templates/device.html`)

Two changes are needed on the device detail page:

#### 1. Traffic Monitor toggle

Add a **"Traffic Monitor"** toggle to the device settings card, alongside the existing toggles (watchlist, notifications, hidden). It should:

- Display the current state of `traffic_monitor` for the device
- Allow the user to enable or disable it with a single click, via a new Flask route `POST /device/<mac>/set_traffic_monitor` that updates the `traffic_monitor` column in the `devices` table
- Only be visible when `netmon_enabled` is `true` in `config.json` — if the global feature is disabled, hide the toggle and show a small note: *"Enable Network Monitor in config to use this feature"*
- Follow the same toggle UI pattern used for watchlist/notifications on the existing device detail page

#### 2. Recent Network Activity card

When `traffic_monitor = 1` for the device and `netmon_enabled` is `true`, show a **"Recent Network Activity"** card displaying the last 10 DNS queries from that device's IP (timestamp, domain, app name). If no data exists yet, show a placeholder message: *"No traffic recorded yet"*.

---

## Systemd Service (optional, for separate process)

If `bt_netmon.py` is implemented as a standalone script rather than a thread inside `bt_scanner.py`, provide a systemd unit file `bt-netmon.service` following the same pattern as `bt-scanner.service`.

---

## Important Technical Notes

### iOS-specific considerations

1. **Private Wi-Fi Address** — iOS randomises MAC addresses per network by default. The user will disable this for their home network (Settings → Wi-Fi → network → Private Wi-Fi Address → off). The module should log a warning if the watched MAC appears to be changing.

2. **iCloud Private Relay** — If enabled, DNS queries may be routed through Apple's relay and won't appear locally. The user should disable this on the home network. The module can detect this situation if DNS queries from the device drop to near-zero while the device is active.

3. **DNS over HTTPS (DoH)** — Some iOS apps bypass port 53 entirely. The connection tracker (port 443 traffic) acts as a fallback for these cases.

### Interface selection

The Pi may be on `eth0` (wired) or `wlan0` (wireless). The `netmon_interface` config field controls this. Default to `eth0`. The sniffer must be on the interface that sees traffic to/from the router — if the iPhone is on WiFi and the Pi is on Ethernet, traffic should still pass through the router and be visible.

### Permissions

Scapy packet capture requires a raw socket, which requires root. The existing services run as root. No change needed.

---

## Files to Create/Modify Summary

| File | Action |
|---|---|
| `bt_netmon.py` | **Create** — new network monitor module |
| `bt_db.py` | **Modify** — add `dns_log`, `connection_log` tables; migrate `traffic_monitor` column onto `devices`; add query helpers |
| `bt_web.py` | **Modify** — add `/netmon` route, `POST /device/<mac>/set_traffic_monitor` route, and device detail card |
| `bt_scanner.py` | **Modify** — optionally launch `bt_netmon` thread if `netmon_enabled` |
| `config.json` | **Modify** — add `netmon_*` config fields (no `netmon_watched_macs`) |
| `requirements.txt` | **Modify** — add `scapy` |
| `templates/netmon.html` | **Create** — network monitor dashboard page |
| `templates/base.html` | **Modify** — add Network nav item |
| `templates/device.html` | **Modify** — add Traffic Monitor toggle and recent activity card |
| `bt-netmon.service` | **Create (optional)** — if running as separate systemd service |

---

## Style & Conventions

- Follow the existing code style in the repo (Python 3.11+, f-strings, type hints where used)
- Use the existing dark theme CSS (`static/style.css`) — do not introduce new CSS frameworks
- All DB operations should go through `bt_db.py` — do not access SQLite directly from `bt_netmon.py`
- Log to stdout/stderr using Python's `logging` module, consistent with existing scripts
- Handle `KeyboardInterrupt` and `SystemExit` gracefully in any new threads

---

*End of integration brief.*
