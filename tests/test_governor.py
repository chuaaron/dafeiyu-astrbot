"""V06b governor offline tests (stdlib unittest).

Covers: probe JSON guards (real VPS fixture), disabled-bucket skip,
ladder decisions incl. hysteresis and stale handling, Retry-After math.
Synthetic states below are FORGED for testing — clearly not real quota.
"""
import copy
import json
import math
import os
import sys
import unittest
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "deploy", "agy_quota_probe"))

import quota_probe as qp  # noqa: E402

FIXTURE = os.path.join(REPO, "tests", "fixtures", "agy_usage_probe.json")

CFG = {
    "stale_s": 600,
    "ladder": {
        "effort_low_below": 0.40,
        "reflection_pause_below": 0.25,
        "hard_floor_5h_below": 0.10,
        "hard_floor_weekly_below": 0.15,
        "hysteresis_margin": 0.05,
        "reflection_restore_above": 0.30,
    },
    "effort_inject": "low",
    "retry_after_floor_s": 30,
}


def state(frac5h, fracw=0.8, age_s=0, now=1000000.0,
          reset5h_off=7200, resetw_off=172800):
    """Build a quota_state.json-shaped dict (synthetic)."""
    return {
        "fetched_at": now - age_s,
        "groups": {
            "gemini": {
                "5h": {"frac": frac5h,
                       "reset_epoch": now + reset5h_off,
                       "reset_iso": "2026-11-05T20:16:37Z"},
                "weekly": {"frac": fracw,
                           "reset_epoch": now + resetw_off,
                           "reset_iso": "2026-11-07T00:37:07Z"},
            },
            "3p": {"5h": {"frac": 1.0, "reset_epoch": now + 3600,
                          "reset_iso": "2026-11-05T22:43:52Z"}},
        },
    }


NOW = 1000000.0


def decide(st, prev=None):
    return qp.decide(st, CFG, prev, now=NOW)


class ParseProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(FIXTURE, encoding="utf-8") as f:
            cls.payload = json.load(f)

    def test_real_fixture_parses(self):
        groups, skipped = qp.parse_probe_json(self.payload)
        self.assertEqual(
            round(groups["gemini"]["5h"]["frac"], 4), 0.9983)
        self.assertEqual(groups["gemini"]["5h"]["reset_iso"],
                         "2026-11-05T20:16:37Z")
        self.assertEqual(round(groups["gemini"]["weekly"]["frac"], 4),
                         0.8063)
        self.assertEqual(round(groups["3p"]["weekly"]["frac"], 4), 0.9328)
        self.assertEqual(skipped, [])

    def test_disabled_bucket_skipped(self):
        p = copy.deepcopy(self.payload)
        p["command"]["data"]["groups"][1]["buckets"].append(
            {"id": "gemini-argon-wtu-weekly", "window": "weekly",
             "disabled": True, "remaining_fraction": 0.5,
             "reset_time": "2026-11-12T00:00:00Z"})
        groups, skipped = qp.parse_probe_json(p)
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["reason"], "disabled")
        self.assertNotIn("argon", json.dumps(groups))

    def test_rejects_nonzero_total_tokens(self):
        p = copy.deepcopy(self.payload)
        p["usage"]["total_tokens"] = 8123
        with self.assertRaises(qp.ProbeReject):
            qp.parse_probe_json(p)

    def test_rejects_wrong_command_name(self):
        p = copy.deepcopy(self.payload)
        p["command"]["name"] = "prompt"
        with self.assertRaises(qp.ProbeReject):
            qp.parse_probe_json(p)

    def test_rejects_missing_gemini_5h(self):
        p = copy.deepcopy(self.payload)
        p["command"]["data"]["groups"][0]["buckets"] = [
            p["command"]["data"]["groups"][0]["buckets"][0]]
        with self.assertRaises(qp.ProbeReject):
            qp.parse_probe_json(p)

    def test_build_state_shape(self):
        groups, _ = qp.parse_probe_json(self.payload)
        st = qp.build_state(groups, [], now=NOW)
        self.assertEqual(st["fetched_at"], NOW)
        self.assertTrue(st["ok"])
        self.assertIn("gemini", st["groups"])


class Ladder(unittest.TestCase):
    def test_normal(self):
        v = decide(state(0.98))
        self.assertEqual(v["tier"], "normal")
        self.assertIsNone(v["inject_effort"])
        self.assertEqual(v["reflection"], "resume")
        self.assertFalse(v["stale"])

    def test_effort_low_entry(self):
        v = decide(state(0.35))
        self.assertEqual(v["tier"], "effort_low")
        self.assertEqual(v["inject_effort"], "low")
        self.assertEqual(v["reflection"], "resume")

    def test_reflection_pause_entry(self):
        v = decide(state(0.20))
        self.assertEqual(v["tier"], "reflection_pause")
        self.assertEqual(v["reflection"], "pause")

    def test_hard_floor_5h(self):
        v = decide(state(0.05))
        self.assertEqual(v["tier"], "hard_gate")
        self.assertEqual(v["reflection"], "pause")
        # reset is NOW+7200 -> ceil = 7200
        self.assertEqual(v["retry_after_s"], 7200)

    def test_hard_floor_weekly(self):
        v = decide(state(0.50, fracw=0.10))
        self.assertEqual(v["tier"], "hard_gate")
        # governed by the weekly window: NOW+172800
        self.assertEqual(v["retry_after_s"], 172800)

    def test_stale_never_429_always_low(self):
        v = decide(state(0.02, age_s=601))
        self.assertTrue(v["stale"])
        self.assertEqual(v["tier"], "effort_low")
        self.assertIsNone(v["retry_after_s"])
        self.assertEqual(v["inject_effort"], "low")

    def test_missing_state_is_stale_conservative(self):
        v = decide(None)
        self.assertTrue(v["stale"])
        self.assertEqual(v["tier"], "effort_low")

    def test_stale_holds_reflection_pause(self):
        # previously paused -> stale must not resume reflection
        v = decide(state(0.02, age_s=900), prev="reflection_pause")
        self.assertEqual(v["reflection"], "hold")
        v2 = decide(None, prev="hard_gate")
        self.assertEqual(v2["reflection"], "hold")
        # not paused before -> stale resumes (nothing to hold)
        self.assertEqual(decide(None, prev="normal")["reflection"], "resume")

    def test_effort_low_hysteresis(self):
        # entered at 0.39, climbs to 0.42 (< 0.40+0.05) -> still effort_low
        self.assertEqual(decide(state(0.39), prev="effort_low")["tier"],
                         "effort_low")
        # exits only above entry+margin
        self.assertEqual(decide(state(0.46), prev="effort_low")["tier"],
                         "normal")
        # fresh normal at 0.42 never enters
        self.assertEqual(decide(state(0.42), prev="normal")["tier"], "normal")

    def test_reflection_restore_uses_030(self):
        # paused at 0.20; climbs to 0.28 (below restore 0.30) -> stays paused
        self.assertEqual(
            decide(state(0.28), prev="reflection_pause")["tier"],
            "reflection_pause")
        # above 0.30 -> drops out of pause (0.30 < 0.40 -> effort_low hold
        # from prev pause/hard: 0.30 is not < 0.40? it IS < 0.40 -> effort_low)
        self.assertEqual(
            decide(state(0.31), prev="reflection_pause")["tier"],
            "effort_low")

    def test_hard_gate_hysteresis_hold_and_exit(self):
        # was hard_gate, now 0.12 (> entry 0.10 but < exit 0.15) -> hold
        self.assertEqual(decide(state(0.12), prev="hard_gate")["tier"],
                         "hard_gate")
        # exits at >= 0.15 (5h) AND >= 0.20 (weekly): lands in pause tier
        # because 0.15..: 0.16 < 0.25 -> reflection_pause held (prev hard,
        # 0.16 < restore 0.30)
        self.assertEqual(decide(state(0.16, fracw=0.5), prev="hard_gate")
                         ["tier"], "reflection_pause")
        # fully above all exits: 0.5/0.5 with prev hard -> normal
        self.assertEqual(decide(state(0.5, 0.5), prev="hard_gate")["tier"],
                         "normal")

    def test_weekly_floor_blocks_entry_to_hard_even_with_good_5h(self):
        v = decide(state(0.90, fracw=0.14))
        self.assertEqual(v["tier"], "hard_gate")


class RetryAfter(unittest.TestCase):
    def test_ceil_and_floor(self):
        st = state(0.05)
        st["groups"]["gemini"]["5h"]["reset_epoch"] = NOW + 7200.001
        v = decide(st)
        self.assertEqual(v["retry_after_s"], 7201)  # ceil(reset-now)

    def test_past_reset_floors_to_minimum(self):
        st = state(0.05)
        st["groups"]["gemini"]["5h"]["reset_epoch"] = NOW - 5
        v = decide(st)
        self.assertEqual(v["retry_after_s"], CFG["retry_after_floor_s"])

    def test_math_consistency(self):
        st = state(0.05, reset5h_off=7199.2)
        v = decide(st)
        self.assertEqual(v["retry_after_s"],
                         max(30, math.ceil(7199.2)))


class Stale(unittest.TestCase):
    def test_boundary(self):
        self.assertFalse(qp.is_stale(state(0.9, age_s=599), CFG, NOW))
        self.assertTrue(qp.is_stale(state(0.9, age_s=601), CFG, NOW))
        self.assertTrue(qp.is_stale({"fetched_at": "x"}, CFG, NOW))
        self.assertTrue(qp.is_stale({}, CFG, NOW))
        self.assertTrue(qp.is_stale(None, CFG, NOW))


class GovernorStateMerge(unittest.TestCase):
    def test_update_merges_keys(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "governor_state.json")
            qp.update_governor_state(path, lambda c: {**c, "tier": "normal"})
            qp.update_governor_state(
                path, lambda c: {**c, "reflection_paused": True})
            cur = qp.load_json(path)
            self.assertEqual(cur["tier"], "normal")
            self.assertTrue(cur["reflection_paused"])


if __name__ == "__main__":
    unittest.main()
