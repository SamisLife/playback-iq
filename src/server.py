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
            "foul_committed_card": e.get("foul_committed_card"),
            "description": _describe(e),
        })
    return result


# ─── AI helpers ───────────────────────────────────────────────────────────────

def _zone_name(x, y) -> str:
    """Convert StatsBomb coordinates to a readable football zone."""
    if x is None or y is None:
        return "an unknown position"
    # x: 0 = own goal line, 120 = opponent goal line
    if x < 18:     h = "inside his own penalty area"
    elif x < 40:   h = "in the defensive third"
    elif x < 60:   h = "in his own half"
    elif x < 80:   h = "in the attacking half"
    elif x < 102:  h = "in the attacking third"
    elif x < 112:  h = "inside the penalty area"
    else:          h = "at the six-yard box"
    # y: 0 = right touchline, 80 = left touchline; centre goal = 36–44
    if y < 18:     v = "on the right flank"
    elif y < 30:   v = "in the right channel"
    elif y < 38:   v = "right of centre"
    elif y <= 42:  v = "centrally"
    elif y < 50:   v = "left of centre"
    elif y < 62:   v = "in the left channel"
    else:          v = "on the left flank"
    return f"{h}, {v}"


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


# ─── AI explain (agentic with function calling) ───────────────────────────────

_AGENT_TOOLS = genai_types.Tool(function_declarations=[
    genai_types.FunctionDeclaration(
        name="get_events_in_window",
        description="Get all significant match events between two minutes to understand the build-up or aftermath.",
        parameters=genai_types.Schema(
            type=genai_types.Type.OBJECT,
            properties={
                "minute_from": genai_types.Schema(type=genai_types.Type.INTEGER, description="Start minute (inclusive)"),
                "minute_to":   genai_types.Schema(type=genai_types.Type.INTEGER, description="End minute (inclusive)"),
            },
            required=["minute_from", "minute_to"],
        ),
    ),
    genai_types.FunctionDeclaration(
        name="get_passing_sequence",
        description="Get the sequence of passes in a time window to trace ball circulation and build-up patterns.",
        parameters=genai_types.Schema(
            type=genai_types.Type.OBJECT,
            properties={
                "minute_from": genai_types.Schema(type=genai_types.Type.INTEGER, description="Start minute"),
                "minute_to":   genai_types.Schema(type=genai_types.Type.INTEGER, description="End minute"),
            },
            required=["minute_from", "minute_to"],
        ),
    ),
    genai_types.FunctionDeclaration(
        name="get_player_positions",
        description="Get player positions (freeze frame) at the moment of a specific event.",
        parameters=genai_types.Schema(
            type=genai_types.Type.OBJECT,
            properties={
                "event_id": genai_types.Schema(type=genai_types.Type.STRING, description="The event UUID"),
            },
            required=["event_id"],
        ),
    ),
    genai_types.FunctionDeclaration(
        name="get_pressure_events",
        description="Get pressure, duel, and tackle events in a time window — reveals defensive intensity.",
        parameters=genai_types.Schema(
            type=genai_types.Type.OBJECT,
            properties={
                "minute_from": genai_types.Schema(type=genai_types.Type.INTEGER, description="Start minute"),
                "minute_to":   genai_types.Schema(type=genai_types.Type.INTEGER, description="End minute"),
            },
            required=["minute_from", "minute_to"],
        ),
    ),
])

_STEP_LABELS = {
    "get_events_in_window": "Scanning event timeline",
    "get_passing_sequence": "Tracing passing sequence",
    "get_player_positions": "Reading player positions",
    "get_pressure_events":  "Analysing defensive pressure",
}


def _step_str(name: str, args: dict, result: dict) -> str:
    """Build a [STEP] label with result metadata: 'Label | detail'."""
    label = _STEP_LABELS.get(name, name.replace("_", " ").title())
    if name == "get_events_in_window":
        count = len(result.get("events", []))
        detail = f"{count} events · min {args.get('minute_from')}–{args.get('minute_to')}"
    elif name == "get_passing_sequence":
        count = len(result.get("passes", []))
        detail = f"{count} passes · min {args.get('minute_from')}–{args.get('minute_to')}"
    elif name == "get_player_positions":
        count = len(result.get("players", []))
        detail = f"{count} players mapped on pitch"
    elif name == "get_pressure_events":
        count = len(result.get("events", []))
        detail = f"{count} defensive actions · min {args.get('minute_from')}–{args.get('minute_to')}"
    else:
        detail = ""
    return f"{label} | {detail}" if detail else label


@app.post("/api/explain-agent")
async def explain_agent(body: ExplainBody):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not set")

    all_events = _md.get_events()
    event = next((e for e in all_events if e["event_id"] == body.event_id), None)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    minute = event["minute"] or 0
    goals_before = {
        team: sum(
            1 for e in all_events
            if e["type"] == "Shot" and e.get("shot_outcome") == "Goal"
            and e["team"] == team and (e["minute"] or 0) < minute
        )
        for team in ["Morocco", "Portugal"]
    }
    score_str = f"Morocco {goals_before['Morocco']} – {goals_before['Portugal']} Portugal"

    _noise = {"Ball Receipt*", "Carry", "Starting XI", "Half Start", "Half End"}

    def _exec_tool(name: str, args: dict) -> dict:
        if name == "get_events_in_window":
            mf, mt = int(args["minute_from"]), int(args["minute_to"])
            evs = [e for e in all_events if mf <= (e["minute"] or 0) <= mt and e["type"] not in _noise]
            return {"events": [
                {"minute": e["minute"], "second": e["second"], "type": e["type"],
                 "player": e["player"], "team": e["team"],
                 "outcome": e.get("shot_outcome") or e.get("pass_outcome") or ""}
                for e in evs[:30]
            ]}
        if name == "get_passing_sequence":
            mf, mt = int(args["minute_from"]), int(args["minute_to"])
            passes = [e for e in all_events if e["type"] == "Pass" and mf <= (e["minute"] or 0) <= mt]
            return {"passes": [
                {"minute": e["minute"], "second": e["second"], "player": e["player"],
                 "team": e["team"], "length_m": round(e.get("pass_length") or 0, 1),
                 "outcome": e.get("pass_outcome") or "Complete",
                 "is_key_pass": bool(e.get("is_key_pass"))}
                for e in passes[:25]
            ]}
        if name == "get_player_positions":
            ff = _md.get_freeze_frame(str(args["event_id"]))
            return {"players": [
                {"name": p["player_name"], "team": p["team"],
                 "x": round(p["location_x"] or 0, 1), "y": round(p["location_y"] or 0, 1),
                 "is_actor": p["is_actor"]}
                for p in ff if p["location_x"] is not None
            ][:22]}
        if name == "get_pressure_events":
            mf, mt = int(args["minute_from"]), int(args["minute_to"])
            evs = [e for e in all_events
                   if e["type"] in ("Pressure", "Duel", "Tackle", "Interception")
                   and mf <= (e["minute"] or 0) <= mt]
            return {"events": [
                {"minute": e["minute"], "type": e["type"],
                 "player": e["player"], "team": e["team"]}
                for e in evs[:20]
            ]}
        return {"error": f"Unknown tool: {name}"}

    system_prompt = f"""You are a tactical football analyst providing live commentary for an interactive replay of the 2022 FIFA World Cup quarter-final: Morocco vs Portugal.

MATCH STATE at {minute}' (period {event.get('period', '?')}):
Score: {score_str}

FOCAL EVENT:
  Type      : {event['type']}
  Outcome   : {event.get('shot_outcome') or 'N/A'}
  Player    : {event['player']} ({event['team']})
  Location  : x={event['location_x']}, y={event['location_y']}  (StatsBomb: x 0→120 left→right, y 0→80)
  xG        : {event.get('shot_xg') or 'N/A'}
  Technique : {event.get('shot_technique') or 'N/A'}
  Body part : {event.get('shot_body_part') or 'N/A'}
  Under pressure: {event.get('under_pressure') or False}
  Play pattern: {event.get('play_pattern') or 'N/A'}

Use the tools to investigate this moment:
1. get_events_in_window to see the build-up (e.g., minutes {minute-3}–{minute})
2. get_player_positions for the freeze frame shape
3. get_passing_sequence to trace how the attack developed
4. Optionally get_pressure_events if you need defensive details

Then write a concise 3–4 sentence tactical analysis. Be specific: name players, reference pitch zones, cite actual data from the tools. Reference timestamps like "in the {minute-1}th minute" or "from the 40:22 build-up pass"."""

    client = genai.Client(api_key=api_key)
    config_tools = genai_types.GenerateContentConfig(
        tools=[_AGENT_TOOLS],
        thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
    )
    config_final = genai_types.GenerateContentConfig(
        thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        max_output_tokens=600,
    )

    async def stream_agent():
        contents = [
            genai_types.Content(role="user", parts=[genai_types.Part.from_text(text=system_prompt)])
        ]

        for _ in range(5):
            resp = await client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=contents,
                config=config_tools,
            )

            fn_parts = [p for p in resp.candidates[0].content.parts if p.function_call]
            if not fn_parts:
                break

            contents.append(resp.candidates[0].content)

            response_parts = []
            for part in fn_parts:
                fc = part.function_call
                args_dict = dict(fc.args)
                result = _exec_tool(fc.name, args_dict)
                yield f"[STEP] {_step_str(fc.name, args_dict, result)}\n"
                response_parts.append(
                    genai_types.Part(function_response=genai_types.FunctionResponse(
                        name=fc.name, response=result
                    ))
                )

            contents.append(genai_types.Content(role="user", parts=response_parts))

        yield "[DONE]\n"

        streaming = await client.aio.models.generate_content_stream(
            model="gemini-2.5-flash",
            contents=contents,
            config=config_final,
        )
        async for chunk in streaming:
            if chunk.text:
                yield chunk.text

    return StreamingResponse(stream_agent(), media_type="text/plain")


# ─── AI agent chat (conversational, football-language) ────────────────────────

class ChatBody(BaseModel):
    event_id: str
    question: str
    history: list[dict] = []   # [{"role": "user"|"assistant", "content": str}]


@app.post("/api/agent-chat")
async def agent_chat(body: ChatBody):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not set")

    all_events = _md.get_events()
    event = next((e for e in all_events if e["event_id"] == body.event_id), None)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    minute = event["minute"] or 0
    goals_before = {
        team: sum(
            1 for e in all_events
            if e["type"] == "Shot" and e.get("shot_outcome") == "Goal"
            and e["team"] == team and (e["minute"] or 0) < minute
        )
        for team in ["Morocco", "Portugal"]
    }
    score_str = f"Morocco {goals_before['Morocco']} – {goals_before['Portugal']} Portugal"

    _noise = {"Ball Receipt*", "Carry", "Starting XI", "Half Start", "Half End"}

    def _exec_tool(name: str, args: dict) -> dict:
        if name == "get_events_in_window":
            mf, mt = int(args["minute_from"]), int(args["minute_to"])
            evs = [e for e in all_events if mf <= (e["minute"] or 0) <= mt and e["type"] not in _noise]
            return {"events": [
                {"minute": e["minute"], "second": e["second"], "type": e["type"],
                 "player": e["player"], "team": e["team"],
                 "location": _zone_name(e.get("location_x"), e.get("location_y")),
                 "outcome": e.get("shot_outcome") or e.get("pass_outcome") or ""}
                for e in evs[:30]
            ]}
        if name == "get_passing_sequence":
            mf, mt = int(args["minute_from"]), int(args["minute_to"])
            passes = [e for e in all_events if e["type"] == "Pass" and mf <= (e["minute"] or 0) <= mt]
            return {"passes": [
                {"minute": e["minute"], "second": e["second"], "player": e["player"],
                 "team": e["team"], "from_zone": _zone_name(e.get("location_x"), e.get("location_y")),
                 "length_m": round(e.get("pass_length") or 0, 1),
                 "outcome": e.get("pass_outcome") or "Complete",
                 "is_key_pass": bool(e.get("is_key_pass"))}
                for e in passes[:25]
            ]}
        if name == "get_player_positions":
            ff = _md.get_freeze_frame(str(args["event_id"]))
            return {"players": [
                {"name": p["player_name"] or "unnamed player",
                 "team": p["team"],
                 "zone": _zone_name(p["location_x"], p["location_y"]),
                 "is_actor": p["is_actor"]}
                for p in ff if p["location_x"] is not None
            ][:22]}
        if name == "get_pressure_events":
            mf, mt = int(args["minute_from"]), int(args["minute_to"])
            evs = [e for e in all_events
                   if e["type"] in ("Pressure", "Duel", "Tackle", "Interception")
                   and mf <= (e["minute"] or 0) <= mt]
            return {"events": [
                {"minute": e["minute"], "type": e["type"],
                 "player": e["player"], "team": e["team"],
                 "zone": _zone_name(e.get("location_x"), e.get("location_y"))}
                for e in evs[:20]
            ]}
        return {"error": f"Unknown tool: {name}"}

    shot_zone = _zone_name(event.get("location_x"), event.get("location_y"))
    teams = {"scoring": event["team"], "conceding": "Portugal" if event["team"] == "Morocco" else "Morocco"}

    is_first_question = len(body.history) == 0

    system_context = f"""You are a tactical football analyst for an interactive replay of the 2022 FIFA World Cup quarter-final: Morocco vs Portugal.

MATCH STATE at {minute}' (period {event.get('period', '?')}):  Score: {score_str}

EVENT:
  Type     : {event['type']}
  Outcome  : {event.get('shot_outcome') or event.get('foul_committed_card') or 'N/A'}
  Player   : {event['player']} ({event['team']})
  Zone     : {shot_zone}
  Technique: {event.get('shot_technique') or 'N/A'}
  Body part: {event.get('shot_body_part') or 'N/A'}
  Play pattern: {event.get('play_pattern') or 'N/A'}

CRITICAL RULES — follow these exactly:
• NEVER mention raw numbers like x=112, y=39, xG=0.45 or StatsBomb coordinates. Use football language only.
• Describe positions as: "in the penalty area", "on the right flank", "in the six-yard box", "at the edge of the box", "in behind the defensive line", etc.
• Explain WHAT CAUSED this moment: what did {teams['scoring']} do well tactically? What did {teams['conceding']} get wrong?
• Reference actual player names from the data. Be specific.
• Write for a football fan — clear, engaging, no jargon about data formats.
• Answer the user's specific question in 3–5 sentences."""

    if is_first_question:
        system_context += f"""

For this first question, use the tools to investigate:
1. get_events_in_window to see the build-up (around minute {minute})
2. get_player_positions for the positioning at the moment
3. get_passing_sequence to trace how the attack developed
4. Optionally get_pressure_events for defensive battle details"""
    else:
        system_context += """

The conversation above already has context from tool calls. Answer this follow-up directly using what you know.
Only call tools again if the question requires new data you don't already have."""

    client = genai.Client(api_key=api_key)
    config_tools = genai_types.GenerateContentConfig(
        tools=[_AGENT_TOOLS],
        thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
    )
    config_final = genai_types.GenerateContentConfig(
        thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        max_output_tokens=700,
    )

    async def stream_chat():
        contents = [
            genai_types.Content(role="user", parts=[genai_types.Part.from_text(text=system_context)])
        ]
        # Inject conversation history as alternating turns
        for msg in body.history:
            role = "model" if msg["role"] == "assistant" else "user"
            contents.append(genai_types.Content(
                role=role,
                parts=[genai_types.Part.from_text(text=msg["content"])]
            ))
        # Current question
        contents.append(genai_types.Content(
            role="user",
            parts=[genai_types.Part.from_text(text=body.question)]
        ))

        for _ in range(5):
            resp = await client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=contents,
                config=config_tools,
            )
            fn_parts = [p for p in resp.candidates[0].content.parts if p.function_call]
            if not fn_parts:
                break
            contents.append(resp.candidates[0].content)
            response_parts = []
            for part in fn_parts:
                fc = part.function_call
                args_dict = dict(fc.args)
                result = _exec_tool(fc.name, args_dict)
                yield f"[STEP] {_step_str(fc.name, args_dict, result)}\n"
                response_parts.append(
                    genai_types.Part(function_response=genai_types.FunctionResponse(
                        name=fc.name, response=result
                    ))
                )
            contents.append(genai_types.Content(role="user", parts=response_parts))

        yield "[DONE]\n"
        streaming = await client.aio.models.generate_content_stream(
            model="gemini-2.5-flash",
            contents=contents,
            config=config_final,
        )
        async for chunk in streaming:
            if chunk.text:
                yield chunk.text

    return StreamingResponse(stream_chat(), media_type="text/plain")


# ─── Frontend (serves built React app from frontend/dist/) ───────────────────

_DIST = Path(__file__).parent.parent / "frontend" / "dist"

@app.get("/")
def index():
    return FileResponse(_DIST / "index.html")

@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    candidate = _DIST / full_path
    if candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(_DIST / "index.html")
