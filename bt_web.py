#!/usr/bin/env python3
"""Bluetooth Radar Web Dashboard — Flask app for viewing and managing
discovered Bluetooth devices."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, redirect, render_template, request, session
from flask.sessions import SecureCookieSessionInterface
from itsdangerous import URLSafeTimedSerializer

import bt_auth
import bt_calendar
import bt_cleanup
import bt_db
import bt_health
import bt_news
import bt_pair
import bt_people

logger = logging.getLogger("bt_web")

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"),
            static_folder=str(BASE_DIR / "static"))

# Stops two "Clean up now" requests (e.g. a double click) running at once.
_cleanup_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Login (see bt_auth): reading stays open, changing data needs the password
# ---------------------------------------------------------------------------

AUTH_FILE = bt_auth.AUTH_FILE
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
_login_throttle = bt_auth.LoginThrottle()
_auth_cache: dict[str, Any] = {"mtime": object(), "data": {}}

_FALLBACK_KEY = secrets.token_hex(32)  # used only while no password is set; can never grant a login
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


def auth_data() -> dict[str, Any]:
    """The stored password hash/secret, re-read whenever the file changes (no restart needed)."""
    try:
        mtime = os.stat(AUTH_FILE).st_mtime_ns
    except OSError:
        mtime = None
    if _auth_cache["mtime"] != mtime:
        _auth_cache["mtime"] = mtime
        _auth_cache["data"] = bt_auth.load(AUTH_FILE) if mtime is not None else {}
    return _auth_cache["data"]


class _AuthSessionInterface(SecureCookieSessionInterface):
    """Signs login cookies with the stored secret, looked up when each request's session is opened.

    Flask opens the session *before* before_request handlers run, so swapping ``app.secret_key``
    from a handler would leave one request still honouring a cookie signed with the old secret.
    Asking for the key here means changing or removing the password takes effect immediately.
    """

    def get_signing_serializer(self, app):
        data = auth_data()
        key = data["secret"] if bt_auth.is_configured(data) else _FALLBACK_KEY
        return URLSafeTimedSerializer(
            key, salt=self.salt, serializer=self.serializer,
            signer_kwargs={"key_derivation": self.key_derivation, "digest_method": self.digest_method},
        )


app.session_interface = _AuthSessionInterface()


def auth_enabled() -> bool:
    return bt_auth.is_configured(auth_data())


def logged_in() -> bool:
    return auth_enabled() and session.get("auth") is True


@app.context_processor
def inject_auth() -> dict[str, bool]:
    return {"auth_enabled": auth_enabled(), "logged_in": logged_in()}


@app.before_request
def require_login_for_changes():
    """Block every request that changes data unless a password is unset or the user is logged in."""
    if request.method in _SAFE_METHODS or request.path in ("/login", "/logout"):
        return None
    if not auth_enabled() or logged_in():
        return None
    return jsonify({"error": "login required", "login": "/login"}), 401


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.route("/login", methods=["GET", "POST"])
def login():
    target = bt_auth.safe_next(request.values.get("next"))
    if request.method == "GET":
        if logged_in():
            return redirect(target)
        return render_template("login.html", active="", next=target, error=None, configured=auth_enabled())
    client = request.remote_addr or "unknown"
    if not auth_enabled():
        return render_template("login.html", active="", next=target, error=None, configured=False), 200
    if not _login_throttle.allowed(client):
        wait = max(1, _login_throttle.retry_after(client) // 60 + 1)
        return render_template("login.html", active="", next=target, configured=True,
                               error=f"Too many wrong passwords. Try again in about {wait} minute(s)."), 429
    if bt_auth.check_password(auth_data(), request.form.get("password", "")):
        _login_throttle.success(client)
        session.clear()
        session["auth"] = True
        session.permanent = True
        return redirect(target)
    _login_throttle.failure(client)
    logger.warning("Wrong dashboard password from %s", client)
    return render_template("login.html", active="", next=target, configured=True,
                           error="That password is not right."), 401


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/")



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
        auto_role=bt_people.role_from_type(device.get("device_type")),
        auto_person=bt_people.person_from_name(device.get("friendly_name")),
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
    for dev in devices:
        dev["effective_role"] = bt_people.effective_role(dev)
        dev["effective_person"] = bt_people.effective_person(dev)
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
    if "always_on" in data:
        kwargs["always_on"] = bool(data["always_on"])
    if "role" in data:
        role = (data["role"] or "").strip().lower()
        if role and role not in bt_people.ROLES:
            conn.close()
            return jsonify({"error": f"role must be one of {', '.join(bt_people.ROLES)}"}), 400
        kwargs["role"] = role  # "" clears it (the role is then worked out from the device type)
    if "person" in data:
        kwargs["person"] = bt_people.normalise_person(data["person"]) or ""

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


@app.route("/api/health")
def api_health():
    """Latest results of the health watchdog (see bt_health)."""
    conn = get_conn()
    try:
        return jsonify(bt_health.load_results(conn))
    finally:
        conn.close()


@app.route("/api/people")
def api_people():
    """Home/away for each person, decided by their phone (see bt_people)."""
    conn = get_conn()
    try:
        people = bt_people.people_status(conn, load_config())
    finally:
        conn.close()
    return jsonify(people)


@app.route("/api/stats")
def api_stats():
    conn = get_conn()
    stats = bt_db.get_stats(conn)
    conn.close()
    return jsonify(stats)


# ---------------------------------------------------------------------------
# Housekeeping API (stale device cleanup)
# ---------------------------------------------------------------------------

@app.route("/api/cleanup/preview")
def api_cleanup_preview():
    settings = bt_cleanup.load_settings(load_config())
    conn = get_conn()
    try:
        counts = bt_cleanup.preview(conn, settings)
    finally:
        conn.close()
    return jsonify({**counts, "settings": settings.as_dict()})


@app.route("/api/cleanup/run", methods=["POST"])
def api_cleanup_run():
    """Hide and delete stale devices now. Body: {"dry_run": bool} (default: real run)."""
    body = request.get_json(silent=True) or {}
    settings = bt_cleanup.load_settings(load_config())
    if not _cleanup_lock.acquire(blocking=False):
        return jsonify({"error": "A cleanup is already running"}), 409
    conn = get_conn()
    try:
        result = bt_cleanup.run_cleanup(
            conn, settings, force=True, dry_run=bool(body.get("dry_run", False)),
            unlimited=True, vacuum=True,
        )
    except Exception:
        logger.error("Manual cleanup failed", exc_info=True)
        return jsonify({"error": "Cleanup failed - see the bt-web log"}), 500
    finally:
        conn.close()
        _cleanup_lock.release()
    if result["error"]:
        return jsonify(result), 500
    logger.info("Manual cleanup: %s", result)
    return jsonify(result)


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
    if "tasks_enabled" in data:
        kwargs["tasks_enabled"] = bool(data["tasks_enabled"])
    if "tasks_interval" in data:
        kwargs["tasks_interval"] = int(data["tasks_interval"])

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
    try:
        from waitress import serve
    except ImportError:
        logger.warning("waitress is not installed (sudo apt install python3-waitress); "
                       "using Flask's development server")
        app.run(host="0.0.0.0", port=port, debug=False)
    else:
        serve(app, host="0.0.0.0", port=port, threads=4, ident="bt-web")


if __name__ == "__main__":
    main()
