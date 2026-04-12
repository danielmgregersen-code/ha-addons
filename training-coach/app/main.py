import json
import os
import asyncio
from datetime import datetime, timedelta
from collections import deque
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from agent import TrainingAgent

OPTIONS_FILE = "/data/options.json"
HISTORY_FILE = "/data/chat_history.json"
MAX_HISTORY_MESSAGES = 200
POLL_INTERVAL_SECONDS = 120  # 2 minutes

# Notification queue — stores auto-review notifications for the UI to pick up
notifications: deque = deque(maxlen=50)


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
    }


def load_sessions() -> dict:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_sessions(sessions: dict):
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(sessions, f)
    except Exception as e:
        print(f"Warning: could not save chat history: {e}", flush=True)


options = load_options()

if not options.get("openai_api_key"):
    raise RuntimeError("openai_api_key is not configured.")
if not options.get("intervals_athlete_id"):
    raise RuntimeError("intervals_athlete_id is not configured.")
if not options.get("intervals_api_key"):
    raise RuntimeError("intervals_api_key is not configured.")

sessions: dict[str, list] = load_sessions()

agent = TrainingAgent(
    openai_api_key=options["openai_api_key"],
    intervals_athlete_id=options["intervals_athlete_id"],
    intervals_api_key=options["intervals_api_key"],
    hrv_min=options.get("hrv_min", 0),
    hrv_max=options.get("hrv_max", 0),
    rhr_min=options.get("rhr_min", 0),
    rhr_max=options.get("rhr_max", 0),
)


# ── Background auto-review polling ──
async def auto_review_loop():
    """Polls for new unreviewed activities every POLL_INTERVAL_SECONDS."""
    last_checked = datetime.now() - timedelta(hours=1)
    print("Auto-review polling started.", flush=True)
    while True:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        try:
            since = last_checked
            last_checked = datetime.now()
            new_activities = agent.icu.get_activities_since(since)
            for activity in new_activities:
                # Skip if already reviewed (has a coach tick)
                if activity.get("coach_tick"):
                    continue
                print(f"Auto-reviewing new activity: {activity.get('name')} ({activity.get('id')})", flush=True)
                comment = agent.auto_review(activity)
                notifications.append({
                    "id": f"auto-{activity['id']}",
                    "timestamp": datetime.now().isoformat(),
                    "activity_id": activity["id"],
                    "activity_name": activity.get("name", "Unnamed activity"),
                    "activity_date": activity.get("date", ""),
                    "comment": comment,
                    "type": "auto_review",
                })
                print(f"Auto-review posted for {activity.get('id')}", flush=True)
        except Exception as e:
            print(f"Auto-review error: {e}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(auto_review_loop())
    yield
    task.cancel()


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
    history = sessions.get(req.session_id, [])
    try:
        reply, updated_history = agent.chat(req.message, history)
        if len(updated_history) > MAX_HISTORY_MESSAGES:
            updated_history = updated_history[-MAX_HISTORY_MESSAGES:]
        sessions[req.session_id] = updated_history
        save_sessions(sessions)
        return ChatResponse(reply=reply)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/history/{session_id}", response_model=HistoryResponse)
def get_history(session_id: str):
    raw = sessions.get(session_id, [])
    displayable = [
        m for m in raw
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str)
    ]
    return HistoryResponse(messages=displayable)


@app.delete("/chat/{session_id}")
def clear_session(session_id: str):
    sessions.pop(session_id, None)
    save_sessions(sessions)
    return {"cleared": session_id}


@app.get("/sessions")
def list_sessions():
    return {
        sid: {
            "message_count": len([
                m for m in hist
                if isinstance(m, dict) and m.get("role") in ("user", "assistant")
            ]),
        }
        for sid, hist in sessions.items()
    }


@app.get("/notifications")
def get_notifications(since: str = None):
    """Return pending auto-review notifications, optionally filtered by timestamp."""
    all_notifs = list(notifications)
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            all_notifs = [n for n in all_notifs if datetime.fromisoformat(n["timestamp"]) > since_dt]
        except Exception:
            pass
    return {"notifications": all_notifs}


@app.get("/health")
def health():
    return {"status": "ok"}


app.mount("/static", StaticFiles(directory="static"), name="static")
