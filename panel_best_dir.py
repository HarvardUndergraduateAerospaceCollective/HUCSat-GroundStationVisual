"""
Panel: Best Direction
Displays which CubeSat face has the best sun exposure (orient_best_direction).
Data source: decoded packet JSON, keys "FSM_best_dir" and "FSM_pan_light".

Unlike other panels, this returns *distribution* data (counts per face)
rather than a time series, since the value is a discrete direction index.
Only real faces are charted; readings with no direction (-1, e.g. every
detumble-state beacon) are left out of the chart but counted, so the panel
can show what share of packets carry a real direction.

Saturated packets get their own slice. In daylight the flown light sensors
overflow, and the firmware then reports all four faces as 0, so the light data
in those packets is meaningless. A packet counts as saturated when its four
face readings are all 0 while the satellite is in sunlight (SGP4 + Sun
geometry). All-zero readings during a real eclipse stay under their face.

Direction mapping (from OBC state_orient.py):
    0 → +Y    1 → −X    2 → −Y    3 → +X    −1 → N/A
"""

import json
import threading
from datetime import datetime

import packet_store
import visualizer

TITLE         = "BEST DIRECTION"
COLOR         = "#ffb000"
SOURCE        = "telemetry"
TELEMETRY_KEY = "FSM_best_dir"

# Label for each face index (matches OBC orient_best_direction mapping)
FACE_LABELS = {
    0: "+Y",
    1: "−X",   # −X  (proper minus sign)
    2: "−Y",   # −Y
    3: "+X",
    -1: "N/A",
}

FACE_COLORS = {
    0: "#33ff00",   # +Y  — green
    1: "#ffb000",   # −X  — amber
    2: "#ff6600",   # −Y  — orange
    3: "#00ff88",   # +X  — teal
    -1: "#1a1a1a",  # N/A — dim
}

SAT_LABEL = "SAT"
SAT_COLOR = "#ff3366"   # saturated: daylight sensor overflow, light data meaningless

# received_at -> sunlit. Each packet is classified once, with the TLE that was
# current when it arrived (the closest TLE it will ever get).
_sunlit_cache: dict[str, bool] = {}

# Packets read so far, so each refresh only reads new rows. Packets are never
# edited in place by the live services; a manual re-decode needs a restart.
_packets: list[dict] = []    # {received_at, face, all_zero}
_seen_id = 0
_lock = threading.Lock()


def _all_zero(pan_light) -> bool:
    """True when the four face readings are present and all exactly 0."""
    if isinstance(pan_light, str):
        try:
            pan_light = json.loads(pan_light)
        except ValueError:
            return False
    return isinstance(pan_light, list) and len(pan_light) > 0 and all(v == 0 for v in pan_light)


def _sunlit(times: list[str]) -> dict[str, bool]:
    """Sunlit flag per received_at; empty when no TLE is loaded yet."""
    todo = []
    for t in times:
        if t not in _sunlit_cache:
            try:
                todo.append((t, datetime.fromisoformat(t).timestamp()))
            except ValueError:
                _sunlit_cache[t] = False
    if todo:
        flags = visualizer.in_sunlight([u for _, u in todo])
        if flags is None:
            return {}
        _sunlit_cache.update((t, bool(f)) for (t, _), f in zip(todo, flags))
    return {t: _sunlit_cache[t] for t in times}


def compute():
    """Return distribution dict for the doughnut chart.

    Returns dict with keys:
        labels  – face direction labels  ["+Y", "−X", "−Y", "+X", "SAT"]
        counts  – number of readings per slice
        colors  – per-slice color
        latest  – most recent real direction index (int, -1 if none yet)
        latest_label – human-readable label for latest
        with_direction – readings with a real direction (not saturated)
        saturated      – readings with a face whose light data is saturated
        total          – all best-direction readings (incl. no direction)
        pct_with_direction – with_direction / total, as a percentage
    """
    global _seen_id
    faces = [0, 1, 2, 3]
    with _lock:
        for r in packet_store.decoded_fields(TELEMETRY_KEY, "FSM_pan_light", after_id=_seen_id):
            try:
                face = int(round(float(r[TELEMETRY_KEY])))
            except (TypeError, ValueError):
                face = -1
            _packets.append({"received_at": r["received_at"], "face": face,
                             "all_zero": _all_zero(r["FSM_pan_light"])})
            _seen_id = max(_seen_id, r["id"])
        packets = list(_packets)

    candidates = [p for p in packets if p["face"] in faces and p["all_zero"]]
    sunlit = _sunlit([p["received_at"] for p in candidates])

    counts = {k: 0 for k in faces}
    saturated = no_direction = 0
    latest, latest_at = -1, ""
    for p in packets:
        if p["face"] not in counts:
            no_direction += 1
        elif p["all_zero"] and sunlit.get(p["received_at"], False):
            saturated += 1
        else:
            counts[p["face"]] += 1
            if p["received_at"] >= latest_at:
                latest, latest_at = p["face"], p["received_at"]

    with_direction = sum(counts.values())
    total = with_direction + saturated + no_direction
    return {
        "labels":       [FACE_LABELS[k] for k in faces] + [SAT_LABEL],
        "counts":       [counts[k] for k in faces] + [saturated],
        "colors":       [FACE_COLORS[k] for k in faces] + [SAT_COLOR],
        "latest":       latest,
        "latest_label": FACE_LABELS[latest] if latest in counts else "-",
        "with_direction": with_direction,
        "saturated":      saturated,
        "total":          total,
        "pct_with_direction": round(100 * with_direction / total, 1) if total else 0.0,
    }
