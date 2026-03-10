#!/usr/bin/env python3
"""Bluetooth Radar Web Dashboard — Flask app for viewing and managing
discovered Bluetooth devices."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

import bt_calendar
import bt_db
import bt_news
import bt_pair

logger = logging.getLogger("bt_web")

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"),
            static_folder=str(BASE_DIR / "static"))


def load_config() -> dict[str, Any]:
    """Load configuration."""
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open() as f:
            return json.load(f)
    return {}


def get_db_path() -> Path:
    config = load_config()
    return BASE_DIR / config.get("db_path", "bt_radar.db")


def get_conn():
    return bt_db.get_connection(get_db_path())


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    return render_template("dashboard.html", active="dashboard")


@app.route("/device/<path:mac>")
def device_detail(mac: str):
    conn = get_conn()
    device = bt_db.get_device(conn, mac)
    if not device:
        conn.close()
        return "Device not found", 404
    events = bt_db.get_events(conn, mac=mac, limit=50)
    link_group = bt_db.get_link_group(conn, mac)

    # Build list of devices that can be linked to this one
    # Exclude: self, already linked devices in this group, and devices that are secondaries of another group
    group_macs = set()
    if link_group["primary"]:
        group_macs.add(link_group["primary"]["mac_address"])
    for s in link_group["secondaries"]:
        group_macs.add(s["mac_address"])

    all_devs = bt_db.get_all_devices(conn, include_hidden=False)
    linkable = [
        d for d in all_devs
        if d["mac_address"] not in group_macs and not d.get("linked_to")
    ]

    # Collect custom device types (types in the DB not in the standard list)
    standard_types = {
        'Unknown', 'Phone', 'Tablet', 'Watch', 'Laptop', 'Desktop',
        'Printer', 'Speaker', 'Headphones', 'TV', 'Gaming Console',
        'IoT', 'Beacon', 'Other', 'Network Device',
    }
    rows = conn.execute(
        "SELECT DISTINCT device_type FROM devices WHERE device_type IS NOT NULL"
    ).fetchall()
    custom_types = sorted(
        {r["device_type"] for r in rows if r["device_type"] not in standard_types}
    )

    echo_devices = bt_db.get_all_echo_devices(conn)

    conn.close()

    config = load_config()
    calendar_names = bt_calendar.get_available_calendars(config)
    try:
        device_calendars = json.loads(device.get("calendar_calendars") or "[]")
    except (json.JSONDecodeError, TypeError):
        device_calendars = []

    news_feeds = bt_news.get_available_feeds()
    try:
        device_news_feeds = json.loads(device.get("news_feeds") or "[]")
    except (json.JSONDecodeError, TypeError):
        device_news_feeds = []

    return render_template(
        "device.html", device=device, events=events,
        link_group=link_group, linkable=linkable,
        custom_types=custom_types, echo_devices=echo_devices,
        calendar_names=calendar_names, device_calendars=device_calendars,
        news_feeds=news_feeds, device_news_feeds=device_news_feeds,
        active="",
    )


@app.route("/history")
def history():
    return render_template("history.html", active="history")


@app.route("/pairing")
def pairing():
    return render_template("pairing.html", active="pairing")


@app.route("/alexa")
def alexa():
    config = load_config()
    alexa_enabled = config.get("alexa_enabled", False)
    return render_template("alexa.html", active="alexa", alexa_enabled=alexa_enabled)


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.route("/api/devices")
def api_devices():
    conn = get_conn()
    state = request.args.get("state")
    watchlisted = request.args.get("watchlisted") == "1"
    include_hidden = request.args.get("hidden") == "1"
    scan_type = request.args.get("scan_type")
    unmerged = request.args.get("unmerged") == "1"

    if unmerged:
        devices = bt_db.get_all_devices(
            conn,
            state=state or None,
            watchlisted_only=watchlisted,
            include_hidden=include_hidden,
            scan_type=scan_type or None,
        )
    else:
        devices = bt_db.get_all_devices_merged(
            conn,
            state=state or None,
            watchlisted_only=watchlisted,
            include_hidden=include_hidden,
            scan_type=scan_type or None,
        )
    conn.close()
    return jsonify(devices)


@app.route("/api/devices/<path:mac>")
def api_device(mac: str):
    conn = get_conn()
    device = bt_db.get_device(conn, mac)
    conn.close()
    if not device:
        return jsonify({"error": "not found"}), 404
    return jsonify(device)


@app.route("/api/devices/<path:mac>", methods=["PATCH"])
def api_update_device(mac: str):
    conn = get_conn()
    data = request.get_json()
    if not data:
        conn.close()
        return jsonify({"error": "no data"}), 400

    kwargs: dict[str, Any] = {}
    if "friendly_name" in data:
        kwargs["friendly_name"] = data["friendly_name"]
    if "device_type" in data:
        kwargs["device_type"] = data["device_type"]
    if "is_watchlisted" in data:
        kwargs["is_watchlisted"] = bool(data["is_watchlisted"])
    if "is_hidden" in data:
        kwargs["is_hidden"] = bool(data["is_hidden"])
    if "is_notify" in data:
        kwargs["is_notify"] = bool(data["is_notify"])
    if "is_welcome" in data:
        kwargs["is_welcome"] = bool(data["is_welcome"])
    if "proximity_enabled" in data:
        kwargs["proximity_enabled"] = bool(data["proximity_enabled"])
    if "proximity_rssi_threshold" in data:
        kwargs["proximity_rssi_threshold"] = int(data["proximity_rssi_threshold"])
    if "proximity_interval" in data:
        kwargs["proximity_interval"] = int(data["proximity_interval"])
    if "proximity_alexa_device" in data:
        kwargs["proximity_alexa_device"] = data["proximity_alexa_device"] or None
    if "proximity_prompt" in data:
        kwargs["proximity_prompt"] = data["proximity_prompt"]
    if "calendar_calendars" in data:
        kwargs["calendar_calendars"] = data["calendar_calendars"]
    if "news_feeds" in data:
        kwargs["news_feeds"] = data["news_feeds"]
    if "alexa_voice" in data:
        kwargs["alexa_voice"] = data["alexa_voice"]

    updated = bt_db.update_device(conn, mac, **kwargs)
    conn.close()

    if not updated:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/devices/present")
def api_devices_present():
    """Return only devices currently detected as present."""
    conn = get_conn()
    devices = bt_db.get_all_devices_merged(conn, state="DETECTED", include_hidden=False)
    conn.close()
    return jsonify(devices)


@app.route("/api/device/<path:device_id>/notifications", methods=["POST"])
def api_device_notifications(device_id: str):
    """Toggle notifications on or off for a specific device."""
    conn = get_conn()
    data = request.get_json()
    if not data or "enabled" not in data:
        conn.close()
        return jsonify({"error": "enabled field required"}), 400

    enabled = bool(data["enabled"])
    updated = bt_db.update_device(conn, device_id, is_notify=enabled)
    conn.close()

    if not updated:
        return jsonify({"error": "not found"}), 404
    return jsonify({"id": device_id.upper(), "notifications_enabled": enabled})


@app.route("/api/events")
def api_events():
    conn = get_conn()
    mac = request.args.get("mac")
    event_type = request.args.get("event_type")
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))

    events = bt_db.get_events(conn, mac=mac or None, event_type=event_type or None,
                              limit=limit, offset=offset)
    total = bt_db.count_events(conn, mac=mac or None, event_type=event_type or None)
    conn.close()
    return jsonify({"events": events, "total": total})


@app.route("/api/stats")
def api_stats():
    conn = get_conn()
    stats = bt_db.get_stats(conn)
    conn.close()
    return jsonify(stats)


# ---------------------------------------------------------------------------
# Pairing API
# ---------------------------------------------------------------------------

@app.route("/api/devices/<path:mac>/pair", methods=["POST"])
def api_pair_device(mac: str):
    result = bt_pair.pair_device(mac)
    if result.success:
        conn = get_conn()
        bt_db.update_device(conn, mac, is_paired=True)
        conn.close()
    return jsonify({"success": result.success, "message": result.message})


@app.route("/api/devices/<path:mac>/unpair", methods=["POST"])
def api_unpair_device(mac: str):
    result = bt_pair.unpair_device(mac)
    if result.success:
        conn = get_conn()
        bt_db.update_device(conn, mac, is_paired=False)
        conn.close()
    return jsonify({"success": result.success, "message": result.message})


@app.route("/api/devices/<path:mac>/pair-status")
def api_pair_status(mac: str):
    info = bt_pair.get_device_info(mac)
    if info is None:
        return jsonify({"paired": False, "trusted": False, "connected": False})
    return jsonify({
        "paired": info.paired,
        "trusted": info.trusted,
        "connected": info.connected,
        "name": info.name,
    })


# ---------------------------------------------------------------------------
# Device Linking API
# ---------------------------------------------------------------------------

@app.route("/api/devices/<path:mac>/link", methods=["POST"])
def api_link_device(mac: str):
    data = request.get_json()
    if not data or "target_mac" not in data:
        return jsonify({"error": "target_mac required"}), 400

    target_mac = data["target_mac"]
    conn = get_conn()

    # Determine which is primary: the current device page is the primary
    ok = bt_db.link_device(conn, secondary_mac=target_mac, primary_mac=mac)
    conn.close()

    if not ok:
        return jsonify({"error": "cannot link device to itself"}), 400
    return jsonify({"ok": True})


@app.route("/api/devices/<path:mac>/unlink", methods=["POST"])
def api_unlink_device(mac: str):
    conn = get_conn()
    ok = bt_db.unlink_device(conn, mac)
    conn.close()

    if not ok:
        return jsonify({"error": "device was not linked"}), 400
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Echo Devices API (Alexa encourage mode)
# ---------------------------------------------------------------------------

@app.route("/api/echo-devices")
def api_echo_devices():
    conn = get_conn()
    devices = bt_db.get_all_echo_devices(conn)
    conn.close()
    return jsonify(devices)


@app.route("/api/echo-devices", methods=["POST"])
def api_create_echo_device():
    data = request.get_json()
    if not data or "device_name" not in data:
        return jsonify({"error": "device_name required"}), 400

    conn = get_conn()
    bt_db.upsert_echo_device(
        conn,
        data["device_name"],
        alias=data.get("alias"),
        encourage_enabled=data.get("encourage_enabled", False),
        encourage_interval=data.get("encourage_interval", 30),
        encourage_prompt=data.get("encourage_prompt", ""),
    )
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/echo-devices/<path:name>", methods=["PATCH"])
def api_update_echo_device(name: str):
    data = request.get_json()
    if not data:
        return jsonify({"error": "no data"}), 400

    conn = get_conn()
    kwargs: dict[str, Any] = {}
    if "alias" in data:
        kwargs["alias"] = data["alias"]
    if "encourage_enabled" in data:
        kwargs["encourage_enabled"] = bool(data["encourage_enabled"])
    if "encourage_interval" in data:
        kwargs["encourage_interval"] = int(data["encourage_interval"])
    if "encourage_prompt" in data:
        kwargs["encourage_prompt"] = data["encourage_prompt"]

    bt_db.upsert_echo_device(conn, name, **kwargs)
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/echo-devices/<path:name>", methods=["DELETE"])
def api_delete_echo_device(name: str):
    conn = get_conn()
    ok = bt_db.delete_echo_device(conn, name)
    conn.close()
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config()
    port = config.get("web_port", 8080)

    # Ensure DB exists
    bt_db.init_db(get_db_path())

    logger.info("Starting Bluetooth Radar dashboard on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
