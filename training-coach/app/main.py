import json
import os
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from agent import TrainingAgent

OPTIONS_FILE = "/data/options.json"
HISTORY_FILE = "/data/chat_history.json"
MAX_HISTORY_MESSAGES = 200  # cap per session to avoid unbounded growth


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
    }


def load_sessions() -> dict:
    """Load persisted chat sessions from disk."""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_sessions(sessions: dict):
    """Persist chat sessions to disk."""
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(sessions, f)
    except Exception as e:
        print(f"Warning: could not save chat history: {e}")


options = load_options()

if not options.get("openai_api_key"):
    raise RuntimeError("openai_api_key is not configured.")
if not options.get("intervals_athlete_id"):
    raise RuntimeError("intervals_athlete_id is not configured.")
if not options.get("intervals_api_key"):
    raise RuntimeError("intervals_api_key is not configured.")

app = FastAPI(title="Training Coach", root_path_in_servers=False)

# Load history from disk on startup
sessions: dict[str, list] = load_sessions()

agent = TrainingAgent(
    openai_api_key=options["openai_api_key"],
    intervals_athlete_id=options["intervals_athlete_id"],
    intervals_api_key=options["intervals_api_key"],
)


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str


class HistoryResponse(BaseModel):
    messages: list


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
        # Trim if over limit (keep most recent messages)
        if len(updated_history) > MAX_HISTORY_MESSAGES:
            updated_history = updated_history[-MAX_HISTORY_MESSAGES:]
        sessions[req.session_id] = updated_history
        save_sessions(sessions)
        return ChatResponse(reply=reply)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/history/{session_id}", response_model=HistoryResponse)
def get_history(session_id: str):
    """Return the conversation history for a session so the UI can restore it."""
    raw = sessions.get(session_id, [])
    # Only return user/assistant turns — strip tool calls (not renderable in UI)
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
    """List all sessions with their message counts."""
    return {
        sid: {
            "message_count": len([
                m for m in hist
                if isinstance(m, dict) and m.get("role") in ("user", "assistant")
            ]),
            "last_message": next(
                (m.get("content", "")[:80] for m in reversed(hist)
                 if isinstance(m, dict) and m.get("role") == "user"
                 and isinstance(m.get("content"), str)),
                ""
            )
        }
        for sid, hist in sessions.items()
    }


@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/debug/activities")
def debug_activities():
    """Show raw ID fields from the last 5 activities to find the correct format."""
    import requests as req
    from datetime import datetime, timedelta
    oldest = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    newest = datetime.now().strftime("%Y-%m-%d")
    r = req.get(
        f"https://intervals.icu/api/v1/athlete/{agent.icu.athlete_id}/activities",
        headers=agent.icu.headers,
        params={"oldest": oldest, "newest": newest},
    )
    activities = r.json()[:5]
    # Return every field that looks like an ID so we can find the right one
    return [
        {k: v for k, v in a.items() if "id" in k.lower() or k in ("name", "type", "start_date_local")}
        for a in activities
    ]


@app.get("/debug/intervals/{activity_id}")
def debug_intervals(activity_id: str):
    """Try the intervals endpoint and return raw response."""
    import requests as req
    url = f"https://intervals.icu/api/v1/activity/{activity_id}/intervals"
    r = req.get(url, headers=agent.icu.headers)
    return {
        "status_code": r.status_code,
        "url": url,
        "response": r.json() if r.status_code == 200 else r.text[:500],
    }

app.mount("/static", StaticFiles(directory="static"), name="static")
