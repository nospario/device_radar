# Feature: Alexa Welcome Home Announcements

## Overview

Add Alexa Echo announcements to Device Radar, triggered when a watched device arrives home. The system should generate personalised welcome messages via the local Ollama LLM and speak them through an Amazon Echo using the `alexa_remote_control.sh` shell script.

The welcome message should be spoken on **"Laura's Echo"** (the Kitchen Echo).

## Context

- Device Radar is a Raspberry Pi 5 presence-monitoring system located at `/opt/bt-monitor/` (production) and `/var/www/bluetooth/` (development)
- It already tracks device arrival/departure and sends Telegram notifications from `bt_scanner.py`
- It already has Ollama integration in `bt_telegram.py` for chat responses
- The `alexa_remote_control.sh` script (from https://github.com/thorsten-gehrig/alexa-remote-control) is installed on the Pi and working. It authenticates with Amazon via a refresh token and can make any Echo in the household speak arbitrary text
- Authentication credentials (refresh token etc.) are already configured and working
- The command to make the kitchen Echo speak is:
  ```bash
  ./alexa_remote_control.sh -d "Laura's Echo" -e speak:"Hello from the Pi"
  ```

## Requirements

### 1. New module: `bt_alexa.py`

Create a new module that handles Alexa announcements. It should:

- **Wrap `alexa_remote_control.sh` execution** — provide a function to make a named Echo device speak a given message
- **Generate welcome messages via Ollama** — call the local Ollama instance to produce a short, contextual, personalised greeting
- **Be called from `bt_scanner.py`** when an arrival is detected for a device that has Alexa announcements enabled
- **Run the announcement asynchronously** so it doesn't block the scan loop (Ollama can take a few seconds, and the Alexa command adds a couple more)

#### Key function: `announce_arrival(device_name, person_name, config)`

This is the main entry point called by the scanner on arrival. It should:

1. Gather context for the greeting:
   - Current time of day (morning/afternoon/evening)
   - Day of the week
   - The person's name (from `person_aliases` in config, resolved from the device)
   - How long the person has been away (calculate from the device's last departure event in the DB, if available)
2. Send a prompt to Ollama asking for a short, natural welcome home greeting using the context above
3. Fall back to a simple static message (e.g. "Welcome home {name}") if Ollama times out or fails
4. Execute `alexa_remote_control.sh` to speak the message on the configured Echo device
5. Log the announcement (what was said, which device, success/failure)

#### Ollama prompt design

The prompt to Ollama should:
- Ask for a **single sentence** greeting, casual and warm
- Include the context variables (time of day, day of week, person name, time away)
- Instruct the model **not** to use emoji, hashtags, or special characters (these won't work via Alexa TTS)
- Instruct the model to keep it under 30 words
- Use the same `ollama_url` and `ollama_timeout_seconds` from the existing config
- Use a **dedicated model config key** `alexa_ollama_model` (defaulting to the existing `ollama_model` value) so the user can optionally run a different/larger model for greetings vs chat

#### alexa_remote_control.sh wrapper

- The path to the script should be configurable via `alexa_script_path` in config
- Set the required environment variables before calling the script:
  - `REFRESH_TOKEN` — from a file path or env var (see config below)
  - `AMAZON` = `amazon.co.uk`
  - `ALEXA` = `alexa.amazon.co.uk`
  - `TTS_LOCALE` = `en-GB`
- Use `subprocess.run()` with a timeout (e.g. 30 seconds) to execute the script
- Capture and log stderr/stdout for debugging

#### Cooldown

- Implement a cooldown period (`alexa_cooldown_seconds`, default 300) so that if a device flaps (departs and arrives quickly), the Echo doesn't spam announcements
- Track the last announcement time per device in memory (a simple dict, not persisted to DB)

### 2. Modifications to `bt_scanner.py`

In the existing arrival detection logic (where Telegram departure/arrival notifications are sent):

- After sending the Telegram arrival notification, also trigger `announce_arrival()` from `bt_alexa.py`
- Only trigger for devices that have Alexa announcements enabled (see new DB field below)
- Run the announcement in a background thread so the scan loop is not blocked
- Import and initialise the Alexa module only if `alexa_enabled` is `true` in config

### 3. Modifications to `bt_db.py`

- Add a new boolean column `alexa_announce` (default `false`) to the devices table
- Add a migration to add this column to existing databases
- Add query functions to get/set this field

### 4. Modifications to `bt_web.py` and `templates/device.html`

- Add an "Alexa Announcement" toggle on the device detail page, alongside the existing "Notifications" toggle
- This toggle controls the `alexa_announce` field — when enabled, arrivals for this device trigger an Echo announcement
- Only show this toggle if `alexa_enabled` is `true` in config

### 5. Configuration additions to `config.json`

Add a new section for Alexa settings:

```json
{
  "alexa_enabled": false,
  "alexa_script_path": "/opt/bt-monitor/alexa_remote_control.sh",
  "alexa_device_name": "Laura's Echo",
  "alexa_env_file": "/home/pi/.alexa-env",
  "alexa_cooldown_seconds": 300,
  "alexa_ollama_model": null
}
```

| Field | Default | Description |
|---|---|---|
| `alexa_enabled` | `false` | Master switch for Alexa announcements |
| `alexa_script_path` | `/opt/bt-monitor/alexa_remote_control.sh` | Path to the alexa_remote_control.sh script |
| `alexa_device_name` | `"Laura's Echo"` | Name of the Echo device to announce on (must match the name shown by `alexa_remote_control.sh -a`) |
| `alexa_env_file` | `/home/pi/.alexa-env` | Path to file containing Alexa environment variables (REFRESH_TOKEN, AMAZON, ALEXA, TTS_LOCALE) |
| `alexa_cooldown_seconds` | `300` | Minimum seconds between announcements for the same device |
| `alexa_ollama_model` | `null` | Ollama model for greetings (null = use the main `ollama_model`) |

The `alexa_env_file` should be a simple KEY=VALUE file like:

```
REFRESH_TOKEN=Atnr|...
AMAZON=amazon.co.uk
ALEXA=alexa.amazon.co.uk
TTS_LOCALE=en-GB
```

The module should parse this file and pass the variables as environment to the subprocess call. This keeps credentials out of config.json and the git repo.

### 6. README.md updates

Add a new section documenting:
- How to set up `alexa_remote_control.sh` (clone the repo, obtain refresh token via `alexa-cookie-cli`)
- How to configure the env file
- How to enable Alexa announcements per device
- The Ollama greeting generation feature

## Architecture Notes

- Follow the same patterns used in the existing codebase — the Telegram bot module (`bt_telegram.py`) is a good reference for how Ollama calls are structured
- The Ollama call in this module should be independent of the Telegram bot — the bot may or may not be running
- The module should gracefully handle: Ollama not running, alexa_remote_control.sh not found, auth failures, network issues — all with logging and fallback to static message or silent failure
- Log output should use Python's `logging` module consistent with the rest of the project

## Testing

- After implementing, test with: `sudo python3 bt_alexa.py --test "Richard"` which should generate a greeting via Ollama and speak it on the configured Echo
- This `--test` mode should be implemented as a simple `if __name__ == "__main__"` block in `bt_alexa.py`

## File Changes Summary

| File | Action |
|---|---|
| `bt_alexa.py` | **New** — Alexa announcement module |
| `bt_scanner.py` | **Modify** — call `announce_arrival()` on device arrival |
| `bt_db.py` | **Modify** — add `alexa_announce` column and migration |
| `bt_web.py` | **Modify** — add Alexa toggle to device settings API |
| `templates/device.html` | **Modify** — add Alexa announcement toggle in UI |
| `config.json` | **Modify** — add Alexa configuration keys |
| `README.md` | **Modify** — document Alexa setup and usage |
