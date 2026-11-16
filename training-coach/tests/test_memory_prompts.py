"""Tests that the training memory actually reaches every prompt.

Run with:  PYTHONPATH=app python3 -m unittest discover -s tests -v

Needs openai importable (it imports agent), unlike test_memory.py.

The load-bearing test here is test_memory_reaches_every_prompt. Unlike a missing
.format() kwarg, which raises KeyError, a forgotten memory argument at one of the
call sites is silent: that prompt simply renders without the block, formats
cleanly, and passes everything else while the feature quietly does nothing there.
"""
import os
import tempfile
import unittest
from datetime import date, timedelta

from agent import TrainingAgent
from memory import MEMORY_SENTINEL, MemoryStore

MARKER = "7777"   # a TSS value distinctive enough to find in a prompt


def make_store(days=14):
    store = MemoryStore(os.path.join(tempfile.mkdtemp(), "m.json"))
    today = date.today()
    for i in range(days):
        day = (today - timedelta(days=i + 2)).isoformat()
        store.upsert_session({
            "id": f"i{i}", "date": day, "type": "Ride", "name": "Session",
            "duration_seconds": 3600, "tss": int(MARKER), "intensity": 0.7,
            "rpe": 6, "feel": 2, "start_date_local": f"{day}T10:00:00",
        })
    return store


def make_agent(memory=None, **kw):
    return TrainingAgent(openai_api_key="test", intervals_athlete_id="a1",
                         intervals_api_key="k1", memory=memory, **kw)


def captured_system(agent, call):
    seen = {}

    def fake_loop(**kwargs):
        seen["input"] = kwargs["input_items"]
        return ("ok", {})

    agent._run_response_loop = fake_loop
    call()
    return seen["input"][0]["content"]


def all_prompts(agent):
    """Every prompt that should carry a memory block, by the route it really uses:
    planning and review through the cached _build_system, the other three through
    their own .format() calls."""
    return {
        "planning": agent._build_system("planning"),
        "review": agent._build_system("review"),
        "health": agent._build_system("health"),
        "auto_review": captured_system(
            agent, lambda: agent.auto_review({"id": "i1", "type": "Ride"})),
        "weekly_recap": captured_system(agent, agent.weekly_recap),
        "wellness_check": captured_system(agent, agent.wellness_check),
    }


class TestMemoryReachesPrompts(unittest.TestCase):
    def test_memory_reaches_every_prompt(self):
        agent = make_agent(memory=make_store())
        for name, prompt in all_prompts(agent).items():
            with self.subTest(prompt=name):
                self.assertIn("TRAINING MEMORY", prompt)
                self.assertIn(MARKER, prompt)

    def test_every_prompt_formats_without_a_missing_kwarg(self):
        agent = make_agent(memory=make_store())
        for name, prompt in all_prompts(agent).items():
            with self.subTest(prompt=name):
                self.assertTrue(prompt)

    def test_wellness_readiness_variant_also_carries_memory(self):
        agent = make_agent(memory=make_store(), readiness_entity="sensor.readiness")
        agent._get_training_readiness = lambda: {"available": False, "reason": "test"}
        self.assertIn("TRAINING MEMORY", captured_system(agent, agent.wellness_check))

    def test_sentinel_never_reaches_the_model(self):
        """A leaked sentinel is worse than no feature."""
        for memory in (make_store(), None):
            agent = make_agent(memory=memory)
            for name, prompt in all_prompts(agent).items():
                with self.subTest(memory=bool(memory), prompt=name):
                    self.assertNotIn(MEMORY_SENTINEL, prompt)


class TestPurelyAdditive(unittest.TestCase):
    def test_without_a_store_every_prompt_still_builds(self):
        agent = make_agent(memory=None)
        for name, prompt in all_prompts(agent).items():
            with self.subTest(prompt=name):
                self.assertTrue(prompt)
                self.assertNotIn("TRAINING MEMORY", prompt)

    def test_an_empty_store_renders_no_header(self):
        empty = MemoryStore(os.path.join(tempfile.mkdtemp(), "m.json"))
        agent = make_agent(memory=empty)
        self.assertNotIn("TRAINING MEMORY", agent._build_system("planning"))

    def test_a_broken_store_cannot_take_down_a_prompt(self):
        class Exploding:
            def render(self, *a, **k):
                raise RuntimeError("boom")
        agent = make_agent(memory=Exploding())
        self.assertTrue(agent._build_system("planning"))


class TestCacheFreshness(unittest.TestCase):
    def test_new_data_is_visible_without_waiting_for_the_date_to_roll(self):
        """config_key carries today's date, but memory changes hourly — a block
        baked into the cached value would be stale for the rest of the day."""
        store = make_store()
        agent = make_agent(memory=store)
        before = agent._build_system("planning")
        store.upsert_session({"id": "new", "date": date.today().isoformat(),
                              "type": "Ride", "duration_seconds": 7200, "tss": 1234,
                              "start_date_local": f"{date.today().isoformat()}T10:00:00"})
        after = agent._build_system("planning")
        self.assertNotEqual(before, after)
        self.assertIn("1234", after)

    def test_the_cache_does_not_churn(self):
        store = make_store()
        agent = make_agent(memory=store)
        for i in range(5):
            agent._build_system("planning")
            store.upsert_session({"id": f"x{i}", "date": date.today().isoformat(),
                                  "type": "Ride", "duration_seconds": 600, "tss": 10,
                                  "start_date_local": f"{date.today().isoformat()}T10:00:00"})
        self.assertEqual(len(agent._prompt_cache), 1)


if __name__ == "__main__":
    unittest.main()
