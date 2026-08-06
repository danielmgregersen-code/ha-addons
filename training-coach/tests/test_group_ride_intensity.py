"""Offline tests for the configurable group ride intensity.

Run with:  PYTHONPATH=app python3 -m unittest discover -s tests -v

The coach used to ask, per ride, whether a group ride should count as one of the
weekly hard interval sessions. A single setting now answers that once, and the
answer has to reach four prompts consistently: planning, weekly recap, review and
auto-review.

The highest-value test here is the format-completeness one: two of those prompts
are built outside _build_system, so a placeholder added without its kwarg would
otherwise only surface as a KeyError at 06:00 on a Monday in production.
"""
import unittest

import contextlib
import io
import pathlib

from agent import (
    DEFAULT_GROUP_RIDE_INTENSITY,
    DEFAULT_GROUP_RIDE_LONG_RIDE_HOURS,
    GROUP_RIDE_FACTOR_MAX,
    GROUP_RIDE_FACTOR_MIN,
    GROUP_RIDE_INTENSITY_DESCRIPTIONS,
    GROUP_RIDE_INTENSITY_FACTORS,
    PLANNING_SYSTEM_PROMPT,
    TrainingAgent,
    _hours_phrase,
    group_ride_factors_from_options,
    group_ride_intensity_context,
    normalize_group_ride_factors,
    normalize_group_ride_intensity,
)

try:
    import yaml
except ImportError:  # not in requirements.txt; only used by the config-agreement test
    yaml = None

CONFIG_YAML = pathlib.Path(__file__).resolve().parents[1] / "config.yaml"

TIERS = ("easy", "moderate", "hard")


def make_agent(**kw):
    return TrainingAgent(openai_api_key="test", intervals_athlete_id="a1",
                         intervals_api_key="k1", **kw)


def captured_system(agent, call):
    """The fully formatted system prompt for paths that bypass _build_system.

    weekly_recap() and auto_review() format their own constants and then hand the
    messages to _run_response_loop, so stubbing that captures the real prompt —
    including every .format() kwarg — without any network.
    """
    seen = {}

    def fake_loop(**kwargs):
        seen["input"] = kwargs["input_items"]
        return ("ok", {})

    agent._run_response_loop = fake_loop
    call()
    return seen["input"][0]["content"]


def recap_system(agent):
    return captured_system(agent, agent.weekly_recap)


def auto_review_system(agent):
    return captured_system(agent, lambda: agent.auto_review({"id": "i1", "type": "Ride"}))


class TestNormalisation(unittest.TestCase):
    def test_default_is_moderate(self):
        self.assertEqual(DEFAULT_GROUP_RIDE_INTENSITY, "moderate")
        self.assertEqual(make_agent().group_ride_intensity, "moderate")

    def test_unusable_values_fall_back_to_the_default(self):
        for value in ("", "   ", None, "bogus", "Moderate ", "5"):
            with self.subTest(value=value):
                self.assertEqual(make_agent(group_ride_intensity=value).group_ride_intensity,
                                 "moderate")

    def test_valid_tiers_survive_case_and_padding(self):
        self.assertEqual(normalize_group_ride_intensity("  HARD "), "hard")
        self.assertEqual(normalize_group_ride_intensity("Easy"), "easy")
        for tier in TIERS:
            self.assertEqual(make_agent(group_ride_intensity=tier).group_ride_intensity, tier)


class TestPromptThreading(unittest.TestCase):
    """The setting has to reach all four prompts, or it silently does nothing."""

    def test_tier_description_reaches_every_prompt(self):
        for tier in TIERS:
            agent = make_agent(group_ride_intensity=tier)
            description = GROUP_RIDE_INTENSITY_DESCRIPTIONS[tier]
            prompts = {
                "planning": agent._build_system("planning"),
                "review": agent._build_system("review"),
                "weekly_recap": recap_system(agent),
                "auto_review": auto_review_system(agent),
            }
            for name, prompt in prompts.items():
                with self.subTest(tier=tier, prompt=name):
                    self.assertIn(description, prompt)

    def test_every_prompt_formats_without_a_missing_kwarg(self):
        """Building each prompt at all proves no placeholder lacks its kwarg."""
        agent = make_agent()
        for mode in ("planning", "review", "health", "unknown-mode"):
            with self.subTest(mode=mode):
                self.assertTrue(agent._build_system(mode))
        self.assertTrue(recap_system(agent))
        self.assertTrue(auto_review_system(agent))

    def test_review_prompts_explain_the_group_ride_flag(self):
        agent = make_agent()
        for name, prompt in (("review", agent._build_system("review")),
                             ("auto_review", auto_review_system(agent))):
            with self.subTest(prompt=name):
                self.assertIn("group_ride flag", prompt)

    def test_recap_receives_the_hard_session_target(self):
        prompt = recap_system(make_agent(hard_intervals_per_week=4))
        self.assertIn("4 hard interval sessions", prompt)


class TestTierSemantics(unittest.TestCase):
    def test_slot_consumption_is_stated_unambiguously(self):
        for tier in ("easy", "moderate"):
            self.assertIn("does NOT consume", GROUP_RIDE_INTENSITY_DESCRIPTIONS[tier])
        self.assertIn("DOES consume", GROUP_RIDE_INTENSITY_DESCRIPTIONS["hard"])

    def test_readiness_band_disambiguation_is_present(self):
        """'moderate' is also a Garmin readiness band; the prompt must say it isn't one."""
        for tier in TIERS:
            self.assertIn("NOT a Garmin", group_ride_intensity_context(tier))

    def test_both_intensity_factor_bands_are_rendered(self):
        for tier in TIERS:
            short_if, long_if = GROUP_RIDE_INTENSITY_FACTORS[tier]
            context = group_ride_intensity_context(tier)
            with self.subTest(tier=tier):
                self.assertIn(f"{short_if:.2f}", context)
                self.assertIn(f"{long_if:.2f}", context)
                self.assertIn(_hours_phrase(DEFAULT_GROUP_RIDE_LONG_RIDE_HOURS), context)

    def test_shipped_defaults_drop_off_for_long_rides(self):
        """Constrains the SHIPPED DEFAULTS, not user input — a configured set may be
        inverted and is deliberately used as given (see TestFactorOverrides)."""
        for tier in TIERS:
            short_if, long_if = GROUP_RIDE_INTENSITY_FACTORS[tier]
            self.assertLess(long_if, short_if, tier)

    def test_shipped_defaults_increase_with_tier(self):
        """Also defaults only. Cross-tier ordering is never enforced on user input:
        one tier is active at a time, so the others cannot affect any prompt."""
        for band in (0, 1):
            values = [GROUP_RIDE_INTENSITY_FACTORS[t][band] for t in TIERS]
            self.assertEqual(values, sorted(values))


class TestFactorOverrides(unittest.TestCase):
    def _all_prompts(self, agent):
        return {
            "planning": agent._build_system("planning"),
            "review": agent._build_system("review"),
            "weekly_recap": recap_system(agent),
            "auto_review": auto_review_system(agent),
        }

    def test_configured_factors_reach_every_prompt(self):
        """The load-bearing test. A forgotten argument at one of the four renderer call
        sites is NOT a KeyError — that path would keep rendering the defaults, format
        cleanly, and pass every other test while the setting silently did nothing."""
        agent = make_agent(group_ride_factors={"moderate": (0.91, 0.88)})
        for name, prompt in self._all_prompts(agent).items():
            with self.subTest(prompt=name):
                self.assertIn("0.91", prompt)
                self.assertIn("0.88", prompt)
                self.assertNotIn("0.72", prompt)

    def test_configured_threshold_reaches_every_prompt(self):
        agent = make_agent(group_ride_long_ride_hours=4)
        for name, prompt in self._all_prompts(agent).items():
            with self.subTest(prompt=name):
                self.assertIn("4 hours", prompt)
                self.assertNotIn("3 hours", prompt)

    def test_defaults_are_unchanged_when_nothing_is_configured(self):
        """This change must be purely additive."""
        for factors in (None, {}):
            context = group_ride_intensity_context("moderate", factors)
            with self.subTest(factors=factors):
                self.assertIn("0.72", context)
                self.assertIn("0.70", context)
                self.assertIn("3 hours", context)

    def test_overrides_apply_per_tier_and_band(self):
        resolved = normalize_group_ride_factors({"easy": (0.90, None)})
        self.assertEqual(resolved["easy"], (0.90, 0.60))
        self.assertEqual(resolved["moderate"], GROUP_RIDE_INTENSITY_FACTORS["moderate"])
        self.assertEqual(resolved["hard"], GROUP_RIDE_INTENSITY_FACTORS["hard"])

    def test_unusable_values_fall_back(self):
        bad = [None, "", "   ", "abc", True, False, [], {},
               GROUP_RIDE_FACTOR_MIN - 0.01, GROUP_RIDE_FACTOR_MAX + 0.01,
               -1, 0, 5.0, float("nan"), float("inf")]
        for value in bad:
            with self.subTest(value=repr(value)):
                resolved = normalize_group_ride_factors({"hard": (value, value)})
                self.assertEqual(resolved["hard"], GROUP_RIDE_INTENSITY_FACTORS["hard"])

    def test_numeric_strings_are_accepted(self):
        """The env-var path hands everything over as strings."""
        self.assertEqual(normalize_group_ride_factors({"hard": ("0.85", "0.83")})["hard"],
                         (0.85, 0.83))

    def test_option_names_map_to_tier_and_band(self):
        """Guards the mapping main.py depends on and cannot itself test."""
        resolved = group_ride_factors_from_options({"group_ride_if_hard_long": 0.90})
        self.assertEqual(resolved["hard"], (0.80, 0.90))
        self.assertEqual(resolved["easy"], GROUP_RIDE_INTENSITY_FACTORS["easy"])
        self.assertEqual(group_ride_factors_from_options({}), GROUP_RIDE_INTENSITY_FACTORS)
        self.assertEqual(group_ride_factors_from_options(None), GROUP_RIDE_INTENSITY_FACTORS)

    def test_inverted_factors_are_kept_and_warned_about(self):
        """Deliberate policy: warn, don't clamp or swap. Substituting numbers the athlete
        never set would make the prompt disagree with the settings page silently."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            resolved = normalize_group_ride_factors({"hard": (0.70, 0.90)})
        self.assertEqual(resolved["hard"], (0.70, 0.90))
        self.assertIn("inverted", buf.getvalue())


class TestThresholdRendering(unittest.TestCase):
    def test_integral_threshold_has_no_decimal_point(self):
        """Home Assistant coerces float options, so a configured 3 always arrives 3.0."""
        self.assertEqual(_hours_phrase(3.0), "3 hours")
        self.assertNotIn("3.0", _hours_phrase(3.0))

    def test_a_one_hour_threshold_is_singular(self):
        self.assertEqual(_hours_phrase(1), "1 hour")

    def test_fractional_threshold(self):
        self.assertEqual(_hours_phrase(2.5), "2.5 hours")

    def test_unusable_threshold_falls_back(self):
        for value in (None, "", "x", 0, 0.5, 99, True, float("nan")):
            with self.subTest(value=repr(value)):
                self.assertEqual(_hours_phrase(value), "3 hours")


@unittest.skipUnless(yaml, "PyYAML not installed")
class TestConfigYamlAgreement(unittest.TestCase):
    """The only mechanism that can catch config.yaml drifting from the code — main.py
    is unimportable offline and the defaults now live in more than one place."""

    @classmethod
    def setUpClass(cls):
        cls.config = yaml.safe_load(CONFIG_YAML.read_text())

    def test_factor_defaults_match_the_code(self):
        for tier, (short_if, long_if) in GROUP_RIDE_INTENSITY_FACTORS.items():
            self.assertAlmostEqual(self.config["options"][f"group_ride_if_{tier}_short"], short_if)
            self.assertAlmostEqual(self.config["options"][f"group_ride_if_{tier}_long"], long_if)

    def test_threshold_default_matches_the_code(self):
        self.assertAlmostEqual(self.config["options"]["group_ride_long_ride_hours"],
                               DEFAULT_GROUP_RIDE_LONG_RIDE_HOURS)

    def test_factor_schema_ranges_match_the_code_bounds(self):
        expected = f"float({GROUP_RIDE_FACTOR_MIN:g},{GROUP_RIDE_FACTOR_MAX:g})"
        for tier in GROUP_RIDE_INTENSITY_FACTORS:
            for band in ("short", "long"):
                self.assertEqual(self.config["schema"][f"group_ride_if_{tier}_{band}"], expected)

    def test_every_option_has_a_schema_entry(self):
        self.assertEqual(set(self.config["options"]), set(self.config["schema"]))


class TestPromptCache(unittest.TestCase):
    def test_changing_the_setting_invalidates_the_cached_prompt(self):
        """_build_system md5s a config tuple; the setting must be in it."""
        agent = make_agent(group_ride_intensity="moderate")
        moderate = agent._build_system("planning")
        agent.group_ride_intensity = "hard"
        self.assertNotEqual(moderate, agent._build_system("planning"))

    def test_changing_a_factor_invalidates_the_cached_prompt(self):
        agent = make_agent()
        before = agent._build_system("planning")
        agent.group_ride_factors["moderate"] = (0.90, 0.88)
        self.assertNotEqual(before, agent._build_system("planning"))

    def test_changing_the_threshold_invalidates_the_cached_prompt(self):
        agent = make_agent()
        before = agent._build_system("planning")
        agent.group_ride_long_ride_hours = 4.0
        self.assertNotEqual(before, agent._build_system("planning"))


class TestPlanningRuleReplaced(unittest.TestCase):
    def test_planning_no_longer_asks_per_ride(self):
        self.assertNotIn("should it count as one of the", PLANNING_SYSTEM_PROMPT)
        self.assertIn("do not ask them to classify each ride", PLANNING_SYSTEM_PROMPT)

    def test_planning_forbids_creating_a_workout_for_a_group_ride(self):
        """A planned workout would make the ride compliance-matched and get the
        athlete marked down for missing intervals nobody planned."""
        self.assertIn("never create a planned workout", PLANNING_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
