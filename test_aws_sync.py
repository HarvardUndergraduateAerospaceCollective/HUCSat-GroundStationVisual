"""
Tests for aws_sync.py cursor safety, cursor URL-encoding, late-delivery
timestamps, the settle window, and packet+telemetry atomicity.  Uses a temp DB, temp state file and a fake AWS endpoint — never
touches mission_data.db or the network.

Run:
    python -m unittest test_aws_sync -v
"""

import base64
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import aws_sync
import packet_store

T0 = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)   # well past the settle window


def ts(seconds: float) -> str:
    """AWS-style received_at, like ingest/app.py writes."""
    return (T0 + timedelta(seconds=seconds)).isoformat()


def mk(pid: str, received_at: str, gs_time=None) -> dict:
    pkt = {
        "satellite_id": "CUBESAT-1",
        "received_at": received_at,
        "packet_id": pid,
        "raw_data": base64.b64encode(f"frame-{pid}-".encode() * 8).decode(),
        "crc_error": False,
    }
    if gs_time is not None:
        pkt["gs_time"] = gs_time
    return pkt


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self._reset_conn()
        patches = [
            mock.patch.object(packet_store, "_DB_PATH", d / "test.db"),
            mock.patch.object(aws_sync, "_STATE_FILE", d / "state.json"),
            mock.patch.object(aws_sync, "_QUARANTINE_FILE", d / "quarantine.jsonl"),
            mock.patch.object(aws_sync, "_failures", {}),
            mock.patch.object(aws_sync, "_mem_cursor", aws_sync._EPOCH),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.aws = []          # what the fake DynamoDB holds
        self.batch_order = None
        self.fail_counts = {}  # packet_id -> remaining failures
        self.fail_exc = lambda: sqlite3.OperationalError("database is locked")
        self._orig_store = packet_store.store_packet_if_new
        fetch = mock.patch.object(aws_sync, "_fetch_packets", self._fake_fetch)
        store = mock.patch.object(packet_store, "store_packet_if_new", self._flaky_store)
        for p in (fetch, store):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self._reset_conn()
        self.tmp.cleanup()

    @staticmethod
    def _reset_conn():
        conn = getattr(packet_store._local, "conn", None)
        if conn is not None:
            conn.close()
            del packet_store._local.conn

    def _fake_fetch(self, since):
        # Same contract as the sync Lambda: received_at > since, ascending.
        items = sorted((p for p in self.aws if p["received_at"] > since),
                       key=lambda p: p["received_at"])[:aws_sync._PAGE_LIMIT]
        if self.batch_order:
            items = self.batch_order(items)
        return {"packets": items}

    def _flaky_store(self, **kw):
        pid = kw["decoded"].get("packet_id")
        if self.fail_counts.get(pid, 0) > 0:
            self.fail_counts[pid] -= 1
            raise self.fail_exc()
        return self._orig_store(**kw)

    def stored(self) -> dict:
        rows = packet_store._get_conn().execute(
            "SELECT received_at, decoded_json FROM packets").fetchall()
        return {json.loads(r["decoded_json"])["packet_id"]: r["received_at"] for r in rows}

    def cursor(self) -> str:
        return aws_sync._load_last_synced()

    def telemetry_counts(self) -> dict:
        rows = packet_store._get_conn().execute(
            "SELECT p.decoded_json, COUNT(t.id) AS n FROM packets p "
            "LEFT JOIN telemetry t ON t.packet_id = p.id GROUP BY p.id").fetchall()
        return {json.loads(r["decoded_json"])["packet_id"]: r["n"] for r in rows}


class TestCursor(SyncTestCase):
    def test_all_packets_synced_and_cursor_at_last(self):
        self.aws = [mk("p1", ts(0)), mk("p2", ts(10)), mk("p3", ts(20))]
        self.assertEqual(aws_sync._sync_once(), 3)
        self.assertEqual(set(self.stored()), {"p1", "p2", "p3"})
        self.assertEqual(self.cursor(), ts(20))

    def test_failed_store_holds_cursor_then_retries(self):
        self.aws = [mk("p1", ts(0)), mk("p2", ts(10)), mk("p3", ts(20))]
        self.fail_counts = {"p2": 1}
        aws_sync._sync_once()
        self.assertEqual(set(self.stored()), {"p1"})
        self.assertEqual(self.cursor(), ts(0), "cursor must not pass the failed packet")
        aws_sync._sync_once()
        self.assertEqual(set(self.stored()), {"p1", "p2", "p3"})
        self.assertEqual(self.cursor(), ts(20))

    def test_unsorted_batch_still_holds_before_failure(self):
        self.aws = [mk("p1", ts(0)), mk("p2", ts(10)), mk("p3", ts(20))]
        self.batch_order = lambda items: list(reversed(items))
        self.fail_counts = {"p2": 1}
        aws_sync._sync_once()
        self.assertEqual(self.cursor(), ts(0))
        aws_sync._sync_once()
        self.assertEqual(set(self.stored()), {"p1", "p2", "p3"})

    def test_poison_packet_quarantined_after_max_attempts(self):
        self.aws = [mk("p1", ts(0)), mk("p2", ts(10)), mk("p3", ts(20))]
        self.fail_counts = {"p2": 99}
        self.fail_exc = lambda: ValueError("bad field in packet")
        for _ in range(aws_sync._MAX_ATTEMPTS - 1):
            aws_sync._sync_once()
            self.assertEqual(self.cursor(), ts(0))
            self.assertNotIn("p3", self.stored())
        aws_sync._sync_once()
        lines = aws_sync._QUARANTINE_FILE.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["packet"]["packet_id"], "p2")
        self.assertEqual(set(self.stored()), {"p1", "p3"})
        self.assertEqual(self.cursor(), ts(20))

    def test_unwritable_quarantine_never_skips(self):
        self.aws = [mk("p1", ts(0)), mk("p2", ts(10)), mk("p3", ts(20))]
        self.fail_counts = {"p2": 99}
        self.fail_exc = lambda: ValueError("bad field in packet")
        aws_sync._QUARANTINE_FILE.mkdir()   # opening a directory for append fails
        for _ in range(aws_sync._MAX_ATTEMPTS + 2):
            aws_sync._sync_once()
        self.assertEqual(self.cursor(), ts(0))
        self.assertEqual(set(self.stored()), {"p1"})

    def test_db_error_holds_but_never_quarantines(self):
        self.aws = [mk("p1", ts(0)), mk("p2", ts(10)), mk("p3", ts(20))]
        self.fail_counts = {"p2": 3 * aws_sync._MAX_ATTEMPTS}   # sick DB, not a bad packet
        for _ in range(3 * aws_sync._MAX_ATTEMPTS):
            aws_sync._sync_once()
        self.assertEqual(self.cursor(), ts(0))
        self.assertFalse(aws_sync._QUARANTINE_FILE.exists())
        aws_sync._sync_once()                                    # DB recovered
        self.assertEqual(set(self.stored()), {"p1", "p2", "p3"})

    def test_telemetry_committed_with_packet(self):
        self.aws = [dict(mk(p, ts(i * 10)), rssi=-100.0) for i, p in enumerate(("p1", "p2", "p3"))]
        real = packet_store._insert_telemetry_rows
        calls = {"n": 0}

        def fail_second(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise sqlite3.OperationalError("disk I/O error")
            return real(*a, **kw)

        with mock.patch.object(packet_store, "_insert_telemetry_rows", fail_second):
            aws_sync._sync_once()
            self.assertEqual(self.telemetry_counts(), {"p1": 1}, "p2 must roll back whole")
            aws_sync._sync_once()
        self.assertEqual(self.telemetry_counts(), {"p1": 1, "p2": 1, "p3": 1})

    def test_unwritable_state_file_still_progresses(self):
        self.aws = [mk(f"p{i}", ts(i)) for i in range(5)]
        aws_sync._STATE_FILE.mkdir()          # can't be read or replaced
        with mock.patch.object(aws_sync, "_PAGE_LIMIT", 2):
            for _ in range(3):
                aws_sync._sync_once()
        self.assertEqual(len(self.stored()), 5)

    def test_fresh_packets_wait_for_settle_window(self):
        now = datetime.now(timezone.utc)
        old = mk("old", (now - timedelta(seconds=aws_sync.AWS_SYNC_SETTLE_S + 30)).isoformat())
        fresh = mk("fresh", (now - timedelta(seconds=5)).isoformat())
        self.aws = [old, fresh]
        aws_sync._sync_once()
        self.assertEqual(set(self.stored()), {"old"})
        self.assertEqual(self.cursor(), old["received_at"])
        with mock.patch.object(aws_sync, "AWS_SYNC_SETTLE_S", 0):
            aws_sync._sync_once()
        self.assertEqual(set(self.stored()), {"old", "fresh"})

    def test_duplicate_does_not_hold_cursor(self):
        self.aws = [mk("p1", ts(0))]
        aws_sync._sync_once()
        dup = mk("p1-again", ts(5))
        dup["raw_data"] = self.aws[0]["raw_data"]     # same frame bytes
        self.aws += [dup, mk("p2", ts(10))]
        aws_sync._sync_once()
        self.assertEqual(set(self.stored()), {"p1", "p2"})
        self.assertEqual(self.cursor(), ts(10))


class TestPacketStore(SyncTestCase):
    def test_frameless_packet_stores_telemetry_too(self):
        pid, new = packet_store.store_packet_if_new(
            satellite="S", raw_frame=None, decoded={"packet_id": "nf"}, source="t",
            telemetry=[("rssi", -99.0, "dBm")])
        self.assertTrue(new)
        self.assertEqual(self.telemetry_counts(), {"nf": 1})


class TestUrlEncoding(unittest.TestCase):
    def test_since_round_trips_through_query_string(self):
        since = "2026-07-15T09:54:49.502354+00:00"
        seen = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"packets": []}'

        def fake_urlopen(req, timeout):
            seen["url"] = req.full_url
            return Resp()

        with mock.patch.object(aws_sync, "AWS_SYNC_URL", "https://example.test/prod"), \
             mock.patch.object(aws_sync, "urlopen", fake_urlopen):
            aws_sync._fetch_packets(since)
        self.assertNotIn("+", seen["url"])
        self.assertEqual(parse_qs(urlparse(seen["url"]).query)["since"], [since])


class TestReceptionTime(SyncTestCase):
    def test_live_packet_keeps_aws_time(self):
        gs = int((T0 - timedelta(seconds=20)).timestamp())
        self.aws = [mk("p1", ts(0), gs_time=gs)]
        aws_sync._sync_once()
        self.assertEqual(self.stored()["p1"], ts(0))

    def test_replayed_packet_stores_original_time(self):
        original = T0 - timedelta(days=3)
        self.aws = [mk("p1", ts(0), gs_time=int(original.timestamp()))]
        aws_sync._sync_once()
        self.assertEqual(self.stored()["p1"], original.isoformat(timespec="microseconds"))
        self.assertEqual(self.cursor(), ts(0), "cursor still follows AWS received_at")
        row = packet_store._get_conn().execute("SELECT decoded_json FROM packets").fetchone()
        self.assertEqual(json.loads(row["decoded_json"])["received_at"], ts(0))

    def test_gs_time_in_milliseconds(self):
        original = T0 - timedelta(hours=2)
        self.aws = [mk("p1", ts(0), gs_time=int(original.timestamp() * 1000))]
        aws_sync._sync_once()
        self.assertEqual(self.stored()["p1"], original.isoformat(timespec="microseconds"))

    def test_implausible_or_missing_gs_time_keeps_aws_time(self):
        self.aws = [mk("p1", ts(0), gs_time=946684800),      # beacon RTC epoch 2000
                    mk("p2", ts(10), gs_time="garbage"),
                    mk("p3", ts(20))]
        aws_sync._sync_once()
        self.assertEqual(self.stored(), {"p1": ts(0), "p2": ts(10), "p3": ts(20)})


if __name__ == "__main__":
    unittest.main(verbosity=2)
