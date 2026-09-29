"""
Decoder tests against real HUCSat frames.

Run:
    python -m unittest test_beacon_decoder -v
"""

import base64
import unittest

import beacon_decoder

# Orient-state beacon (TinyGS download "packet (74).bin"): best_dir is a string.
ORIENT_FRAME = base64.b64decode(
    "//8AAA8AAAABTQAAAKIABkhVQ1NhdAAAAJUABm9yaWVudAAAAKwFPfd5oAAAAK0FPfd5oAAA"
    "AK4FPfKTGAAAAD8ABFRydWUAAABhBcHTMzAAAABgBUMSZmQAAABjBcCxmZgAAACnBUO4gAAA"
    "AADcABlbNjQ5LjAsIDcuMCwgMS4wLCAxNDQ4LjBdAAAAVwU/gAAAAAAAmgU9D974AAAAmwU6"
    "8DO0AAAAmAW9AXvcAAAA6AVBAfvkAAAAogAHK1ggQXhpcwAAAPAAEzIwMDAtMDEtMDMgMjE6"
    "MTQ6MjAAAADVBU5hxEQ="
)

# Detumble-state beacon, last packet from the webhook (2026-07-15T09:54:49Z):
# best_dir is numeric -1.
DETUMBLE_FRAME = base64.b64decode(
    "//8AAFMAAAABRQAAAKIABkhVQ1NhdAAAAJUACGRldHVtYmxlAAAArAU+DOOoAAAArQU+Axac"
    "AAAArgU9st5AAAAAPwAFRmFsc2UAAABhBcFJmZgAAABgBULjszAAAABjBcGUzMgAAACnBQAA"
    "AAAAAADcAAJbXQAAAFcFP4AAAAAAAJoFPFcuTAAAAJsFAAAAAAAAAJgFvRnhHAAAAOgFQQLx"
    "pAAAAKIFv4AAAAAAAPAAEzIwMDAtMDEtMTMgMjM6MzU6MjUAAADVBU5h+Wg="
)


class TestNameBestDirCollision(unittest.TestCase):
    def test_orient_frame_keeps_name_and_maps_best_dir(self):
        t = beacon_decoder.decode_beacon(ORIENT_FRAME)["telemetry"]
        self.assertEqual(t["name"], "HUCSat")
        self.assertEqual(t["FSM_best_dir"], 3)
        self.assertEqual(t["FSM_best_dir_label"], "+X Axis")
        self.assertEqual(t["FSM_state"], "orient")

    def test_orient_best_dir_reaches_telemetry(self):
        t = beacon_decoder.decode_beacon(ORIENT_FRAME)["telemetry"]
        readings = dict((k, v) for k, v, _ in beacon_decoder.extract_telemetry_readings(t))
        self.assertEqual(readings["FSM_best_dir"], 3.0)

    def test_detumble_frame_unchanged(self):
        t = beacon_decoder.decode_beacon(DETUMBLE_FRAME)["telemetry"]
        self.assertEqual(t["name"], "HUCSat")
        self.assertEqual(t["FSM_best_dir"], -1)
        self.assertNotIn("FSM_best_dir_label", t)
        self.assertEqual(t["FSM_state"], "detumble")

    def test_unknown_label_kept_but_not_numeric(self):
        t = {"FSM_best_dir": "+Z Axis"}
        beacon_decoder._normalize_best_dir(t)
        self.assertEqual(t, {"FSM_best_dir": "+Z Axis", "FSM_best_dir_label": "+Z Axis"})

    def test_angular_velocity_reported_in_deg_per_s(self):
        t = beacon_decoder.decode_beacon(ORIENT_FRAME)["telemetry"]
        self.assertAlmostEqual(t["FSM_av_0"], 2.0125, places=3)    # raw 0.0351 rad/s


if __name__ == "__main__":
    unittest.main(verbosity=2)
