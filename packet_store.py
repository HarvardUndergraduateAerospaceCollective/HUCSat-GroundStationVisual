"""
Packet Store — SQLite database for long-term telemetry storage.

Stores raw packets received from TinyGS (or any other source) and
parsed telemetry key-value pairs for post-mission analysis.

Database file lives next to this script as ``mission_data.db``.
All timestamps are stored as ISO-8601 UTC strings.
"""

import base64
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_DB_PATH = Path(__file__).parent / "mission_data.db"
_local = threading.local()

# ──────────────────────────────────────────────
# Connection helpers
# ──────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection (creates DB/tables on first call)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        # Two processes write this DB (dashboard + aws_sync); wait up to 30 s
        # for the write lock rather than Python's 5 s default.
        conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        # WAL is safe for concurrent readers. Switching a brand-new DB file to
        # WAL needs an exclusive lock that ignores the busy timeout, so retry
        # briefly when several connections open a fresh file at once.
        for attempt in range(50):
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError:
                if attempt == 49:
                    conn.close()
                    raise
                time.sleep(0.1)
        conn.execute("PRAGMA foreign_keys=ON")
        _init_tables(conn)
        _local.conn = conn
    return conn


@contextmanager
def _cursor():
    conn = _get_conn()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _init_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS packets (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at   TEXT    NOT NULL,          -- ISO-8601 UTC
            satellite     TEXT    NOT NULL DEFAULT '',
            norad_id      INTEGER,
            station       TEXT    NOT NULL DEFAULT '',
            frequency_mhz REAL,
            rssi          REAL,
            snr           REAL,
            crc_error     INTEGER DEFAULT 0,           -- 1 if CRC failed
            raw_frame     BLOB,
            decoded_json  TEXT,                       -- full decoded beacon telemetry
            source        TEXT    NOT NULL DEFAULT 'unknown',
            frame_hash    TEXT                        -- SHA-256 hex digest of raw_frame
        );

        CREATE INDEX IF NOT EXISTS idx_packets_sat
            ON packets(satellite);
        CREATE INDEX IF NOT EXISTS idx_packets_time
            ON packets(received_at);

        CREATE TABLE IF NOT EXISTS telemetry (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            packet_id  INTEGER NOT NULL REFERENCES packets(id),
            timestamp  TEXT    NOT NULL,               -- ISO-8601 UTC
            key        TEXT    NOT NULL,
            value      REAL,
            unit       TEXT    NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_telem_key_time
            ON telemetry(key, timestamp);
    """)

    # Migration: add frame_hash column to existing databases that lack it.
    try:
        conn.execute("ALTER TABLE packets ADD COLUMN frame_hash TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_packets_frame_hash "
        "ON packets(frame_hash) WHERE frame_hash IS NOT NULL"
    )
    conn.commit()


# ──────────────────────────────────────────────
# Write API
# ──────────────────────────────────────────────

def store_packet(
    *,
    satellite: str = "",
    norad_id: Optional[int] = None,
    station: str = "",
    frequency_mhz: Optional[float] = None,
    rssi: Optional[float] = None,
    snr: Optional[float] = None,
    crc_error: bool = False,
    raw_frame: Optional[bytes] = None,
    decoded: Optional[dict] = None,
    source: str = "unknown",
    received_at: Optional[str] = None,
) -> int:
    """Insert a packet and return its row id."""
    if received_at is None:
        received_at = datetime.now(timezone.utc).isoformat()
    decoded_json = json.dumps(decoded) if decoded else None
    frame_hash = hashlib.sha256(raw_frame).hexdigest() if raw_frame is not None else None

    with _cursor() as cur:
        cur.execute(
            """INSERT INTO packets
               (received_at, satellite, norad_id, station,
                frequency_mhz, rssi, snr, crc_error,
                raw_frame, decoded_json, source, frame_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (received_at, satellite, norad_id, station,
             frequency_mhz, rssi, snr, int(crc_error),
             raw_frame, decoded_json, source, frame_hash),
        )
        return cur.lastrowid


def store_packet_if_new(
    *,
    satellite: str = "",
    norad_id: Optional[int] = None,
    station: str = "",
    frequency_mhz: Optional[float] = None,
    rssi: Optional[float] = None,
    snr: Optional[float] = None,
    crc_error: bool = False,
    raw_frame: Optional[bytes] = None,
    decoded: Optional[dict] = None,
    source: str = "unknown",
    received_at: Optional[str] = None,
    telemetry: Optional[list[tuple[str, float, str]]] = None,
) -> tuple[int, bool]:
    """Insert a packet only if its frame has not been seen before.

    Returns ``(packet_id, was_new)`` — if a duplicate exists the
    existing row id is returned and *was_new* is ``False``.
    Packets without a *raw_frame* (hash is NULL) are always inserted.
    *telemetry* (key, value, unit) rows are written in the same transaction
    as the packet, so a crash can never leave a packet without them.
    """
    if received_at is None:
        received_at = datetime.now(timezone.utc).isoformat()
    decoded_json = json.dumps(decoded) if decoded else None
    frame_hash = hashlib.sha256(raw_frame).hexdigest() if raw_frame is not None else None

    row_values = (received_at, satellite, norad_id, station,
                  frequency_mhz, rssi, snr, int(crc_error),
                  raw_frame, decoded_json, source, frame_hash)

    if frame_hash is None:
        # No raw frame — can't dedup, always insert
        with _cursor() as cur:
            packet_id = _insert_packet_row(cur, row_values)
            _insert_telemetry_rows(cur, packet_id, telemetry, received_at)
        return (packet_id, True)

    # Single transaction: check + insert atomically. Take the write lock up
    # front — in a deferred transaction the SELECT holds a read snapshot, and if
    # another writer commits before our INSERT, SQLite fails the upgrade with
    # "database is locked" immediately, without honouring the busy timeout.
    with _cursor() as cur:
        if not cur.connection.in_transaction:
            cur.execute("BEGIN IMMEDIATE")
        cur.execute("SELECT id FROM packets WHERE frame_hash = ?", (frame_hash,))
        row = cur.fetchone()
        if row is not None:
            return (row[0], False)

        packet_id = _insert_packet_row(cur, row_values)
        _insert_telemetry_rows(cur, packet_id, telemetry, received_at)
        return (packet_id, True)


def _insert_packet_row(cur: sqlite3.Cursor, row_values: tuple) -> int:
    cur.execute(
        """INSERT INTO packets
           (received_at, satellite, norad_id, station,
            frequency_mhz, rssi, snr, crc_error,
            raw_frame, decoded_json, source, frame_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        row_values,
    )
    return cur.lastrowid


def _insert_telemetry_rows(cur: sqlite3.Cursor, packet_id: int,
                           readings: Optional[list[tuple[str, float, str]]],
                           timestamp: str) -> None:
    if readings:
        cur.executemany(
            "INSERT INTO telemetry (packet_id, timestamp, key, value, unit) "
            "VALUES (?, ?, ?, ?, ?)",
            [(packet_id, timestamp, k, v, u) for k, v, u in readings],
        )


def store_telemetry(packet_id: int, key: str, value: float,
                    unit: str = "", timestamp: Optional[str] = None):
    """Insert a single parsed telemetry reading linked to a packet."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO telemetry (packet_id, timestamp, key, value, unit) "
            "VALUES (?, ?, ?, ?, ?)",
            (packet_id, timestamp, key, value, unit),
        )


def store_telemetry_batch(packet_id: int,
                          readings: list[tuple[str, float, str]],
                          timestamp: Optional[str] = None):
    """Insert many (key, value, unit) telemetry rows for one packet."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    with _cursor() as cur:
        cur.executemany(
            "INSERT INTO telemetry (packet_id, timestamp, key, value, unit) "
            "VALUES (?, ?, ?, ?, ?)",
            [(packet_id, timestamp, k, v, u) for k, v, u in readings],
        )


# ──────────────────────────────────────────────
# Read / query API
# ──────────────────────────────────────────────

def packet_count(satellite: Optional[str] = None) -> int:
    """Total number of stored packets (optionally filtered by satellite)."""
    with _cursor() as cur:
        if satellite:
            cur.execute("SELECT COUNT(*) FROM packets WHERE satellite = ?",
                        (satellite,))
        else:
            cur.execute("SELECT COUNT(*) FROM packets")
        return cur.fetchone()[0]


def packets_in_window(since_utc: str, until_utc: str) -> list[dict]:
    """Return packets received between two ISO-8601 UTC timestamps."""
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM packets WHERE received_at >= ? AND received_at <= ? "
            "ORDER BY received_at",
            (since_utc, until_utc),
        )
        return [dict(row) for row in cur.fetchall()]


def packets_since(since_iso: str) -> list[dict]:
    """Return all packets with ``received_at >= since_iso``, oldest first.

    The *raw_frame* bytes column is base64-encoded as a string so that
    the returned dicts are directly JSON-serializable.
    """
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM packets WHERE received_at >= ? "
            "ORDER BY received_at",
            (since_iso,),
        )
        rows = []
        for row in cur.fetchall():
            d = dict(row)
            if d.get("raw_frame") is not None:
                d["raw_frame"] = base64.b64encode(d["raw_frame"]).decode("ascii")
            rows.append(d)
        return rows


def recent_packets(n: int = 20, satellite: Optional[str] = None) -> list[dict]:
    """Return the *n* most recent packets as dicts."""
    with _cursor() as cur:
        if satellite:
            cur.execute(
                "SELECT * FROM packets WHERE satellite = ? "
                "ORDER BY received_at DESC LIMIT ?", (satellite, n))
        else:
            cur.execute(
                "SELECT * FROM packets ORDER BY received_at DESC LIMIT ?", (n,))
        return [dict(row) for row in cur.fetchall()]


def telemetry_series(key: str, since: Optional[str] = None,
                     satellite: Optional[str] = None) -> list[dict]:
    """Return time-series for a telemetry key as [{timestamp, value, unit}, ...]."""
    clauses = ["t.key = ?"]
    params: list = [key]

    if since:
        clauses.append("t.timestamp >= ?")
        params.append(since)
    if satellite:
        clauses.append("p.satellite = ?")
        params.append(satellite)

    where = " AND ".join(clauses)
    with _cursor() as cur:
        cur.execute(
            f"SELECT t.timestamp, t.value, t.unit "
            f"FROM telemetry t JOIN packets p ON t.packet_id = p.id "
            f"WHERE {where} ORDER BY t.timestamp",
            params,
        )
        return [dict(row) for row in cur.fetchall()]


def decoded_fields(key: str, *extra: str, after_id: int = 0) -> list[dict]:
    """Return [{id, received_at, key, *extra}, ...] for packets with
    ``id > after_id`` whose decoded JSON has ``key``, in id order.

    For fields the numeric telemetry table can't hold (e.g. FSM_pan_light,
    which is a string). Missing extra fields come back as None. Pass the
    largest id seen so far as ``after_id`` to read only new packets.
    """
    fields = (key,) + extra
    if not all(f.replace("_", "").isalnum() for f in fields):
        raise ValueError(f"not a plain field name: {fields}")
    cols = ", ".join(f"json_extract(decoded_json, '$.{f}') AS {f}" for f in fields)
    with _cursor() as cur:
        cur.execute(
            f"SELECT id, received_at, {cols} FROM packets "
            f"WHERE id > ? AND json_valid(decoded_json) "
            f"AND json_extract(decoded_json, '$.{key}') IS NOT NULL "
            f"ORDER BY id",
            (after_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def fsm_state_history(n: int = 500) -> list[dict]:
    """Return FSM state timeline from decoded packet JSON.

    Returns a list of dicts with keys: received_at, fsm_state, fsm_depl,
    uptime.  Only packets with a valid decoded_json containing FSM_state
    are included.
    """
    with _cursor() as cur:
        cur.execute(
            "SELECT received_at, decoded_json FROM packets "
            "WHERE decoded_json IS NOT NULL "
            "ORDER BY received_at DESC LIMIT ?",
            (n,),
        )
        rows = cur.fetchall()

    results = []
    for row in reversed(rows):  # oldest first
        try:
            decoded = json.loads(row["decoded_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if "FSM_state" not in decoded:
            continue
        results.append({
            "received_at": row["received_at"],
            "fsm_state": decoded.get("FSM_state", ""),
            "fsm_depl": decoded.get("FSM_depl", ""),
            "fsm_pay_set": decoded.get("FSM_pay_set", ""),
            "uptime": decoded.get("uptime", ""),
            "rtc": decoded.get("time", ""),
        })
    return results


def latest_fsm_state() -> Optional[dict]:
    """Return the most recent FSM state info, or None if no data."""
    with _cursor() as cur:
        cur.execute(
            "SELECT decoded_json FROM packets "
            "WHERE decoded_json IS NOT NULL "
            "ORDER BY received_at DESC LIMIT 20"
        )
        for row in cur.fetchall():
            try:
                decoded = json.loads(row["decoded_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            if "FSM_state" in decoded:
                return {
                    "fsm_state": decoded.get("FSM_state", ""),
                    "fsm_depl": decoded.get("FSM_depl", ""),
                    "fsm_pay_set": decoded.get("FSM_pay_set", ""),
                    "fsm_pan_light": decoded.get("FSM_pan_light", ""),
                    "fsm_payl_light": decoded.get("FSM_payl_light", ""),
                    "fsm_best_dir": decoded.get("FSM_best_dir", ""),
                    "uptime": decoded.get("uptime", ""),
                    "rtc": decoded.get("time", ""),
                }
    return None


def all_telemetry_keys() -> list[str]:
    """List distinct telemetry keys stored in the database."""
    with _cursor() as cur:
        cur.execute("SELECT DISTINCT key FROM telemetry ORDER BY key")
        return [row[0] for row in cur.fetchall()]


# ──────────────────────────────────────────────
# Export helpers (post-mission analysis)
# ──────────────────────────────────────────────

def export_packets_csv(path: str, satellite: Optional[str] = None):
    """Export all packets to a CSV file."""
    import csv
    rows = recent_packets(n=10_000_000, satellite=satellite)
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def export_telemetry_csv(path: str, key: Optional[str] = None,
                         satellite: Optional[str] = None):
    """Export telemetry time-series to a CSV file."""
    import csv
    with _cursor() as cur:
        clauses, params = [], []
        if key:
            clauses.append("t.key = ?")
            params.append(key)
        if satellite:
            clauses.append("p.satellite = ?")
            params.append(satellite)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        cur.execute(
            f"SELECT p.satellite, p.station, t.key, t.timestamp, t.value, t.unit "
            f"FROM telemetry t JOIN packets p ON t.packet_id = p.id "
            f"{where} ORDER BY t.timestamp",
            params,
        )
        rows = cur.fetchall()
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["satellite", "station", "key", "timestamp", "value", "unit"])
        writer.writerows(rows)


def summary() -> dict:
    """Quick overview of what's in the database."""
    with _cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM packets")
        pkt_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM telemetry")
        telem_count = cur.fetchone()[0]
        cur.execute("SELECT MIN(received_at), MAX(received_at) FROM packets")
        row = cur.fetchone()
        return {
            "total_packets": pkt_count,
            "total_telemetry_rows": telem_count,
            "earliest_packet": row[0],
            "latest_packet": row[1],
            "telemetry_keys": all_telemetry_keys(),
        }
