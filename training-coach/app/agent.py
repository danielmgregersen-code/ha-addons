import json
from openai import OpenAI
from intervals import IntervalsClient

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_recent_activities",
            "description": "Fetch the athlete's recent activities/workouts from Intervals.icu.",
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
            "name": "get_wellness",
            "description": "Fetch the athlete's wellness data (HRV, resting HR, sleep, fatigue, form).",
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

SYSTEM_PROMPT = """You are an expert triathlon and endurance sports coach with direct access 
to the athlete's training data via Intervals.icu.

Your role:
- Analyse recent training and provide honest, specific feedback
- Comment on individual workouts when asked
- Plan and schedule future workouts based on the athlete's goals and fatigue
- Adjust training plans based on athlete requests or wellness data
- Explain your reasoning clearly

Guidelines:
- Always fetch relevant data before commenting — don't assume what workouts look like
- When creating planned workouts, write detailed descriptions with warm-up, main set, and cool-down
- Be specific with targets: heart rate zones, power zones, pace ranges
- Consider cumulative fatigue — check wellness and recent load before adding hard sessions
- Confirm with the athlete before making changes to the calendar
- Today's date: {today}

Sport types: Ride, Run, Swim, VirtualRide, VirtualRun, Walk, WeightTraining, Yoga, Other
"""


class TrainingAgent:
    def __init__(self, openai_api_key: str, intervals_athlete_id: str, intervals_api_key: str):
        self.openai = OpenAI(api_key=openai_api_key)
        self.icu = IntervalsClient(intervals_athlete_id, intervals_api_key)

    def _run_tool(self, name: str, args: dict) -> str:
        try:
            if name == "get_recent_activities":
                result = self.icu.get_activities(args.get("days_back", 14))
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
                model="gpt-4o",
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
