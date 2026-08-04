"""Offline tests for the /v1/responses tool loop.

Run with:  PYTHONPATH=app python3 -m unittest discover -s tests -v

No network and no API key required: IntervalsClient, HAClient and OpenAI() all
do zero I/O at construction, so a TrainingAgent can be built and its
`openai.responses.create` monkeypatched with scripted responses.

These guard the constraints that downstream code depends on but cannot assert
for itself — above all the usage key mapping, which fails *silently* if wrong
(main.accumulate_tokens reads three keys via .get(k, 0), so Responses-style
names would accumulate zero and quietly disable the daily budget gate).
"""
import json
import unittest
from types import SimpleNamespace as NS

from agent import (
    TOOLS, TrainingAgent, _HEALTH_READINESS_TOOL_NAMES, _HEALTH_TOOL_NAMES,
    _PLANNING_TOOL_NAMES, _RECAP_TOOL_NAMES, _REVIEW_TOOL_NAMES,
    _WELLNESS_READINESS_TOOL_NAMES, _WELLNESS_TOOL_NAMES,
)


# ── Fakes mirroring the SDK's response shape ──

def _usage(i=10, o=5, r=3):
    return NS(input_tokens=i, output_tokens=o, total_tokens=i + o,
              output_tokens_details=NS(reasoning_tokens=r))


def _reasoning(rid="rs_1"):
    return NS(type="reasoning", id=rid, summary=[], encrypted_content="ENC")


def _call(call_id, name, arguments):
    return NS(type="function_call", call_id=call_id, name=name,
              arguments=arguments, id="fc_" + call_id)


def _message(text):
    return NS(type="message", content=[NS(type="output_text", text=text)])


_DEFAULT = object()


class FakeResponse:
    """Mimics the SDK's output_text property: joins output_text parts, never None."""

    def __init__(self, output, usage=_DEFAULT, status="completed"):
        self.output = output
        self.usage = _usage() if usage is _DEFAULT else usage
        self.status = status
        self.incomplete_details = None

    @property
    def output_text(self):
        return "".join(c.text for it in self.output if it.type == "message"
                       for c in it.content if c.type == "output_text")


def make_agent(scripted, **kw):
    """Build an offline agent whose responses.create replays `scripted`.

    The script pointer is independent of `seen` so tests may clear the record
    without rewinding the script. Each recorded request snapshots its `input`
    list — the loop appends to that list in place, so storing it by reference
    would let later iterations rewrite earlier records.
    """
    agent = TrainingAgent(openai_api_key="test", intervals_athlete_id="a1",
                          intervals_api_key="k1", **kw)
    agent.seen = []
    agent.tools_run = []
    pos = [0]

    def fake_create(**kwargs):
        agent.seen.append({**kwargs, "input": list(kwargs["input"])})
        item = scripted[min(pos[0], len(scripted) - 1)]
        pos[0] += 1
        if isinstance(item, Exception):
            raise item
        return item

    agent.openai = NS(responses=NS(create=fake_create))
    agent._run_tool = lambda n, a: (agent.tools_run.append((n, a)) or f"RESULT:{n}")
    return agent


def loop(agent, tools=None, max_iterations=5, items=None):
    return agent._run_response_loop(
        model="gpt-5.6",
        input_items=items if items is not None else [{"role": "user", "content": "hi"}],
        tools=tools if tools is not None else TOOLS,
        max_iterations=max_iterations,
        label="Test loop",
    )


class TestUsageMapping(unittest.TestCase):
    def test_responses_usage_maps_to_chat_completions_keys(self):
        """Guards the silent-zero failure in main.accumulate_tokens."""
        agent = make_agent([
            FakeResponse([_call("c1", "get_coach_ticks", "{}")], _usage(10, 5, 3)),
            FakeResponse([_message("done")], _usage(10, 5, 3)),
        ])
        _, usage = loop(agent)
        self.assertEqual(usage, {"prompt_tokens": 20, "completion_tokens": 10,
                                 "total_tokens": 30, "reasoning_tokens": 6})

    def test_missing_usage_does_not_crash(self):
        agent = make_agent([FakeResponse([_message("hi")], usage=None)])
        _, usage = loop(agent)
        self.assertEqual(usage["total_tokens"], 0)


class TestReasoningCarryOver(unittest.TestCase):
    def test_reasoning_item_precedes_its_call_on_the_next_request(self):
        """The whole point of the migration: reasoning must survive tool turns."""
        agent = make_agent([
            FakeResponse([_reasoning("rs_1"), _call("c1", "get_coach_ticks", "{}")]),
            FakeResponse([_message("final")]),
        ])
        text, _ = loop(agent)
        self.assertEqual(text, "final")

        sent = agent.seen[1]["input"]
        types = [getattr(i, "type", None) or i.get("type") for i in sent[1:]]
        self.assertEqual(types, ["reasoning", "function_call", "function_call_output"])
        # Fed back as the SDK object, not a lossy model_dump().
        self.assertEqual(sent[1].encrypted_content, "ENC")

    def test_caller_input_list_is_not_mutated(self):
        original = [{"role": "user", "content": "hi"}]
        agent = make_agent([FakeResponse([_message("ok")])])
        loop(agent, items=original)
        self.assertEqual(original, [{"role": "user", "content": "hi"}])


class TestToolDispatch(unittest.TestCase):
    def test_parallel_calls_each_get_a_matching_output(self):
        agent = make_agent([
            FakeResponse([_call("c1", "get_coach_ticks", "{}"),
                          _call("c2", "get_wellness", '{"days_back": 3}')]),
            FakeResponse([_message("done")]),
        ])
        loop(agent)
        outs = [i for i in agent.seen[1]["input"]
                if isinstance(i, dict) and i.get("type") == "function_call_output"]
        self.assertEqual([o["call_id"] for o in outs], ["c1", "c2"])
        self.assertEqual(agent.tools_run,
                         [("get_coach_ticks", {}), ("get_wellness", {"days_back": 3})])

    def test_malformed_arguments_report_back_without_running_the_tool(self):
        agent = make_agent([
            FakeResponse([_call("c1", "get_wellness", "{oops")]),
            FakeResponse([_message("recovered")]),
        ])
        text, _ = loop(agent)
        self.assertEqual(text, "recovered")
        self.assertEqual(agent.tools_run, [])
        out = [i for i in agent.seen[1]["input"]
               if isinstance(i, dict) and i.get("type") == "function_call_output"][0]
        self.assertIn("Error parsing tool arguments", out["output"])

    def test_empty_arguments_become_an_empty_dict(self):
        """Zero-arg tools can send "" rather than "{}"; json.loads("") would raise."""
        agent = make_agent([
            FakeResponse([_call("c1", "get_training_readiness", "")]),
            FakeResponse([_message("ok")]),
        ])
        loop(agent)
        self.assertEqual(agent.tools_run, [("get_training_readiness", {})])


class TestTermination(unittest.TestCase):
    def test_empty_text_raises_instead_of_returning_empty(self):
        agent = make_agent([FakeResponse([_reasoning()])])
        with self.assertRaises(RuntimeError) as ctx:
            loop(agent)
        self.assertIn("returned no text", str(ctx.exception))

    def test_max_iterations_is_enforced_exactly(self):
        agent = make_agent([FakeResponse([_call("c1", "get_coach_ticks", "{}")])])
        with self.assertRaises(RuntimeError) as ctx:
            loop(agent, max_iterations=3)
        self.assertIn("exceeded max iterations (3)", str(ctx.exception))
        self.assertEqual(len(agent.seen), 3)


class TestRequestShape(unittest.TestCase):
    def test_reasoning_effort_and_no_store_or_include(self):
        agent = make_agent([FakeResponse([_message("ok")])])
        loop(agent)
        req = agent.seen[0]
        self.assertEqual(req["tool_choice"], "auto")
        self.assertEqual(req["reasoning"], {"effort": "medium"})
        # We keep OpenAI's default retention, so neither key is sent.
        self.assertNotIn("store", req)
        self.assertNotIn("include", req)

    def test_blank_effort_omits_the_parameter(self):
        agent = make_agent([FakeResponse([_message("ok")])], reasoning_effort="")
        loop(agent)
        self.assertNotIn("reasoning", agent.seen[0])

    def test_effort_is_normalised(self):
        agent = make_agent([FakeResponse([_message("ok")])], reasoning_effort="  HIGH ")
        loop(agent)
        self.assertEqual(agent.seen[0]["reasoning"], {"effort": "high"})


class TestReasoningFallback(unittest.TestCase):
    def test_model_rejecting_reasoning_is_retried_without_it_and_remembered(self):
        ok = FakeResponse([_message("ok")])
        err = Exception("400 - Unsupported parameter: 'reasoning' is not supported "
                        "with this model.")
        agent = make_agent([err, ok])
        text, _ = loop(agent)
        self.assertEqual(text, "ok")
        self.assertIn("reasoning", agent.seen[0])     # tried optimistically
        self.assertNotIn("reasoning", agent.seen[1])  # retried without it
        self.assertIn("gpt-5.6", agent._no_reasoning_models)

        loop(agent)
        self.assertNotIn("reasoning", agent.seen[2])  # now omitted upfront

    def test_unrelated_errors_propagate(self):
        agent = make_agent([Exception("rate limit exceeded")])
        with self.assertRaises(Exception) as ctx:
            loop(agent)
        self.assertIn("rate limit", str(ctx.exception))
        self.assertEqual(agent._no_reasoning_models, set())


class TestChatContract(unittest.TestCase):
    """Guards constraints 1, 3, 4, 6 — the persisted-history shape."""

    def test_chat_appends_exactly_two_json_safe_string_entries(self):
        agent = make_agent([
            FakeResponse([_reasoning(), _call("c1", "get_recent_activities", "{}")]),
            FakeResponse([_message("Solid ride.\n\n[[PROPOSE_REVIEW_POST activity_id=42]]")]),
        ])
        reply, history, usage = agent.chat("how did my last ride go?", [], mode="review")

        self.assertEqual(len(history), 2)
        self.assertEqual([m["role"] for m in history], ["user", "assistant"])
        for m in history:
            self.assertIsInstance(m, dict)
            self.assertIsInstance(m["content"], str)
        # Must survive json.dump into /data/chat_history.json — catches leaked SDK objects.
        json.dumps(history)
        # Marker must reach storage un-stripped, or the buttons vanish on reload.
        self.assertIn("[[PROPOSE_REVIEW_POST activity_id=42]]", history[-1]["content"])
        self.assertEqual(reply, history[-1]["content"])
        self.assertEqual(usage["prompt_tokens"], 20)

    def test_legacy_tool_rows_never_reach_the_model(self):
        """Pre-1.1.49 /data rows contain tool_calls and role:'tool'. Constraint 5."""
        legacy = [
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]},
            {"role": "tool", "tool_call_id": "x", "content": "{}"},
            {"role": "assistant", "content": "earlier answer"},
        ]
        agent = make_agent([FakeResponse([_message("new answer")])])
        agent.chat("new question", legacy, mode="review")

        sent = agent.seen[0]["input"]
        self.assertTrue(all(m["role"] in ("system", "user", "assistant") for m in sent))
        self.assertTrue(all("tool_calls" not in m for m in sent))
        self.assertTrue(all(isinstance(m["content"], str) for m in sent))

    def test_repeat_question_short_circuits_without_calling_the_api(self):
        agent = make_agent([FakeResponse([_message("should not be used")])])
        history = [{"role": "user", "content": "same"},
                   {"role": "assistant", "content": "cached"}]
        reply, _, usage = agent.chat("same", history, mode="review")
        self.assertEqual(reply, "cached")
        self.assertEqual(agent.seen, [])
        self.assertEqual(usage["total_tokens"], 0)


class TestToolSchemas(unittest.TestCase):
    def test_all_tools_use_the_flat_responses_shape(self):
        for t in TOOLS:
            self.assertEqual(t["type"], "function")
            self.assertIn("name", t)
            self.assertIn("parameters", t)
            self.assertIs(t.get("strict"), False, f"{t['name']} must set strict=False")
            self.assertNotIn("function", t, "nested Chat Completions shape left behind")

    def test_every_referenced_tool_name_exists(self):
        names = {t["name"] for t in TOOLS}
        self.assertEqual(len(names), len(TOOLS), "duplicate tool name")
        for s in (_REVIEW_TOOL_NAMES, _HEALTH_TOOL_NAMES, _HEALTH_READINESS_TOOL_NAMES,
                  _PLANNING_TOOL_NAMES, _RECAP_TOOL_NAMES,
                  _WELLNESS_READINESS_TOOL_NAMES, _WELLNESS_TOOL_NAMES):
            self.assertLessEqual(s, names, f"unknown tool name(s): {s - names}")


if __name__ == "__main__":
    unittest.main()
