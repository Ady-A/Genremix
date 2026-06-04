"""
Multiplayer queue and session management over WebSockets.

Flow:
  client connects → sends join_queue{name} → server queues them
  when MIN_PLAYERS are queued → session starts, secret song chosen
  server sends game_start{secret_pos, duration, players} to everyone
  clients send guess{title, artist} at any time during the 60-second window
  server computes genre position, broadcasts to all players
  after GAME_DURATION seconds → server sends game_over{winner, scores, secret}
"""
import asyncio
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from . import song_db
from .genre_map import analyze_genres, calculate_position
from .genre_map import temperature as get_temp

GAME_DURATION = 60          # seconds
MIN_PLAYERS   = 2           # minimum to start a session
MAX_PLAYERS   = 6           # cap per session

PLAYER_COLORS = ["#a855f7", "#06b6d4", "#f97316", "#22c55e", "#ef4444", "#eab308"]


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class Player:
    id: str
    name: str
    ws: WebSocket
    color: str = "#a855f7"
    guesses: list = field(default_factory=list)
    best_distance: float = float("inf")
    best_pos: Optional[dict] = None
    connected: bool = True


@dataclass
class MultiSession:
    id: str
    players: list[Player]
    secret_title: str
    secret_artist: str
    secret_pos: tuple[float, float]
    secret_weights: dict
    started_at: float = 0.0
    game_over: bool = False
    timer_task: Optional[asyncio.Task] = None


# ── Global state ───────────────────────────────────────────────────────────────

_queue: list[Player] = []
_sessions: dict[str, MultiSession] = {}


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _send(player: Player, msg: dict) -> None:
    try:
        await player.ws.send_json(msg)
    except Exception:
        player.connected = False


async def _broadcast(session: MultiSession, msg: dict, exclude: str = "") -> None:
    for p in session.players:
        if p.id != exclude and p.connected:
            await _send(p, msg)


async def _end_session(session: MultiSession) -> None:
    if session.game_over:
        return
    session.game_over = True

    if session.timer_task and not session.timer_task.done():
        session.timer_task.cancel()

    ranked = sorted(
        [p for p in session.players if p.best_distance < float("inf")],
        key=lambda p: p.best_distance,
    )
    winner = ranked[0] if ranked else None

    scores = [
        {
            "player_id": p.id,
            "name": p.name,
            "color": p.color,
            "best_distance": round(p.best_distance, 4) if p.best_distance < float("inf") else None,
            "best_pos": p.best_pos,
            "total_guesses": len(p.guesses),
        }
        for p in sorted(session.players, key=lambda p: p.best_distance)
    ]

    await _broadcast(session, {
        "type": "game_over",
        "winner": {"id": winner.id, "name": winner.name, "color": winner.color} if winner else None,
        "scores": scores,
        "secret": {
            "title": session.secret_title,
            "artist": session.secret_artist,
            "pos": {"x": session.secret_pos[0], "y": session.secret_pos[1]},
            "weights": session.secret_weights,
        },
    })


async def _run_timer(session: MultiSession) -> None:
    await asyncio.sleep(GAME_DURATION)
    await _end_session(session)


async def _try_start_session() -> None:
    global _queue
    if len(_queue) < MIN_PLAYERS:
        return

    count = min(len(_queue), MAX_PLAYERS)
    players, _queue = _queue[:count], _queue[count:]

    genius_token = os.getenv("GENIUS_ACCESS_TOKEN") or None
    song = await song_db.pick_random(genius_token)
    if not song:
        _queue = players + _queue
        return

    weights  = analyze_genres(song)
    pos      = calculate_position(weights)
    sid      = str(uuid.uuid4())

    for i, p in enumerate(players):
        p.color = PLAYER_COLORS[i % len(PLAYER_COLORS)]

    session = MultiSession(
        id=sid,
        players=players,
        secret_title=song.title,
        secret_artist=song.artist,
        secret_pos=pos,
        secret_weights=weights,
        started_at=time.time(),
    )
    _sessions[sid] = session

    player_list = [{"id": p.id, "name": p.name, "color": p.color} for p in players]

    for p in players:
        await _send(p, {
            "type": "game_start",
            "session_id": sid,
            "secret_pos": {"x": pos[0], "y": pos[1]},
            "duration": GAME_DURATION,
            "players": player_list,
            "your_id": p.id,
            "your_color": p.color,
        })

    session.timer_task = asyncio.create_task(_run_timer(session))


# ── WebSocket handler ──────────────────────────────────────────────────────────

async def handle_websocket(ws: WebSocket) -> None:
    await ws.accept()
    player = Player(id=str(uuid.uuid4()), name="Anonymous", ws=ws)
    current_session: Optional[MultiSession] = None

    try:
        async for data in ws.iter_json():
            action = data.get("action", "")

            # ── join queue ─────────────────────────────────────────────────────
            if action == "join_queue":
                name = str(data.get("name", "Anonymous")).strip()[:24] or "Anonymous"
                player.name = name
                if player not in _queue:
                    _queue.append(player)
                await _send(player, {
                    "type": "queued",
                    "player_id": player.id,
                    "queue_size": len(_queue),
                    "waiting_for": max(0, MIN_PLAYERS - len(_queue)),
                })
                await _try_start_session()

            # ── guess ──────────────────────────────────────────────────────────
            elif action == "guess":
                sid = data.get("session_id", "")
                session = _sessions.get(sid)
                if not session or session.game_over:
                    continue

                # Enforce time window
                elapsed = time.time() - session.started_at
                if elapsed > GAME_DURATION + 1:
                    continue

                title  = str(data.get("title",  "")).strip()
                artist = str(data.get("artist", "")).strip()
                if not title or not artist:
                    continue

                genius_token = os.getenv("GENIUS_ACCESS_TOKEN") or None
                song = await song_db.get_song(title, artist, genius_token=genius_token, require_lyrics=False)
                if not song:
                    await _send(player, {"type": "error", "message": "Song not found — try another"})
                    continue

                g_weights = analyze_genres(song)
                g_pos     = calculate_position(g_weights)
                dist      = math.sqrt(
                    (g_pos[0] - session.secret_pos[0]) ** 2 +
                    (g_pos[1] - session.secret_pos[1]) ** 2
                )
                temp      = get_temp(dist)
                guess_num = len(player.guesses) + 1

                player.guesses.append({"title": title, "artist": artist, "distance": dist})
                if dist < player.best_distance:
                    player.best_distance = dist
                    player.best_pos = {"x": g_pos[0], "y": g_pos[1]}

                # Full result back to the guesser
                await _send(player, {
                    "type": "guess_result",
                    "title": song.title,
                    "artist": song.artist,
                    "pos": {"x": g_pos[0], "y": g_pos[1]},
                    "distance": round(dist, 4),
                    "temperature": temp,
                    "number": guess_num,
                })

                # Broadcast position + temp to everyone else (no distance number)
                await _broadcast(session, {
                    "type": "opponent_guess",
                    "player_id": player.id,
                    "player_name": player.name,
                    "color": player.color,
                    "pos": {"x": g_pos[0], "y": g_pos[1]},
                    "temperature": temp,
                    "number": guess_num,
                }, exclude=player.id)

                current_session = session

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        player.connected = False
        if player in _queue:
            _queue.remove(player)
        if current_session and not current_session.game_over:
            await _broadcast(current_session, {
                "type": "player_left",
                "player_id": player.id,
                "name": player.name,
            }, exclude=player.id)
            if all(not p.connected for p in current_session.players):
                await _end_session(current_session)


def queue_size() -> int:
    return len(_queue)
