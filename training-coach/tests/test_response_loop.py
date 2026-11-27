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
    agent._run_tool = lambda n, a, collect=None: (
        agent.tools_run.append((n, a)) or f"RESULT:{n}")
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
                                 "total_tokens": 30, "reasoning_tokens": 6,
                                 "cached_tokens": 0})

    def test_cached_input_tokens_are_counted_separately(self):
        """Most of what this loop re-sends is a prefix the API has already cached.
        Counting it is what lets the daily budget say so."""
        usage = NS(input_tokens=1000, output_tokens=5, total_tokens=1005,
                   output_tokens_details=NS(reasoning_tokens=0),
                   input_tokens_details=NS(cached_tokens=896))
        agent = make_agent([FakeResponse([_message("done")], usage)])
        _, got = loop(agent)
        self.assertEqual(got["cached_tokens"], 896)
        self.assertEqual(got["prompt_tokens"], 1000)

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
    @staticmethod
    def _rejection(status=400):
        err = Exception("400 - Unsupported parameter: 'reasoning' is not supported "
                        "with this model.")
        err.status_code = status
        return err

    def test_model_rejecting_reasoning_is_retried_without_it_and_remembered(self):
        ok = FakeResponse([_message("ok")])
        agent = make_agent([self._rejection(), ok])
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

    def test_a_server_error_mentioning_reasoning_does_not_disable_reasoning(self):
        """It used to: the word anywhere in any exception was enough, and the model
        was then marked as reasoning-incapable for the life of the process."""
        agent = make_agent([self._rejection(status=500)])
        with self.assertRaises(Exception):
            loop(agent)
        self.assertEqual(agent._no_reasoning_models, set())
        self.assertEqual(len(agent.seen), 1)          # no silent retry


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

    def test_get_coach_ticks_takes_no_sport_argument(self):
        """Deliberate: it returns every sport's thresholds and the model matches on
        `sports`. A `sport` parameter would fail silently toward cycling whenever
        the model forgot to pass it — which is the bug this design removes."""
        tool = next(t for t in TOOLS if t["name"] == "get_coach_ticks")
        self.assertEqual(tool["parameters"]["properties"], {})

    def test_every_referenced_tool_name_exists(self):
        names = {t["name"] for t in TOOLS}
        self.assertEqual(len(names), len(TOOLS), "duplicate tool name")
        for s in (_REVIEW_TOOL_NAMES, _HEALTH_TOOL_NAMES, _HEALTH_READINESS_TOOL_NAMES,
                  _PLANNING_TOOL_NAMES, _RECAP_TOOL_NAMES,
                  _WELLNESS_READINESS_TOOL_NAMES, _WELLNESS_TOOL_NAMES):
            self.assertLessEqual(s, names, f"unknown tool name(s): {s - names}")


class TestChatKeepsTheWholeConversation(unittest.TestCase):
    """chat() returned the TRIMMED history, and main.py stores what it returns — so
    every turn threw away everything older than the model's own window and the
    athlete's visible scrollback could never exceed four exchanges."""

    def _history(self, exchanges):
        out = []
        for i in range(exchanges):
            out.append({"role": "user", "content": f"q{i}"})
            out.append({"role": "assistant", "content": f"a{i}"})
        return out

    def test_older_turns_survive_a_new_one(self):
        agent = make_agent([FakeResponse([_message("fresh")])])
        history = self._history(10)
        _reply, new_history, _usage = agent.chat("q10", history, mode="review")
        self.assertEqual(len(new_history), 22)
        self.assertEqual(new_history[0]["content"], "q0")
        self.assertEqual(new_history[-1]["content"], "fresh")

    def test_the_model_still_sees_only_the_recent_window(self):
        agent = make_agent([FakeResponse([_message("fresh")])])
        agent.chat("q10", self._history(10), mode="review")
        sent = [m for m in agent.seen[0]["input"] if m.get("role") != "system"]
        self.assertEqual(len(sent), 7)                  # 3 exchanges + the question
        self.assertEqual(sent[0]["content"], "q7")

    def test_a_repeated_question_is_answered_from_the_record(self):
        """Re-sending because a slow reply looked like a failure must not re-bill."""
        agent = make_agent([FakeResponse([_message("should not be called")])])
        history = [{"role": "user", "content": "same"},
                   {"role": "assistant", "content": "cached answer"}]
        reply, _history, usage = agent.chat("same", history, mode="review")
        self.assertEqual(reply, "cached answer")
        self.assertEqual(usage["total_tokens"], 0)
        self.assertEqual(agent.seen, [])


class TestMemoIsATool(unittest.TestCase):
    def test_a_recorded_memo_is_folded_back_into_the_reply(self):
        from agent import TrainingAgent
        agent = make_agent([
            FakeResponse([_call("c1", "record_memo",
                                json.dumps({"text": "Held 3x12 at the top of the band."}))]),
            FakeResponse([_message("Good session.")]),
        ])
        agent._run_tool = TrainingAgent._run_tool.__get__(agent)
        collected = {}
        text, _usage = agent._run_response_loop(
            model="gpt-5.6", input_items=[{"role": "user", "content": "review"}],
            tools=TOOLS, max_iterations=5, label="t", collect=collected)
        self.assertEqual(collected["memo"], "Held 3x12 at the top of the band.")
        self.assertIn("[[MEMO Held 3x12 at the top of the band.]]",
                      agent._with_memo(text, collected))

    def test_a_marker_the_model_wrote_itself_is_not_duplicated(self):
        agent = make_agent([FakeResponse([_message("x")])])
        text = "Good session.\n[[MEMO already written]]"
        self.assertEqual(agent._with_memo(text, {"memo": "second one"}), text)


class TestAutoReviewToolSurface(unittest.TestCase):
    def test_the_nightly_review_cannot_touch_the_calendar(self):
        """It used to be handed the full tool list, including delete_planned_workout."""
        from agent import _AUTO_REVIEW_TOOL_NAMES
        forbidden = {"create_planned_workout", "update_planned_workout",
                     "delete_planned_workout", "write_weekly_note"}
        self.assertEqual(_AUTO_REVIEW_TOOL_NAMES & forbidden, set())
        self.assertIn("post_activity_comment", _AUTO_REVIEW_TOOL_NAMES)
        self.assertIn("record_memo", _AUTO_REVIEW_TOOL_NAMES)

    def test_the_recap_cannot_call_a_tool_that_tells_it_nothing(self):
        """get_planned_workouts cannot return past events, so it could not inform a
        recap of the week behind it — the prompt said so and asked for it anyway."""
        self.assertNotIn("get_planned_workouts", _RECAP_TOOL_NAMES)


class TestAlertToken(unittest.TestCase):
    """is_alert was content.lstrip().startswith("[ALERT]"), so one stray character
    turned a poor-readiness morning into a silent one."""

    def test_the_token_is_found_through_markdown_and_quoting(self):
        from agent import _alert_requested
        for body in ("[ALERT]\nHRV is suppressed.",
                     "**[ALERT]**\nHRV is suppressed.",
                     '"[ALERT]" — sleep was poor',
                     "> [ALERT]",
                     "# [ALERT]",
                     "\n\n  [ALERT] rest today"):
            with self.subTest(body=body):
                self.assertTrue(_alert_requested(body))

    def test_ok_is_not_an_alert(self):
        from agent import _alert_requested
        for body in ("[OK]\nAll normal.", "**[OK]** all normal", "", None,
                     "Readiness is fine today."):
            with self.subTest(body=body):
                self.assertFalse(_alert_requested(body))

    def test_the_first_token_wins(self):
        from agent import _alert_requested
        self.assertFalse(_alert_requested("[OK]\nNothing to see.\n[ALERT] ignore me"))


class TestLoopCeilings(unittest.TestCase):
    def test_a_token_ceiling_stops_a_loop_an_iteration_count_would_not(self):
        from agent import MAX_LOOP_TOKENS
        big = NS(input_tokens=MAX_LOOP_TOKENS + 1, output_tokens=1,
                 total_tokens=MAX_LOOP_TOKENS + 2,
                 output_tokens_details=NS(reasoning_tokens=0))
        agent = make_agent([FakeResponse([_call("c1", "get_coach_ticks", "{}")], big)])
        with self.assertRaises(RuntimeError) as ctx:
            loop(agent, max_iterations=50)
        self.assertIn("ceiling", str(ctx.exception))

    def test_an_output_cap_is_always_sent(self):
        from agent import MAX_OUTPUT_TOKENS
        agent = make_agent([FakeResponse([_message("ok")])])
        loop(agent)
        self.assertEqual(agent.seen[0]["max_output_tokens"], MAX_OUTPUT_TOKENS)


class TestPromptShape(unittest.TestCase):
    def test_the_volatile_block_is_last_in_every_prompt(self):
        """Prompt caching matches on a prefix, so the memory block — which changes
        every sweep — must not sit above the static rules."""
        import agent as mod
        names = [n for n in dir(mod) if n.endswith(("_SYSTEM_PROMPT", "_PROMPT"))]
        self.assertGreaterEqual(len(names), 8)
        for name in names:
            text = getattr(mod, name)
            with self.subTest(prompt=name):
                self.assertTrue(text.rstrip().endswith("{memory_context}"), name)

    def test_the_metric_reference_exists_once(self):
        import agent as mod
        names = [n for n in dir(mod) if n.endswith(("_SYSTEM_PROMPT", "_PROMPT"))]
        copies = sum(1 for n in names if "polarization_index" in getattr(mod, n))
        self.assertEqual(copies, 2)   # the two ride-analysis prompts share one source
        self.assertIn("polarization_index", mod._METRIC_REFERENCE)



class TestEveryPromptStillFormats(unittest.TestCase):
    """A missing or renamed format key is a KeyError at request time, on a path with
    no offline cover otherwise: the prompts are built from fragments now."""

    def _agent(self, **kw):
        return TrainingAgent(openai_api_key="test", intervals_athlete_id="a1",
                             intervals_api_key="k1", **kw)

    def test_the_three_chat_modes_build(self):
        agent = self._agent()
        for mode in ("review", "health", "planning"):
            with self.subTest(mode=mode):
                built = agent._build_system(mode)
                self.assertNotIn("{", built.replace("{memory_context}", ""))
                self.assertTrue(built.strip())

    def test_the_readiness_variant_builds(self):
        agent = self._agent(readiness_entity="sensor.readiness")
        built = agent._build_system("health")
        self.assertIn("training readiness", built)
        self.assertNotIn("{", built)

    def test_the_automated_prompts_build_with_their_own_kwargs(self):
        """Mirrors weekly_recap() and wellness_check() exactly: same keys, same
        values, so a drifted key fails here rather than on a Monday morning."""
        from agent import (WEEKLY_RECAP_SYSTEM_PROMPT, WELLNESS_CHECK_SYSTEM_PROMPT,
                           WELLNESS_CHECK_READINESS_PROMPT, AUTO_REVIEW_SYSTEM_PROMPT,
                           group_ride_flag_context, group_ride_intensity_context)
        agent = self._agent()
        cases = [
            (WEEKLY_RECAP_SYSTEM_PROMPT, dict(
                today="2026-11-25", memory_context="M", max_hours=10, max_tss=700,
                hrv_context="H", rhr_context="R", hard_intervals_per_week=3,
                group_ride_context=group_ride_intensity_context("moderate"))),
            (WELLNESS_CHECK_SYSTEM_PROMPT, dict(
                today="2026-11-25", memory_context="M", hrv_context="H",
                rhr_context="R", hrv_min=48, hrv_max=69, rhr_max=54)),
            (WELLNESS_CHECK_READINESS_PROMPT, dict(
                today="2026-11-25", memory_context="M")),
            (AUTO_REVIEW_SYSTEM_PROMPT, dict(
                today="2026-11-25", memory_context="M",
                group_ride_context=group_ride_flag_context("moderate"))),
        ]
        for template, kwargs in cases:
            with self.subTest(prompt=template[:40]):
                built = template.format(**kwargs)
                self.assertNotIn("{", built)
                self.assertTrue(built.rstrip().endswith("M"))
        del agent


class TestTextArrivingWithACall(unittest.TestCase):
    """The reported failure: "model returned no text (status=completed)".

    One reply can carry both a message and a function_call. The loop read the text
    only when there were no calls, so an answer written in the same reply as
    propose_review_post was discarded — and the model, having already said
    everything, returned an empty reply that the loop reported as an error."""

    REVIEW = "Three reps at 270 W, all inside the band, fade -0.4%."

    def _real_dispatch(self, agent):
        from agent import TrainingAgent
        agent._run_tool = TrainingAgent._run_tool.__get__(agent)
        return agent

    def test_a_review_offered_in_the_same_reply_is_not_lost(self):
        agent = self._real_dispatch(make_agent([
            FakeResponse([_message(self.REVIEW),
                          _call("c1", "propose_review_post",
                                json.dumps({"activity_id": "i172850190", "tick": 4}))]),
        ]))
        reply, _history, _usage = agent.chat("in-depth review please", [], mode="review")
        self.assertIn(self.REVIEW, reply)
        self.assertIn("[[PROPOSE_REVIEW_POST activity_id=i172850190 tick=4]]", reply)
        self.assertEqual(len(agent.seen), 1, "no second round trip is needed")

    def test_a_memo_in_the_same_reply_costs_no_extra_call(self):
        agent = self._real_dispatch(make_agent([
            FakeResponse([_message(self.REVIEW),
                          _call("c1", "record_memo", json.dumps({"text": "Held the band."}))]),
        ]))
        collected = {}
        text, _usage = agent._run_response_loop(
            model="gpt-5.6", input_items=[{"role": "user", "content": "review"}],
            tools=TOOLS, max_iterations=5, label="t", collect=collected)
        self.assertEqual(text, self.REVIEW)
        self.assertEqual(collected["memo"], "Held the band.")
        self.assertEqual(len(agent.seen), 1)
        self.assertIn("[[MEMO Held the band.]]", agent._with_memo(text, collected))

    def test_a_real_tool_call_beside_text_still_continues(self):
        """The short-circuit must not swallow work the model still owes."""
        agent = make_agent([
            FakeResponse([_message("Let me look at the intervals."),
                          _call("c1", "get_activity_intervals",
                                json.dumps({"activity_id": "i1"}))]),
            FakeResponse([_message("Final answer.")]),
        ])
        text, _usage = loop(agent)
        self.assertEqual(text, "Final answer.")
        self.assertEqual(len(agent.seen), 2)
        self.assertEqual(agent.tools_run[0][0], "get_activity_intervals")

    def test_earlier_text_is_returned_when_the_last_reply_is_empty(self):
        """The belt to the short-circuit's braces: a mixed call set, then nothing."""
        agent = make_agent([
            FakeResponse([_message("The analysis."),
                          _call("c1", "get_coach_ticks", "{}")]),
            FakeResponse([]),
        ])
        text, _usage = loop(agent)
        self.assertEqual(text, "The analysis.")

    def test_a_reply_with_nothing_at_all_still_raises_and_says_what_came_back(self):
        agent = make_agent([FakeResponse([_reasoning()])])
        with self.assertRaises(RuntimeError) as ctx:
            loop(agent)
        message = str(ctx.exception)
        self.assertIn("returned no text", message)
        self.assertIn("reasoning", message, "the error must name what it did receive")


if __name__ == "__main__":
    unittest.main()
