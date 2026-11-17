"""Offline tests for the persistent training memory.

Run with:  PYTHONPATH=app python3 -m unittest discover -s tests -v

memory.py is stdlib-only by design, so these run even where openai and requests
are missing — which is the normal state of a fresh container.
"""
import json
import os
import tempfile
import unittest
from datetime import date, timedelta

from memory import (
    MAX_CHARS,
    MEMORY_USAGE_INSTRUCTIONS,
    MemoryStore,
    extract_memo,
    is_hard_session,
    judge_planned,
    monday_of,
    recap_target_monday,
    session_record_from_activity,
)


def activity(aid="i1", day="2026-11-10", **kw):
    base = {"id": aid, "date": day, "type": "Ride", "name": "SS 3x15",
            "duration_seconds": 5520, "tss": 98, "intensity": 0.78,
            "rpe": 6, "feel": 2, "power_zone_mins": [10, 30, 20, 15, 8, 0, 0],
            "start_date_local": f"{day}T16:32:00"}
    base.update(kw)
    return base


class CountingStore(MemoryStore):
    """Counts disk writes — the point of the change gate is that there are few."""

    def __init__(self, path):
        self.writes = 0
        super().__init__(path)

    def _save(self):
        self.writes += 1
        super()._save()


def store(**kw):
    d = tempfile.mkdtemp()
    return CountingStore(os.path.join(d, "m.json"))


class TestIdempotency(unittest.TestCase):
    def test_repeated_upserts_write_once(self):
        """The loop re-sees each activity every 120s for 14 days. Without the
        change gate the file is rewritten forever, which on an SD-card install
        costs more than the tokens this feature saves."""
        s = store()
        self.assertTrue(s.upsert_session(activity()))
        for _ in range(50):
            self.assertFalse(s.upsert_session(activity()))
        self.assertEqual(s.writes, 1)

    def test_float_noise_does_not_defeat_the_gate(self):
        s = store()
        s.upsert_session(activity())
        self.assertFalse(s.upsert_session(activity(tss=98.0000001, intensity=0.7800001)))
        self.assertEqual(s.writes, 1)

    def test_late_rpe_updates(self):
        s = store()
        s.upsert_session(activity(rpe=None, feel=None))
        self.assertTrue(s.upsert_session(activity(rpe=7, feel=3)))
        self.assertEqual(s._data["sessions"]["i1"]["rpe"], 7)

    def test_numeric_refresh_never_clobbers_the_note(self):
        s = store()
        s.upsert_session(activity())
        s.set_note("i1", "Faded on the last climb.")
        before = s.writes
        for _ in range(5):
            s.upsert_session(activity())
        self.assertEqual(s._data["sessions"]["i1"]["note"], "Faded on the last climb.")
        self.assertEqual(s.writes, before)

    def test_activity_without_id_or_date_is_rejected(self):
        self.assertIsNone(session_record_from_activity({"date": "2026-11-10"}))
        self.assertIsNone(session_record_from_activity({"id": "i1", "date": ""}))
        self.assertIsNone(session_record_from_activity(None))


class TestRollups(unittest.TestCase):
    def test_week_is_derived_not_incremented(self):
        """Incrementing would double-count the moment the same ride is upserted
        twice, which happens thousands of times a day."""
        s = store()
        for _ in range(5):
            s.upsert_session(activity())
        week = s._data["weeks"]["2026-11-09"]
        self.assertEqual(week["tss"], 98)
        self.assertEqual(week["sessions"], 1)

    def test_two_rides_sum(self):
        s = store()
        s.upsert_session(activity("i1", "2026-11-10", tss=98))
        s.upsert_session(activity("i2", "2026-11-12", tss=60, duration_seconds=3600))
        week = s._data["weeks"]["2026-11-09"]
        self.assertEqual((week["tss"], week["sessions"]), (158, 2))

    def test_sunday_and_monday_fall_in_different_weeks(self):
        s = store()
        s.upsert_session(activity("i1", "2026-11-15"))   # Sunday
        s.upsert_session(activity("i2", "2026-11-16"))   # Monday
        self.assertEqual(sorted(s._data["weeks"]), ["2026-11-09", "2026-11-16"])

    def test_monday_arithmetic_crosses_a_year_boundary(self):
        self.assertEqual(monday_of(date(2027, 1, 1)).isoformat(), "2026-12-28")

    def test_deleted_session_leaves_its_week(self):
        s = store()
        s.upsert_session(activity("i1", "2026-11-10", tss=98))
        s.upsert_session(activity("i2", "2026-11-11", tss=50))
        s.reconcile_deleted(["i1"], since="2026-11-01")
        self.assertEqual(s._data["weeks"]["2026-11-09"]["tss"], 98)

    def test_rebuild_weeks_preserves_prose(self):
        s = store()
        s.upsert_session(activity())
        s.set_week_summary("2026-11-09", "A good week.")
        s.rebuild_weeks()
        self.assertEqual(s._data["weeks"]["2026-11-09"]["summary"], "A good week.")
        self.assertEqual(s._data["weeks"]["2026-11-09"]["tss"], 98)


class TestRecapAttribution(unittest.TestCase):
    def test_recap_summary_belongs_to_the_week_it_reviews(self):
        """A Monday recap reviews the PREVIOUS seven days. Keying it to today's
        Monday misfiles every note by a week — invisible, and it makes the
        planner read last week's verdict as this week's."""
        self.assertEqual(recap_target_monday(date(2026, 11, 16)), "2026-11-09")
        self.assertEqual(recap_target_monday(date(2027, 1, 4)), "2026-12-28")


class TestSkipDetection(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 11, 16)
        self.day = lambda n: (self.today - timedelta(days=n)).isoformat()

    def _judge(self, events, sessions):
        return {r["id"]: st for r, st in judge_planned(events, sessions, self.today)}

    def test_four_statuses(self):
        events = [
            {"id": 111, "date": self.day(5), "category": "WORKOUT", "name": "paired"},
            {"id": 222, "date": self.day(4), "category": "WORKOUT", "name": "replaced"},
            {"id": 333, "date": self.day(3), "category": "WORKOUT", "name": "skipped"},
            {"id": 444, "date": self.day(1), "category": "WORKOUT", "name": "in grace"},
            {"id": 555, "date": self.day(4), "category": "RACE_A", "name": "race"},
            {"id": 666, "date": self.day(4), "category": "NOTE", "name": "weekly note"},
        ]
        sessions = [
            {"date": self.day(5), "paired_event_id": "111", "duration_min": 90},
            {"date": self.day(4), "duration_min": 75},
        ]
        got = self._judge(events, sessions)
        self.assertEqual(got, {"111": "done", "222": "replaced", "333": "skipped"})

    def test_compliance_counts_as_done_without_pairing(self):
        events = [{"id": 777, "date": self.day(4), "category": "WORKOUT"}]
        sessions = [{"date": self.day(4), "compliance": 93, "duration_min": 80}]
        self.assertEqual(self._judge(events, sessions), {"777": "done"})

    def test_a_late_upload_flips_skipped_to_done(self):
        events = [{"id": 888, "date": self.day(4), "category": "WORKOUT"}]
        self.assertEqual(self._judge(events, []), {"888": "skipped"})
        later = [{"date": self.day(4), "paired_event_id": "888", "duration_min": 80}]
        self.assertEqual(self._judge(events, later), {"888": "done"})

    def test_skipped_sessions_reach_the_week_rollup(self):
        s = store()
        s.upsert_session(activity("i1", "2026-11-10"))
        s.record_planned([({"id": "9", "date": "2026-11-12", "name": "VO2", "tss": 85,
                            "duration_min": 70}, "skipped")])
        self.assertEqual(s._data["weeks"]["2026-11-09"]["skipped"], 1)


class TestMemo(unittest.TestCase):
    def test_extracted_and_stripped(self):
        memo, text = extract_memo("Good ride.\n\n[[MEMO Faded late.]]")
        self.assertEqual(memo, "Faded late.")
        self.assertEqual(text, "Good ride.")
        self.assertNotIn("MEMO", text)

    def test_absent_marker(self):
        self.assertEqual(extract_memo("Just a comment."), (None, "Just a comment."))

    def test_long_memo_is_capped_on_a_word_boundary(self):
        memo, _ = extract_memo("x [[MEMO " + "word " * 100 + "]]", max_chars=50)
        self.assertLessEqual(len(memo), 51)
        self.assertTrue(memo.endswith("…"))

    def test_single_bracket_inside_survives(self):
        memo, _ = extract_memo("[[MEMO Z4 effort (see note] here) held.]]")
        self.assertIn("held.", memo)

    def test_non_string_passes_through(self):
        self.assertEqual(extract_memo(None), (None, None))


class TestHardSession(unittest.TestCase):
    def test_group_ride_follows_the_config(self):
        ride = {"group_ride": True, "intensity": 0.72}
        self.assertFalse(is_hard_session(ride, group_ride_is_hard=False))
        self.assertTrue(is_hard_session(ride, group_ride_is_hard=True))

    def test_intensity_and_time_in_zone(self):
        self.assertTrue(is_hard_session({"intensity": 0.9}, False))
        self.assertTrue(is_hard_session({"z4plus_min": 20}, False))
        self.assertFalse(is_hard_session({"intensity": 0.6, "z4plus_min": 2}, False))


class TestFileHandling(unittest.TestCase):
    def _with_contents(self, text):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "m.json")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_corrupt_file_loads_empty(self):
        s = MemoryStore(self._with_contents("{not json"))
        self.assertEqual(s._data["sessions"], {})

    def test_truncated_file_loads_empty(self):
        s = MemoryStore(self._with_contents('{"version": 1, "sessions": {"i1": {"da'))
        self.assertEqual(s._data["sessions"], {})

    def test_non_object_json_loads_empty(self):
        self.assertEqual(MemoryStore(self._with_contents("[1,2,3]"))._data["sessions"], {})

    def test_a_newer_schema_loads_read_only(self):
        """A downgrade must never destroy months of irreplaceable prose."""
        path = self._with_contents(json.dumps({"version": 99, "sessions": {}, "weeks": {}}))
        s = MemoryStore(path)
        self.assertTrue(s.read_only)
        s.upsert_session(activity())
        with open(path) as f:
            self.assertEqual(json.load(f)["sessions"], {})

    def test_legacy_record_missing_new_fields_renders(self):
        path = self._with_contents(json.dumps({
            "version": 1, "sessions": {"i1": {"id": "i1", "date": "2026-11-10", "tss": 90}},
            "weeks": {}, "planned": {}}))
        s = MemoryStore(path)
        s.rebuild_weeks()
        self.assertIn("TRAINING MEMORY", s.render("planning", max_hours=8, max_tss=500))

    def test_writes_are_atomic(self):
        s = store()
        s.upsert_session(activity())
        self.assertFalse(os.path.exists(s._path + ".tmp"))
        with open(s._path) as f:
            json.load(f)


class TestRender(unittest.TestCase):
    def _populated(self, weeks=40):
        s = store()
        start = date(2026, 1, 5)
        for w in range(weeks):
            for d in range(5):
                day = (start + timedelta(weeks=w, days=d)).isoformat()
                s.upsert_session(activity(f"i{w}-{d}", day, tss=80 + d))
            s.set_week_summary((start + timedelta(weeks=w)).isoformat(), f"Week {w} went fine.")
        return s

    def test_empty_store_renders_nothing(self):
        s = store()
        for scope in ("planning", "review", "recap", "wellness"):
            self.assertEqual(s.render(scope, max_hours=8, max_tss=500), "", scope)

    def test_every_scope_stays_within_budget(self):
        s = self._populated()
        for scope, limit in MAX_CHARS.items():
            text = s.render(scope, max_hours=8, max_tss=500)
            with self.subTest(scope=scope):
                self.assertTrue(text)
                # The static usage paragraph is prepended after the fit, so the
                # budget applies to the data block only.
                body = text.replace(MEMORY_USAGE_INSTRUCTIONS, "").strip()
                self.assertLessEqual(len(body), limit, f"{scope} body {len(body)} > {limit}")

    def test_planning_carries_the_usage_rules_and_others_do_not(self):
        s = self._populated(weeks=3)
        self.assertIn("How to use TRAINING MEMORY", s.render("planning", max_hours=8, max_tss=500))
        self.assertNotIn("How to use TRAINING MEMORY", s.render("review"))

    def test_planning_windows_to_twelve_weeks(self):
        s = self._populated()
        self.assertEqual(s.render("planning", max_hours=8, max_tss=500).count("Wk "), 12)

    def test_the_detail_block_lists_sessions_for_the_last_eight_weeks(self):
        """12 weeks of rollups, 8 of them session by session — the rest stay summarised
        so the block does not grow without bound."""
        s = self._populated()
        for scope in ("planning", "recap"):
            text = s.render(scope, max_hours=8, max_tss=500)
            with self.subTest(scope=scope):
                self.assertIn("Session by session, last 8 weeks:", text)
                self.assertIn("By month:", text)

    def test_a_week_with_prose_but_no_rides_renders_no_dashes(self):
        """A week off sick carries a recap summary and no sessions. It used to render
        "– / – · 0 sessions, 0 hard · RPE – feel –"."""
        s = store()
        s.upsert_session(activity("i1", "2026-11-10"))
        s.set_week_summary("2026-11-02", "Off sick all week, no riding.")
        text = s.render("recap", max_hours=8, max_tss=500)
        self.assertIn("Off sick all week", text)
        self.assertIn("no sessions recorded", text)
        self.assertNotIn("0 sessions, 0 hard", text)

    def test_no_none_leaks_into_a_prompt(self):
        s = store()
        s.upsert_session({"id": "i9", "date": "2026-11-10"})
        for scope in ("planning", "review", "recap", "wellness"):
            self.assertNotIn("None", s.render(scope, max_hours=8, max_tss=500), scope)

    def test_hard_count_follows_the_group_ride_setting(self):
        s = store()
        s.upsert_session(activity("g1", "2026-11-10", group_ride=True,
                                  intensity=0.72, power_zone_mins=[30, 60, 20, 2, 0, 0, 0]))
        soft = s.render("planning", group_ride_is_hard=False, max_hours=8, max_tss=500)
        hard = s.render("planning", group_ride_is_hard=True, max_hours=8, max_tss=500)
        self.assertIn("0 hard", soft)
        self.assertIn("1 hard", hard)


class TestBackfill(unittest.TestCase):
    def test_runs_once_and_is_guarded(self):
        s = store()
        self.assertFalse(s.backfilled)
        count = s.backfill([activity("i1", "2026-09-01"), activity("i2", "2026-09-03")])
        self.assertEqual(count, 2)
        self.assertTrue(s.backfilled)

    def test_backfilled_records_are_marked_and_have_no_prose(self):
        s = store()
        s.backfill([activity("i1", "2026-09-01")])
        rec = s._data["sessions"]["i1"]
        self.assertEqual(rec["source"], "backfill")
        self.assertNotIn("note", rec)

    def test_a_live_session_is_not_downgraded_by_a_later_backfill(self):
        s = store()
        s.upsert_session(activity("i1", "2026-09-01"))
        s.set_note("i1", "Kept.")
        s.backfill([activity("i1", "2026-09-01")])
        self.assertEqual(s._data["sessions"]["i1"]["source"], "live")
        self.assertEqual(s._data["sessions"]["i1"]["note"], "Kept.")


if __name__ == "__main__":
    unittest.main()


class TestWellnessStore(unittest.TestCase):
    """Field names come from the live Intervals.icu spec (components.schemas.Wellness),
    not from guesswork: the repo only ever evidenced sleepScore and sleepSecs, and a
    wrong HRV name would have lost HRV entirely once the live fetch was removed."""

    def rows(self, **over):
        base = {"id": "2026-11-10", "hrv": 61.2, "restingHR": 47, "sleepScore": 82.0,
                "sleepSecs": 27000, "ctl": 72.4, "atl": 88.1}
        base.update(over)
        return [base]

    def test_kept_fields_and_computed_form(self):
        s = store()
        self.assertEqual(s.record_wellness(self.rows()), 1)
        row = s._data["wellness"]["2026-11-10"]
        self.assertEqual(row["hrv"], 61.2)
        self.assertEqual(row["restingHR"], 47)
        self.assertEqual(row["form"], round(72.4 - 88.1, 1))   # no TSB field upstream

    def test_the_other_forty_fields_are_dropped(self):
        s = store()
        s.record_wellness(self.rows(weight=74.1, spO2=97, menstrualPhase="x", comments="hi"))
        self.assertEqual(set(s._data["wellness"]["2026-11-10"]),
                         {"hrv", "restingHR", "sleepScore", "sleepSecs", "ctl", "atl", "form"})

    def test_missing_values_are_absent_not_null(self):
        s = store()
        s.record_wellness([{"id": "2026-11-10", "hrv": 60}])
        row = s._data["wellness"]["2026-11-10"]
        self.assertEqual(row, {"hrv": 60})
        self.assertNotIn("form", row)

    def test_an_empty_day_is_not_stored(self):
        s = store()
        self.assertEqual(s.record_wellness([{"id": "2026-11-10"}, {"no_id": 1}, None]), 0)
        self.assertEqual(s._data["wellness"], {})

    def test_unchanged_rows_write_nothing(self):
        s = store()
        s.record_wellness(self.rows())
        before = s.writes
        self.assertEqual(s.record_wellness(self.rows()), 0)
        self.assertEqual(s.writes, before)

    def test_has_wellness_gates_the_recap_tool_swap(self):
        s = store()
        self.assertFalse(s.has_wellness)
        s.record_wellness(self.rows())
        self.assertTrue(s.has_wellness)

    def test_the_trend_renders_into_the_block(self):
        s = store()
        s.upsert_session(activity("i1", "2026-11-10"))
        s.record_wellness([{"id": f"2026-11-{10 + i}", "hrv": 60 + i, "restingHR": 47}
                           for i in range(5)])
        self.assertIn("Wellness by week", s.render("recap", max_hours=8, max_tss=500))
