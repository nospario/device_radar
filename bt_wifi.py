"""WiFi/LAN device scanning via ARP.

Performs a ping sweep to populate the ARP cache, then parses /proc/net/arp
to discover all devices on the local network. No external dependencies needed.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from dataclasses import dataclass
from ipaddress import IPv4Network
from typing import Optional

logger = logging.getLogger("bt_wifi")

# Common OUI prefixes (first 3 octets) -> vendor name
OUI_VENDORS: dict[str, str] = {
    "00:03:93": "Apple",
    "00:0A:95": "Apple",
    "00:17:F2": "Apple",
    "00:1B:63": "Apple",
    "00:1E:C2": "Apple",
    "00:25:00": "Apple",
    "00:26:BB": "Apple",
    "00:50:E4": "Apple",
    "04:0C:CE": "Apple",
    "04:15:52": "Apple",
    "04:26:65": "Apple",
    "08:66:98": "Apple",
    "10:DD:B1": "Apple",
    "14:10:9F": "Apple",
    "18:AF:8F": "Apple",
    "20:78:F0": "Apple",
    "24:A0:74": "Apple",
    "28:6A:BA": "Apple",
    "2C:BE:08": "Apple",
    "34:36:3B": "Apple",
    "38:F9:D3": "Apple",
    "3C:15:C2": "Apple",
    "40:A6:D9": "Apple",
    "44:D8:84": "Apple",
    "48:D7:05": "Apple",
    "4C:57:CA": "Apple",
    "50:ED:3C": "Apple",
    "54:4E:90": "Apple",
    "58:55:CA": "Apple",
    "5C:F7:E6": "Apple",
    "60:03:08": "Apple",
    "64:A3:CB": "Apple",
    "68:DB:CA": "Apple",
    "6C:94:66": "Apple",
    "70:DE:E2": "Apple",
    "74:E2:F5": "Apple",
    "78:CA:39": "Apple",
    "7C:D1:C3": "Apple",
    "80:E6:50": "Apple",
    "84:FC:FE": "Apple",
    "88:66:A5": "Apple",
    "8C:85:90": "Apple",
    "90:B2:1F": "Apple",
    "94:E9:79": "Apple",
    "98:01:A7": "Apple",
    "9C:20:7B": "Apple",
    "A4:D1:D2": "Apple",
    "A8:5C:2C": "Apple",
    "AC:BC:32": "Apple",
    "B0:34:95": "Apple",
    "B4:F0:AB": "Apple",
    "B8:09:8A": "Apple",
    "BC:52:B7": "Apple",
    "C0:B6:58": "Apple",
    "C8:69:CD": "Apple",
    "CC:08:8D": "Apple",
    "D0:03:4B": "Apple",
    "D4:F4:6F": "Apple",
    "DC:A4:CA": "Apple",
    "E0:B9:BA": "Apple",
    "E4:CE:8F": "Apple",
    "F0:B4:79": "Apple",
    "F4:5C:89": "Apple",
    "F8:1E:DF": "Apple",
    # Samsung
    "00:07:AB": "Samsung",
    "00:12:FB": "Samsung",
    "00:16:32": "Samsung",
    "00:1A:8A": "Samsung",
    "00:1E:E1": "Samsung",
    "00:21:19": "Samsung",
    "00:24:54": "Samsung",
    "00:26:37": "Samsung",
    "08:D4:2B": "Samsung",
    "10:D5:42": "Samsung",
    "14:49:E0": "Samsung",
    "18:3A:2D": "Samsung",
    "1C:62:B8": "Samsung",
    "24:4B:03": "Samsung",
    "28:98:7B": "Samsung",
    "2C:AE:2B": "Samsung",
    "30:C7:AE": "Samsung",
    "34:C3:AC": "Samsung",
    "38:01:95": "Samsung",
    "40:4E:36": "Samsung",
    "44:6D:6C": "Samsung",
    "4C:BC:98": "Samsung",
    "50:01:BB": "Samsung",
    "54:92:BE": "Samsung",
    "58:C3:8B": "Samsung",
    "5C:3C:27": "Samsung",
    "64:B5:C6": "Samsung",
    "6C:F3:73": "Samsung",
    "78:47:1D": "Samsung",
    "84:25:DB": "Samsung",
    "8C:71:F8": "Samsung",
    "94:35:0A": "Samsung",
    "98:52:B1": "Samsung",
    "A0:82:1F": "Samsung",
    "AC:5F:3E": "Samsung",
    "B4:79:A7": "Samsung",
    "BC:B1:F3": "Samsung",
    "C4:42:02": "Samsung",
    "CC:3A:61": "Samsung",
    "D0:22:BE": "Samsung",
    "D8:90:E8": "Samsung",
    "E4:7C:F9": "Samsung",
    "F0:25:B7": "Samsung",
    "F8:04:2E": "Samsung",
    "FC:A1:83": "Samsung",
    # Google / Nest
    "00:1A:11": "Google",
    "08:9E:08": "Google",
    "20:DF:B9": "Google",
    "30:FD:38": "Google",
    "3C:5A:B4": "Google",
    "44:07:0B": "Google",
    "48:D6:D5": "Google",
    "54:60:09": "Google",
    "5C:E8:83": "Google",
    "60:57:18": "Google",
    "64:B7:08": "Google",
    "6C:EC:EB": "Google",
    "7C:2E:BD": "Google",
    "94:EB:2C": "Google",
    "A4:77:33": "Google",
    "B4:A5:EF": "Google",
    "D4:E5:6D": "Google",
    "F4:F5:D8": "Google",
    "F4:F5:E8": "Google",
    "F8:8F:CA": "Google",
    # Amazon / Echo / Ring
    "00:FC:8B": "Amazon",
    "08:84:9D": "Amazon",
    "10:CE:A9": "Amazon",
    "14:91:82": "Amazon",
    "18:74:2E": "Amazon",
    "24:4C:E3": "Amazon",
    "34:D2:70": "Amazon",
    "38:F7:3D": "Amazon",
    "40:A2:DB": "Amazon",
    "44:00:49": "Amazon",
    "48:45:20": "Amazon",
    "50:DC:E7": "Amazon",
    "68:37:E9": "Amazon",
    "6C:56:97": "Amazon",
    "74:C2:46": "Amazon",
    "84:D6:D0": "Amazon",
    "8C:49:62": "Amazon",
    "A0:02:DC": "Amazon",
    "AC:63:BE": "Amazon",
    "B0:FC:36": "Amazon",
    "B4:7C:9C": "Amazon",
    "CC:F7:35": "Amazon",
    "F0:27:2D": "Amazon",
    "F0:F0:A4": "Amazon",
    "FC:65:DE": "Amazon",
    # Garmin
    "00:1C:0D": "Garmin",
    "00:1D:E0": "Garmin",
    "C8:3E:99": "Garmin",
    # Huawei / Honor
    "00:1E:10": "Huawei",
    "00:25:68": "Huawei",
    "04:B0:E7": "Huawei",
    "08:63:61": "Huawei",
    "10:1B:54": "Huawei",
    "18:DE:D7": "Huawei",
    "20:A6:80": "Huawei",
    "24:09:95": "Huawei",
    "28:31:52": "Huawei",
    "30:D1:7E": "Huawei",
    "48:46:FB": "Huawei",
    "54:A5:1B": "Huawei",
    "5C:C3:07": "Huawei",
    "60:DE:44": "Huawei",
    "70:8A:09": "Huawei",
    "80:B6:86": "Huawei",
    "88:53:95": "Huawei",
    "C0:70:09": "Huawei",
    "CC:A2:23": "Huawei",
    "D4:6E:5C": "Huawei",
    "E8:CD:2D": "Huawei",
    "F4:C7:14": "Huawei",
    # Sony
    "00:13:A9": "Sony",
    "00:19:63": "Sony",
    "00:1D:BA": "Sony",
    "00:24:BE": "Sony",
    "04:5D:4B": "Sony",
    "28:0D:FC": "Sony",
    "40:B8:37": "Sony",
    "78:84:3C": "Sony",
    "AC:9B:0A": "Sony",
    "FC:0F:E6": "Sony",
    # Microsoft / Xbox
    "00:50:F2": "Microsoft",
    "28:18:78": "Microsoft",
    "3C:83:75": "Microsoft",
    "50:1A:C5": "Microsoft",
    "58:82:A8": "Microsoft",
    "7C:1E:52": "Microsoft",
    "7C:ED:8D": "Microsoft",
    "98:5F:D3": "Microsoft",
    "B4:0E:DE": "Microsoft",
    "C8:3F:26": "Microsoft",
    "DC:B4:C4": "Microsoft",
    # TP-Link
    "00:31:92": "TP-Link",
    "10:FE:ED": "TP-Link",
    "14:EB:B6": "TP-Link",
    "18:A6:F7": "TP-Link",
    "30:DE:4B": "TP-Link",
    "3C:52:A1": "TP-Link",
    "50:C7:BF": "TP-Link",
    "54:AF:97": "TP-Link",
    "60:A4:B7": "TP-Link",
    "68:FF:7B": "TP-Link",
    "78:8C:B5": "TP-Link",
    "84:16:F9": "TP-Link",
    "98:DA:C4": "TP-Link",
    "A4:2B:B0": "TP-Link",
    "B0:A7:B9": "TP-Link",
    "C0:06:C3": "TP-Link",
    "C0:25:E9": "TP-Link",
    "D8:07:B6": "TP-Link",
    "E8:48:B8": "TP-Link",
    "F4:F2:6D": "TP-Link",
    # Intel
    "00:02:B3": "Intel",
    "00:13:02": "Intel",
    "00:1B:21": "Intel",
    "00:1E:64": "Intel",
    "00:22:FA": "Intel",
    "3C:F0:11": "Intel",
    "48:51:B7": "Intel",
    "5C:87:9C": "Intel",
    "68:05:CA": "Intel",
    "80:86:F2": "Intel",
    "8C:8D:28": "Intel",
    "A4:C4:94": "Intel",
    "B4:96:91": "Intel",
    # Raspberry Pi
    "28:CD:C1": "Raspberry Pi",
    "B8:27:EB": "Raspberry Pi",
    "D8:3A:DD": "Raspberry Pi",
    "DC:A6:32": "Raspberry Pi",
    "E4:5F:01": "Raspberry Pi",
    # Xiaomi
    "00:9E:C8": "Xiaomi",
    "04:CF:8C": "Xiaomi",
    "0C:1D:AF": "Xiaomi",
    "10:2A:B3": "Xiaomi",
    "14:F6:5A": "Xiaomi",
    "18:59:36": "Xiaomi",
    "20:47:DA": "Xiaomi",
    "28:6C:07": "Xiaomi",
    "34:CE:00": "Xiaomi",
    "38:A4:ED": "Xiaomi",
    "50:64:2B": "Xiaomi",
    "58:44:98": "Xiaomi",
    "64:CC:2E": "Xiaomi",
    "74:23:44": "Xiaomi",
    "7C:8B:CA": "Xiaomi",
    "8C:BE:BE": "Xiaomi",
    "98:FA:E3": "Xiaomi",
    "AC:C1:EE": "Xiaomi",
    "F0:B4:29": "Xiaomi",
    "FC:64:BA": "Xiaomi",
    # OnePlus
    "64:A2:F9": "OnePlus",
    "94:65:2D": "OnePlus",
    "C0:EE:40": "OnePlus",
    # LG
    "00:1C:62": "LG",
    "00:1E:75": "LG",
    "10:68:3F": "LG",
    "20:3D:BD": "LG",
    "34:4D:F7": "LG",
    "58:A2:B5": "LG",
    "78:F8:82": "LG",
    "88:C9:D0": "LG",
    "A8:16:B2": "LG",
    "BC:F5:AC": "LG",
    "CC:FA:00": "LG",
    "E8:F2:E2": "LG",
    # Roku
    "B0:A7:37": "Roku",
    "CC:6D:A0": "Roku",
    "D4:E2:2F": "Roku",
    # Sonos
    "00:0E:58": "Sonos",
    "34:7E:5C": "Sonos",
    "48:A6:B8": "Sonos",
    "5C:AA:FD": "Sonos",
    "78:28:CA": "Sonos",
    "94:9F:3E": "Sonos",
    "B8:E9:37": "Sonos",
    # Philips / Signify (Hue)
    "00:17:88": "Philips Hue",
    "EC:B5:FA": "Philips Hue",
}


@dataclass
class WifiDevice:
    """A device discovered via WiFi/LAN scanning."""
    ip_address: str
    mac_address: str
    hostname: Optional[str] = None
    vendor: Optional[str] = None


def detect_subnet(interface: str = "wlan0") -> Optional[str]:
    """Detect the subnet CIDR for the given network interface.

    Parses output of `ip -4 addr show <interface>` to find the network.
    Returns e.g. '192.168.1.0/24' or None if detection fails.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["ip", "-4", "addr", "show", interface],
            capture_output=True, text=True, timeout=5,
        )
        # Look for inet x.x.x.x/prefix
        match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", result.stdout)
        if match:
            ip_addr = match.group(1)
            prefix = match.group(2)
            network = IPv4Network(f"{ip_addr}/{prefix}", strict=False)
            return str(network)
    except Exception:
        logger.error("Failed to detect subnet for %s", interface, exc_info=True)
    return None


async def ping_sweep(network: str, concurrency: int = 50) -> None:
    """Ping all hosts in the subnet to populate the ARP cache.

    Uses asyncio.Semaphore to limit concurrency.
    """
    sem = asyncio.Semaphore(concurrency)
    net = IPv4Network(network, strict=False)

    async def _ping(ip: str) -> None:
        async with sem:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ping", "-c", "1", "-W", "1", ip,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=3)
            except (asyncio.TimeoutError, Exception):
                pass

    tasks = [_ping(str(ip)) for ip in net.hosts()]
    await asyncio.gather(*tasks)


def read_arp_table(interface: str = "wlan0") -> list[tuple[str, str]]:
    """Parse /proc/net/arp for complete entries on the given interface.

    Returns list of (ip_address, mac_address) tuples.
    Only includes entries with HW type 0x1 (ethernet) and flags 0x2 (complete).
    """
    entries: list[tuple[str, str]] = []
    try:
        with open("/proc/net/arp", "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("IP address") or not line:
                    continue
                parts = line.split()
                if len(parts) < 6:
                    continue
                ip = parts[0]
                flags = parts[2]
                mac = parts[3].upper()
                dev = parts[5]
                # flags 0x2 = complete entry, skip 0x0 (incomplete)
                if dev == interface and flags != "0x0" and mac != "00:00:00:00:00:00":
                    entries.append((ip, mac))
    except Exception:
        logger.error("Failed to read /proc/net/arp", exc_info=True)
    return entries


def _resolve_hostname(ip: str) -> Optional[str]:
    """Try to resolve a hostname for the given IP address."""
    try:
        hostname, _, _ = socket.gethostbyaddr(ip)
        return hostname
    except (socket.herror, socket.gaierror, OSError):
        return None


def lookup_oui_vendor(mac: str) -> Optional[str]:
    """Look up the vendor from the OUI (first 3 octets) of a MAC address."""
    prefix = mac.upper()[:8]  # "AA:BB:CC"
    return OUI_VENDORS.get(prefix)


async def scan_wifi(
    interface: str = "wlan0",
    subnet: Optional[str] = None,
) -> list[WifiDevice]:
    """Perform a full WiFi/LAN scan.

    1. Detect subnet if not provided
    2. Ping sweep to populate ARP cache
    3. Read ARP table
    4. Resolve hostnames and look up vendors

    Returns list of WifiDevice.
    """
    if subnet is None:
        subnet = detect_subnet(interface)
        if subnet is None:
            logger.error("Could not detect subnet for %s — skipping WiFi scan", interface)
            return []

    logger.debug("WiFi scan: ping sweep on %s", subnet)
    await ping_sweep(subnet)

    logger.debug("WiFi scan: reading ARP table for %s", interface)
    arp_entries = read_arp_table(interface)

    devices: list[WifiDevice] = []
    for ip, mac in arp_entries:
        hostname = _resolve_hostname(ip)
        vendor = lookup_oui_vendor(mac)
        devices.append(WifiDevice(
            ip_address=ip,
            mac_address=mac,
            hostname=hostname,
            vendor=vendor,
        ))

    logger.debug("WiFi scan found %d devices", len(devices))
    return devices


async def discover_wifi(
    interface: str = "wlan0",
    subnet: Optional[str] = None,
) -> None:
    """Scan and print all LAN devices for discovery purposes."""
    print(f"Scanning WiFi/LAN devices on {interface}...")

    if subnet is None:
        subnet = detect_subnet(interface)
        if subnet is None:
            print(f"ERROR: Could not detect subnet for {interface}")
            return
        print(f"Detected subnet: {subnet}")

    print("Running ping sweep (this may take a few seconds)...")
    await ping_sweep(subnet)

    arp_entries = read_arp_table(interface)

    if not arp_entries:
        print("No devices found on the network.")
        return

    results: list[WifiDevice] = []
    for ip, mac in arp_entries:
        hostname = _resolve_hostname(ip)
        vendor = lookup_oui_vendor(mac)
        results.append(WifiDevice(ip_address=ip, mac_address=mac,
                                  hostname=hostname, vendor=vendor))

    print(f"\n{'IP ADDRESS':<18} {'MAC ADDRESS':<20} {'HOSTNAME':<30} {'VENDOR'}")
    print("-" * 90)
    for d in sorted(results, key=lambda x: tuple(int(p) for p in x.ip_address.split("."))):
        hostname_str = d.hostname or "(unknown)"
        vendor_str = d.vendor or ""
        print(f"{d.ip_address:<18} {d.mac_address:<20} {hostname_str:<30} {vendor_str}")

    print(f"\n{len(results)} device(s) found.")
