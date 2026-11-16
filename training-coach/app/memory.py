"""Persistent training memory: what actually happened, so the coach can plan
from it instead of re-fetching months of history through the Intervals.icu API.

Why this exists: _run_response_loop re-sends the whole input list on every tool
iteration, so a 28-day activity fetch (~4.5k tokens) costs ~27k prompt tokens
across a six-iteration planning turn. A ~400-token memory block costs ~2.4k.

Everything here is stdlib only and the decisions live in pure functions, so the
tests run anywhere and do not need fastapi, openai or requests.

Known limitation: a session's date comes from the ride's local start time while
week boundaries come from the container's clock. If the container runs UTC and
the athlete rides UTC+2, a late Sunday evening ride can land in the wrong week.
Not cheaply fixable; stated rather than hidden.
"""
import json
import os
import re
import threading
from datetime import date, datetime, timedelta

MEMORY_FILE = "/data/training_memory.json"
SCHEMA_VERSION = 1

# Substituted into cached prompts at render time. Not a {}-placeholder: .format()
# has already run by then. See TrainingAgent._inject_memory.
MEMORY_SENTINEL = "<<MEMORY>>"

# Hard caps per scope. Enforced by dropping the oldest week, never by truncating
# mid-string — a half-sentence in a system prompt is worse than one fewer week.
MAX_CHARS = {"planning": 1800, "review": 800, "recap": 500, "wellness": 600}

# Fields the code owns. Anything outside this set (note, note_source, status,
# source) is model- or sweep-authored and must survive every numeric upsert.
_NUMERIC_FIELDS = (
    "date", "ts", "name", "type", "duration_min", "tss", "intensity",
    "avg_power", "avg_hr", "rpe", "feel", "coach_tick", "compliance",
    "paired_event_id", "group_ride", "decoupling", "efficiency_factor",
    "z4plus_min",
)

# Numeric keys on a week rollup. Recomputed from sessions; prose keys
# (summary, summary_ts, recap_text) are preserved across a recompute.
_WEEK_NUMERIC = ("hours", "tss", "sessions", "group_rides", "avg_rpe", "avg_feel", "skipped")

_MEMO_RE = re.compile(r"\[\[MEMO\s+(.*?)\]\]", re.S)

MEMORY_USAGE_INSTRUCTIONS = """How to use TRAINING MEMORY: it is written by this add-on from your own completed
sessions and from earlier coach notes. For any week already closed it is the record — do not call
get_recent_activities to re-derive history it already states. Its coverage window is printed in the
block; the absence of a session before coverage_from means "not recorded", never "did not train".
Still fetch live, every time — memory never holds these: today's readiness or wellness, the upcoming
calendar and races, the athlete's thresholds and coach ticks, the existing weekly note before writing
one, and any session from the last 48 hours whose detail matters (use get_recent_activities(days_back=3),
not more). When memory and a live fetch disagree, the live fetch wins and memory is wrong. Never add an
estimated TSS for a ride memory already reports as completed, and do not quote the block back to the
athlete as if it were fresh analysis."""


# ── Pure helpers ──

def monday_of(d: date) -> date:
    """Monday of d's week. Plain arithmetic, not isocalendar(), so the result is
    a real date at a year boundary rather than a (year, week) pair."""
    return d - timedelta(days=d.weekday())


def recap_target_monday(today: date) -> str:
    """The Monday of the week a Monday-morning recap reviews: the PREVIOUS week.

    The recap fires Monday >=08:00 and reviews "the past 7 days". Keying its
    summary to today's Monday misfiles every rollup note by one week — invisible
    on inspection, and it makes the planner read last week's verdict as this
    week's.
    """
    return (monday_of(today) - timedelta(days=7)).isoformat()


def _num(value, nd=None):
    """Coerce to a rounded number, or None. Rounding on the way in is what makes
    the upsert change-gate fire: re-serialised API floats differ in the last bit,
    and an ungated store rewrites itself every 120 seconds forever."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN/inf: every NaN compare is False
        return None
    return round(f, nd) if nd else int(round(f))


def _z4plus_minutes(power_zone_mins):
    """Minutes in Z4 and above — the load-bearing half of "was this hard"."""
    if not isinstance(power_zone_mins, (list, tuple)):
        return None
    total = 0.0
    for mins in power_zone_mins[3:]:
        v = _num(mins, 1)
        if v:
            total += v
    return round(total, 1)


def session_record_from_activity(activity: dict):
    """Normalise an activity into a memory record, or None if it cannot be stored.

    Rejects records with no id or no date: _simplify_activity can emit date="",
    and a dateless session cannot be placed in a week.
    """
    if not isinstance(activity, dict):
        return None
    sid = str(activity.get("id") or "").strip()
    day = (activity.get("date") or (activity.get("start_date_local") or "")[:10]).strip()
    if not sid or not day:
        return None
    paired = activity.get("paired_event_id")
    duration = _num(activity.get("duration_seconds"))
    return {
        "id": sid,
        "date": day,
        "ts": activity.get("start_date_local"),
        "name": activity.get("name"),
        "type": activity.get("type"),
        "duration_min": round(duration / 60) if duration else None,
        "tss": _num(activity.get("tss")),
        "intensity": _num(activity.get("intensity"), 3),
        "avg_power": _num(activity.get("avg_power")),
        "avg_hr": _num(activity.get("avg_hr")),
        "rpe": _num(activity.get("rpe")),
        "feel": _num(activity.get("feel")),
        "coach_tick": _num(activity.get("coach_tick")),
        "compliance": _num(activity.get("compliance")),
        # Event ids are ints upstream but JSON object keys are strings. Canonicalise
        # on both sides or paired-event matching silently never fires.
        "paired_event_id": str(paired) if paired not in (None, "") else None,
        "group_ride": bool(activity.get("group_ride")),
        "decoupling": _num(activity.get("decoupling"), 1),
        "efficiency_factor": _num(activity.get("efficiency_factor"), 2),
        "z4plus_min": _z4plus_minutes(activity.get("power_zone_mins")),
    }


def planned_record_from_event(event: dict):
    """Normalise a planned calendar event, or None if it is not a workout."""
    if not isinstance(event, dict):
        return None
    eid = str(event.get("id") or "").strip()
    day = (event.get("date") or "").strip()
    if not eid or not day:
        return None
    category = (event.get("category") or "WORKOUT").upper()
    if category != "WORKOUT":
        return None  # NOTE carries the weekly plan; RACE_* is not a skippable session
    duration = _num(event.get("duration_seconds"))
    return {
        "id": eid,
        "date": day,
        "name": event.get("name"),
        "type": event.get("type"),
        "tss": _num(event.get("tss")),
        "duration_min": round(duration / 60) if duration else None,
    }


def is_hard_session(session: dict, group_ride_is_hard: bool) -> bool:
    """Whether a session spends a hard-interval slot.

    Computed, never stored: "hard" depends on the group_ride_intensity setting,
    so a frozen count would silently disagree with the prompts the day the
    athlete changes it.
    """
    if session.get("group_ride"):
        return bool(group_ride_is_hard)
    if (session.get("intensity") or 0) >= 0.85:
        return True
    return (session.get("z4plus_min") or 0) >= 12


def extract_memo(text: str, max_chars: int = 240):
    """Pull a [[MEMO ...]] marker out of a reply. Returns (memo, text_without_marker).

    Non-greedy to the first ']]' so a note containing a single ']' survives, and
    length-capped on a word boundary because a model told "200 characters"
    writes 600 often enough to matter.
    """
    if not isinstance(text, str):
        return None, text
    match = _MEMO_RE.search(text)
    if not match:
        return None, text
    memo = " ".join(match.group(1).split())
    if len(memo) > max_chars:
        memo = memo[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    cleaned = _MEMO_RE.sub("", text).strip()
    return (memo or None), cleaned


def judge_planned(events, sessions, today: date, grace_days: int = 2, window_days: int = 10):
    """Classify past planned workouts as done / replaced / skipped.

    'replaced' is distinct on purpose: counting "rode something else that day" as
    a skip makes the planner think the week was emptier than it actually was.

    Nothing newer than today-grace_days is judged — Garmin and Strava sync land
    late, and a premature 'skipped' that later flips is worse than no verdict.
    """
    newest = today - timedelta(days=grace_days)
    oldest = today - timedelta(days=window_days)
    paired = {str(s.get("paired_event_id")) for s in sessions if s.get("paired_event_id")}
    by_date = {}
    for s in sessions:
        if s.get("deleted"):
            continue
        by_date.setdefault(s.get("date"), []).append(s)

    verdicts = []
    for event in events:
        rec = planned_record_from_event(event)
        if rec is None:
            continue
        try:
            when = date.fromisoformat(rec["date"])
        except ValueError:
            continue
        if not (oldest <= when <= newest):
            continue
        same_day = by_date.get(rec["date"], [])
        if rec["id"] in paired or any((s.get("compliance") or 0) > 0 for s in same_day):
            status = "done"
        elif any((s.get("duration_min") or 0) > 10 for s in same_day):
            status = "replaced"
        else:
            status = "skipped"
        verdicts.append((rec, status))
    return verdicts


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def _pct(values, fraction):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    idx = min(len(vals) - 1, max(0, int(round(fraction * (len(vals) - 1)))))
    return vals[idx]


def _avg(values, nd=1):
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), nd) if vals else None


def _fmt(value, suffix="", nd=None):
    if value is None:
        return "–"
    if nd is not None:
        return f"{value:.{nd}f}{suffix}"
    return f"{value:g}{suffix}"


# ── Store ──

class MemoryStore:
    """Disk-backed training memory. Mirrors ChainManager structurally, with three
    deliberate deviations: a path parameter (so it is testable), atomic writes
    (the prose cannot be re-derived from the API at any price), and stdlib only.
    """

    def __init__(self, path: str = MEMORY_FILE):
        self._path = path
        self._lock = threading.RLock()
        self._data = self._empty()
        self.read_only = False
        self._load()

    @staticmethod
    def _empty() -> dict:
        return {
            "version": SCHEMA_VERSION,
            "as_of": None,
            "coverage_from": None,
            "backfilled_through": None,
            "sessions": {},
            "weeks": {},
            "planned": {},
        }

    def _load(self):
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (json.JSONDecodeError, ValueError, IOError) as e:
            print(f"Warning: training memory unreadable ({e}); starting empty.", flush=True)
            return
        version = data.get("version") or 1
        if version > SCHEMA_VERSION:
            # Written by a newer add-on. Render it, never write it: a downgrade
            # must not destroy months of irreplaceable prose.
            self.read_only = True
            print(f"Training memory is version {version}, this add-on writes "
                  f"{SCHEMA_VERSION} — loaded read-only.", flush=True)
        merged = self._empty()
        merged.update(data)
        for key in ("sessions", "weeks", "planned"):
            if not isinstance(merged.get(key), dict):
                merged[key] = {}
        self._data = merged
        print(f"Training memory loaded: {len(merged['sessions'])} sessions, "
              f"{len(merged['weeks'])} weeks.", flush=True)

    def _save(self):
        """Atomic write. ChainManager writes in place, which is recoverable for
        wear (re-derivable from the API) but not for coach notes."""
        if self.read_only:
            return
        self._data["as_of"] = datetime.now().isoformat(timespec="seconds")
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self._path)
        except IOError as e:
            print(f"Warning: could not save training memory: {e}", flush=True)

    # ── Writes ──

    def upsert_session(self, activity: dict) -> bool:
        """Insert or refresh one session. Returns True if anything changed.

        The auto-review loop re-fetches the same 14 days every 120 seconds, so
        this runs thousands of times a day against unchanged data. The change
        gate is what keeps that a no-op instead of a disk write every two
        minutes. The _NUMERIC_FIELDS whitelist is what keeps a numeric refresh
        from wiping the model-authored note.
        """
        rec = session_record_from_activity(activity)
        if rec is None:
            return False
        with self._lock:
            current = self._data["sessions"].get(rec["id"])
            merged = dict(current or {})
            merged["id"] = rec["id"]
            for key in _NUMERIC_FIELDS:
                value = rec.get(key)
                if value is not None:
                    merged[key] = value
            merged.setdefault("source", "live")
            merged.setdefault("deleted", False)
            if current == merged:
                return False
            self._data["sessions"][rec["id"]] = merged
            self._touch_coverage(merged["date"])
            self._recompute_week(merged["date"])
            self._save()
            return True

    def set_note(self, activity_id, note: str, source: str = "auto_review") -> bool:
        note = " ".join((note or "").split())
        if not note:
            return False
        sid = str(activity_id)
        with self._lock:
            session = self._data["sessions"].setdefault(sid, {"id": sid, "source": "live", "deleted": False})
            if session.get("note") == note and session.get("note_source") == source:
                return False
            session["note"] = note
            session["note_source"] = source
            session["note_ts"] = datetime.now().isoformat(timespec="seconds")
            self._save()
            return True

    def set_week_summary(self, monday: str, summary: str, recap_text: str = None) -> bool:
        summary = " ".join((summary or "").split())
        with self._lock:
            week = self._data["weeks"].setdefault(monday, {})
            if week.get("summary") == summary and (recap_text is None or week.get("recap_text") == recap_text):
                return False
            if summary:
                week["summary"] = summary
                week["summary_ts"] = datetime.now().isoformat(timespec="seconds")
            if recap_text:
                week["recap_text"] = recap_text
            self._save()
            return True

    def record_planned(self, verdicts) -> bool:
        changed = False
        with self._lock:
            for rec, status in verdicts:
                entry = dict(self._data["planned"].get(rec["id"]) or {})
                new = dict(entry)
                new.update(rec)
                new["status"] = status
                new["judged_on"] = date.today().isoformat()
                if {k: v for k, v in entry.items() if k != "judged_on"} == \
                   {k: v for k, v in new.items() if k != "judged_on"}:
                    continue
                self._data["planned"][rec["id"]] = new
                self._recompute_week(rec["date"])
                changed = True
            if changed:
                self._save()
        return changed

    def reconcile_deleted(self, seen_ids, since: str) -> int:
        """Mark sessions on/after `since` that upstream no longer returns.

        Without this a deleted or double-uploaded ride inflates its week forever.
        Only covers the fetch window — a ride deleted three months later stays.
        """
        seen = {str(i) for i in seen_ids}
        removed = 0
        with self._lock:
            for sid, session in self._data["sessions"].items():
                if session.get("deleted") or (session.get("date") or "") < since:
                    continue
                if sid not in seen:
                    session["deleted"] = True
                    self._recompute_week(session.get("date"))
                    removed += 1
            if removed:
                self._save()
        return removed

    def _touch_coverage(self, day: str):
        current = self._data.get("coverage_from")
        if day and (not current or day < current):
            self._data["coverage_from"] = day

    def _recompute_week(self, day: str):
        """Rebuild a week's numbers from its sessions. Derived, never incremented
        — incrementing double-counts the moment the same activity is upserted
        twice, which happens thousands of times a day."""
        if not day:
            return
        try:
            monday = monday_of(date.fromisoformat(day)).isoformat()
        except ValueError:
            return
        sessions = [s for s in self._data["sessions"].values()
                    if not s.get("deleted") and self._monday_for(s.get("date")) == monday]
        week = dict(self._data["weeks"].get(monday) or {})
        if sessions:
            week["hours"] = round(sum((s.get("duration_min") or 0) for s in sessions) / 60, 1)
            week["tss"] = round(sum((s.get("tss") or 0) for s in sessions))
            week["sessions"] = len(sessions)
            week["group_rides"] = sum(1 for s in sessions if s.get("group_ride"))
            week["avg_rpe"] = _avg([s.get("rpe") for s in sessions])
            week["avg_feel"] = _avg([s.get("feel") for s in sessions])
        else:
            for key in _WEEK_NUMERIC:
                week.pop(key, None)
        skipped = sum(1 for p in self._data["planned"].values()
                      if p.get("status") == "skipped" and self._monday_for(p.get("date")) == monday)
        if skipped:
            week["skipped"] = skipped
        else:
            week.pop("skipped", None)
        if week:
            self._data["weeks"][monday] = week
        else:
            self._data["weeks"].pop(monday, None)

    @staticmethod
    def _monday_for(day):
        try:
            return monday_of(date.fromisoformat(day)).isoformat()
        except (TypeError, ValueError):
            return None

    def rebuild_weeks(self):
        """Recompute every week from sessions. Runs after a backfill, and is the
        repair path if rollups ever disagree with the sessions beneath them."""
        with self._lock:
            self._data["weeks"] = {
                m: {k: v for k, v in w.items() if k not in _WEEK_NUMERIC}
                for m, w in self._data["weeks"].items()
            }
            for day in {s.get("date") for s in self._data["sessions"].values()}:
                self._recompute_week(day)
            self._data["weeks"] = {m: w for m, w in self._data["weeks"].items() if w}
            self._save()

    # ── Backfill ──

    @property
    def backfilled(self) -> bool:
        return bool(self._data.get("backfilled_through"))

    def mark_backfilled(self, through: str):
        with self._lock:
            self._data["backfilled_through"] = through
            self._save()

    def backfill(self, activities, events=None, through: str = None) -> int:
        """One-off import. Costs zero tokens: one HTTP request parsed in Python
        that never enters a prompt. Deliberately generates no prose — writing
        notes for old weeks would mean dozens of recap-sized LLM calls for weeks
        nobody remembers."""
        count = 0
        with self._lock:
            for activity in activities or []:
                rec = session_record_from_activity(activity)
                if rec is None:
                    continue
                existing = self._data["sessions"].get(rec["id"])
                merged = dict(existing or {})
                merged["id"] = rec["id"]
                for key in _NUMERIC_FIELDS:
                    if rec.get(key) is not None:
                        merged[key] = rec[key]
                merged.setdefault("source", "backfill")
                merged.setdefault("deleted", False)
                self._data["sessions"][rec["id"]] = merged
                self._touch_coverage(merged["date"])
                count += 1
            if events:
                sessions = [s for s in self._data["sessions"].values() if not s.get("deleted")]
                for rec, status in judge_planned(events, sessions, date.today(), window_days=3650):
                    self._data["planned"][rec["id"]] = {**rec, "status": status,
                                                        "judged_on": date.today().isoformat()}
            self._data["backfilled_through"] = through or date.today().isoformat()
        self.rebuild_weeks()
        return count

    # ── Reads ──

    def stats(self) -> dict:
        with self._lock:
            return {
                "sessions": len(self._data["sessions"]),
                "weeks": len(self._data["weeks"]),
                "coverage_from": self._data.get("coverage_from"),
                "backfilled_through": self._data.get("backfilled_through"),
                "as_of": self._data.get("as_of"),
            }

    def live_sessions(self):
        """Every session that still exists upstream, oldest first."""
        with self._lock:
            return list(self._live_sessions())

    def _live_sessions(self):
        return sorted((s for s in self._data["sessions"].values()
                       if not s.get("deleted") and s.get("date")),
                      key=lambda s: (s.get("date"), s.get("ts") or ""))

    def render(self, scope: str, *, group_ride_is_hard: bool = False,
               max_hours=None, max_tss=None) -> str:
        """The prompt block for one scope, or "" when there is nothing to say.

        Returning "" rather than an empty labelled header matters: a header with
        no rows invites the model to explain that the athlete has no history.
        """
        with self._lock:
            sessions = self._live_sessions()
            if not sessions:
                return ""
            if scope == "planning":
                text = self._render_planning(sessions, group_ride_is_hard, max_hours, max_tss)
            elif scope == "review":
                text = self._render_review(sessions, group_ride_is_hard)
            elif scope == "wellness":
                text = self._render_wellness(sessions, group_ride_is_hard, max_hours, max_tss)
            elif scope == "recap":
                text = self._render_recap(sessions)
            else:
                return ""
        fitted = self._fit(text, MAX_CHARS.get(scope, 1200))
        if fitted and scope == "planning":
            # The usage rules only pay for themselves where the model would
            # otherwise re-fetch history. Prepended after _fit so the static
            # paragraph never eats the data budget.
            return f"{MEMORY_USAGE_INSTRUCTIONS}\n{fitted}"
        return fitted

    @staticmethod
    def _fit(text: str, limit: int) -> str:
        """Drop whole lines from the middle-oldest end until under budget. Never
        truncate mid-string — a half-sentence in a system prompt reads as data
        corruption to the model."""
        if len(text) <= limit:
            return text
        lines = text.split("\n")
        while len(lines) > 3 and len("\n".join(lines)) > limit:
            del lines[-2]
        out = "\n".join(lines)
        return out if len(out) <= limit else out[:limit].rsplit("\n", 1)[0]

    def _header(self, extra: str = "") -> str:
        cov = self._data.get("coverage_from") or "?"
        as_of = (self._data.get("as_of") or "")[:16].replace("T", " ")
        return (f"TRAINING MEMORY — recorded by this add-on from your own completed sessions. "
                f"Coverage {cov} → now (as of {as_of}).{extra}")

    def _week_rows(self, mondays, group_ride_is_hard, with_prose=0, max_hours=None, max_tss=None):
        rows = []
        for i, monday in enumerate(mondays):
            week = self._data["weeks"].get(monday) or {}
            sessions = [s for s in self._live_sessions() if self._monday_for(s.get("date")) == monday]
            hard = sum(1 for s in sessions if is_hard_session(s, group_ride_is_hard))
            cap = ""
            if i == 0 and max_hours and max_tss:
                cap = f" (cap {max_hours} h / {max_tss} TSS)"
            n = week.get("sessions", 0)
            bits = [f"Wk {monday[5:]}", f"{_fmt(week.get('hours'), ' h', 1)} / {_fmt(week.get('tss'), ' TSS')}{cap}",
                    f"{n} session{'' if n == 1 else 's'}, {hard} hard"]
            if week.get("group_rides"):
                bits.append(f"{week['group_rides']} group")
            if week.get("skipped"):
                bits.append(f"{week['skipped']} skipped")
            bits.append(f"RPE {_fmt(week.get('avg_rpe'))} feel {_fmt(week.get('avg_feel'))}")
            rows.append("  ".join([bits[0], " · ".join(bits[1:])]))
            if i < with_prose and week.get("summary"):
                rows.append(f'    "{week["summary"]}"')
        return rows

    def _render_planning(self, sessions, group_ride_is_hard, max_hours, max_tss):
        mondays = sorted(self._data["weeks"])[-8:][::-1]
        lines = [self._header(" Authoritative for closed weeks; do not re-fetch them.")]
        lines += self._week_rows(mondays, group_ride_is_hard, with_prose=3,
                                 max_hours=max_hours, max_tss=max_tss)
        recent = [self._data["weeks"].get(m, {}).get("tss") for m in mondays[::-1]]
        recent = [t for t in recent if t]
        if len(recent) >= 2:
            direction = "rising" if recent[-1] > recent[0] else "falling" if recent[-1] < recent[0] else "flat"
            lines.append(f"Trend: weekly TSS {recent[0]} → {recent[-1]} {direction} over {len(recent)} weeks")
        skipped = sum(1 for p in self._data["planned"].values() if p.get("status") == "skipped")
        planned = sum(1 for p in self._data["planned"].values() if p.get("status"))
        if planned:
            lines.append(f"Planned sessions recorded: {planned}, of which {skipped} skipped")
        lines.append("The last 48 h may be incomplete — call get_recent_activities(days_back=3) "
                     "if today's load matters.")
        return "\n".join(lines)

    def _render_review(self, sessions, group_ride_is_hard):
        tss = [s.get("tss") for s in sessions]
        mins = [s.get("duration_min") for s in sessions]
        lines = [self._header(f" {len(sessions)} sessions recorded."),
                 f"Your normals: median ride {_fmt(_median(mins))} min / {_fmt(_median(tss))} TSS"
                 f" · decoupling typically {_fmt(_median([s.get('decoupling') for s in sessions]), '%', 1)}"
                 f" (p10–p90 {_fmt(_pct([s.get('decoupling') for s in sessions], 0.1), '', 1)}–"
                 f"{_fmt(_pct([s.get('decoupling') for s in sessions], 0.9), '', 1)})"
                 f" · RPE median {_fmt(_median([s.get('rpe') for s in sessions]))}"
                 f" · feel median {_fmt(_median([s.get('feel') for s in sessions]))}"]
        for s in sessions[-5:][::-1]:
            lines.append(f"  {s.get('date', '')[5:]} {s.get('name') or s.get('type') or 'session'} "
                         f"{_fmt(s.get('duration_min'), 'm')}/{_fmt(s.get('tss'))} "
                         f"rpe{_fmt(s.get('rpe'))} feel{_fmt(s.get('feel'))}"
                         + (f" — {s['note']}" if s.get("note") else ""))
        return "\n".join(lines)

    def _render_wellness(self, sessions, group_ride_is_hard, max_hours, max_tss):
        today = date.today()
        window = {(today - timedelta(days=i)).isoformat() for i in range(7)}
        recent = [s for s in sessions if s.get("date") in window]
        lines = [self._header(" Last 48 h may be incomplete.")]
        by_day = {}
        for s in recent:
            by_day.setdefault(s["date"], []).append(s)
        for i in range(6, -1, -1):
            day = (today - timedelta(days=i)).isoformat()
            rides = by_day.get(day)
            if not rides:
                lines.append(f"  {day[5:]} rest")
            else:
                for s in rides:
                    lines.append(f"  {day[5:]} {s.get('type') or 'session'} "
                                 f"{_fmt(s.get('duration_min'), 'm')}/{_fmt(s.get('tss'))} "
                                 f"rpe{_fmt(s.get('rpe'))} feel{_fmt(s.get('feel'))}")
        total_tss = round(sum((s.get("tss") or 0) for s in recent))
        total_h = round(sum((s.get("duration_min") or 0) for s in recent) / 60, 1)
        hard = sum(1 for s in recent if is_hard_session(s, group_ride_is_hard))
        cap = f" (cap {max_hours} h / {max_tss} TSS)" if max_hours and max_tss else ""
        lines.append(f"7-day: {total_h} h / {total_tss} TSS{cap} · {hard} hard")
        return "\n".join(lines)

    def _render_recap(self, sessions):
        mondays = sorted(self._data["weeks"])[-4:-1][::-1]
        if not mondays:
            return ""
        lines = [self._header(" Previous weeks, for comparison.")]
        lines += self._week_rows(mondays, False, with_prose=3)
        return "\n".join(lines)
