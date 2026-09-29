"""
Panel: Best Direction
Displays which CubeSat face has the best sun exposure (orient_best_direction).
Data source: packet_store telemetry table, key "FSM_best_dir".

Unlike other panels, this returns *distribution* data (counts per face)
rather than a time series, since the value is a discrete direction index.
Only real faces are charted; readings with no direction (-1, e.g. every
detumble-state beacon) are left out of the chart but counted, so the panel
can show what share of packets carry a real direction.

Direction mapping (from OBC state_orient.py):
    0 → +Y    1 → −X    2 → −Y    3 → +X    −1 → N/A
"""

import packet_store

TITLE         = "BEST DIRECTION"
COLOR         = "#ffb000"
SOURCE        = "telemetry"
TELEMETRY_KEY = "FSM_best_dir"

# Label for each face index (matches OBC orient_best_direction mapping)
FACE_LABELS = {
    0: "+Y",
    1: "\u2212X",   # −X  (proper minus sign)
    2: "\u2212Y",   # −Y
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


def compute():
    """Return distribution dict for the doughnut chart.

    Returns dict with keys:
        labels  – face direction labels  ["+Y", "−X", "−Y", "+X"]
        counts  – number of readings per direction
        colors  – per-slice color
        latest  – most recent real direction index (int, -1 if none yet)
        latest_label – human-readable label for latest
        with_direction – readings with a real direction
        total          – all best-direction readings (incl. no direction)
        pct_with_direction – with_direction / total, as a percentage
    """
    rows = packet_store.telemetry_series(TELEMETRY_KEY)

    faces = [0, 1, 2, 3]
    counts = {k: 0 for k in faces}
    no_direction = 0
    latest = -1
    for r in rows:
        val = int(round(r["value"]))
        if val in counts:
            counts[val] += 1
            latest = val
        else:
            no_direction += 1

    with_direction = sum(counts.values())
    total = with_direction + no_direction
    return {
        "labels":       [FACE_LABELS[k] for k in faces],
        "counts":       [counts[k] for k in faces],
        "colors":       [FACE_COLORS[k] for k in faces],
        "latest":       latest,
        "latest_label": FACE_LABELS[latest] if latest in counts else "-",
        "with_direction": with_direction,
        "total":          total,
        "pct_with_direction": round(100 * with_direction / total, 1) if total else 0.0,
    }
