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
| `bt_alexa.py` | Alexa TTS via `alexa_remote_control.sh`, Ollama-generated welcome greetings, encouragement loop, proximity-triggered messages, per-device SSML voice selection, and Obsidian task-reminder loop |
| `bt_tasks.py` | Parses Obsidian notes (Master Task List + Daily Reoccurring Tasks) for uncompleted tasks |
| `bt_calendar.py` | Apple Calendar (iCloud CalDAV) integration — event fetching, caching, and prompt context for proximity/welcome messages |
| `bt_weather.py` | Current weather via Open-Meteo API — fetches temperature and conditions, caches in memory, provides formatted string for Alexa TTS prefix |
| `bt_news.py` | BBC News RSS headline fetching, per-device read tracking, and spoken suffix formatting for Alexa TTS |
| `bt_search.py` | Ollama chat with web search — tool-calling agent loop using Ollama's `/api/chat` endpoint with `web_search` and `web_fetch` cloud tools; provides the async entry point used by the Telegram bot |

*Note: a standalone "Kitkat" memory-agent app (port 8081, `/opt/kitkat/`) used to run alongside Device Radar. It was decommissioned on 2026-10-07 and removed from the Pi; the `kitkat_memories` table, `kitkat_*` config keys, nav link and design docs were removed from this repo.*

## Database

SQLite with WAL mode (`bt_radar.db`). Core tables:

- **devices** — all known devices with state (`DETECTED`/`LOST`), scan info, flags (`is_watchlisted`, `is_notify`, `is_hidden`, `is_paired`), device linking (`linked_to`), `role` (`phone`/`laptop`/`smart_home`/`other`, optional; otherwise implied by the device type) and `person` (optional; otherwise taken from the name), see *People and Roles*, `always_on` (alert if offline, see *Health Watchdog*), proximity alert settings (`proximity_enabled`, `proximity_rssi_threshold`, `proximity_interval`, `proximity_alexa_device`, `proximity_prompt`, `last_proximity_message`), calendar integration (`calendar_calendars` — JSON array of calendar names), news feed selection (`news_feeds` — JSON array of feed keys), and Alexa voice selection (`alexa_voice` — Amazon Polly voice name for SSML)
- **events** — arrival/departure event log with timestamps
- **news_headlines** — fetched BBC RSS headlines with guid deduplication, feed_key, title, published timestamp
- **news_read** — per-device read tracking (mac_address + headline_id), ensures headlines aren't repeated
- **chat_history** — conversation history for Ollama context (Telegram bot, keyed by numeric chat_id; entries older than 7 days are cleaned up on bot start)
- **health_results** / **health_state** — latest result of each health check, and what has already been alerted (see *Health Watchdog*)
- **late_alerts** — which absence has already produced a "later than usual" alert (see *Presence Reports and Predictions*)
- **device_alerts** — which MACs have been announced as new (`kind` 'new') or deleted by the cleanup (`kind` 'forgotten'), see *New Device Alerts*
- **scanner_state** / **scanner_gaps** — scanner heartbeat and recorded downtime periods (used by the cleanup, see *Device Cleanup*)
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
12. Device cleanup: every 100th cycle, `bt_cleanup` hides and deletes stale, unprotected device records (BLE devices rotate random addresses, so one phone creates thousands of one-off rows). See *Device Cleanup*.

## Device Cleanup

BLE devices rotate their random addresses roughly every 15 minutes and each new address becomes a new `devices` row, so unchecked the table grows by 1,000-2,000 rows a day (it reached ~76,000 rows, of which ~99% were one-off records). `bt_cleanup.py` keeps it bounded. It runs from the scanner every 100th cycle, from a "Clean up now" button on the dashboard, and from the command line.

**Two stages** (both skip protected devices and anything currently `DETECTED`):
1. **Hide** — unprotected devices not seen for `cleanup_hide_after_hours` (default 2) get `is_hidden = 1`.
2. **Delete** — unprotected devices not seen for `cleanup_delete_short_lived_after_days` (default 3) if short-lived (last_seen - first_seen under 60 minutes, i.e. a rotated address), or `cleanup_delete_other_after_days` (default 30) otherwise. Their `news_read` rows go too. Deletes run in batches of 500, at most `max_deletes_per_run` (5,000) per scheduled run.

**Protected (never hidden or deleted):** a friendly name, watchlisted / notify / paired / welcome / proximity, linked to or from another device (`linked_to`), or calendar / news / Alexa voice / proximity Alexa settings. An advertised name alone does not protect a device. An `ip_address` alone (an unnamed WiFi device) only keeps it from being *hidden* (hidden devices stay hidden when they return, and WiFi devices often go quiet for hours); it is still deleted after the normal retention period. Event history does not protect a device either: the scanner only logs events for watchlisted devices now, but early versions (3 March 2026) logged them for every device, and treating those as protection left ~100 junk records behind. When a device is deleted its `events` and `news_read` rows are deleted with it. The selection is behaviour-based, not MAC-format-based (the old `hide_stale_random_macs` used the Ethernet locally-administered bit, which is wrong for BLE and missed about half of random addresses; it and `bt_classify.is_random_mac` were removed).

**Downtime:** "unseen for N days/hours" is measured in time the *scanner was running*, not calendar time, so a Pi that was switched off for a month does not make every device look stale (on 7 Oct 2026, after a 29-day outage, the first cleanup treated everything last seen on 8 Sept as 30 days unseen). `bt_cleanup.heartbeat()` is called every scan cycle and `note_scanner_start()` at scanner start-up; any silence over 2 minutes since the last heartbeat is stored as a gap in `scanner_gaps`, and `running_cutoff()` skips gaps when turning "N seconds" into a cut-off time. A cleanup run while the scanner is down (web button / CLI) also treats the current silence as a gap. On first start, past outages are inferred once from stretches of 2+ days with no events (`backfill_gaps`, marker `scanner_gaps_backfill` in `migrations`).

**Safety:** the first real purge copies the database to `backups/` (beside the DB, gitignored via `*.db`) using SQLite's backup API and records this in `migrations` (`cleanup_pre_purge_backup`); if the backup fails nothing is deleted. `cleanup_dry_run: true` makes scheduled runs only log what they would do. Each delete batch runs in one `BEGIN IMMEDIATE` transaction (select + delete), so a device cannot be watchlisted between being selected and being deleted. `foreign_keys=ON` is enforced, so a device's `events` are always deleted before the device. Cleanup commits any pending writes on the connection it is given first (sqlite3's `backup()` loops forever if the source has an open write transaction).

**Config keys** (all optional, in `config.json`): `cleanup_enabled` (true), `cleanup_dry_run` (false), `cleanup_hide_after_hours` (2), `cleanup_delete_short_lived_after_days` (3), `cleanup_delete_other_after_days` (30), `cleanup_short_lived_max_minutes` (60), `cleanup_max_deletes_per_run` (5000), `cleanup_batch_size` (500), `cleanup_backup_before_first_purge` (true). Replaces the old `cleanup_stale_hours`. Scanner settings are read at startup, so restart `bt-scanner` after changing them.

**CLI** (from the project directory): `python3 bt_cleanup.py` previews counts; `python3 bt_cleanup.py --run [--vacuum]` applies now. The manual button/API ignores the per-run cap and compacts (VACUUM) the database after a large purge.

## People and Roles

Module: `bt_people.py`. Home/away for a **person** is decided by their **phone**, not by laptops or smart-home gear that sit on the WiFi all day.

- **Role** (`devices.role`, one of `phone` / `laptop` / `smart_home` / `other`): if empty it is worked out from the device type (`role_from_type`: Phone/iPhone = phone; Laptop/Desktop/Tablet = laptop; Smart Speaker/Smart Plug/IoT/WiFi Router/Printer/TV/... = smart_home; "Network Device"/"Unknown" = no role). So devices named before roles existed need no migration. Settable on the device page ("Role") and set automatically when naming from Telegram.
- **Person** (`devices.person`): if empty it is taken from the friendly name when it has the form "<Name>'s ..." (`person_from_name`: "Laura's MacBook" belongs to `laura`). Keys are lowercase letters/digits/space/hyphen (`normalise_person`). People also come from the keys of `person_aliases` in config.json.
- **Home / away / no phone** (`people_status`): *home* if any phone in the person's group (the phone plus its linked WiFi/Bluetooth records) is `DETECTED`; *away* if they have a phone but none is detected; *no_phone* if no phone is tracked for them. `since` is the last `arrived` (home) or `departed` (away) event of the phone group.
- **Alerts are phone-only** (`notify_allowed`, used at all four notification sites in `bt_scanner._check_arrivals/_check_departures`): a Telegram arrival/departure alert needs `is_notify` on some member of the device's link group **and** (config `notify_phones_only`, default true) a phone in that group. Laptops and smart-home devices with notify switched on stay silent; their events are still recorded. Set `"notify_phones_only": false` to restore the old behaviour.
- **Surfaces**: dashboard "who's home" strip (`/api/people`, `renderPeople` in `static/app.js`); Telegram "who's home" and `/home` answer **by person** (`format_people_summary`); "is Laura home?" answers from the person's phone (`bt_people.best_phone`) and, if none is tracked, says so (`no_phone_message`) instead of falling back to a laptop; `GET /api/devices` adds `effective_role` / `effective_person`; `PATCH /api/devices/<mac>` accepts `role` (validated) and `person` (sanitised).
- **Config-seeded devices**: `devices` in config.json (MAC -> name) are re-created and set watched + notify on **every scanner start** (`bt_scanner.migrate_config_devices`), so deleting such a record in the database does not stick. Remove its entry from config.json as well (done for an old phone on 7 Oct 2026).

## Presence Reports and Predictions

Module: `bt_presence.py`; page `/reports` (open to view, no login needed), `GET /api/reports`, a prediction on each dashboard "who's home" chip (`GET /api/people` adds `prediction`), Telegram questions and `/eta`. Built only from the phone events already recorded (see *People and Roles*): a person's phone records plus their linked records.

- **Sessions** (`build_sessions`): arrive/depart events of all the person's phone records are united into home periods, and absences shorter than `presence_flap_minutes` (20; 45 when any Bluetooth record is involved, `presence_flap_minutes_bluetooth`) are treated as signal flicker and merged (on the first real data 65-91% of raw events were flicker). A session is flagged unreliable if the scanner was off during it (`spans_gap`) or it began within 15 minutes of a restart (`start_ok`), because the scanner only learned of the arrival/departure then.
- **Outings** (`outings_from_sessions`): reliable absences of at least `presence_min_outing_minutes` (60) between two sessions, never overlapping scanner downtime (`scanner_gaps`, see *Device Cleanup*). Every statistic uses outings only.
- **Time of day** is measured on a day that starts at 04:00 local (`shifted`), so a 00:30 return belongs to the evening before and counts as a weekday/weekend by that evening.
- **Reports** (`build_report`): typical leave / return / time out, weekday vs weekend (median, middle half, n; "not enough data" below `presence_min_samples` = 8); hours at home per day counting only observed time (days the scanner watched less than 80% are marked partial); a 14-day timeline (green home, hatched = scanner off); when the house is empty (tracked phones only, finished periods, only since someone was first tracked); a weekday-return trend (last 28 days vs the 28 before, bootstrap 80% interval on the median difference; reported as a trend only if the interval excludes zero); data quality.
- **Data quality** (`quality`): `insufficient` (< 8 usable outings), `noisy`, `usable`, `good`. **`noisy` (more than 4 home periods a day, 85%+ flicker, or weekday return times spread over more than 8 hours) means no predictions are made for that person**; the page says why. A Bluetooth-only phone gets advice to link its WiFi record. Observed on the first run: Lilou and Mathilde (WiFi) good/usable; Richard (Bluetooth flicker, phone going quiet overnight) noisy.
- **Predictions** (`predict_return`, `predict_leave`): from outings of the same day type (weekday/weekend) in the last `presence_history_days` (180), each weighted by recency (`presence_halflife_days` 42). Only return times still ahead of *now* are considered ("not back by 17:30" shifts the estimate later); if almost none remain the person is `overdue` ("later than usual"). Two methods: `time_of_day` (usual return time) and `duration` (leave time + the length of outings that began at a similar time, within 1.5 h); each person gets whichever had the smaller median error in a **walk-forward backtest** (`choose_method`: every past outing predicted from only the outings before it; the page shows the 80%-window hit rate and the error against the naive "guess the usual time"). Statuses: `ready` (median + 80% window + n), `overdue`, `insufficient`, `long_away` (gone more than `presence_long_away_hours`, 18), `unknown` (the scanner was off after they left), `unreliable` (noisy data). Plain statistics, not machine learning: a few dozen samples per person do not support more.
- **Caching:** sessions are rebuilt only when that person's events or the scanner downtime change; the backtest is cached by the outings themselves (not by event count), so Bluetooth flicker that merges away does not re-run it.
- **Telegram:** "when will Lilou be home?", "when does Lilou usually get home / leave?" and `/eta [name]` (everyone who is out). Never answers from noisy data; says so when there is not enough history or the scanner was off.
- **Late alerts (opt-in, off by default):** `late_alerts_enabled` true **and** names in `late_alerts_people` (e.g. `["lilou"]`); sends one Telegram message per absence when the person is later than any similar day plus `late_alerts_margin_minutes` (30); never from noisy data or after scanner downtime; `late_alerts_dry_run` logs only. Table `late_alerts`.
- **Starting history again for one person:** `presence_ignore_before` in config.json, e.g. `{"richard": "2026-10-07"}`, leaves that person's events before that date out of the analysis (they stay in the database). Use it when a period is known to be bad, such as a Bluetooth-only phone that flickered, so reports and predictions restart from clean data instead of staying "noisy" for months. Unknown names and unreadable dates are ignored (a warning is logged). It is part of the cache key, so changing it takes effect at once.
- **Config keys** (optional): `presence_ignore_before`, `presence_flap_minutes`, `presence_flap_minutes_bluetooth`, `presence_min_outing_minutes`, `presence_min_samples`, `presence_halflife_days`, `presence_history_days`, `presence_long_away_hours`, `late_alerts_enabled`, `late_alerts_people`, `late_alerts_margin_minutes`, `late_alerts_dry_run`.
- **Needs a continuously running Pi:** from March to October 2026 the scanner ran for only about 70 of 218 days. Predictions become useful after roughly 4 weeks of continuous running. Only watched devices record events, so a phone's WiFi twin must be watched for its events to count.
- The page reveals when the house is empty. It is deliberately open to view like the rest of the dashboard (owner's decision); to protect it, add `/reports` and `/api/reports` to the login guard.

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
  - `/home`, `/devices`, `/lastseen <name>`, `/unnamed`, `/eta [name]` slash commands, and questions such as "when will Lilou be home?"
- **Person resolution** — maps names to devices via `person_aliases` config, then fuzzy-matches `friendly_name`, then resolves link groups
- **General chat** — forwarded to local Ollama instance (configurable model, default `gemma3:4b`); emoji suppressed via system prompt
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
  "cleanup_enabled": true,
  "cleanup_dry_run": false,
  "cleanup_hide_after_hours": 2,
  "cleanup_delete_short_lived_after_days": 3,
  "cleanup_delete_other_after_days": 30,
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
  "ollama_model": "llama3.2:3b",
  "ollama_timeout_seconds": 60,
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
  "web_search_enabled": true
}
```

Additional scanner config keys:
- `arrival_cooldown_seconds` (default 300) — suppress arrival notifications if device departed less than this many seconds ago

On first run, if `config.json` doesn't exist, a default is created and the script exits with instructions.

## Web Dashboard & REST API

Flask app on port 8080 with dark theme, served by waitress. Reads are open; changes need the dashboard password once one is set (see *Dashboard Login*).

### Pages
- **Dashboard** (`/`) — "who's home" strip (one chip per person, decided by phones), health panel, live device list with stats, per-column filters (the Name filter also matches IP address and manufacturer), watchlist/notify toggles, and a Housekeeping panel (stale-record counts and a "Clean up now" button)
- **Reports** (`/reports`) — per person: typical times, 14-day timeline, hours at home, trend, arrival predictions with their measured accuracy and data quality; plus when the house is empty
- **Device Detail** (`/device/<mac>`) — info, settings (including Alexa voice selection), linking, event history, proximity Alexa config (BLE devices only), calendar selection, BBC News feed selection (all devices)
- **History** (`/history`) — filterable paginated event log
- **Pairing** (`/pairing`) — pair/unpair via web UI
### Key API Endpoints
- `GET /api/devices` — all devices (filters: state, watchlisted, hidden, scan_type, unmerged)
- `GET /api/devices/present` — currently detected devices (merged)
- `GET /api/devices/<mac>` — single device
- `PATCH /api/devices/<mac>` — update device fields (including `role` and `person`)
- `GET /api/events` — paginated events (filters: mac, event_type)
- `GET /api/stats` — dashboard counters
- `GET /api/people` — home/away per person, decided by phones (see *People and Roles*)
- `GET /api/health` — latest health watchdog results (see *Health Watchdog*)
- `GET /api/reports` — presence reports, trends and predictions (see *Presence Reports and Predictions*)
- `GET /api/cleanup/preview` — what a cleanup would do (total, protected, to_delete, to_hide, DB size, settings); changes nothing
- `POST /api/cleanup/run` — hide + delete stale devices now (body `{"dry_run": bool}`; real run backs up first, ignores the per-run cap, compacts the DB)
- `POST /api/devices/<mac>/link` — link devices
- `POST /api/devices/<mac>/pair` — initiate pairing
- `POST /api/device/<id>/notifications` — toggle notifications

## Proximity Alerts

Per-device BLE proximity-triggered Alexa messages. Configured on the device detail page:

- **Proximity enabled** — toggle on/off
- **Proximity level** — RSSI threshold: Very close (>= -50 dBm, ~1m), Near (>= -70, ~3m), Medium (>= -85, ~10m)
- **Interval** — minutes between messages (stored as `proximity_interval`)
- **Alexa device** — which Echo to speak through (falls back to default)
- **Prompt** — Ollama prompt for message generation

Each scan cycle, `bt_alexa.check_proximity_devices()` queries devices with `proximity_enabled=1` and `state=DETECTED`, checks RSSI meets threshold and interval has elapsed, generates a message via Ollama (reuses `generate_encouragement()`), and speaks via the configured Echo. `last_proximity_message` timestamp is stored in the DB to survive restarts. A proximity message is only spoken if the device's Bluetooth record was actually *seen* within `departure_threshold_seconds` (`_ble_sighting_is_fresh`): `last_rssi` is never cleared, and a record can stay `DETECTED` for hours because a linked WiFi record is still home, so without this check a stale strong reading could trigger the hourly message while the person is elsewhere.

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

## Web Search

Telegram bot web search via Ollama's cloud search API. Module: `bt_search.py`.

The Telegram bot (`bt_telegram.py`) uses `bt_search.chat_with_search_async()` for all Ollama chat interactions. (The web dashboard's Assistant page, which used a synchronous variant, was removed on 2026-10-07.) The module uses Ollama's `/api/chat` endpoint (via the `ollama` Python library) with tool calling support. When web search is enabled, the model can autonomously decide to call `web_search` or `web_fetch` tools, which hit Ollama's cloud API at `ollama.com`. The model inference remains local.

Config keys in `config.json`:
- `web_search_enabled` — toggle on/off (default: false)
- `ollama_think` — enable thinking mode for thinking models like qwen3 (default: false; disabled by default because thinking is extremely slow on CPU-only devices)

Environment variables in `/home/pi/.device-radar.env`:
- `OLLAMA_API_KEY` — API key for Ollama cloud web search (required when `web_search_enabled` is true)

Requires a tool-calling-capable Ollama model (e.g. `llama3.2:3b`). Models that don't support tools (e.g. `gemma3:4b`) will gracefully fall back to chat without search. Tools are only passed to the model when the user's message contains search-related keywords (e.g. "news", "latest", "current", "war", "search") — this keeps regular chat fast (~8s) while search queries take longer (~2 min) due to multiple inference passes and the cloud search call. The agent loop runs up to 5 tool-call iterations per query. If the `ollama` Python package is not installed, the module falls back to raw `httpx` calls to `/api/generate` (no tool calling). The web assistant UI shows a "Searched the web" badge when search was used. The Telegram bot prefixes responses with `[searched the web]` when search was invoked.

`bt_alexa.py` continues to use raw `httpx` calls to `/api/generate` for greeting and encouragement generation (no web search needed for those use cases).

## Alexa TTS Chunking

`bt_alexa.speak()` speaks via `alexa_remote_control.sh`, which delivers text through Alexa's Simon Says skill (`Alexa.Speak` with `textToSpeak`). That skill rejects utterances above ~500 chars with the error "Sorry I'm having trouble accessing your Simon Says skill right now" — and critically, the `alexa_remote_control.sh` exit code is still 0 because the rejection happens Alexa-side after the request is accepted. To avoid this, `speak()` chunks long messages on sentence boundaries via `_chunk_tts()` (limit `_MAX_TTS_CHARS = 350`) and sends each chunk as a separate `speak:` invocation with a `_TTS_CHUNK_GAP_SECS = 25` second gap in between — long enough for Alexa to finish speaking the previous chunk (a 350-char chunk takes ~18–22 seconds to speak, and submitting the next `speak:` while Alexa is still mid-sentence causes the first utterance to be cut off and eventually trips the Simon Says error anyway). If a single sentence exceeds the limit, the chunker falls back to word-boundary splitting. When an SSML voice is set, each chunk is individually wrapped in `<speak><voice>…</voice></speak>` tags.

## Obsidian Task Reminders

Per-Echo toggle that speaks a reminder of today's outstanding Obsidian tasks on a configurable interval. Module: `bt_tasks.py`; loop in `bt_alexa.run_task_reminder_loop()`.

Two files are read each cycle:

* **Due today** — read from the **Master Task List** (default `/home/nospario/ObsidianVaults/Main/3. Todo Lists/MASTER TASK LIST.md`, overridable via `obsidian_master_task_path`). Only uncompleted tasks **without** the `#habit` tag whose `📅` due date is **exactly today** are included. Overdue items and undated backlog are deliberately excluded.
* **Daily habits** — read from **today's Daily Note** in `obsidian_daily_notes_dir` (default `/home/nospario/ObsidianVaults/Main/1. Journal`, filename `YYYY-MM-DD.md`). Obsidian's Daily Notes plugin creates the note from the Daily Template each day; the template contains the `#habit`-tagged tasks. Every uncompleted `- [ ]` line tagged `#habit` (matched as a whole word, case-insensitive) is a habit reminder. If the note hasn't been created yet, the habits list is empty and habit reminders skip that cycle.

A separate cron (`bt_tasks.py complete-habits`) runs at 23:55 each day to mark any still-unchecked `#habit` lines in today's Daily Note as `- [x]` with `✅ YYYY-MM-DD`, so forgotten habits don't show as overdue tomorrow. Tomorrow's note is generated fresh from the template by Obsidian, so we don't need to stamp new instances.

A separate `bt_alexa.run_telegram_habit_reminder_loop()` (spawned by the scanner, independent of `alexa_enabled`) sends a Telegram message on the hour listing outstanding habits. Controlled by `telegram_habit_reminders_enabled` (default true), `telegram_habit_start_hour` (default 8), `telegram_habit_end_hour` (default 22). Skipped on the hour if no habits remain outstanding.

Each hourly reminder (and the on-demand `/habits` command) attaches an **inline keyboard** with one tap-to-toggle button per habit. The keyboard shows **all** habits for the day — completed ones are prefixed with ✓ so an accidental tap can be undone without leaving Telegram. Callback data is `habit:<sha256_16>` of the cleaned description — stable across processes so the scanner (sender) and bot (receiver) don't need shared state. Tapping a button fires `_on_habit_callback` in `bt_telegram.py`: it re-reads today's Daily Note via `bt_tasks.get_all_habits()`, finds the habit whose hash matches, and calls `bt_tasks.set_habit_done_state(done=not currently_done)` to flip state — adding ``✅ YYYY-MM-DD`` when completing, stripping it when uncompleting. The message is then edited in place with the refreshed summary + keyboard. The bot polls with `allowed_updates=["message", "callback_query"]` so button taps flow through. The hourly reminder still skips sending when no habits are outstanding (avoids notification spam), but when it does send, the keyboard includes all habits so any can be toggled.

Each morning, `bt_alexa.run_telegram_habit_summary_loop()` sends a retrospective summary of yesterday's habit completion via Telegram. Uses `bt_tasks.summarize_habits()` to count completed vs incomplete `#habit` lines in yesterday's Daily Note, then generates a warm message via Ollama (celebratory one-liner at 100%, otherwise names the missed habits and the completion percentage). Coverage check via `_incomplete_habits_covered()` falls back to a deterministic message if the LLM drops habits. Controlled by `telegram_habit_summary_enabled` (default true), `telegram_habit_summary_hour` (default 7), `telegram_habit_summary_minute` (default 0). Silently skipped if yesterday's Daily Note doesn't exist or contains no `#habit` lines.

Each Alexa bundle re-reads the current task state just before generating its message and drops items that have been completed (in Obsidian) since the cycle started; fully-empty bundles are skipped so the user never hears about a habit they've just ticked off.

The parser recognises Obsidian Tasks plugin emoji syntax: `- [ ]`/`- [x]` state, `📅` due, `⏳` scheduled, `🛫` start, `✅` done, `🔁` recurrence, plus priority markers. Wikilinks, `#tags` and all of those emojis are stripped before the description is spoken. Missing files are logged and treated as empty lists — so it's safe to configure a path before the note has synced from Obsidian.

Echo-device fields in `echo_devices`: `tasks_enabled`, `tasks_interval` (minutes, default 120), `last_tasks_message`. Configured via the Alexa page (`/alexa`) alongside Encourage Mode.

Each cycle the outstanding tasks are split into bundles by `_bundle_tasks()` — at most `tasks_max_per_bundle` items each (default 4). Due-today items come first so today's time-critical commitments are mentioned in the earlier bundles; daily habits fill the remaining slots, which means a bundle may mix both groups. Each task inside a bundle carries a `group` annotation ("due_today" or "daily") and `_generate_bundle_message()` passes those annotations to Ollama so mixed bundles can be framed naturally. `_bundle_covered()` verifies every task appears in the output; if coverage fails or Ollama errors, the bundle falls back to a deterministic read via `_bundle_fallback()` that names due-today and daily items distinctly so nothing is dropped. Because bundles are small and targeted, each message comfortably fits inside a single Simon Says utterance — the global `_chunk_tts()` splitter in `speak()` is a belt-and-braces safety net.

Bundles are spoken with an inter-bundle gap computed per-cycle: `tasks_bundle_minutes` (default 10) divided across the gaps between bundles, with a floor of `tasks_min_bundle_gap_seconds` (default 30) so Alexa has time to finish each utterance before the next one is submitted. Because bundle count scales with task count (more tasks → more bundles → smaller gaps), the floor prevents overlapping speech when the total list grows large.

**Interval semantics:** `last_tasks_message` is stamped after the *final* bundle in a cycle is dispatched, so the next cycle fires `tasks_interval` minutes after the previous cycle *finished*, not when it started. This makes the configured interval the actual quiet period between reminders. The timestamp is also updated when both lists are empty so the loop doesn't re-check every minute.

## Alexa Voice Selection

Per-device configurable TTS voice using Amazon Polly SSML voices. Stored in the `alexa_voice` column (Polly voice name string). Configured via a dropdown in the Settings card on the device detail page.

Available voices: Brian (British male), Amy (British female), Emma (British female), Matthew (US male), Joanna (US female), Kendra (US female). Default is the standard Alexa voice (empty string).

When a voice is set, the `speak()` function in `bt_alexa.py` wraps the message in SSML: `<speak><voice name='Brian'>...</voice></speak>`. Applied to both arrival greetings and proximity alert messages.

## WiFi Departure Confirmation

Before marking a WiFi device as LOST, the scanner sends targeted unicast pings to the device's known IP address via `bt_wifi.ping_host()`. Sleeping phones (especially iPhones) often miss broadcast ping sweeps but respond to direct pings. If the device responds, `last_seen` is updated and departure is cancelled. This prevents false departure/arrival flapping for WiFi-tracked devices.

## New Device Alerts

Module: `bt_newdevice.py`. When the WiFi scan finds a MAC that was never stored, the scanner (`_announce_new_wifi_device`) sends one Telegram message: hostname, IP, MAC, vendor (or an explanation that it uses a **private address**), a "Looks like" guess, and buttons **Phone / Laptop / Smart home / Ignore**. This replaced the old bare "📡 name detected" message.

- **Naming flow** (handlers in `bt_telegram.py`: `_on_newdevice_callback`, `_maybe_handle_naming`): tapping a role asks "Reply with a name"; the next message from the authorised chat is the name (send `cancel` to abort; expires after 15 min; `_handle_message` checks this before presence/chat routing). `bt_newdevice.apply_role` then sets `friendly_name`, `device_type`, `role` and watch/notify: **Phone** = named + watched + notify on (phones drive home/away alerts); **Laptop** (laptops/tablets) and **Smart home** = named only; **Ignore** = hidden (the cleanup removes it later). It never overwrites an existing friendly name. Callback payloads are `nd:<phone|laptop|home|ignore>:<MAC>` and are validated by `parse_callback`.
- **`/unnamed`** lists connected, visible, unnamed WiFi devices (not linked secondaries), at most 8, each with the same buttons. Use it for devices that connected before the alert existed or while Telegram was unreachable.
- **Announced once per MAC** (`device_alerts` table, `kind='new'`). The cleanup records the WiFi devices it deletes as `kind='forgotten'` (inside the delete transaction, `bt_newdevice.forget`, which must not commit) so a returning device is not "new" again. BLE devices are never announced (their addresses rotate every ~15 minutes).
- **Hostname guess** (`guess_action`): whole-word match on hostname parts (so "Sterling" is not "ring"), then vendor (TP-Link, Ring, Amazon, ...). Only a hint (the guessed button is starred and listed first).
- **Config keys** (all optional): `new_device_alerts_enabled` (true), `new_device_alerts_dry_run` (false: log "would announce" only), `new_device_alerts_max_per_hour` (6; extra devices are recorded as `suppressed` and found via `/unnamed`).

### Vendor lookup

`bt_wifi.lookup_oui_vendor()` reads the full IEEE registry (`/usr/share/ieee-data/oui.txt`, apt package **`ieee-data`**, ~35,800 vendors, loaded once on first use) and falls back to the short built-in `OUI_VENDORS` table if the file is missing. A **locally administered** address (bit 0x02 of the first octet, `bt_wifi.is_private_mac`) is a private/randomised WiFi address and has no vendor; the dashboard shows "Private address" in the Manufacturer column for WiFi-only devices (`manufacturerLabel` in `static/app.js`, searchable). The locally administered test is meaningful for WiFi/Ethernet MACs only, not for Bluetooth LE random addresses. Vendor names are filled in on the next WiFi scan.

## Backups

Module: `bt_backup.py`. Nightly copy of the database (and `config.json`) to `<external drive>/device-radar-backups/` (default `/mnt/external`, `health_external_path`). Runs as a loop in the **Telegram bot process** (next to the health watchdog; checks every 10 min, first look 2 min after start).

- **How:** SQLite's online backup API (a consistent snapshot while the scanner keeps writing), written under a `.partial` name, verified (`PRAGMA integrity_check` + the `devices` table must exist), then renamed into place. The copy is converted to a plain single-file database (`journal_mode=DELETE`) so it has no `-wal`/`-shm` side files. The secrets file (`.device-radar.env`) is never copied.
- **When:** the first backup ever runs straight away; after that once per calendar day at/after `backup_hour:backup_minute` (03:30). If the Pi was off at that time, it catches up at the first opportunity (`is_due`). Skipped (with a warning) if the drive is not mounted. One sequential write a night suits the spinning USB disk.
- **Retention** (`prune`): the newest backup of each of the last `backup_keep_daily` (7) days that have one, plus the newest of each of the last `backup_keep_weekly` (4) ISO weeks; the matching `config-*.json` goes with it. With fewer than 7 days of history nothing is deleted, so an old backup is kept until newer ones push it out. Unrelated files in the folder are never touched.
- **Health:** the watchdog's "Database backup" check warns after 36 h without a backup and fails after 72 h (needs 3 consecutive passes), and warns if the drive is not mounted.
- **CLI** (run from the project directory as root): `python3 bt_backup.py` (back up now), `--list`, `--verify` (opens every backup and runs `integrity_check`).
- **Restore:** `sudo systemctl stop bt-scanner bt-web bt-telegram`, copy a `bt_radar-*.db` over `/opt/bt-monitor/bt_radar.db`, delete `bt_radar.db-wal` / `bt_radar.db-shm` beside it, start the services.
- **Config keys** (optional): `backup_enabled` (true), `backup_hour` (3), `backup_minute` (30), `backup_keep_daily` (7), `backup_keep_weekly` (4).
- The backups are **not encrypted** and hold your household's device history, so treat the drive accordingly.

## Dashboard Login

Module: `bt_auth.py`, wired into `bt_web.py`. **Reading the dashboard is open to the network; anything that changes data needs the password** (editing devices, pairing, linking, Echo settings, "Clean up now": every non-GET route). The Telegram bot only reads (`_api_get`), so it is unaffected.

- **Until a password is set the dashboard is open exactly as before**, and the health watchdog warns ("Dashboard password") until one is set. Set it on the Pi, in a terminal, as root: `sudo python3 /opt/bt-monitor/bt_auth.py set-password` (also `status`, `remove`). Min 8 characters. No restart is needed: the file is re-read when it changes.
- **Storage:** `web_auth.json` beside the code (mode 0600, git-ignored): a salted **scrypt hash** (Werkzeug) and the secret that signs login cookies. Setting a password always makes a new secret, which logs everyone out. The password itself is never stored or logged.
- **Guard:** `require_login_for_changes` (`before_request`) returns `401 {"error": "login required", "login": "/login"}` for any non-GET request without a login, except `/login` and `/logout`. The test suite walks `app.url_map` and checks every non-GET route, so a new write endpoint cannot be added without the guard covering it. The browser helper `api()` in `static/app.js` turns a 401 into a redirect to `/login?next=<current page>`.
- **Login** (`/login`): throttled to 5 wrong passwords per client IP per 15 minutes (`LoginThrottle`, in memory; the 6th attempt gets 429 even with the right password). `next` is restricted to paths on this site (`safe_next`: no `//host`, no scheme, no backslashes or newlines). Cookie: `HttpOnly`, `SameSite=Strict` (this is the CSRF protection; no tokens), 30 days. `/logout` is a POST form shown in the nav bar.
- **Session signing** uses a custom session interface (`_AuthSessionInterface`) that looks up the secret when each request's session is opened. (Setting `app.secret_key` from a request handler was tried first and is wrong: Flask opens the session *before* `before_request`, so a cookie signed with the old secret was accepted for one more request after a password change or removal.)
- **Headers** on every response: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`. No CSP (the templates use inline scripts and `onclick`).
- **Server:** `bt_web.main()` serves with **waitress** (apt `python3-waitress`, 4 threads) instead of Flask's development server; if waitress is missing it logs a warning and falls back to the development server.
- **Config:** `health_check_web_password` (default true; set false to silence the watchdog warning if you deliberately want an open dashboard).

## Health Watchdog

Module: `bt_health.py`. A loop in the **Telegram bot process** (`bt-telegram`, started in `_post_init`, first pass 45 s after start) runs the checks every `health_interval_seconds` (300) and stores the latest result of each in `health_results`; alert bookkeeping is in `health_state`. It lives in the bot process, not the scanner, so it still reports if the scanner dies.

**Checks:** scanner heartbeat (`scanner_state`; warn after 3 min, fail after 10), nightly **backup** freshness (see *Backups*), **dashboard password** set (see *Dashboard Login*), systemd services (`health_services`; default bt-scanner, bt-web, bt-telegram, pihole-FTL, ollama, obsidian-sync, nftables, ssh), Ollama API, **calendar login** (`bt_calendar.check_login`, every 6 h; a rejected login fails immediately, being unreachable only warns), disk space of `/` and the external drive (warn 85%, fail 95%), external drive mounted, CPU temperature (warn 80 °C, fail 85) and throttling/under-voltage *right now*, clock sync, reboot required, pending updates (`apt-get -s upgrade`, daily, warn at 50), database `PRAGMA quick_check` (daily), and **always-on devices**.

**Always-on devices** (`devices.always_on`, "Always on" checkbox on the device page): a device is reported offline when it is not `DETECTED` and has not been seen for `health_offline_minutes` (20) of **scanner running time** (`bt_cleanup.running_cutoff`, same rule as the cleanup), so a scanner restart or a powered-off Pi never makes everything look offline.

**Quiet by design** (`process_results`): a problem must be seen on `confirm` consecutive passes (2; 1 for a revoked calendar login, a reboot request, a failed database check and offline devices); one Telegram message covers everything that changed in a pass; a "back to normal after N" message follows; a failure that persists is repeated at most every `health_reminder_hours` (24); a drop from fail to warn is not re-announced. Slow checks (calendar, updates, database) are reused between their runs.

**"Back online" message**: when the scanner starts after 30+ minutes of downtime (the gap recorded by `bt_cleanup.note_scanner_start`) it sends "Device Radar is back online after N offline", retrying while the network comes up (`announce_restart`).

**Where it shows**: dashboard health panel (`GET /api/health`, `renderHealth`; opens itself when something is wrong and remembers if you open/close it; turns amber if the watchdog stops reporting), and the `/status` Telegram command.

**Config keys** (all optional): `health_alerts_enabled` (true), `health_alerts_dry_run` (false: log only), `health_interval_seconds`, `health_reminder_hours`, `health_services`, `health_disk_warn_percent` / `health_disk_fail_percent`, `health_temp_warn_c` / `health_temp_fail_c`, `health_updates_warn`, `health_offline_minutes`, `health_external_path` (`/mnt/external`). Changes are picked up on the next pass without a restart.

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
- Tests live in `tests/` (stdlib `unittest`, temp databases; `test_dashboard_js.py` runs dashboard JS helpers through node); run `python3 -m unittest discover -s tests -v` before deploying (cleanup logic, schema, and web page/endpoint smoke tests; tests must never read the real `config.json` or touch the network)

## Dependencies

```
bleak>=0.21.0
httpx>=0.25.0
flask>=3.0.0
python-telegram-bot>=21.0
python-dotenv>=1.0.0
caldav>=1.3.0
vobject>=0.9.6
ollama>=0.4.0
```

Install: `pip install -r requirements.txt --break-system-packages`

System packages: `sudo apt install ieee-data python3-waitress` (`ieee-data`: offline WiFi vendor list used by `bt_wifi.lookup_oui_vendor`, without it only ~285 built-in vendors are recognised; `python3-waitress`: production web server for the dashboard, without it Flask's development server is used).

## File Structure

```
bt-monitor/
├── bt_scanner.py          # Background scanner service
├── bt_web.py              # Flask web dashboard service
├── bt_telegram.py         # Telegram bot service
├── bt_db.py               # SQLite database module
├── bt_cleanup.py          # Stale device cleanup (hide/delete, protection rules, backup, CLI)
├── bt_newdevice.py        # New WiFi device alerts + tap-to-name from Telegram
├── bt_people.py           # People, device roles, phone-only alerts, who's home
├── bt_presence.py         # Presence analytics: sessions, reports, trends, arrival predictions, late alerts
├── bt_health.py           # Health watchdog: checks, quiet alerting, always-on devices, restart message
├── bt_backup.py           # Nightly database backup to the external drive (verify, retention, CLI)
├── bt_auth.py             # Dashboard password (scrypt hash, cookie secret, login throttle, CLI)
├── web_auth.json          # Dashboard password hash + cookie secret (created by bt_auth.py, gitignored, 0600)
├── bt_alexa.py            # Alexa TTS, welcome greetings, encouragement, proximity alerts
├── bt_classify.py         # Device classification logic
├── bt_pair.py             # Bluetooth pairing helper
├── bt_calendar.py         # Apple Calendar (iCloud CalDAV) integration
├── bt_weather.py          # Current weather via Open-Meteo API
├── bt_news.py             # BBC News RSS headline integration
├── bt_tasks.py            # Obsidian Master Task List parser
├── bt_search.py           # Ollama chat with web search (tool calling agent loop)
├── bt_wifi.py             # WiFi/LAN scanning module + targeted ping confirmation
├── config.json            # User configuration (gitignored)
├── bt_radar.db            # SQLite database (auto-created, gitignored)
├── requirements.txt       # Python dependencies
├── deploy.sh              # Pull latest code and restart services
├── bt-scanner.service     # Systemd unit for scanner
├── bt-web.service         # Systemd unit for web dashboard
├── bt-telegram.service    # Systemd unit for Telegram bot
├── tests/                 # unittest suite (python3 -m unittest discover -s tests)
├── templates/             # Jinja2 templates (dashboard, device, history, pairing, alexa)
├── static/                # CSS and JS (dark theme)
└── README.md
```

## Deployment

Development in `/var/www/bluetooth/`, production in `/opt/bt-monitor/` (separate git clone). Deploy via `./deploy.sh` which pulls latest and restarts services. Database and config are gitignored.
