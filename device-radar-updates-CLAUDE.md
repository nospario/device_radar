# Device Radar Telegram Bot + Ollama Integration

## Project Overview

Extend the existing Device Radar system on a Raspberry Pi 5 to add a Telegram bot that integrates with:
1. **Device Radar** — the existing BLE/WiFi/Classic Bluetooth presence detection system at `/opt/bt-monitor/`
2. **Ollama** — local LLM running on the Pi (Qwen 2.5 7B) for natural language chat and response formatting

The bot should handle two types of interaction:
- **Presence queries** — questions about who's home, when someone arrived/departed, device status, etc. These are answered by querying Device Radar data directly, with Ollama optionally formatting the response into natural language.
- **General chat** — everything else gets forwarded to Ollama for a conversational response.

Additionally, the bot should send **proactive notifications** to Telegram when Device Radar detects arrivals or departures. ntfy.sh support has been removed — Telegram is now the sole notification channel.

## Target Environment

- Raspberry Pi 5 (16GB RAM, 128GB storage)
- Raspberry Pi OS 64-bit (Bookworm)
- Python 3.11+
- Ollama running locally at `http://localhost:11434`
- Model: `qwen2.5:7b`
- Device Radar (bt-monitor) running as a systemd service at `/opt/bt-monitor/`

## Existing Codebase Context

**Important:** Device Radar already has a full SQLite database layer. Do NOT create new schemas — use the existing one.

### Existing database (`bt_radar.db`, managed by `bt_db.py`)

**`devices` table** — already tracks all device state:
- `mac_address` (PRIMARY KEY), `advertised_name`, `friendly_name`, `device_type`, `manufacturer`
- `state` — values are `"DETECTED"` or `"LOST"` (not present/absent)
- `last_seen`, `first_seen` — Unix timestamps
- `last_rssi`, `scan_type` (`"BLE"`, `"Classic"`, `"WiFi"`, `"BLE+Classic"`)
- `is_watchlisted`, `is_notify`, `is_hidden`, `is_paired`
- `linked_to` — MAC of primary device (for device grouping)
- `ip_address` — for WiFi-discovered devices

**`events` table** — already logs arrivals and departures:
- `mac_address`, `event_type` (`"arrived"` / `"departed"`), `timestamp` (Unix)
- `device_name`, `device_type`, `rssi`
- Indexed on `mac_address` and `timestamp`

**Existing helper functions in `bt_db.py`:**
- `get_all_devices_merged()` — returns devices with linked groups merged
- `get_events(mac=None, event_type=None, limit=50, offset=0)` — paginated event queries
- `get_stats()` — dashboard counters (total, detected, lost, watchlisted, events_today)
- `get_device(mac)` — single device lookup
- `get_link_group(mac)` — returns `{primary, secondaries}` for grouped devices

**Existing REST API (`bt_web.py`, Flask on port 8080):**
- `GET /api/devices` — all devices (with filters: state, watchlisted, hidden, scan_type)
- `GET /api/devices/present` — only DETECTED devices
- `GET /api/events` — paginated event history
- `GET /api/stats` — dashboard statistics

**Existing Telegram integration in `bt_scanner.py`:**
- `send_telegram_alert(device_name, event)` already sends arrival/departure messages via Telegram Bot API
- Uses synchronous `requests.post()` — should be migrated to async `httpx` for consistency
- Bot token and chat ID loaded from environment or `/home/pi/.openclaw/.env`

### Database path

The database path is configured via `config.json` `db_path` key (default: `bt_radar.db`, relative to working directory). In production this resolves to `/opt/bt-monitor/bt_radar.db`.

## Architecture

The bot will be added as a new module within the existing `/opt/bt-monitor/` project to share config, DB helpers, and avoid path coordination issues.

```
Telegram
   ↕
bt_telegram.py (async Python module)
   ├── Intent Router
   │   ├── Presence queries → bt_db.py query helpers → (optional) Ollama formatter
   │   └── General chat → Ollama /api/chat with conversation history
   └── Proactive Notifications
       └── bt_scanner.py calls bot's notify function on arrival/departure events
```

### Key Design Principles

- **Do the heavy lifting in code, not the LLM.** For presence queries, Python queries the database, computes the answer, and only uses Ollama to format a natural language sentence. The LLM should never be asked to reason about raw event data.
- **Keep Ollama calls to a minimum.** One call per interaction maximum. For presence queries where the answer is simple (e.g. "yes, Laura is home"), skip Ollama entirely and return a direct response.
- **Conversation history for general chat.** Maintain a rolling history of the last 10 messages per chat in SQLite (add a `chat_history` table via the existing migration system in `bt_db.py`). This survives service restarts.
- **Send a "typing" indicator** while waiting for Ollama responses so the user knows the bot is working.
- **Graceful degradation.** If Ollama is down or slow, presence queries should still work (just return the factual answer without LLM formatting). General chat should return a friendly error message.

## Intent Routing

Use keyword/pattern matching to identify presence queries. Do NOT use Ollama for intent detection — it's too slow and unnecessary.

Patterns to match (case-insensitive, flexible phrasing):
- "is anyone home" / "who's home" / "who is home" / "anyone in"
- "is [name] home" / "is [name] in" / "where is [name]"
- "when did [name] get home" / "when did [name] arrive" / "when did [name] leave"
- "how long has [name] been home/away/out"
- "what devices are home/connected/present"
- "last seen" / "device status"
- `/home` — slash command: quick summary of who's home
- `/devices` — slash command: list all tracked devices and their status
- `/lastseen [name]` — slash command: when a specific device was last detected

### Name-to-device resolution

Use the existing device linking system in `bt_db.py` to map people to devices. Each person's primary device has a `friendly_name` (e.g. "Richard's iPhone") and secondary devices are linked via `linked_to`. The bot should:

1. Match user input names against `friendly_name` values (fuzzy/substring match)
2. When a person is queried, check the state of their entire link group via `get_link_group()`
3. A person is "home" if any device in their group has `state = "DETECTED"`

Optionally, a `person_aliases` map can be added to config for shorthand names:
```json
{
  "person_aliases": {
    "richard": "Richard's iPhone",
    "laura": "Laura's iPhone"
  }
}
```
The values should reference a device's `friendly_name` or `mac_address`. The link group is then resolved from that device.

Everything that doesn't match a presence pattern goes to Ollama for general chat.

## Device Radar Integration

### Reading Device Radar State

Use the existing REST API (Flask on `localhost:8080`) for presence queries. This avoids SQLite write-locking conflicts between the bot and scanner processes.

**Endpoints to use:**
- `GET http://localhost:8080/api/devices/present` — all currently detected devices
- `GET http://localhost:8080/api/devices` — all devices with optional filters
- `GET http://localhost:8080/api/events?mac=XX:XX&limit=10` — recent events for a device
- `GET http://localhost:8080/api/stats` — summary counters

For queries the API doesn't cover (e.g. "when did Laura arrive today"), open a **read-only** SQLite connection to `bt_radar.db` and query the `events` table directly. Use WAL mode (already enabled) so reads don't block the scanner's writes.

### Proactive Notifications

Replace the existing `send_telegram_alert()` in `bt_scanner.py` with a call to the bot module's notification function. This keeps all Telegram logic in one place and uses async `httpx` instead of synchronous `requests`.

The scanner already triggers notifications at the right moments (arrival/departure with group-awareness), so the bot just needs to expose an async function that `bt_scanner.py` can call:

```python
# bt_telegram.py
async def send_notification(device_name: str, event: str) -> None:
    """Send arrival/departure notification to Telegram."""
```

Format messages as:
- Arrival: "📱 Richard's iPhone detected"
- Departure: "👋 Laura's iPhone departed"

These are sent to the configured Telegram chat ID.

## Ollama Integration

### Chat Endpoint

Use Ollama's `/api/chat` endpoint for conversational responses:

```
POST http://localhost:11434/api/chat
{
  "model": "qwen2.5:7b",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant running on a Raspberry Pi. Keep responses concise — aim for 2-3 sentences unless the user asks for more detail."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."},
    ...
  ],
  "stream": false
}
```

Set `stream: false` for simplicity. The response time will be 5-10 seconds for typical queries.

### Presence Response Formatting

For presence queries where you want a natural language response, use Ollama's `/api/generate` endpoint with a tight, factual prompt:

```
POST http://localhost:11434/api/generate
{
  "model": "qwen2.5:7b",
  "prompt": "Given these facts:\n- Laura's iPhone: present, last seen 2 minutes ago\n- Richard's iPhone: present, last seen just now\n- House status: 2 devices home\n\nAnswer this question in one natural sentence: \"Is anyone home?\"",
  "stream": false
}
```

Keep the prompt short and factual. The LLM is just formatting, not reasoning.

### Timeout Handling

Set a **15-second timeout** on all Ollama requests. If Ollama doesn't respond in time:
- For presence queries: return the raw factual answer without formatting
- For general chat: reply with "Sorry, I'm thinking too hard about that one. Try again in a moment."

## Configuration

Add Telegram bot and Ollama settings to the **existing** `config.json` at `/opt/bt-monitor/config.json`, alongside the existing scanner/web config:

```json
{
  "...existing scanner config fields...",

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

The Telegram token and chat ID should be loaded from environment variables (or `/home/pi/.openclaw/.env`), not stored in `config.json`. The `telegram_token_env` and `telegram_chat_id_env` fields name the environment variables to read.

## Project Structure

Add the following files to the existing `/opt/bt-monitor/` project:

```
/opt/bt-monitor/
├── ...existing files...
├── bt_telegram.py              # Telegram bot — message handler, intent router, Ollama client
├── bt-telegram.service         # systemd unit file
```

Keep it to a single new module. The intent router, Ollama client, and notification sender are all small enough to live in one file, consistent with how `bt_scanner.py` is a single file.

The query layer uses existing `bt_db.py` helpers and the REST API — no new query module needed.

## Database Changes

Add one new table for conversation history, using the existing migration system in `bt_db.py`:

```sql
CREATE TABLE IF NOT EXISTS chat_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   TEXT NOT NULL,
    role      TEXT NOT NULL,       -- 'user' or 'assistant'
    content   TEXT NOT NULL,
    timestamp REAL NOT NULL        -- Unix timestamp
);
CREATE INDEX IF NOT EXISTS idx_chat_history_chat_id ON chat_history(chat_id, timestamp DESC);
```

Add a cleanup call to purge entries older than 7 days, consistent with existing stale data cleanup patterns.

## Dependencies

Add to the existing `requirements.txt`:

```
python-telegram-bot>=21.0
python-dotenv>=1.0.0
```

`httpx` and `aiosqlite` are not needed — `httpx` is already a dependency, and `python-telegram-bot` handles async Telegram I/O. For Ollama HTTP calls, use the existing `httpx.AsyncClient`.

## systemd Service

```ini
[Unit]
Description=Device Radar Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/bt-monitor/bt_telegram.py
WorkingDirectory=/opt/bt-monitor
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/home/pi/.openclaw/.env

[Install]
WantedBy=multi-user.target
```

> **Note:** Runs as root (same as `bt-scanner.service`) since it shares the working directory. The `EnvironmentFile` directive loads the Telegram token and chat ID from the `.env` file.

## Coding Standards

- Python 3.11+ with type hints throughout
- async/await for all I/O operations (use `httpx.AsyncClient` for Ollama and Telegram API calls)
- Logging via the `logging` module (not print statements)
- Log format: `%(asctime)s [%(levelname)s] %(name)s: %(message)s`
- Handle all exceptions gracefully — the bot must never crash from a bad Ollama response or database error
- Keep functions small and focused
- Use dataclasses for structured data

## Testing

- Test intent routing with a variety of phrasings to ensure presence queries are caught
- Test Ollama timeout handling by temporarily stopping Ollama and sending messages
- Test proactive notifications by manually inserting events into the database
- Test conversation history by having a multi-turn chat and verifying context is maintained

## Migration Steps

When implementing, follow this order:

1. Add `chat_history` table migration to `bt_db.py`
2. Create `bt_telegram.py` with intent router, Ollama client, and message handler
3. Refactor `bt_scanner.py` to call `bt_telegram.send_notification()` instead of `send_telegram_alert()` (remove the synchronous `requests` import and inline function)
4. Add `bt-telegram.service` and test
5. ~~Once Telegram notifications are confirmed working, remove ntfy.sh code from `bt_scanner.py`~~ **Done**

## Git

- GitHub org: `nospario`
- This lives in the existing `bluetooth` repo (not a separate repo)
- `.gitignore` should already exclude: `config.json`, `.env`, `*.db`, `__pycache__/`, `*.pyc`

## Future Considerations (Do Not Implement Yet)

- Integration with Hive heating API for smart home control based on presence
- Health data from Pebble Index 01 ring
- Alexa voice interface
- AI HAT+ acceleration for faster Ollama inference when hardware is available

## Important Notes

- The existing bt-monitor **already writes to SQLite** — `bt_db.py` has full schema, migrations, and query helpers. Do NOT create new database schemas or duplicate existing tables.
- The existing `send_telegram_alert()` in `bt_scanner.py` uses synchronous `requests.post()`. This should be replaced with the bot module's async implementation.
- Ollama is already installed and running with `qwen2.5:7b`. Test connectivity with `curl http://localhost:11434/api/tags` before starting.
- The Pi has 16GB RAM so memory is not a constraint, but be mindful of CPU — avoid unnecessary polling or busy loops.
- The existing Flask web dashboard runs on port 8080 and provides REST APIs that the bot can use for presence queries.
