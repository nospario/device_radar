# DNS Traffic Monitor — Device Radar Integration

## Overview

Integrate Pi-hole DNS query logging into the existing Device Radar application to provide network traffic visibility, browsing behaviour analytics, and time-based Alexa alerts that nudge users away from distracting websites.

DNS monitoring is **opt-in per device**. Only devices with `dns_tracking_enabled` turned on will have their DNS queries ingested and displayed. By default all devices are opted out. Initially, Richard's iPhone and Richard's MacBook will be enabled.

This feature is delivered in two phases:

### Phase 1 (Core)

1. **Traffic Page** — A new dashboard page showing all DNS query traffic with filtering, stats, and per-device breakdowns
2. **Device Alert Cards** — Per-device alert configuration on existing device pages, enabling website-specific Alexa announcements after configurable dwell times
3. **Alert Engine** — Background dwell time detection and notification delivery

### Phase 2 (Analytics)

4. **Reporting Page** — Aggregated browsing behaviour reports and trends over configurable time periods, powered by pre-computed daily aggregation tables

---

## Architecture Context

### Existing Device Radar Stack

- **Framework:** Flask (Python)
- **Database:** SQLite (WAL mode, `mac_address TEXT` as primary key on `devices` table)
- **Frontend:** HTML/CSS/JS dashboard with column filtering, AJAX data loading, dark theme
- **Notifications:** Telegram, Alexa (via `alexa_remote_control.sh`), ntfy
- **AI:** Ollama (local LLM for generating dynamic messages)
- **Presence Detection:** BLE/WiFi/Classic Bluetooth scanning
- **Background Tasks:** Async tasks in `bt_scanner.py` via `asyncio.create_task()` (encourage loop, proximity checks)
- **Hosting:** Raspberry Pi 5

### New Dependencies

- **Pi-hole** must be installed and configured as the network DNS server (blocking can be disabled — this is purely for logging)
- Pi-hole's FTL database is located at `/etc/pihole/pihole-FTL.db` (SQLite, WAL mode)
- Pi-hole also exposes a REST API at `http://localhost/admin/api.php`
- **`tldextract`** Python library for domain normalisation (add to `requirements.txt`)
- **Chart.js** via CDN for traffic and reporting charts (no build step required, ~60KB)

---

## Component 0: Per-Device Opt-In

### Rationale

DNS queries reveal browsing behaviour. Monitoring is opt-in per device — only devices explicitly enabled will have their queries ingested, stored, and displayed. This respects household privacy and keeps storage manageable.

### Implementation

Add a new column to the `devices` table:

```python
_add_column(conn, "devices", "dns_tracking_enabled", "INTEGER DEFAULT 0")
```

This follows the existing pattern used for `proximity_enabled`, `is_welcome`, etc.

### Device Detail Page

Add a **"DNS Tracking"** checkbox to the existing **Settings** card on the device detail page (`/device/<mac>`), alongside the existing toggles (watchlist, notify, welcome, hidden). The checkbox label should read "Track DNS activity" with a subtitle "Monitor this device's DNS queries via Pi-hole".

The PATCH `/api/devices/<mac>` endpoint already supports updating arbitrary device fields, so no new API endpoint is needed — just include `dns_tracking_enabled` in the existing form submission.

### Initial State

All devices default to `dns_tracking_enabled = 0`. After deployment, manually enable tracking for:
- **Richard's iPhone** (via the device detail page)
- **Richard's MacBook** (via the device detail page)

A one-time migration can also set these automatically:

```python
_run_migration(conn, "enable_dns_tracking_richard", """
    UPDATE devices SET dns_tracking_enabled = 1
    WHERE friendly_name IN ("Richard's iPhone", "Richard's MacBook")
""")
```

### Enforcement

The ingestion service must **only** store queries from devices that have `dns_tracking_enabled = 1`. The IP-to-device mapping step (see Component 1) checks this flag and silently discards queries from opted-out or unknown devices.

The traffic page, reporting page, and alert engine all filter by `dns_tracking_enabled` devices only. Queries from unknown IPs (no matching device) are **not** stored — they can be seen in Pi-hole's own admin interface if needed.

---

## Component 1: Pi-hole Data Ingestion

### New Module

Create `bt_dns.py` as a new module (following the existing `bt_news.py`, `bt_weather.py` pattern). This module handles all DNS-related logic: ingestion, domain normalisation, category lookup, session detection, and alert evaluation.

### Data Source

Pi-hole's FTL database (`/etc/pihole/pihole-FTL.db`) contains a `queries` table with:

- `timestamp` — Unix epoch
- `type` — Query type (A, AAAA, CNAME, etc.)
- `domain` — The domain queried
- `client` — IP address of the requesting device
- `status` — Whether the query was forwarded, cached, blocked, etc.
- `forward` — Upstream DNS server used

### Data Access Strategy

Use a **hybrid approach**:

- **Primary (real-time polling):** Use Pi-hole's REST API (`http://localhost/admin/api.php?getAllQueries&from=TIMESTAMP`) for periodic polling of recent queries. This avoids file permission issues and SQLite locking conflicts with FTL.
- **Fallback (direct DB):** If the API is unavailable or for historical backfill, open the FTL database in **read-only mode** (`sqlite3.connect('file:/etc/pihole/pihole-FTL.db?mode=ro', uri=True)`). The FTL database is owned by `pihole:pihole` — the Device Radar process will need read access (add the `pihole` group to the service user, or use API-only mode).

### Ingestion Service

Run as an **async background task inside `bt_scanner.py`**, following the same pattern as the encourage loop (`run_encourage_loop()`). Start via `asyncio.create_task(dns_ingestion_loop())` in the scanner's `run()` method.

The ingestion loop:

1. Runs every `dns_poll_interval_seconds` (default 30)
2. Fetches new queries from Pi-hole since the last poll timestamp (tracked in memory, seeded from the most recent `dns_queries.timestamp` on startup)
3. Resolves each Pi-hole `client` IP to a Device Radar device MAC address via ARP lookup (see IP-to-Device Mapping below)
4. **Checks `dns_tracking_enabled`** — skips queries from devices that are not opted in
5. Normalises the domain (extracts root domain via `tldextract`)
6. Looks up category from the `domain_categories` table
7. Inserts into `dns_queries` table
8. Updates `dns_daily_stats` aggregation table (increment counters, update first/last seen)
9. Tracks the last processed Pi-hole timestamp to avoid re-processing

### IP-to-Device Mapping

**Critical:** Device Radar's `ip_address` column is only populated for WiFi-scanned devices. BLE-only devices (like phones tracked via Bluetooth) won't have an IP in the database. The ingestion service must therefore maintain its own IP→MAC mapping via ARP.

Each ingestion cycle:

1. Read `/proc/net/arp` to build a current `{IP: MAC}` mapping table
2. For each Pi-hole query, look up the `client` IP in the ARP table to get the MAC address
3. Match the MAC against the `devices` table (checking both `mac_address` and linked devices in the same group)
4. Cache the IP→MAC mapping in memory with a TTL of 5 minutes (ARP entries are relatively stable)
5. If a device's IP changes (DHCP), the ARP lookup will catch this naturally

**Linked device handling:** If a query maps to a device that is a secondary in a link group, attribute it to the **primary** device in the group. This ensures all DNS activity for a person is unified under one device.

### Domain Normalisation

DNS queries for social media and other sites come through as many subdomains (e.g. `scontent-lhr8-1.cdninstagram.com`, `graph.instagram.com`, `www.instagram.com`). The ingestion layer should:

- Extract and store the root/registered domain using `tldextract` (e.g. `tldextract.extract(domain).registered_domain`)
- Also store the full queried domain for detail views
- Look up the category from the `domain_categories` database table

### New Database Tables

All tables use `mac_address TEXT` as the device foreign key, consistent with the existing `devices`, `events`, and `news_read` tables. There is no integer device ID in Device Radar.

#### `dns_queries`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PRIMARY KEY | Auto-increment |
| `timestamp` | REAL | Unix epoch when the query occurred (matches Pi-hole format) |
| `device_mac` | TEXT | MAC address of the device (FK to `devices.mac_address`), NULL for unknown |
| `client_ip` | TEXT | Raw IP from Pi-hole |
| `full_domain` | TEXT | Full queried domain |
| `root_domain` | TEXT | Extracted root domain (e.g. `instagram.com`) |
| `query_type` | TEXT | A, AAAA, CNAME, etc. |
| `status` | TEXT | forwarded, cached, blocked |
| `category` | TEXT | Social Media, News, etc. (NULL if uncategorised) |
| `upstream` | TEXT | Which upstream DNS resolved it (NULL if cached/blocked) |

**Indexes** (critical for performance on Raspberry Pi):

```sql
CREATE INDEX idx_dns_queries_ts ON dns_queries(timestamp DESC);
CREATE INDEX idx_dns_queries_device_ts ON dns_queries(device_mac, timestamp DESC);
CREATE INDEX idx_dns_queries_domain_ts ON dns_queries(root_domain, timestamp DESC);
CREATE INDEX idx_dns_queries_device_domain_ts ON dns_queries(device_mac, root_domain, timestamp DESC);
CREATE INDEX idx_dns_queries_category ON dns_queries(category);
```

#### `dns_daily_stats`

Pre-computed daily aggregation table for the reporting page. Updated incrementally during ingestion — **never query raw `dns_queries` for reports**.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PRIMARY KEY | Auto-increment |
| `date` | TEXT | Date string `YYYY-MM-DD` |
| `device_mac` | TEXT | MAC address of the device |
| `root_domain` | TEXT | Root domain |
| `category` | TEXT | Category at time of aggregation |
| `query_count` | INTEGER | Total queries that day |
| `first_seen` | REAL | Earliest query timestamp that day |
| `last_seen` | REAL | Latest query timestamp that day |
| `estimated_minutes` | REAL | Estimated browsing time (updated by nightly rollup) |

**Unique constraint:** `(date, device_mac, root_domain)` — upsert on conflict.

```sql
CREATE UNIQUE INDEX idx_dns_daily_stats_key ON dns_daily_stats(date, device_mac, root_domain);
```

#### `domain_categories`

Stored in the database (not a Python dict) so it can be extended via the UI.

| Column | Type | Description |
|---|---|---|
| `root_domain` | TEXT PRIMARY KEY | e.g. `instagram.com` |
| `category` | TEXT NOT NULL | Social Media, News, Productivity, Entertainment, Shopping, Other |

Seed with initial data on first run:

```python
SEED_CATEGORIES = {
    'instagram.com': 'Social Media',
    'facebook.com': 'Social Media',
    'twitter.com': 'Social Media',
    'x.com': 'Social Media',
    'tiktok.com': 'Social Media',
    'reddit.com': 'Social Media',
    'linkedin.com': 'Social Media',
    'snapchat.com': 'Social Media',
    'pinterest.com': 'Social Media',
    'threads.net': 'Social Media',
    'youtube.com': 'Entertainment',
    'netflix.com': 'Entertainment',
    'twitch.tv': 'Entertainment',
    'spotify.com': 'Entertainment',
    'disneyplus.com': 'Entertainment',
    'primevideo.com': 'Entertainment',
    'bbc.co.uk': 'News',
    'theguardian.com': 'News',
    'dailymail.co.uk': 'News',
    'sky.com': 'News',
    'telegraph.co.uk': 'News',
    'independent.co.uk': 'News',
    'github.com': 'Productivity',
    'stackoverflow.com': 'Productivity',
    'notion.so': 'Productivity',
    'slack.com': 'Productivity',
    'trello.com': 'Productivity',
    'amazon.co.uk': 'Shopping',
    'amazon.com': 'Shopping',
    'ebay.co.uk': 'Shopping',
    'ebay.com': 'Shopping',
    'google.com': 'Search',
    'google.co.uk': 'Search',
    'bing.com': 'Search',
    'duckduckgo.com': 'Search',
}
```

#### `website_alerts`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PRIMARY KEY | Auto-increment |
| `device_mac` | TEXT NOT NULL | MAC address (FK to `devices.mac_address`) |
| `domain` | TEXT NOT NULL | Root domain to monitor (e.g. `instagram.com`) |
| `threshold_minutes` | INTEGER NOT NULL DEFAULT 5 | Minutes of sustained browsing before alert fires |
| `cooldown_minutes` | INTEGER NOT NULL DEFAULT 30 | Minimum gap between repeat alerts |
| `alert_type` | TEXT NOT NULL DEFAULT 'alexa' | `alexa`, `telegram`, `ntfy`, or `all` |
| `use_ollama` | INTEGER NOT NULL DEFAULT 1 | Whether to generate dynamic messages via Ollama |
| `custom_message` | TEXT | Static message override (used if `use_ollama` is 0) |
| `is_active` | INTEGER NOT NULL DEFAULT 1 | Enable/disable toggle |
| `created_at` | REAL | Unix epoch when the alert was created |
| `last_triggered` | REAL | Unix epoch of last alert fire (NULL if never) |

**Index:**

```sql
CREATE INDEX idx_website_alerts_device ON website_alerts(device_mac, is_active);
```

#### `alert_history`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PRIMARY KEY | Auto-increment |
| `alert_id` | INTEGER | FK to `website_alerts.id` |
| `device_mac` | TEXT | MAC address of the device |
| `domain` | TEXT | Domain that triggered it |
| `triggered_at` | REAL | Unix epoch when the alert fired |
| `message` | TEXT | The actual message delivered |
| `alert_type` | TEXT | Which channel was used |
| `browsing_duration_mins` | REAL | How long they'd been browsing when it fired |

**Index:**

```sql
CREATE INDEX idx_alert_history_device_ts ON alert_history(device_mac, triggered_at DESC);
```

### Data Retention

Raw `dns_queries` rows grow fast (10,000–50,000/day on an active network). On a Raspberry Pi with SD card storage, this must be managed aggressively:

- **Raw queries:** Auto-purge after `dns_data_retention_days` (default **14 days**). This keeps the table under ~700K rows worst case.
- **Daily stats:** Kept indefinitely (one row per device per domain per day — modest growth).
- **Alert history:** Kept indefinitely (low volume).
- **Purge schedule:** Run daily as part of the existing cleanup cycle in `bt_scanner.py` (every 100th scan cycle already runs `hide_stale_random_macs()`).

---

## Component 2: Traffic Page

### Route

`/traffic` — New Flask route in `bt_web.py`, add "Traffic" to the navbar in `base.html` alongside Dashboard, History, Pairing, Alexa, Assistant.

### Page Layout

#### Stats Bar (Top of Page)

Display summary statistics in card format (matching the existing `stats-grid` CSS pattern on the dashboard):

- **Total Queries Today** — Count of all DNS queries in the last 24 hours
- **Unique Domains Today** — Distinct root domains queried
- **Most Active Device** — Device with the highest query count today (linked to `/device/<mac>`)
- **Top Domain** — Most queried root domain today with count
- **Blocked Queries** — Count of blocked queries (show 0 if Pi-hole blocking is disabled)
- **Active Alerts** — Number of currently active website alerts across all devices

#### Filters Section

Implement **server-side filtering** via API query parameters. Unlike the dashboard (which loads ~50 devices client-side and filters in JS), the traffic page handles thousands of rows and must paginate server-side.

- **Date/Time Range** — Start and end datetime pickers, with quick presets: Last Hour, Last 6 Hours, Today, Yesterday, Last 7 Days, Custom Range
- **Device** — Dropdown of DNS-tracked devices only (those with `dns_tracking_enabled = 1`), by friendly name
- **Domain** — Text search with autocomplete from known root domains
- **Category** — Multi-select: Social Media, News, Productivity, Entertainment, Shopping, Search, Other, Uncategorised
- **Query Status** — forwarded, cached, blocked

Filters update the table and stats via AJAX (no full page reload). Filter state persisted in `localStorage` (matching the existing dashboard pattern in `app.js`).

#### Traffic Table

Sortable, server-side paginated table:

| Column | Notes |
|---|---|
| **Time** | Formatted timestamp (relative + absolute on hover), sortable |
| **Device** | Friendly device name, clickable link to `/device/<mac>` |
| **Domain** | Root domain, with full queried domain shown on hover/tooltip |
| **Category** | Colour-coded badge (Social Media = blue, Entertainment = purple, News = orange, Productivity = green, Shopping = yellow, Search = grey) |
| **Status** | forwarded/cached/blocked with appropriate badge styling |
| **Upstream** | Which DNS server resolved it |

- Default sort: newest first
- Pagination: 50 rows per page with page navigation (server-side limit/offset)
- Row count indicator showing "Showing X–Y of Z queries"
- Export option: CSV download of current filtered view (server-side generation)

#### Domain Breakdown Chart

Below the table, a **bar chart** (Chart.js via CDN) showing the top 10 domains by query count for the currently selected time range and filters. Updated via AJAX when filters change.

---

## Component 3: Device Page — Alert Management Card

### Location

Add a new `detail-card` section to the existing device detail page (`/device/<mac>`), below the BBC News card. **Only visible** for devices with `dns_tracking_enabled = 1`.

### Card Title

"Browsing Alerts"

### Card Content

#### Active Alerts Table

Display all `website_alerts` for this device:

| Column | Notes |
|---|---|
| Domain | e.g. `instagram.com` |
| Threshold | e.g. "5 minutes" |
| Cooldown | e.g. "30 minutes" |
| Alert Via | Alexa / Telegram / ntfy / All |
| Dynamic Message | Yes/No (Ollama toggle) |
| Last Triggered | Relative timestamp or "Never" |
| Status | Active/Inactive toggle |
| Actions | Edit / Delete buttons |

#### Add New Alert Form

Button or expandable section with fields:

- **Domain** — Text input with autocomplete from previously seen root domains (queried from `dns_queries` for this device). Allow manual entry for domains not yet seen.
- **Time Threshold** — Number input in minutes (how long sustained browsing before alert fires). Default: 5. Min: 1, Max: 120.
- **Cooldown** — Number input in minutes (gap between repeat alerts). Default: 30. Min: 5, Max: 1440.
- **Alert Channel** — Select: Alexa, Telegram, ntfy, All. Default: Alexa.
- **Use Dynamic Messages** — Toggle/checkbox. When enabled, Ollama generates a unique message each time. Default: On.
- **Custom Message** — Textarea, only shown when dynamic messages are off. Placeholder: "Stop browsing {domain} and get back to work!" Supports `{domain}` and `{minutes}` template variables.
- **Save / Cancel buttons**

#### Edit Alert

Same form as above, pre-populated with existing values. Inline edit consistent with Device Radar's existing form patterns (same card, fields become editable).

#### Alert History (Collapsed/Expandable)

Recent alert history for this device, showing last 10 triggered alerts with: timestamp, domain, message delivered, and browsing duration at trigger time.

---

## Component 4: Alert Engine

### Background Process

Run as an **async background task inside `bt_scanner.py`**, following the same pattern as `run_encourage_loop()` and `check_proximity_devices()`. Start via `asyncio.create_task(dns_alert_loop())` in the scanner's `run()` method.

The alert check loop:

1. Runs every `alert_check_interval_seconds` (default 60)
2. Queries all active alerts from `website_alerts` where `is_active = 1`
3. For each alert, runs the dwell time detection logic against `dns_queries`
4. Fires alerts when thresholds are met and cooldown has elapsed

### Dwell Time Detection Logic

```
For each active alert:
  1. Get all dns_queries for this device_mac + root_domain
     in the last (threshold_minutes + session_gap_minutes) minutes
     where session_gap_minutes = 3 (from config: alert_session_gap_minutes)
  2. Sort by timestamp ascending
  3. Walk through queries, tracking "sessions":
     - A session starts with the first query
     - A session continues as long as subsequent queries are within
       session_gap_minutes of the previous one
     - A session ends when there's a gap > session_gap_minutes
  4. If the most recent session's duration >= threshold_minutes,
     AND cooldown has elapsed since last_triggered,
     fire the alert
  5. Only consider the most recent session (not historical ones) —
     we care about current browsing, not past
```

### Alert Execution

When an alert fires:

1. **If `use_ollama` is True:**
   - Send a prompt to the local Ollama instance (using the existing `bt_alexa` Ollama integration pattern):
     ```
     Generate a short, witty, slightly sarcastic message (1-2 sentences max) telling {person_name} to stop browsing {domain} after {minutes} minutes. Be creative and vary the tone — sometimes motivational, sometimes funny, sometimes gently mocking. Don't be mean. Keep it under 30 words.
     ```
   - Resolve `{person_name}` via the existing `person_aliases` reverse lookup (same as arrival greetings)
   - Use the response as the alert message

2. **If `use_ollama` is False:**
   - Use the `custom_message` field
   - Replace `{domain}` and `{minutes}` template variables

3. **Deliver via configured channel:**
   - **Alexa:** Use the existing `bt_alexa.speak()` function. Use the device's `alexa_voice` if set (same SSML wrapping as arrival greetings). Speak on the device's `proximity_alexa_device` if configured, otherwise `DEFAULT_ALEXA_DEVICE` from config.
   - **Telegram:** Use the existing `bt_telegram.send_notification()` async function
   - **ntfy:** Use the existing ntfy integration (httpx POST)
   - **All:** Send via all three channels

4. **Log to `alert_history`** with the full message text, timestamp, and browsing duration

5. **Update `last_triggered`** on the `website_alerts` record

---

## Component 5: Reporting Page (Phase 2)

> **This component should be built after Phase 1 is stable.** It depends on the `dns_daily_stats` aggregation table being reliably populated by the ingestion service.

### Route

`/reports` — New Flask route, add to navigation.

### Purpose

Provide aggregated browsing behaviour insights over longer time periods, querying only from the pre-computed `dns_daily_stats` table (never raw `dns_queries`).

### Page Layout

#### Report Configuration

- **Time Period Selector** — Today, Yesterday, This Week, Last Week, This Month, Last Month, Custom Range
- **Device Filter** — All tracked devices or specific device (only devices with `dns_tracking_enabled = 1`)
- **Generate Report Button**

#### Report Sections

**1. Browsing Summary**

- Total queries in period (sum of `query_count` from `dns_daily_stats`)
- Total unique domains
- Estimated browsing time per device (sum of `estimated_minutes`)
- Peak browsing hour (requires hourly aggregation — add `dns_hourly_stats` table if needed, or derive from raw queries for short periods only)

**2. Top Domains Table**

| Column | Notes |
|---|---|
| Domain | Root domain |
| Category | Social Media, News, etc. |
| Total Queries | Sum of query_count |
| Estimated Time | Sum of estimated_minutes |
| Devices | Which tracked devices accessed it |
| First Seen | Earliest first_seen in period |
| Last Seen | Latest last_seen in period |

**3. Category Breakdown**

- Doughnut chart (Chart.js) of query distribution by category
- Bar chart of estimated time per category

**4. Device Comparison** (when "All Devices" is selected)

- Side-by-side bar chart comparing top 5 domains per device
- Table showing each device's top 5 domains and estimated time

**5. Alert Activity**

- Summary of alerts triggered in the period
- Table from `alert_history`: timestamp, device, domain, message, duration at trigger time

### Export

- CSV download of the report data (server-side generation)

---

## API Endpoints

All new endpoints follow existing conventions: MAC-based device references, plural `/api/devices/` prefix, JSON responses, query parameter filtering.

### Traffic Endpoints

- `GET /api/traffic` — Paginated, server-side filtered query log
  - Query params: `from` (timestamp), `to` (timestamp), `device_mac`, `domain` (search), `category`, `status`, `limit` (default 50), `offset` (default 0), `sort` (default `timestamp_desc`)
  - Response: `{ queries: [...], total: N }`
- `GET /api/traffic/stats` — Summary statistics for the current filter context
  - Same filter params as above (minus pagination)
  - Response: `{ total_queries, unique_domains, most_active_device, top_domain, blocked_count, active_alerts }`
- `GET /api/traffic/top-domains` — Top N domains for chart data
  - Query params: `from`, `to`, `device_mac`, `limit` (default 10)
  - Response: `{ domains: [{ domain, count, category }] }`
- `GET /api/traffic/export` — CSV download of current filtered view
  - Same filter params as `/api/traffic`
  - Response: CSV file attachment

### Alert Endpoints

- `GET /api/devices/<mac>/alerts` — List alerts for a device
- `POST /api/devices/<mac>/alerts` — Create a new alert
  - Body: `{ domain, threshold_minutes, cooldown_minutes, alert_type, use_ollama, custom_message }`
- `PATCH /api/devices/<mac>/alerts/<alert_id>` — Update an alert
  - Body: partial update of any alert field
- `DELETE /api/devices/<mac>/alerts/<alert_id>` — Delete an alert
- `GET /api/devices/<mac>/alerts/history` — Recent alert history for a device
  - Query params: `limit` (default 10)

### DNS Activity Endpoint

- `GET /api/devices/<mac>/dns-activity` — Recent DNS activity for a specific device
  - Query params: `limit` (default 20), `from`, `to`
  - Response: `{ queries: [...] }`

### Domain Category Endpoints

- `GET /api/dns/categories` — List all domain→category mappings
- `POST /api/dns/categories` — Add or update a domain category
  - Body: `{ domain, category }`
- `DELETE /api/dns/categories/<domain>` — Remove a domain category

### Report Endpoints (Phase 2)

- `GET /api/reports/generate` — Generate report data for given parameters
  - Query params: `from`, `to`, `device_mac`
  - Response: full report JSON (summary, top domains, categories, device comparison, alert activity)
- `GET /api/reports/export` — CSV download of report data

---

## Configuration

Add the following keys to `config.json` (following the existing `setdefault()` pattern in `load_or_create_config()`):

```json
{
  "dns_monitor_enabled": true,
  "pihole_ftl_db_path": "/etc/pihole/pihole-FTL.db",
  "pihole_api_url": "http://localhost/admin/api.php",
  "dns_poll_interval_seconds": 30,
  "dns_data_retention_days": 14,
  "alert_check_interval_seconds": 60,
  "alert_session_gap_minutes": 3,
  "dns_default_alexa_device": "Laura's Echo"
}
```

Note: Ollama model and URL are already configured globally (`ollama_model`, `ollama_url`). No need for a separate `OLLAMA_ALERT_MODEL` — reuse the existing config.

The `dns_monitor_enabled` flag is a global kill switch for the entire feature. When false, the ingestion loop and alert engine do not run, and the Traffic nav link is hidden.

---

## Implementation Notes

### Module Structure

All DNS-related logic lives in a new `bt_dns.py` module:

```python
# bt_dns.py — DNS traffic monitoring via Pi-hole

# Ingestion
async def dns_ingestion_loop(config: dict) -> None: ...
def _poll_pihole_api(api_url: str, since_timestamp: float) -> list[dict]: ...
def _poll_pihole_db(db_path: str, since_timestamp: float) -> list[dict]: ...
def _resolve_ip_to_mac(ip: str, arp_cache: dict) -> str | None: ...
def _refresh_arp_cache() -> dict[str, str]: ...
def _normalise_domain(full_domain: str) -> str: ...
def _get_category(conn, root_domain: str) -> str | None: ...

# Alert engine
async def dns_alert_loop(config: dict) -> None: ...
def _detect_active_session(conn, device_mac: str, domain: str, gap_minutes: int) -> float | None: ...
async def _fire_alert(alert: dict, session_minutes: float, config: dict) -> None: ...

# Queries (for API endpoints)
def get_dns_queries(conn, filters: dict, limit: int, offset: int) -> tuple[list, int]: ...
def get_traffic_stats(conn, filters: dict) -> dict: ...
def get_top_domains(conn, filters: dict, limit: int) -> list: ...
def get_daily_stats(conn, filters: dict) -> list: ...

# Data maintenance
def purge_old_queries(conn, retention_days: int) -> int: ...
def update_daily_stats(conn, date: str, device_mac: str, root_domain: str, category: str, timestamp: float) -> None: ...
```

### Scanner Integration

In `bt_scanner.py`, the `run()` method starts the DNS tasks alongside existing background tasks:

```python
async def run(self):
    # ... existing setup ...
    asyncio.create_task(bt_alexa.run_encourage_loop(self.config))

    # DNS monitoring (new)
    if self.config.get('dns_monitor_enabled', False):
        asyncio.create_task(bt_dns.dns_ingestion_loop(self.config))
        asyncio.create_task(bt_dns.dns_alert_loop(self.config))

    while True:
        await self.process_scan()
        # ... existing loop ...
```

### Performance Considerations

- **Server-side pagination is mandatory** for the traffic page. Never load the full `dns_queries` table into memory. The existing dashboard loads all devices client-side (fine for ~50 rows), but that pattern does not scale to DNS queries.
- **Indexes** on `dns_queries` are critical — see table definition above. Without them, filtered queries on a 500K+ row table will be unusably slow on Pi hardware.
- **The `dns_daily_stats` table** is the only source for the reporting page. Never aggregate raw queries for multi-day reports.
- **ARP cache refresh** happens once per ingestion cycle (every 30s), not per query. The result is cached in memory.
- **`tldextract`** results should be cached in memory (it does network lookups on first use for the public suffix list — call `tldextract.TLDExtract(cache_dir=...)` with a local cache directory).

### UI Consistency

- Match all new pages to the existing Device Radar dark theme (`style.css` variables: `--bg: #0f1117`, `--accent: #5b8af5`, etc.)
- Use the same `stats-grid`, `detail-card`, `btn`, `badge` CSS classes
- Traffic page pagination and filter controls should feel consistent with the existing dashboard and history page patterns
- The "DNS Tracking" toggle on the device detail page goes in the existing Settings card, not a separate card
- The "Browsing Alerts" card follows the same `detail-card` pattern as the Proximity Alexa and BBC News cards
- Navigation should be: Dashboard | **Traffic** | History | Pairing | Alexa | Assistant
- Chart.js loaded via CDN `<script>` tag in the traffic and reports templates only (not in `base.html`)

---

## Suggested Implementation Order

### Phase 1 (Core)

1. **Pi-hole installation and configuration** — Install Pi-hole, disable blocking if desired, configure as network DNS
2. **Dependencies** — Add `tldextract` to `requirements.txt`, install
3. **Database schema** — Add `dns_tracking_enabled` column to `devices`, create `dns_queries`, `dns_daily_stats`, `domain_categories`, `website_alerts`, `alert_history` tables in `bt_db.init_db()`. Run migration to enable tracking for Richard's iPhone and Richard's MacBook
4. **`bt_dns.py` module** — Ingestion loop, ARP-based IP→MAC resolution, domain normalisation, category lookup, data retention purge
5. **Scanner integration** — Start `dns_ingestion_loop()` and `dns_alert_loop()` as async tasks in `bt_scanner.py`
6. **Device page: DNS tracking toggle** — Add checkbox to Settings card, wire to existing PATCH endpoint
7. **Device page: Browsing Alerts card** — Alert CRUD UI (add/edit/delete alerts, view history)
8. **Alert engine** — Dwell time detection, Ollama message generation, delivery via Alexa/Telegram/ntfy
9. **Traffic page** — Template, route, stats bar, server-side filtered/paginated table, Chart.js domain breakdown
10. **Testing and tuning** — ARP resolution accuracy, dwell time sensitivity, Ollama prompt quality, pagination performance

### Phase 2 (Analytics)

11. **Daily stats rollup** — Nightly job to compute `estimated_minutes` from query clustering in `dns_daily_stats`
12. **Reporting page** — Template, route, report generation from `dns_daily_stats`, Chart.js charts, CSV export
13. **Domain category management** — API endpoints and optional UI for adding/editing categories

---

## Testing Considerations

- Test ARP-based IP→MAC resolution with both static and DHCP-assigned IPs
- Test that queries from opted-out devices are **not** stored
- Test that queries from unknown IPs (no ARP match) are discarded
- Test linked device attribution (secondary device queries attributed to primary)
- Test `tldextract` with edge cases: CDN subdomains, `.co.uk` TLDs, IP-based queries
- Test Pi-hole API polling with various query volumes (ensure no queries are missed between polls)
- Test alert dwell time detection with realistic browsing patterns (social media generates many CDN subdomain queries — all should resolve to one root domain)
- Test alert cooldown logic to ensure alerts don't spam
- Test Ollama message generation for variety and appropriateness
- Test Alexa announcement delivery under various conditions (Echo online/offline, voice selection)
- Test with multiple simultaneous alerts for different domains on the same device
- Test data retention purge (verify old queries are deleted, daily stats are preserved)
- Test traffic page pagination with 10K+ rows — verify response times under 500ms
- Test Pi-hole API fallback to direct DB access
- Test the global `dns_monitor_enabled` kill switch (all DNS features disabled cleanly)
