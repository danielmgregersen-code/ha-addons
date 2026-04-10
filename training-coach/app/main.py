import json
import os
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from agent import TrainingAgent

# Home Assistant add-ons get their config at /data/options.json
OPTIONS_FILE = "/data/options.json"

def load_options() -> dict:
    """Load config from HA add-on options, fall back to env vars for local dev."""
    if os.path.exists(OPTIONS_FILE):
        with open(OPTIONS_FILE) as f:
            return json.load(f)
    # Fallback for local development
    return {
        "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
        "intervals_athlete_id": os.getenv("INTERVALS_ATHLETE_ID", ""),
        "intervals_api_key": os.getenv("INTERVALS_API_KEY", ""),
        "days_back": int(os.getenv("DAYS_BACK", "14")),
        "days_ahead": int(os.getenv("DAYS_AHEAD", "21")),
    }

options = load_options()

if not options.get("openai_api_key"):
    raise RuntimeError("openai_api_key is not configured. Set it in the add-on configuration.")
if not options.get("intervals_athlete_id"):
    raise RuntimeError("intervals_athlete_id is not configured.")
if not options.get("intervals_api_key"):
    raise RuntimeError("intervals_api_key is not configured.")

app = FastAPI(title="Training Coach")

sessions: dict[str, list] = {}

agent = TrainingAgent(
    openai_api_key=options["openai_api_key"],
    intervals_athlete_id=options["intervals_athlete_id"],
    intervals_api_key=options["intervals_api_key"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str


@app.get("/")
def root():
    return FileResponse("static/index.html")


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    history = sessions.get(req.session_id, [])
    try:
        reply, updated_history = agent.chat(req.message, history)
        sessions[req.session_id] = updated_history
        return ChatResponse(reply=reply)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/chat/{session_id}")
def clear_session(session_id: str):
    sessions.pop(session_id, None)
    return {"cleared": session_id}


@app.get("/health")
def health():
    return {"status": "ok"}
