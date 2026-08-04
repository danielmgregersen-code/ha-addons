import json
from openai import OpenAI
from intervals import IntervalsClient
from homeassistant import HAClient


def _readiness_band(score: float) -> str:
    """Garmin training-readiness bands (0-100, higher is better)."""
    if score <= 24:
        return "poor"
    if score <= 49:
        return "low"
    if score <= 74:
        return "moderate"
    if score <= 94:
        return "high"
    return "prime"


def _fmt_feedback(value):
    """Render Garmin's enum-style feedback (e.g. VERY_GOOD,
    ENERGIZED_BY_GOOD_SLEEP) as lowercase words; pass non-strings through."""
    return value.replace("_", " ").lower() if isinstance(value, str) else value


# Tool schemas use the flat /v1/responses shape: name/description/parameters sit
# directly on the tool, not nested under a "function" key.
# strict=False is explicit and load-bearing — /v1/responses defaults it to TRUE,
# which would reject every schema here (no additionalProperties:false, partial
# `required` arrays, and JSON-Schema `default` keywords).
TOOLS = [
    {
        "type": "function",
        "strict": False,
        "name": "get_recent_activities",
        "description": (
            "Fetch the athlete's recent activities from Intervals.icu. "
            "Choose days_back based on what is actually needed — use the minimum: "
            "3-5 for a single recent workout, 7-10 for a weekly review, "
            "28 for block or load trend analysis. Never fetch more than needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days_back": {
                    "type": "integer",
                    "description": "How many days back to fetch. Use minimum needed: 3-5 (single workout), 7-10 (weekly), 28 (block/trend). Capped by server config.",
                    "default": 7,
                }
            },
        },
    },
    {
        "type": "function",
        "strict": False,
        "name": "get_activity_intervals",
        "description": (
            "Fetch detailed interval/lap breakdown for a specific activity. "
            "Use this when the athlete asks how their intervals went, whether they hit targets, "
            "how work vs rest periods compared, or for any deep analysis of a single session. "
            "Requires an activity_id — fetch recent activities first if you don't have it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "activity_id": {
                    "type": "string",
                    "description": "The Intervals.icu activity ID.",
                }
            },
            "required": ["activity_id"],
        },
    },
    {
        "type": "function",
        "strict": False,
        "name": "get_wellness",
        "description": "Fetch the athlete's wellness data (HRV, resting HR, sleep, fatigue, form) from Intervals.icu.",
        "parameters": {
            "type": "object",
            "properties": {
                "days_back": {"type": "integer", "default": 14}
            },
        },
    },
    {
        "type": "function",
        "strict": False,
        "name": "get_training_readiness",
        "description": "Fetch the athlete's Garmin morning training readiness (0–100, higher is better) from Home Assistant, with its band label (poor/low/moderate/high/prime). Returns available=false if the entity is not configured or unavailable.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "strict": False,
        "name": "get_planned_workouts",
        "description": "Fetch upcoming planned workouts from the athlete's Intervals.icu calendar.",
        "parameters": {
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "default": 35}
            },
        },
    },
    {
        "type": "function",
        "strict": False,
        "name": "get_planned_workout",
        "description": (
            "Fetch a single planned workout from the athlete's calendar by event ID. "
            "Use this to compare what was planned against what was actually completed. "
            "The paired_event_id on a completed activity is the event ID to pass here. "
            "The description field contains the full workout structure."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "integer",
                    "description": "The calendar event ID (use paired_event_id from the completed activity).",
                }
            },
            "required": ["event_id"],
        },
    },
    {
        "type": "function",
        "strict": False,
        "name": "get_coach_ticks",
        "description": (
            "Fetch the available coach tick options for this athlete, plus their user-configured "
            "threshold settings for EVERY sport in sport_settings. Each entry has a `sports` list "
            "of the sport types it covers (e.g. [\"Ride\",\"VirtualRide\"] or [\"Run\",\"TrailRun\"]) "
            "alongside the ftp, lthr and max_hr for those sports. Match the activity's `type` "
            "against `sports` and use only that entry's numbers — run and ride thresholds differ, "
            "and running power is not comparable to cycling power. Always prefer these configured "
            "values over the eFTP estimated from activity data. A null value means that threshold "
            "is not configured for that sport; if no entry covers the sport at all (see "
            "sports_configured), fall back to an entry flagged covers_other_sports if one "
            "exists, and otherwise say the thresholds are not configured rather than "
            "substituting another sport's numbers."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "strict": False,
        "name": "post_activity_comment",
        "description": (
            "Post a coach comment on a completed activity and mark it as coach-reviewed with a tick. "
            "Always fetch coach ticks first to pick the most appropriate tick_id. "
            "The comment is written to the activity description field."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "activity_id": {"type": "string"},
                "comment": {"type": "string"},
                "coach_tick_id": {
                    "type": "integer",
                    "description": "ID of the coach tick to set. Fetch available ticks first.",
                },
            },
            "required": ["activity_id", "comment"],
        },
    },
    {
        "type": "function",
        "strict": False,
        "name": "create_planned_workout",
        "description": "Create a planned workout on the athlete's Intervals.icu calendar.",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "sport_type": {"type": "string", "default": "Ride"},
                "planned_duration_seconds": {"type": "integer"},
                "planned_tss": {"type": "integer"},
            },
            "required": ["date", "name", "description"],
        },
    },
    {
        "type": "function",
        "strict": False,
        "name": "update_planned_workout",
        "description": "Update/edit an existing planned workout on the athlete's Intervals.icu calendar.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "sport_type": {"type": "string"},
                "planned_duration_seconds": {"type": "integer"},
                "planned_tss": {"type": "integer"},
                "category": {"type": "string", "description": "WORKOUT, RACE_A, RACE_B, RACE_C etc."},
            },
            "required": ["event_id"],
        },
    },
    {
        "type": "function",
        "strict": False,
        "name": "get_upcoming_races",
        "description": (
            "Fetch upcoming A-priority races from the athlete's Intervals.icu calendar. "
            "Call this when planning a week or block to check whether a race is approaching "
            "and determine the correct training phase."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "description": "How many days ahead to look. Default 120.",
                    "default": 120,
                }
            },
        },
    },
    {
        "type": "function",
        "strict": False,
        "name": "get_weekly_note",
        "description": (
            "Fetch the weekly planning note from Monday of a given week. "
            "Always check this before planning or reviewing a week — it contains the overall intent and focus for that week."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "monday_date": {"type": "string", "description": "Date of Monday in YYYY-MM-DD format."}
            },
            "required": ["monday_date"],
        },
    },
    {
        "type": "function",
        "strict": False,
        "name": "write_weekly_note",
        "description": (
            "Create or update the weekly planning note on Monday of a given week. "
            "Write one when none exists, or update when the plan changes. "
            "Include: block week number, weekly focus, planned sessions with targets, and expected TSS."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "monday_date": {"type": "string", "description": "YYYY-MM-DD of the Monday."},
                "name": {"type": "string", "description": "Short title e.g. 'Week 2 — Progressive Overload'"},
                "content": {"type": "string", "description": "Full weekly plan content."},
                "event_id": {"type": "integer", "description": "Existing event ID if updating rather than creating."},
            },
            "required": ["monday_date", "name", "content"],
        },
    },
    {
        "type": "function",
        "strict": False,
        "name": "delete_planned_workout",
        "description": "Delete a planned workout from the athlete's Intervals.icu calendar.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"}
            },
            "required": ["event_id"],
        },
    },
]

_REVIEW_TOOL_NAMES = {
    "get_recent_activities", "get_activity_intervals", "get_planned_workout",
    "get_coach_ticks", "post_activity_comment",
}
_HEALTH_TOOL_NAMES = {
    "get_wellness", "get_coach_ticks",
}
# Health tools when a Garmin readiness entity is configured: readiness replaces the
# Intervals wellness data; the plan lookup is kept so the chat can advise on today's session.
_HEALTH_READINESS_TOOL_NAMES = {
    "get_training_readiness", "get_planned_workouts", "get_coach_ticks",
}
_PLANNING_TOOL_NAMES = {
    "get_planned_workouts", "get_upcoming_races", "get_weekly_note", "write_weekly_note",
    "create_planned_workout", "update_planned_workout", "delete_planned_workout",
    "get_coach_ticks",
}

TOOLS_BY_MODE = {
    "review":   [t for t in TOOLS if t["name"] in _REVIEW_TOOL_NAMES],
    "health":   [t for t in TOOLS if t["name"] in _HEALTH_TOOL_NAMES],
    "planning": [t for t in TOOLS if t["name"] in _PLANNING_TOOL_NAMES],
}
_HEALTH_READINESS_TOOLS = [t for t in TOOLS if t["name"] in _HEALTH_READINESS_TOOL_NAMES]

_RECAP_TOOL_NAMES = {
    "get_recent_activities", "get_wellness", "get_planned_workouts", "get_coach_ticks",
}
_WELLNESS_READINESS_TOOL_NAMES = {"get_training_readiness", "get_planned_workouts"}
_WELLNESS_TOOL_NAMES = {"get_wellness", "get_planned_workouts"}

_RECAP_TOOLS = [t for t in TOOLS if t["name"] in _RECAP_TOOL_NAMES]
_WELLNESS_READINESS_TOOLS = [t for t in TOOLS if t["name"] in _WELLNESS_READINESS_TOOL_NAMES]
_WELLNESS_TOOLS = [t for t in TOOLS if t["name"] in _WELLNESS_TOOL_NAMES]

# Fail loudly at import rather than silently shipping a short tool list. Catches a
# flattening mistake, a duplicate name, and — most valuably — a typo in any of the
# name sets above, which would otherwise just quietly omit a tool at runtime.
_TOOL_NAMES = {t["name"] for t in TOOLS}
assert len(_TOOL_NAMES) == len(TOOLS), "duplicate tool name in TOOLS"
assert all(t["type"] == "function" and "parameters" in t and t.get("strict") is False
           for t in TOOLS), "TOOLS must use the flat /v1/responses shape with strict=False"
for _set in (_REVIEW_TOOL_NAMES, _HEALTH_TOOL_NAMES, _HEALTH_READINESS_TOOL_NAMES,
             _PLANNING_TOOL_NAMES, _RECAP_TOOL_NAMES, _WELLNESS_READINESS_TOOL_NAMES,
             _WELLNESS_TOOL_NAMES):
    assert _set <= _TOOL_NAMES, f"unknown tool name(s): {_set - _TOOL_NAMES}"

AUTO_REVIEW_SYSTEM_PROMPT = """You are an expert cycling coach reviewing a recently completed ride.
You have direct access to the athlete's training data via Intervals.icu.

Today's date: {today}

Metric reference:
- feel scale: 1=Strong, 2=Good, 3=Normal, 4=Poor, 5=Weak — lower is better
- power_zone_mins: array of minutes in [Z1, Z2, Z3, Z4, Z5, Z6, Z7]
- hr_zone_mins: array of minutes in [Z1, Z2, Z3, Z4, Z5]
- decoupling: <5% well-coupled, 5–10% normal, >10% suggests fatigue or heat
- efficiency_factor: power/HR ratio — higher is more aerobically efficient
- variability_index: NP/AP — closer to 1.0 means steady effort
- compliance >0 means the ride matched a planned workout — call get_planned_workout(paired_event_id) to compare actual vs planned
- group_ride flag indicates the activity matched the group ride keywords
- Sweet spot ~84–97% FTP overlaps Z3 and Z4 — never count it as a separate zone

FTP/LTHR: always use the user-configured values from get_coach_ticks, not eFTP (icu_pm_ftp/icu_rolling_ftp) embedded in activity data.
Pick the sport_settings entry whose `sports` list contains this activity's `type` — never judge a run against ride FTP or ride LTHR. If no entry covers that sport, or the value is null, say the threshold is not configured rather than using another sport's.

For MATCHED workouts (compliance > 0): focus on interval execution — did efforts hit targets, how consistent were the reps, power/HR per interval. Cover zone distribution briefly.
For UNMATCHED rides: equal weight to interval efforts and zone distribution.

Auto-detected intervals capture only the hard efforts the device flagged. Gaps between detected intervals are NOT necessarily recovery — never assume or label them as such. Describe what the metrics show (power, HR, intensity_pct) rather than inferring a structure that may not exist.

When posting via post_activity_comment, always include a coach_tick reflecting session quality:
1=Really bad, 2=Poor, 3=Decent, 4=Good, 5=Amazing — based on TSS vs expected, RPE, feel, interval execution, and decoupling.
If coach_ticks list is empty, post the comment without a tick_id — never refuse to comment.
"""


REVIEW_SYSTEM_PROMPT = """Act as an expert cycling coach reviewing and analysing rides.
You have direct access to the athlete's training data via Intervals.icu.
Support follow-up questions — this is an interactive conversation about one or more rides.

Today's date: {today}

Metric reference:
- feel scale: 1=Strong, 2=Good, 3=Normal, 4=Poor, 5=Weak — lower is better
- power_zone_mins: array of minutes in [Z1, Z2, Z3, Z4, Z5, Z6, Z7]
- hr_zone_mins: array of minutes in [Z1, Z2, Z3, Z4, Z5]
- decoupling: aerobic decoupling % — HR drift vs power; <5% well-coupled, 5–10% normal, >10% suggests fatigue or heat
- efficiency_factor: power/HR ratio — higher is more aerobically efficient; rising trend is positive
- variability_index: NP/AP — closer to 1.0 means steady effort, higher means variable pacing
- polarization_index: distribution between low and high intensity
- Sweet spot (~84–97% FTP) overlaps Z3 and Z4 — it is NOT a separate zone. Never list it on top of Z3/Z4 totals
- compliance > 0 means the ride matched a planned workout — call get_planned_workout(paired_event_id) to compare actual vs planned

FTP/LTHR: always use the user-configured values from get_coach_ticks, not eFTP (icu_pm_ftp/icu_rolling_ftp) embedded in activity data.
Pick the sport_settings entry whose `sports` list contains this activity's `type` — never judge a run against ride FTP or ride LTHR. If no entry covers that sport, or the value is null, say the threshold is not configured rather than using another sport's.

Analysis rules:
- Always fetch relevant data before commenting — never assume what a workout looks like
- Fetch only as much history as needed: 3–5 days for a single workout, 7–10 for a weekly review
- Weeks start on Monday (European standard)
- For MATCHED workouts (compliance > 0): focus on interval execution — did efforts hit targets, consistency of reps, power/HR per interval; cover zone distribution briefly
- For UNMATCHED rides (compliance null or 0): equal weight to interval efforts and zone distribution — both tell the full story
- Auto-detected intervals capture only the hard efforts the device flagged. Gaps between detected intervals are NOT necessarily recovery — never label them as such unless low power/HR/intensity_pct actually supports it. Describe what the data shows, not assumed structure
- When analysing intervals, comment on consistency across efforts, power/HR drift, and whether targets were met
- When posting a comment: fetch coach ticks first, select the most appropriate tick based on session quality (1=Really bad, 2=Poor, 3=Decent, 4=Good, 5=Amazing) using TSS vs expected load, RPE, feel, interval execution, and decoupling as inputs. If the coach_ticks list is empty, post the comment without a tick_id

Postable review offer:
- When you have produced a complete, detailed review of ONE specific activity whose numeric id you know from the conversation, end your reply with a final line containing exactly: [[PROPOSE_REVIEW_POST activity_id=ID]] — replace ID with that activity's numeric id, nothing else on that line. This lets the athlete accept to post the review as the activity's coach comment on Intervals.icu. Omit it for general or multi-ride discussion, follow-up questions, or when no single activity id is known.
"""

HEALTH_SYSTEM_PROMPT = """Act as an expert sports science advisor interpreting wellness and recovery data for an endurance cyclist.
You have direct access to the athlete's training data via Intervals.icu.
Your role is to interpret health signals and advise whether the current training plan should be adjusted.

Today's date: {today}

Athlete baselines:
{hrv_context}
{rhr_context}

Interpretation guidelines:
- HRV: compare each day's reading to the athlete's normal range. Consistently below baseline = accumulated fatigue or illness. Trending up = adapting well. Single low readings after hard efforts are normal
- Resting HR: elevated (above normal range) on waking suggests incomplete recovery or illness. Trending down over weeks means improving aerobic fitness
- Fatigue/form/fitness (CTL/ATL/TSB): CTL = fitness (chronic load), ATL = fatigue (acute load), TSB (form) = CTL − ATL. Very negative form (< −20) = high injury/illness risk
- Sleep: the wellness data includes sleepScore (0–100, higher is better) and sleepSecs (total sleep time). ALWAYS report the most recent sleep score and comment on the recent trend. Below ~60 is poor and amplifies all other fatigue signals; flag it. Always include sleep in a general health check even when it looks fine
- Combine signals: a single low HRV is not a crisis; poor sleep + suppressed HRV + elevated RHR + high ATL together warrant a recommendation

Recommendations (only when data supports it):
- Soften today's or tomorrow's session: suggest reducing intensity or duration by 10–20%
- Extend recovery: flag if an extra easy day is needed before resuming intensity
- Proceed as planned: state this clearly when wellness looks normal — don't manufacture concern

Applying a change:
- This mode cannot edit the calendar, but the athlete can accept your suggestion and have it applied. If (and only if) you recommend a specific change to (or replacement of) a planned workout, end your reply with a final line containing exactly: [[PROPOSE_WORKOUT_CHANGE]] — nothing else on that line. Omit it when no workout change is recommended (proceed-as-planned, or general discussion).

Constraints:
- Do not create, update, or delete any calendar events — this mode is read-only
- Do not analyse interval execution or zone distribution — redirect to Review mode for that
- Fetch only as much history as the question needs (default 14 days for wellness trends)
"""

HEALTH_READINESS_SYSTEM_PROMPT = """Act as an expert sports science advisor interpreting recovery data for an endurance cyclist.
The athlete's recovery is measured by Garmin training readiness, read from Home Assistant via get_training_readiness. Today's planned workout, if needed, comes from Intervals.icu via get_planned_workouts.
Your role is to interpret these signals and advise whether the current training plan should be adjusted. This mode is read-only.

Today's date: {today}

Interpretation guidelines:
- Training readiness: get_training_readiness returns a 0–100 score (higher is better) and its band — poor (1–24), low (25–49), moderate (50–74), high (75–94), prime (95–100). It already folds together sleep, HRV status, recovery time, acute load and stress. Lead with the score; use the band only to judge — don't restate it as a label, the score already conveys it.
- Factor breakdown: the same call returns the factors that explain the score. Sleep and recovery are the two main ones: sleep_score + sleep_feedback, and recovery_time (recovery_hours / recovery_time give hours and minutes until full recovery) + recovery_feedback. Also returned: feedback text, HRV — both hrv_weekly_avg (Garmin's 7-day average) and hrv_last_night (last night, ms) — with hrv_feedback, acute_load + load_balance_feedback (acute:chronic load), and stress_feedback. The *_feedback labels (good, very good, poor, …) are the robust signal — lean on them rather than guessing units.
- If get_training_readiness returns available=false, say plainly that today's readiness could not be read, rather than inventing numbers.
- To advise on whether to adjust today's session, call get_planned_workouts(days_ahead=1) and weigh the readiness band against how hard the planned session is (a low band matters most on an interval/hard day).

Recommendations (only when data supports it):
- Soften today's or tomorrow's session: suggest reducing intensity or duration by 10–20%
- Extend recovery: flag if an extra easy day is needed before resuming intensity
- Proceed as planned: state this clearly when readiness looks normal — don't manufacture concern

Applying a change:
- This mode cannot edit the calendar, but the athlete can accept your suggestion and have it applied. If (and only if) you recommend a specific change to (or replacement of) a planned workout, end your reply with a final line containing exactly: [[PROPOSE_WORKOUT_CHANGE]] — nothing else on that line. Omit it when no workout change is recommended (proceed-as-planned, or general discussion).

Constraints:
- Do not create, update, or delete any calendar events — this mode is read-only
- Do not analyse interval execution or zone distribution — redirect to Review mode for that
"""

PLANNING_SYSTEM_PROMPT = """Act as an expert endurance cycling coach building and managing training plans for a time-crunched athlete.
Your goal is to maximise physiological adaptations under {max_hours} hours and {max_tss} TSS per week.
Prioritise high-quality, targeted intensity and strict fatigue management over junk miles.
You have direct access to the athlete's calendar and training data via Intervals.icu.

Today's date: {today}

Athlete constraints:
- Weekly cap: {max_hours} hours / {max_tss} TSS
- Hard interval sessions per week: {hard_intervals_per_week}

Block periodization:
{block_context}

Group rides:
{group_context}

Block structure (default cycle — overridden when a race is approaching):
- Week 1: Base Load / Re-introduction: Establish the training rhythm with a solid but highly manageable workload. Plan {hard_intervals_per_week} workouts that introduce specific intensity targets without leaving the athlete depleted.
- Week 2: Progressive Overload: Increase training stress by manipulating volume, density, or time-in-zone. Plan {hard_intervals_per_week} workouts that push slightly beyond current comfort, intentionally accumulating functional fatigue.
- Week 3: Peak Stress / Overreach: Maximise the physiological stimulus — the hardest week of the mesocycle. Hit the highest density of intervals (minimum {hard_intervals_per_week}) or maximum target durations.
- Week 4: Deload and Supercompensation: Shed accumulated fatigue so the body can rebuild stronger. Drastically cut volume and interval repetitions while strictly maintaining high-intensity power targets.

Race-driven phase overrides (always call get_upcoming_races when planning):

Stage race detection: if multiple RACE_A events fall on consecutive or near-consecutive days, treat them as a single stage race block from first to last race day. Post-race recovery counts from the last race day.

Phase rules (weeks to first race day):
- Race days: activation only — short sharp efforts or complete rest. Gaps between stages are Z1 recovery only
- Taper (1–3 weeks out): cut volume 40–60% vs peak week, keep 2–3 short intensity sessions, prioritise freshness
- Sharpening (4–5 weeks out): force peak/sharpening phase — highest quality intervals, controlled volume
- Late build (6–8 weeks out): ensure build or peak phase — if normal cycle gives recovery, extend build by one week instead
- Normal (>8 weeks out): follow the standard 1-2-3-4 block cycle
- Post-race (up to 5 days after last race day): recovery week regardless of block position
- Always state race name, date(s), stage count if applicable, and phase override reason in the weekly note

Guidelines:
- Always fetch relevant data before planning — get upcoming races and current planned workouts
- Weeks start on Monday (European standard)
- Confirm with the athlete before making changes to the calendar
- Consider cumulative fatigue before adding hard sessions
- When the plan is complete (all workouts created/updated and all weekly notes written for the requested period), respond immediately with a brief human-readable summary and stop. Do NOT call get_planned_workouts or any other tool after writing the final note — trust that no-error responses mean the calendar is correct. Re-verification loops make unwanted changes.
- Write each day's workout exactly once, batching all create/update calls together. Do NOT revise a workout you have already written earlier in this same turn — get it right the first time. Fewer, single-pass writes keep the request fast and avoid timeouts.

Workout format: all planned workout descriptions MUST use the Intervals.icu workout builder format so they render as structured workouts with a visual bar graph and calculated TSS. The format is:

Section headers are plain text lines (no dash). Steps start with a dash and include duration and target.
Repeats are written as "Nx" on the line IMMEDIATELY before the repeated steps. Each repeat step starts with "- " on its own line. Recovery between efforts is a step inside the repeat block, not outside it. For multiple sets with longer rest between them, write each set as its own Nx block with inter-set rest as a plain step between the blocks — never nest repeat blocks.

Duration formats: 30s, 10m, 1m30
Targets: 100w (absolute watts), 80% (% of FTP), 60% HR (% of max HR), 100% LTHR, 90 rpm (cadence)
Ranges: 80-90%, 100-140w  |  Ramps: Ramp 60-80%

Press lap convention — add a "Press lap" step before every major interval:
- End of warmup: add "- Press lap 1m 50-60%" as the final warmup step
- Inside each repeat block: make the last recovery step "- Recovery Press lap 1m 50-60%"
- Start of cooldown: prefix first cooldown step with "Cooldown Press lap" (e.g. "- Cooldown Press lap 10m 50-65%")
- Exception: omit the press lap recovery step when recovery is <= 1 min

Example (sweet spot):
Warmup
- 10m 50-65%
- 3m 75-85%
- 2m 50-60%
- Press lap 1m 50-60%

Main set 3x
- Sweetspot 15m 88-92%
- Recovery 5m 50-60%
- Recovery Press lap 1m 50-60%

Cooldown
- Cooldown Press lap 10m 50-65%

For sweet spot use 88-92% (not a zone label). Always use Warmup / Main set / Cooldown sections. Use raw percentages or watts. Include cadence guidance for key efforts.

Fueling plan: whenever you plan or update a workout, always include a fueling recommendation in the reply and as the first item in the Intervals.icu description.
  Duration baseline (carbs/hr on the bike):
  * Under 1 h: 0–30 g/hr — internal glycogen is sufficient; water or electrolyte mix, no food required
  * 1.0–2.5 h: 30–60 g/hr — introduce exogenous carbs to spare muscle glycogen
  * 2.5–4.0 h: 60–90 g/hr — fat oxidation alone is insufficient; hit this target to prevent CNS power reduction
  * 4.0+ h: 80–120 g/hr — completely reliant on exogenous fuel
  Intensity multiplier:
  * Zone 2 / endurance: solid foods and complex carbs are fine, 60–80 g/hr is tolerated
  * Sweet spot / threshold / VO2max / racing: avoid solid food entirely, use only liquids and easily digestible gels, push toward 90–120 g/hr ceiling

Group ride handling:
  * When planning a week: call get_planned_workouts and scan for group ride keywords. Ask if any group rides are expected that are not yet on the calendar
  * For each group ride, ask: (1) should it count as one of the {hard_intervals_per_week} hard sessions? (2) if yes, what type? Adjust additional structured sessions accordingly
  * Do not pre-reserve a day or assign a fixed TSS estimate for group rides

Planned workouts — duplicate prevention: before calling create_planned_workout for any date, check get_planned_workouts results for that date. If a workout already exists there, use update_planned_workout with its event_id instead. Never call create_planned_workout for a date that already has a workout. The system silently handles deduplication — if a workout already exists on a date, the create call is automatically redirected to an update and returns status: ok. Do not re-fetch the calendar to verify this; proceed directly to the next workout.

delete_planned_workout must only be called when the athlete explicitly asks to delete a workout. Never delete as part of a planning workflow — always use update_planned_workout instead.

Weekly planning note: ALWAYS call get_weekly_note for the target Monday BEFORE writing any weekly note — mandatory, not optional. If the result has an id, pass it as event_id to write_weekly_note to update. If found=False, call write_weekly_note without event_id to create. Never write a weekly note without first fetching the existing one. Never create a second note on the same Monday. Check upcoming A races before planning; when a race is within 8 weeks, state the phase override and weeks-to-race in the note.

When writing a weekly note: block week and focus (1-2 sentences), each planned session with date/name and a brief natural description (e.g. "60m Z2 with sprint efforts", "90m easy aerobic", "60m threshold work") — NO percentages, NO interval structure details, just the character of the session. Finish with expected weekly TSS.
"""

WEEKLY_RECAP_SYSTEM_PROMPT = """You are an expert cycling coach generating an automated Monday morning training recap.
You have direct access to the athlete's training data via Intervals.icu.

Today's date: {today}

Athlete targets: weekly cap {max_hours} h / {max_tss} TSS.
Athlete baselines: {hrv_context} {rhr_context}

Your task: generate a structured weekly recap for the past 7 days.

Steps:
1. Call get_recent_activities(days_back=7) to see what was completed
2. Call get_wellness(days_back=7) to get the HRV/RHR/fatigue trend
3. Call get_planned_workouts(days_ahead=0) — this won't return past events; use what activities returned to compare planned vs actual based on compliance fields

Structure your recap as follows:
## Week in review — {today}
**Load:** total TSS and hours for the week vs the {max_tss} TSS / {max_hours} h target.
**Sessions:** bullet per session — date, name, brief description of what it was and how it went (compliance, RPE, feel). Flag any sessions that were missed or significantly under/over target.
**Wellness trend:** HRV and RHR over the week relative to athlete baselines. Note any suppressed days and whether they aligned with hard sessions.
**Highlights:** 1-2 standout positives from the week.
**Concerns:** any patterns worth watching (chronic underperformance, signs of fatigue, compliance issues).
**Coming week:** 1-2 sentence recommendation based on this week's load and wellness trend.

Keep the recap concise — under 400 words. Write in a direct, coach-to-athlete tone.
FTP/LTHR: always use the user-configured values from get_coach_ticks, not eFTP, and pick the sport_settings entry matching each activity's type — ride and run thresholds differ.
feel scale: 1=Strong, 2=Good, 3=Normal, 4=Poor, 5=Weak — lower is better.
"""

WELLNESS_CHECK_SYSTEM_PROMPT = """You are an expert sports science advisor running a morning wellness check for an endurance cyclist.
You have direct access to the athlete's training data via Intervals.icu.

Today's date: {today}

Athlete baselines: {hrv_context} {rhr_context}
Alert thresholds: a 7-day running average HRV below {hrv_min} ms is suppressed. RHR above {rhr_max} bpm is elevated. TSB (form) below −20 is high-fatigue risk. Sleep score below 60 (out of 100) is poor.

HRV interpretation — read this carefully:
- Compute a 7-day running average HRV from the daily readings returned (mean of the available HRV values over the last 7 days). This running average is your PRIMARY HRV signal — it filters out day-to-day noise and reflects the athlete's true recovery state.
- Base your HRV assessment on the 7-day running average. A suppressed running average (below {hrv_min} ms) or a clear downward trend in the running average indicates accumulated fatigue or illness and is the main driver of an HRV-related alert.
- Also note today's single-day HRV reading, but treat it as secondary. A single low reading after a hard effort is normal and does NOT by itself warrant an alert when the running average is healthy.
- HOWEVER, call out the single-day reading explicitly whenever it is far from the normal range (roughly more than ~15% outside {hrv_min}–{hrv_max}, in either direction). A single day that is dramatically low — even with a healthy running average — is worth flagging as something to watch, and a single day that is dramatically high may indicate a measurement issue or parasympathetic rebound.

Your task:
1. Call get_wellness(days_back=7) to get a full week of HRV, RHR, fatigue, form, and sleep readings (sleepScore 0–100, sleepSecs)
2. Call get_planned_workouts(days_ahead=1) to see if there is a structured workout planned today
3. Compute the 7-day running average HRV and assess it against the normal range; separately note today's single-day HRV
4. Assess whether any metric crosses the alert threshold above
5. Write a brief morning check-in

IMPORTANT — begin your response with exactly one of these tokens on its own line:
- `[ALERT]` if ANY metric is at or beyond an alert threshold (7-day running average HRV suppressed, RHR elevated, TSB ≤ −20, or sleep score below 60)
- `[OK]` if all metrics are within normal range

After the token, write 3-6 sentences:
- Always state last night's sleep score, the 7-day running average HRV, today's single-day HRV, and the recent RHR/form readings
- Lead your HRV comment with the 7-day running average; mention the single-day reading after it, and explicitly call it out if it is far from the normal range
- State which metrics are concerning (if ALERT) or reassuring (if OK)
- If ALERT and a structured workout is planned today: suggest a specific adjustment (e.g. replace intervals with 60 min easy Z2, or shorten duration by 30%)
- If ALERT and no workout planned: advise rest or very light movement
- If OK: brief confirmation that metrics support going ahead with today's plan

If (and only if) you suggested a specific change to today's planned workout, add a final line containing exactly: [[PROPOSE_WORKOUT_CHANGE]] — nothing else on that line. This lets the athlete accept and apply the change. Omit it on OK days or when no workout change is suggested.

Constraints: do not create or modify any calendar events. Suggestions only.
"""


WELLNESS_CHECK_READINESS_PROMPT = """You are an expert sports science advisor running a morning wellness check for an endurance cyclist.
The athlete's recovery is measured by Garmin training readiness, read from Home Assistant. Today's planned workout comes from Intervals.icu.

Today's date: {today}

PRIMARY SIGNAL — Garmin training readiness:
- get_training_readiness returns today's readiness score (0–100, higher is better) and its band: poor (1–24), low (25–49), moderate (50–74), high (75–94), prime (95–100). The score already folds together sleep, HRV status, recovery time, acute load and stress.
- It also returns the factor breakdown that explains the score — use these to say WHY readiness is where it is. Sleep and recovery are the two main factors: sleep_score + sleep_feedback, and recovery_time (recovery_hours / recovery_time give hours and minutes until full recovery) + recovery_feedback. The remaining factors are feedback text, HRV — both hrv_weekly_avg (Garmin's 7-day average) and hrv_last_night (last night's average, in ms) — with hrv_feedback, acute_load + load_balance_feedback (acute:chronic load), and stress_feedback. The *_feedback labels (e.g. good, very good, poor) are the robust signal — lean on them rather than guessing units.

Alert logic — the readiness band drives the alert, conditioned on today's planned workout:
- poor (≤24): ALWAYS an alert, no matter what is planned (or if nothing is planned). The body needs rest.
- low (25–49): an alert ONLY if a hard / interval / structured intensity workout is planned today. Low readiness on an easy/Z2/recovery/rest day — or a day with no workout — is NOT an alert; note it and advise keeping any session genuinely easy.
- moderate / high / prime (≥50): not an alert. If a specific factor reads clearly poor (e.g. sleep_feedback or hrv_feedback is POOR/LOW), call it out as a watch-item, but it does not by itself flip to an alert.
- If readiness is unavailable (get_training_readiness returns available=false): do NOT alert. Write a brief [OK] note that today's readiness could not be read and advise proceeding with the plan as scheduled while listening to the body.

Your task:
1. Call get_training_readiness to get today's score, band and factor breakdown
2. Call get_planned_workouts(days_ahead=1) to see whether a structured/interval workout is planned today and how hard it is
3. Apply the alert logic above (readiness band + planned workout)
4. Write a brief morning check-in

IMPORTANT — begin your response with exactly one of these tokens on its own line:
- `[ALERT]` if the readiness band warrants an alert under the rules above
- `[OK]` otherwise

After the token, write 3-6 sentences:
- Lead with the readiness score (use the band only to decide the alert — do not print the band or level as a label, the score already conveys it); then always state the two main factors — last night's sleep score and the recovery time in hours (and minutes) — followed by both HRV figures (weekly average and last night, ms), load balance and stress
- State which signals are concerning (if ALERT) or reassuring (if OK)
- If ALERT and a structured workout is planned today: suggest a specific adjustment (e.g. replace intervals with 60 min easy Z2, or shorten duration by 30%)
- If ALERT and no workout planned: advise rest or very light movement
- If OK: brief confirmation that today's plan can go ahead (note any low-readiness caveat to keep easy days easy)

If (and only if) you suggested a specific change to today's planned workout, add a final line containing exactly: [[PROPOSE_WORKOUT_CHANGE]] — nothing else on that line. This lets the athlete accept and apply the change. Omit it on OK days or when no workout change is suggested.

Constraints: do not create or modify any calendar events. Suggestions only.
"""


class TrainingAgent:
    def __init__(
        self,
        openai_api_key: str,
        intervals_athlete_id: str,
        intervals_api_key: str,
        max_hours: int = 8,
        max_tss: int = 500,
        hrv_min: int = 48,
        hrv_max: int = 69,
        rhr_min: int = 45,
        rhr_max: int = 54,
        hard_intervals_per_week: int = 3,
        block_start_date: str = "",
        group_ride_keywords: str = "group,klub,klubtur",
        days_back: int = 28,
        chat_model: str = "gpt-5.5",
        auto_review_model: str = "gpt-5.5",
        reasoning_effort: str = "medium",
        readiness_entity: str = "",
        nightly_hrv_entity: str = "",
    ):
        self.openai = OpenAI(api_key=openai_api_key)
        self.icu = IntervalsClient(intervals_athlete_id, intervals_api_key)
        self.ha = HAClient()
        self.readiness_entity = readiness_entity
        self.nightly_hrv_entity = nightly_hrv_entity
        self.max_hours = max_hours
        self.max_tss = max_tss
        self.hrv_min = hrv_min
        self.hrv_max = hrv_max
        self.rhr_min = rhr_min
        self.rhr_max = rhr_max
        self.hard_intervals_per_week = hard_intervals_per_week
        self.block_start_date = block_start_date
        self.group_ride_keywords = [k.strip() for k in group_ride_keywords.split(",") if k.strip()]
        self.days_back_cap = days_back
        self.chat_model = chat_model
        self.auto_review_model = auto_review_model
        # "" omits the parameter entirely (let the model pick its own default);
        # "none" is a real API value meaning "reason as little as possible".
        self.reasoning_effort = (reasoning_effort or "").strip().lower() or None

        self._prompt_cache: dict = {}  # "mode:hash" → prompt string

        # Models that reject the `reasoning` parameter. Learned at runtime — see
        # _create_response.
        self._no_reasoning_models: set[str] = set()

    def _create_response(self, **kwargs):
        """Call /v1/responses, degrading gracefully on models without reasoning.

        Model names are free text in the add-on config, so we can't know upfront
        whether one accepts `reasoning` — older models (gpt-4o) reject it. The
        first such failure is detected, remembered, and the call retried without
        it; later calls for that model omit it upfront.

        Retrying is safe: a request that 400s never reached a tool, so no side
        effect is replayed.
        """
        model = kwargs.get("model")
        if model in self._no_reasoning_models:
            kwargs.pop("reasoning", None)
        try:
            return self.openai.responses.create(**kwargs)
        except Exception as e:
            if "reasoning" not in kwargs or "reasoning" not in str(e).lower():
                raise
            print(
                f"Model {model} does not accept the reasoning parameter — "
                f"retrying without it.",
                flush=True,
            )
            self._no_reasoning_models.add(model)
            kwargs.pop("reasoning", None)
            return self.openai.responses.create(**kwargs)

    def _run_response_loop(self, *, model: str, input_items: list, tools: list,
                           max_iterations: int, label: str) -> tuple[str, dict]:
        """Drive a /v1/responses tool-calling loop to a final text answer.

        Returns (text, usage). `usage` deliberately keeps the Chat Completions
        key names (prompt_tokens/completion_tokens/total_tokens) because
        main.accumulate_tokens reads exactly those three via .get(k, 0) —
        emitting the Responses names instead would silently accumulate zero and
        quietly disable the daily budget gate.
        """
        items = list(input_items)  # never mutate the caller's list
        usage = {"prompt_tokens": 0, "completion_tokens": 0,
                 "total_tokens": 0, "reasoning_tokens": 0}

        for _ in range(max_iterations):
            response = self._create_response(
                model=model,
                input=items,
                tools=tools,
                tool_choice="auto",
                **({"reasoning": {"effort": self.reasoning_effort}}
                   if self.reasoning_effort else {}),
            )

            u = response.usage
            if u:
                usage["prompt_tokens"] += u.input_tokens
                usage["completion_tokens"] += u.output_tokens
                usage["total_tokens"] += u.total_tokens
                details = getattr(u, "output_tokens_details", None)
                if details:
                    usage["reasoning_tokens"] += details.reasoning_tokens

            # Feed the whole output back verbatim. Reasoning items must travel
            # with the function_call items they produced, in order, or the next
            # request 400s ("reasoning item provided without its required
            # following item") and the model loses its chain of thought between
            # tool calls. These are SDK objects — do NOT model_dump() them (bare
            # dumps emit nulls the API rejects), and never persist this list.
            items += response.output

            calls = [it for it in response.output if it.type == "function_call"]
            if not calls:
                text = response.output_text
                if not text:
                    raise RuntimeError(
                        f"{label}: model returned no text "
                        f"(status={response.status}, "
                        f"incomplete={response.incomplete_details})"
                    )
                return text, usage

            for call in calls:
                try:
                    # Zero-arg tools can return "" rather than "{}".
                    args = json.loads(call.arguments or "{}")
                except json.JSONDecodeError as e:
                    items.append({
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": (f"Error parsing tool arguments: {e}. "
                                   f"Please retry with valid JSON."),
                    })
                    continue
                items.append({
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": self._run_tool(call.name, args),
                })

        raise RuntimeError(f"{label} exceeded max iterations ({max_iterations}). "
                           f"LLM may be stuck in a loop.")

    def _get_training_readiness(self) -> dict:
        """Read the Garmin training-readiness entity from Home Assistant and
        surface its score, band, and the recovery factor breakdown carried in
        the entity attributes. Returns available=false when the entity is not
        configured, unreachable, or has no numeric score yet."""
        if not self.readiness_entity:
            return {"available": False, "reason": "No training readiness entity configured."}
        if not self.ha.token:
            return {"available": False, "reason": "SUPERVISOR_TOKEN is missing — the add-on cannot reach the HA Core API."}
        state = self.ha.get_state(self.readiness_entity)
        if not state:
            return {"available": False, "reason": f"Could not read {self.readiness_entity} from the HA Core API (see HA get_state log line for the HTTP status)."}
        attrs = state.get("attributes", {}) or {}
        # The state is the readiness score (e.g. "79"); fall back to the score attribute.
        try:
            score = float(state.get("state"))
        except (TypeError, ValueError):
            try:
                score = float(attrs.get("score"))
            except (TypeError, ValueError):
                return {"available": False, "reason": f"Readiness score not available yet (state={state.get('state')!r})."}
        recovery_time = attrs.get("recoveryTime")
        recovery_hours = (
            round(recovery_time / 60, 1)
            if isinstance(recovery_time, (int, float))
            else None
        )
        result = {
            "available": True,
            "score": round(score),
            "band": _readiness_band(score),
            "feedback": _fmt_feedback(attrs.get("feedbackShort")),
            "sleep_score": attrs.get("sleepScore"),
            "sleep_feedback": _fmt_feedback(attrs.get("sleepScoreFactorFeedback")),
            "hrv_weekly_avg": attrs.get("hrvWeeklyAverage"),
            "hrv_last_night": self._get_nightly_hrv(),
            "hrv_feedback": _fmt_feedback(attrs.get("hrvFactorFeedback")),
            "recovery_time": recovery_time,
            "recovery_hours": recovery_hours,
            "recovery_feedback": _fmt_feedback(attrs.get("recoveryTimeFactorFeedback")),
            "acute_load": attrs.get("acuteLoad"),
            "load_balance_feedback": _fmt_feedback(attrs.get("acwrFactorFeedback")),
            "stress_feedback": _fmt_feedback(attrs.get("stressHistoryFactorFeedback")),
            "calendar_date": attrs.get("calendarDate"),
        }
        return result

    def _get_nightly_hrv(self):
        """Read last night's average HRV (ms) from its own Garmin entity.
        Returns the numeric value, or None if not configured / unreachable /
        not yet a number (best-effort; never blocks the readiness check)."""
        if not self.nightly_hrv_entity:
            return None
        state = self.ha.get_state(self.nightly_hrv_entity)
        if not state:
            return None
        try:
            return round(float(state.get("state")))
        except (TypeError, ValueError):
            return None

    def readiness_status(self) -> dict:
        """Public accessor for the current readiness read, including the reason
        when unavailable. Used by the morning loop so it can log WHY it defers."""
        return self._get_training_readiness()

    def training_readiness_available(self) -> bool:
        """True only when a readiness entity is configured AND currently returns
        a numeric score. Used to defer the morning check until Garmin has
        published the reading (it often reads 'unavailable' for a while)."""
        if not self.readiness_entity:
            return False
        return self._get_training_readiness().get("available", False)

    def _run_tool(self, name: str, args: dict) -> str:
        try:
            if name == "get_recent_activities":
                result = self.icu.get_activities(args.get("days_back", 7), max_days_back=self.days_back_cap, group_keywords=self.group_ride_keywords)
            elif name == "get_activity_intervals":
                result = self.icu.get_activity_intervals(args["activity_id"])
            elif name == "get_wellness":
                result = self.icu.get_wellness(args.get("days_back", 14))
            elif name == "get_training_readiness":
                result = self._get_training_readiness()
            elif name == "get_planned_workouts":
                result = self.icu.get_events(args.get("days_ahead", 35))
            elif name == "get_planned_workout":
                result = self.icu.get_event(args["event_id"])
            elif name == "get_coach_ticks":
                result = self.icu.get_athlete()
            elif name == "post_activity_comment":
                result = self.icu.post_activity_comment(
                    args["activity_id"],
                    args["comment"],
                    coach_tick_id=args.get("coach_tick_id"),
                )
            elif name == "create_planned_workout":
                # Enforce deduplication: redirect to update if a WORKOUT already exists on this date.
                # This is a server-level guarantee — model instructions alone are insufficient.
                target_date = args["date"]
                existing_event = None
                try:
                    from datetime import datetime as _dt
                    target_dt = _dt.strptime(target_date, "%Y-%m-%d")
                    days_ahead = max(1, (_dt.now() - target_dt).days * -1 + 1)
                    existing = self.icu.get_events(days_ahead=days_ahead)
                    existing_event = next(
                        (e for e in existing
                         if e.get("date") == target_date and e.get("category") == "WORKOUT"),
                        None,
                    )
                except Exception:
                    pass  # on any failure, fall through to normal create

                if existing_event:
                    self.icu.update_planned_workout(
                        event_id=str(existing_event["id"]),
                        name=args.get("name"),
                        description=args.get("description"),
                        sport_type=args.get("sport_type"),
                        planned_duration_seconds=args.get("planned_duration_seconds"),
                        planned_tss=args.get("planned_tss"),
                    )
                    # Return a neutral success response — the model doesn't need to know
                    # it was a dedup redirect; telling it would trigger re-verification.
                    result = {"status": "ok", "event_id": existing_event["id"], "date": target_date}
                else:
                    result = self.icu.create_planned_workout(
                        date=target_date,
                        name=args["name"],
                        description=args["description"],
                        sport_type=args.get("sport_type", "Ride"),
                        planned_duration_seconds=args.get("planned_duration_seconds"),
                        planned_tss=args.get("planned_tss"),
                    )
            elif name == "update_planned_workout":
                result = self.icu.update_planned_workout(
                    event_id=args["event_id"],
                    name=args.get("name"),
                    description=args.get("description"),
                    date=args.get("date"),
                    sport_type=args.get("sport_type"),
                    planned_duration_seconds=args.get("planned_duration_seconds"),
                    planned_tss=args.get("planned_tss"),
                    category=args.get("category"),
                )
            elif name == "get_upcoming_races":
                result = self.icu.get_upcoming_races(args.get("days_ahead", 120))
            elif name == "get_weekly_note":
                result = self.icu.get_weekly_note(args["monday_date"])
                if result is None:
                    result = {"found": False, "monday_date": args["monday_date"],
                              "message": "No weekly note found for this Monday."}
            elif name == "write_weekly_note":
                monday_date = args["monday_date"]
                event_id = args.get("event_id")
                if not event_id:
                    # Server-level dedup: always update if a note already exists on this Monday.
                    try:
                        existing_note = self.icu.get_weekly_note(monday_date)
                        if existing_note:
                            event_id = existing_note.get("id")
                    except Exception:
                        pass
                self.icu.write_weekly_note(
                    monday_date=monday_date,
                    name=args["name"],
                    content=args["content"],
                    event_id=event_id,
                )
                result = {"status": "ok", "event_id": event_id, "monday_date": monday_date}
            elif name == "delete_planned_workout":
                result = self.icu.delete_event(args["event_id"])
            else:
                result = {"error": f"Unknown tool: {name}"}
            return json.dumps(result)
        except (KeyError, ValueError, TypeError, AttributeError, RuntimeError, OSError) as e:
            return json.dumps({"error": str(e)})

    def _build_system(self, mode: str) -> str:
        import hashlib
        from datetime import date as date_cls
        today = date_cls.today()

        config_key = (
            mode,
            self.max_hours, self.max_tss,
            self.hrv_min, self.hrv_max,
            self.rhr_min, self.rhr_max,
            self.hard_intervals_per_week,
            self.block_start_date,
            tuple(self.group_ride_keywords),
            bool(self.readiness_entity),
            today.isoformat(),
        )
        cache_key = hashlib.md5(str(config_key).encode()).hexdigest()
        if cache_key in self._prompt_cache:
            return self._prompt_cache[cache_key]

        hrv_context = (
            f"HRV normal range: {self.hrv_min}–{self.hrv_max} ms. Below {self.hrv_min} is suppressed, above {self.hrv_max} is elevated."
            if self.hrv_min and self.hrv_max else
            f"HRV typical value: ~{self.hrv_max} ms." if self.hrv_max else
            "HRV range: not configured."
        )
        rhr_context = (
            f"Resting HR normal range: {self.rhr_min}–{self.rhr_max} bpm. Above {self.rhr_max} may indicate fatigue or illness."
            if self.rhr_min and self.rhr_max else
            f"Resting HR typical value: ~{self.rhr_max} bpm." if self.rhr_max else
            "Resting HR range: not configured."
        )

        if self.block_start_date:
            try:
                block_start = date_cls.fromisoformat(self.block_start_date)
                days_in = (today - block_start).days
                block_week = (days_in // 7) % 4 + 1
                week_label = {1: "Week 1 — Base Load", 2: "Week 2 — Progressive Overload",
                              3: "Week 3 — Peak Stress", 4: "Week 4 — Deload"}[block_week]
                block_context = f"Current block week: {block_week}/4 ({week_label})"
            except (ValueError, KeyError):
                block_context = "Block start date is set but could not be parsed (use YYYY-MM-DD)."
        else:
            block_context = "Block start date not configured — ask the athlete which week of the block they are in if relevant."

        kw_str = ", ".join(self.group_ride_keywords) if self.group_ride_keywords else "none"
        group_context = f"Group ride auto-detection keywords: {kw_str}."

        if mode == "health":
            if self.readiness_entity:
                prompt = HEALTH_READINESS_SYSTEM_PROMPT.format(today=today.isoformat())
            else:
                prompt = HEALTH_SYSTEM_PROMPT.format(
                    today=today.isoformat(),
                    hrv_context=hrv_context,
                    rhr_context=rhr_context,
                )
        elif mode == "planning":
            prompt = PLANNING_SYSTEM_PROMPT.format(
                today=today.isoformat(),
                max_hours=self.max_hours,
                max_tss=self.max_tss,
                block_context=block_context,
                hard_intervals_per_week=self.hard_intervals_per_week,
                group_context=group_context,
            )
        else:  # "review" and any unknown mode
            prompt = REVIEW_SYSTEM_PROMPT.format(today=today.isoformat())

        self._prompt_cache[cache_key] = prompt
        return prompt

    def _tools_for_mode(self, mode: str) -> list:
        """Tools available in a chat mode. Health uses the Garmin readiness tool set
        when a readiness entity is configured, otherwise the Intervals wellness set."""
        if mode == "health" and self.readiness_entity:
            return _HEALTH_READINESS_TOOLS
        return TOOLS_BY_MODE.get(mode, TOOLS_BY_MODE["review"])

    def _safe_role(self, m) -> str:
        """Get role from either a dict or a ChatCompletionMessage object."""
        return m.get("role") if isinstance(m, dict) else getattr(m, "role", None)

    def chat(self, user_message: str, history: list, mode: str = "review") -> tuple[str, list, dict]:
        system = self._build_system(mode)

        # Strip tool-call internals before building the context window.
        # Intermediate tool_calls/tool-result messages are implementation plumbing;
        # the assistant's final text reply already summarises what was found.
        # Sending all those JSON blobs on every call bloats requests significantly.
        # We keep only user messages and final assistant text replies, then take
        # the last 5 exchanges — enough for follow-up continuity without dragging
        # in days of unrelated workout analysis.
        lean_history = [
            m for m in history
            if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)
            and m.get("content")
            and not m.get("tool_calls")
        ]

        # Short-circuit: if the identical question was already answered in the most recent
        # turn, return the cached reply without calling OpenAI. This prevents repeat API
        # calls when the user re-sends because a slow response appeared to have failed.
        last_user = next((m["content"] for m in reversed(lean_history) if m.get("role") == "user"), None)
        last_reply = next((m["content"] for m in reversed(lean_history) if m.get("role") == "assistant"), None)
        if last_user == user_message and last_reply:
            return last_reply, history, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        # Keep last 3 exchanges (6 messages), starting on a user message.
        # Capped at 3 (down from 5) to prevent long ride analyses from bloating context —
        # each analysis can be 3000+ tokens and 5 copies would exceed 15k tokens of history alone.
        trimmed = lean_history[-6:]
        while trimmed and self._safe_role(trimmed[0]) != "user":
            trimmed = trimmed[1:]
        trimmed_history = trimmed

        messages = [{"role": "system", "content": system}]
        messages += trimmed_history
        messages.append({"role": "user", "content": user_message})

        reply, usage = self._run_response_loop(
            model=self.chat_model,
            input_items=messages,
            tools=self._tools_for_mode(mode),
            max_iterations=25,
            label="Chat tool loop",
        )
        # Store only the lean conversational record (user + assistant text).
        # Tool-call internals are not re-sent on subsequent turns, so
        # persisting them just bloats /data/chat_history.json.
        new_history = list(trimmed_history) + [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": reply},
        ]
        return reply, new_history, usage

    def auto_review(self, activity: dict) -> tuple[str, dict]:
        """Generate a coach review for a newly uploaded activity. Returns (comment, token_usage)."""
        from datetime import date as date_cls
        paired_event_id = activity.get("paired_event_id")
        planned_instruction = (
            f"The activity was matched to a planned workout (paired_event_id={paired_event_id}). "
            f"Fetch the planned workout with get_planned_workout({paired_event_id}), then fetch the "
            f"actual intervals with get_activity_intervals. Compare planned vs actual: which targets "
            f"were hit, which were missed, how many reps were completed vs planned."
            if paired_event_id else
            "Fetch the activity intervals with get_activity_intervals for a full breakdown."
        )
        # Name the sport explicitly: this path knows it server-side, so the model
        # never has to infer it to pick the right thresholds.
        sport = activity.get("type") or "unknown"
        prompt = (
            f"A new {sport} activity was just uploaded. {planned_instruction} "
            f"Also fetch coach ticks (get_coach_ticks) so you know the athlete's configured "
            f"thresholds, and use the sport_settings entry whose `sports` list contains "
            f"\"{sport}\" — not the Ride entry — when interpreting power or HR. "
            f"Then post a brief coach note on the activity with the most appropriate tick "
            f"(skip tick if list is empty, but always post the comment). "
            f"The note should be 5-10 sentences covering overall execution, standout metrics "
            f"(power consistency, decoupling, HR drift, or planned vs actual), RPE/feel, and "
            f"one or two concrete takeaways. End with: 'Ask me for a deeper analysis if you want more detail.' "
            f"Activity summary: {json.dumps(activity)}"
        )
        # Use a dedicated short system prompt — the full chat prompt's planning,
        # race-phase, and workout-creation rules aren't needed for ride review.
        system = AUTO_REVIEW_SYSTEM_PROMPT.format(today=date_cls.today().isoformat())
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]

        return self._run_response_loop(
            model=self.auto_review_model,
            input_items=messages,
            tools=TOOLS,
            max_iterations=15,
            label="Auto-review tool loop",
        )

    def weekly_recap(self) -> tuple[str, dict]:
        """Generate a Monday morning training recap. Returns (recap_text, usage)."""
        from datetime import date as date_cls
        today = date_cls.today()
        hrv_context = (
            f"HRV normal range: {self.hrv_min}–{self.hrv_max} ms."
            if self.hrv_min and self.hrv_max else "HRV range: not configured."
        )
        rhr_context = (
            f"Resting HR normal range: {self.rhr_min}–{self.rhr_max} bpm."
            if self.rhr_min and self.rhr_max else "Resting HR range: not configured."
        )
        system = WEEKLY_RECAP_SYSTEM_PROMPT.format(
            today=today.isoformat(),
            max_hours=self.max_hours,
            max_tss=self.max_tss,
            hrv_context=hrv_context,
            rhr_context=rhr_context,
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": "Generate this week's training recap."},
        ]
        return self._run_response_loop(
            model=self.auto_review_model,
            input_items=messages,
            tools=_RECAP_TOOLS,
            max_iterations=20,
            label="Weekly recap tool loop",
        )

    def wellness_check(self) -> tuple[str, bool, dict]:
        """Check today's wellness. Uses Garmin training readiness when a readiness
        entity is configured, otherwise the Intervals.icu HRV/RHR/sleep/form logic.
        Returns (message, is_alert, usage)."""
        from datetime import date as date_cls
        today = date_cls.today()
        if self.readiness_entity:
            system = WELLNESS_CHECK_READINESS_PROMPT.format(today=today.isoformat())
            wellness_tools = _WELLNESS_READINESS_TOOLS
        else:
            hrv_context = (
                f"HRV normal range: {self.hrv_min}–{self.hrv_max} ms."
                if self.hrv_min and self.hrv_max else "HRV range: not configured."
            )
            rhr_context = (
                f"Resting HR normal range: {self.rhr_min}–{self.rhr_max} bpm."
                if self.rhr_min and self.rhr_max else "Resting HR range: not configured."
            )
            system = WELLNESS_CHECK_SYSTEM_PROMPT.format(
                today=today.isoformat(),
                hrv_context=hrv_context,
                rhr_context=rhr_context,
                hrv_min=self.hrv_min or 0,
                hrv_max=self.hrv_max or 0,
                rhr_max=self.rhr_max or 999,
            )
            wellness_tools = _WELLNESS_TOOLS
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": "Run this morning's wellness check."},
        ]
        content, usage = self._run_response_loop(
            model=self.auto_review_model,
            input_items=messages,
            tools=wellness_tools,
            max_iterations=10,
            label="Wellness check tool loop",
        )
        is_alert = content.lstrip().startswith("[ALERT]")
        return content, is_alert, usage
