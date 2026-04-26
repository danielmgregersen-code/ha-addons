import json
import os
import asyncio
import threading
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from agent import TrainingAgent

OPTIONS_FILE = "/data/options.json"

# /data is always persistent for HA add-ons — survives updates and restarts.
# It is only wiped on a full manual uninstall, not on version updates.
STORAGE_DIR = "/data"
HISTORY_FILE = f"{STORAGE_DIR}/chat_history.json"
NOTIFICATIONS_FILE = f"{STORAGE_DIR}/notifications.json"
SESSION_NAMES_FILE = f"{STORAGE_DIR}/session_names.json"
TOKEN_USAGE_FILE = f"{STORAGE_DIR}/token_usage.json"
MAX_HISTORY_MESSAGES = 200
MAX_NOTIFICATIONS = 100
POLL_INTERVAL_SECONDS = 120
AUTO_REVIEW_SESSION = "auto-reviews"  # Fixed session for auto-review history


def load_options() -> dict:
    if os.path.exists(OPTIONS_FILE):
        with open(OPTIONS_FILE) as f:
            return json.load(f)
    return {
        "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
        "intervals_athlete_id": os.getenv("INTERVALS_ATHLETE_ID", ""),
        "intervals_api_key": os.getenv("INTERVALS_API_KEY", ""),
        "days_back": int(os.getenv("DAYS_BACK", "14")),
        "days_ahead": int(os.getenv("DAYS_AHEAD", "21")),
        "hrv_min": int(os.getenv("HRV_MIN", "0")),
        "hrv_max": int(os.getenv("HRV_MAX", "0")),
        "rhr_min": int(os.getenv("RHR_MIN", "0")),
        "rhr_max": int(os.getenv("RHR_MAX", "0")),
        "hard_intervals_per_week": int(os.getenv("HARD_INTERVALS_PER_WEEK", "2")),
        "block_start_date": os.getenv("BLOCK_START_DATE", ""),
        "group_ride_keywords": os.getenv("GROUP_RIDE_KEYWORDS", "group,klub,klubtur"),
        "chat_model": os.getenv("CHAT_MODEL", "gpt-5.5"),
        "auto_review_model": os.getenv("AUTO_REVIEW_MODEL", "gpt-5.5"),
    }


def _load_json_file(path: str, default=None):
    """Generic helper to load JSON file. Returns default if file missing or parsing fails."""
    if default is None:
        default = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, IOError):
            return default
    return default


def _save_json_file(path: str, data, on_error_msg: str):
    """Generic helper to save JSON file. Prints warning message on error."""
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except IOError as e:
        print(f"Warning: {on_error_msg}: {e}", flush=True)


def load_sessions() -> dict:
    return _load_json_file(HISTORY_FILE, {})


def save_sessions(sessions: dict):
    _save_json_file(HISTORY_FILE, sessions, "could not save chat history")


def load_notifications() -> dict:
    """Load notifications from file and return as dict keyed by notif_id."""
    notif_list = _load_json_file(NOTIFICATIONS_FILE, [])
    # Convert list to dict for O(1) lookup
    return {n["id"]: n for n in notif_list if isinstance(n, dict)}


def save_notifications(notifs: dict):
    """Save notifications dict to file as an ordered list (newest first)."""
    # Convert dict to list, keeping newest notifications first
    notif_list = list(notifs.values())
    # Sort by timestamp descending (newest first)
    notif_list.sort(key=lambda n: n.get("timestamp", ""), reverse=True)
    # Keep only the most recent MAX_NOTIFICATIONS
    _save_json_file(NOTIFICATIONS_FILE, notif_list[:MAX_NOTIFICATIONS], "could not save notifications")


def load_session_names() -> dict:
    return _load_json_file(SESSION_NAMES_FILE, {})


def save_session_names(names: dict):
    _save_json_file(SESSION_NAMES_FILE, names, "could not save session names")


def load_token_usage() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    data = _load_json_file(TOKEN_USAGE_FILE, {})
    if data.get("date") != today:
        return {"date": today, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return data


def save_token_usage(data: dict):
    _save_json_file(TOKEN_USAGE_FILE, data, "could not save token usage")


def accumulate_tokens(usage: dict):
    """Add usage dict to today's running total and persist."""
    with token_usage_lock:
        today = datetime.now().strftime("%Y-%m-%d")
        if token_usage.get("date") != today:
            token_usage.clear()
            token_usage.update({"date": today, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        token_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
        token_usage["completion_tokens"] += usage.get("completion_tokens", 0)
        token_usage["total_tokens"] += usage.get("total_tokens", 0)
        save_token_usage(token_usage)


def append_to_auto_review_session(sessions: dict, activity_name: str, activity_date: str, comment: str):
    """Add an auto-review comment to the dedicated auto-reviews session."""
    history = sessions.get(AUTO_REVIEW_SESSION, [])
    # Add a synthetic user message as context, then the coach reply
    history.append({
        "role": "user",
        "content": f"[Auto-review] {activity_name} — {activity_date}",
    })
    history.append({
        "role": "assistant",
        "content": comment,
    })
    if len(history) > MAX_HISTORY_MESSAGES:
        history = history[-MAX_HISTORY_MESSAGES:]
    sessions[AUTO_REVIEW_SESSION] = history


options = load_options()

if not options.get("openai_api_key"):
    raise RuntimeError("openai_api_key is not configured.")
if not options.get("intervals_athlete_id"):
    raise RuntimeError("intervals_athlete_id is not configured.")
if not options.get("intervals_api_key"):
    raise RuntimeError("intervals_api_key is not configured.")

sessions: dict[str, list] = load_sessions()
notifications: dict = load_notifications()
session_names: dict[str, str] = load_session_names()
token_usage: dict = load_token_usage()

# Thread locks for concurrent request safety
sessions_lock = threading.RLock()
notifications_lock = threading.RLock()
session_names_lock = threading.RLock()
token_usage_lock = threading.RLock()

# Debouncer for session saves — batch writes within a time window.
# Uses threading.Timer so it works correctly from sync endpoints (which run
# in a thread pool and have no running event loop).
class Debouncer:
    def __init__(self, delay_seconds: float = 5.0):
        self.delay = delay_seconds
        self._timer: threading.Timer | None = None
        self._pending_func = None
        self.lock = threading.Lock()

    def schedule_save(self, save_func):
        """Schedule a save, canceling any pending save first."""
        with self.lock:
            if self._timer:
                self._timer.cancel()
            self._pending_func = save_func
            self._timer = threading.Timer(self.delay, self._run)
            self._timer.daemon = True
            self._timer.start()

    def _run(self):
        with self.lock:
            func = self._pending_func
            self._pending_func = None
            self._timer = None
        if func:
            func()

    def flush(self):
        """Cancel pending timer and run the save immediately if one is queued."""
        with self.lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            func = self._pending_func
            self._pending_func = None
        if func:
            func()

sessions_debouncer = Debouncer(delay_seconds=5.0)

agent = TrainingAgent(
    openai_api_key=options["openai_api_key"],
    intervals_athlete_id=options["intervals_athlete_id"],
    intervals_api_key=options["intervals_api_key"],
    hrv_min=options.get("hrv_min", 0),
    hrv_max=options.get("hrv_max", 0),
    rhr_min=options.get("rhr_min", 0),
    rhr_max=options.get("rhr_max", 0),
    hard_intervals_per_week=options.get("hard_intervals_per_week", 2),
    block_start_date=options.get("block_start_date", ""),
    group_ride_keywords=options.get("group_ride_keywords", "group,klub,klubtur"),
    days_back=options.get("days_back", 28),
    chat_model=options.get("chat_model", "gpt-5.5"),
    auto_review_model=options.get("auto_review_model", "gpt-5.5"),
)


# ── Background auto-review polling ──
async def auto_review_loop():
    last_checked = datetime.now() - timedelta(hours=1)
    print("Auto-review polling started.", flush=True)
    while True:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        try:
            since = last_checked
            last_checked = datetime.now()
            new_activities = agent.icu.get_activities_since(since)
            for activity in new_activities:
                if activity.get("coach_tick"):
                    continue
                if not activity.get("rpe") or not activity.get("feel"):
                    continue
                print(f"Auto-reviewing: {activity.get('name')} ({activity.get('id')})", flush=True)
                comment, usage = agent.auto_review(activity)
                accumulate_tokens(usage)

                # Build notification
                notif = {
                    "id": f"auto-{activity['id']}",
                    "timestamp": datetime.now().isoformat(),
                    "activity_id": activity["id"],
                    "activity_name": activity.get("name", "Unnamed activity"),
                    "activity_date": activity.get("date", ""),
                    "comment": comment,
                    "type": "auto_review",
                    "seen": False,
                }

                # Store in persistent notifications with lock
                with notifications_lock:
                    notifications[notif["id"]] = notif
                    save_notifications(notifications)

                # Log to auto-reviews session with lock
                with sessions_lock:
                    append_to_auto_review_session(
                        sessions,
                        activity.get("name", "Unnamed activity"),
                        activity.get("date", ""),
                        comment,
                    )
                    # Debounce the save
                    sessions_debouncer.schedule_save(lambda: save_sessions(sessions))

                print(f"Auto-review done for {activity.get('id')}", flush=True)
        except Exception as e:
            print(f"Auto-review error: {e}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(auto_review_loop())
    yield
    # On shutdown: cancel auto-review loop and flush pending saves
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Flush any pending debounced saves
    sessions_debouncer.flush()


app = FastAPI(title="Training Coach", root_path_in_servers=False, lifespan=lifespan)


# ── Models ──
class ChatRequest(BaseModel):
    session_id: str
    message: str

class ChatResponse(BaseModel):
    reply: str

class HistoryResponse(BaseModel):
    messages: list


# ── Routes ──
@app.get("/")
async def root(request: Request):
    ingress_path = request.headers.get("X-Ingress-Path", "")
    with open("static/index.html") as f:
        html = f.read()
    html = html.replace("__INGRESS_PATH__", ingress_path)
    return HTMLResponse(html)


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    with sessions_lock:
        history = sessions.get(req.session_id, [])
    try:
        reply, updated_history, usage = agent.chat(req.message, history)
        accumulate_tokens(usage)
        if len(updated_history) > MAX_HISTORY_MESSAGES:
            updated_history = updated_history[-MAX_HISTORY_MESSAGES:]
        with sessions_lock:
            sessions[req.session_id] = updated_history
            # Debounce the save — batch writes within 5 second window
            sessions_debouncer.schedule_save(lambda: save_sessions(sessions))
        return ChatResponse(reply=reply)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/history/{session_id}", response_model=HistoryResponse)
def get_history(session_id: str):
    with sessions_lock:
        raw = list(sessions.get(session_id, []))
    displayable = [
        m for m in raw
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str)
    ]
    return HistoryResponse(messages=displayable)


@app.delete("/chat/{session_id}")
def clear_session(session_id: str):
    with sessions_lock:
        sessions.pop(session_id, None)
        # Debounce the save
        sessions_debouncer.schedule_save(lambda: save_sessions(sessions))
    return {"cleared": session_id}


@app.get("/sessions")
def list_sessions():
    with sessions_lock:
        snapshot = {sid: list(hist) for sid, hist in sessions.items()}
    with session_names_lock:
        names_snapshot = dict(session_names)
    return {
        sid: {
            "message_count": len([
                m for m in hist
                if isinstance(m, dict) and m.get("role") in ("user", "assistant")
            ]),
            "name": names_snapshot.get(sid, sid),
        }
        for sid, hist in snapshot.items()
    }


@app.get("/session-names")
def get_session_names():
    """Return server-side session name mappings."""
    with session_names_lock:
        return dict(session_names)


@app.post("/session-names")
def update_session_name(req: dict):
    """Save a session name to the server."""
    sid = req.get("id")
    name = req.get("name")
    if sid and name:
        with session_names_lock:
            session_names[sid] = name
            save_session_names(session_names)
    return {"ok": True}


@app.get("/notifications")
def get_notifications(since: str = None):
    """Return notifications, optionally filtered to only those after a timestamp."""
    all_notifs = list(notifications.values())  # Get notification objects (not keys)
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            all_notifs = [n for n in all_notifs if datetime.fromisoformat(n["timestamp"]) > since_dt]
        except ValueError:
            pass
    return {"notifications": all_notifs}


@app.post("/notifications/{notif_id}/seen")
def mark_seen(notif_id: str):
    """Mark a notification as seen."""
    with notifications_lock:
        if notif_id in notifications:
            notifications[notif_id]["seen"] = True
        save_notifications(notifications)
    return {"ok": True}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/tokens")
def get_tokens():
    """Return today's token usage totals."""
    with token_usage_lock:
        return dict(token_usage)


@app.get("/debug/storage")
def debug_storage():
    """Diagnose where data is being stored and whether files exist."""
    import os
    return {
        "storage_dir": STORAGE_DIR,
        "storage_dir_exists": os.path.exists(STORAGE_DIR),
        "history_file": HISTORY_FILE,
        "history_file_exists": os.path.exists(HISTORY_FILE),
        "history_file_size_bytes": os.path.getsize(HISTORY_FILE) if os.path.exists(HISTORY_FILE) else 0,
        "notifications_file_exists": os.path.exists(NOTIFICATIONS_FILE),
        "session_names_file_exists": os.path.exists(SESSION_NAMES_FILE),
        "sessions_in_memory": list(sessions.keys()),
        "session_names_in_memory": session_names,
        "config_dir_accessible": os.path.exists("/config"),
        "config_dir_writable": os.access("/config", os.W_OK),
    }


app.mount("/static", StaticFiles(directory="static"), name="static")
