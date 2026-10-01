"""Tests for the best-direction panel's SAT slice and the sunlight model behind it."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import packet_store
import panel_best_dir
import visualizer

# HUCSat TLE (epoch 2026-09-30 13:28 UTC) and TinyGS's own sunLit flag for
# packets received that day: (unix time, sunLit).
TLE = (
    "1 69794U 98067YK  26273.56170678  .00022218  00000+0  33507-3 0  9990",
    "2 69794  51.6297 136.7249 0008522 213.8190 146.2258 15.54651920 14012",
)
TINYGS_SUNLIT = [
    (1790741057, True),
    (1790774944, True),
    (1790792589, True),
    (1790808066, True),
    (1790743990, False),
    (1790772599, False),
    (1790778867, False),
]


class InSunlightTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(visualizer, "_sgp4_sat", visualizer._Satrec.twoline2rv(*TLE))
        p.start()
        self.addCleanup(p.stop)

    def test_matches_tinygs_sunlit_flag(self):
        flags = visualizer.in_sunlight([t for t, _ in TINYGS_SUNLIT])
        self.assertEqual([bool(f) for f in flags], [s for _, s in TINYGS_SUNLIT])

    def test_eclipse_fraction_is_physical(self):
        t = 1790726400 + np.arange(0, 86400, 30)       # 2026-09-30, every 30 s
        shadow = 1 - visualizer.in_sunlight(t).mean()
        self.assertGreater(shadow, 0.30)
        self.assertLess(shadow, 0.42)

    def test_no_tle_returns_none(self):
        with mock.patch.object(visualizer, "_sgp4_sat", None):
            self.assertIsNone(visualizer.in_sunlight([1790741057]))


SUN_TIME = "2026-09-30T12:13:02+00:00"     # TinyGS: sunLit
SHADOW_TIME = "2026-09-30T12:49:59+00:00"  # TinyGS: in shadow


class BestDirPanelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._reset_conn()
        for p in (
            mock.patch.object(packet_store, "_DB_PATH", Path(self.tmp.name) / "test.db"),
            mock.patch.object(panel_best_dir, "_sunlit_cache", {}),
            mock.patch.object(panel_best_dir, "_packets", []),
            mock.patch.object(panel_best_dir, "_seen_id", 0),
            mock.patch.object(visualizer, "_sgp4_sat", visualizer._Satrec.twoline2rv(*TLE)),
        ):
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

    def _store(self, received_at, best_dir, pan_light=None):
        decoded = {"FSM_best_dir": best_dir}
        if pan_light is not None:
            decoded["FSM_pan_light"] = pan_light
        packet_store.store_packet(decoded=decoded, received_at=received_at, source="test")

    def test_daylight_zeros_go_to_sat_slice(self):
        self._store("2026-07-08T07:04:28+00:00", -1)                         # detumble: N/A
        self._store(SHADOW_TIME, 3, "[0.0, 0.0, 0.0, 0.0]")                  # real eclipse: +X
        self._store("2026-09-30T12:13:00+00:00", 2, "[0.0, 5.0, 264.0, 136.0]")  # lit: −Y
        self._store(SUN_TIME, 3, "[0.0, 0.0, 0.0, 0.0]")                     # daylight zeros: SAT
        d = panel_best_dir.compute()
        self.assertEqual(d["labels"], ["+Y", "−X", "−Y", "+X", "SAT"])
        self.assertEqual(d["counts"], [0, 0, 1, 1, 1])
        self.assertEqual(d["colors"][-1], panel_best_dir.SAT_COLOR)
        self.assertEqual((d["with_direction"], d["saturated"], d["total"]), (2, 1, 4))
        self.assertEqual(d["latest_label"], "+X")   # last non-saturated packet

    def test_refresh_reads_only_new_packets(self):
        self._store(SUN_TIME, 3, "[0.0, 0.0, 0.0, 0.0]")
        self.assertEqual(panel_best_dir.compute()["counts"], [0, 0, 0, 0, 1])
        self._store(SHADOW_TIME, 1, "[0.0, 0.0, 0.0, 0.0]")
        with mock.patch.object(packet_store, "decoded_fields", wraps=packet_store.decoded_fields) as q:
            d = panel_best_dir.compute()
        self.assertEqual(q.call_args.kwargs["after_id"], 1)
        self.assertEqual(d["counts"], [0, 1, 0, 0, 1])
        self.assertEqual(panel_best_dir.compute()["total"], 2)   # no double counting

    def test_late_packet_does_not_become_latest(self):
        self._store(SHADOW_TIME, 1, "[0.0, 0.0, 0.0, 0.0]")
        self._store("2026-09-30T12:13:00+00:00", 2, "[0.0, 5.0, 264.0, 136.0]")  # older, stored later
        self.assertEqual(panel_best_dir.compute()["latest_label"], "−X")

    def test_without_tle_nothing_is_marked_sat(self):
        self._store(SUN_TIME, 3, "[0.0, 0.0, 0.0, 0.0]")
        with mock.patch.object(visualizer, "_sgp4_sat", None):
            d = panel_best_dir.compute()
        self.assertEqual(d["counts"], [0, 0, 0, 1, 0])
        self.assertEqual(panel_best_dir._sunlit_cache, {})   # retried once a TLE loads
        self.assertEqual(panel_best_dir.compute()["counts"], [0, 0, 0, 0, 1])


if __name__ == "__main__":
    unittest.main()
