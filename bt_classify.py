"""Device classification logic for Bluetooth Radar.

Identifies device type and manufacturer from BLE advertisement data,
classic Bluetooth device class codes, advertised names, and service UUIDs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class DeviceInfo:
    """Classification result for a discovered device."""
    device_type: str = "Unknown"
    manufacturer: str | None = None


# ---------------------------------------------------------------------------
# Manufacturer data (company ID -> manufacturer name)
# ---------------------------------------------------------------------------

MANUFACTURER_IDS: dict[int, str] = {
    76: "Apple",
    117: "Samsung",
    224: "Google",
    6: "Microsoft",
    89: "Nordic Semiconductor",
    343: "Xiaomi",
    741: "Xiaomi",
    301: "Huawei",
    637: "Huawei",
    15: "Broadcom",
    13: "Texas Instruments",
    10: "Qualcomm",
    3503: "Tile",
    919: "Garmin",
    269: "Fitbit",
    135: "Bose",
    87: "Jabra",
    86: "Sony",
    256: "Logitech",
}

# Apple sub-type byte (byte index 0 of manufacturer-specific data for company 76)
APPLE_DEVICE_TYPES: dict[int, str] = {
    0x01: "iPhone",
    0x02: "iPhone",       # iBeacon
    0x03: "AirPrint",
    0x05: "AirDrop",
    0x06: "HomeKit",
    0x07: "AirPods",
    0x08: "Siri",
    0x09: "AirPlay",
    0x0A: "iPhone",
    0x0B: "Apple Watch",
    0x0C: "Handoff",
    0x0D: "Wi-Fi Settings",
    0x0E: "Hotspot",
    0x0F: "Nearby Join",
    0x10: "Nearby Action",
    0x12: "Nearby Info",
    0x14: "AirPods",
}


# ---------------------------------------------------------------------------
# Classic Bluetooth major device classes
# ---------------------------------------------------------------------------

MAJOR_DEVICE_CLASSES: dict[int, str] = {
    1: "Computer",
    2: "Phone",
    3: "Network AP",
    4: "Audio/Video",
    5: "Peripheral",
    6: "Imaging",
    7: "Wearable",
    8: "Toy",
    9: "Health",
}

# Minor classes for Phone (major class 2)
PHONE_MINOR_CLASSES: dict[int, str] = {
    0: "Phone",
    1: "Cell Phone",
    2: "Cordless Phone",
    3: "Smartphone",
    4: "Modem/Gateway",
    5: "ISDN Access",
}

# Minor classes for Audio/Video (major class 4)
AV_MINOR_CLASSES: dict[int, str] = {
    1: "Wearable Headset",
    2: "Hands-free",
    4: "Microphone",
    5: "Loudspeaker",
    6: "Headphones",
    7: "Portable Audio",
    8: "Car Audio",
    9: "Set-top Box",
    10: "HiFi Audio",
    11: "VCR",
    12: "Video Camera",
    13: "Camcorder",
    14: "Video Monitor",
    15: "Video Display/Speaker",
}

# Minor classes for Wearable (major class 7)
WEARABLE_MINOR_CLASSES: dict[int, str] = {
    1: "Wrist Watch",
    2: "Pager",
    3: "Jacket",
    4: "Helmet",
    5: "Glasses",
}


# ---------------------------------------------------------------------------
# Name patterns -> device type
# ---------------------------------------------------------------------------

NAME_PATTERNS: list[tuple[str, str, str | None]] = [
    # (regex_pattern, device_type, manufacturer)
    (r"iPhone", "Phone", "Apple"),
    (r"iPad", "Tablet", "Apple"),
    (r"MacBook", "Computer", "Apple"),
    (r"Apple\s?Watch", "Watch", "Apple"),
    (r"AirPods", "Earbuds", "Apple"),
    (r"HomePod", "Speaker", "Apple"),
    (r"Galaxy\s?Watch", "Watch", "Samsung"),
    (r"Galaxy\s?(S|A|Z|Note|Fold|Flip)", "Phone", "Samsung"),
    (r"Galaxy\s?Tab", "Tablet", "Samsung"),
    (r"Galaxy\s?Buds", "Earbuds", "Samsung"),
    (r"Pixel\s?(Watch|\d)", "Phone", "Google"),
    (r"Pixel\s?Watch", "Watch", "Google"),
    (r"Pixel\s?Buds", "Earbuds", "Google"),
    (r"Fitbit", "Fitness Tracker", "Fitbit"),
    (r"Garmin", "Watch", "Garmin"),
    (r"Tile\b", "Tracker", "Tile"),
    (r"AirTag", "Tracker", "Apple"),
    (r"SmartTag", "Tracker", "Samsung"),
    (r"Bose.*(QC|QuietComfort|NC|700)", "Headphones", "Bose"),
    (r"Bose", "Audio", "Bose"),
    (r"Sony.*WH-", "Headphones", "Sony"),
    (r"Sony.*WF-", "Earbuds", "Sony"),
    (r"JBL", "Audio", "JBL"),
    (r"Echo\b|Alexa", "Speaker", "Amazon"),
    (r"Raspberry\s?Pi", "Computer", "Raspberry Pi"),
    (r"Nintendo\s?Switch", "Game Console", "Nintendo"),
    (r"Xbox", "Game Console", "Microsoft"),
    (r"PlayStation|DualSense|DualShock", "Game Console", "Sony"),
]


# ---------------------------------------------------------------------------
# Service UUIDs -> device type hints
# ---------------------------------------------------------------------------

SERVICE_UUID_HINTS: dict[str, str] = {
    "0000180d": "Fitness Device",    # Heart Rate
    "00001812": "Input Device",      # HID
    "0000180f": "Peripheral",        # Battery Service
    "0000180a": "Device",            # Device Information
    "00001802": "Alert Device",      # Immediate Alert
    "00001803": "Device",            # Link Loss
    "0000fff0": "IoT Device",        # Common custom service
    "0000fe9f": "Phone",             # Google Fast Pair
    "0000fd6f": "Device",            # COVID Exposure Notification
}


# ---------------------------------------------------------------------------
# Main classification function
# ---------------------------------------------------------------------------

def classify_device(
    *,
    advertised_name: str | None = None,
    manufacturer_data: dict[int, bytes] | None = None,
    service_uuids: list[str] | None = None,
    device_class: int | None = None,
) -> DeviceInfo:
    """Classify a Bluetooth device using available data.

    Priority:
    1. Manufacturer data (company IDs, Apple sub-types)
    2. Classic Bluetooth device class codes
    3. Advertised name patterns
    4. Service UUID hints
    """
    info = DeviceInfo()

    # 1. Manufacturer data
    if manufacturer_data:
        for company_id, data in manufacturer_data.items():
            if company_id in MANUFACTURER_IDS:
                info.manufacturer = MANUFACTURER_IDS[company_id]

                # Apple-specific sub-type detection
                if company_id == 76 and len(data) >= 1:
                    sub_type = data[0]
                    if sub_type in APPLE_DEVICE_TYPES:
                        info.device_type = APPLE_DEVICE_TYPES[sub_type]
                        return info
                    info.device_type = "Apple Device"
                    return info

                # Samsung with data often means a phone
                if company_id == 117:
                    info.device_type = "Samsung Device"
                    return info

                # Google
                if company_id == 224:
                    info.device_type = "Google Device"
                    return info

                break

    # 2. Classic Bluetooth device class
    if device_class is not None and device_class > 0:
        major = (device_class >> 8) & 0x1F
        minor = (device_class >> 2) & 0x3F

        if major in MAJOR_DEVICE_CLASSES:
            info.device_type = MAJOR_DEVICE_CLASSES[major]

            if major == 2 and minor in PHONE_MINOR_CLASSES:
                info.device_type = PHONE_MINOR_CLASSES[minor]
            elif major == 4 and minor in AV_MINOR_CLASSES:
                info.device_type = AV_MINOR_CLASSES[minor]
            elif major == 7 and minor in WEARABLE_MINOR_CLASSES:
                info.device_type = WEARABLE_MINOR_CLASSES[minor]

            return info

    # 3. Name pattern matching
    if advertised_name:
        for pattern, dev_type, mfr in NAME_PATTERNS:
            if re.search(pattern, advertised_name, re.IGNORECASE):
                info.device_type = dev_type
                if mfr:
                    info.manufacturer = info.manufacturer or mfr
                return info

    # 4. Service UUID hints
    if service_uuids:
        for uuid in service_uuids:
            short = uuid.lower()[:8]
            if short in SERVICE_UUID_HINTS:
                info.device_type = SERVICE_UUID_HINTS[short]
                return info

    return info


def is_random_mac(mac: str) -> bool:
    """Check if a MAC address is locally administered (random).

    The locally administered bit is bit 1 of the first octet.
    This means the second hex character is one of: 2, 3, 6, 7, A, B, E, F.
    """
    if len(mac) < 2:
        return False
    second_char = mac[1].upper()
    return second_char in ("2", "3", "6", "7", "A", "B", "E", "F")


def parse_device_class(class_hex: str) -> int | None:
    """Parse a device class from hex string (e.g. '0x5a020c')."""
    try:
        return int(class_hex, 16)
    except (ValueError, TypeError):
        return None
