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

    DEFAULT_COACH_TICKS = [
        {"id": 1, "text": "Really bad"},
        {"id": 2, "text": "Poor"},
        {"id": 3, "text": "Decent"},
        {"id": 4, "text": "Good"},
        {"id": 5, "text": "Amazing"},
    ]

    def get_athlete(self):
        """Fetch athlete profile including available coach_ticks and sport settings.
        If no ticks are configured, creates the default set automatically."""
        data = self._get(f"/athlete/{self.athlete_id}")
        ticks = data.get("coach_ticks", [])
        if not ticks:
            self._ensure_default_coach_ticks()
            ticks = self.DEFAULT_COACH_TICKS

        # Extract user-configured FTP and LTHR from sport settings (prefer Ride)
        ftp = None
        lthr = None
        max_hr = None
        sport_settings = data.get("sportSettings") or []
        # Prefer the Ride entry; fall back to the first entry with an FTP set
        ride_settings = next(
            (s for s in sport_settings if "Ride" in (s.get("types") or [])), None
        ) or next((s for s in sport_settings if s.get("ftp")), None)
        if ride_settings:
            ftp = ride_settings.get("ftp")
            lthr = ride_settings.get("lthr")
            max_hr = ride_settings.get("max_hr")

        return {
            "id": data.get("id"),
            "name": data.get("name"),
            "coach_ticks": ticks,
            "ftp": ftp,           # user-configured FTP (watts) — use this, not eFTP
            "lthr": lthr,         # user-configured lactate threshold HR
            "max_hr": max_hr,     # user-configured max HR
        }

    def _ensure_default_coach_ticks(self):
        """Create default coach ticks on the athlete profile if none exist."""
        try:
            self._put(f"/athlete/{self.athlete_id}", {"coach_ticks": self.DEFAULT_COACH_TICKS})
            print("Default coach ticks created.", flush=True)
        except Exception as e:
            print(f"Warning: could not create default coach ticks: {e}", flush=True)

    def get_activities(self, days_back: int = 7, max_days_back: int = None, group_keywords: list[str] = None):
        if max_days_back:
            days_back = min(days_back, max_days_back)
        oldest = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        newest = datetime.now().strftime("%Y-%m-%d")
        activities = self._get(
            f"/athlete/{self.athlete_id}/activities",
            params={"oldest": oldest, "newest": newest},
        )
        return [self._simplify_activity(a, group_keywords) for a in activities]

    def get_activities_since(self, since: datetime, group_keywords: list[str] = None):
        """Fetch activities uploaded since a given datetime — used for auto-review polling."""
        oldest = since.strftime("%Y-%m-%d")
        newest = datetime.now().strftime("%Y-%m-%d")
        activities = self._get(
            f"/athlete/{self.athlete_id}/activities",
            params={"oldest": oldest, "newest": newest},
        )
        result = []
        for a in activities:
            created = a.get("created") or a.get("start_date_local", "")
            try:
                ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if ts.replace(tzinfo=None) >= since:
                    result.append(self._simplify_activity(a, group_keywords))
            except Exception:
                pass
        return result

    def _is_group_ride(self, a: dict, keywords: list[str]) -> bool:
        """Detect if an activity is a group ride based on name or tags."""
        if not keywords:
            return False
        name = (a.get("name") or "").lower()
        tags = [t.lower() for t in (a.get("tags") or [])]
        return any(kw.lower() in name or kw.lower() in tags for kw in keywords)

    @staticmethod
    def _strip_nulls(d: dict) -> dict:
        return {k: v for k, v in d.items() if v is not None}

    def _simplify_activity(self, a: dict, group_keywords: list[str] = None) -> dict:
        raw_power_zones = a.get("icu_zone_times")
        raw_hr_zones = a.get("icu_hr_zone_times")
        return {
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
            "feel": a.get("feel"),                   # 1=Strong, 2=Good, 3=Normal, 4=Poor, 5=Weak
            "compliance": a.get("icu_compliance"),   # >0 means matched a planned workout
            "paired_event_id": a.get("paired_event_id"),  # planned workout event ID, use with get_planned_workout
            "power_zone_mins": [round(z["secs"] / 60, 1) for z in raw_power_zones] if raw_power_zones else None,
            "hr_zone_mins": [round(s / 60, 1) for s in raw_hr_zones] if raw_hr_zones else None,
            "decoupling": a.get("decoupling"),
            "efficiency_factor": a.get("icu_efficiency_factor"),
            "variability_index": a.get("icu_variability_index"),
            "power_hr_ratio": a.get("icu_power_hr"),
            "polarization_index": a.get("polarization_index"),
            "strain_score": a.get("strain_score"),
            "avg_cadence": a.get("average_cadence"),
            "coach_tick": a.get("coach_tick"),       # ID of coach tick if already reviewed
            "description": a.get("description"),     # coach comment lives here
            "group_ride": self._is_group_ride(a, group_keywords),
        }

    def get_activity_detail(self, activity_id: str):
        return self._get(f"/athlete/{self.athlete_id}/activities/{activity_id}")

    def get_event(self, event_id: int) -> dict:
        """Fetch a single calendar event (planned workout) by ID."""
        e = self._get(f"/athlete/{self.athlete_id}/events/{event_id}")
        return {
            "id": e.get("id"),
            "date": e.get("start_date_local", "")[:10],
            "name": e.get("name"),
            "category": e.get("category"),
            "description": e.get("description"),  # workout structure in Intervals.icu format
            "duration_seconds": e.get("moving_time"),
            "tss": e.get("icu_training_load"),
            "type": e.get("type"),
        }

    def get_activity_intervals(self, activity_id: str):
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
                "type": iv.get("type"),
                "group_id": iv.get("group_id"),
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
                "category": e.get("category"),
                "type": e.get("type"),
                "sub_type": e.get("sub_type"),
                "description": e.get("description"),
                "duration_seconds": e.get("moving_time"),
                "distance_meters": e.get("distance"),
                "tss": e.get("icu_training_load"),
                "indoor": e.get("indoor"),
                "tags": e.get("tags"),
            })
        return simplified

    def post_activity_comment(self, activity_id: str, comment: str, coach_tick_id: int = None):
        """Post a comment and optionally set a coach tick to mark as reviewed."""
        payload = {"description": comment}
        if coach_tick_id is not None:
            payload["coach_tick"] = coach_tick_id
        return self._put(f"/activity/{activity_id}", payload)

    def set_coach_tick(self, activity_id: str, coach_tick_id: int):
        """Mark an activity as coach-reviewed without changing the description."""
        return self._put(f"/activity/{activity_id}", {"coach_tick": coach_tick_id})

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
        return self._post(f"/athlete/{self.athlete_id}/events", event)

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
        payload = {}
        if name: payload["name"] = name
        if description: payload["description"] = description
        if date: payload["start_date_local"] = f"{date}T00:00:00"
        if sport_type: payload["type"] = sport_type
        if planned_duration_seconds: payload["moving_time"] = planned_duration_seconds
        if planned_tss: payload["icu_training_load"] = planned_tss
        if category: payload["category"] = category
        return self._put(f"/athlete/{self.athlete_id}/events/{event_id}", payload)

    def get_upcoming_races(self, days_ahead: int = 120) -> list:
        """Fetch upcoming A-priority race events, sorted by date."""
        from datetime import datetime, timedelta
        oldest = datetime.now().strftime("%Y-%m-%d")
        newest = (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        events = self._get(
            f"/athlete/{self.athlete_id}/events",
            params={"oldest": oldest, "newest": newest},
        )
        races = []
        for e in events:
            if e.get("category") == "RACE_A":
                start = e.get("start_date_local", "")[:10]
                end = e.get("end_date_local", "")[:10]
                # Multi-day if end is set and different from start
                multi_day = bool(end and end != start and end > start)
                races.append({
                    "id": e.get("id"),
                    "name": e.get("name"),
                    "start_date": start,
                    "end_date": end if multi_day else start,
                    "multi_day": multi_day,
                    "type": e.get("type"),
                    "description": e.get("description"),
                })
        return sorted(races, key=lambda r: r["start_date"])

    def get_weekly_note(self, monday_date: str) -> dict | None:
        """Fetch the NOTE event on a given Monday (YYYY-MM-DD). Returns None if not found."""
        events = self._get(
            f"/athlete/{self.athlete_id}/events",
            params={"oldest": monday_date, "newest": monday_date},
        )
        for e in events:
            if e.get("category") == "NOTE":
                return {
                    "id": e.get("id"),
                    "date": monday_date,
                    "name": e.get("name", ""),
                    "description": e.get("description", ""),
                }
        return None

    def write_weekly_note(self, monday_date: str, name: str, content: str, event_id: int = None) -> dict:
        """Create or update a NOTE event on the given Monday."""
        payload = {
            "category": "NOTE",
            "start_date_local": f"{monday_date}T00:00:00",
            "name": name,
            "description": content,
        }
        if event_id:
            return self._put(f"/athlete/{self.athlete_id}/events/{event_id}", payload)
        else:
            return self._post(f"/athlete/{self.athlete_id}/events", payload)

    def delete_event(self, event_id: str):
        r = requests.delete(
            f"{self.BASE_URL}/athlete/{self.athlete_id}/events/{event_id}",
            headers=self.headers,
        )
        r.raise_for_status()
        return {"deleted": event_id}
