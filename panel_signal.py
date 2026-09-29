"""
Panel: Onboard RSSI
The satellite's own RSSI reading, carried in every frame's header: the flight
firmware writes abs(radio.get_rssi()) -- the RSSI of the last LoRa packet the
*satellite* received. It is not ground-station signal strength (the TinyGS
webhook doesn't include that).
Data source: packet_store raw frames, decoded with beacon_decoder.
"""

import numpy as np
import beacon_decoder
import mission_time
import packet_store

TITLE   = "ONBOARD RSSI"
Y_LABEL = "RSSI (dBm)"
X_LABEL = "Time (min)"
COLOR   = "#33ff00"
SOURCE  = "telemetry"


def compute():
    """Return (MET minutes, onboard_rssi_dbm) arrays, oldest first.

    Decoded from each stored frame's header; frames without a HUCSat header
    are skipped. Returns empty arrays when there is nothing to show.
    """
    t_min, values = [], []
    for r in reversed(packet_store.recent_packets(n=3000)):
        raw = r.get("raw_frame")
        rssi = beacon_decoder.onboard_rssi_dbm(bytes(raw)) if raw is not None else None
        if rssi is None:
            continue
        try:
            t_min.append(mission_time.met_minutes(r["received_at"]))
        except (TypeError, ValueError):
            continue
        values.append(rssi)
    return np.array(t_min, dtype=float), np.array(values, dtype=float)
