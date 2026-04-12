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
            "name": "post_activity_comment",
            "description": "Post a coach comment on a completed activity in Intervals.icu.",
            "parameters": {
                "type": "object",
                "properties": {
                    "activity_id": {"type": "string"},
                    "comment": {"type": "string"},
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
            "description": "Update/edit an existing planned workout on the athlete's Intervals.icu calendar. Use this to change the name, description, date, sport type, duration or TSS of a planned workout.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "The event ID to update."},
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

Your role:
- Analyse recent training and provide honest, specific feedback
- Drill into individual interval sessions when asked — compare work efforts, check if targets were hit
- Comment on completed workouts
- Plan and schedule future workouts based on goals and fatigue
- Adjust training plans based on athlete requests or wellness data

Guidelines:
- Always fetch relevant data before commenting — never assume what a workout looks like
- Weeks start on Monday (European standard). When referring to "this week" or "last week", Monday is the first day.
- For interval analysis: fetch activities first to get the ID, then fetch intervals for that activity
- When analysing intervals, comment on consistency across efforts, power/HR drift, 
  work-to-rest ratio, and whether targets were met
- Speed is stored as m/s — convert to min/km (pace = 1000/speed/60) or km/h as appropriate
- power_zone_times is an array of seconds spent in each power zone [Z1, Z2, Z3, Z4, Z5, Z6, Z7]
- hr_zone_times is an array of seconds spent in each HR zone [Z1, Z2, Z3, Z4, Z5]
- When discussing zone distribution, convert seconds to minutes and comment on the balance
- When creating planned workouts, write detailed descriptions: warm-up, main set, cool-down with targets
- Consider cumulative fatigue before adding hard sessions
- Confirm with the athlete before making changes to the calendar
- Today's date: {today}

Sport types: Ride
"""


class TrainingAgent:
    def __init__(self, openai_api_key: str, intervals_athlete_id: str, intervals_api_key: str):
        self.openai = OpenAI(api_key=openai_api_key)
        self.icu = IntervalsClient(intervals_athlete_id, intervals_api_key)

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
            elif name == "post_activity_comment":
                result = self.icu.post_activity_comment(args["activity_id"], args["comment"])
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
            elif name == "delete_planned_workout":
                result = self.icu.delete_event(args["event_id"])
            else:
                result = {"error": f"Unknown tool: {name}"}
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def chat(self, user_message: str, history: list) -> tuple[str, list]:
        from datetime import date
        system = SYSTEM_PROMPT.format(today=date.today().isoformat())

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
