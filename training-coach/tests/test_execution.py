"""Offline tests for interval execution measurement.

Run with:  PYTHONPATH=app python3 -m unittest discover -s tests -v

Imports `memory` only (stdlib), so these run where openai and requests are absent.

The property that matters most here is the refusal to accuse: when lap pressing
goes wrong, the result must be "unclear" with no rep count, not a confident
"you missed two reps".
"""
import os
import tempfile
import unittest

from memory import (
    MemoryStore,
    _duration_seconds,
    _exec_tag,
    _power_target,
    compare_execution,
    execution_sentence,
    parse_planned_workout,
)

PLAN_TEXT = """Warmup
- 10m 50-65%
- 3m 75-85%
- Press lap 1m 50-60%

Main set 3x
- Sweetspot 15m 88-92%
- Recovery 5m 50-60%
- Recovery Press lap 1m 50-60%

Cooldown
- Cooldown Press lap 10m 50-65%"""


def plan():
    return parse_planned_workout(PLAN_TEXT)


def iv(duration, power, pct=None, group_id=1, type_="WORK", index=None):
    return {"type": type_, "duration_seconds": duration, "avg_power": power,
            "intensity_pct": pct, "group_id": group_id, "start_index": index}


def payload(intervals, count=None):
    groups = [{"group_id": 1, "count": count}] if count else []
    return {"activity_id": "i1", "interval_count": len(intervals),
            "intervals": intervals, "groups": groups}


class TestDurationParsing(unittest.TestCase):
    def test_every_documented_form(self):
        cases = {"Sweetspot 15m 88-92%": 900, "1m30 110%": 90, "1h30m 70%": 5400,
                 "10:00 80%": 600, "45sec 130%": 45, "10min 75%": 600, "30s 120%": 30}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(_duration_seconds(text)[0], expected)

    def test_cadence_is_not_a_duration(self):
        """"90 rpm" read as 90 minutes would make every rep look cut short."""
        self.assertIsNone(_duration_seconds("90 rpm")[0])

    def test_ambiguous_forms_are_refused(self):
        for text in ("20-30m 70%", "2x20 90%"):
            with self.subTest(text=text):
                self.assertIsNone(_duration_seconds(text)[0])


class TestTargetParsing(unittest.TestCase):
    def test_power_bands(self):
        self.assertEqual(_power_target("88-92%")["low"], 88)
        self.assertEqual(_power_target("76-80% FTP")["high"], 80)
        self.assertEqual(_power_target("100-140w")["unit"], "w")
        self.assertTrue(_power_target("Ramp 60-80%")["ramp"])

    def test_heart_rate_and_cadence_are_not_power(self):
        """A 60% HR step read as 60% FTP becomes a missed sweetspot rep."""
        for text in ("60% HR", "100% LTHR", "90 rpm", "3m 120% HR 95rpm"):
            with self.subTest(text=text):
                self.assertIsNone(_power_target(text))


class TestWorkoutParser(unittest.TestCase):
    def test_the_documented_example(self):
        parsed = plan()
        self.assertEqual(parsed["reps_planned"], 3)
        self.assertEqual(len(parsed["sets"]), 1)
        self.assertEqual(parsed["sets"][0]["work_seconds"], 900)
        self.assertEqual(parsed["sets"][0]["band"], {"unit": "pct", "low": 88,
                                                     "high": 92, "ramp": False})

    def test_warmup_cooldown_recovery_and_press_lap_are_excluded(self):
        self.assertEqual(plan()["sets"][0]["reps"], 3)
        self.assertEqual(plan()["sets"][0]["steps"], 1)

    def test_two_sets(self):
        parsed = parse_planned_workout(
            "Set 1 4x\n- VO2max 3m 118-122%\n- Recovery 3m 50-60%\n\n"
            "Set 2 2x\n- Threshold 8m 98-102%\n- Recovery 4m 50-60%")
        self.assertEqual(parsed["reps_planned"], 6)
        self.assertEqual(len(parsed["sets"]), 2)

    def test_refuses_what_it_does_not_understand(self):
        """A guess here becomes "you missed two reps" months later."""
        for bad in (None, "", "   ", 42, "Easy spin for an hour, keep it chill",
                    "Main set 2x20\n- 90%", "- Ride steady\n- Coffee\n- Home\n- 10m 80%"):
            with self.subTest(bad=repr(bad)[:30]):
                self.assertIsNone(parse_planned_workout(bad))

    def test_endurance_plan_is_recognised_but_has_no_reps(self):
        """Distinct from None: the format was understood, there is just nothing
        rep-shaped to judge."""
        parsed = parse_planned_workout("Endurance\n- 90m 60-70%")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["sets"], [])
        self.assertEqual(parsed["reps_planned"], 0)

    def test_over_unders_are_marked_ambiguous(self):
        parsed = parse_planned_workout(
            "Main set 3x\n- Over 2m 105%\n- Under 3m 88%\n- Recovery 5m 50%")
        self.assertTrue(parsed["sets"][0]["ambiguous"])


class TestComparison(unittest.TestCase):
    def test_clean_session(self):
        got = compare_execution(plan(), payload(
            [iv(900, 225, 90), iv(898, 223, 89), iv(901, 221, 88)], count=3))
        self.assertEqual(got["basis"], "plan")
        self.assertEqual(got["match"], "group")
        self.assertEqual((got["reps_done"], got["reps_planned"]), (3, 3))
        self.assertEqual(got["target_hit"], 3)
        self.assertEqual(got["short_reps"], 0)

    def test_a_genuine_shortfall_is_reported(self):
        got = compare_execution(plan(), payload([iv(900, 225, 90), iv(900, 223, 89)]))
        self.assertEqual((got["reps_done"], got["reps_planned"]), (2, 3))

    def test_merged_reps_make_no_accusation(self):
        """Same work, fewer pieces: a lap pressed late, not a missed rep."""
        got = compare_execution(plan(), payload([iv(1800, 224, 89), iv(900, 222, 88)]))
        self.assertEqual(got["basis"], "unclear")
        self.assertEqual(got["reason"], "split_or_merged")
        self.assertNotIn("reps_done", got)

    def test_split_reps_make_no_accusation(self):
        got = compare_execution(plan(), payload(
            [iv(450, 225, 90), iv(450, 224, 90), iv(900, 223, 89), iv(900, 221, 88)]))
        self.assertEqual(got["basis"], "unclear")
        self.assertNotIn("reps_done", got)

    def test_a_warmup_surge_is_not_a_rep(self):
        got = compare_execution(plan(), payload(
            [iv(40, 340, 135), iv(900, 225, 90), iv(898, 223, 89), iv(900, 221, 88)]))
        self.assertEqual(got["reps_done"], 3)

    def test_fade_and_under_target(self):
        faded = compare_execution(plan(), payload(
            [iv(900, 240, 96), iv(900, 225, 90), iv(900, 209, 84)], count=3))
        self.assertLess(faded["fade_pct"], -10)
        low = compare_execution(plan(), payload(
            [iv(900, 200, 80), iv(900, 198, 79), iv(900, 196, 78)], count=3))
        self.assertEqual(low["target_hit"], 0)
        self.assertLess(low["target_rel"], 95)   # direction survives, not just the miss

    def test_a_cut_short_rep(self):
        got = compare_execution(plan(), payload(
            [iv(900, 225, 90), iv(900, 223, 89), iv(600, 221, 88)], count=3))
        self.assertEqual(got["short_reps"], 1)

    def test_stale_ftp_does_not_fail_an_old_session(self):
        """intensity_pct carries the FTP in force at the time; today's FTP would
        score these 0/3 instead of 3/3."""
        got = compare_execution(plan(), payload(
            [iv(900, 205, 90), iv(900, 204, 89), iv(900, 203, 88)], count=3), ftp=250)
        self.assertEqual(got["target_hit"], 3)

    def test_no_plan_yields_actual_side_only(self):
        got = compare_execution(None, payload([iv(600, 250), iv(620, 248)]))
        self.assertEqual(got["basis"], "actual_only")
        self.assertNotIn("reps_planned", got)

    def test_empty_payload(self):
        got = compare_execution(plan(), {"activity_id": "i1", "interval_count": 0,
                                         "message": "No interval data found."})
        self.assertEqual(got["basis"], "none")

    def test_erg_is_flagged(self):
        got = compare_execution(plan(), payload([iv(900, 225, 90)] * 3, count=3), trainer=True)
        self.assertTrue(got["erg"])


class TestRendering(unittest.TestCase):
    def test_only_an_established_record_is_tagged(self):
        self.assertIn("ex 3/3", _exec_tag({"basis": "plan", "reps_done": 3, "reps_planned": 3}))
        for record in ({"basis": "unclear"}, {"basis": "actual_only"}, {"basis": "none"}, None):
            self.assertEqual(_exec_tag(record), "")

    def test_the_sentence_refuses_to_guess(self):
        unclear = execution_sentence({"basis": "unclear", "reason": "split_or_merged"})
        self.assertIn("not established", unclear)
        self.assertEqual(execution_sentence({"basis": "actual_only"}), "")
        self.assertEqual(execution_sentence(None), "")

    def test_the_sentence_states_the_numbers(self):
        text = execution_sentence({"basis": "plan", "reps_done": 2, "reps_planned": 3,
                                   "target_hit": 1, "target_rel": 94, "fade_pct": -6.1})
        self.assertIn("2 of 3 reps", text)
        self.assertIn("1 on target", text)

    def test_trainer_sessions_say_target_hits_are_the_trainers_doing(self):
        text = execution_sentence({"basis": "plan", "reps_done": 3, "reps_planned": 3,
                                   "erg": True})
        self.assertIn("trainer", text)


class TestStore(unittest.TestCase):
    def store(self):
        return MemoryStore(os.path.join(tempfile.mkdtemp(), "m.json"))

    def _activity(self, **kw):
        base = {"id": "i1", "date": "2026-11-10", "type": "Ride",
                "duration_seconds": 5400, "tss": 98,
                "start_date_local": "2026-11-10T16:00:00"}
        base.update(kw)
        return base

    def test_execution_survives_a_numeric_upsert(self):
        """The 120-second loop must not wipe a measurement."""
        s = self.store()
        s.upsert_session(self._activity())
        s.set_execution("i1", {"basis": "plan", "reps_done": 3, "reps_planned": 3})
        for _ in range(5):
            s.upsert_session(self._activity())
        self.assertEqual(s.execution_for("i1")["reps_done"], 3)

    def test_identical_measurements_write_once(self):
        s = self.store()
        record = {"basis": "plan", "reps_done": 3, "reps_planned": 3}
        self.assertTrue(s.set_execution("i1", record))
        self.assertFalse(s.set_execution("i1", dict(record)))

    def test_the_fetch_guard(self):
        s = self.store()
        s.upsert_session(self._activity())
        self.assertTrue(s.needs_execution("i1", tss=98))
        s.set_execution("i1", {"basis": "plan", "tss": 98})
        self.assertFalse(s.needs_execution("i1", tss=98))
        self.assertTrue(s.needs_execution("i1", tss=140))   # re-upload self-heals

    def test_failures_stop_retrying(self):
        s = self.store()
        for _ in range(3):
            s.mark_execution_attempt("i1")
        self.assertFalse(s.needs_execution("i1"))
        self.assertEqual(s.execution_for("i1")["basis"], "none")

    def test_plans_are_recorded_and_retrievable(self):
        s = self.store()
        s.record_plans([{"id": 77, "date": "2026-11-10", "category": "WORKOUT",
                         "description": PLAN_TEXT}])
        self.assertEqual(s.plan_for_event(77)["reps_planned"], 3)
        self.assertEqual(s.plan_for_event("77")["reps_planned"], 3)
        self.assertIsNone(s.plan_for_event(None))

    def test_the_queue_skips_group_rides_and_done_sessions(self):
        s = self.store()
        s.upsert_session(self._activity(id="i1"))
        s.upsert_session(self._activity(id="i2", group_ride=True))
        s.upsert_session(self._activity(id="i3"))
        s.set_execution("i3", {"basis": "plan"})
        self.assertEqual(set(s.sessions_needing_execution(10)), {"i1"})


if __name__ == "__main__":
    unittest.main()
