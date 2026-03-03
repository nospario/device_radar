# Bluetooth Presence Monitor

## Project Overview

A Python application for Raspberry Pi 5 that scans for known BLE (Bluetooth Low Energy) devices (phones, watches, etc.) and sends push notifications via ntfy.sh when they arrive or depart.

## Target Environment

- Raspberry Pi 5 running Raspberry Pi OS 64-bit (Bookworm)
- Python 3.11+
- Runs as a systemd service under root (required for BLE scanning)
- Deployed to `/opt/bt-monitor/`

## Architecture

Single async Python script using:

- **bleak** — BLE scanning (async, cross-platform)
- **httpx** — async HTTP client for ntfy.sh API calls
- **asyncio** — main event loop

No database. No web UI. No Docker. Keep it simple and lightweight.

## Core Behaviour

1. Every `scan_interval_seconds` (default 15), perform a BLE scan lasting `scan_duration_seconds` (default 8)
2. Compare discovered devices against a configured list of known MAC addresses
3. Track each device's state as either HOME or AWAY
4. On state transition HOME→AWAY (departure) or AWAY→HOME (arrival), send a notification to ntfy.sh
5. Ignore signals weaker than `rssi_threshold` (default -85) to avoid false positives from distant devices
6. A device is considered departed after `departure_threshold_seconds` (default 90) without detection

## ntfy.sh Configuration

- **Server:** `https://ntfy.sh`
- **Topic:** `nospario_bluetooth_672051`
- Use ntfy HTTP headers for rich notifications: `Title`, `Priority`, `Tags`
- Arrival notifications: tags `house,green_circle`
- Departure notifications: tags `wave,red_circle`

## Configuration File

Use a `config.json` file in the same directory as the script. Structure:

```json
{
  "ntfy_topic": "nospario_bluetooth_672051",
  "ntfy_server": "https://ntfy.sh",
  "scan_interval_seconds": 15,
  "scan_duration_seconds": 8,
  "departure_threshold_seconds": 90,
  "rssi_threshold": -85,
  "devices": {
    "AA:BB:CC:DD:EE:FF": "Example Phone",
    "11:22:33:44:55:66": "Example Watch"
  }
}
```

On first run, if `config.json` doesn't exist, create a default one with an empty devices dict and the ntfy topic above, then exit with a message telling the user to add their devices.

## Discovery Mode

When `devices` is empty, the script should run in discovery mode: scan and log all detected BLE devices with their MAC address, advertised name (if any), and RSSI. This helps the user find their device addresses. Format each line clearly, e.g.:

```
12:34:56:78:9A:BC  "Galaxy Watch5"  RSSI: -62
AA:BB:CC:DD:EE:FF  (unknown)        RSSI: -78
```

## Device Name Matching (Optional Feature)

Because modern phones randomise BLE MAC addresses, support an optional `device_names` field in config as a fallback matching strategy:

```json
{
  "device_names": {
    "Richard's iPhone": "Richard's Phone",
    "Galaxy Watch5": "Laura's Watch"
  }
}
```

When present, match on the advertised BLE name in addition to MAC address. MAC matches take priority. Document this in the README.

## Logging

- Use Python's `logging` module
- Default level: INFO
- Format: `%(asctime)s [%(levelname)s] %(message)s` with `%H:%M:%S` time format
- Log arrival/departure events at INFO
- Log scan results at DEBUG
- Log errors (scan failures, ntfy failures) at ERROR with the exception detail

## Systemd Service

Provide a `bt-monitor.service` unit file:

- Run as root (BLE requires it)
- WorkingDirectory: `/opt/bt-monitor`
- Restart on failure with 10s delay
- After: `bluetooth.target`, `network-online.target`

## File Structure

```
bt-monitor/
├── bt_monitor.py          # Main application
├── config.json            # User configuration (generated on first run)
├── requirements.txt       # Python dependencies: bleak, httpx
├── bt-monitor.service     # Systemd unit file
└── README.md              # Setup and usage guide
```

## Error Handling

- If a BLE scan fails (e.g. adapter busy), log the error and continue to the next scan cycle. Do not crash.
- If ntfy.sh POST fails, log the error and continue. Do not retry — the next state change will trigger a new notification.
- Wrap the main loop in a try/except so individual scan cycle failures never kill the service.

## Code Style

- Type hints throughout
- Dataclasses for device state
- Enum for presence states (HOME/AWAY)
- async/await for all I/O
- No global mutable state — encapsulate in a monitor class
- Keep it in a single file (`bt_monitor.py`) — this isn't complex enough to warrant a package

## Testing

- The script must be testable by running `python3 bt_monitor.py` as root on the Pi
- For development without a BLE adapter, the scan method should be the only hardware-dependent part, making it easy to mock
- Include a `--discover` CLI flag as a shortcut for discovery mode (scan and print all devices, ignore config)

## Dependencies

```
bleak>=0.21.0
httpx>=0.25.0
```

Install with: `pip install -r requirements.txt --break-system-packages`

## README Content

The README should cover:

1. What the project does (one paragraph)
2. Prerequisites (Pi model, OS, Python version, Bluetooth enabled)
3. Quick start (copy files, install deps, edit config, test run)
4. How to find device MAC addresses (discovery mode + bluetoothctl)
5. MAC randomisation warning and workarounds (pairing, name matching)
6. Configuration reference table
7. How to subscribe to notifications in the ntfy app
8. Systemd service installation and log viewing
9. Device name matching as an alternative to MAC addresses
