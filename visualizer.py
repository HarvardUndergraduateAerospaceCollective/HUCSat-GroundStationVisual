"""
Satellite Orbit Data Module
Handles TLE fetching from CelesTrak and Keplerian orbital mechanics.
All computation, no plotting — plotting lives in missioncontrol.py.
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

try:
    from sgp4.api import Satrec as _Satrec
    _SGP4_AVAILABLE = True
except ImportError:
    _SGP4_AVAILABLE = False

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

CACHE_DIR = Path(__file__).parent / ".tle_cache"
CACHE_MAX_AGE = 2 * 60 * 60  # 2 hours in seconds

# Default satellite for all orbit calculations: HUCSat / CubeSAT-I, cataloged
# 2026-07 as NORAD 69794 ("ISS OBJECT YJ", int'l designator 1998-067YJ — an
# ISS-deployed object; inclination ~51.6°). TLEs are fetched live from CelesTrak
# and cached 2h (CACHE_MAX_AGE); web_server.py re-fetches on that cadence while
# running. (Superseded the pre-launch predicted-TLE override once cataloged.)
DEFAULT_CAT_NR = 69794

MU_EARTH = 3.986004418e14          # m^3 s^-2
R_EARTH = 6_371_000                # m
EARTH_ROT_RATE = 7.2921159e-5      # rad/s
DEG = np.degrees
RAD = np.radians

# ──────────────────────────────────────────────
# SGP4 state (set once TLE lines are loaded)
# ──────────────────────────────────────────────

_sgp4_sat = None          # Satrec object; None until TLE lines are available
_sgp4_epoch_unix = 0.0    # TLE epoch as Unix timestamp
_sgp4_period = 0.0        # Orbital period in seconds


def has_sgp4() -> bool:
    """True when an SGP4 model is loaded and ready."""
    return _sgp4_sat is not None


def _init_sgp4(line1: str, line2: str, epoch_unix: float, sma: float):
    """Build the module-level SGP4 satellite model from raw TLE lines."""
    global _sgp4_sat, _sgp4_epoch_unix, _sgp4_period
    if not _SGP4_AVAILABLE:
        return
    try:
        _sgp4_sat = _Satrec.twoline2rv(line1, line2)
        _sgp4_epoch_unix = epoch_unix
        _sgp4_period = orbital_period(sma)
    except Exception:
        _sgp4_sat = None


def _gmst_rad(jd: np.ndarray) -> np.ndarray:
    """Greenwich Mean Sidereal Time in radians for the given Julian date(s). IAU 1982."""
    T = (jd - 2451545.0) / 36525.0
    gmst_sec = (67310.54841
                + (876600.0 * 3600.0 + 8640184.812866) * T
                + 0.093104 * T ** 2
                - 6.2e-6 * T ** 3)
    return (gmst_sec % 86400.0) * (2.0 * np.pi / 86400.0)


def _sgp4_propagate(n_orbits: float, n_points: int, start_orbit: float):
    """
    Propagate from (TLE epoch + start_orbit × period) for n_orbits.
    Returns (lon_deg, lat_deg, alt_km, t_sec) — same contract as the
    Keplerian functions.  t_sec is seconds since the start of the window.
    """
    _JD_UNIX = 2440587.5          # Julian date of Unix epoch (1970-01-01)
    _R_EARTH_KM = R_EARTH / 1000.0

    t_start = _sgp4_epoch_unix + start_orbit * _sgp4_period
    t_unix = np.linspace(t_start, t_start + n_orbits * _sgp4_period, n_points)

    jd = t_unix / 86400.0 + _JD_UNIX
    jd_whole = np.floor(jd)
    jd_frac = jd - jd_whole

    # Vectorised propagation (sgp4 ≥ 2.21); loop fallback for older installs
    try:
        e_arr, r_arr, _ = _sgp4_sat.sgp4_array(jd_whole, jd_frac)
        bad = e_arr != 0
        if bad.any():
            for i in np.where(bad)[0]:
                r_arr[i] = r_arr[i - 1] if i > 0 else r_arr[min(i + 1, n_points - 1)]
    except AttributeError:
        r_arr = np.zeros((n_points, 3))
        for i in range(n_points):
            e, pos, _ = _sgp4_sat.sgp4(float(jd_whole[i]), float(jd_frac[i]))
            r_arr[i] = pos if e == 0 else (r_arr[i - 1] if i > 0 else [0.0, 0.0, 0.0])

    # TEME → ECEF: rotate around z-axis by GMST
    gmst = _gmst_rad(jd)
    cos_g, sin_g = np.cos(gmst), np.sin(gmst)
    x = r_arr[:, 0] * cos_g + r_arr[:, 1] * sin_g
    y = -r_arr[:, 0] * sin_g + r_arr[:, 1] * cos_g
    z = r_arr[:, 2]

    # ECEF → geodetic (spherical, consistent with R_EARTH used throughout)
    lon = (np.degrees(np.arctan2(y, x)) + 180.0) % 360.0 - 180.0
    lat = np.degrees(np.arctan2(z, np.hypot(x, y)))   # geocentric latitude
    alt_km = np.sqrt(x ** 2 + y ** 2 + z ** 2) - _R_EARTH_KM

    return lon, lat, alt_km, t_unix - t_unix[0]


def _parse_tle_epoch(epoch_token: str) -> float:
    """Parse TLE epoch token (YYDDD.DDD...) into UTC Unix seconds."""
    token = epoch_token.strip()
    if len(token) < 5:
        raise ValueError(f"Invalid TLE epoch token: {epoch_token!r}")

    year_2d = int(token[:2])
    doy = float(token[2:])
    year = year_2d + (2000 if year_2d < 57 else 1900)

    epoch_dt = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1.0)
    return epoch_dt.timestamp()


def _parse_tle_lines(line1: str, line2: str):
    """Parse TLE lines into orbital elements and epoch/phase metadata."""
    inclination = float(line2[8:16].strip())
    raan = float(line2[17:25].strip())
    eccentricity = float("0." + line2[26:33].strip())
    arg_periapsis = float(line2[34:42].strip())
    mean_anomaly = float(line2[43:51].strip())
    mean_motion = float(line2[52:63].strip())
    sma = (MU_EARTH / (mean_motion * 2 * np.pi / 86400) ** 2) ** (1 / 3)

    epoch_token = line1[18:32]
    epoch_unix = _parse_tle_epoch(epoch_token)

    return {
        "inclination": inclination,
        "raan": raan,
        "eccentricity": eccentricity,
        "arg_periapsis": arg_periapsis,
        "semi_major_axis": sma,
        "mean_anomaly_deg": mean_anomaly,
        "epoch_unix": epoch_unix,
    }


def _fetch_tle_lines(cat_nr: int):
    """Fetch raw TLE lines (line1, line2) from CelesTrak for a NORAD catalog number."""
    import requests

    url = f"https://celestrak.org/NORAD/elements/gp.php?CATNR={cat_nr}&FORMAT=TLE"
    response = requests.get(url, timeout=20)
    if response.status_code != 200:
        raise Exception(f"Failed to fetch TLE data: {response.status_code}")

    tle_data = response.text.strip().splitlines()
    if len(tle_data) < 3:
        raise Exception("Unexpected TLE response format")

    return tle_data[1].rstrip(), tle_data[2].rstrip()


# ──────────────────────────────────────────────
# Orbital mechanics
# ──────────────────────────────────────────────

def kepler_equation(M: np.ndarray, e: float, tol: float = 1e-10) -> np.ndarray:
    """Solve Kepler's equation  M = E - e·sin(E)  via Newton-Raphson."""
    E = M.copy()
    for _ in range(50):
        dE = (E - e * np.sin(E) - M) / (1.0 - e * np.cos(E))
        E -= dE
        if np.max(np.abs(dE)) < tol:
            break
    return E


def orbital_period(semi_major_axis: float) -> float:
    """Return the orbital period in seconds."""
    return 2 * np.pi * np.sqrt(semi_major_axis ** 3 / MU_EARTH)


def ground_track(
    semi_major_axis: float,
    eccentricity: float,
    inclination: float,
    raan: float,
    arg_periapsis: float,
    n_orbits: float = 3,
    n_points: int = 3000,
    start_orbit: float = 0.0,
):
    """
    Compute sub-satellite ground track.
    Returns (longitude, latitude, time_seconds) arrays.
    """
    if _sgp4_sat is not None:
        lon, lat, _alt, t = _sgp4_propagate(n_orbits, n_points, start_orbit)
        return lon, lat, t

    inc = RAD(inclination)
    Omega = RAD(raan)
    omega = RAD(arg_periapsis)

    period = orbital_period(semi_major_axis)
    start_t = max(start_orbit, 0.0) * period
    t = np.linspace(start_t, start_t + n_orbits * period, n_points)
    M = 2 * np.pi / period * t
    E = kepler_equation(M, eccentricity)

    nu = 2 * np.arctan2(
        np.sqrt(1 + eccentricity) * np.sin(E / 2),
        np.sqrt(1 - eccentricity) * np.cos(E / 2),
    )

    u = omega + nu
    lat = np.arcsin(np.sin(inc) * np.sin(u))
    lon = np.arctan2(np.sin(u) * np.cos(inc), np.cos(u)) + Omega
    lon -= EARTH_ROT_RATE * t

    lon = DEG(lon)
    lat = DEG(lat)
    lon = (lon + 180) % 360 - 180

    return lon, lat, t


def ground_track_with_alt(
    semi_major_axis: float,
    eccentricity: float,
    inclination: float,
    raan: float,
    arg_periapsis: float,
    n_orbits: float = 3,
    n_points: int = 3000,
    start_orbit: float = 0.0,
):
    """
    Compute sub-satellite ground track with altitude.
    Returns (longitude, latitude, altitude_km, time_seconds) arrays.
    """
    if _sgp4_sat is not None:
        return _sgp4_propagate(n_orbits, n_points, start_orbit)

    inc = RAD(inclination)
    Omega = RAD(raan)
    omega = RAD(arg_periapsis)

    period = orbital_period(semi_major_axis)
    start_t = max(start_orbit, 0.0) * period
    t = np.linspace(start_t, start_t + n_orbits * period, n_points)
    M = 2 * np.pi / period * t
    E = kepler_equation(M, eccentricity)

    nu = 2 * np.arctan2(
        np.sqrt(1 + eccentricity) * np.sin(E / 2),
        np.sqrt(1 - eccentricity) * np.cos(E / 2),
    )
    u = omega + nu
    lat = np.arcsin(np.sin(inc) * np.sin(u))
    lon = np.arctan2(np.sin(u) * np.cos(inc), np.cos(u)) + Omega
    lon -= EARTH_ROT_RATE * t
    lon = (DEG(lon) + 180) % 360 - 180
    lat = DEG(lat)

    r = semi_major_axis * (1 - eccentricity * np.cos(E))
    alt_km = (r - R_EARTH) / 1000.0

    return lon, lat, alt_km, t


def orbital_altitude(
    semi_major_axis: float,
    eccentricity: float,
    n_orbits: float = 3,
    n_points: int = 3000,
):
    """
    Compute altitude above Earth's surface over time.
    Returns (time_seconds, altitude_km) arrays.
    """
    if _sgp4_sat is not None:
        _, _, alt_km, t_sec = _sgp4_propagate(n_orbits, n_points, start_orbit=0.0)
        return t_sec, alt_km

    period = orbital_period(semi_major_axis)
    t = np.linspace(0, n_orbits * period, n_points)
    M = 2 * np.pi / period * t
    E = kepler_equation(M, eccentricity)
    r = semi_major_axis * (1 - eccentricity * np.cos(E))
    alt_km = (r - R_EARTH) / 1000.0
    return t, alt_km


def altitude_window(t_start_unix: float, t_end_unix: float, n_points: int = 500):
    """Altitude over an absolute UTC window via SGP4.

    Returns (t_unix, alt_km), or None when SGP4 isn't loaded (no TLE yet).
    """
    if _sgp4_sat is None or _sgp4_period <= 0:
        return None
    n_orbits = (t_end_unix - t_start_unix) / _sgp4_period
    start_orbit = (t_start_unix - _sgp4_epoch_unix) / _sgp4_period
    _, _, alt_km, t_rel = _sgp4_propagate(n_orbits, n_points, start_orbit)
    return t_start_unix + t_rel, alt_km


def _sun_unit(t_unix: np.ndarray) -> np.ndarray:
    """Unit vector to the Sun, equator of date (Astronomical Almanac low-precision, ~0.01°)."""
    d = (t_unix - 946728000.0) / 86400.0           # days since J2000.0 (2000-01-01 12:00 UTC)
    L = np.radians((280.460 + 0.9856474 * d) % 360.0)
    g = np.radians((357.528 + 0.9856003 * d) % 360.0)
    lam = L + np.radians(1.915) * np.sin(g) + np.radians(0.020) * np.sin(2 * g)
    eps = np.radians(23.439 - 4e-7 * d)
    return np.stack([np.cos(lam), np.cos(eps) * np.sin(lam), np.sin(eps) * np.sin(lam)], axis=-1)


def in_sunlight(t_unix):
    """Whether the satellite is outside Earth's shadow at each UTC time, via SGP4.

    Cylindrical shadow model; it agreed with TinyGS's own sunLit flag on 990 of
    993 packets (2026-09-29..10-01). Returns a bool array, or None when SGP4
    isn't loaded. Times SGP4 can't propagate count as not sunlit.
    """
    if _sgp4_sat is None:
        return None
    t = np.atleast_1d(np.asarray(t_unix, dtype=float))
    jd = t / 86400.0 + 2440587.5
    jd_whole = np.floor(jd)
    err, r, _ = _sgp4_sat.sgp4_array(jd_whole, jd - jd_whole)
    s = _sun_unit(t)
    along = np.einsum("ij,ij->i", r, s)                 # km toward the Sun
    off_axis_sq = np.einsum("ij,ij->i", r, r) - along ** 2
    shadow = (along < 0) & (off_axis_sq < 6378.137 ** 2)
    return ~shadow & (err == 0)


# ──────────────────────────────────────────────
# TLE fetching
# ──────────────────────────────────────────────

def _cache_path(cat_nr: int) -> Path:
    return CACHE_DIR / f"{cat_nr}.json"


def _load_cache_payload(cat_nr: int):
    path = _cache_path(cat_nr)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if time.time() - data["timestamp"] < CACHE_MAX_AGE:
            return data
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _load_cache(cat_nr: int):
    payload = _load_cache_payload(cat_nr)
    if payload is None:
        return None
    elements = payload.get("elements")
    if isinstance(elements, list) and len(elements) >= 5:
        return tuple(elements[:5])
    return None


def _save_cache(cat_nr: int, elements: tuple, extras=None):
    CACHE_DIR.mkdir(exist_ok=True)
    payload = {"timestamp": time.time(), "elements": list(elements)}
    if extras:
        payload.update(extras)
    _cache_path(cat_nr).write_text(json.dumps(payload))


def get_orbital_state(cat_nr: int = DEFAULT_CAT_NR):
    """Fetch TLE and return epoch-anchored orbital state.

    Returns keys:
      inclination, raan, eccentricity, arg_periapsis,
      semi_major_axis, mean_anomaly_deg, epoch_unix
    """
    cached_payload = _load_cache_payload(cat_nr)
    if cached_payload is not None:
        cached_elements = cached_payload.get("elements")
        if (
            isinstance(cached_elements, list)
            and len(cached_elements) >= 5
            and "mean_anomaly_deg" in cached_payload
            and "epoch_unix" in cached_payload
        ):
            state = {
                "inclination": float(cached_elements[0]),
                "raan": float(cached_elements[1]),
                "eccentricity": float(cached_elements[2]),
                "arg_periapsis": float(cached_elements[3]),
                "semi_major_axis": float(cached_elements[4]),
                "mean_anomaly_deg": float(cached_payload["mean_anomaly_deg"]),
                "epoch_unix": float(cached_payload["epoch_unix"]),
            }
            if _sgp4_sat is None:
                l1 = cached_payload.get("tle_line1")
                l2 = cached_payload.get("tle_line2")
                if l1 and l2:
                    _init_sgp4(l1, l2, state["epoch_unix"], state["semi_major_axis"])
            return state

    try:
        line1, line2 = _fetch_tle_lines(cat_nr)
        state = _parse_tle_lines(line1, line2)
    except Exception:
        # Fallback for legacy cache entries that predate epoch/mean-anomaly fields.
        if cached_payload is not None:
            cached_elements = cached_payload.get("elements")
            if isinstance(cached_elements, list) and len(cached_elements) >= 5:
                return {
                    "inclination": float(cached_elements[0]),
                    "raan": float(cached_elements[1]),
                    "eccentricity": float(cached_elements[2]),
                    "arg_periapsis": float(cached_elements[3]),
                    "semi_major_axis": float(cached_elements[4]),
                    "mean_anomaly_deg": float(cached_payload.get("mean_anomaly_deg", 0.0)),
                    "epoch_unix": float(cached_payload.get("epoch_unix", time.time())),
                }
        raise

    elements = (
        state["inclination"],
        state["raan"],
        state["eccentricity"],
        state["arg_periapsis"],
        state["semi_major_axis"],
    )
    _save_cache(
        cat_nr,
        elements,
        extras={
            "mean_anomaly_deg": state["mean_anomaly_deg"],
            "epoch_unix": state["epoch_unix"],
            "tle_line1": line1,
            "tle_line2": line2,
        },
    )
    _init_sgp4(line1, line2, state["epoch_unix"], state["semi_major_axis"])
    return state


def get_elements(cat_nr: int = DEFAULT_CAT_NR):
    """
    Fetch TLE from CelesTrak for the given NORAD catalog number.
    Results are cached for 2 hours to avoid rate-limiting.
    Returns (inclination, raan, eccentricity, arg_periapsis, semi_major_axis).
    """
    state = get_orbital_state(cat_nr)
    return (
        state["inclination"],
        state["raan"],
        state["eccentricity"],
        state["arg_periapsis"],
        state["semi_major_axis"],
    )
