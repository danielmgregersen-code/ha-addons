import json
from openai import OpenAI
from intervals import IntervalsClient

TOOLS = [
    {
        "type": "function",
        "function": {
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
    },
    {
        "type": "function",
        "function": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "get_wellness",
            "description": "Fetch the athlete's wellness data (HRV, resting HR, sleep, fatigue, form) from Intervals.icu.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days_back": {"type": "integer", "default": 14}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_planned_workouts",
            "description": "Fetch upcoming planned workouts from the athlete's Intervals.icu calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days_ahead": {"type": "integer", "default": 21}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "get_coach_ticks",
            "description": (
                "Fetch the available coach tick options for this athlete. Also returns the athlete's "
                "user-configured ftp, lthr and max_hr — always use these values when interpreting "
                "power percentages or HR zones, not the eFTP estimated from activity data."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
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
    },
    {
        "type": "function",
        "function": {
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
    },
    {
        "type": "function",
        "function": {
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
    },
    {
        "type": "function",
        "function": {
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
    },
    {
        "type": "function",
        "function": {
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
    },
    {
        "type": "function",
        "function": {
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
                    "name": {"type": "string", "description": "Short title e.g. 'Week 2 — Build'"},
                    "content": {"type": "string", "description": "Full weekly plan content."},
                    "event_id": {"type": "integer", "description": "Existing event ID if updating rather than creating."},
                },
                "required": ["monday_date", "name", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
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
    },
]

SYSTEM_PROMPT = """You are an expert bicycling coach with direct access
to the athlete's training data via Intervals.icu.

Athlete baselines:
{hrv_context}
{rhr_context}

Block periodization:
{block_context}
Hard interval sessions per week: {hard_intervals_per_week}

Group rides:
{group_context}

Block structure (default cycle — overridden when a race is approaching):
- Week 1 (Foundation): aerobic base, zone 2 focus, {hard_intervals_per_week} light interval sessions (e.g. tempo or sweet spot), moderate TSS
- Week 2 (Build): increase load ~10%, {hard_intervals_per_week} structured interval sessions (threshold or VO2), higher TSS
- Week 3 (Peak): highest load of the block, {hard_intervals_per_week} hard interval sessions (VO2max or above threshold), push TSS
- Week 4 (Recovery): drop volume to ~50-60% of peak week, no hard intervals, zone 1-2 only, let adaptations consolidate

Race-driven phase overrides (check upcoming A races when planning — use get_upcoming_races):

Stage race detection: if multiple RACE_A events fall on consecutive or near-consecutive days, treat them as a single stage race block. The block runs from the first to the last race day. Use the first day for taper/phase calculations. Post-race recovery counts from the last race day.

Phase rules (weeks to first race day):
- Race days: activation only — short sharp efforts or complete rest. Gaps between stages are Z1 recovery only, no structured work
- Taper (1–3 weeks out): cut volume 40–60% vs peak week, keep 1–2 short intensity sessions to maintain sharpness, prioritise freshness
- Sharpening (4–5 weeks out): force peak/sharpening phase regardless of normal block cycle — highest quality intervals, controlled volume
- Late build (6–8 weeks out): ensure build or peak phase — if normal cycle gives recovery, extend build by one week instead
- Normal (>8 weeks out): follow the standard 1-2-3-4 block cycle
- Post-race (up to 5 days after last race day): recovery week regardless of block position
- Multiple race blocks: use the nearest to determine phase; mention the next in the weekly note
- Always state race name, date(s), stage count if applicable, and phase override reason in the weekly note

Your role:
- Analyse recent training and provide honest, specific feedback
- Drill into individual interval sessions when asked — compare work efforts, check if targets were hit
- Comment on completed workouts and mark them as coach-reviewed using a coach tick
- Plan and schedule future workouts based on goals and fatigue
- Adjust training plans based on athlete requests or wellness data

Guidelines:
- Always fetch relevant data before commenting — never assume what a workout looks like
- Fetch only as much history as the question needs — 3-5 days for a single workout, 7-10 for a weekly review, 28 for block or load trend analysis. Default to 7 if unsure
- Weeks start on Monday (European standard). When referring to "this week" or "last week", Monday is the first day.
- For interval analysis: fetch activities first to get the ID, then fetch intervals for that activity
- When analysing intervals, comment on consistency across efforts, power/HR drift,
  work-to-rest ratio, and whether targets were met
- Speed is stored as m/s — convert to min/km (pace = 1000/speed/60) or km/h as appropriate
- When creating planned workouts, the description MUST use the Intervals.icu workout builder format so it renders as a structured workout with a visual bar graph and calculated TSS. The format is:

Section headers are plain text lines (no dash). Steps start with a dash and include duration and target.
Repeats are written as "Nx" on the line IMMEDIATELY before the indented repeated steps — the steps in the repeat block must each start with "- " and the whole block is indented or simply follows the Nx line directly. Each repeat step must be on its own line. The recovery between efforts is a step inside the repeat block, not outside it. When a session has multiple distinct sets with longer rest between them, write each set as its own separate Nx block with the inter-set rest as a plain step between the blocks — never nest repeat blocks inside each other.

Duration formats: 30s, 10m, 1m30
Targets: 100w (absolute watts), 80% (% of FTP), 60% HR (% of max HR), 100% LTHR, 90 rpm (cadence)
Ranges: 80-90%, 100-140w
Ramps: Ramp 60-80%

Example:
Warmup
- 15m 55-65%

Main set 4x
- 8m 95-105%
- 4m 40-50%

Cooldown
- 10m 55-65%

For sweet spot work, use 88-93% rather than a zone label:
Main set 3x
- 12m 88-93%
- 5m 50-60%

Always structure workouts with Warmup / Main set / Cooldown sections. Use raw percentages or watt values rather than zone notation — fixed values (e.g. 75%, 250w) or ranges (e.g. 70-80%, 240-260w) are both fine. Include cadence guidance for key efforts
- Consider cumulative fatigue before adding hard sessions
- Confirm with the athlete before making changes to the calendar
- Group ride handling:
  * Group rides are detected by keywords in activity name/tags — the `group_ride` field will be True
  * On scheduled group ride days: do not plan structured work, reserve the day, count estimated TSS toward weekly load
  * If no group ride days are configured: when planning a week, ask if any group rides are expected and on which days
  * After a group ride: check decoupling and RPE — if decoupling >8% or RPE >=8, soften the following day's session
  * In weekly analysis: flag group rides separately, note their contribution to weekly TSS
- Weekly planning note: always fetch the Monday note and check upcoming A races before analysing or planning a week. If no note exists, create one. If one already exists, ALWAYS update it by passing its event_id to write_weekly_note — never create a second note on the same Monday. When a race is within 8 weeks, state the phase override and weeks-to-race in the note
- When writing a weekly note include: block week and focus (1-2 sentences), each planned session with date/name and a brief natural description of the session type and duration (e.g. "60m Z2 with sprint efforts", "90m easy aerobic", "60m threshold work") — NO specific percentages, NO interval structure details (no "3x15s at 150%", no "warm-up 15m 50-65%"), just the overall character of the session. Finish with expected weekly TSS. Example line: "Tue 21 Apr: Hibernation & Sparks — 60m Z2 with short sprint efforts"
- feel scale: 1=Strong, 2=Good, 3=Normal, 4=Poor, 5=Weak — lower is better
- power_zone_mins is an array of minutes spent in each power zone [Z1, Z2, Z3, Z4, Z5, Z6, Z7]
- Sweet spot (~88–93% FTP) overlaps the upper portion of Z3 and lower portion of Z4 — it is NOT a separate zone in the 7-zone model. When reporting zone times, sweet spot time is already counted within Z3 and Z4. Never list it as its own zone or add it on top of Z3/Z4 totals
- hr_zone_mins is an array of minutes spent in each HR zone [Z1, Z2, Z3, Z4, Z5]
- When discussing zone distribution, comment on the balance (values are already in minutes)
- FTP: always use the user-configured ftp from get_coach_ticks, not eFTP (icu_pm_ftp or icu_rolling_ftp) embedded in activity data. Call get_coach_ticks once per conversation if you need to reference FTP or LTHR.
- compliance field: if >0 the ride matched a planned workout; if null/0 it was unstructured. When compliance >0 and paired_event_id is set, call get_planned_workout(paired_event_id) to fetch the planned structure, then compare it against the actual intervals — note which targets were hit or missed, how many reps were completed vs planned, and power/HR vs targets
- For MATCHED workouts (compliance > 0): focus primarily on interval execution — did efforts hit targets, how consistent were the reps, power/HR per interval. Cover zone distribution briefly as secondary context
- For UNMATCHED/UNSTRUCTURED rides (compliance null or 0): give equal weight to interval efforts and zone distribution — both tell the story of what kind of ride it was
- decoupling: aerobic decoupling % — HR drift relative to power over a ride; <5% is well-coupled, >10% suggests fatigue or heat
- efficiency_factor: power/HR ratio — higher is more aerobically efficient; rising over time is a good sign
- variability_index: normalised power / average power — closer to 1.0 means steady effort, higher means variable pacing
- polarization_index: distribution between low and high intensity; higher = more polarised training
- When posting a comment, fetch coach ticks first and select the most appropriate tick based on session quality: 1=Really bad, 2=Poor, 3=Decent, 4=Good, 5=Amazing. Base the choice on TSS vs expected load, RPE, feel, interval execution, and decoupling. If the coach_ticks list is empty, post the comment without a tick_id — never refuse to comment just because ticks are unavailable
- Today's date: {today}

Sport types: Ride
"""


class TrainingAgent:
    def __init__(
        self,
        openai_api_key: str,
        intervals_athlete_id: str,
        intervals_api_key: str,
        hrv_min: int = 0,
        hrv_max: int = 0,
        rhr_min: int = 0,
        rhr_max: int = 0,
        hard_intervals_per_week: int = 2,
        block_start_date: str = "",
        group_ride_days: str = "",
        group_ride_weekday_tss: int = 120,
        group_ride_weekend_tss: int = 180,
        group_ride_keywords: str = "group,club",
        group_ride_counts_as_intervals: str = "",
        days_back: int = 28,
        chat_model: str = "gpt-5.4",
        auto_review_model: str = "gpt-5.4-mini",
    ):
        self.openai = OpenAI(api_key=openai_api_key)
        self.icu = IntervalsClient(intervals_athlete_id, intervals_api_key)
        self.hrv_min = hrv_min
        self.hrv_max = hrv_max
        self.rhr_min = rhr_min
        self.rhr_max = rhr_max
        self.hard_intervals_per_week = hard_intervals_per_week
        self.block_start_date = block_start_date
        self.group_ride_days = [d.strip() for d in group_ride_days.split(",") if d.strip()]
        self.group_ride_weekday_tss = group_ride_weekday_tss
        self.group_ride_weekend_tss = group_ride_weekend_tss
        self.group_ride_keywords = [k.strip() for k in group_ride_keywords.split(",") if k.strip()]
        self.group_ride_counts_as_intervals = [d.strip() for d in group_ride_counts_as_intervals.split(",") if d.strip()]
        self.days_back_cap = days_back
        self.chat_model = chat_model
        self.auto_review_model = auto_review_model

        # Cache for system prompt — only rebuild if config changes
        self._cached_system_prompt = None
        self._cached_config_hash = None

    def _run_tool(self, name: str, args: dict) -> str:
        try:
            if name == "get_recent_activities":
                result = self.icu.get_activities(args.get("days_back", 7), max_days_back=self.days_back_cap, group_keywords=self.group_ride_keywords)
            elif name == "get_activity_intervals":
                result = self.icu.get_activity_intervals(args["activity_id"])
            elif name == "get_wellness":
                result = self.icu.get_wellness(args.get("days_back", 14))
            elif name == "get_planned_workouts":
                result = self.icu.get_events(args.get("days_ahead", 21))
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
                result = self.icu.create_planned_workout(
                    date=args["date"],
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
                result = self.icu.write_weekly_note(
                    monday_date=args["monday_date"],
                    name=args["name"],
                    content=args["content"],
                    event_id=args.get("event_id"),
                )
            elif name == "delete_planned_workout":
                result = self.icu.delete_event(args["event_id"])
            else:
                result = {"error": f"Unknown tool: {name}"}
            return json.dumps(result)
        except (KeyError, ValueError, TypeError, AttributeError, RuntimeError, OSError) as e:
            return json.dumps({"error": str(e)})

    def _build_system(self) -> str:
        import hashlib
        from datetime import date as date_cls
        today = date_cls.today()

        # Create a hash of config values to detect changes
        config_key = (
            self.hrv_min, self.hrv_max,
            self.rhr_min, self.rhr_max,
            self.hard_intervals_per_week,
            self.block_start_date,
            tuple(self.group_ride_days),
            self.group_ride_weekday_tss,
            self.group_ride_weekend_tss,
            tuple(self.group_ride_keywords),
            tuple(self.group_ride_counts_as_intervals),
            today.isoformat(),  # Include today so it updates daily
        )
        current_hash = hashlib.md5(str(config_key).encode()).hexdigest()

        # Return cached prompt if config hasn't changed
        if self._cached_config_hash == current_hash and self._cached_system_prompt is not None:
            return self._cached_system_prompt

        # Config changed or cache empty — rebuild
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

        # Calculate block week
        if self.block_start_date:
            try:
                block_start = date_cls.fromisoformat(self.block_start_date)
                days_in = (today - block_start).days
                block_week = (days_in // 7) % 4 + 1
                week_label = {1: "Week 1 — Foundation", 2: "Week 2 — Build",
                              3: "Week 3 — Peak", 4: "Week 4 — Recovery"}[block_week]
                block_context = f"Current block week: {block_week}/4 ({week_label})"
            except (ValueError, KeyError):
                block_context = "Block start date is set but could not be parsed (use YYYY-MM-DD)."
        else:
            block_context = "Block start date not configured — ask the athlete which week of the block they are in if relevant."

        # Group ride context
        if self.group_ride_days:
            days_str = ", ".join(self.group_ride_days)
            weekend_days = {"saturday", "sunday"}
            wd_days = [d for d in self.group_ride_days if d.lower() not in weekend_days]
            we_days = [d for d in self.group_ride_days if d.lower() in weekend_days]
            tss_parts = []
            if wd_days:
                tss_parts.append(f"{', '.join(wd_days)}: ~{self.group_ride_weekday_tss} TSS")
            if we_days:
                tss_parts.append(f"{', '.join(we_days)}: ~{self.group_ride_weekend_tss} TSS")
            tss_str = "; ".join(tss_parts)
            group_context = (
                f"Scheduled group ride days: {days_str}. "
                f"Estimated TSS per day — {tss_str}. "
                f"Do not schedule structured workouts on these days. "
                f"Count the estimated TSS toward the weekly load budget."
            )
        else:
            group_context = (
                "No group ride days configured. When planning a week, ask the athlete "
                "whether they have any group rides planned and on which days, then account "
                "for them in the plan."
            )

        kw_str = ", ".join(self.group_ride_keywords) if self.group_ride_keywords else "none"
        group_context += f" Auto-detection keywords: {kw_str}."
        if self.group_ride_counts_as_intervals:
            ci_str = ", ".join(self.group_ride_counts_as_intervals)
            group_context += (
                f" Group rides on {ci_str} count as one of the {self.hard_intervals_per_week} "
                f"hard interval sessions for the week — reduce planned structured hard sessions "
                f"by 1 on weeks containing a group ride on these days."
            )
        else:
            group_context += " Group rides do not count toward the hard interval quota."

        prompt = SYSTEM_PROMPT.format(
            today=today.isoformat(),
            hrv_context=hrv_context,
            rhr_context=rhr_context,
            block_context=block_context,
            hard_intervals_per_week=self.hard_intervals_per_week,
            group_context=group_context,
        )

        # Cache the result
        self._cached_system_prompt = prompt
        self._cached_config_hash = current_hash
        return prompt

    def _safe_role(self, m) -> str:
        """Get role from either a dict or a ChatCompletionMessage object."""
        return m.get("role") if isinstance(m, dict) else getattr(m, "role", None)

    def chat(self, user_message: str, history: list) -> tuple[str, list]:
        system = self._build_system()

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
        # Keep last 5 exchanges (10 messages), starting on a user message
        trimmed = lean_history[-10:]
        while trimmed and self._safe_role(trimmed[0]) != "user":
            trimmed = trimmed[1:]
        trimmed_history = trimmed

        messages = [{"role": "system", "content": system}]
        messages += trimmed_history
        messages.append({"role": "user", "content": user_message})

        max_iterations = 15
        for _ in range(max_iterations):
            response = self.openai.chat.completions.create(
                model=self.chat_model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
            )
            msg = response.choices[0].message

            if msg.tool_calls:
                # Serialise to dict so history stays JSON-serialisable
                messages.append(msg.model_dump())
                for call in msg.tool_calls:
                    try:
                        args = json.loads(call.function.arguments)
                    except json.JSONDecodeError as e:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": f"Error parsing tool arguments: {e}. Please retry with valid JSON.",
                        })
                        continue
                    result = self._run_tool(call.function.name, args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result,
                    })
            else:
                reply = msg.content
                new_history = messages[1:]
                new_history.append({"role": "assistant", "content": reply})
                return reply, new_history

        raise RuntimeError(f"Tool loop exceeded max iterations ({max_iterations}). LLM may be stuck in a loop.")

    def auto_review(self, activity: dict) -> str:
        """Generate a thorough coach review for a newly uploaded activity."""
        paired_event_id = activity.get("paired_event_id")
        planned_instruction = (
            f"The activity was matched to a planned workout (paired_event_id={paired_event_id}). "
            f"Fetch the planned workout with get_planned_workout({paired_event_id}), then fetch the "
            f"actual intervals with get_activity_intervals. Compare planned vs actual: which targets "
            f"were hit, which were missed, how many reps were completed vs planned."
            if paired_event_id else
            "Fetch the activity intervals with get_activity_intervals for a full breakdown."
        )
        prompt = (
            f"A new ride was just uploaded. {planned_instruction} "
            f"Also fetch coach ticks (get_coach_ticks) so you know the athlete's configured FTP, "
            f"then post a brief coach note on the activity with the most appropriate tick "
            f"(skip tick if list is empty, but always post the comment). "
            f"The note should be 5-10 sentences covering overall execution, standout metrics "
            f"(power consistency, decoupling, HR drift, or planned vs actual), RPE/feel, and "
            f"one or two concrete takeaways. End with: 'Ask me for a deeper analysis if you want more detail.' "
            f"Activity summary: {json.dumps(activity)}"
        )
        system = self._build_system()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]

        max_iterations = 15
        for _ in range(max_iterations):
            response = self.openai.chat.completions.create(
                model=self.chat_model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
            )
            msg = response.choices[0].message
            if msg.tool_calls:
                # Serialise to dict so history stays JSON-serialisable
                messages.append(msg.model_dump())
                for call in msg.tool_calls:
                    try:
                        args = json.loads(call.function.arguments)
                    except json.JSONDecodeError as e:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": f"Error parsing tool arguments: {e}. Please retry with valid JSON.",
                        })
                        continue
                    result = self._run_tool(call.function.name, args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result,
                    })
            else:
                return msg.content

        raise RuntimeError(f"Auto-review tool loop exceeded max iterations ({max_iterations}).")
