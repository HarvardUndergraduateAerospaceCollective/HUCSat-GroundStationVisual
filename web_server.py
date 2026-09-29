"""
Mission Control — Web Dashboard Server

Replaces the matplotlib-based missioncontrol.py with a Flask + SocketIO
server that feeds a Leaflet/Chart.js browser frontend.

Usage::

    python web_server.py                  # offline mode (orbital only)
    python web_server.py --live           # live MQTT + orbital
    python web_server.py --port 8080      # custom port

Then open http://localhost:5000 (or your Pi's IP) in a browser.
"""

import argparse
import json
import logging
import os
import socket
import time
from datetime import datetime, timezone
from threading import Lock
from urllib.request import Request, urlopen

import numpy as np
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

import mission_time
import visualizer
import panel_altitude
import panel_signal
import panel_temperature
import panel_power
import panel_magnetometer
import panel_best_dir
import packet_store

# ──────────────────────────────────────────────
# Mission epoch (deployment) — defined in mission_time.py, which the telemetry
# charts' MET axis also uses. None would make MET show --:--:--.
# ──────────────────────────────────────────────
MISSION_EPOCH_UTC = mission_time.MISSION_EPOCH_UTC

# CARTO basemap tiles need an API key (since 2026-09); without one CARTO serves
# "API KEY REQUIRED" placeholder tiles. Set it in mission.env, not in the repo.
CARTO_BASEMAPS_KEY = os.environ.get("CARTO_BASEMAPS_KEY", "")

log = logging.getLogger(__name__)
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = "missioncontrol"
socketio = SocketIO(app, async_mode="threading")

# ──────────────────────────────────────────────
# Orbital state (computed once at startup, refreshed on demand)
# ──────────────────────────────────────────────

_state_lock = Lock()
_state = {
    "t0": 0.0,
    "live": False,
    "n_orbits": 3.0,
    "tle_epoch_unix": 0.0,
    "mean_anomaly_deg": 0.0,
    "orbital": {},
    "period": 0.0,
    "alt_km": 0.0,
    "inc": 0.0,
    "raan": 0.0,
    "ecc": 0.0,
    "argp": 0.0,
    "sma": 0.0,
}


def _init_orbital(reset_t0: bool = True):
    """Fetch TLE and populate global orbital state.

    reset_t0=True (startup) anchors the ground-track head-start clock (`t0`,
    used by _current_n_orbits) to now. The periodic refresher passes
    reset_t0=False so re-fetching fresh elements every 2h updates the orbit
    without resetting the map's history-growth reference.

    The TLE fetch happens before any state is touched, so a failed fetch
    raises here and leaves the previous elements intact.
    """
    tle_state = visualizer.get_orbital_state()
    inc = tle_state["inclination"]
    raan = tle_state["raan"]
    ecc = tle_state["eccentricity"]
    argp = tle_state["arg_periapsis"]
    sma = tle_state["semi_major_axis"]
    mean_anomaly_deg = tle_state["mean_anomaly_deg"]
    tle_epoch_unix = tle_state["epoch_unix"]

    period = visualizer.orbital_period(sma)
    alt_km = (sma - visualizer.R_EARTH) / 1000
    with _state_lock:
        if reset_t0:
            _state["t0"] = time.time()
        _state.update(
            tle_epoch_unix=tle_epoch_unix,
            mean_anomaly_deg=mean_anomaly_deg,
            inc=inc, raan=raan, ecc=ecc, argp=argp, sma=sma,
            period=period,
            alt_km=alt_km,
            orbital={
                "sma": sma,
                "eccentricity": ecc,
                "inclination": inc,
                "raan": raan,
                "arg_periapsis": argp,
            },
        )


# Re-fetch the TLE on the same cadence as visualizer's disk cache (2h): each
# tick the cache is expired, so CelesTrak is re-queried and the orbit is
# re-anchored to the latest epoch instead of propagating stale elements for
# days on a long-running process.
_TLE_REFRESH_INTERVAL = visualizer.CACHE_MAX_AGE   # seconds (2h)


def _tle_refresher():
    """Background task: refresh the cached TLE every _TLE_REFRESH_INTERVAL.
    A failed fetch is logged and the previous elements are kept."""
    while True:
        socketio.sleep(_TLE_REFRESH_INTERVAL)
        try:
            _init_orbital(reset_t0=False)
            with _state_lock:
                epoch, period = _state["tle_epoch_unix"], _state["period"]
            log.info("TLE refreshed (epoch_unix=%.0f, period=%.1f min)",
                     epoch, period / 60)
        except Exception:
            log.exception("TLE refresh failed; keeping previous elements")


# Head-start: show 1 orbit of history on first load, then grow in real time.
HEAD_START_ORBITS = 1.0
SPEED_FACTOR = 1.0       # 1.0 = real-time (1 orbital period → 1 new orbit drawn)
TRACK_WINDOW_ORBITS = 1.1

# Per-panel sliding window in minutes (None = show all data).
# Tune these once you know what looks right for each panel.
PANEL_WINDOWS = {
    "panel_altitude":      240,    # last 4 hours
    "panel_signal":        2880,   # onboard RSSI — last 2 days, like the other telemetry
    "panel_temperature":   2880,   # gyroscope — last 2 days
    "panel_power":         2880,   # last 2 days
    "panel_magnetometer":  2880,   # last 2 days
}

HARVARD_LAT = 42.3736
HARVARD_LON = -71.1097
HARVARD_GS_ALT_M = (7 * 10 + 15) * 0.3048
HARVARD_LOOKAHEAD_ORBITS = 2.0
HARVARD_POINTS_PER_ORBIT = 1200

# Ground-station horizon mask: local obstructions (buildings/trees/terrain)
# block the antenna below this elevation, so the satellite is only actually
# acquirable above it. This is the single source of truth for the pass/closest-
# approach visibility threshold; the map coverage circle and the approach polar
# plot consume it via /api/status and /api/harvard_approach.
GS_MIN_ELEVATION_DEG = 30.0


def _current_n_orbits():
    """Return restart-relative orbit count (kept for panel/history pacing)."""
    with _state_lock:
        t0 = _state["t0"]
        period = _state["period"]
        max_orbits = _state["n_orbits"]
    elapsed = time.time() - t0
    return min(HEAD_START_ORBITS + (elapsed * SPEED_FACTOR) / period if period > 0 else HEAD_START_ORBITS, max_orbits)


def _current_phase_orbit():
    """Return absolute orbit phase anchored to TLE epoch.

    With SGP4 active the mean anomaly offset is omitted — SGP4 handles
    M0 internally, so start_orbit = elapsed_time / period maps directly
    to the correct absolute UTC time in the propagator.
    """
    with _state_lock:
        period = _state["period"]
        tle_epoch_unix = _state["tle_epoch_unix"]
        mean_anomaly_deg = _state["mean_anomaly_deg"]

    if period <= 0:
        return 0.0

    phase_orbits = (time.time() - tle_epoch_unix) / period
    if not visualizer.has_sgp4():
        phase_orbits += mean_anomaly_deg / 360.0
    return max(float(phase_orbits), 0.0)


def _central_angle_deg(lat_deg: np.ndarray, lon_deg: np.ndarray,
                       ref_lat_deg: float, ref_lon_deg: float) -> np.ndarray:
    """Great-circle angular separation in degrees to a reference point."""
    lat = np.radians(lat_deg)
    lon = np.radians(lon_deg)
    ref_lat = np.radians(ref_lat_deg)
    ref_lon = np.radians(ref_lon_deg)
    cos_c = (
        np.sin(ref_lat) * np.sin(lat)
        + np.cos(ref_lat) * np.cos(lat) * np.cos(lon - ref_lon)
    )
    cos_c = np.clip(cos_c, -1.0, 1.0)
    return np.degrees(np.arccos(cos_c))


_WGS84_E2 = 0.00669437999014  # first eccentricity squared

def _geodetic_to_geocentric_lat(lat_rad):
    """Convert geodetic latitude to geocentric (spherical) latitude."""
    return np.arctan((1 - _WGS84_E2) * np.tan(lat_rad))


def _observer_altaz(obs_lat_deg: float, obs_lon_deg: float,
                    sat_lat_deg: float, sat_lon_deg: float,
                    sat_alt_km: float, obs_alt_m: float = 0.0):
    """Convert satellite geodetic position to observer azimuth/elevation/range."""
    obs_lat = _geodetic_to_geocentric_lat(np.radians(obs_lat_deg))
    obs_lon = np.radians(obs_lon_deg)
    sat_lat = np.radians(sat_lat_deg)
    sat_lon = np.radians(sat_lon_deg)

    obs_r = visualizer.R_EARTH + obs_alt_m
    sat_r = visualizer.R_EARTH + sat_alt_km * 1000.0

    obs_x = obs_r * np.cos(obs_lat) * np.cos(obs_lon)
    obs_y = obs_r * np.cos(obs_lat) * np.sin(obs_lon)
    obs_z = obs_r * np.sin(obs_lat)

    sat_x = sat_r * np.cos(sat_lat) * np.cos(sat_lon)
    sat_y = sat_r * np.cos(sat_lat) * np.sin(sat_lon)
    sat_z = sat_r * np.sin(sat_lat)

    dx = sat_x - obs_x
    dy = sat_y - obs_y
    dz = sat_z - obs_z

    east = -np.sin(obs_lon) * dx + np.cos(obs_lon) * dy
    north = (
        -np.sin(obs_lat) * np.cos(obs_lon) * dx
        - np.sin(obs_lat) * np.sin(obs_lon) * dy
        + np.cos(obs_lat) * dz
    )
    up = (
        np.cos(obs_lat) * np.cos(obs_lon) * dx
        + np.cos(obs_lat) * np.sin(obs_lon) * dy
        + np.sin(obs_lat) * dz
    )

    horizontal = np.hypot(east, north)
    az_deg = (np.degrees(np.arctan2(east, north)) + 360.0) % 360.0
    el_deg = np.degrees(np.arctan2(up, horizontal))
    slant_range_km = np.sqrt(dx * dx + dy * dy + dz * dz) / 1000.0

    return float(az_deg), float(el_deg), float(slant_range_km)


def _observer_altaz_many(obs_lat_deg: float, obs_lon_deg: float,
                         sat_lat_deg: np.ndarray, sat_lon_deg: np.ndarray,
                         sat_alt_km: np.ndarray, obs_alt_m: float = 0.0):
    """Vectorized observer azimuth/elevation/slant-range for many satellite points."""
    obs_lat = _geodetic_to_geocentric_lat(np.radians(obs_lat_deg))
    obs_lon = np.radians(obs_lon_deg)
    sat_lat = np.radians(sat_lat_deg)
    sat_lon = np.radians(sat_lon_deg)

    obs_r = visualizer.R_EARTH + obs_alt_m
    sat_r = visualizer.R_EARTH + sat_alt_km * 1000.0

    obs_x = obs_r * np.cos(obs_lat) * np.cos(obs_lon)
    obs_y = obs_r * np.cos(obs_lat) * np.sin(obs_lon)
    obs_z = obs_r * np.sin(obs_lat)

    sat_x = sat_r * np.cos(sat_lat) * np.cos(sat_lon)
    sat_y = sat_r * np.cos(sat_lat) * np.sin(sat_lon)
    sat_z = sat_r * np.sin(sat_lat)

    dx = sat_x - obs_x
    dy = sat_y - obs_y
    dz = sat_z - obs_z

    east = -np.sin(obs_lon) * dx + np.cos(obs_lon) * dy
    north = (
        -np.sin(obs_lat) * np.cos(obs_lon) * dx
        - np.sin(obs_lat) * np.sin(obs_lon) * dy
        + np.cos(obs_lat) * dz
    )
    up = (
        np.cos(obs_lat) * np.cos(obs_lon) * dx
        + np.cos(obs_lat) * np.sin(obs_lon) * dy
        + np.sin(obs_lat) * dz
    )

    horizontal = np.hypot(east, north)
    az_deg = (np.degrees(np.arctan2(east, north)) + 360.0) % 360.0
    el_deg = np.degrees(np.arctan2(up, horizontal))
    slant_range_km = np.sqrt(dx * dx + dy * dy + dz * dz) / 1000.0

    return az_deg, el_deg, slant_range_km


# ──────────────────────────────────────────────
# Slack notifications
# ──────────────────────────────────────────────

def _notify_slack(message: str, level: str = "info"):
    """Post to Slack webhook. No-op when CUBESAT_SLACK_WEBHOOK is unset."""
    webhook = os.environ.get("CUBESAT_SLACK_WEBHOOK", "")
    if not webhook:
        return
    payload = json.dumps({
        "text": f"*HUCSAT Mission Control* — {message}",
        "username": "MissionStation",
    })
    try:
        req = Request(webhook, data=payload.encode(),
                      headers={"Content-Type": "application/json"})
        urlopen(req, timeout=10)
    except Exception as exc:
        log.debug("Slack notify failed: %s", exc)


# ──────────────────────────────────────────────
# Pass watcher (upcoming pass alerts + pass summaries)
# ──────────────────────────────────────────────

PASS_WARN_MINUTES = 10
_PASS_WATCHER_INTERVAL = 60   # seconds between checks

_alerted_pass_times: set = set()   # AOS unix timestamps (rounded) already alerted
_in_pass = [False]
_pass_start_utc = [None]           # ISO string when current pass began


def _fmt_eta(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {sec:02d}s"


def _check_pass_events():
    n_now = _current_phase_orbit()
    passes, az_arr, el_arr, slant_range_arr, sep_deg, lat, lon, alt_km, t_sec = \
        _find_visible_passes(n_now, lookahead_orbits=3.0)

    now_unix = time.time()

    # ── #1: Upcoming pass warning ────────────────────────────────
    for p in passes:
        if p["aos_eta_sec"] <= 0:
            continue
        if p["aos_eta_sec"] > PASS_WARN_MINUTES * 60:
            break
        aos_key = round(now_unix + p["aos_eta_sec"], -1)  # round to 10s for dedup
        if aos_key not in _alerted_pass_times:
            _alerted_pass_times.add(aos_key)
            aos_dt = datetime.fromtimestamp(now_unix + p["aos_eta_sec"], tz=timezone.utc)
            cpa_dt = datetime.fromtimestamp(now_unix + p["cpa_eta_sec"], tz=timezone.utc)
            los_dt = datetime.fromtimestamp(now_unix + p["los_eta_sec"], tz=timezone.utc)
            duration_min = (p["los_eta_sec"] - p["aos_eta_sec"]) / 60
            _notify_slack(
                f"Pass in {_fmt_eta(p['aos_eta_sec'])}\n"
                f"AOS {aos_dt.strftime('%H:%M:%S')} UTC  |  "
                f"CPA {cpa_dt.strftime('%H:%M:%S')} UTC  |  "
                f"LOS {los_dt.strftime('%H:%M:%S')} UTC\n"
                f"Max El: {p['max_el_deg']:.1f}°  |  "
                f"Min Range: {p['min_range_km']:.0f} km  |  "
                f"Duration: {duration_min:.1f} min",
                level="info",
            )
            break  # only alert on the soonest upcoming pass

    # Prune stale alert keys
    _alerted_pass_times -= {t for t in _alerted_pass_times if t < now_unix - 7200}

    # ── #3: Pass summary on LOS ──────────────────────────────────
    currently_in_pass = bool(
        passes and passes[0]["aos_eta_sec"] <= 0 < passes[0]["los_eta_sec"]
    )

    if currently_in_pass and not _in_pass[0]:
        _in_pass[0] = True
        _pass_start_utc[0] = datetime.fromtimestamp(now_unix, tz=timezone.utc).isoformat()

    elif not currently_in_pass and _in_pass[0]:
        _in_pass[0] = False
        if _pass_start_utc[0]:
            pass_end_utc = datetime.fromtimestamp(now_unix, tz=timezone.utc).isoformat()
            pkts = packet_store.packets_in_window(_pass_start_utc[0], pass_end_utc)
            n_pkts = len(pkts)
            rssi_vals = [p["rssi"] for p in pkts if p.get("rssi") is not None]
            avg_rssi = sum(rssi_vals) / len(rssi_vals) if rssi_vals else None
            duration_min = (
                now_unix
                - datetime.fromisoformat(_pass_start_utc[0]).timestamp()
            ) / 60
            rssi_str = f"Avg RSSI: {avg_rssi:.1f} dBm" if avg_rssi is not None else "No RSSI data"
            _notify_slack(
                f"Pass complete\n"
                f"Duration: {duration_min:.1f} min  |  "
                f"Packets received: {n_pkts}  |  {rssi_str}",
                level="info",
            )
            _pass_start_utc[0] = None


def _pass_watcher():
    """Background task: upcoming pass alerts and pass-end summaries."""
    while True:
        socketio.sleep(_PASS_WATCHER_INTERVAL)
        try:
            _check_pass_events()
        except Exception:
            log.exception("Pass watcher error")


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", live=_state["live"],
                           cache_bust=int(time.time()),
                           carto_key=CARTO_BASEMAPS_KEY)


def _split_at_dateline(lon, lat):
    """Split lon/lat arrays into [lat, lon] segments at ±180° wrap-arounds."""
    segments = []
    cur = []
    for i in range(len(lon)):
        if i > 0 and abs(lon[i] - lon[i - 1]) > 180:
            if len(cur) >= 2:
                segments.append(cur)
            cur = []
        cur.append([round(float(lat[i]), 4), round(float(lon[i]), 4)])
    if len(cur) >= 2:
        segments.append(cur)
    return segments


FUTURE_TRACK_ORBITS = 1.0


@app.route("/api/track")
def api_track():
    """Return ground-track polyline as JSON arrays of [lat, lon] pairs."""
    n_total = _current_phase_orbit()
    n_draw = max(TRACK_WINDOW_ORBITS, 0.1)
    start_orbit = max(n_total - n_draw, 0.0)
    n_points = int(request.args.get("n_points", max(int(n_draw * 500), 200)))

    with _state_lock:
        sma, ecc, inc = _state["sma"], _state["ecc"], _state["inc"]
        raan, argp = _state["raan"], _state["argp"]

    lon, lat, t_sec = visualizer.ground_track(
        sma, ecc, inc, raan, argp,
        n_orbits=n_draw,
        n_points=n_points,
        start_orbit=start_orbit,
    )

    segments = _split_at_dateline(lon, lat)

    current = [round(float(lat[-1]), 4), round(float(lon[-1]), 4)]
    start = [round(float(lat[0]), 4), round(float(lon[0]), 4)]

    future_lon, future_lat, _ = visualizer.ground_track(
        sma, ecc, inc, raan, argp,
        n_orbits=FUTURE_TRACK_ORBITS,
        n_points=max(int(FUTURE_TRACK_ORBITS * 500), 300),
        start_orbit=n_total,
    )
    future_segments = _split_at_dateline(future_lon, future_lat)

    return jsonify(segments=segments, current=current, start=start,
                   future_segments=future_segments)


def _x_label(mod):
    """Every time-series panel (telemetry and the live altitude) is sent in
    hours of mission elapsed time."""
    return "MET (h)"


def _build_multi_panel(mod, window_min):
    """Serialize a multi-series panel (shared x, N y-series) with the same
    windowing + downsampling used for single-series panels. Read-only."""
    d = mod.compute_series()
    x = list(d.get("x", []))
    series = d.get("series", [])
    if window_min is not None and x:
        cutoff = x[-1] - window_min
        keep = [i for i, xv in enumerate(x) if xv >= cutoff]
        x = [x[i] for i in keep]
        series = [{"label": s["label"], "color": s["color"],
                   "y": [s["y"][i] for i in keep]} for s in series]
    step = max(1, len(x) // 300) if x else 1
    return {
        "title": mod.TITLE,
        "ylabel": mod.Y_LABEL,
        "xlabel": _x_label(mod),
        "multi": True,
        "x": [round(float(v) / 60.0, 4) for v in x[::step]],   # minutes -> hours
        "series": [{"label": s["label"], "color": s["color"],
                    "y": [round(float(v), 3) for v in s["y"][::step]]}
                   for s in series],
    }


@app.route("/api/panels")
def api_panels():
    """Return data for all side panels (single-series + the gyroscope quad)."""
    n = _current_n_orbits()

    with _state_lock:
        orbital = _state["orbital"]

    panels = []
    for mod in [panel_altitude, panel_signal, panel_magnetometer, panel_temperature, panel_power]:
        window_min = PANEL_WINDOWS.get(mod.__name__)

        # Multi-series panels (gyroscope quad) return {x, series:[{label,color,y}]}.
        if getattr(mod, "MULTI_SERIES", False):
            panels.append(_build_multi_panel(mod, window_min))
            continue

        if getattr(mod, "SOURCE", "orbital") == "telemetry":
            x, y = mod.compute()
        else:
            x, y = mod.compute(orbital, n)

        # Sliding window: keep only the last N minutes of data
        if window_min is not None and len(x) > 0:
            cutoff = x[-1] - window_min
            mask = x >= cutoff
            x, y = x[mask], y[mask]

        if len(x) == 0:
            panels.append({
                "title": mod.TITLE,
                "color": mod.COLOR,
                "ylabel": mod.Y_LABEL,
                "xlabel": _x_label(mod),
                "x": [],
                "y": [],
            })
        else:
            # Downsample for the browser if too many points
            step = max(1, len(x) // 300)
            panels.append({
                "title": mod.TITLE,
                "color": mod.COLOR,
                "ylabel": mod.Y_LABEL,
                "xlabel": _x_label(mod),
                "x": [round(float(v) / 60.0, 4) for v in x[::step]],   # minutes -> hours
                "y": [round(float(v), 3) for v in y[::step]],
            })

    return jsonify(panels=panels)


# HUCSat firmware quirk: the beacon "uptime" field is anchored to the OBC
# clock's epoch instead of boot, so it arrives as ~9.47e8 s. Subtracting the
# boot anchor recovers real seconds since boot (calibrated 2026-07-08 against
# a known-good uptime of 518144 s). Plausible values pass through untouched
# in case a firmware update fixes this upstream.
HUCSAT_UPTIME_BOOT_ANCHOR = 946689024
_UPTIME_PLAUSIBLE_MAX = 315_360_000  # 10 years


def _fix_uptime(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return value
    if v > _UPTIME_PLAUSIBLE_MAX:
        v -= HUCSAT_UPTIME_BOOT_ANCHOR
    return int(v) if v >= 0 else value


@app.route("/api/status")
def api_status():
    """Return HUD info: orbital params, MET, packet count."""
    with _state_lock:
        alt_km = _state["alt_km"]
        inc = _state["inc"]
        ecc = _state["ecc"]
        period = _state["period"]
        sma = _state["sma"]

    # Mission Elapsed Time — anchored to MISSION_EPOCH_UTC, null until deployed.
    met_elapsed = None
    orbits_since_deploy = None
    if MISSION_EPOCH_UTC is not None:
        epoch = datetime.fromisoformat(MISSION_EPOCH_UTC)
        if epoch.tzinfo is None:
            epoch = epoch.replace(tzinfo=timezone.utc)
        met_secs = (datetime.now(timezone.utc) - epoch).total_seconds()
        if met_secs >= 0:
            met_elapsed = round(met_secs)
            if period > 0:
                orbits_since_deploy = round(met_secs / period, 3)

    try:
        n_pkts = packet_store.packet_count()
    except Exception:
        n_pkts = 0

    # Most recent packet timestamp (ISO-8601 UTC), for the status bar.
    try:
        recent = packet_store.recent_packets(n=1)
        last_pkt_at = recent[0]["received_at"] if recent else None
    except Exception:
        last_pkt_at = None

    # Latest FSM state for HUD
    fsm = packet_store.latest_fsm_state()
    fsm_state = fsm["fsm_state"] if fsm else "—"
    fsm_depl = fsm["fsm_depl"] if fsm else "—"
    fsm_uptime = _fix_uptime(fsm["uptime"]) if fsm else "—"

    # Average orbital speed: v = 2*pi*a / T (equals sqrt(mu/a) since the period
    # is Kepler-derived from a). ~7.7 km/s in LEO. Reported in km/s and mph.
    avg_v_ms = (2 * np.pi * sma / period) if period > 0 else 0.0

    return jsonify(
        alt_km=round(alt_km, 1),
        inc=round(inc, 2),
        ecc=round(ecc, 6),
        period_min=round(period / 60, 1),
        velocity_kms=round(avg_v_ms / 1000, 2),
        velocity_mph=round(avg_v_ms * 2.2369362920544),
        gs_min_elevation_deg=GS_MIN_ELEVATION_DEG,
        met_elapsed=met_elapsed,
        orbits_since_deploy=orbits_since_deploy,
        n_pkts=n_pkts,
        last_pkt_at=last_pkt_at,
        live=_state["live"],
        fsm_state=fsm_state,
        fsm_depl=fsm_depl,
        fsm_uptime=fsm_uptime,
    )


# ──────────────────────────────────────────────
# FSM state timeline endpoint
# ──────────────────────────────────────────────

@app.route("/api/fsm")
def api_fsm():
    """Return FSM state history for the timeline panel."""
    history = packet_store.fsm_state_history(n=200)
    for entry in history:
        entry["uptime"] = _fix_uptime(entry.get("uptime"))
    return jsonify(history=history)


@app.route("/api/best_dir")
def api_best_dir():
    """Return best-direction distribution for the doughnut chart."""
    return jsonify(panel_best_dir.compute())


NEXT_APPROACHES_LOOKAHEAD_ORBITS = 10.0


def _find_visible_passes(n_now, lookahead_orbits, n_points=None):
    """Find all visible passes in a lookahead window.

    Returns list of dicts, each with aos/cpa/los timing, min range,
    max elevation, and (for the first pass) the az/el path array.
    """
    if n_points is None:
        n_points = int(lookahead_orbits * HARVARD_POINTS_PER_ORBIT)

    with _state_lock:
        sma, ecc, inc = _state["sma"], _state["ecc"], _state["inc"]
        raan, argp = _state["raan"], _state["argp"]

    lon, lat, alt_km, t_sec = visualizer.ground_track_with_alt(
        sma, ecc, inc, raan, argp,
        n_orbits=lookahead_orbits,
        n_points=n_points,
        start_orbit=n_now,
    )

    separation_deg = _central_angle_deg(lat, lon, HARVARD_LAT, HARVARD_LON)
    az_arr, el_arr, slant_range_arr = _observer_altaz_many(
        HARVARD_LAT, HARVARD_LON, lat, lon, alt_km,
        obs_alt_m=HARVARD_GS_ALT_M,
    )

    # A pass only counts once the satellite clears our obstruction mask — below
    # GS_MIN_ELEVATION_DEG the antenna's view is blocked, so AOS/LOS are the
    # mask-crossing times and passes that never reach it are dropped entirely.
    vis_idx = np.where(el_arr >= GS_MIN_ELEVATION_DEG)[0]
    passes = []

    if len(vis_idx) == 0:
        return passes, az_arr, el_arr, slant_range_arr, separation_deg, lat, lon, alt_km, t_sec

    cuts = np.where(np.diff(vis_idx) > 1)[0]
    seg_starts = np.concatenate(([vis_idx[0]], vis_idx[cuts + 1]))
    seg_ends = np.concatenate((vis_idx[cuts], [vis_idx[-1]]))

    for s, e in zip(seg_starts, seg_ends):
        s_i, e_i = int(s), int(e)
        seg = np.arange(s_i, e_i + 1)
        cpa_local = int(np.argmin(slant_range_arr[seg]))
        cpa_idx = int(seg[cpa_local])
        max_el_local = int(np.argmax(el_arr[seg]))
        max_el_idx = int(seg[max_el_local])

        passes.append({
            "aos_eta_sec": round(float(t_sec[s_i] - t_sec[0]), 1),
            "cpa_eta_sec": round(float(t_sec[cpa_idx] - t_sec[0]), 1),
            "los_eta_sec": round(float(t_sec[e_i] - t_sec[0]), 1),
            "min_range_km": round(float(slant_range_arr[cpa_idx]), 1),
            "max_el_deg": round(float(el_arr[max_el_idx]), 1),
            "cpa_idx": cpa_idx,
            "seg_start": s_i,
            "seg_end": e_i,
        })

    return passes, az_arr, el_arr, slant_range_arr, separation_deg, lat, lon, alt_km, t_sec


@app.route("/api/harvard_approach")
def api_harvard_approach():
    """Return alt/az pass-arc around the next closest slant-range approach."""
    n_now = _current_phase_orbit()
    lookahead_orbits = max(float(request.args.get("n_orbits", HARVARD_LOOKAHEAD_ORBITS)), 0.25)
    n_points = int(request.args.get(
        "n_points",
        max(int(lookahead_orbits * HARVARD_POINTS_PER_ORBIT), 600),
    ))

    passes, az_arr, el_arr, slant_range_arr, separation_deg, lat, lon, alt_km, t_sec = \
        _find_visible_passes(n_now, lookahead_orbits, n_points)

    path = []
    pass_start_eta_sec = None
    pass_end_eta_sec = None

    if passes:
        best_pass = min(passes, key=lambda p: p["min_range_km"])
        idx = best_pass["cpa_idx"]
        best_seg_start = best_pass["seg_start"]
        best_seg_end = best_pass["seg_end"]

        seg = np.arange(best_seg_start, best_seg_end + 1)
        step = max(1, len(seg) // 260)
        seg_ds = seg[::step]
        if seg_ds[-1] != seg[-1]:
            seg_ds = np.append(seg_ds, seg[-1])

        path = [
            {
                "az": round(float(az_arr[i]), 2),
                "el": round(float(el_arr[i]), 2),
                "eta_sec": round(float(t_sec[i] - t_sec[0]), 1),
            }
            for i in seg_ds
        ]

        pass_start_eta_sec = max(float(t_sec[best_seg_start] - t_sec[0]), 0.0)
        pass_end_eta_sec = max(float(t_sec[best_seg_end] - t_sec[0]), 0.0)
        visible = True
    else:
        idx = int(np.argmin(slant_range_arr))
        visible = False

    az_deg = float(az_arr[idx])
    el_deg = float(el_arr[idx])
    slant_range_km = float(slant_range_arr[idx])

    eta_sec = max(float(t_sec[idx] - t_sec[0]), 0.0)

    return jsonify(
        observer={"lat": HARVARD_LAT, "lon": HARVARD_LON},
        observer_alt_m=round(HARVARD_GS_ALT_M, 2),
        gs_min_elevation_deg=GS_MIN_ELEVATION_DEG,
        az_deg=round(az_deg, 2),
        el_deg=round(el_deg, 2),
        eta_sec=round(eta_sec, 1),
        slant_range_km=round(slant_range_km, 1),
        separation_deg=round(float(separation_deg[idx]), 2),
        approach_lat=round(float(lat[idx]), 4),
        approach_lon=round(float(lon[idx]), 4),
        approach_alt_km=round(float(alt_km[idx]), 1),
        visible=visible,
        pass_start_eta_sec=round(pass_start_eta_sec, 1) if pass_start_eta_sec is not None else None,
        pass_end_eta_sec=round(pass_end_eta_sec, 1) if pass_end_eta_sec is not None else None,
        path=path,
    )


@app.route("/api/next_approaches")
def api_next_approaches():
    """Return the current and next 3 visible passes over Harvard."""
    n_now = _current_phase_orbit()
    lookahead = float(request.args.get("n_orbits", NEXT_APPROACHES_LOOKAHEAD_ORBITS))

    passes, az_arr, el_arr, slant_range_arr, separation_deg, lat, lon, alt_km, t_sec = \
        _find_visible_passes(n_now, lookahead)

    result_passes = []
    for p in passes[:4]:
        result_passes.append({
            "aos_eta_sec": p["aos_eta_sec"],
            "cpa_eta_sec": p["cpa_eta_sec"],
            "los_eta_sec": p["los_eta_sec"],
            "min_range_km": p["min_range_km"],
            "max_el_deg": p["max_el_deg"],
        })

    current = result_passes[0] if result_passes else None
    upcoming = result_passes[1:4] if len(result_passes) > 1 else []

    return jsonify(current_pass=current, upcoming=upcoming)


# ──────────────────────────────────────────────
# Test-only endpoint: simulate a packet write (for stress testing)
# ──────────────────────────────────────────────

import random as _random

@app.route("/api/test/write", methods=["POST"])
def api_test_write():
    """Insert a fake packet + telemetry rows (stress test only)."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify(ok=False, error="forbidden"), 403
    _fsm_states = ["nominal", "safe", "detumble", "deploy", "standby"]
    pkt_id = packet_store.store_packet(
        satellite="STRESS-TEST",
        norad_id=99999,
        station="stress-client",
        frequency_mhz=437.5,
        rssi=round(_random.uniform(-120, -80), 1),
        snr=round(_random.uniform(0, 15), 1),
        decoded={
            "FSM_state": _random.choice(_fsm_states),
            "FSM_depl": _random.choice([True, False]),
            "FSM_batt_v": round(_random.uniform(3.0, 4.2), 2),
            "uptime": _random.randint(100, 100000),
        },
        source="stress_test",
    )
    packet_store.store_telemetry_batch(pkt_id, [
        ("FSM_batt_v",   round(_random.uniform(3.0, 4.2), 2),   "V"),
        ("FSM_magn_v_0", round(_random.uniform(-50, 50), 2),     "µT"),
        ("FSM_av_0",     round(_random.uniform(-10, 10), 3),     "°/s"),
        ("FSM_best_dir", _random.choices([0, 1, 2, 3, -1], weights=[4, 2, 3, 2, 1])[0], ""),
    ])
    return jsonify(ok=True, packet_id=pkt_id)


@app.route("/api/test/cleanup", methods=["POST"])
def api_test_cleanup():
    """Remove all rows inserted by stress tests."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify(ok=False, error="forbidden"), 403
    try:
        from packet_store import _get_conn
        conn = _get_conn()
        conn.execute("DELETE FROM telemetry WHERE packet_id IN "
                     "(SELECT id FROM packets WHERE source IN ('stress_test', 'stress_bench'))")
        conn.execute("DELETE FROM packets WHERE source IN ('stress_test', 'stress_bench')")
        conn.commit()
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# ──────────────────────────────────────────────
# SocketIO: push new packets to browser in real time
# ──────────────────────────────────────────────

_last_pkt_id = [0]


def _check_new_packets():
    """Periodically check for new packets and push to connected clients."""
    while True:
        socketio.sleep(5)
        try:
            pkts = packet_store.recent_packets(n=5)
            if pkts and pkts[0]["id"] > _last_pkt_id[0]:
                _last_pkt_id[0] = pkts[0]["id"]
                socketio.emit("new_packets", {
                    "count": packet_store.packet_count(),
                    "latest": pkts[0]["received_at"],
                })
        except Exception:
            pass


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Mission Control Web Dashboard")
    parser.add_argument("--live", action="store_true",
                        help="Enable live packet capture (TinyGS v3 API poller)")
    parser.add_argument("--mqtt", action="store_true",
                        help="Also start the MQTT listener (only useful if you operate "
                             "your OWN TinyGS ground station; not needed for network-wide capture)")
    parser.add_argument("--poll-interval", type=int, default=300,
                        help="Seconds between TinyGS v3 API polls when --live (default 300)")
    parser.add_argument("--no-tinygs-poller", action="store_true",
                        help="With --live, don't poll the TinyGS API; packets come only "
                             "from the TinyGS webhook via AWS (aws_sync.py)")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--n-orbits", type=float, default=3.0,
                        help="Default number of orbits to display")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind address (0.0.0.0 for network access)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(name)-20s  %(message)s")

    _state["live"] = args.live
    _state["n_orbits"] = args.n_orbits

    # Pre-flight: claim the listen port BEFORE any network activity. If another
    # instance already holds it (e.g. a forgotten screen session), systemd
    # crash-loops us every RestartSec — and each attempt used to hit CelesTrak
    # and fire a TinyGS poll before dying at the bind. Thousands of aborted
    # requests/day from one IP reads as bot abuse (it got the Pi tarpitted);
    # failing fast here keeps a restart loop completely network-silent.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((args.host, args.port))
    except OSError as exc:
        log.error("Port %d is already in use (%s) — is another web_server "
                  "(screen session?) still running? Exiting before touching "
                  "the network.", args.port, exc)
        raise SystemExit(2)
    finally:
        probe.close()

    log.info("Fetching orbital elements from CelesTrak...")
    _init_orbital()
    log.info("Orbital data ready  (period=%.1f min, alt=%.0f km)",
             _state["period"] / 60, _state["alt_km"])

    socketio.start_background_task(_pass_watcher)
    socketio.start_background_task(_tle_refresher)
    log.info("TLE auto-refresh every %.0f min", _TLE_REFRESH_INTERVAL / 60)

    if args.live:
        # Push new DB packets (from aws_sync and/or the poller) to the browser.
        socketio.start_background_task(_check_new_packets)
        if args.no_tinygs_poller:
            log.info("Live capture: TinyGS API poller disabled; packets arrive via aws_sync")
        else:
            # Network-wide capture: poll the TinyGS v3 API (headless, signed request).
            # This replaces the MQTT listener, which only ever delivers packets from
            # your OWN ground stations (we run none) — see tinygs_poller.py.
            import tinygs_poller
            tinygs_poller.start_poller(interval=args.poll_interval)
            log.info("Live capture: TinyGS v3 poller started (every %ds)", args.poll_interval)
        if args.mqtt:
            import tinygs_mqtt
            tinygs_mqtt.start_listener()
            log.info("MQTT listener also started (own-station packets)")

    log.info("Dashboard at  http://localhost:%d", args.port)
    # allow_unsafe_werkzeug: Flask-SocketIO blocks its dev Werkzeug server when
    # there's no interactive TTY (e.g. under systemd). Fine here — this is a
    # localhost/LAN mission dashboard, the same server we've always run.
    socketio.run(app, host=args.host, port=args.port,
                 debug=False, use_reloader=False, log_output=False,
                 allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
