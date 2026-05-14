"""FastAPI backend for Playback IQ."""
import json
import os
from contextlib import asynccontextmanager
from typing import Optional

import truststore
truststore.inject_into_ssl()  # uses the OS certificate store; fixes Python 3.14 on Windows

from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

load_dotenv()

from src.parser import MatchData

_md: Optional[MatchData] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _md
    _md = MatchData()
    _md.load()
    print("MatchData loaded successfully.")
    yield


app = FastAPI(title="Playback IQ", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Match info ───────────────────────────────────────────────────────────────

@app.get("/api/match-info")
def match_info():
    info = _md.get_match_info()
    events = _md.get_events()
    max_minute = max((e["minute"] or 0 for e in events), default=90)
    return {**info, "total_duration_minutes": max_minute}


# ─── Timeline ─────────────────────────────────────────────────────────────────

@app.get("/api/timeline")
def timeline():
    return _md.get_timeline_data()


# ─── Events ───────────────────────────────────────────────────────────────────

@app.get("/api/events")
def events(
    minute_from: Optional[int] = Query(None),
    minute_to: Optional[int] = Query(None),
    event_types: Optional[str] = Query(None, description="Comma-separated event types"),
):
    result = _md.get_events()
    if minute_from is not None:
        result = [e for e in result if (e["minute"] or 0) >= minute_from]
    if minute_to is not None:
        result = [e for e in result if (e["minute"] or 0) <= minute_to]
    if event_types:
        types = {t.strip() for t in event_types.split(",")}
        result = [e for e in result if e["type"] in types]
    return result


# ─── Freeze frame ─────────────────────────────────────────────────────────────

@app.get("/api/freeze-frame/{event_id}")
def freeze_frame(event_id: str):
    players = _md.get_freeze_frame(event_id)
    if not players:
        raise HTTPException(status_code=404, detail="No freeze frame for this event")
    return players


# ─── Lineups ──────────────────────────────────────────────────────────────────

@app.get("/api/lineups")
def lineups():
    return _md.get_lineups()


# ─── Key events ───────────────────────────────────────────────────────────────

def _describe(e: dict) -> str:
    player = e.get("player") or "Unknown"
    team = e.get("team") or ""
    minute = e.get("minute", 0)
    etype = e.get("type", "")
    t = f"{minute}'"

    if etype == "Shot":
        outcome = e.get("shot_outcome") or ""
        technique = e.get("shot_technique") or ""
        if outcome == "Goal":
            detail = f" ({technique.lower()})" if technique and technique != "Normal" else ""
            return f"{player}{detail} goal, {t}"
        if outcome == "Saved":
            return f"Shot saved — {player} ({team}), {t}"
        if outcome == "Blocked":
            return f"Shot blocked — {player} ({team}), {t}"
        if outcome == "Post":
            return f"Post! — {player} ({team}), {t}"
        return f"Shot off target — {player} ({team}), {t}"

    if etype == "Foul Committed":
        card = e.get("foul_committed_card") or ""
        return f"{card} — {player} ({team}), {t}" if card else f"Foul — {player} ({team}), {t}"

    if etype == "Substitution":
        on = e.get("substitution_replacement") or "?"
        return f"Sub: {on} on for {player} ({team}), {t}"

    if etype == "Pass":
        suffix = " (goal assist)" if e.get("is_goal_assist") else ""
        return f"Key pass{suffix} — {player} ({team}), {t}"

    return f"{etype} — {player} ({team}), {t}"


@app.get("/api/key-events")
def key_events():
    result = []
    for e in _md.get_events():
        etype = e.get("type", "")
        is_key = (
            etype == "Shot"
            or etype == "Substitution"
            or (etype == "Foul Committed" and e.get("foul_committed_card"))
            or (etype == "Pass" and e.get("is_key_pass"))
        )
        if not is_key:
            continue
        result.append({
            "event_id": e["event_id"],
            "minute": e["minute"],
            "second": e["second"],
            "period": e["period"],
            "type": etype,
            "team": e["team"],
            "player": e["player"],
            "shot_outcome": e.get("shot_outcome"),
            "shot_xg": e.get("shot_xg"),
            "description": _describe(e),
        })
    return result


# ─── AI explain ───────────────────────────────────────────────────────────────

class ExplainBody(BaseModel):
    event_id: str
    context_minutes: int = 2


@app.post("/api/explain")
async def explain(body: ExplainBody):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=500, detail="GEMINI_API_KEY not set — add it to your .env file"
        )

    all_events = _md.get_events()
    event = next((e for e in all_events if e["event_id"] == body.event_id), None)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    minute = event["minute"] or 0

    # Surrounding context — filter out pure ball-movement noise
    _noise = {"Ball Receipt*", "Carry", "Starting XI", "Half Start", "Half End"}
    ctx = [
        e for e in all_events
        if abs((e["minute"] or 0) - minute) <= body.context_minutes
        and e["type"] not in _noise
    ][:25]

    ff = _md.get_freeze_frame(body.event_id)
    lineups = _md.get_lineups()

    # Score at this exact moment (goals strictly before this minute)
    goals_before = {
        team: sum(
            1 for e in all_events
            if e["type"] == "Shot"
            and e.get("shot_outcome") == "Goal"
            and e["team"] == team
            and (e["minute"] or 0) < minute
        )
        for team in ["Morocco", "Portugal"]
    }
    score_str = f"Morocco {goals_before['Morocco']} – {goals_before['Portugal']} Portugal"

    ctx_block = "\n".join(
        f"  {e['minute']}:{str(e['second'] or 0).zfill(2)} | {e['type']:<22} | "
        f"{(e['player'] or '—'):<35} | {e['team']}"
        for e in ctx
    )

    ff_block = "\n".join(
        f"  {(p['player_name'] or 'Unknown'):<35} | "
        f"{'teammate' if p['is_teammate'] else 'opponent'} | "
        f"({p['location_x']:.1f}, {p['location_y']:.1f})"
        for p in ff
        if p["location_x"] is not None
    )[:15]  # cap at 15 players

    morocco_shape = " · ".join(p["position"] for p in lineups.get("Morocco", []))
    portugal_shape = " · ".join(p["position"] for p in lineups.get("Portugal", []))

    prompt = f"""You are a tactical football analyst providing real-time commentary for an interactive match replay of the 2022 FIFA World Cup quarter-final: Morocco vs Portugal.

MATCH STATE at {minute}' (period {event.get('period', '?')}):
Score: {score_str}

FOCAL EVENT:
  Type       : {event['type']}
  Player     : {event['player']} ({event['team']})
  Location   : x={event['location_x']}, y={event['location_y']}
               (StatsBomb pitch: x 0→120 = goal to goal, y 0→80 = bottom to top)
  Outcome    : {event.get('shot_outcome') or 'N/A'}
  xG         : {event.get('shot_xg') or 'N/A'}
  Technique  : {event.get('shot_technique') or 'N/A'}
  Play pattern: {event.get('play_pattern') or 'N/A'}

EVENTS ±{body.context_minutes} min (noise filtered):
{ctx_block or '  (none)'}

FREEZE FRAME — player positions at this moment:
{ff_block or '  (no positional data)'}

FORMATIONS:
  Morocco : {morocco_shape}
  Portugal: {portugal_shape}

Write a concise tactical analysis in 3–4 sentences:
1. What is happening at this exact moment and why the player is in this position
2. What the tactical battle looks like — shape, pressure, space
3. Why this moment matters in the context of the match narrative
Be specific. Reference player names, positions, and pitch zones."""

    client = genai.Client(api_key=api_key)

    async def stream_response():
        response = await client.aio.models.generate_content_stream(
            model="gemini-2.5-flash",
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                max_output_tokens=500,
            ),
        )
        async for chunk in response:
            if chunk.text:
                yield chunk.text

    return StreamingResponse(stream_response(), media_type="text/plain")


# ─── Frontend ─────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(Path(__file__).parent.parent / "index.html")
