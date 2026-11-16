"""The planned-events window used by the memory sweep.

get_events gained a days_back parameter so the sweep can look at past planned
workouts. The default must stay byte-identical, because every existing caller —
including the get_planned_workouts tool — relies on it.
"""
import unittest
from datetime import datetime, timedelta

from intervals import IntervalsClient


def client():
    c = IntervalsClient("a1", "k1")
    c.seen = []

    def fake_get(path, params=None):
        c.seen.append((path, dict(params or {})))
        return []

    c._get = fake_get
    return c


class TestEventsWindow(unittest.TestCase):
    def test_default_still_starts_today(self):
        c = client()
        c.get_events()
        self.assertEqual(c.seen[0][1]["oldest"], datetime.now().strftime("%Y-%m-%d"))

    def test_days_back_reaches_into_the_past(self):
        c = client()
        c.get_events(days_ahead=0, days_back=14)
        params = c.seen[0][1]
        today = datetime.now()
        self.assertEqual(params["oldest"], (today - timedelta(days=14)).strftime("%Y-%m-%d"))
        self.assertEqual(params["newest"], today.strftime("%Y-%m-%d"))

    def test_the_tool_schema_does_not_expose_days_back(self):
        """Letting the model pull past events is the token sink the memory removes."""
        from agent import TOOLS
        tool = next(t for t in TOOLS if t["name"] == "get_planned_workouts")
        self.assertNotIn("days_back", tool["parameters"]["properties"])


if __name__ == "__main__":
    unittest.main()
