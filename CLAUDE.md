# Device Radar

## Project Overview

A multi-module Python application for Raspberry Pi 5 that scans for nearby devices via BLE, Classic Bluetooth, and WiFi/LAN. It tracks device presence in a SQLite database, provides a real-time Flask web dashboard, and sends notifications via Telegram (and optionally ntfy.sh) when watched devices arrive or depart. Includes a Telegram bot with natural language presence queries powered by Ollama.

## Target Environment

- Raspberry Pi 5 running Raspberry Pi OS 64-bit (Bookworm)
- Python 3.11+
- Runs as systemd services under root (required for BLE scanning)
- Development directory: `/var/www/bluetooth/`
- Production directory: `/opt/bt-monitor/`

## Architecture

Three services plus supporting modules:

- **bt_scanner.py** — async background scanner: BLE, Classic Bluetooth, WiFi/LAN discovery, device classification, state tracking, and notifications (ntfy.sh + Telegram)
- **bt_web.py** — Flask web dashboard on port 8080 with REST API
- **bt_telegram.py** — Telegram bot with presence queries, Ollama chat integration, and proactive arrival/departure notifications

Supporting modules:

| Module | Purpose |
|---|---|
| `bt_db.py` | SQLite schema (WAL mode), migrations, and all query/mutation functions |
| `bt_classify.py` | Device type and manufacturer identification from BLE data, device class codes, and name patterns |
| `bt_pair.py` | Bluetooth pairing/unpairing via `bluetoothctl` subprocess |
| `bt_wifi.py` | WiFi/LAN device discovery via ping sweep + ARP table parsing |

## Database

SQLite with WAL mode (`bt_radar.db`). Core tables:

- **devices** — all known devices with state (`DETECTED`/`LOST`), scan info, flags (`is_watchlisted`, `is_notify`, `is_hidden`, `is_paired`), and device linking (`linked_to`)
- **events** — arrival/departure event log with timestamps
- **chat_history** — Telegram bot conversation history for Ollama context
- **migrations** — tracks one-time data migrations

Schema is created/migrated in `bt_db.init_db()`. New columns are added via `_add_column()`. One-time data migrations use `_run_migration()`.

## Core Behaviour

1. Every `scan_interval_seconds` (default 15), perform a BLE scan lasting `scan_duration_seconds` (default 8)
2. Every 4th cycle, also scan Classic Bluetooth via `hcitool inq`
3. Every Nth cycle (configurable), scan WiFi/LAN via ping sweep + ARP
4. Classify devices using manufacturer data, device class, name patterns, and service UUIDs
5. Upsert all discovered devices to SQLite with state `DETECTED`
6. Track state transitions: `LOST→DETECTED` (arrival) and `DETECTED→LOST` (departure after threshold)
7. On transitions for watchlisted devices, send notifications via ntfy.sh and Telegram
8. Ignore BLE signals weaker than `rssi_threshold` (default -85 dBm)

## Device Linking

Devices can appear with different MACs across scan types (BLE vs WiFi). The `linked_to` column creates groups with one primary and N secondaries. Group-aware behaviour:

- Dashboard merges linked devices into one row
- Arrival notification fires when the **first** member is detected (and no notify-enabled member was already home)
- Departure notification fires when **all** members are lost

## Notifications

### Telegram (primary)
- Arrival/departure notifications sent via `bt_telegram.send_notification()` (async, httpx)
- Bot token and chat ID loaded from environment variables or `/home/pi/.openclaw/.env`
- Config fields: `telegram_token_env`, `telegram_chat_id_env`

### ntfy.sh (secondary)
- POST to `{ntfy_server}/{ntfy_topic}` with Title, Priority, Tags headers
- Arrival: tags `house,green_circle` — Departure: tags `wave,red_circle`

## Telegram Bot

`bt_telegram.py` runs as a separate service. Features:

- **Presence queries** — intent-routed via regex patterns (no LLM needed):
  - "who's home", "is anyone home", "is Richard home", "where is Laura"
  - "when did Richard arrive", "how long has Laura been away"
  - `/home`, `/devices`, `/lastseen <name>` slash commands
- **Person resolution** — maps names to devices via `person_aliases` config, then fuzzy-matches `friendly_name`, then resolves link groups
- **General chat** — forwarded to local Ollama instance (configurable model, default `qwen2.5:7b`)
- **Conversation history** — stored in `chat_history` SQLite table, last N messages sent as context
- **Authorization** — only responds to the configured `TELEGRAM_CHAT_ID`
- **Graceful degradation** — presence queries work without Ollama; chat returns friendly error on timeout

Presence queries use the REST API (`localhost:8080`) where possible and fall back to direct DB reads for history queries.

## Configuration File

`config.json` in the same directory as the scripts. Gitignored — not deployed via git.

```json
{
  "ntfy_topic": "nospario_bluetooth_672051",
  "ntfy_server": "https://ntfy.sh",
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
  "ollama_model": "qwen2.5:7b",
  "ollama_timeout_seconds": 15,
  "conversation_history_length": 10,
  "person_aliases": {
    "richard": "Richard's iPhone",
    "laura": "Laura's iPhone"
  },
  "system_prompt": "You are a helpful assistant running locally on a Raspberry Pi at home. Keep responses concise and conversational — aim for 2-3 sentences unless asked for more detail."
}
```

On first run, if `config.json` doesn't exist, a default is created and the script exits with instructions.

## Web Dashboard & REST API

Flask app on port 8080 with dark theme.

### Pages
- **Dashboard** (`/`) — live device list with stats, filters, watchlist/notify toggles
- **Device Detail** (`/device/<mac>`) — info, settings, linking, event history
- **History** (`/history`) — filterable paginated event log
- **Pairing** (`/pairing`) — pair/unpair via web UI

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
| `bt-telegram` | `bt-telegram.service` | Telegram bot (loads env from `/home/pi/.openclaw/.env`) |

## Error Handling

- Scan failures (BLE, Classic, WiFi) are caught per-type; the cycle continues
- Notification failures (ntfy, Telegram) are logged but never block
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
```

Install: `pip install -r requirements.txt --break-system-packages`

## File Structure

```
bt-monitor/
├── bt_scanner.py          # Background scanner service
├── bt_web.py              # Flask web dashboard service
├── bt_telegram.py         # Telegram bot service
├── bt_db.py               # SQLite database module
├── bt_classify.py         # Device classification logic
├── bt_pair.py             # Bluetooth pairing helper
├── bt_wifi.py             # WiFi/LAN scanning module
├── config.json            # User configuration (gitignored)
├── bt_radar.db            # SQLite database (auto-created, gitignored)
├── requirements.txt       # Python dependencies
├── deploy.sh              # Pull latest code and restart services
├── bt-scanner.service     # Systemd unit for scanner
├── bt-web.service         # Systemd unit for web dashboard
├── bt-telegram.service    # Systemd unit for Telegram bot
├── templates/             # Jinja2 templates (dashboard, device, history, pairing)
├── static/                # CSS and JS (dark theme)
└── README.md
```

## Deployment

Development in `/var/www/bluetooth/`, production in `/opt/bt-monitor/` (separate git clone). Deploy via `./deploy.sh` which pulls latest and restarts services. Database and config are gitignored.
