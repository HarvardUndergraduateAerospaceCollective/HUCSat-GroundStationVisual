"""Tests for flight-computer uptime decoding (mission_time.fc_uptime_seconds)."""

import unittest

from mission_time import fc_uptime_seconds


class FcUptimeTest(unittest.TestCase):
    def test_clock_string_is_seconds_since_boot(self):
        self.assertEqual(fc_uptime_seconds("2000-01-01 00:03:33", 946684928.0), 213)

    def test_long_boot(self):
        # Boot 3 (2026-09-26 04:09 UTC), last beacon before the battery fault.
        self.assertEqual(fc_uptime_seconds("2000-01-07 20:41:11", 947277312.0), 6 * 86400 + 74471)

    def test_clock_string_wins_when_fields_disagree(self):
        # July beacons: the uptime field ran ~30 min behind the clock string.
        self.assertEqual(fc_uptime_seconds("2000-01-02 00:00:00", 946769000.0), 86400)

    def test_uptime_field_fallback(self):
        self.assertEqual(fc_uptime_seconds(None, 946684928.0), 128)

    def test_truncated_reading_just_after_boot_is_zero_not_garbage(self):
        # 42 s after boot the 256 s truncation lands just below the epoch; the
        # old anchor turned this into 946684672 s (~262968 h) on the dashboard.
        self.assertEqual(fc_uptime_seconds("", 946684672.0), 0)

    def test_plain_seconds_pass_through(self):
        self.assertEqual(fc_uptime_seconds(None, 518144), 518144)

    def test_unknown(self):
        self.assertIsNone(fc_uptime_seconds(None, None))
        self.assertIsNone(fc_uptime_seconds("not a time", "n/a"))
        self.assertIsNone(fc_uptime_seconds(None, -5))


if __name__ == "__main__":
    unittest.main()
