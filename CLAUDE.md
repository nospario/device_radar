# Device Radar

## Project Overview

A multi-module Python application for Raspberry Pi 5 that scans for nearby devices via BLE, Classic Bluetooth, and WiFi/LAN. It tracks device presence in a SQLite database, provides a real-time Flask web dashboard, and sends notifications via Telegram when watched devices arrive or depart. Includes a Telegram bot with natural language presence queries powered by Ollama.

## Target Environment

- Raspberry Pi 5 running Raspberry Pi OS 64-bit (Bookworm)
- Python 3.11+
- Runs as systemd services under root (required for BLE scanning)
- Development directory: `/var/www/bluetooth/`
- Production directory: `/opt/bt-monitor/`

## Architecture

Three services plus supporting modules:

- **bt_scanner.py** — async background scanner: BLE, Classic Bluetooth, WiFi/LAN discovery, device classification, state tracking, and Telegram notifications
- **bt_web.py** — Flask web dashboard on port 8080 with REST API
- **bt_telegram.py** — Telegram bot with presence queries, Ollama chat integration, and proactive arrival/departure notifications

Supporting modules:

| Module | Purpose |
|---|---|
| `bt_db.py` | SQLite schema (WAL mode), migrations, and all query/mutation functions |
| `bt_classify.py` | Device type and manufacturer identification from BLE data, device class codes, and name patterns |
| `bt_pair.py` | Bluetooth pairing/unpairing via `bluetoothctl` subprocess |
| `bt_wifi.py` | WiFi/LAN device discovery via ping sweep + ARP table parsing; targeted ping confirmation |
| `bt_alexa.py` | Alexa TTS via `alexa_remote_control.sh`, Ollama-generated welcome greetings, encouragement loop, proximity-triggered messages, and per-device SSML voice selection |
| `bt_calendar.py` | Apple Calendar (iCloud CalDAV) integration — event fetching, caching, and prompt context for proximity/welcome messages |
| `bt_weather.py` | Current weather via Open-Meteo API — fetches temperature and conditions, caches in memory, provides formatted string for Alexa TTS prefix |
| `bt_news.py` | BBC News RSS headline fetching, per-device read tracking, and spoken suffix formatting for Alexa TTS |
| `bt_dns.py` | DNS traffic monitoring via Pi-hole — query ingestion, ARP-based IP→MAC resolution, domain categorisation, session detection, browsing alerts, daily stats aggregation |

## Database

SQLite with WAL mode (`bt_radar.db`). Core tables:

- **devices** — all known devices with state (`DETECTED`/`LOST`), scan info, flags (`is_watchlisted`, `is_notify`, `is_hidden`, `is_paired`), device linking (`linked_to`), proximity alert settings (`proximity_enabled`, `proximity_rssi_threshold`, `proximity_interval`, `proximity_alexa_device`, `proximity_prompt`, `last_proximity_message`), calendar integration (`calendar_calendars` — JSON array of calendar names), news feed selection (`news_feeds` — JSON array of feed keys), Alexa voice selection (`alexa_voice` — Amazon Polly voice name for SSML), and DNS tracking opt-in (`dns_tracking_enabled`)
- **events** — arrival/departure event log with timestamps
- **news_headlines** — fetched BBC RSS headlines with guid deduplication, feed_key, title, published timestamp
- **news_read** — per-device read tracking (mac_address + headline_id), ensures headlines aren't repeated
- **chat_history** — conversation history for Ollama context (Telegram bot uses numeric chat_id, web assistant uses `"web_assistant"`)
- **dns_queries** — raw Pi-hole DNS queries mapped to devices (device_mac, domain, root_domain, query_type, status, client_ip, timestamp)
- **dns_daily_stats** — pre-computed daily aggregates per device+domain for reporting performance
- **domain_categories** — root_domain→category mapping (seeded with 35 common domains; Social Media, Entertainment, Shopping, News, Streaming, Productivity, Gaming)
- **website_alerts** — per-device browsing alerts with domain, dwell threshold, cooldown, alert channels (alexa/telegram/ntfy), Ollama message toggle
- **alert_history** — log of triggered browsing alerts with timestamps and messages
- **migrations** — tracks one-time data migrations

Schema is created/migrated in `bt_db.init_db()`. New columns are added via `_add_column()`. One-time data migrations use `_run_migration()`.

## Core Behaviour

1. Every `scan_interval_seconds` (default 15), perform a BLE scan lasting `scan_duration_seconds` (default 8)
2. Every 4th cycle, also scan Classic Bluetooth via `hcitool inq`
3. Every Nth cycle (configurable), scan WiFi/LAN via ping sweep + ARP
4. Classify devices using manufacturer data, device class, name patterns, and service UUIDs
5. Upsert all discovered devices to SQLite with state `DETECTED`
6. Track state transitions: `LOST→DETECTED` (arrival) and `DETECTED→LOST` (departure after threshold)
7. On transitions for watchlisted devices, send notifications via Telegram
8. Ignore BLE signals weaker than `rssi_threshold` (default -85 dBm)
9. WiFi departure confirmation: before marking a WiFi device as LOST, send targeted unicast pings to its known IP — sleeping phones often respond to direct pings even when missed by broadcast sweeps
10. Arrival cooldown: suppress arrival notifications if the device departed less than `arrival_cooldown_seconds` ago (prevents flapping spam from WiFi sleep/wake cycles)
11. Proximity alerts: for BLE devices with proximity enabled, generate Ollama messages and speak via Alexa when RSSI meets the configured threshold

## Device Linking

Devices can appear with different MACs across scan types (BLE vs WiFi). The `linked_to` column creates groups with one primary and N secondaries. Group-aware behaviour:

- Dashboard merges linked devices into one row
- Arrival notification fires when the **first** member is detected (and no notify-enabled member was already home)
- Departure notification fires when **all** members are lost

## Notifications

### Telegram (primary)
- Arrival/departure notifications sent via `bt_telegram.send_notification()` (async, httpx)
- Bot token and chat ID loaded from environment variables or `/home/pi/.device-radar.env`
- Config fields: `telegram_token_env`, `telegram_chat_id_env`

## Telegram Bot

`bt_telegram.py` runs as a separate service. Features:

- **Presence queries** — intent-routed via regex patterns (no LLM needed):
  - "who's home", "is anyone home", "is Richard home", "where is Laura"
  - "when did Richard arrive", "how long has Laura been away"
  - `/home`, `/devices`, `/lastseen <name>` slash commands
- **Person resolution** — maps names to devices via `person_aliases` config, then fuzzy-matches `friendly_name`, then resolves link groups
- **General chat** — forwarded to local Ollama instance (configurable model, default `qwen2.5:1.5b`)
- **Conversation history** — stored in `chat_history` SQLite table, last N messages sent as context
- **Read-aloud mode** — `/readaloud on [device]` toggles automatic Alexa TTS for chat responses; `/readaloud voice <name>` sets Polly voice; state is in-memory (resets on restart, default off)
- **Authorization** — only responds to the configured `TELEGRAM_CHAT_ID`
- **Graceful degradation** — presence queries work without Ollama; chat returns friendly error on timeout

Presence queries use the REST API (`localhost:8080`) where possible and fall back to direct DB reads for history queries.

## Configuration File

`config.json` in the same directory as the scripts. Gitignored — not deployed via git.

```json
{
  "scan_interval_seconds": 15,
  "scan_duration_seconds": 8,
  "departure_threshold_seconds": 300,
  "rssi_threshold": -85,
  "db_path": "bt_radar.db",
  "web_port": 8080,
  "cleanup_stale_hours": 24,
  "wifi_scan_enabled": true,
  "wifi_scan_interval_cycles": 4,
  "wifi_departure_threshold_seconds": 600,
  "wifi_interface": "wlan0",
  "wifi_subnet": null,
  "devices": {},
  "telegram_bot_enabled": true,
  "telegram_token_env": "TELEGRAM_BOT_TOKEN",
  "telegram_chat_id_env": "TELEGRAM_CHAT_ID",
  "ollama_url": "http://localhost:11434",
  "ollama_model": "qwen2.5:1.5b",
  "ollama_timeout_seconds": 15,
  "conversation_history_length": 10,
  "person_aliases": {
    "richard": "Richard's iPhone",
    "laura": "Laura's iPhone"
  },
  "system_prompt": "You are a helpful assistant running locally on a Raspberry Pi at home. Keep responses concise and conversational — aim for 2-3 sentences unless asked for more detail.",
  "calendar_enabled": true,
  "calendar_url": "https://caldav.icloud.com",
  "calendar_username_env": "APPLE_ID_EMAIL",
  "calendar_password_env": "APPLE_ID_APP_PASSWORD",
  "calendar_cache_minutes": 15,
  "weather_latitude": 52.93,
  "weather_longitude": -1.13,
  "weather_cache_minutes": 30,
  "news_enabled": true,
  "news_headline_count": 3,
  "news_cache_minutes": 15,
  "dns_monitor_enabled": true,
  "dns_poll_interval_seconds": 30,
  "pihole_ftl_db_path": "/etc/pihole/pihole-FTL.db",
  "dns_data_retention_days": 14,
  "alert_check_interval_seconds": 60,
  "alert_session_gap_minutes": 3,
  "dns_aggregation_hour": 3
}
```

Additional scanner config keys:
- `arrival_cooldown_seconds` (default 300) — suppress arrival notifications if device departed less than this many seconds ago

On first run, if `config.json` doesn't exist, a default is created and the script exits with instructions.

## Web Dashboard & REST API

Flask app on port 8080 with dark theme.

### Pages
- **Dashboard** (`/`) — live device list with stats, filters, watchlist/notify toggles, DNS resolver toggle
- **Device Detail** (`/device/<mac>`) — info, settings (including Alexa voice selection, DNS tracking toggle), linking, event history, proximity Alexa config (BLE devices only), calendar selection, BBC News feed selection, browsing alerts (when DNS tracking enabled)
- **Traffic** (`/traffic`) — DNS traffic dashboard with device filter, time range, server-side paginated query table, Chart.js top domains bar chart, traffic stats
- **History** (`/history`) — filterable paginated event log
- **Pairing** (`/pairing`) — pair/unpair via web UI
- **Assistant** (`/assistant`) — Ollama chat interface with "Read on Alexa" toggle, Echo device/voice selection, persistent conversation history

### Key API Endpoints
- `GET /api/devices` — all devices (filters: state, watchlisted, hidden, scan_type, unmerged)
- `GET /api/devices/present` — currently detected devices (merged)
- `GET /api/devices/<mac>` — single device
- `PATCH /api/devices/<mac>` — update device fields
- `GET /api/events` — paginated events (filters: mac, event_type)
- `GET /api/stats` — dashboard counters
- `POST /api/devices/<mac>/link` — link devices
- `POST /api/devices/<mac>/pair` — initiate pairing
- `POST /api/device/<id>/notifications` — toggle notifications
- `GET /api/assistant/history` — web assistant conversation history
- `POST /api/assistant/chat` — send message to Ollama, returns response
- `DELETE /api/assistant/history` — clear web assistant conversation
- `POST /api/assistant/speak` — speak text on Alexa device
- `GET /api/traffic` — paginated DNS queries (filters: device_mac, domain, category, status, time range)
- `GET /api/traffic/stats` — DNS traffic statistics (total queries, unique domains, blocked, top category)
- `GET /api/traffic/top-domains` — top domains by query count with Chart.js data
- `GET /api/traffic/export` — CSV export of DNS queries
- `GET /api/devices/<mac>/alerts` — browsing alerts for a device
- `POST /api/devices/<mac>/alerts` — create a browsing alert
- `PATCH /api/devices/<mac>/alerts/<id>` — update a browsing alert
- `DELETE /api/devices/<mac>/alerts/<id>` — delete a browsing alert
- `GET /api/devices/<mac>/alerts/history` — alert trigger history
- `GET /api/devices/<mac>/dns-activity` — recent DNS activity summary for a device
- `GET /api/dns/categories` — all domain categories
- `POST /api/dns/categories` — add/update a domain category
- `DELETE /api/dns/categories/<domain>` — delete a domain category
- `GET /api/dns/status` — current DNS resolver status (Pi-hole vs router)
- `POST /api/dns/toggle` — toggle DNS resolver between Pi-hole and router via nmcli

## Proximity Alerts

Per-device BLE proximity-triggered Alexa messages. Configured on the device detail page:

- **Proximity enabled** — toggle on/off
- **Proximity level** — RSSI threshold: Very close (>= -50 dBm, ~1m), Near (>= -70, ~3m), Medium (>= -85, ~10m)
- **Interval** — minutes between messages (stored as `proximity_interval`)
- **Alexa device** — which Echo to speak through (falls back to default)
- **Prompt** — Ollama prompt for message generation

Each scan cycle, `bt_alexa.check_proximity_devices()` queries devices with `proximity_enabled=1` and `state=DETECTED`, checks RSSI meets threshold and interval has elapsed, generates a message via Ollama (reuses `generate_encouragement()`), and speaks via the configured Echo. `last_proximity_message` timestamp is stored in the DB to survive restarts.

## Calendar Integration

Per-device Apple Calendar (iCloud CalDAV) context injected into Ollama proximity prompts and welcome-home greetings. Module: `bt_calendar.py`.

Config keys in `config.json`:
- `calendar_enabled` — toggle on/off (default: false)
- `calendar_url` — CalDAV server URL (default: `https://caldav.icloud.com`)
- `calendar_username_env` / `calendar_password_env` — env var names for iCloud credentials (default: `APPLE_ID_EMAIL`, `APPLE_ID_APP_PASSWORD`)
- `calendar_cache_minutes` — how long to cache calendar names and events in memory (default: 15)

Available calendars are discovered automatically from the iCloud account via CalDAV and cached in memory. Per-device calendar selection is stored in the `calendar_calendars` column (JSON array of calendar names), configured via checkboxes in a dedicated Calendar card on the device detail page (visible for all device types). Events for today and tomorrow are fetched and cached in memory, keyed by calendar name set. CalDAV fetches are synchronous (caldav library) wrapped in `run_in_executor`. Credentials stored in `/home/pi/.device-radar.env` as `APPLE_ID_EMAIL` and `APPLE_ID_APP_PASSWORD` (app-specific password from Apple).

## Weather Integration

Current weather conditions from Open-Meteo (free, no API key) are prepended to all Alexa messages alongside the current time. Module: `bt_weather.py`.

Config keys in `config.json`:
- `weather_latitude` / `weather_longitude` — location coordinates for weather lookup (required)
- `weather_cache_minutes` — how long to cache weather data (default: 30)

Weather is fetched in parallel with Ollama calls using `asyncio.gather`. The result is a fixed prefix like "It's 11:30 AM, 10 degrees and partly cloudy." — no LLM involvement. Gracefully degrades to time-only prefix if the API is unavailable or coordinates not configured.

## News Headlines

BBC News RSS headlines appended to all Alexa messages (arrival greetings and proximity alerts) as fixed spoken text. Module: `bt_news.py`.

Config keys in `config.json`:
- `news_enabled` — toggle on/off (default: true)
- `news_headline_count` — how many headlines per alert (default: 3)
- `news_cache_minutes` — how often to re-fetch each RSS feed (default: 15)

20 BBC RSS feeds are hardcoded in `bt_news.BBC_FEEDS` (Top Stories, UK, World, Business, Politics, Technology, Science, Health, Education, Entertainment, England, Sport, Football, Cricket, F1, Rugby Union, Tennis, Golf, Nottm Forest, Leicester City). Per-device feed selection is stored in the `news_feeds` column (JSON array of feed keys), configured via grouped checkboxes on the device detail page. Headlines are stored in `news_headlines` table with guid deduplication (URL fragments stripped, guids prefixed with feed key). Cross-feed title deduplication via `GROUP BY title` in queries prevents the same story from being read twice even when it appears in multiple feeds. Per-device read tracking via `news_read` table ensures headlines aren't repeated — one device hearing a headline does not mark it as read for other devices. Marking a headline as read also marks all other rows with the same title, preventing cross-feed resurface. Headlines older than 7 days are automatically pruned. Feeds are refreshed before each alert to catch breaking news.

## Alexa Voice Selection

Per-device configurable TTS voice using Amazon Polly SSML voices. Stored in the `alexa_voice` column (Polly voice name string). Configured via a dropdown in the Settings card on the device detail page.

Available voices: Brian (British male), Amy (British female), Emma (British female), Matthew (US male), Joanna (US female), Kendra (US female). Default is the standard Alexa voice (empty string).

When a voice is set, the `speak()` function in `bt_alexa.py` wraps the message in SSML: `<speak><voice name='Brian'>...</voice></speak>`. Applied to both arrival greetings and proximity alert messages.

## DNS Traffic Monitoring

Per-device DNS query tracking via Pi-hole integration. Module: `bt_dns.py`. Opt-in per device via `dns_tracking_enabled` column.

### Pi-hole Infrastructure

Pi-hole v6 runs on the Pi as the network DNS server. Key setup requirements:

- **Pi-hole DHCP enabled** — router DHCP disabled; Pi-hole assigns IPs and tells clients to use itself (`192.168.1.162`) as DNS. This ensures queries arrive with individual device IPs rather than the router's IP.
- **Apple encrypted DNS blocked** — Pi-hole blocks `mask.icloud.com`, `mask-h2.icloud.com`, `doh.dns.apple.com` via dnsmasq `address=` directives to force Apple devices to use standard DNS on port 53.
- **IPv6 disabled on router** — prevents router advertising itself as IPv6 DNS server via Router Advertisements, which would cause devices to bypass Pi-hole.
- **Apple device settings** — iCloud Private Relay and "Limit IP Address Tracking" must be disabled per-device for DNS queries to flow through Pi-hole.
- **Private Wi-Fi Address** — Apple devices randomise MAC per network; secondary MACs must be linked to the primary device in Device Radar for attribution.

Pi-hole admin interface at `http://192.168.1.162/admin/`. FTL database at `/etc/pihole/pihole-FTL.db`. DHCP leases at `/etc/pihole/dhcp.leases`. Config at `/etc/pihole/pihole.toml`.

### How It Works

1. **Ingestion loop** polls Pi-hole's FTL SQLite database every `dns_poll_interval_seconds` (default 30) for new queries (falls back from v5 API to direct DB read for Pi-hole v6 compatibility)
2. **ARP resolution** maps Pi-hole client IPs to Device Radar MAC addresses via `/proc/net/arp`, with in-memory cache (300s TTL)
3. **Domain normalisation** via `tldextract` extracts root domains (handles `.co.uk`, CDN subdomains, etc.)
4. **Linked device attribution** — queries from secondary devices (including private Wi-Fi MACs) are attributed to the primary device in the link group, even if the secondary has `dns_tracking_enabled=0`
5. **Daily aggregation** runs once per day at `dns_aggregation_hour` (default 3 AM), pre-computing stats for the reporting dashboard
6. **Data retention** — raw queries older than `dns_data_retention_days` (default 14) are purged during the scanner's cleanup cycle

### Browsing Alerts

Per-device alerts triggered when a device spends sustained time on a specific domain. Configured on the device detail page:

- **Domain** — root domain to monitor (e.g. `instagram.com`)
- **Dwell threshold** — minutes of sustained browsing before triggering (session detection uses gap/threshold logic)
- **Cooldown** — minutes between repeated alerts for the same domain
- **Channels** — alexa, telegram, ntfy (multi-select)
- **Ollama message** — toggle to generate dynamic alert messages via Ollama
- **Custom message** — static fallback message template

Session detection: queries to the same domain within `dns_session_gap_minutes` are grouped into sessions. If a session exceeds the configured threshold, the alert fires.

### DNS Resolver Toggle

Dashboard stat card that shows current DNS resolver mode (Pi-hole or Router) and allows toggling. Uses NetworkManager (`nmcli`) to modify the active WiFi connection's `ipv4.dns` setting:

- **Pi-hole mode**: `ipv4.dns 127.0.0.1` + `ipv4.ignore-auto-dns yes`
- **Router mode**: clears `ipv4.dns` + `ipv4.ignore-auto-dns no` (uses DHCP-provided DNS)

The toggle auto-detects the active WiFi connection name via `nmcli`. Requires `sudo nmcli` permissions.

## WiFi Departure Confirmation

Before marking a WiFi device as LOST, the scanner sends targeted unicast pings to the device's known IP address via `bt_wifi.ping_host()`. Sleeping phones (especially iPhones) often miss broadcast ping sweeps but respond to direct pings. If the device responds, `last_seen` is updated and departure is cancelled. This prevents false departure/arrival flapping for WiFi-tracked devices.

## Discovery Mode

```bash
sudo python3 bt_scanner.py --discover        # BLE + Classic Bluetooth
sudo python3 bt_scanner.py --discover-wifi   # WiFi/LAN devices
```

## Logging

- Python `logging` module
- Default level: INFO
- Format: `%(asctime)s [%(levelname)s] %(message)s` with `%H:%M:%S` time
- Telegram bot uses: `%(asctime)s [%(levelname)s] %(name)s: %(message)s`

## Systemd Services

Three services:

| Service | Unit File | Description |
|---|---|---|
| `bt-scanner` | `bt-scanner.service` | Background scanner |
| `bt-web` | `bt-web.service` | Flask web dashboard |
| `bt-telegram` | `bt-telegram.service` | Telegram bot (loads env from `/home/pi/.device-radar.env`) |

## Error Handling

- Scan failures (BLE, Classic, WiFi) are caught per-type; the cycle continues
- Notification failures (Telegram) are logged but never block
- Ollama timeouts return a friendly fallback message
- Main scanner loop wrapped in try/except to survive any crash
- The bot never crashes from bad Ollama responses or DB errors

## Code Style

- Type hints throughout
- `from __future__ import annotations` for modern annotation syntax
- async/await for all I/O
- Dataclasses for structured data where appropriate
- No global mutable state — encapsulate in classes or module-level caches
- Single-file modules (each service is one .py file)

## Dependencies

```
bleak>=0.21.0
httpx>=0.25.0
flask>=3.0.0
python-telegram-bot>=21.0
python-dotenv>=1.0.0
caldav>=1.3.0
vobject>=0.9.6
tldextract>=5.0.0
```

Install: `pip install -r requirements.txt --break-system-packages`

## File Structure

```
bt-monitor/
├── bt_scanner.py          # Background scanner service
├── bt_web.py              # Flask web dashboard service
├── bt_telegram.py         # Telegram bot service
├── bt_db.py               # SQLite database module
├── bt_alexa.py            # Alexa TTS, welcome greetings, encouragement, proximity alerts
├── bt_classify.py         # Device classification logic
├── bt_pair.py             # Bluetooth pairing helper
├── bt_calendar.py         # Apple Calendar (iCloud CalDAV) integration
├── bt_weather.py          # Current weather via Open-Meteo API
├── bt_news.py             # BBC News RSS headline integration
├── bt_wifi.py             # WiFi/LAN scanning module + targeted ping confirmation
├── bt_dns.py              # DNS traffic monitoring via Pi-hole integration
├── config.json            # User configuration (gitignored)
├── bt_radar.db            # SQLite database (auto-created, gitignored)
├── requirements.txt       # Python dependencies
├── deploy.sh              # Pull latest code and restart services
├── bt-scanner.service     # Systemd unit for scanner
├── bt-web.service         # Systemd unit for web dashboard
├── bt-telegram.service    # Systemd unit for Telegram bot
├── templates/             # Jinja2 templates (dashboard, device, history, pairing, assistant, traffic)
├── static/                # CSS and JS (dark theme)
└── README.md
```

## Deployment

Development in `/var/www/bluetooth/`, production in `/opt/bt-monitor/` (separate git clone). Deploy via `./deploy.sh` which pulls latest and restarts services. Database and config are gitignored.
