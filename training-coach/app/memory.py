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
MAX_CHARS = {"planning": 5000, "review": 800, "recap": 5000, "wellness": 600}

# Execution record schema. Bumping it makes needs_execution re-measure every stored
# session through the existing drip — the raw interval payloads are not kept, so a
# richer record can only be rebuilt by fetching again.
EXEC_VERSION = 2
# Beyond this many reps the per-rep list is summarised: a 10x1min VO2 session would
# otherwise crowd a week of other sessions out of the block when _fit starts trimming.
MAX_RENDERED_REPS = 6

# Wellness fields kept from the 47 Intervals.icu returns. Names verified against
# components.schemas.Wellness in the live spec, not guessed: the repo only ever
# evidenced sleepScore and sleepSecs, and silently missing the HRV field while
# removing the live fetch would lose HRV altogether.
# There is no form/TSB field upstream — it is ctl - atl.
_WELLNESS_FIELDS = ("hrv", "restingHR", "sleepScore", "sleepSecs", "ctl", "atl")

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
athlete as if it were fresh analysis.
Lines headed "Interval execution" and session tags like "ex 3/3 @97% fade -6%" are measured by this
add-on from the device's own intervals against the workout it wrote — they are the record of interval
execution; do not call get_activity_intervals to re-derive a session memory already states. A session
with NO such tag was unstructured, a group ride, or could not be measured — never read a missing tag
as a missed session."""


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


# ── Interval execution: was the prescribed workout actually done? ──
# TSS and compliance cannot answer this. compliance is Intervals.icu's load/time
# match score and this codebase only ever asks `> 0`; a 5x4 session with three
# reps done and tempo afterwards scores the same TSS as one executed properly.
#
# The planned structure is free text, but it is text THIS add-on wrote, in the
# format the planning prompt mandates. Its "Press lap" convention exists so device
# lap markers land on the prescribed step boundaries — that alignment is what makes
# rep-for-rep comparison possible, and its absence is what makes it unreliable.
#
# Known limitation: the first computation wins. If the planned description is
# edited afterwards the stored verdict is not revisited; exec.event_id records
# which plan was used so it stays traceable.

_STEP_RE = re.compile(r"^\s*[-*\u2022]\s*(.+?)\s*$")
_REPEAT_TAIL_RE = re.compile(r"(?:^|\s)(\d{1,2})\s*x\s*$", re.I)   # "Main set 3x"
_REPEAT_ANY_RE = re.compile(r"(?<![\w])(\d{1,2})\s*x", re.I)        # "2x20" -> refuse
_RECOVERY_RE = re.compile(
    r"\b(press\s*lap|recover(y|ies)?|rest|easy|spin|float|"
    r"cool\s*down|cooldown|warm\s*up|warmup)\b", re.I)
_RAMP_RE = re.compile(r"\bramp\b", re.I)

# Durations. The lookbehind rejects a number glued to a previous token, which is
# what stops "90 rpm" reading as 90 minutes and "20-30m" as 30 minutes.
_DUR_PATTERNS = (
    (re.compile(r"(?<![\w%.\-])(\d+)h(\d+)m(\d+)s(?![\w%])", re.I), (3600, 60, 1)),
    (re.compile(r"(?<![\w%.\-])(\d+)h(\d+)m?(?![\w%])", re.I), (3600, 60)),
    (re.compile(r"(?<![\w%.\-])(\d+)h(?![\w%])", re.I), (3600,)),
    (re.compile(r"(?<![\w%.\-])(\d+)m(\d+)s?(?![\w%])", re.I), (60, 1)),       # 1m30
    (re.compile(r"(?<![\w%.\-])(\d+)(?:min|mins|m)(?![\w%])", re.I), (60,)),
    (re.compile(r"(?<![\w%.\-])(\d+)(?:sec|secs|s)(?![\w%])", re.I), (1,)),
    (re.compile(r"(?<![\w%.\-])(\d+):([0-5]\d)(?![\w%:])"), (60, 1)),
)
# The qualifier after % is load-bearing: bare % and "% FTP" are power, "% HR" and
# "% LTHR" are not. A 60% HR step read as 60% FTP becomes a missed sweetspot rep.
_PCT_RE = re.compile(
    r"(?<![\w.\-])(\d{1,3})(?:\s*-\s*(\d{1,3}))?\s*%\s*"
    r"(lthr|hrr|hr|ftp|mhr|max\s*hr|pace)?", re.I)
_W_RE = re.compile(r"(?<![\w.\-])(\d{2,4})(?:\s*-\s*(\d{2,4}))?\s*w(?:atts)?(?![\w])", re.I)

WORK_FLOOR_PCT = 72       # a step inside a repeat below this is recovery, whatever it is called
SOLO_FLOOR_PCT = 85       # a step outside a repeat must be this hard to count as a one-rep set
SOLO_MIN_SECONDS = 300
TARGET_TOL_PCT = 2        # ~5 W at 250 FTP — inside a power meter's own accuracy
WORK_TIME_TOL = 0.12      # total work time agreeing this closely means laps, not missed reps
SHORT_REP_FLOOR = 10      # seconds of lap-press slop forgiven on any rep length


def _duration_seconds(text: str):
    """(seconds, offset) of the first duration token, or (None, None)."""
    best = None
    for pattern, mult in _DUR_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        key = (m.start(), -(m.end() - m.start()))   # earliest, then longest
        if best is None or key < best[0]:
            best = (key, sum(k * int(g) for k, g in zip(mult, m.groups()) if g), m.start())
    return (None, None) if best is None else (best[1], best[2])


def _power_target(text: str):
    """The POWER band of a step, or None for heart-rate, pace and cadence targets."""
    ramp = bool(_RAMP_RE.search(text))
    for m in _PCT_RE.finditer(text):
        qualifier = (m.group(3) or "").lower().replace(" ", "")
        if qualifier in ("", "ftp"):
            lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
            return {"unit": "pct", "low": min(lo, hi), "high": max(lo, hi), "ramp": ramp}
    m = _W_RE.search(text)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        return {"unit": "w", "low": min(lo, hi), "high": max(lo, hi), "ramp": ramp}
    return None


def parse_planned_workout(description):
    """Normalise a planned workout written in this add-on's format, or None.

    Returning None rather than a guess is the whole point: a wrong parse becomes a
    confident "you missed two reps" in a coach note months later, which is worse
    than having no feature at all.

    An empty `sets` list is a THIRD outcome, distinct from None — the format was
    recognised but holds nothing rep-shaped to judge (an endurance ride).
    """
    if not isinstance(description, str) or not description.strip():
        return None
    blocks, current, pending_reps = [], None, 1
    steps_total = unparsed = 0

    for raw in description.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _STEP_RE.match(line)
        if match:
            body = match.group(1)
            steps_total += 1
            seconds, offset = _duration_seconds(body)
            if seconds is None:
                unparsed += 1          # a bullet with no duration is not a step
                continue
            if current is None:
                current = {"reps": pending_reps, "steps": []}
                blocks.append(current)
            current["steps"].append({
                "label": body[:offset].strip(" -:"),
                "seconds": seconds,
                "target": _power_target(body[offset:]) or _power_target(body),
            })
            continue

        current = None                 # a header closes the open block
        tail = _REPEAT_TAIL_RE.search(line)
        if tail:
            pending_reps = int(tail.group(1))
        elif _REPEAT_ANY_RE.search(line):
            return None                # "2x20", "3 x 10m": the rep count is ambiguous
        else:
            pending_reps = 1

    if steps_total == 0 or unparsed * 4 > steps_total:
        return None                    # prose, or a markdown bullet list

    sets, warnings = [], []
    for block in blocks:
        work, ambiguous = [], False
        for step in block["steps"]:
            target = step["target"]
            if _RECOVERY_RE.search(step["label"]):
                if target and target["unit"] == "pct" and target["high"] >= 88:
                    ambiguous = True   # "Recovery" at 90% is a contradiction we won't resolve
                continue
            if target and target["unit"] == "pct" and target["high"] < WORK_FLOOR_PCT:
                continue               # recovery by intensity, whatever the label says
            work.append(step)
        if not work:
            continue
        if block["reps"] < 2:
            # Not a repeat block. Admit a one-rep set only when it is unmistakably
            # an effort, or every warmup becomes a "missed rep".
            target = work[0]["target"]
            if (len(work) > 1 or not target or target["unit"] != "pct"
                    or target["low"] < SOLO_FLOOR_PCT
                    or work[0]["seconds"] < SOLO_MIN_SECONDS):
                continue
        if len(work) > 1:
            ambiguous = True           # over-unders: one rep is not one effort
            warnings.append("ambiguous_set")
        longest = max(work, key=lambda s: s["seconds"])
        sets.append({
            "reps": block["reps"],
            "label": (work[0]["label"] or "").strip() or None,
            "work_seconds": sum(s["seconds"] for s in work),
            "steps": len(work),
            "band": longest["target"],
            "ambiguous": ambiguous,
        })
    return {"format": "addon", "sets": sets,
            "reps_planned": sum(s["reps"] for s in sets),
            "warnings": sorted(set(warnings))}


def _work_intervals(payload):
    """Candidate work efforts, in time order. Falls back to every interval when the
    API labelled none, and sorts by start_index when present — get_activity_intervals
    drops the time fields, so fade would otherwise depend on API ordering."""
    intervals = (payload or {}).get("intervals") or []
    work = [i for i in intervals if (i.get("type") or "").upper() == "WORK"] or list(intervals)
    if all(i.get("start_index") is not None for i in work):
        work = sorted(work, key=lambda i: i["start_index"])
    return work


def _rep_pct(interval, ftp):
    """A rep's intensity as % FTP. intensity_pct is preferred because it carries the
    FTP in force AT THE TIME — decisive when judging a six-month backfill against
    today's threshold."""
    pct = interval.get("intensity_pct")
    if pct:
        value = _num(pct, 1)
        if value:
            return value * 100 if value <= 3 else value
    power = interval.get("avg_power") or interval.get("normalized_power")
    if power and ftp:
        return round(power / ftp * 100, 1)
    return None


def compare_execution(plan, payload, ftp=None, trainer=False):
    """Measure how a session was executed against its plan.

    Returns a dict whose `basis` says how far the measurement can be trusted:
      plan        — reps compared against a parsed plan
      unclear     — a plan existed but the detected efforts did not line up with it
      actual_only — no usable plan; the actual side only, no claim about a plan
      none        — nothing to judge

    The `unclear` path is the point of the whole function. When the rep COUNT
    disagrees but the total work TIME agrees, the same work was done in a different
    number of pieces — a lap pressed late, two reps merged, one split — and no rep
    shortfall is reported at all. A wrong "you missed two reps" is worse than silence.
    """
    out = {"v": EXEC_VERSION, "erg": bool(trainer)}
    efforts = _work_intervals(payload)
    if not efforts:
        return {**out, "basis": "none", "reason": "no_intervals"}

    sets = (plan or {}).get("sets") or []
    if not sets:
        durations = [_num(i.get("duration_seconds")) or 0 for i in efforts]
        pcts = [p for p in (_rep_pct(i, ftp) for i in efforts) if p]
        result = {**out, "basis": "actual_only", "efforts": len(efforts),
                  "work_min": round(sum(durations) / 60),
                  "median_min": round((_median(durations) or 0) / 60, 1),
                  "median_pct": round(_median(pcts)) if pcts else None}
        # Fade only when the efforts actually look like a set.
        if len(efforts) >= 3 and durations and _median(durations):
            spread = max(durations) - min(durations)
            if spread / (_median(durations) or 1) < 0.5:
                result["fade_pct"] = _fade(efforts)
        return result

    reps_planned = plan["reps_planned"]
    per_rep = _median([s["work_seconds"] for s in sets]) or 0
    pool = [i for i in efforts
            if per_rep and 0.40 * per_rep <= (_num(i.get("duration_seconds")) or 0) <= 2.50 * per_rep]
    if not pool:
        return {**out, "basis": "unclear", "reason": "no_matching_efforts",
                "reps_planned": reps_planned, "efforts": len(efforts)}

    # Prefer Intervals.icu's own repeat-set grouping: it decided these are one set.
    match = "filtered"
    groups = (payload or {}).get("groups") or []
    for group in groups:
        if group.get("count") == reps_planned:
            members = [i for i in pool if i.get("group_id") == group.get("group_id")]
            if len(members) == reps_planned:
                pool, match = members, "group"
                break
    else:
        if len(pool) == reps_planned:
            match = "count"

    planned_work = sum(s["reps"] * s["work_seconds"] for s in sets)
    actual_work = sum(_num(i.get("duration_seconds")) or 0 for i in pool)
    if len(pool) != reps_planned and planned_work:
        if abs(actual_work - planned_work) / planned_work <= WORK_TIME_TOL:
            # Same work, different number of pieces. Say nothing about reps.
            return {**out, "basis": "unclear", "reason": "split_or_merged",
                    "reps_planned": reps_planned, "efforts": len(pool),
                    "work_min": round(actual_work / 60)}
    if any(s["ambiguous"] for s in sets):
        return {**out, "basis": "unclear", "reason": "ambiguous_set",
                "reps_planned": reps_planned, "efforts": len(pool),
                "work_min": round(actual_work / 60)}

    band = sets[0]["band"] if len(sets) == 1 else None
    hits = unknown = 0
    for interval in pool:
        if not band or band.get("ramp"):
            continue
        if band["unit"] == "pct":
            value = _rep_pct(interval, ftp)
        else:
            value = _num(interval.get("avg_power"))
        if value is None:
            unknown += 1
            continue
        if band["low"] - TARGET_TOL_PCT <= value <= band["high"] + TARGET_TOL_PCT:
            hits += 1

    short = 0
    for interval, expected in zip(pool, _expected_durations(sets)):
        actual = _num(interval.get("duration_seconds")) or 0
        if actual < expected - max(SHORT_REP_FLOOR, 0.10 * expected):
            short += 1

    result = {**out, "basis": "plan", "match": match,
              "reps_planned": reps_planned, "reps_done": len(pool),
              "short_reps": short, "work_min": round(actual_work / 60),
              "label": sets[0]["label"]}
    prescribed = plan_text(plan)
    if prescribed:
        result["plan_text"] = prescribed
    values, unit = _rep_values(pool, ftp)
    if values:
        result["reps"] = values
        result["rep_unit"] = unit
    if band and not band.get("ramp") and unknown * 3 <= len(pool):
        result["target_hit"] = hits
        midpoint = (band["low"] + band["high"]) / 2
        values = [v for v in ((_rep_pct(i, ftp) if band["unit"] == "pct"
                               else _num(i.get("avg_power"))) for i in pool) if v]
        if values and midpoint:
            result["target_rel"] = round((_median(values) or 0) / midpoint * 100)
    fade = _fade(pool)
    if fade is not None:
        result["fade_pct"] = fade
    return result


def _expected_durations(sets):
    """Prescribed duration per rep, flattened in order."""
    out = []
    for item in sets:
        out.extend([item["work_seconds"]] * item["reps"])
    return out


def _fade(efforts):
    """First rep versus last, as a percentage. Negative means faded.

    Within one set only and only from three reps up: across sets it would compare a
    VO2 rep to a threshold rep and call the athlete inconsistent.
    """
    powers = [_num(i.get("avg_power")) for i in efforts]
    if len(powers) < 3 or not all(powers) or not powers[0]:
        return None
    return round((powers[-1] / powers[0] - 1) * 100, 1)


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


def execution_sentence(record) -> str:
    """Plain-language execution facts for the auto-review's user message, or "".

    When the measurement could not be trusted this says so outright, which is what
    stops the model inventing a verdict the numbers do not support.
    """
    if not record:
        return ""
    basis = record.get("basis")
    if basis == "unclear":
        return ("The detected intervals did not line up with the planned structure "
                "(likely lap presses, not missed work), so rep counts are not "
                "established for this ride — do not state one.")
    if basis != "plan":
        return ""
    bits = []
    if record.get("plan_text"):
        bits.append(f"prescribed {record['plan_text']}")
    bits.append(f"{record.get('reps_done')} of {record.get('reps_planned')} reps")
    if record.get("target_hit") is not None:
        bits.append(f"{record['target_hit']} on target"
                    + (f" (median {record['target_rel']}% of the band)"
                       if record.get("target_rel") else ""))
    if record.get("fade_pct") is not None:
        bits.append(f"fade {record['fade_pct']:+g}% first to last")
    if record.get("short_reps"):
        bits.append(f"{record['short_reps']} rep(s) cut short")
    sentence = ("Measured execution for this ride, computed by the add-on from the device "
                "intervals against the workout it planned: " + ", ".join(bits) + ".")
    if record.get("erg"):
        sentence += (" Ridden on a trainer, so hitting the target is the trainer's doing — "
                     "judge fade and completion, not target accuracy.")
    return sentence + " Treat these as given; call get_activity_intervals only for detail beyond them."


def plan_text(plan) -> str:
    """The prescribed structure, compactly: "3x15m@88-92%", or two sets joined by +.

    The comparison already parses this and then threw it away, so the review could
    say how many reps landed but never what was asked for.
    """
    bits = []
    for item in (plan or {}).get("sets") or []:
        minutes = round((item.get("work_seconds") or 0) / 60)
        if not minutes:
            continue
        band = item.get("band") or {}
        if band.get("unit") == "pct":
            target = f"@{band['low']}-{band['high']}%" if band["low"] != band["high"] else f"@{band['low']}%"
        elif band.get("unit") == "w":
            target = f"@{band['low']}-{band['high']}w" if band["low"] != band["high"] else f"@{band['low']}w"
        else:
            target = ""
        bits.append(f"{item.get('reps', 1)}x{minutes}m{target}")
    return " + ".join(bits)


def _rep_values(efforts, ftp):
    """Per-rep average watts in order, or intensity percentages when there is no
    power. Returns (values, unit) — ("", "") when neither exists, so a run or a
    dropout never renders "None"."""
    watts = [_num(i.get("avg_power")) for i in efforts]
    if all(w for w in watts):
        return watts, "W"
    pcts = [_rep_pct(i, ftp) for i in efforts]
    if all(p for p in pcts):
        return [round(p) for p in pcts], "%"
    return [], ""


def _exec_tag(record) -> str:
    """Short execution tag for a session line, or "" when nothing is established.

    Only `basis: plan` is tagged. An actual_only or unclear record renders nothing
    rather than half a claim — and the usage instructions tell the model that a
    missing tag means unmeasured, never a missed session.
    """
    if not record or record.get("basis") != "plan":
        return ""
    bits = []
    if record.get("plan_text"):
        bits.append(record["plan_text"])
    bits.append(f"{record.get('reps_done')}/{record.get('reps_planned')}")
    if record.get("target_hit") is not None:
        # The count the review prompt always claimed was here and never was.
        bits.append(f"{record['target_hit']}\u2713")
    values = record.get("reps") or []
    if values:
        shown = values[:MAX_RENDERED_REPS]
        rendered = "/".join(f"{v:g}" for v in shown) + record.get("rep_unit", "")
        if len(values) > MAX_RENDERED_REPS:
            rendered += f"+{len(values) - MAX_RENDERED_REPS} more"
        bits.append(rendered)
    elif record.get("target_rel") is not None:
        bits.append(f"@{record['target_rel']}%")
    if record.get("fade_pct") is not None:
        bits.append(f"fade {record['fade_pct']:+g}%")
    if record.get("short_reps"):
        bits.append(f"{record['short_reps']} short")
    return " ex " + " ".join(bits)


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
            "wellness": {},
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
        for key in ("sessions", "weeks", "planned", "wellness"):
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

    def set_execution(self, activity_id, data: dict) -> bool:
        """Store a measured execution record. Same change gate as set_note, because
        this runs inside the 120-second loop."""
        if not data:
            return False
        sid = str(activity_id)
        with self._lock:
            session = self._data["sessions"].setdefault(
                sid, {"id": sid, "source": "live", "deleted": False})
            current = session.get("exec") or {}
            if {k: v for k, v in current.items() if k != "ts"} == \
               {k: v for k, v in data.items() if k != "ts"}:
                return False
            session["exec"] = {**data, "ts": datetime.now().isoformat(timespec="seconds")}
            self._save()
            return True

    def execution_for(self, activity_id):
        with self._lock:
            return (self._data["sessions"].get(str(activity_id)) or {}).get("exec")

    def needs_execution(self, activity_id, tss=None) -> bool:
        """Whether this session still wants an interval fetch.

        The loop re-sees the same fortnight every 120 seconds, so this must be a
        dict lookup that says no almost always. It says yes again when the TSS
        moved, which is how a re-uploaded or re-cut activity self-heals.
        """
        with self._lock:
            session = self._data["sessions"].get(str(activity_id))
            if not session:
                return True
            record = session.get("exec")
            if not record:
                return True
            if (record.get("v") or 1) < EXEC_VERSION:
                return True   # richer record; only a refetch can rebuild it
            if record.get("basis") == "pending":
                return (record.get("attempts") or 0) < 3
            stored_tss = record.get("tss")
            current = _num(tss)
            return bool(stored_tss is not None and current is not None
                        and abs(stored_tss - current) > 2)

    def mark_execution_attempt(self, activity_id) -> None:
        """Record a failed fetch. After three tries it settles as unmeasurable
        rather than retrying every two minutes forever."""
        sid = str(activity_id)
        with self._lock:
            session = self._data["sessions"].setdefault(
                sid, {"id": sid, "source": "live", "deleted": False})
            record = session.get("exec") or {"v": EXEC_VERSION, "basis": "pending", "attempts": 0}
            attempts = (record.get("attempts") or 0) + 1
            # Stamped with the CURRENT version: a hardcoded 1 would read as stale
            # after every schema bump, so a permanently unfetchable session would be
            # retried three times per poll, forever.
            session["exec"] = ({"v": EXEC_VERSION, "basis": "none", "reason": "fetch_failed"}
                               if attempts >= 3 else
                               {"v": EXEC_VERSION, "basis": "pending", "attempts": attempts})
            self._save()

    def sessions_needing_execution(self, limit: int = 10):
        """Ids worth fetching, newest first — the loop's own 14-day window cannot
        see the backfilled months, so the drip needs its own queue."""
        with self._lock:
            pending = [s for s in self._live_sessions()
                       if not s.get("group_ride") and (
                           not s.get("exec")
                           or (s.get("exec", {}).get("basis") == "pending"
                               and (s["exec"].get("attempts") or 0) < 3))]
        return [s["id"] for s in reversed(pending)][:limit]

    def record_plans(self, events) -> bool:
        """Store the parsed plan for each planned workout.

        Separate from record_planned because that one judges done/skipped and so
        deliberately ignores anything inside a two-day grace window — which would
        mean the plan for a ride that just uploaded is never available.
        """
        changed = False
        with self._lock:
            for event in events or []:
                rec = planned_record_from_event(event)
                if rec is None:
                    continue
                plan = parse_planned_workout(event.get("description"))
                entry = dict(self._data["planned"].get(rec["id"]) or {})
                merged = {**entry, **rec}
                if plan is not None:
                    merged["plan"] = plan
                if merged != entry:
                    self._data["planned"][rec["id"]] = merged
                    changed = True
            if changed:
                self._save()
        return changed

    def record_wellness(self, rows) -> int:
        """Store the daily wellness rows the add-on fetches for itself.

        Keeps six fields out of forty-seven and computes form, so the recap can
        read a trend instead of the model being handed raw upstream JSON — which
        measured ~4,100 characters for one week, re-sent on every loop iteration.
        """
        changed = 0
        with self._lock:
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                day = (row.get("id") or "").strip()[:10]
                if not day:
                    continue
                entry = {}
                for field in _WELLNESS_FIELDS:
                    value = _num(row.get(field), 1)
                    if value is not None:
                        entry[field] = value
                if not entry:
                    continue   # a day with nothing recorded is not worth a row
                if entry.get("ctl") is not None and entry.get("atl") is not None:
                    entry["form"] = round(entry["ctl"] - entry["atl"], 1)
                if self._data["wellness"].get(day) == entry:
                    continue
                self._data["wellness"][day] = entry
                changed += 1
            if changed:
                self._save()
        return changed

    @property
    def has_wellness(self) -> bool:
        with self._lock:
            return bool(self._data.get("wellness"))

    def plan_for_event(self, event_id):
        if not event_id:
            return None
        with self._lock:
            return (self._data["planned"].get(str(event_id)) or {}).get("plan")

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

    @staticmethod
    def _exec_records(sessions):
        return [s["exec"] for s in sessions
                if (s.get("exec") or {}).get("basis") == "plan"]

    def _week_rows(self, mondays, group_ride_is_hard, with_prose=0, max_hours=None,
                   max_tss=None, with_exec=False):
        rows = []
        for i, monday in enumerate(mondays):
            week = self._data["weeks"].get(monday) or {}
            sessions = [s for s in self._live_sessions() if self._monday_for(s.get("date")) == monday]
            hard = sum(1 for s in sessions if is_hard_session(s, group_ride_is_hard))
            cap = ""
            if i == 0 and max_hours and max_tss:
                cap = f" (cap {max_hours} h / {max_tss} TSS)"
            if not sessions:
                # Prose with no rides behind it — a week off sick, or one that
                # predates coverage. Render the note, never "– / – · 0 sessions".
                if with_prose and week.get("summary"):
                    rows.append(f"Wk {monday[5:]}  no sessions recorded")
                    rows.append(f'    "{week["summary"]}"')
                continue
            n = week.get("sessions", 0)
            bits = [f"Wk {monday[5:]}", f"{_fmt(week.get('hours'), ' h', 1)} / {_fmt(week.get('tss'), ' TSS')}{cap}",
                    f"{n} session{'' if n == 1 else 's'}, {hard} hard"]
            if week.get("group_rides"):
                bits.append(f"{week['group_rides']} group")
            if week.get("skipped"):
                bits.append(f"{week['skipped']} skipped")
            if with_exec:
                records = self._exec_records(sessions)
                done = sum(r.get("reps_done") or 0 for r in records)
                planned = sum(r.get("reps_planned") or 0 for r in records)
                if planned:
                    bits.append(f"reps {done}/{planned}")
            bits.append(f"RPE {_fmt(week.get('avg_rpe'))} feel {_fmt(week.get('avg_feel'))}")
            rows.append("  ".join([bits[0], " · ".join(bits[1:])]))
            if i < with_prose and week.get("summary"):
                rows.append(f'    "{week["summary"]}"')
        return rows

    def _detail_block(self, sessions, group_ride_is_hard, weeks=12, session_weeks=8):
        """Weekly rollups, then the recent weeks session by session with their
        interval execution, then monthly totals and the wellness trend.

        Sized against measurement: ~3,600 characters for 12 weeks and 33 sessions,
        which replaces ~16,600 characters of raw wellness JSON across the loop.
        """
        mondays = sorted(self._data["weeks"])[-weeks:][::-1]
        lines = self._week_rows(mondays, group_ride_is_hard, with_prose=3, with_exec=True)

        tss_by_week = [self._data["weeks"].get(m, {}).get("tss") for m in mondays[::-1]]
        blocks = [tss_by_week[i:i + 4] for i in range(0, len(tss_by_week), 4)]
        averages = [_avg([t for t in b if t]) for b in blocks]
        averages = [a for a in averages if a is not None]
        if len(averages) > 1:
            lines.append("Block averages (oldest→newest, 4-wk): "
                         + " · ".join(f"{_fmt(a)} TSS/wk" for a in averages))

        detail_weeks = set(mondays[:session_weeks])
        recent = [s for s in sessions if self._monday_for(s.get("date")) in detail_weeks]
        if recent:
            lines.append(f"\nSession by session, last {session_weeks} weeks:")
            for session in recent:
                lines.append(
                    f"  {session.get('date', '')[5:]} "
                    f"{(session.get('name') or session.get('type') or 'session')[:22]:22} "
                    f"{_fmt(session.get('duration_min'), 'm')}/{_fmt(session.get('tss'))} "
                    f"rpe{_fmt(session.get('rpe'))} feel{_fmt(session.get('feel'))}"
                    + _exec_tag(session.get("exec"))
                    + (f" — {session['note']}" if session.get("note") else ""))

        months = {}
        for session in sessions:
            months.setdefault((session.get("date") or "")[:7], []).append(session)
        months.pop("", None)
        if len(months) > 1:
            lines.append("\nBy month: " + " · ".join(
                f"{month[5:]} {round(sum(x.get('tss') or 0 for x in rows))} TSS/{len(rows)} sess"
                for month, rows in sorted(months.items())))

        trend = self._wellness_rows(mondays[:session_weeks])
        if trend:
            lines.append("Wellness by week (avg): " + " · ".join(trend))
        return lines

    def _wellness_rows(self, mondays):
        """Weekly HRV / resting HR / sleep / form averages from the stored rows."""
        out = []
        for monday in mondays:
            try:
                start = date.fromisoformat(monday)
            except (TypeError, ValueError):
                continue
            days = {(start + timedelta(days=i)).isoformat() for i in range(7)}
            rows = [r for d, r in self._data.get("wellness", {}).items() if d in days]
            if not rows:
                continue
            bits = []
            for field, label in (("hrv", "hrv"), ("restingHR", "rhr"),
                                 ("sleepScore", "sleep"), ("form", "form")):
                value = _avg([r.get(field) for r in rows], 0)
                if value is not None:
                    bits.append(f"{label} {_fmt(value)}")
            if bits:
                out.append(f"{monday[5:]} " + " ".join(bits))
        return out

    def _render_planning(self, sessions, group_ride_is_hard, max_hours, max_tss):
        mondays = sorted(self._data["weeks"])[-12:][::-1]
        lines = [self._header(" Authoritative for closed weeks; do not re-fetch them.")]
        lines += self._detail_block(sessions, group_ride_is_hard)
        recent = [self._data["weeks"].get(m, {}).get("tss") for m in mondays[::-1]]
        recent = [t for t in recent if t]
        if len(recent) >= 2:
            direction = "rising" if recent[-1] > recent[0] else "falling" if recent[-1] < recent[0] else "flat"
            lines.append(f"Trend: weekly TSS {recent[0]} → {recent[-1]} {direction} over {len(recent)} weeks")
        skipped = sum(1 for p in self._data["planned"].values() if p.get("status") == "skipped")
        planned = sum(1 for p in self._data["planned"].values() if p.get("status"))
        if planned:
            lines.append(f"Planned sessions recorded: {planned}, of which {skipped} skipped")
        window = {m for m in mondays}
        in_window = [s for s in sessions if self._monday_for(s.get("date")) in window]
        execution = self._execution_summary(in_window, len(mondays))
        if execution:
            lines.append(execution)
        lines.append("The last 48 h may be incomplete — call get_recent_activities(days_back=3) "
                     "if today's load matters.")
        return "\n".join(lines)

    def _execution_summary(self, sessions, weeks):
        """The pattern the planner needs: are reps being completed, are targets being
        hit, is fade getting worse. This is what TSS alone could never show."""
        records = self._exec_records(sessions)
        if not records:
            return ""
        done = sum(r.get("reps_done") or 0 for r in records)
        planned = sum(r.get("reps_planned") or 0 for r in records)
        if not planned:
            return ""
        bits = [f"Interval execution ({weeks} wks, {len(records)} structured): reps {done}/{planned}"]
        hits = [r for r in records if r.get("target_hit") is not None]
        if hits:
            on_target = sum(r["target_hit"] for r in hits)
            attempted = sum(r.get("reps_done") or 0 for r in hits)
            rel = _median([r.get("target_rel") for r in hits])
            bits.append(f"on target {on_target}/{attempted}"
                        + (f" (median {_fmt(rel)}% of band)" if rel else ""))
        fades = [r["fade_pct"] for r in records if r.get("fade_pct") is not None]
        if fades:
            trend = ""
            if len(fades) >= 6:
                half = len(fades) // 2
                older, newer = _avg(fades[:half]), _avg(fades[half:])
                if older is not None and newer is not None:
                    trend = (", worsening" if newer < older - 1
                             else ", improving" if newer > older + 1 else ", steady")
            bits.append(f"fade {_fmt(_avg(fades))}% first→last{trend}")
        short = sum(r.get("short_reps") or 0 for r in records)
        if short:
            bits.append(f"{short} reps cut short")
        return " · ".join(bits)

    def _render_review(self, sessions, group_ride_is_hard):
        tss = [s.get("tss") for s in sessions]
        mins = [s.get("duration_min") for s in sessions]
        decoupling = [s.get("decoupling") for s in sessions]
        normals = f"Your normals: median ride {_fmt(_median(mins))} min / {_fmt(_median(tss))} TSS"
        if _median(decoupling) is not None:
            normals += (f" · decoupling typically {_fmt(_median(decoupling), '%', 1)}"
                        f" (p10–p90 {_fmt(_pct(decoupling, 0.1), '', 1)}–"
                        f"{_fmt(_pct(decoupling, 0.9), '', 1)})")
        normals += (f" · RPE median {_fmt(_median([s.get('rpe') for s in sessions]))}"
                    f" · feel median {_fmt(_median([s.get('feel') for s in sessions]))}")
        lines = [self._header(f" {len(sessions)} sessions recorded."), normals]
        records = self._exec_records(sessions)
        if records:
            done = sum(r.get("reps_done") or 0 for r in records)
            planned = sum(r.get("reps_planned") or 0 for r in records)
            hits = [r for r in records if r.get("target_hit") is not None]
            norm = f" · intervals normally {done}/{planned} reps"
            if hits:
                on_target = sum(r["target_hit"] for r in hits)
                attempted = sum(r.get("reps_done") or 0 for r in hits)
                if attempted:
                    norm += f", {round(on_target / attempted * 100)}% on target"
            fades = [r["fade_pct"] for r in records if r.get("fade_pct") is not None]
            if fades:
                norm += f", fade {_fmt(_avg(fades))}%"
            lines[-1] += norm
        for s in sessions[-5:][::-1]:
            lines.append(f"  {s.get('date', '')[5:]} {s.get('name') or s.get('type') or 'session'} "
                         f"{_fmt(s.get('duration_min'), 'm')}/{_fmt(s.get('tss'))} "
                         f"rpe{_fmt(s.get('rpe'))} feel{_fmt(s.get('feel'))}"
                         + _exec_tag(s.get("exec"))
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
        """The recap compares this week against the block behind it, so it gets the
        same detail as planning — including the wellness trend, which replaces the
        raw get_wellness payload it used to be handed on every iteration."""
        if not self._data["weeks"]:
            return ""
        lines = [self._header(" The weeks behind the one you are recapping.")]
        lines += self._detail_block(sessions, False)
        return "\n".join(lines)
