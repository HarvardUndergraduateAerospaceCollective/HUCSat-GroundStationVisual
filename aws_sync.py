"""
AWS Sync — Polls an AWS REST endpoint for satellite packets and stores them
into the local SQLite database.

Architecture
------------
TinyGS ground stations POST received packets to an AWS API Gateway endpoint
(primary collection path).  The Pi also runs a local TinyGS MQTT listener
(tinygs_mqtt.py) as a backup.  This module handles the third case: after an
MQTT disconnect, the Pi may have missed packets that AWS already collected.
It periodically polls AWS, pulling everything since the last sync, and stores
each packet into the local DB to fill any gaps.

Dedup
-----
packet_store.store_packet_if_new() deduplicates by frame_hash (SHA-256 of
raw bytes).  Packets already in the DB — whether from MQTT or a prior sync
— are silently skipped.  ``was_new=False`` means it was a duplicate.

Configuration (environment variables)
--------------------------------------
AWS_SYNC_URL      Base URL of the AWS API endpoint (required).
                  e.g. https://xxx.execute-api.us-east-1.amazonaws.com/prod
AWS_SYNC_API_KEY  API key sent as x-api-key header (optional but recommended).
AWS_SYNC_INTERVAL Poll interval in seconds (default: 60).
AWS_SYNC_SETTLE_S Only take packets at least this old, in seconds (default: 60).

Usage
-----
As a background thread::

    from aws_sync import start_sync
    start_sync()   # returns Thread or None if AWS_SYNC_URL not set

Standalone::

    python aws_sync.py

State
-----
Last-synced cursor is stored in .aws_sync_state.json (next to this script).
The file survives reboots so the sync never re-fetches the full history.
The cursor only moves past packets that were actually stored (or were
duplicates).  A packet that fails to store holds the cursor so the next poll
retries it; after _MAX_ATTEMPTS failures it is written verbatim to
aws_sync_quarantine.jsonl and skipped, so one bad packet can't stall the feed
and nothing is silently lost.  Errors from the local DB itself (locked, full,
read-only, I/O) are never blamed on a packet: the cursor just holds until the
DB recovers.  Packets younger than AWS_SYNC_SETTLE_S are left for the next
poll, so an ingest write that lands out of order in a burst can't be skipped.
To re-sync from scratch: stop the service, set last_synced to
1970-01-01T00:00:00Z, start it (frame_hash dedup makes this safe).

Timestamps
----------
Packets are stored with the AWS arrival time (received_at), except when
TinyGS delivered them late (e.g. a webhook replay after an outage): if
received_at is more than _REPLAY_THRESHOLD_S after the TinyGS reception time
(gs_time), the original reception time is stored instead so backfilled
packets plot where they belong.  The AWS received_at is kept in decoded_json.
"""

import base64
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import beacon_decoder
import packet_store

log = logging.getLogger("aws_sync")

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

AWS_SYNC_URL      = os.environ.get("AWS_SYNC_URL", "")
AWS_SYNC_API_KEY  = os.environ.get("AWS_SYNC_API_KEY", "")
AWS_SYNC_INTERVAL = int(os.environ.get("AWS_SYNC_INTERVAL", "60"))
# Overlapping ingest Lambdas can commit out of received_at order, and the sync
# query is eventually consistent; waiting this long before taking a packet
# means everything older than it has landed (ingest Lambda timeout is 10 s).
AWS_SYNC_SETTLE_S = int(os.environ.get("AWS_SYNC_SETTLE_S", "60"))

_STATE_FILE = Path(__file__).parent / ".aws_sync_state.json"
_QUARANTINE_FILE = Path(__file__).parent / "aws_sync_quarantine.jsonl"
_EPOCH      = "1970-01-01T00:00:00Z"
_TIMEOUT    = 15   # seconds for HTTP requests
_PAGE_LIMIT = 200  # packets per poll

_MAX_ATTEMPTS = 5            # store failures before a packet is quarantined
_REPLAY_THRESHOLD_S = 600    # arrival this long after reception = late delivery
# gs_time earlier than this can't be a real HUCSat reception (pre-launch, or
# the beacon RTC's year-2000 epoch) — ignore it and keep the AWS time.
_MIN_PLAUSIBLE_GS_TIME = 1735689600   # 2025-01-01T00:00:00Z

_failures: dict[str, int] = {}   # received_at -> consecutive store failures
_mem_cursor = _EPOCH             # survives a state file that can't be written


# ──────────────────────────────────────────────
# State persistence
# ──────────────────────────────────────────────

def _load_last_synced() -> str:
    """Return the ISO-8601 cursor from state file, or epoch if missing."""
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        return data.get("last_synced", _EPOCH)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _EPOCH


def _save_last_synced(ts: str) -> None:
    """Persist the ISO-8601 cursor to state file (atomically).

    The cursor is also kept in memory, so an unwritable state file can't make
    every poll re-fetch the same stale page forever.
    """
    global _mem_cursor
    if ts > _mem_cursor:
        _mem_cursor = ts
    tmp = _STATE_FILE.with_name(_STATE_FILE.name + ".tmp")
    try:
        tmp.write_text(json.dumps({"last_synced": ts}), encoding="utf-8")
        os.replace(tmp, _STATE_FILE)
    except OSError as exc:
        log.error("aws_sync: could not save sync state to %s: %s "
                  "(continuing with in-memory cursor)", _STATE_FILE, exc)


# ──────────────────────────────────────────────
# HTTP fetch
# ──────────────────────────────────────────────

def _fetch_packets(since: str) -> dict:
    """GET /packets?since=<since>&limit=<limit> and return parsed JSON.

    Raises URLError / ValueError on network or parse failures — callers
    should catch and log rather than crash.
    """
    # Encode the cursor: a literal '+' in '+00:00' arrives as a space at API
    # Gateway, which made every poll re-fetch the last packet.
    url = f"{AWS_SYNC_URL}/packets?since={quote(since, safe='')}&limit={_PAGE_LIMIT}"
    headers = {"Accept": "application/json"}
    if AWS_SYNC_API_KEY:
        headers["x-api-key"] = AWS_SYNC_API_KEY

    req = Request(url, headers=headers)
    with urlopen(req, timeout=_TIMEOUT) as resp:
        raw = resp.read()

    return json.loads(raw)


# ──────────────────────────────────────────────
# Packet processing
# ──────────────────────────────────────────────

def _reception_time(pkt: dict) -> str:
    """Timestamp to store for *pkt*: the AWS arrival time, or the original
    TinyGS reception time (gs_time) if the packet was delivered late."""
    aws_ts = pkt.get("received_at") or datetime.now(timezone.utc).isoformat()
    try:
        gs_s = float(pkt.get("gs_time") or pkt.get("unix_GS_time"))
        aws_dt = datetime.fromisoformat(aws_ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return aws_ts
    if gs_s > 100_000_000_000:          # milliseconds
        gs_s /= 1000
    if aws_dt.tzinfo is None:
        aws_dt = aws_dt.replace(tzinfo=timezone.utc)
    lag = aws_dt.timestamp() - gs_s
    if gs_s < _MIN_PLAUSIBLE_GS_TIME or lag <= _REPLAY_THRESHOLD_S:
        return aws_ts
    original = datetime.fromtimestamp(gs_s, tz=timezone.utc).isoformat(timespec="microseconds")
    log.info("aws_sync: late delivery (%.0f s after TinyGS reception) — storing original time %s",
             lag, original)
    return original


def _quarantine(pkt: dict, error: str) -> bool:
    """Append a packet that keeps failing to the quarantine file.

    Returns True only if it was written, so the caller never skips a packet
    that exists nowhere else on the Pi.
    """
    record = {"quarantined_at": datetime.now(timezone.utc).isoformat(),
              "error": error, "packet": pkt}
    try:
        with open(_QUARANTINE_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except OSError:
        log.exception("aws_sync: could not write quarantine file %s", _QUARANTINE_FILE)
        return False


def _process_and_store(pkt: dict) -> bool:
    """Decode one AWS packet dict, store in local DB.

    Returns True if the packet was newly inserted, False if it was a duplicate.
    """
    # ── Extract envelope fields ──────────────────────────────────
    satellite  = pkt.get("satellite_id", "") or pkt.get("satellite", "")
    norad_id   = pkt.get("NORAD", pkt.get("norad"))
    station    = pkt.get("ground_station", "") or pkt.get("station", pkt.get("stationName", ""))
    freq       = pkt.get("frequency")
    rssi       = pkt.get("rssi")
    snr        = pkt.get("snr")
    crc_error  = bool(pkt.get("crc_error", False))
    gs_time    = pkt.get("gs_time") or pkt.get("unix_GS_time")
    received_at = _reception_time(pkt)

    # ── Decode raw satellite bytes ───────────────────────────────
    raw_b64 = pkt.get("raw_data", "") or pkt.get("data", "")
    try:
        raw_bytes = base64.b64decode(raw_b64) if raw_b64 else None
    except Exception:
        raw_bytes = None
        log.warning("aws_sync: failed to base64-decode 'data' field for station=%s gs_time=%s",
                    station, gs_time)

    # ── Run beacon decoder ───────────────────────────────────────
    beacon_telemetry: dict = {}
    if raw_bytes and not crc_error:
        try:
            result = beacon_decoder.decode_beacon(raw_bytes)
            beacon_telemetry = result.get("telemetry", {})
        except Exception:
            log.debug("aws_sync: beacon decode failed", exc_info=True)

    # Beacon fields merged at top level so FSM/light consumers can find them
    # directly (e.g. FSM_state), and also preserved under _beacon.
    decoded_combined = {**pkt}
    if beacon_telemetry:
        decoded_combined.update(beacon_telemetry)   # top-level for consumers
        decoded_combined["_beacon"] = beacon_telemetry  # preserved copy

    readings: list[tuple[str, float, str]] = []
    if rssi is not None:
        readings.append(("rssi", float(rssi), "dBm"))
    if snr is not None:
        readings.append(("snr", float(snr), "dB"))
    if beacon_telemetry:
        readings.extend(beacon_decoder.extract_telemetry_readings(beacon_telemetry))

    # ── Insert packet + telemetry in one transaction (dedup by frame_hash) ──
    pkt_id, was_new = packet_store.store_packet_if_new(
        satellite=str(satellite),
        norad_id=int(norad_id) if norad_id is not None else None,
        station=str(station),
        frequency_mhz=float(freq) if freq is not None else None,
        rssi=float(rssi) if rssi is not None else None,
        snr=float(snr) if snr is not None else None,
        crc_error=crc_error,
        raw_frame=raw_bytes,
        decoded=decoded_combined,
        source="aws_sync",
        received_at=received_at,
        telemetry=readings,
    )

    if not was_new:
        log.debug("aws_sync: duplicate skipped  station=%s  gs_time=%s", station, gs_time)
        return False

    log.info(
        "aws_sync: stored packet #%d  sat=%s  station=%s  rssi=%s  snr=%s  beacon_fields=%d",
        pkt_id, satellite, station, rssi, snr, len(beacon_telemetry),
    )
    return True


# ──────────────────────────────────────────────
# Sync loop
# ──────────────────────────────────────────────

def _is_db_error(exc: Exception) -> bool:
    """True for failures of the local DB/disk rather than of one packet."""
    return (isinstance(exc, (sqlite3.OperationalError, OSError))
            or type(exc) is sqlite3.DatabaseError)      # e.g. "malformed"


def _is_settled(pkt_ts: str, cutoff: datetime) -> bool:
    try:
        dt = datetime.fromisoformat(pkt_ts.replace("Z", "+00:00"))
    except ValueError:
        return True            # can't tell; never let it block the feed
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= cutoff


def _sync_once() -> int:
    """Fetch one batch from AWS, store all packets, advance cursor.

    Returns the number of newly inserted packets.
    """
    since = max(_load_last_synced(), _mem_cursor)
    log.debug("aws_sync: fetching packets since %s", since)

    response = _fetch_packets(since)

    packets = response.get("packets", response if isinstance(response, list) else [])
    if not packets:
        log.debug("aws_sync: no new packets since %s", since)
        return 0

    inserted = 0
    latest_ts = since
    held = False
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=AWS_SYNC_SETTLE_S)

    # The cursor is a high-water mark, so process strictly in time order.
    for pkt in sorted(packets, key=lambda p: p.get("received_at") or ""):
        pkt_ts = pkt.get("received_at") or ""
        if pkt_ts and not _is_settled(pkt_ts, cutoff):
            break              # too fresh: take it (and anything after) next poll
        try:
            if _process_and_store(pkt):
                inserted += 1
            _failures.pop(pkt_ts, None)
        except Exception as exc:
            if _is_db_error(exc):
                log.error("aws_sync: local DB error storing packet received_at=%s: %s "
                          "— holding cursor until the DB recovers", pkt_ts, exc)
                held = True
                break
            attempts = _failures.get(pkt_ts, 0) + 1
            _failures[pkt_ts] = attempts
            log.exception("aws_sync: failed to store packet received_at=%s (attempt %d/%d)",
                          pkt_ts, attempts, _MAX_ATTEMPTS)
            if attempts < _MAX_ATTEMPTS or not _quarantine(pkt, repr(exc)):
                # Hold the cursor just before this packet so the next poll
                # retries it. Everything stored so far in this batch is safe.
                held = True
                break
            _failures.pop(pkt_ts, None)
            log.error("aws_sync: packet received_at=%s failed %d times — saved to %s and skipped",
                      pkt_ts, attempts, _QUARANTINE_FILE.name)

        # Only reached for packets that were stored, deduped, or quarantined.
        if pkt_ts and pkt_ts > latest_ts:
            latest_ts = pkt_ts

    if latest_ts != since:
        _save_last_synced(latest_ts)
        log.debug("aws_sync: cursor advanced to %s", latest_ts)

    log.info("aws_sync: sync complete — %d new / %d total%s", inserted, len(packets),
             " (holding cursor for retry)" if held else "")
    return inserted


def _sync_loop() -> None:
    """Poll AWS indefinitely, sleeping AWS_SYNC_INTERVAL between each cycle."""
    log.info("aws_sync: starting sync loop (interval=%ds, url=%s)", AWS_SYNC_INTERVAL, AWS_SYNC_URL)
    while True:
        try:
            _sync_once()
        except (URLError, OSError) as exc:
            log.warning("aws_sync: network error, will retry next interval: %s", exc)
        except Exception:
            log.exception("aws_sync: unexpected error in sync loop, will retry")
        time.sleep(AWS_SYNC_INTERVAL)


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def start_sync() -> threading.Thread | None:
    """Start the AWS sync background thread.

    Returns the Thread object, or None if AWS_SYNC_URL is not configured.
    """
    if not AWS_SYNC_URL:
        log.warning(
            "aws_sync: AWS_SYNC_URL not set — sync disabled. "
            "Set the AWS_SYNC_URL environment variable to enable."
        )
        return None

    t = threading.Thread(target=_sync_loop, name="aws-sync", daemon=True)
    t.start()
    log.info("aws_sync: background sync thread started")
    return t


# ──────────────────────────────────────────────
# Standalone entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    if not AWS_SYNC_URL:
        print(
            "ERROR: AWS_SYNC_URL environment variable not set.\n"
            "Example: export AWS_SYNC_URL=https://xxx.execute-api.us-east-1.amazonaws.com/prod"
        )
        raise SystemExit(1)

    print(f"Starting AWS sync (interval={AWS_SYNC_INTERVAL}s, url={AWS_SYNC_URL}) - Ctrl+C to stop")
    try:
        _sync_loop()
    except KeyboardInterrupt:
        print("\nStopped.")
