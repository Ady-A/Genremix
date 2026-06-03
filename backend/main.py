"""
Genremix: Genre Map Edition
Place songs on a 2D genre map. Find the secret song by proximity.
"""
import asyncio
import math
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from . import cache as hint_cache
from . import song_db
from .ai_engine import generate_hints
from .genre_map import GENRE_POLES, analyze_genres, calculate_position, temperature
from .models import SongData
from .multiplayer import handle_websocket, queue_size

MAX_HINTS = 4


# ── Session ────────────────────────────────────────────────────────────────────

@dataclass
class GenreSession:
    secret: SongData
    secret_pos: tuple[float, float]
    secret_weights: dict
    guesses: list[dict] = field(default_factory=list)  # [{title,artist,pos,weights,dist,temp,correct}]
    game_over: bool = False
    won: bool = False
    hints_revealed: list[int] = field(default_factory=list)


_sessions: dict[str, GenreSession] = {}


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await song_db.initialize()
    yield


app = FastAPI(title="Genremix Genre Map API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Helpers ────────────────────────────────────────────────────────────────────

def _dist(a: tuple, b: tuple) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)


def _hints_for_session(session: GenreSession):
    song = session.secret
    bundle = hint_cache.get_cached(song.title, song.artist)
    if bundle is None:
        bundle = generate_hints(song)
        hint_cache.store(song.title, song.artist, bundle)
    return bundle


# ── Game routes ────────────────────────────────────────────────────────────────

async def _start(mode: str):
    genius_token = os.getenv("GENIUS_ACCESS_TOKEN") or None
    song = await (song_db.pick_daily(genius_token) if mode == "daily"
                  else song_db.pick_random(genius_token))
    if not song:
        raise HTTPException(503, "No songs with lyrics available yet — try again shortly")
    weights = analyze_genres(song)
    pos = calculate_position(weights)
    sid = str(uuid.uuid4())
    _sessions[sid] = GenreSession(secret=song, secret_pos=pos, secret_weights=weights)
    return {"session_id": sid, "mode": mode, "secret_pos": {"x": pos[0], "y": pos[1]}}


@app.post("/api/game/start")
async def start_game():
    return await _start("random")


@app.post("/api/game/daily")
async def start_daily():
    return await _start("daily")


@app.post("/api/game/guess")
async def submit_guess(body: dict):
    sid = body.get("session_id", "")
    title = body.get("title", "").strip()
    artist = body.get("artist", "").strip()

    session = _sessions.get(sid)
    if not session:
        raise HTTPException(404, "Session not found")
    if session.game_over:
        raise HTTPException(400, "Game already over")

    genius_token = os.getenv("GENIUS_ACCESS_TOKEN") or None
    song = await song_db.get_song(title, artist, genius_token=genius_token)
    if not song:
        raise HTTPException(404, "Could not find lyrics for that song — try another")

    # Prevent re-guessing the same song
    already = any(
        g["title"].lower() == song.title.lower() and g["artist"].lower() == song.artist.lower()
        for g in session.guesses
    )
    if already:
        raise HTTPException(400, "Already guessed this song")

    correct = (
        song.title.lower() == session.secret.title.lower()
        and song.artist.lower() == session.secret.artist.lower()
    )

    weights = analyze_genres(song)
    pos = calculate_position(weights)
    dist = _dist(pos, session.secret_pos)
    temp = temperature(dist)

    guess_entry = {
        "title": song.title,
        "artist": song.artist,
        "pos": {"x": pos[0], "y": pos[1]},
        "weights": weights,
        "distance": round(dist, 4),
        "temperature": temp,
        "correct": correct,
        "number": len(session.guesses) + 1,
    }
    session.guesses.append(guess_entry)

    if correct:
        session.game_over = True
        session.won = True

    return {
        "correct": correct,
        "game_over": session.game_over,
        "guess": guess_entry,
        "total_guesses": len(session.guesses),
        "secret": {"title": session.secret.title, "artist": session.secret.artist,
                   "pos": {"x": session.secret_pos[0], "y": session.secret_pos[1]},
                   "weights": session.secret_weights} if session.game_over else None,
    }


@app.get("/api/game/state/{session_id}")
async def get_state(session_id: str):
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    return {
        "game_over": session.game_over,
        "won": session.won,
        "total_guesses": len(session.guesses),
        "guesses": session.guesses,
        "hints_revealed": session.hints_revealed,
        "secret": {"title": session.secret.title, "artist": session.secret.artist,
                   "pos": {"x": session.secret_pos[0], "y": session.secret_pos[1]},
                   "weights": session.secret_weights} if session.game_over else None,
    }


# ── Hint routes (manual reveal, no auto-unlock) ────────────────────────────────

HINT_LABELS = {1: "Style Matrix", 2: "Semantic Jargon", 3: "Metadata Riddle", 4: "Lyric Snippet"}


@app.post("/api/game/hint/{session_id}/{layer}")
async def reveal_hint(session_id: str, layer: int):
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    if layer not in range(1, MAX_HINTS + 1):
        raise HTTPException(400, "Layer must be 1–4")
    if layer not in session.hints_revealed:
        session.hints_revealed.append(layer)
    _hints_for_session(session)   # pre-generate so next fetch is instant
    return {"unlocked": layer}


@app.get("/api/game/hint/{session_id}/{layer}")
async def get_hint(session_id: str, layer: int):
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    if layer not in range(1, MAX_HINTS + 1):
        raise HTTPException(400, "Layer must be 1–4")

    locked = layer not in session.hints_revealed
    if locked:
        return {"layer": layer, "label": HINT_LABELS[layer], "locked": True, "content": None}

    bundle = _hints_for_session(session)
    if layer == 1:   content = bundle.matrix_breakdown
    elif layer == 2: content = bundle.jargon_translation.model_dump()
    elif layer == 3: content = bundle.biographical_riddle
    else:            content = bundle.obfuscated_snippet

    return {"layer": layer, "label": HINT_LABELS[layer], "locked": False, "content": content}


# ── Song list for dropdown ─────────────────────────────────────────────────────

@app.get("/api/songs/list")
async def songs_list():
    """Return all songs if pool is small; empty list if large (use /search instead)."""
    if song_db.is_large():
        return []
    return song_db.search("", limit=10000) if song_db.total() > 0 else []


@app.get("/api/songs/search")
async def songs_search(q: str = ""):
    return song_db.search(q, limit=12)


@app.get("/api/stats")
async def stats():
    return {"total": song_db.total(), "large_pool": song_db.is_large(), "queue": queue_size()}


@app.websocket("/ws/multiplayer")
async def multiplayer_ws(ws: WebSocket):
    await handle_websocket(ws)


# ── Genre poles (sent to frontend for map rendering) ──────────────────────────

@app.get("/api/genre-poles")
async def genre_poles():
    return GENRE_POLES


@app.get("/api/capabilities")
async def capabilities():
    return {"websockets": not bool(os.getenv("VERCEL"))}


# ── Serve frontend (local dev only — Vercel serves public/ via CDN) ────────────

if not os.getenv("VERCEL"):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
