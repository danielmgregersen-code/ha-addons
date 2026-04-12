import json
from openai import OpenAI
from intervals import IntervalsClient

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_recent_activities",
            "description": "Fetch the athlete's recent activities/workouts from Intervals.icu. Use this to review training history.",
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
            "name": "get_coach_ticks",
            "description": "Fetch the available coach tick options for this athlete. Call this before posting a comment so you know which tick IDs are available to mark the workout as reviewed.",
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

Block structure:
- Week 1 (Foundation): aerobic base, zone 2 focus, {hard_intervals_per_week} light interval sessions (e.g. tempo or sweet spot), moderate TSS
- Week 2 (Build): increase load ~10%, {hard_intervals_per_week} structured interval sessions (threshold or VO2), higher TSS
- Week 3 (Peak): highest load of the block, {hard_intervals_per_week} hard interval sessions (VO2max or above threshold), push TSS
- Week 4 (Recovery): drop volume to ~50-60% of peak week, no hard intervals, zone 1-2 only, let adaptations consolidate

Your role:
- Analyse recent training and provide honest, specific feedback
- Drill into individual interval sessions when asked — compare work efforts, check if targets were hit
- Comment on completed workouts and mark them as coach-reviewed using a coach tick
- Plan and schedule future workouts based on goals and fatigue
- Adjust training plans based on athlete requests or wellness data

Guidelines:
- Always fetch relevant data before commenting — never assume what a workout looks like
- Weeks start on Monday (European standard). When referring to "this week" or "last week", Monday is the first day.
- For interval analysis: fetch activities first to get the ID, then fetch intervals for that activity
- When analysing intervals, comment on consistency across efforts, power/HR drift, 
  work-to-rest ratio, and whether targets were met
- Speed is stored as m/s — convert to min/km (pace = 1000/speed/60) or km/h as appropriate
- When creating planned workouts, write detailed descriptions: warm-up, main set, cool-down with targets
- Consider cumulative fatigue before adding hard sessions
- Confirm with the athlete before making changes to the calendar
- Weekly planning note: always fetch the Monday note before analysing or planning a week. If none exists, write one based on the current block week, planned sessions, and expected TSS. Update it when the week's plan changes
- When writing a weekly note include: block week and focus, each planned session with day/type/targets/duration, total weekly TSS estimate
- feel scale: 1=Strong, 2=Good, 3=Normal, 4=Poor, 5=Weak — lower is better
- power_zone_times is an array of seconds spent in each power zone [Z1, Z2, Z3, Z4, Z5, Z6, Z7]
- Sweet spot is an overlapping zone (typically ~88–93% FTP) that sits between Z3 and Z4 — it is NOT part of the 7-zone model and must never be added to or subtracted from any individual zone. Treat it as a separate descriptor only
- hr_zone_times is an array of seconds spent in each HR zone [Z1, Z2, Z3, Z4, Z5]
- When discussing zone distribution, convert seconds to minutes and comment on the balance
- compliance field: if >0 the ride matched a planned workout; if null/0 it was unstructured
- For MATCHED workouts (compliance > 0): focus primarily on interval execution — did efforts hit targets, how consistent were the reps, power/HR per interval. Cover zone distribution briefly as secondary context
- For UNMATCHED/UNSTRUCTURED rides (compliance null or 0): give equal weight to interval efforts and zone distribution — both tell the story of what kind of ride it was
- decoupling: aerobic decoupling % — HR drift relative to power over a ride; <5% is well-coupled, >10% suggests fatigue or heat
- efficiency_factor: power/HR ratio — higher is more aerobically efficient; rising over time is a good sign
- variability_index: normalised power / average power — closer to 1.0 means steady effort, higher means variable pacing
- polarization_index: distribution between low and high intensity; higher = more polarised training
- When posting a comment, ALWAYS also fetch coach ticks and set an appropriate coach_tick_id to mark the workout as reviewed
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
    ):
        self.openai = OpenAI(api_key=openai_api_key)
        self.icu = IntervalsClient(intervals_athlete_id, intervals_api_key)
        self.hrv_min = hrv_min
        self.hrv_max = hrv_max
        self.rhr_min = rhr_min
        self.rhr_max = rhr_max
        self.hard_intervals_per_week = hard_intervals_per_week
        self.block_start_date = block_start_date

    def _run_tool(self, name: str, args: dict) -> str:
        try:
            if name == "get_recent_activities":
                result = self.icu.get_activities(args.get("days_back", 14))
            elif name == "get_activity_intervals":
                result = self.icu.get_activity_intervals(args["activity_id"])
            elif name == "get_wellness":
                result = self.icu.get_wellness(args.get("days_back", 14))
            elif name == "get_planned_workouts":
                result = self.icu.get_events(args.get("days_ahead", 21))
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
        except Exception as e:
            return json.dumps({"error": str(e)})

    def _build_system(self) -> str:
        from datetime import date
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
        from datetime import date as date_cls
        today = date_cls.today()

        # Calculate block week
        block_context = ""
        if self.block_start_date:
            try:
                block_start = date_cls.fromisoformat(self.block_start_date)
                days_in = (today - block_start).days
                block_week = (days_in // 7) % 4 + 1
                week_label = {1: "Week 1 — Foundation", 2: "Week 2 — Build",
                              3: "Week 3 — Peak", 4: "Week 4 — Recovery"}[block_week]
                block_context = f"Current block week: {block_week}/4 ({week_label})"
            except Exception:
                block_context = "Block start date is set but could not be parsed (use YYYY-MM-DD)."
        else:
            block_context = "Block start date not configured — ask the athlete which week of the block they are in if relevant."

        return SYSTEM_PROMPT.format(
            today=today.isoformat(),
            hrv_context=hrv_context,
            rhr_context=rhr_context,
            block_context=block_context,
            hard_intervals_per_week=self.hard_intervals_per_week,
        )

    def chat(self, user_message: str, history: list) -> tuple[str, list]:
        system = self._build_system()
        messages = [{"role": "system", "content": system}]
        messages += history
        messages.append({"role": "user", "content": user_message})

        while True:
            response = self.openai.chat.completions.create(
                model="gpt-5.4-mini",
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
            )
            msg = response.choices[0].message

            if msg.tool_calls:
                messages.append(msg)
                for call in msg.tool_calls:
                    args = json.loads(call.function.arguments)
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

    def auto_review(self, activity: dict) -> str:
        """Generate a short auto-review comment for a newly uploaded activity.
        Posts the comment + coach tick and returns the comment text."""
        matched = activity.get("compliance") and activity.get("compliance") > 0
        focus = (
            "Focus primarily on interval execution vs planned targets, then briefly on zones."
            if matched else
            "Cover both interval efforts and zone distribution with equal weight."
        )
        prompt = (
            f"A new ride was just uploaded. Write a concise coach review (3-5 sentences). {focus} "
            f"Fetch coach ticks first, then post the comment with a tick if available (skip tick if list is empty, but always post the comment). "
            f"Activity data: {json.dumps(activity)}"
        )
        system = self._build_system()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]

        while True:
            response = self.openai.chat.completions.create(
                model="gpt-5.4-mini",
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
            )
            msg = response.choices[0].message
            if msg.tool_calls:
                messages.append(msg)
                for call in msg.tool_calls:
                    args = json.loads(call.function.arguments)
                    result = self._run_tool(call.function.name, args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result,
                    })
            else:
                return msg.content
