#!/usr/bin/env python3
"""Bluetooth Radar Scanner — discovers all nearby BLE and classic Bluetooth
devices, classifies them, stores to SQLite, and sends Telegram notifications
for watchlisted devices on state transitions."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

import bt_alexa
import bt_classify
import bt_db
import bt_dns
import bt_pair
import bt_telegram
import bt_wifi

logger = logging.getLogger("bt_scanner")

DEFAULT_CONFIG: dict[str, Any] = {
    "scan_interval_seconds": 15,
    "scan_duration_seconds": 8,
    "departure_threshold_seconds": 300,
    "rssi_threshold": -85,
    "db_path": "bt_radar.db",
    "cleanup_stale_hours": 24,
    "devices": {},
    "wifi_scan_enabled": False,
    "wifi_scan_interval_cycles": 1,
    "wifi_departure_threshold_seconds": 90,
    "wifi_interface": "wlan0",
    "wifi_subnet": None,
    "arrival_cooldown_seconds": 300,
}

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_or_create_config() -> dict[str, Any]:
    """Load config.json, creating a default one if it doesn't exist."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
        logger.info("Created default config at %s", CONFIG_PATH)
        print(
            f"Default configuration created at {CONFIG_PATH}\n"
            "Edit it to configure your settings, then run again."
        )
        sys.exit(0)

    with CONFIG_PATH.open() as f:
        config = json.load(f)

    for key, value in DEFAULT_CONFIG.items():
        config.setdefault(key, value)

    return config


def migrate_config_devices(config: dict[str, Any], db_path: str | Path) -> None:
    """Migrate devices from config.json into the database as watchlisted."""
    devices = config.get("devices", {})
    if not devices:
        return

    conn = bt_db.get_connection(db_path)
    migrated = 0
    for mac, name in devices.items():
        mac = mac.upper()
        existing = bt_db.get_device(conn, mac)
        if existing is None:
            bt_db.upsert_device(
                conn, mac,
                advertised_name=name,
                device_type="Phone",
                state="LOST",
            )
            bt_db.update_device(conn, mac, friendly_name=name, is_watchlisted=True, is_notify=True)
            migrated += 1
        elif not existing["is_watchlisted"]:
            bt_db.update_device(conn, mac, friendly_name=name, is_watchlisted=True, is_notify=True)
            migrated += 1

    conn.close()
    if migrated:
        logger.info("Migrated %d device(s) from config.json to database", migrated)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class BluetoothRadarScanner:
    """Scans for all Bluetooth devices and tracks presence in SQLite."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.scan_interval: int = config["scan_interval_seconds"]
        self.scan_duration: int = config["scan_duration_seconds"]
        self.departure_threshold: int = config["departure_threshold_seconds"]
        self.rssi_threshold: int = config["rssi_threshold"]
        self.cleanup_hours: int = config["cleanup_stale_hours"]

        self.db_path = Path(__file__).resolve().parent / config["db_path"]
        self.scan_cycle: int = 0

        # WiFi scanning config
        self.wifi_enabled: bool = config.get("wifi_scan_enabled", False)
        self.wifi_interval_cycles: int = config.get("wifi_scan_interval_cycles", 4)
        self.wifi_departure_threshold: int = config.get("wifi_departure_threshold_seconds", 600)
        self.wifi_interface: str = config.get("wifi_interface", "wlan0")
        self.wifi_subnet: str | None = config.get("wifi_subnet")

        # Arrival cooldown: suppress arrival notifications if the device
        # departed less than this many seconds ago (prevents flapping spam
        # from WiFi devices that sleep and wake frequently).
        self.arrival_cooldown: int = config.get("arrival_cooldown_seconds", 300)

    # ------------------------------------------------------------------
    # BLE scanning
    # ------------------------------------------------------------------

    async def scan_ble(self) -> list[tuple[BLEDevice, AdvertisementData]]:
        """Perform a BLE scan and return all discovered devices."""
        devices: list[tuple[BLEDevice, AdvertisementData]] = []

        def _callback(device: BLEDevice, adv: AdvertisementData) -> None:
            devices.append((device, adv))

        scanner = BleakScanner(detection_callback=_callback)
        await scanner.start()
        await asyncio.sleep(self.scan_duration)
        await scanner.stop()
        return devices

    # ------------------------------------------------------------------
    # Classic Bluetooth scanning
    # ------------------------------------------------------------------

    async def scan_classic(self) -> list[tuple[str, str | None, int | None]]:
        """Perform a classic Bluetooth inquiry.

        Returns list of (mac, name, device_class) tuples.
        """
        results: list[tuple[str, str | None, int | None]] = []

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "hcitool", "inq", "--length=8",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ),
                timeout=30,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = stdout.decode()

            for line in output.splitlines():
                line = line.strip()
                if not line or line.startswith("Inquiring"):
                    continue
                parts = line.split()
                if parts and ":" in parts[0]:
                    mac = parts[0].upper()
                    # Parse device class if present
                    dev_class = None
                    for i, part in enumerate(parts):
                        if part == "class:" and i + 1 < len(parts):
                            dev_class = bt_classify.parse_device_class(parts[i + 1])
                    name = await self._resolve_classic_name(mac)
                    results.append((mac, name, dev_class))
        except Exception:
            logger.error("Classic Bluetooth scan failed", exc_info=True)

        return results

    async def _resolve_classic_name(self, mac: str) -> str | None:
        """Resolve a classic Bluetooth device name."""
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "hcitool", "name", mac,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ),
                timeout=10,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            name = stdout.decode().strip()
            return name if name else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # WiFi/LAN scanning
    # ------------------------------------------------------------------

    async def scan_wifi_devices(self) -> list[bt_wifi.WifiDevice]:
        """Perform a WiFi/LAN scan via ping sweep + ARP."""
        return await bt_wifi.scan_wifi(
            interface=self.wifi_interface,
            subnet=self.wifi_subnet,
        )

    # ------------------------------------------------------------------
    # Discovery mode
    # ------------------------------------------------------------------

    async def discover(self) -> None:
        """Scan and print all visible Bluetooth devices."""
        print("Scanning for BLE devices...")
        ble_results = await self.scan_ble()

        best: dict[str, tuple[str, int, str, str, str | None]] = {}
        for device, adv in ble_results:
            addr = device.address.upper()
            rssi = adv.rssi if adv.rssi is not None else -999
            name = device.name or adv.local_name or "(unknown)"

            info = bt_classify.classify_device(
                advertised_name=name if name != "(unknown)" else None,
                manufacturer_data=dict(adv.manufacturer_data) if adv.manufacturer_data else None,
                service_uuids=adv.service_uuids or None,
            )

            if addr not in best or rssi > best[addr][1]:
                best[addr] = (name, rssi, "BLE", info.device_type, info.manufacturer)

        print("Scanning for classic Bluetooth devices...")
        classic_results = await self.scan_classic()
        for mac, name, dev_class in classic_results:
            display_name = name or "(unknown)"
            info = bt_classify.classify_device(
                advertised_name=name,
                device_class=dev_class,
            )
            if mac not in best:
                best[mac] = (display_name, 0, "Classic", info.device_type, info.manufacturer)
            else:
                existing = best[mac]
                merged_name = display_name if existing[0] == "(unknown)" and display_name != "(unknown)" else existing[0]
                best[mac] = (merged_name, existing[1], "BLE+Classic",
                             info.device_type if info.device_type != "Unknown" else existing[3],
                             info.manufacturer or existing[4])

        if not best:
            print("No Bluetooth devices found.")
            return

        print(f"\n{'ADDRESS':<20} {'NAME':<25} {'RSSI':<7} {'TYPE':<10} {'CLASS':<18} {'MFR'}")
        print("-" * 95)
        for addr, (name, rssi, scan_type, dev_type, mfr) in sorted(
            best.items(), key=lambda x: x[1][1], reverse=True
        ):
            display_name = f'"{name}"' if name != "(unknown)" else name
            rssi_str = str(rssi) if rssi != 0 else "n/a"
            mfr_str = mfr or ""
            print(f"{addr:<20} {display_name:<25} {rssi_str:<7} {scan_type:<10} {dev_type:<18} {mfr_str}")

    # ------------------------------------------------------------------
    # Main scan cycle
    # ------------------------------------------------------------------

    async def process_scan(self) -> None:
        """Run one scan cycle: discover devices, classify, upsert, track state."""
        self.scan_cycle += 1
        now = time.time()
        conn = bt_db.get_connection(self.db_path)

        # Capture previous states before upserting
        prev_states: dict[str, str] = {}
        for row in conn.execute("SELECT mac_address, state FROM devices").fetchall():
            prev_states[row["mac_address"]] = row["state"]

        seen_macs: set[str] = set()

        # -- BLE scan --
        try:
            ble_results = await self.scan_ble()
            logger.debug("BLE scan found %d raw advertisements", len(ble_results))
        except Exception:
            logger.error("BLE scan failed", exc_info=True)
            ble_results = []

        # De-duplicate BLE results (keep strongest RSSI per MAC)
        ble_best: dict[str, tuple[BLEDevice, AdvertisementData]] = {}
        for device, adv in ble_results:
            addr = device.address.upper()
            rssi = adv.rssi if adv.rssi is not None else -999
            if addr not in ble_best or rssi > (ble_best[addr][1].rssi or -999):
                ble_best[addr] = (device, adv)

        for addr, (device, adv) in ble_best.items():
            rssi = adv.rssi if adv.rssi is not None else -999
            if rssi < self.rssi_threshold:
                continue

            name = device.name or adv.local_name
            mfr_data = dict(adv.manufacturer_data) if adv.manufacturer_data else None
            svc_uuids = adv.service_uuids or None

            info = bt_classify.classify_device(
                advertised_name=name,
                manufacturer_data=mfr_data,
                service_uuids=svc_uuids,
            )

            bt_db.upsert_device(
                conn, addr,
                advertised_name=name,
                device_type=info.device_type,
                manufacturer=info.manufacturer,
                scan_type="BLE",
                rssi=rssi,
                state="DETECTED",
                manufacturer_data={str(k): list(v) for k, v in mfr_data.items()} if mfr_data else None,
                service_uuids=svc_uuids,
            )
            seen_macs.add(addr)

        # -- Classic Bluetooth scan (every 4th cycle) --
        if self.scan_cycle % 4 == 0:
            try:
                classic_results = await self.scan_classic()
                logger.debug("Classic scan found %d devices", len(classic_results))
            except Exception:
                logger.error("Classic Bluetooth scan failed", exc_info=True)
                classic_results = []

            for mac, name, dev_class in classic_results:
                info = bt_classify.classify_device(
                    advertised_name=name,
                    device_class=dev_class,
                )
                bt_db.upsert_device(
                    conn, mac,
                    advertised_name=name,
                    device_type=info.device_type,
                    manufacturer=info.manufacturer,
                    scan_type="Classic" if mac not in seen_macs else "BLE+Classic",
                    state="DETECTED",
                )
                seen_macs.add(mac)

        # -- WiFi/LAN scan (every Nth cycle) --
        if self.wifi_enabled and self.scan_cycle % self.wifi_interval_cycles == 0:
            try:
                wifi_results = await self.scan_wifi_devices()
                logger.debug("WiFi scan found %d devices", len(wifi_results))
            except Exception:
                logger.error("WiFi scan failed", exc_info=True)
                wifi_results = []

            for wd in wifi_results:
                display_name = wd.hostname or wd.ip_address
                is_new = bt_db.get_device(conn, wd.mac_address) is None
                bt_db.upsert_device(
                    conn, wd.mac_address,
                    advertised_name=display_name,
                    device_type="Network Device",
                    manufacturer=wd.vendor,
                    scan_type="WiFi",
                    state="DETECTED",
                    ip_address=wd.ip_address,
                )
                seen_macs.add(wd.mac_address)
                if is_new:
                    logger.info("New WiFi device detected: %s (%s)", display_name, wd.mac_address)
                    await bt_telegram.send_notification(display_name, "arrived")

        # -- State transitions --
        await self._check_arrivals(conn, seen_macs, prev_states)
        await self._check_departures(conn, seen_macs, now)

        # -- Proximity-triggered Alexa messages --
        if self.config.get("alexa_enabled"):
            try:
                await bt_alexa.check_proximity_devices(self.config, self.db_path)
            except Exception:
                logger.error("Proximity check failed", exc_info=True)

        # -- Periodic paired status sync (every 20th cycle) --
        if self.scan_cycle % 20 == 0:
            try:
                bt_pair.sync_paired_status(conn)
            except Exception:
                logger.error("Failed to sync paired status", exc_info=True)

        # -- Periodic cleanup --
        if self.scan_cycle % 100 == 0:
            hidden = bt_db.hide_stale_random_macs(conn, self.cleanup_hours)
            if hidden:
                logger.info("Hidden %d stale random-MAC devices", hidden)
            # Purge old DNS queries
            if self.config.get("dns_monitor_enabled"):
                retention = self.config.get("dns_data_retention_days", 14)
                purged = bt_dns.purge_old_queries(conn, retention)
                if purged:
                    logger.info("Purged %d old DNS queries (>%d days)", purged, retention)

        conn.close()

    async def _check_arrivals(
        self, conn: sqlite3.Connection, seen_macs: set[str], prev_states: dict[str, str]
    ) -> None:
        """Check for arrival events and fire notifications for watchlisted devices.

        For linked devices, only sends one notification per group — using the
        primary's name — and only if no other group member was already HOME.
        """
        notified_groups: set[str] = set()  # primary MACs already notified
        welcomed_groups: set[str] = set()  # primary MACs already welcomed

        alexa_enabled = self.config.get("alexa_enabled", False)

        for mac in seen_macs:
            prev = prev_states.get(mac)
            # Only trigger arrival if device was previously AWAY or is brand new
            if prev == "DETECTED":
                continue

            dev = bt_db.get_device(conn, mac)
            if not dev:
                continue

            # Only record events and notify for watchlisted devices
            if not dev["is_watchlisted"]:
                continue

            dev_name = dev["friendly_name"] or dev["advertised_name"] or mac
            bt_db.record_event(
                conn, mac, "arrived",
                device_name=dev_name,
                device_type=dev["device_type"],
                rssi=dev["last_rssi"],
            )
            logger.info("%s arrived (RSSI %s)", dev_name, dev["last_rssi"])

            # Arrival cooldown: if the device departed very recently, this is
            # likely a WiFi sleep/wake bounce — not a real return.  Still record
            # the event above (for audit), but suppress notifications and Alexa.
            if self.arrival_cooldown > 0:
                last_events = bt_db.get_events(
                    conn, mac=mac, event_type="departed", limit=1,
                )
                if last_events:
                    departed_at = last_events[0]["timestamp"]
                    if (time.time() - departed_at) < self.arrival_cooldown:
                        logger.info(
                            "%s arrival suppressed — departed only %ds ago (cooldown %ds)",
                            dev_name,
                            int(time.time() - departed_at),
                            self.arrival_cooldown,
                        )
                        continue

            # Group-aware notification: check if part of a link group
            group = bt_db.get_link_group(conn, mac)
            primary = group["primary"]
            if primary and group["secondaries"]:
                primary_mac = primary["mac_address"]
                all_members = [primary] + group["secondaries"]
                notify_name = primary["friendly_name"] or primary["advertised_name"] or primary_mac

                if primary_mac not in notified_groups:
                    # Only consider notify-enabled members when deciding to suppress.
                    # Non-notifying members (e.g. WiFi devices always HOME) should
                    # not prevent notifications from firing.
                    other_was_home = any(
                        prev_states.get(m["mac_address"]) == "DETECTED"
                        for m in all_members
                        if m["mac_address"] != mac and m.get("is_notify")
                    )

                    if not other_was_home:
                        group_notify = any(m.get("is_notify") for m in all_members)
                        if group_notify:
                            notified_groups.add(primary_mac)
                            await bt_telegram.send_notification(notify_name, "arrived")

                # Alexa welcome announcement (group-aware, independent of notify)
                if alexa_enabled and primary_mac not in welcomed_groups:
                    group_welcome = any(m.get("is_welcome") for m in all_members)
                    if group_welcome:
                        welcomed_groups.add(primary_mac)
                        asyncio.create_task(
                            bt_alexa.announce_arrival(
                                notify_name, primary_mac, self.config, self.db_path,
                            )
                        )
            else:
                # Standalone device (not in a link group)
                if dev["is_notify"]:
                    await bt_telegram.send_notification(dev_name, "arrived")

                # Alexa welcome announcement (independent of notify)
                if alexa_enabled and dev.get("is_welcome"):
                    asyncio.create_task(
                        bt_alexa.announce_arrival(
                            dev_name, mac, self.config, self.db_path,
                        )
                    )

    async def _check_departures(
        self, conn: sqlite3.Connection, seen_macs: set[str], now: float
    ) -> None:
        """Check for devices that have departed.

        Uses scan-type-appropriate thresholds (WiFi gets a longer window).
        For linked devices, a member is only marked LOST when ALL group
        members have exceeded their thresholds — so a WiFi detection keeps
        linked BLE devices alive.  Departure notifications fire once per
        group when the last member goes LOST.
        """
        notified_groups: set[str] = set()  # primary MACs already notified

        # Get all HOME devices not seen this cycle
        home_devices = bt_db.get_all_devices(conn, state="DETECTED", include_hidden=True)
        for dev in home_devices:
            mac = dev["mac_address"]
            if mac in seen_macs:
                continue

            # Use the appropriate threshold for this device's scan type
            scan_type = (dev.get("scan_type") or "").lower()
            if "wifi" in scan_type:
                threshold = self.wifi_departure_threshold
            else:
                threshold = self.departure_threshold

            # Check if departure threshold exceeded for this device
            last_seen = dev["last_seen"] or 0
            if not (last_seen > 0 and (now - last_seen) > threshold):
                continue

            # WiFi devices: do a targeted confirmation ping before declaring
            # departure.  Sleeping phones often miss broadcast ping sweeps but
            # respond to direct unicast pings.
            if "wifi" in scan_type and dev.get("ip_address"):
                if await bt_wifi.ping_host(dev["ip_address"]):
                    # Device is still reachable — update last_seen, skip departure
                    bt_db.update_device(conn, mac, last_seen=now)
                    logger.debug(
                        "%s confirmed alive via direct ping to %s",
                        dev["friendly_name"] or mac, dev["ip_address"],
                    )
                    continue

            # If device is part of a link group, don't mark it LOST while
            # any other group member is still within its own threshold.
            group = bt_db.get_link_group(conn, mac)
            primary = group["primary"]
            if primary and group["secondaries"]:
                all_members = [primary] + group["secondaries"]
                group_still_home = False
                for member in all_members:
                    m_mac = member["mac_address"]
                    if m_mac == mac:
                        continue
                    if m_mac in seen_macs:
                        group_still_home = True
                        break
                    m_dev = bt_db.get_device(conn, m_mac)
                    if m_dev and m_dev["state"] == "DETECTED":
                        # Check if this member is also past its threshold
                        m_scan = (m_dev.get("scan_type") or "").lower()
                        m_threshold = self.wifi_departure_threshold if "wifi" in m_scan else self.departure_threshold
                        m_last = m_dev["last_seen"] or 0
                        if m_last > 0 and (now - m_last) <= m_threshold:
                            group_still_home = True
                            break
                if group_still_home:
                    continue  # another group member is still home — keep this one alive

            bt_db.update_device(conn, mac, state="LOST")

            # Only record events and notify for watchlisted devices
            if not dev["is_watchlisted"]:
                continue

            dev_name = dev["friendly_name"] or dev["advertised_name"] or mac
            bt_db.record_event(
                conn, mac, "departed",
                device_name=dev_name,
                device_type=dev["device_type"],
                rssi=dev["last_rssi"],
            )
            logger.info("%s departed", dev_name)

            # Group-aware notification: only notify when ALL members departed
            if primary and group["secondaries"]:
                primary_mac = primary["mac_address"]
                if primary_mac in notified_groups:
                    continue

                # Re-read all members — only send when ALL are LOST
                all_macs = [primary["mac_address"]] + [s["mac_address"] for s in group["secondaries"]]
                any_still_home = any(
                    bt_db.get_device(conn, m) and bt_db.get_device(conn, m)["state"] == "DETECTED"
                    for m in all_macs
                )
                if any_still_home:
                    continue

                group_notify = any(m.get("is_notify") for m in [primary] + group["secondaries"])
                if not group_notify:
                    continue

                notified_groups.add(primary_mac)
                notify_name = primary["friendly_name"] or primary["advertised_name"] or primary_mac
                await bt_telegram.send_notification(notify_name, "departed")
            elif dev["is_notify"]:
                await bt_telegram.send_notification(dev_name, "departed")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the scanner loop indefinitely."""
        logger.info(
            "Starting Bluetooth Radar scanner — "
            "scan every %ds, departure after %ds, RSSI threshold %d",
            self.scan_interval,
            self.departure_threshold,
            self.rssi_threshold,
        )
        if self.wifi_enabled:
            logger.info(
                "WiFi scanning enabled on %s — every %d cycles, departure after %ds",
                self.wifi_interface,
                self.wifi_interval_cycles,
                self.wifi_departure_threshold,
            )

        # Sync paired status on startup
        try:
            conn = bt_db.get_connection(self.db_path)
            bt_pair.sync_paired_status(conn)
            conn.close()
        except Exception:
            logger.error("Failed to sync paired status on startup", exc_info=True)

        # Start Alexa encourage loop as a background task
        if self.config.get("alexa_enabled"):
            asyncio.create_task(
                bt_alexa.run_encourage_loop(self.config, self.db_path)
            )

        # Start DNS monitoring background tasks
        if self.config.get("dns_monitor_enabled"):
            asyncio.create_task(
                bt_dns.dns_ingestion_loop(self.config, self.db_path)
            )
            asyncio.create_task(
                bt_dns.dns_alert_loop(self.config, self.db_path)
            )
            logger.info("DNS monitoring enabled")

        while True:
            try:
                await self.process_scan()
            except Exception:
                logger.error("Error in scan cycle", exc_info=True)

            await asyncio.sleep(self.scan_interval)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def setup_logging(debug: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Bluetooth Radar Scanner")
    parser.add_argument(
        "--discover", action="store_true",
        help="Scan and list all visible Bluetooth devices, then exit",
    )
    parser.add_argument(
        "--discover-wifi", action="store_true",
        help="Scan and list all WiFi/LAN devices, then exit",
    )
    parser.add_argument(
        "--wifi-interface", default="wlan0",
        help="Network interface for WiFi scanning (default: wlan0)",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug-level logging",
    )
    args = parser.parse_args()

    setup_logging(args.debug)

    if args.discover:
        scanner = BluetoothRadarScanner(DEFAULT_CONFIG)
        asyncio.run(scanner.discover())
        return

    if args.discover_wifi:
        asyncio.run(bt_wifi.discover_wifi(interface=args.wifi_interface))
        return

    config = load_or_create_config()

    # Resolve DB path
    db_path = Path(__file__).resolve().parent / config["db_path"]

    # Initialize database
    bt_db.init_db(db_path)

    # Migrate config.json devices to database
    migrate_config_devices(config, db_path)

    scanner = BluetoothRadarScanner(config)
    asyncio.run(scanner.run())


if __name__ == "__main__":
    main()
