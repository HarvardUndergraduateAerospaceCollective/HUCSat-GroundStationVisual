"""
Panel: Signal Strength
Displays ground-station RSSI from received TinyGS packets over time.
Data source: packet_store (packets.rssi column, populated by tinygs_mqtt).
"""

import numpy as np
import mission_time
import packet_store

TITLE   = "SIGNAL"
Y_LABEL = "RSSI (dBm)"
X_LABEL = "Time (min)"
COLOR   = "#33ff00"
SOURCE  = "telemetry"


def compute():
    """Return (MET minutes, rssi_dbm) arrays from stored packets.

    Reads RSSI values from the packets table.  Returns empty arrays
    when no packets have been received yet.
    """
    rows = packet_store.recent_packets(n=500)
    if not rows:
        return np.array([]), np.array([])

    # Build arrays — oldest first
    rows = list(reversed(rows))
    values = np.array([r["rssi"] for r in rows if r.get("rssi") is not None],
                      dtype=float)
    if len(values) == 0:
        return np.array([]), np.array([])

    # X-axis: minutes of mission elapsed time (sample index if unparseable)
    try:
        t_min = np.array([mission_time.met_minutes(r["received_at"]) for r in rows
                          if r.get("rssi") is not None])
    except Exception:
        t_min = np.arange(len(values), dtype=float)

    return t_min, values
