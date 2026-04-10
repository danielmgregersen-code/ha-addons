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
                "description": a.get("description"),
                "coach_text": a.get("coach_text"),
            })
        return simplified

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
        return self._get(
            f"/athlete/{self.athlete_id}/events",
            params={"oldest": oldest, "newest": newest},
        )

    def post_activity_comment(self, activity_id: str, comment: str):
        return self._put(
            f"/athlete/{self.athlete_id}/activity/{activity_id}",
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

    def delete_event(self, event_id: str):
        r = requests.delete(
            f"{self.BASE_URL}/athlete/{self.athlete_id}/events/{event_id}",
            headers=self.headers,
        )
        r.raise_for_status()
        return {"deleted": event_id}
