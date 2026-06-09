import json
import os
import threading
from datetime import datetime

CHAINS_FILE = "/data/chains.json"

CONDITION_MULTIPLIERS = {
    "Home Trainer": 0.2,
    "Tarmac": 0.8,
    "Mostly Tarmac": 0.9,
    "Standard Dry": 1.0,
    "Intermittent Puddles": 1.5,
    "Damp Roads": 2.0,
    "Active Rain": 3.0,
    "Heavy Mud": 4.0,
}


def infer_condition(sport_type: str, activity: dict) -> tuple[str, float]:
    """Return (condition_name, multiplier) from activity sport type and weather data."""
    if sport_type in ("VirtualRide", "IndoorRide") or activity.get("trainer"):
        return "Home Trainer", 0.2

    precip = activity.get("weather_precipitation") or 0
    humidity = activity.get("weather_humidity") or 0

    if precip > 2.0:
        return "Active Rain", 3.0
    if precip > 0.1 or humidity > 85:
        return "Damp Roads", 2.0
    if humidity > 70:
        return "Intermittent Puddles", 1.5

    if sport_type == "GravelRide":
        return "Standard Dry", 1.0
    return "Tarmac", 0.8


class ChainManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._data: dict = {"chains": []}
        self._load()

    def _load(self):
        if os.path.exists(CHAINS_FILE):
            try:
                with open(CHAINS_FILE) as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._data = {"chains": []}

    def _save(self):
        try:
            with open(CHAINS_FILE, "w") as f:
                json.dump(self._data, f, indent=2)
        except IOError as e:
            print(f"Warning: could not save chains: {e}", flush=True)

    def _compute_status(self, chain: dict) -> dict:
        base = chain.get("base_wax_hours", 12)
        wax_events = chain.get("wax_events", [])
        wear_log = chain.get("wear_log", [])

        last_wax_date = max((e["date"] for e in wax_events), default=None)

        hours_consumed = sum(
            e.get("hours_consumed", 0)
            for e in wear_log
            if last_wax_date is None or e["date"] >= last_wax_date
        )

        hours_remaining = max(0.0, base - hours_consumed)
        pct_remaining = (hours_remaining / base * 100) if base > 0 else 0
        last_sealant_date = max(
            (e["date"] for e in chain.get("sealant_events", [])), default=None
        )

        return {
            **chain,
            "hours_consumed_since_wax": round(hours_consumed, 2),
            "hours_remaining": round(hours_remaining, 2),
            "pct_remaining": round(pct_remaining, 1),
            "last_wax_date": last_wax_date,
            "last_sealant_date": last_sealant_date,
        }

    def get_all(self) -> list[dict]:
        with self._lock:
            return [self._compute_status(c) for c in self._data.get("chains", [])]

    def get_gear_id_map(self) -> dict[str, str]:
        """Return {gear_id: chain_id} for all active chains with a gear_id set."""
        with self._lock:
            return {
                c["gear_id"]: c["id"]
                for c in self._data.get("chains", [])
                if c.get("gear_id") and c.get("active", True)
            }

    def upsert_chain(self, data: dict) -> dict:
        """Create or update a chain. data must include 'id'."""
        with self._lock:
            chains = self._data.setdefault("chains", [])
            existing = next((c for c in chains if c["id"] == data["id"]), None)
            if existing:
                for k, v in data.items():
                    if v is not None:
                        existing[k] = v
            else:
                chains.append({
                    "id": data["id"],
                    "name": data.get("name", data["id"]),
                    "gear_id": data.get("gear_id", ""),
                    "bike_type": data.get("bike_type", "road"),
                    "base_wax_hours": data.get("base_wax_hours", 12),
                    "active": data.get("active", True),
                    "wax_events": [],
                    "sealant_events": [],
                    "wear_log": [],
                })
            self._save()
            updated = next(c for c in self._data["chains"] if c["id"] == data["id"])
            return self._compute_status(updated)

    def delete_chain(self, chain_id: str):
        with self._lock:
            self._data["chains"] = [
                c for c in self._data.get("chains", []) if c["id"] != chain_id
            ]
            self._save()

    def log_wax_event(self, chain_id: str, date: str = None, note: str = "") -> dict:
        date = date or datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            chain = next(
                (c for c in self._data.get("chains", []) if c["id"] == chain_id), None
            )
            if not chain:
                raise ValueError(f"Chain {chain_id!r} not found")
            chain.setdefault("wax_events", []).append({"date": date, "note": note})
            self._save()
            return self._compute_status(chain)

    def log_sealant_event(self, chain_id: str, date: str = None, note: str = "") -> dict:
        date = date or datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            chain = next(
                (c for c in self._data.get("chains", []) if c["id"] == chain_id), None
            )
            if not chain:
                raise ValueError(f"Chain {chain_id!r} not found")
            chain.setdefault("sealant_events", []).append({"date": date, "note": note})
            self._save()
            return self._compute_status(chain)

    def log_activity_wear(
        self,
        activity_id: str,
        activity_name: str,
        date: str,
        chain_id: str,
        duration_seconds: int,
        condition: str,
        multiplier: float,
    ) -> dict:
        duration_hours = round((duration_seconds or 0) / 3600, 4)
        hours_consumed = round(duration_hours * multiplier, 4)
        with self._lock:
            chain = next(
                (c for c in self._data.get("chains", []) if c["id"] == chain_id), None
            )
            if not chain:
                raise ValueError(f"Chain {chain_id!r} not found")
            wear_log = chain.setdefault("wear_log", [])
            if any(e["activity_id"] == activity_id for e in wear_log):
                return self._compute_status(chain)
            wear_log.append({
                "activity_id": activity_id,
                "activity_name": activity_name,
                "date": date,
                "duration_hours": duration_hours,
                "condition": condition,
                "multiplier": multiplier,
                "hours_consumed": hours_consumed,
            })
            self._save()
            return self._compute_status(chain)

    def log_manual_wear(
        self,
        chain_id: str,
        date: str,
        duration_hours: float,
        condition: str,
    ) -> dict:
        multiplier = CONDITION_MULTIPLIERS.get(condition, 1.0)
        hours_consumed = round(duration_hours * multiplier, 4)
        manual_id = f"manual-{date}-{chain_id}-{int(duration_hours*100)}"
        with self._lock:
            chain = next(
                (c for c in self._data.get("chains", []) if c["id"] == chain_id), None
            )
            if not chain:
                raise ValueError(f"Chain {chain_id!r} not found")
            wear_log = chain.setdefault("wear_log", [])
            if any(e["activity_id"] == manual_id for e in wear_log):
                return self._compute_status(chain)
            wear_log.append({
                "activity_id": manual_id,
                "activity_name": "Manual entry",
                "date": date,
                "duration_hours": round(duration_hours, 4),
                "condition": condition,
                "multiplier": multiplier,
                "hours_consumed": hours_consumed,
            })
            self._save()
            return self._compute_status(chain)

    def delete_wear_entry(self, chain_id: str, activity_id: str) -> dict:
        with self._lock:
            chain = next(
                (c for c in self._data.get("chains", []) if c["id"] == chain_id), None
            )
            if not chain:
                raise ValueError(f"Chain {chain_id!r} not found")
            chain["wear_log"] = [
                e for e in chain.get("wear_log", []) if e["activity_id"] != activity_id
            ]
            self._save()
            return self._compute_status(chain)
