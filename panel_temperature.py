"""
Panel: Gyroscope (Angular Velocity)  [file name is legacy — this IS the gyro panel]

Renders four small charts: body-axis angular rates X / Y / Z plus the total
magnitude |w| = sqrt(x^2 + y^2 + z^2), all in deg/s over time.

Data source: packet_store telemetry table, keys FSM_av_0 / FSM_av_1 / FSM_av_2
(already normalized rad/s -> deg/s by beacon_decoder). READ-ONLY — never writes.

``compute()`` (single X-axis series) is kept for backward compatibility with
missioncontrol.py / stress_bench.py. The dashboard uses ``compute_series()``.
"""

import numpy as np

import mission_time
import packet_store

TITLE          = "GYROSCOPE"
Y_LABEL        = "°/s"          # °/s
X_LABEL        = "Time (min)"
COLOR          = "#33ff00"
SOURCE         = "telemetry"
TELEMETRY_KEY  = "FSM_av_0"

# The dashboard renders this panel as a 2x2 quad of sub-charts.
MULTI_SERIES   = True
# (cell label, telemetry key, colour) — magnitude has key None (computed).
GYRO_AXES = [
    ("X",   "FSM_av_0", "#33ff00"),
    ("Y",   "FSM_av_1", "#00ff88"),
    ("Z",   "FSM_av_2", "#ffb000"),
    ("MAG", None,       "#ff6600"),
]


def compute():
    """Return (MET minutes, values) for the X axis (FSM_av_0). Legacy single-series.

    Kept so missioncontrol.py / stress_bench.py keep working; the web dashboard
    uses compute_series() instead.
    """
    rows = packet_store.telemetry_series(TELEMETRY_KEY)
    if not rows:
        return np.array([]), np.array([])

    values = np.array([r["value"] for r in rows], dtype=float)
    try:
        t_min = np.array([mission_time.met_minutes(r["timestamp"]) for r in rows])
    except Exception:
        t_min = np.arange(len(values), dtype=float)

    return t_min, values


def compute_series():
    """Return all 3 body-axis rates + magnitude, aligned by packet timestamp.

    Shape::

        {"x": [MET minutes, ...],
         "series": [{"label": "X", "color": "#..", "y": [..]}, ... 4 entries]}

    The three axes are stored together in each beacon frame, so they share
    timestamps; we align on the timestamps present for all three and compute the
    magnitude per sample. Read-only (SELECT via telemetry_series).
    """
    axis_keys = ["FSM_av_0", "FSM_av_1", "FSM_av_2"]
    maps: dict[str, dict] = {}
    for key in axis_keys:
        m = {}
        for r in packet_store.telemetry_series(key):
            m[r["timestamp"]] = r["value"]
        maps[key] = m

    empty = {"x": [], "series": [{"label": lbl, "color": clr, "y": []}
                                 for lbl, _key, clr in GYRO_AXES]}

    # Timestamps present for ALL three axes (same beacon frame => same timestamp).
    common = set(maps[axis_keys[0]])
    for key in axis_keys[1:]:
        common &= set(maps[key])
    timestamps = sorted(common)
    if not timestamps:
        return empty

    try:
        x = [mission_time.met_minutes(ts) for ts in timestamps]
    except Exception:
        x = [float(i) for i in range(len(timestamps))]

    ax = [maps["FSM_av_0"][ts] for ts in timestamps]
    ay = [maps["FSM_av_1"][ts] for ts in timestamps]
    az = [maps["FSM_av_2"][ts] for ts in timestamps]
    mag = [(a * a + b * b + c * c) ** 0.5 for a, b, c in zip(ax, ay, az)]

    by_label = {"X": ax, "Y": ay, "Z": az, "MAG": mag}
    series = [{"label": lbl, "color": clr, "y": by_label[lbl]}
              for lbl, _key, clr in GYRO_AXES]
    return {"x": x, "series": series}
