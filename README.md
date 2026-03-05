# Device Radar

A presence-monitoring system for Raspberry Pi 5 that scans for nearby devices via BLE, Classic Bluetooth, and WiFi/LAN. It tracks device presence in a SQLite database, provides a real-time web dashboard, and sends push notifications via [ntfy.sh](https://ntfy.sh) when watched devices arrive or depart.

## Prerequisites

- Raspberry Pi 5 (or any Linux system with a BLE adapter)
- Raspberry Pi OS 64-bit (Bookworm) or equivalent
- Python 3.11+
- Bluetooth enabled (`sudo systemctl enable bluetooth && sudo systemctl start bluetooth`)

## Architecture

The system consists of two services:

- **bt_scanner.py** — the background scanner that discovers devices, classifies them, tracks presence state, and sends notifications
- **bt_web.py** — a Flask web dashboard on port 8080 for viewing devices, history, pairing, and managing device settings

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

| Field | Default | Description |
|---|---|---|
| `ntfy_topic` | `nospario_bluetooth_672051` | ntfy.sh topic to publish notifications to |
| `ntfy_server` | `https://ntfy.sh` | ntfy server URL |
| `scan_interval_seconds` | `15` | Seconds between scan cycles |
| `scan_duration_seconds` | `8` | How long each BLE scan lasts |
| `departure_threshold_seconds` | `300` | Seconds without BLE/Classic detection before a device is considered departed |
| `rssi_threshold` | `-85` | Minimum signal strength (dBm); weaker signals are ignored |
| `db_path` | `bt_radar.db` | Path to the SQLite database file |
| `web_port` | `8080` | Port for the web dashboard |
| `cleanup_stale_hours` | `24` | Hours before stale random-MAC devices are auto-hidden |
| `wifi_scan_enabled` | `false` | Enable WiFi/LAN device scanning |
| `wifi_scan_interval_cycles` | `4` | Run WiFi scan every N BLE scan cycles |
| `wifi_departure_threshold_seconds` | `600` | Seconds without WiFi detection before departure |
| `wifi_interface` | `wlan0` | Network interface for WiFi scanning |
| `wifi_subnet` | `null` | Subnet to scan (auto-detected from interface if null) |
| `devices` | `{}` | Map of MAC address to friendly name (migrated to DB on first run) |

### Example config.json

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
  "devices": {}
}
```

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

## Subscribing to Notifications

1. Install the **ntfy** app on your phone ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [iOS](https://apps.apple.com/app/ntfy/id1625396347))
2. Subscribe to the topic: `nospario_bluetooth_672051`
3. You'll receive notifications whenever a watched device arrives or departs

Notification tags:
- Arrival: `house,green_circle`
- Departure: `wave,red_circle`

## Systemd Services

Install both services to run automatically at boot:

```bash
sudo cp bt-scanner.service bt-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bt-scanner bt-web
sudo systemctl start bt-scanner bt-web
```

### Viewing Logs

```bash
# Follow scanner logs
sudo journalctl -u bt-scanner -f

# Follow web dashboard logs
sudo journalctl -u bt-web -f

# View recent scanner logs
sudo journalctl -u bt-scanner --since "1 hour ago"
```

### Managing the Services

```bash
sudo systemctl status bt-scanner bt-web    # Check status
sudo systemctl restart bt-scanner bt-web   # Restart both
sudo systemctl stop bt-scanner bt-web      # Stop both
```

## File Structure

```
bt-monitor/
├── bt_scanner.py          # Background scanner service
├── bt_web.py              # Flask web dashboard service
├── bt_db.py               # SQLite database module
├── bt_classify.py         # Device classification logic
├── bt_pair.py             # Bluetooth pairing helper
├── bt_wifi.py             # WiFi/LAN scanning module
├── config.json            # User configuration
├── bt_radar.db            # SQLite database (auto-created)
├── requirements.txt       # Python dependencies: bleak, httpx, flask
├── deploy.sh              # Pull latest code to /opt/bt-monitor and restart service
├── bt-scanner.service     # Systemd unit for the scanner
├── bt-web.service         # Systemd unit for the web dashboard
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
