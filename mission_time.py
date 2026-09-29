"""
Mission time — the one definition of MET (mission elapsed time) shared by the
dashboard clock and the time axes of the telemetry panels.
"""

from datetime import datetime, timezone

# HUCSat deployment from the ISS.
MISSION_EPOCH_UTC = "2026-07-02T09:00:00+00:00"  # Thu Jul 2 2026, 05:00 EDT (Boston)
MISSION_EPOCH = datetime.fromisoformat(MISSION_EPOCH_UTC)


def met_minutes(timestamp: str) -> float:
    """Minutes since the mission epoch for an ISO-8601 timestamp (naive = UTC)."""
    t = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (t - MISSION_EPOCH).total_seconds() / 60.0
