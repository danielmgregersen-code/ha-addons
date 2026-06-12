import json
import os
import threading
from datetime import datetime, date as date_type

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

    @staticmethod
    def _wear_ts(entry: dict) -> str:
        """Sortable timestamp for a wear entry. Prefers the ride's precise
        start time; legacy date-only entries fall back to the start of that day."""
        return entry.get("ts") or (entry.get("date", "") + "T00:00:00")

    @staticmethod
    def _wax_ts(event: dict) -> str:
        """Sortable timestamp for a wax event. Prefers the precise time the wax
        was logged; legacy date-only events fall back to the end of that day,
        since waxing is normally done after the day's riding."""
        return event.get("ts") or (event.get("date", "") + "T23:59:59")

    def _compute_status(self, chain: dict) -> dict:
        base = chain.get("base_wax_hours", 12)
        wax_events = chain.get("wax_events", [])
        wear_log = chain.get("wear_log", [])

        last_wax_date = max((e["date"] for e in wax_events), default=None)
        # Compare by timestamp, not date, so a ride and a same-day re-wax are
        # ordered correctly — a ride before the wax must not count against the
        # freshly-waxed chain.
        last_wax_ts = max((self._wax_ts(e) for e in wax_events), default=None)

        hours_consumed = sum(
            e.get("hours_consumed", 0)
            for e in wear_log
            if last_wax_ts is None or self._wear_ts(e) > last_wax_ts
        )

        hours_remaining = max(0.0, base - hours_consumed)
        pct_remaining = (hours_remaining / base * 100) if base > 0 else 0

        return {
            **chain,
            "hours_consumed_since_wax": round(hours_consumed, 2),
            "hours_remaining": round(hours_remaining, 2),
            "pct_remaining": round(pct_remaining, 1),
            "last_wax_date": last_wax_date,
            "last_wax_ts": last_wax_ts,
        }

    def _compute_sealant_status(self, s: dict) -> dict:
        events = s.get("events", [])
        last_checkin = max((e["date"] for e in events), default=None)
        interval_days = s.get("interval_days", 90)
        if last_checkin:
            days_since = (date_type.today() - date_type.fromisoformat(last_checkin)).days
        else:
            days_since = None
        pct_elapsed = round(days_since / interval_days * 100, 1) if (days_since is not None and interval_days > 0) else 0
        return {
            **s,
            "last_checkin": last_checkin,
            "days_since": days_since,
            "pct_elapsed": min(pct_elapsed, 100),
            "overdue": days_since is not None and days_since > interval_days,
        }

    def get_all(self) -> list[dict]:
        with self._lock:
            return [self._compute_status(c) for c in self._data.get("chains", [])]

    @staticmethod
    def _crosses_25(remaining_pct: float, was_sent: bool) -> tuple[bool, bool]:
        """Given a remaining-life percentage and whether the 25% alert was
        already sent for this item, return (newly_crossed, new_sent_state).
        Re-arms once life recovers above 25% (e.g. after a re-wax or sealant
        check-in)."""
        if remaining_pct > 25:
            return False, False
        if was_sent:
            return False, True
        return True, True

    def check_threshold_alerts(self) -> list[dict]:
        """Scan chains and sealants for newly-crossed 25%-remaining-life
        thresholds. Each crossing is reported once (tracked per item via
        `alerts_sent`) until life recovers above 25%. The 0% case is handled
        separately by `get_zero_life_items`, which is polled live so its
        banner persists until the underlying issue is resolved."""
        alerts = []
        with self._lock:
            for c in self._data.get("chains", []):
                if c.get("retired"):
                    continue
                pct = self._compute_status(c)["pct_remaining"]
                fired, new_state = self._crosses_25(pct, c.get("alerts_sent", False))
                c["alerts_sent"] = new_state
                if fired:
                    alerts.append({
                        "kind": "wax",
                        "level": 25,
                        "id": c["id"],
                        "name": c.get("name", c["id"]),
                        "bike_type": c.get("bike_type", "road"),
                    })
            for s in self._data.get("sealants", []):
                status = self._compute_sealant_status(s)
                if status["last_checkin"] is None:
                    continue  # never checked in — nothing to count down from
                remaining = 100 - status["pct_elapsed"]
                fired, new_state = self._crosses_25(remaining, s.get("alerts_sent", False))
                s["alerts_sent"] = new_state
                if fired:
                    alerts.append({
                        "kind": "sealant",
                        "level": 25,
                        "id": s["id"],
                        "name": s.get("name", s["id"]),
                        "bike_type": s.get("bike_type", "road"),
                    })
            self._save()
        return alerts

    def get_zero_life_items(self) -> list[dict]:
        """Items currently at or below 0% life remaining (wax depleted or
        sealant interval fully elapsed). Computed live from current state —
        used to drive a persistent banner that stays until the chain is
        re-waxed or the sealant is checked in."""
        items = []
        with self._lock:
            for c in self._data.get("chains", []):
                if c.get("retired"):
                    continue
                if self._compute_status(c)["pct_remaining"] <= 0:
                    items.append({
                        "kind": "wax",
                        "id": c["id"],
                        "name": c.get("name", c["id"]),
                        "bike_type": c.get("bike_type", "road"),
                    })
            for s in self._data.get("sealants", []):
                status = self._compute_sealant_status(s)
                if status["last_checkin"] is None:
                    continue
                if status["pct_elapsed"] >= 100:
                    items.append({
                        "kind": "sealant",
                        "id": s["id"],
                        "name": s.get("name", s["id"]),
                        "bike_type": s.get("bike_type", "road"),
                    })
        return items

    def match_activity(self, gear_id: str = None, pm_serial: str = None) -> dict | None:
        """Find the active, non-retired chain for an activity, matching by Strava
        gear_id first, then by power meter serial. Returns
        {"id", "bike_type", "active_since"} or None.

        Matching by power meter serial lets wear tracking work without Strava sync —
        the serial identifies the physical bike the chain is on. `active_since` lets
        the caller avoid back-syncing rides from before the chain was activated."""
        def _ref(c):
            return {
                "id": c["id"],
                "bike_type": c.get("bike_type", "road"),
                "active_since": c.get("active_since"),
            }
        with self._lock:
            candidates = [
                c for c in self._data.get("chains", [])
                if c.get("active", True) and not c.get("retired", False)
            ]
            for c in candidates:
                if gear_id and c.get("gear_id") and c["gear_id"] == gear_id:
                    return _ref(c)
            for c in candidates:
                if pm_serial and c.get("pm_serial") and str(c["pm_serial"]) == str(pm_serial):
                    return _ref(c)
        return None

    @staticmethod
    def _same_bike(a: dict, b: dict) -> bool:
        """Two chains are on the same bike if they share a gear_id or a power meter serial."""
        if a.get("gear_id") and a["gear_id"] == b.get("gear_id"):
            return True
        if a.get("pm_serial") and str(a["pm_serial"]) == str(b.get("pm_serial") or ""):
            return True
        return False

    def upsert_chain(self, data: dict) -> dict:
        """Create or update a chain. data must include 'id'.
        If active=True, deactivates other chains on the same bike (shared gear_id or pm_serial)."""
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            chains = self._data.setdefault("chains", [])
            existing = next((c for c in chains if c["id"] == data["id"]), None)
            if existing:
                was_active = existing.get("active", False)
                for k, v in data.items():
                    if v is not None:
                        existing[k] = v
                # Transitioning into active stamps the activation date so wear
                # logging won't back-sync rides from before this point.
                if existing.get("active") and not was_active:
                    existing["active_since"] = today
            else:
                chains.append({
                    "id": data["id"],
                    "name": data.get("name", data["id"]),
                    "gear_id": data.get("gear_id", ""),
                    "pm_serial": data.get("pm_serial", ""),
                    "bike_type": data.get("bike_type", "road"),
                    "base_wax_hours": data.get("base_wax_hours", 12),
                    "active": data.get("active", True),
                    "active_since": today if data.get("active", True) else None,
                    "retired": False,
                    "wax_events": [],
                    "sealant_events": [],
                    "wear_log": [],
                })
            # If activating, deactivate other chains on the same bike
            target = next(c for c in chains if c["id"] == data["id"])
            if target.get("active"):
                for c in chains:
                    if c["id"] != data["id"] and self._same_bike(target, c):
                        c["active"] = False
            self._save()
            return self._compute_status(target)

    def set_active(self, chain_id: str) -> list[dict]:
        """Activate a chain and deactivate all other chains on the same bike
        (shared gear_id or power meter serial)."""
        with self._lock:
            chain = next((c for c in self._data.get("chains", []) if c["id"] == chain_id), None)
            if not chain:
                raise ValueError(f"Chain {chain_id!r} not found")
            if chain.get("retired"):
                raise ValueError(f"Chain {chain_id!r} is retired — restore it first")
            today = datetime.now().strftime("%Y-%m-%d")
            for c in self._data.get("chains", []):
                if c["id"] == chain_id:
                    if not c.get("active"):
                        c["active_since"] = today
                    c["active"] = True
                elif self._same_bike(chain, c):
                    c["active"] = False
            self._save()
            return [self._compute_status(c) for c in self._data["chains"]]

    def retire_chain(self, chain_id: str) -> dict:
        """Mark a chain as retired (worn out). Deactivates it automatically."""
        with self._lock:
            chain = next((c for c in self._data.get("chains", []) if c["id"] == chain_id), None)
            if not chain:
                raise ValueError(f"Chain {chain_id!r} not found")
            chain["retired"] = True
            chain["active"] = False
            self._save()
            return self._compute_status(chain)

    def restore_chain(self, chain_id: str) -> dict:
        """Un-retire a chain. Does not auto-activate it."""
        with self._lock:
            chain = next((c for c in self._data.get("chains", []) if c["id"] == chain_id), None)
            if not chain:
                raise ValueError(f"Chain {chain_id!r} not found")
            chain["retired"] = False
            self._save()
            return self._compute_status(chain)

    def delete_chain(self, chain_id: str):
        with self._lock:
            self._data["chains"] = [
                c for c in self._data.get("chains", []) if c["id"] != chain_id
            ]
            self._save()

    def log_wax_event(self, chain_id: str, date: str = None, note: str = "") -> dict:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        date = date or today
        # Logging for today uses the precise current time; a back-dated wax
        # assumes the end of that day (waxing follows the day's riding).
        ts = now.isoformat(timespec="seconds") if date == today else date + "T23:59:59"
        with self._lock:
            chain = next(
                (c for c in self._data.get("chains", []) if c["id"] == chain_id), None
            )
            if not chain:
                raise ValueError(f"Chain {chain_id!r} not found")
            chain.setdefault("wax_events", []).append({"date": date, "note": note, "ts": ts})
            self._save()
            return self._compute_status(chain)

    def set_last_wax(self, chain_id: str, ts: str) -> dict:
        """Correct the datetime of the most recent wax event (for when the
        actual wax time differs from when it was logged). `ts` is an ISO
        datetime string (e.g. from a <input type="datetime-local">)."""
        with self._lock:
            chain = next(
                (c for c in self._data.get("chains", []) if c["id"] == chain_id), None
            )
            if not chain:
                raise ValueError(f"Chain {chain_id!r} not found")
            wax_events = chain.setdefault("wax_events", [])
            date = ts[:10]
            if wax_events:
                latest = max(wax_events, key=self._wax_ts)
                latest["date"] = date
                latest["ts"] = ts
            else:
                wax_events.append({"date": date, "note": "", "ts": ts})
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
        started_at: str = None,
    ) -> dict:
        duration_hours = round((duration_seconds or 0) / 3600, 4)
        hours_consumed = round(duration_hours * multiplier, 4)
        # Keep the ride's precise local start time so same-day re-wax ordering
        # is correct. Fall back to noon when only a date is known.
        ts = (started_at or (date + "T12:00:00")) if date else started_at
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
                "ts": ts,
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

    # ── Sealant tracking (calendar-based, per bike section) ──────────────────

    def get_all_sealants(self) -> list[dict]:
        with self._lock:
            return [self._compute_sealant_status(s) for s in self._data.get("sealants", [])]

    def upsert_sealant(self, data: dict) -> dict:
        """Create or update a sealant entry. data must include 'id'."""
        with self._lock:
            sealants = self._data.setdefault("sealants", [])
            existing = next((s for s in sealants if s["id"] == data["id"]), None)
            if existing:
                for k, v in data.items():
                    if v is not None:
                        existing[k] = v
                target = existing
            else:
                target = {
                    "id": data["id"],
                    "name": data.get("name", data["id"]),
                    "bike_type": data.get("bike_type", "road"),
                    "interval_days": data.get("interval_days", 90),
                    "events": [],
                }
                sealants.append(target)
            self._save()
            return self._compute_sealant_status(target)

    def delete_sealant(self, sealant_id: str):
        with self._lock:
            self._data["sealants"] = [
                s for s in self._data.get("sealants", []) if s["id"] != sealant_id
            ]
            self._save()

    def log_sealant_checkin(self, sealant_id: str, date: str = None, note: str = "") -> dict:
        date = date or datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            sealant = next(
                (s for s in self._data.get("sealants", []) if s["id"] == sealant_id), None
            )
            if not sealant:
                raise ValueError(f"Sealant {sealant_id!r} not found")
            sealant.setdefault("events", []).append({"date": date, "note": note})
            self._save()
            return self._compute_sealant_status(sealant)

    def set_last_checkin(self, sealant_id: str, date: str) -> dict:
        """Correct the date of the most recent sealant check-in (for when it
        wasn't logged the same day). Appends a check-in if none exist yet."""
        with self._lock:
            sealant = next(
                (s for s in self._data.get("sealants", []) if s["id"] == sealant_id), None
            )
            if not sealant:
                raise ValueError(f"Sealant {sealant_id!r} not found")
            events = sealant.setdefault("events", [])
            if events:
                latest = max(events, key=lambda e: e["date"])
                latest["date"] = date
            else:
                events.append({"date": date, "note": ""})
            self._save()
            return self._compute_sealant_status(sealant)

    # ─────────────────────────────────────────────────────────────────────────

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
