# Device Radar

A presence-monitoring system for Raspberry Pi 5 that scans for nearby devices via BLE, Classic Bluetooth, and WiFi/LAN. It tracks device presence in a SQLite database, provides a real-time web dashboard, and sends notifications via Telegram when watched devices arrive or depart. Includes a Telegram bot for presence queries and general chat powered by a local Ollama LLM.

## Prerequisites

- Raspberry Pi 5 (or any Linux system with a BLE adapter)
- Raspberry Pi OS 64-bit (Bookworm) or equivalent
- Python 3.11+
- Bluetooth enabled (`sudo systemctl enable bluetooth && sudo systemctl start bluetooth`)

## Architecture

The system consists of three services:

- **bt_scanner.py** — the background scanner that discovers devices, classifies them, tracks presence state, and sends notifications
- **bt_web.py** — a Flask web dashboard on port 8080 for viewing devices, history, pairing, and managing device settings
- **bt_telegram.py** — a Telegram bot for presence queries, Ollama-powered chat, and proactive arrival/departure notifications

Supporting modules:

| Module | Purpose |
|---|---|
| `bt_db.py` | SQLite schema, migrations, and query functions |
| `bt_classify.py` | Device type and manufacturer identification from BLE advertisements, device class codes, and names |
| `bt_pair.py` | Bluetooth pairing/unpairing via `bluetoothctl` |
| `bt_wifi.py` | WiFi/LAN device discovery via ping sweep + ARP table |

## Quick Start

1. **Clone the repo** to the deployment directory:

   ```bash
   sudo git clone https://github.com/nospario/device_radar.git /opt/bt-monitor
   ```

2. **Install dependencies:**

   ```bash
   sudo pip install -r /opt/bt-monitor/requirements.txt --break-system-packages
   ```

3. **Edit the config:**

   ```bash
   sudo nano /opt/bt-monitor/config.json
   ```

4. **Test run** (scanner):

   ```bash
   sudo python3 /opt/bt-monitor/bt_scanner.py --debug
   ```

5. **Test run** (web dashboard):

   ```bash
   sudo python3 /opt/bt-monitor/bt_web.py
   ```

   Then open `http://<pi-ip>:8080` in your browser.

## Deployment

Development is done in `/var/www/bluetooth/` (the git working copy). The production scanner and web dashboard run from `/opt/bt-monitor/` (a separate clone of the same repo).

To deploy changes after committing and pushing:

```bash
./deploy.sh
```

This pulls the latest code into `/opt/bt-monitor/` and restarts the scanner service. The database (`bt_radar.db`) and config (`config.json`) are gitignored and are not affected by pulls.

## Web Dashboard

The dashboard provides:

- **Dashboard** — live device list with state, scan type, RSSI, and quick toggles for watchlist and notifications
- **Device Detail** — per-device info, settings (friendly name, type, watchlist, notifications, hidden), linked devices, and event history
- **History** — filterable event log with pagination
- **Pairing** — pair/unpair Bluetooth devices via the web UI

## Telegram Bot

The Telegram bot (`bt_telegram.py`) provides interactive presence queries and general chat.

### Setup

1. Create a bot via [@BotFather](https://t.me/BotFather) on Telegram and note the token
2. Get your chat ID by messaging [@userinfobot](https://t.me/userinfobot)
3. Add credentials to `/home/pi/.device-radar.env`:

   ```
   TELEGRAM_BOT_TOKEN=your_bot_token_here
   TELEGRAM_CHAT_ID=your_chat_id_here
   ```

4. Set `"telegram_bot_enabled": true` in `config.json`

### Presence Commands

| Command | Description |
|---|---|
| `/home` | Quick summary of who's detected |
| `/devices` | List all watchlisted devices with status |
| `/lastseen <name>` | When a device was last seen |

You can also ask natural language questions:

- "Is anyone home?"
- "Is Richard home?"
- "When did Laura arrive?"
- "How long has Richard been away?"

### General Chat

Any message that isn't a presence query is forwarded to a local Ollama instance for conversational responses. Conversation history is maintained across messages.

### Person Aliases

Map short names to devices in `config.json`:

```json
{
  "person_aliases": {
    "richard": "Richard's iPhone",
    "laura": "Laura's iPhone"
  }
}
```

Values should match a device's `friendly_name`. The bot resolves linked device groups automatically — a person is "home" if any device in their group is detected.

## Finding Device MAC Addresses

### Discovery Mode

Run the built-in discovery mode to see all nearby Bluetooth devices:

```bash
sudo python3 /opt/bt-monitor/bt_scanner.py --discover
```

This scans both BLE and Classic Bluetooth, printing each device's MAC address, advertised name, RSSI, device type, and manufacturer.

### WiFi Discovery

To see all devices on your local network:

```bash
sudo python3 /opt/bt-monitor/bt_scanner.py --discover-wifi
```

### Using bluetoothctl

```bash
bluetoothctl
[bluetooth]# scan on
```

Watch for your device to appear, then note its MAC address.

## MAC Address Randomisation

Modern phones (especially iPhones and recent Android devices) randomise their BLE MAC address for privacy. This means the MAC address changes periodically, making MAC-based tracking unreliable.

**Workarounds:**

- **Pair the device** via classic Bluetooth (use the Pairing page in the dashboard) — paired devices often use a stable address
- **Use WiFi scanning** — phones on your home WiFi use a consistent MAC
- **Link devices** — if the same physical device appears with different MACs (e.g. BLE and WiFi), link them so they show as one device (see Device Linking below)
- **Smartwatches** generally do not randomise their MAC and are easier to track

## Configuration Reference

Edit `config.json` in the same directory as the scripts:

### Scanner Settings

| Field | Default | Description |
|---|---|---|
| `scan_interval_seconds` | `15` | Seconds between scan cycles |
| `scan_duration_seconds` | `8` | How long each BLE scan lasts |
| `departure_threshold_seconds` | `300` | Seconds without BLE/Classic detection before a device is considered departed |
| `rssi_threshold` | `-85` | Minimum signal strength (dBm); weaker signals are ignored |
| `db_path` | `bt_radar.db` | Path to the SQLite database file |
| `web_port` | `8080` | Port for the web dashboard |
| `cleanup_stale_hours` | `24` | Hours before stale random-MAC devices are auto-hidden |

### WiFi Settings

| Field | Default | Description |
|---|---|---|
| `wifi_scan_enabled` | `false` | Enable WiFi/LAN device scanning |
| `wifi_scan_interval_cycles` | `4` | Run WiFi scan every N BLE scan cycles |
| `wifi_departure_threshold_seconds` | `600` | Seconds without WiFi detection before departure |
| `wifi_interface` | `wlan0` | Network interface for WiFi scanning |
| `wifi_subnet` | `null` | Subnet to scan (auto-detected from interface if null) |

### Telegram Bot Settings

| Field | Default | Description |
|---|---|---|
| `telegram_bot_enabled` | `false` | Enable the Telegram bot service |
| `telegram_token_env` | `TELEGRAM_BOT_TOKEN` | Environment variable name for the bot token |
| `telegram_chat_id_env` | `TELEGRAM_CHAT_ID` | Environment variable name for the authorized chat ID |
| `person_aliases` | `{}` | Map of short names to device `friendly_name` values |

### Ollama Settings

| Field | Default | Description |
|---|---|---|
| `ollama_url` | `http://localhost:11434` | Ollama API endpoint |
| `ollama_model` | `qwen2.5:1.5b` | Model to use for chat responses |
| `ollama_timeout_seconds` | `15` | Timeout for Ollama requests (falls back to direct answer) |
| `conversation_history_length` | `10` | Number of recent messages to include as context |
| `system_prompt` | *(see config)* | System prompt sent to Ollama for general chat |

### Other

| Field | Default | Description |
|---|---|---|
| `devices` | `{}` | Map of MAC address to friendly name (migrated to DB on first run) |

Note: Devices listed in `config.json` under `devices` are migrated into the SQLite database as watchlisted on first run. After that, manage devices through the web dashboard.

## Device Linking

Devices can appear multiple times — once from BLE scanning (Bluetooth MAC) and once from WiFi/ARP scanning (network MAC). Since these are different MAC addresses, they show as separate rows. Device linking merges them into one logical device.

### How it works

- **Each MAC keeps its own state** — the scanner continues to track each MAC independently
- **Merging happens at display and notification time** — the dashboard shows one row per group; notifications are sent once per group
- A linked group has one **primary** device and one or more **secondaries**
- The merged row shows: `DETECTED` if any member is detected, the most recent `last_seen`, and combined scan types
- Arrival notification fires only when the **first** member is detected
- Departure notification fires only when **all** members are lost

### How to link devices

1. Open the device detail page for the device you want to be the **primary**
2. In the "Linked Devices" card, select a device from the dropdown
3. Click **Link**
4. The dashboard now shows one merged row with a "linked" badge

To unlink, click **Unlink** next to any linked device on the detail page.

## Notifications

### Telegram (primary)

Arrival and departure notifications are sent to Telegram via the bot. Notifications are sent as part of the scanner's state transition logic — no polling required.

- Arrival: "📡 **Device Name** detected"
- Departure: "👋 **Device Name** departed"

## Systemd Services

Install all three services to run automatically at boot:

```bash
sudo cp bt-scanner.service bt-web.service bt-telegram.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bt-scanner bt-web bt-telegram
sudo systemctl start bt-scanner bt-web bt-telegram
```

### Viewing Logs

```bash
# Follow scanner logs
sudo journalctl -u bt-scanner -f

# Follow web dashboard logs
sudo journalctl -u bt-web -f

# Follow Telegram bot logs
sudo journalctl -u bt-telegram -f

# View recent scanner logs
sudo journalctl -u bt-scanner --since "1 hour ago"
```

### Managing the Services

```bash
sudo systemctl status bt-scanner bt-web bt-telegram   # Check status
sudo systemctl restart bt-scanner bt-web bt-telegram   # Restart all
sudo systemctl stop bt-scanner bt-web bt-telegram      # Stop all
```

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
├── config.json            # User configuration
├── bt_radar.db            # SQLite database (auto-created)
├── requirements.txt       # Python dependencies
├── deploy.sh              # Pull latest code to /opt/bt-monitor and restart service
├── bt-scanner.service     # Systemd unit for the scanner
├── bt-web.service         # Systemd unit for the web dashboard
├── bt-telegram.service    # Systemd unit for the Telegram bot
├── templates/
│   ├── base.html          # Base layout with navbar
│   ├── dashboard.html     # Main device list
│   ├── device.html        # Device detail + settings + linking
│   ├── history.html       # Event history log
│   └── pairing.html       # Bluetooth pairing UI
├── static/
│   ├── app.js             # Dashboard JavaScript
│   └── style.css          # Dark theme styles
└── README.md
```
