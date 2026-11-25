"""Offline tests for posting a coach review to Intervals.icu.

Run with:  PYTHONPATH=app python3 -m unittest discover -s tests -v

Regression cover for a 404 on "Post review":

    404 Client Error: Not Found for url:
    https://intervals.icu/api/v1/activity/172850190

Intervals.icu activity ids are strings with a letter prefix ("i172850190"), but
the review prompt used to ask the model for the activity's "numeric id", so it
stripped the prefix. The prompt is fixed; post_activity_comment additionally
self-heals a bare-digit id, which also repairs markers already stored in
/data/chat_history.json.

Also covers the coach tick now carried through the Accept flow — without it the
nightly auto-review would overwrite an accepted review.
"""
import json
import pathlib
import re
import unittest
from types import SimpleNamespace as NS

import requests

from agent import REVIEW_SYSTEM_PROMPT
from intervals import IntervalsClient

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

BARE = "/activity/172850190"
PREFIXED = "/activity/i172850190"


def make_client(failures=None):
    """`failures` maps a request path to the HTTP status it should raise."""
    failures = failures or {}
    c = IntervalsClient("a1", "k1")
    c.calls = []

    def _put(path, body):
        c.calls.append((path, dict(body)))
        status = failures.get(path)
        if status:
            raise requests.HTTPError(f"{status} Client Error for url {path}",
                                     response=NS(status_code=status))
        return {"id": path.rsplit("/", 1)[-1]}

    c._put = _put
    return c


class TestActivityIdRepair(unittest.TestCase):
    def test_bare_digit_id_is_retried_with_the_i_prefix(self):
        """The reported bug: the model dropped the 'i' and the PUT 404'd."""
        c = make_client({BARE: 404})
        result = c.post_activity_comment("172850190", "Solid ride.")
        self.assertEqual([p for p, _ in c.calls], [BARE, PREFIXED])
        self.assertEqual(result, {"id": "i172850190"})
        # Same payload both times — the retry changes only the path.
        self.assertEqual(c.calls[0][1], c.calls[1][1])

    def test_non_404_errors_are_not_retried(self):
        """A 500 may mean the write partly landed; repeating it is not safe."""
        c = make_client({BARE: 500})
        with self.assertRaises(requests.HTTPError):
            c.post_activity_comment("172850190", "x")
        self.assertEqual(len(c.calls), 1)

    def test_already_prefixed_id_is_not_retried(self):
        c = make_client({PREFIXED: 404})
        with self.assertRaises(requests.HTTPError):
            c.post_activity_comment("i172850190", "x")
        self.assertEqual([p for p, _ in c.calls], [PREFIXED])
        self.assertNotIn("/activity/ii172850190", [p for p, _ in c.calls])

    def test_retry_is_attempted_at_most_once(self):
        c = make_client({BARE: 404, PREFIXED: 404})
        with self.assertRaises(requests.HTTPError):
            c.post_activity_comment("172850190", "x")
        self.assertEqual(len(c.calls), 2)

    def test_error_without_a_response_object_propagates(self):
        """requests can raise HTTPError with response=None — guards the getattr."""
        c = IntervalsClient("a1", "k1")
        c.calls = []

        def _put(path, body):
            c.calls.append(path)
            raise requests.HTTPError("boom")

        c._put = _put
        with self.assertRaises(requests.HTTPError):
            c.post_activity_comment("172850190", "x")
        self.assertEqual(len(c.calls), 1)

    def test_helper_only_prefixes_bare_ascii_digits(self):
        f = IntervalsClient._prefixed_activity_id
        self.assertEqual(f("172850190"), "i172850190")
        self.assertEqual(f(" 172850190 "), "i172850190")
        self.assertEqual(f(172850190), "i172850190")
        for already in ("i172850190", "abc", "", "17285-0190", "١٧٢"):
            self.assertIsNone(f(already), already)


class TestCoachTick(unittest.TestCase):
    def test_tick_is_sent_and_survives_the_retry(self):
        c = make_client({BARE: 404})
        c.post_activity_comment("172850190", "Solid ride.", coach_tick_id=4)
        for _, body in c.calls:
            self.assertEqual(body["coach_tick"], 4)
        self.assertEqual(len(c.calls), 2)

    def test_no_tick_means_the_key_is_absent_not_null(self):
        c = make_client()
        c.post_activity_comment("i172850190", "Solid ride.")
        self.assertNotIn("coach_tick", c.calls[0][1])


# Python twins of the two shipped regexes. The real ones live in JS
# (static/index.html) and in main.py, which can't be imported here because
# fastapi isn't installed — so the literals are pinned to the source files below
# and any drift fails these tests.
JS_MATCH_SRC = r"/\[\[PROPOSE_REVIEW_POST\s+activity_id=([^\]\s]+)([^\]]*)\]\]/"
PY_STRIP_SRC = r'r"\s*\[\[PROPOSE_[^\]]*\]\]\s*"'

MATCH = re.compile(r"\[\[PROPOSE_REVIEW_POST\s+activity_id=([^\]\s]+)([^\]]*)\]\]")
TICK = re.compile(r"tick\s*=\s*(\d+)")
STRIP = re.compile(r"\s*\[\[PROPOSE_[^\]]*\]\]\s*")

MARKERS = [
    ("[[PROPOSE_REVIEW_POST activity_id=42]]", "42", None),           # legacy, in /data today
    ("[[PROPOSE_REVIEW_POST activity_id=42 ]]", "42", None),          # legacy, trailing space
    ("[[PROPOSE_REVIEW_POST activity_id=i172850190]]", "i172850190", None),
    ("[[PROPOSE_REVIEW_POST activity_id=i172850190 tick=4]]", "i172850190", 4),
    ("[[PROPOSE_REVIEW_POST activity_id=i172850190 tick=bad]]", "i172850190", None),
]


class TestProposalMarker(unittest.TestCase):
    def test_every_marker_form_yields_its_id_and_tick(self):
        for marker, expected_id, expected_tick in MARKERS:
            with self.subTest(marker=marker):
                m = MATCH.search(marker)
                self.assertIsNotNone(m, "Accept button would be lost entirely")
                self.assertEqual(m.group(1), expected_id)
                t = TICK.search(m.group(2))
                self.assertEqual(int(t.group(1)) if t else None, expected_tick)

    def test_strip_removes_every_marker_form(self):
        for marker, _, _ in MARKERS:
            with self.subTest(marker=marker):
                self.assertEqual(STRIP.sub("", f"Solid ride.\n\n{marker}").strip(),
                                 "Solid ride.")

    def test_shipped_js_regex_matches_this_twin(self):
        self.assertIn(JS_MATCH_SRC, (APP / "static/index.html").read_text())

    def test_shipped_python_strip_regex_matches_this_twin(self):
        self.assertIn(PY_STRIP_SRC, (APP / "main.py").read_text())

    def test_the_server_builds_the_marker_from_the_tool_argument(self):
        """The marker is no longer the model's to transcribe. It calls
        propose_review_post and the server writes the marker from the id it was
        given — so a bare-digit id is repaired before it can 404."""
        from test_response_loop import FakeResponse, _call, _message, make_agent
        from agent import TrainingAgent

        agent = make_agent([
            FakeResponse([_call("c1", "propose_review_post",
                                json.dumps({"activity_id": "172850190", "tick": 4}))]),
            FakeResponse([_message("Three reps, all on target.")]),
        ])
        # make_agent stubs the dispatcher; this test needs the real one.
        agent._run_tool = TrainingAgent._run_tool.__get__(agent)

        reply, _history, _usage = agent.chat("how did that one go?", [], mode="review")
        m = MATCH.search(reply)
        self.assertIsNotNone(m, f"no marker in reply: {reply!r}")
        self.assertEqual(m.group(1), "i172850190")
        self.assertIsNotNone(TICK.search(m.group(2)))

    def test_an_empty_tick_list_yields_a_marker_without_a_tick(self):
        from test_response_loop import FakeResponse, _call, _message, make_agent
        from agent import TrainingAgent

        agent = make_agent([
            FakeResponse([_call("c1", "propose_review_post",
                                json.dumps({"activity_id": "i172850190"}))]),
            FakeResponse([_message("Solid.")]),
        ])
        agent._run_tool = TrainingAgent._run_tool.__get__(agent)
        reply, _history, _usage = agent.chat("and that one?", [], mode="review")
        m = MATCH.search(reply)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "i172850190")
        self.assertIsNone(TICK.search(m.group(2)))

    def test_the_prompt_asks_for_the_tool_and_not_the_marker(self):
        """Two mechanisms for the same offer is how they come to disagree."""
        self.assertIn("propose_review_post", REVIEW_SYSTEM_PROMPT)
        self.assertNotIn("[[PROPOSE_REVIEW_POST", REVIEW_SYSTEM_PROMPT)
        self.assertNotIn("numeric", REVIEW_SYSTEM_PROMPT.lower())


if __name__ == "__main__":
    unittest.main()
