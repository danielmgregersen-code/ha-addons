import requests
import base64
from datetime import datetime, timedelta


class IntervalsClient:
    BASE_URL = "https://intervals.icu/api/v1"

    def __init__(self, athlete_id: str, api_key: str):
        self.athlete_id = athlete_id
        auth = base64.b64encode(f"API_KEY:{api_key}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict = None):
        r = requests.get(f"{self.BASE_URL}{path}", headers=self.headers, params=params)
        r.raise_for_status()
        return r.json()

    def _put(self, path: str, data: dict):
        r = requests.put(f"{self.BASE_URL}{path}", headers=self.headers, json=data)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, data: dict):
        r = requests.post(f"{self.BASE_URL}{path}", headers=self.headers, json=data)
        r.raise_for_status()
        return r.json()

    def get_activities(self, days_back: int = 14):
        oldest = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        newest = datetime.now().strftime("%Y-%m-%d")
        activities = self._get(
            f"/athlete/{self.athlete_id}/activities",
            params={"oldest": oldest, "newest": newest},
        )
        simplified = []
        for a in activities:
            simplified.append({
                "id": a.get("id"),
                "name": a.get("name"),
                "type": a.get("type"),
                "date": a.get("start_date_local", "")[:10],
                "duration_seconds": a.get("moving_time"),
                "distance_meters": a.get("distance"),
                "avg_hr": a.get("average_heartrate"),
                "max_hr": a.get("max_heartrate"),
                "avg_power": a.get("average_watts"),
                "tss": a.get("icu_training_load"),
                "intensity": a.get("icu_intensity"),
                "rpe": a.get("icu_rpe"),
                "feel": a.get("feel"),
                "description": a.get("description"),
                "coach_text": a.get("coach_text"),
            })
        return simplified

    def get_activity_detail(self, activity_id: str):
        """Get full detail of a single activity including all metrics."""
        return self._get(f"/athlete/{self.athlete_id}/activities/{activity_id}")

    def get_activity_intervals(self, activity_id: str):
        """
        Get individual interval/lap data for a specific activity.
        Intervals.icu returns icu_intervals (each effort) and icu_groups (repeated sets).
        """
        try:
            data = self._get(f"/activity/{activity_id}/intervals")
        except Exception as e:
            return {"error": str(e), "activity_id": activity_id}

        raw_intervals = data.get("icu_intervals", [])
        raw_groups = data.get("icu_groups", [])

        if not raw_intervals:
            return {
                "activity_id": activity_id,
                "interval_count": 0,
                "message": "No interval data found for this activity.",
                "available_keys": list(data.keys()) if isinstance(data, dict) else str(type(data)),
            }

        simplified = []
        for iv in raw_intervals:
            simplified.append({
                "type": iv.get("type"),                       # WORK / RECOVERY
                "group_id": iv.get("group_id"),               # e.g. "599s@271w87rpm"
                "duration_seconds": iv.get("moving_time"),
                "distance_meters": round(iv.get("distance") or 0, 1),
                "avg_power": iv.get("average_watts"),
                "max_power": iv.get("max_watts"),
                "normalized_power": iv.get("weighted_average_watts"),
                "avg_hr": iv.get("average_heartrate"),
                "max_hr": iv.get("max_heartrate"),
                "avg_cadence": round(iv.get("average_cadence") or 0),
                "intensity_pct": iv.get("intensity"),
                "tss": round(iv.get("training_load") or 0, 1),
                "zone": iv.get("zone"),
                "decoupling_pct": iv.get("decoupling"),
                "joules_above_ftp": iv.get("joules_above_ftp"),
            })

        groups = []
        for g in raw_groups:
            groups.append({
                "group_id": g.get("id"),
                "count": g.get("count"),
                "avg_power": g.get("average_watts"),
                "avg_hr": g.get("average_heartrate"),
                "zone": g.get("zone"),
            })

        return {
            "activity_id": activity_id,
            "interval_count": len(simplified),
            "intervals": simplified,
            "groups": groups,
        }

    def get_wellness(self, days_back: int = 14):
        oldest = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        newest = datetime.now().strftime("%Y-%m-%d")
        return self._get(
            f"/athlete/{self.athlete_id}/wellness",
            params={"oldest": oldest, "newest": newest},
        )

    def get_events(self, days_ahead: int = 21):
        oldest = datetime.now().strftime("%Y-%m-%d")
        newest = (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        events = self._get(
            f"/athlete/{self.athlete_id}/events",
            params={"oldest": oldest, "newest": newest},
        )
        simplified = []
        for e in events:
            simplified.append({
                "id": e.get("id"),
                "date": e.get("start_date_local", "")[:10],
                "name": e.get("name"),
                "category": e.get("category"),        # WORKOUT, RACE_A, RACE_B, RACE_C, etc.
                "type": e.get("type"),                 # GravelRide, Ride, Run, Swim, etc.
                "sub_type": e.get("sub_type"),         # NONE, RACE, WARMUP, COOLDOWN, COMMUTE
                "description": e.get("description"),
                "duration_seconds": e.get("moving_time"),
                "distance_meters": e.get("distance"),
                "tss": e.get("icu_training_load"),
                "indoor": e.get("indoor"),
                "tags": e.get("tags"),
            })
        return simplified

    def post_activity_comment(self, activity_id: str, comment: str):
        # Correct endpoint: PUT /api/v1/activity/{id} (no athlete ID in path)
        return self._put(
            f"/activity/{activity_id}",
            {"coach_text": comment},
        )

    def create_planned_workout(
        self,
        date: str,
        name: str,
        description: str,
        sport_type: str = "Ride",
        planned_duration_seconds: int = None,
        planned_tss: int = None,
    ):
        event = {
            "category": "WORKOUT",
            "start_date_local": f"{date}T00:00:00",
            "name": name,
            "description": description,
            "type": sport_type,
        }
        if planned_duration_seconds:
            event["moving_time"] = planned_duration_seconds
        if planned_tss:
            event["icu_training_load"] = planned_tss
        return self._post(f"/athlete/{self.athlete_id}/events", [event])

    def update_planned_workout(
        self,
        event_id: str,
        name: str = None,
        description: str = None,
        date: str = None,
        sport_type: str = None,
        planned_duration_seconds: int = None,
        planned_tss: int = None,
        category: str = None,
    ):
        # PUT /api/v1/athlete/{id}/events/{eventId}
        payload = {}
        if name: payload["name"] = name
        if description: payload["description"] = description
        if date: payload["start_date_local"] = f"{date}T00:00:00"
        if sport_type: payload["type"] = sport_type
        if planned_duration_seconds: payload["moving_time"] = planned_duration_seconds
        if planned_tss: payload["icu_training_load"] = planned_tss
        if category: payload["category"] = category
        return self._put(f"/athlete/{self.athlete_id}/events/{event_id}", payload)

    def delete_event(self, event_id: str):
        r = requests.delete(
            f"{self.BASE_URL}/athlete/{self.athlete_id}/events/{event_id}",
            headers=self.headers,
        )
        r.raise_for_status()
        return {"deleted": event_id}
