import json
import os
import threading
from datetime import datetime

CHAINS_FILE = "/data/chains.json"

# Condition → wear multiplier, keyed by bike type. These mirror the "Opslag"
# lookup sheet, which maintains a separate condition set per bike type.
CONDITION_MULTIPLIERS = {
    "gravel": {
        "Home Trainer": 0.2,
        "Tarmac": 0.75,
        "Mostly Tarmac": 0.85,
        "Standard Dry": 1.0,
        "Pure Dust": 1.25,
        "Intermittent Puddles": 1.5,
        "Constant Rain": 2.5,
        "Heavy Mud": 4.0,
    },
    "road": {
        "Home Trainer": 0.3,
        "Standard Dry": 1.0,
        "Fast Group/intervals": 1.15,
        "Damp Roads": 1.5,
        "Active Rain": 2.5,
    },
}


def conditions_for(bike_type: str) -> dict:
    """Return the condition→multiplier map for a bike type (defaults to road)."""
    return CONDITION_MULTIPLIERS.get(bike_type, CONDITION_MULTIPLIERS["road"])


def infer_condition(sport_type: str, activity: dict, bike_type: str = "road") -> tuple[str, float]:
    """Return (condition_name, multiplier) from activity sport type and weather data,
    selecting from the condition set appropriate to the chain's bike type."""
    table = conditions_for(bike_type)

    if sport_type in ("VirtualRide", "IndoorRide") or activity.get("trainer"):
        return "Home Trainer", table["Home Trainer"]

    precip = activity.get("weather_precipitation") or 0
    humidity = activity.get("weather_humidity") or 0

    if bike_type == "gravel":
        if precip > 2.0:
            return "Constant Rain", table["Constant Rain"]
        if precip > 0.1 or humidity > 85:
            return "Intermittent Puddles", table["Intermittent Puddles"]
        if humidity > 70:
            return "Mostly Tarmac", table["Mostly Tarmac"]
        return "Standard Dry", table["Standard Dry"]

    # road
    if precip > 2.0:
        return "Active Rain", table["Active Rain"]
    if precip > 0.1 or humidity > 85:
        return "Damp Roads", table["Damp Roads"]
    return "Standard Dry", table["Standard Dry"]


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

    def get_gear_id_map(self) -> dict[str, dict]:
        """Return {gear_id: {"id", "bike_type"}} for active chains with a gear_id set."""
        with self._lock:
            return {
                c["gear_id"]: {"id": c["id"], "bike_type": c.get("bike_type", "road")}
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
        manual_id = f"manual-{date}-{chain_id}-{int(duration_hours*100)}"
        with self._lock:
            chain = next(
                (c for c in self._data.get("chains", []) if c["id"] == chain_id), None
            )
            if not chain:
                raise ValueError(f"Chain {chain_id!r} not found")
            # Resolve the multiplier from this chain's bike-type condition set.
            multiplier = conditions_for(chain.get("bike_type", "road")).get(condition, 1.0)
            hours_consumed = round(duration_hours * multiplier, 4)
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

    def update_wear_entry(
        self,
        chain_id: str,
        activity_id: str,
        new_condition: str = None,
        new_chain_id: str = None,
    ) -> dict:
        """Update condition and/or move an entry to a different chain.
        Recalculates hours_consumed from the target chain's bike_type multiplier table."""
        with self._lock:
            chain = next(
                (c for c in self._data.get("chains", []) if c["id"] == chain_id), None
            )
            if not chain:
                raise ValueError(f"Chain {chain_id!r} not found")
            wear_log = chain.get("wear_log", [])
            entry = next((e for e in wear_log if e["activity_id"] == activity_id), None)
            if not entry:
                raise ValueError(f"Wear entry {activity_id!r} not found on chain {chain_id!r}")

            target_chain = chain
            moving = new_chain_id and new_chain_id != chain_id
            if moving:
                target_chain = next(
                    (c for c in self._data.get("chains", []) if c["id"] == new_chain_id), None
                )
                if not target_chain:
                    raise ValueError(f"Target chain {new_chain_id!r} not found")

            condition = new_condition or entry["condition"]
            bike_type = target_chain.get("bike_type", "road")
            multiplier = conditions_for(bike_type).get(condition, 1.0)
            hours_consumed = round(entry["duration_hours"] * multiplier, 4)

            updated = {**entry, "condition": condition, "multiplier": multiplier, "hours_consumed": hours_consumed}

            if moving:
                chain["wear_log"] = [e for e in wear_log if e["activity_id"] != activity_id]
                # Remove any duplicate in target, then append
                target_chain["wear_log"] = [
                    e for e in target_chain.setdefault("wear_log", []) if e["activity_id"] != activity_id
                ]
                target_chain["wear_log"].append(updated)
            else:
                for i, e in enumerate(wear_log):
                    if e["activity_id"] == activity_id:
                        wear_log[i] = updated
                        break

            self._save()
            return self._compute_status(target_chain)
