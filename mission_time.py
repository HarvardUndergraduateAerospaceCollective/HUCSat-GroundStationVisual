"""
Mission time — the one definition of MET (mission elapsed time) shared by the
dashboard clock and the time axes of the telemetry panels.
"""

from datetime import datetime, timezone

# HUCSat deployment from the ISS.
MISSION_EPOCH_UTC = "2026-07-02T09:00:00+00:00"  # Thu Jul 2 2026, 05:00 EDT (Boston)
MISSION_EPOCH = datetime.fromisoformat(MISSION_EPOCH_UTC)


# The flight computer's clock restarts at 2000-01-01 00:00:00 on every boot.
FC_CLOCK_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)
FC_CLOCK_EPOCH_UNIX = 946684800          # FC_CLOCK_EPOCH as Unix seconds
_UPTIME_PLAUSIBLE_MAX = 315_360_000      # 10 years: anything bigger is a clock reading


def fc_uptime_seconds(rtc=None, uptime=None):
    """Seconds since the flight computer last booted, or None if unknown.

    Because its clock restarts at 2000-01-01 on every boot, the beacon's "time"
    string (1 s resolution) reads directly as time since boot. The "uptime"
    field is the same clock in Unix seconds, held in CircuitPython's
    reduced-precision float, which truncates it to 256 s; it's the fallback.
    """
    if rtc:
        try:
            t = datetime.strptime(rtc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            secs = (t - FC_CLOCK_EPOCH).total_seconds()
            if secs >= 0:
                return int(secs)
        except (TypeError, ValueError):
            pass
    try:
        v = float(uptime)
    except (TypeError, ValueError):
        return None
    if v > _UPTIME_PLAUSIBLE_MAX:
        # Truncation can put a reading from the first 256 s just below the epoch.
        v = max(0.0, v - FC_CLOCK_EPOCH_UNIX)
    return int(v) if v >= 0 else None


def met_minutes(timestamp: str) -> float:
    """Minutes since the mission epoch for an ISO-8601 timestamp (naive = UTC)."""
    t = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (t - MISSION_EPOCH).total_seconds() / 60.0
