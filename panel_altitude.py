"""
Panel: Orbital Altitude
Live altitude above Earth's surface: the last WINDOW_MIN minutes up to now,
propagated from the current TLE with SGP4 (Keplerian fallback without one).
"""

import time

import numpy as np

import mission_time

TITLE   = "ALTITUDE"
Y_LABEL = "Alt (km)"
X_LABEL = "Time (min)"
COLOR   = "#33ff00"
SOURCE  = "orbital"


WINDOW_MIN = 240   # matches PANEL_WINDOWS["panel_altitude"] in web_server.py


def compute(orbital_elements: dict, n_orbits: float, n_points: int = 500):
    """Return (MET minutes, altitude_km) for the last WINDOW_MIN minutes to now.

    Parameters
    ----------
    orbital_elements : dict
        Must contain keys ``sma`` and ``eccentricity`` (Keplerian fallback).
    n_orbits : float
        Unused; kept so existing callers (missioncontrol.py) still work.
    n_points : int
    """
    from visualizer import altitude_window, orbital_altitude, orbital_period

    now = time.time()
    epoch = mission_time.MISSION_EPOCH.timestamp()
    live = altitude_window(now - WINDOW_MIN * 60, now, n_points)
    if live is not None:
        t_unix, alt_km = live
        return (t_unix - epoch) / 60.0, alt_km

    # No TLE/SGP4: there's no absolute orbital phase, so show the Keplerian
    # profile over the same span, ending now.
    sma, ecc = orbital_elements["sma"], orbital_elements["eccentricity"]
    t_sec, alt_km = orbital_altitude(sma, ecc,
                                     n_orbits=WINDOW_MIN * 60 / orbital_period(sma),
                                     n_points=n_points)
    return (now - epoch - (t_sec[-1] - t_sec)) / 60.0, alt_km
