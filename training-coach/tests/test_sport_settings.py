"""Offline tests for per-sport threshold settings.

Run with:  PYTHONPATH=app python3 -m unittest discover -s tests -v

Regression cover for the bug where analysing a run judged it against the cycling
FTP: get_athlete() used to collapse Intervals.icu's per-sport `sportSettings` to
the Ride entry, so run FTP, LTHR and max HR were all silently the bike's.

No network required — IntervalsClient.__init__ does no I/O, so `_get`/`_put` can
be monkeypatched with fixture payloads.
"""
import unittest

from intervals import IntervalsClient

RIDE = {"types": ["Ride", "VirtualRide"], "ftp": 280, "lthr": 165, "max_hr": 188}
RUN = {"types": ["Run", "TrailRun"], "ftp": 310, "lthr": 172, "max_hr": 192,
       "threshold_pace": 3.9, "pace_units": "SECS_100M"}

TICKS = [{"id": 1, "text": "Really bad"}]


def payload(sport_settings, coach_ticks=TICKS):
    return {"id": "i1", "name": "Test Athlete",
            "coach_ticks": list(coach_ticks), "sportSettings": sport_settings}


def client(data, puts=None):
    c = IntervalsClient("a1", "k1")
    c._get = lambda path, params=None: data
    c._put = lambda path, body: (puts.append((path, body)) if puts is not None else None) or {}
    return c


def entry_for(result, sport):
    """Mimic what the model is asked to do: match activity type against `sports`."""
    return next((e for e in result["sport_settings"] if sport in e["sports"]), None)


class TestPerSportThresholds(unittest.TestCase):
    def test_run_gets_run_thresholds(self):
        """The reported bug: a run must not be judged against cycling FTP."""
        result = client(payload([RIDE, RUN])).get_athlete()
        run = entry_for(result, "Run")
        self.assertIsNotNone(run)
        self.assertEqual((run["ftp"], run["lthr"], run["max_hr"]), (310, 172, 192))

    def test_ride_still_gets_ride_thresholds(self):
        """Guard against over-correcting the fix."""
        result = client(payload([RIDE, RUN])).get_athlete()
        ride = entry_for(result, "Ride")
        self.assertEqual((ride["ftp"], ride["lthr"], ride["max_hr"]), (280, 165, 188))

    def test_grouped_sport_shares_its_entry(self):
        result = client(payload([RIDE, RUN])).get_athlete()
        self.assertIs(entry_for(result, "VirtualRide"), entry_for(result, "Ride"))
        self.assertIs(entry_for(result, "TrailRun"), entry_for(result, "Run"))

    def test_no_bare_top_level_thresholds(self):
        """A top-level ftp is the trap that caused the bug — it must stay gone."""
        result = client(payload([RIDE, RUN])).get_athlete()
        for key in ("ftp", "lthr", "max_hr"):
            self.assertNotIn(key, result)

    def test_sports_configured_is_the_sorted_union(self):
        result = client(payload([RIDE, RUN])).get_athlete()
        self.assertEqual(result["sports_configured"],
                         ["Ride", "Run", "TrailRun", "VirtualRide"])


class TestMissingSettings(unittest.TestCase):
    def test_unconfigured_sport_cannot_reach_another_sports_numbers(self):
        result = client(payload([RIDE])).get_athlete()
        self.assertNotIn("Run", result["sports_configured"])
        self.assertIsNone(entry_for(result, "Run"))
        # The old code fell back to "first entry with an ftp" — that path is gone.
        self.assertNotIn(280, [e["ftp"] for e in result["sport_settings"]
                               if "Run" in e["sports"]])

    def test_unset_threshold_is_null_but_present(self):
        """Distinguishes 'run FTP not configured' from 'run FTP is 250W'."""
        run_no_ftp = {**RUN, "ftp": None}
        result = client(payload([RIDE, run_no_ftp])).get_athlete()
        run = entry_for(result, "Run")
        self.assertIn("ftp", run)
        self.assertIsNone(run["ftp"])
        self.assertEqual(run["lthr"], 172)

    def test_optional_fields_appear_only_when_present(self):
        result = client(payload([RIDE, RUN])).get_athlete()
        run, ride = entry_for(result, "Run"), entry_for(result, "Ride")
        self.assertEqual(run["threshold_pace"], 3.9)
        self.assertEqual(run["pace_units"], "SECS_100M")
        # Ride has neither — they must be absent as keys, not null.
        self.assertNotIn("threshold_pace", ride)
        self.assertNotIn("pace_units", ride)

    def test_empty_optional_value_is_treated_as_absent(self):
        result = client(payload([{**RIDE, "pace_units": ""}])).get_athlete()
        self.assertNotIn("pace_units", entry_for(result, "Ride"))

    def test_missing_sport_settings_is_not_an_error(self):
        for data in (payload([]), payload(None), {"id": "i1", "coach_ticks": TICKS}):
            result = client(data).get_athlete()
            self.assertEqual(result["sport_settings"], [])
            self.assertEqual(result["sports_configured"], [])

    def test_entry_without_types_contributes_no_sports(self):
        result = client(payload([{"ftp": 250}])).get_athlete()
        self.assertEqual(result["sport_settings"][0]["sports"], [])
        self.assertEqual(result["sports_configured"], [])


class TestCoachTicksSideEffect(unittest.TestCase):
    def test_existing_ticks_pass_through_without_a_write(self):
        puts = []
        result = client(payload([RIDE]), puts).get_athlete()
        self.assertEqual(result["coach_ticks"], TICKS)
        self.assertEqual(puts, [])

    def test_empty_ticks_trigger_exactly_one_write_of_the_defaults(self):
        puts = []
        result = client(payload([RIDE], coach_ticks=[]), puts).get_athlete()
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0][1], {"coach_ticks": IntervalsClient.DEFAULT_COACH_TICKS})
        self.assertEqual(result["coach_ticks"], IntervalsClient.DEFAULT_COACH_TICKS)


if __name__ == "__main__":
    unittest.main()
