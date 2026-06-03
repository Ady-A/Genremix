"""
Genre map engine.
Gemini analyzes lyrics and outputs percentage weights across 14 genre poles.
A song's map position is the weighted centroid of those poles.
"""
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Optional

import google.generativeai as genai

from .models import SongData

# ── Genre poles ────────────────────────────────────────────────────────────────
# (x, y) in [0,1]×[0,1]; y=1 is top of canvas (flipped when drawing)

GENRE_POLES: dict[str, dict] = {
    "Pop":        {"x": 0.50, "y": 0.55, "color": "#ec4899"},
    "Rock":       {"x": 0.20, "y": 0.75, "color": "#ef4444"},
    "Hip-Hop":    {"x": 0.73, "y": 0.78, "color": "#f97316"},
    "R&B":        {"x": 0.68, "y": 0.38, "color": "#a855f7"},
    "Soul":       {"x": 0.50, "y": 0.26, "color": "#eab308"},
    "Electronic": {"x": 0.87, "y": 0.62, "color": "#06b6d4"},
    "Country":    {"x": 0.22, "y": 0.26, "color": "#22c55e"},
    "Folk":       {"x": 0.14, "y": 0.50, "color": "#d97706"},
    "Jazz":       {"x": 0.37, "y": 0.18, "color": "#3b82f6"},
    "Metal":      {"x": 0.10, "y": 0.90, "color": "#6b7280"},
    "Classical":  {"x": 0.07, "y": 0.40, "color": "#818cf8"},
    "Blues":      {"x": 0.30, "y": 0.33, "color": "#1d4ed8"},
    "Reggae":     {"x": 0.45, "y": 0.40, "color": "#84cc16"},
    "Indie":      {"x": 0.35, "y": 0.67, "color": "#2dd4bf"},
}

GENRES = list(GENRE_POLES.keys())

# ── Gemini prompt ──────────────────────────────────────────────────────────────

_SYSTEM = """\
You are a music genre analysis engine. Output ONLY valid JSON — no preamble, no markdown.

Given lyrics and metadata, return genre percentage weights. Rules:
• All 14 keys must be present; values are integers; they must sum to exactly 100.
• The tagged genre carries weight but production, vocal style, and lyrical themes matter too.
• Most songs have 2–4 dominant genres; the rest near 0.

Output exactly:
{"Pop":0,"Rock":0,"Hip-Hop":0,"R&B":0,"Soul":0,"Electronic":0,"Country":0,"Folk":0,"Jazz":0,"Metal":0,"Classical":0,"Blues":0,"Reggae":0,"Indie":0}\
"""

_model: Optional[genai.GenerativeModel] = None


def _get_model() -> genai.GenerativeModel:
    global _model
    if _model is None:
        genai.configure(api_key=os.environ["GOOGLE_API_KEY"])
        _model = genai.GenerativeModel(
            model_name="gemini-3.1-flash-lite",
            system_instruction=_SYSTEM,
        )
    return _model


# ── SQLite cache ───────────────────────────────────────────────────────────────

DB_PATH = Path("data/hint_cache.db")
_db: Optional[sqlite3.Connection] = None


def _get_db() -> sqlite3.Connection:
    global _db
    if _db is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("""
            CREATE TABLE IF NOT EXISTS genre_cache (
                cache_key TEXT PRIMARY KEY,
                weights   TEXT NOT NULL
            )
        """)
        _db.commit()
    return _db


def _cache_key(title: str, artist: str) -> str:
    return f"genre:{title.lower()}:{artist.lower()}"


def _get_cached(title: str, artist: str) -> Optional[dict]:
    row = _get_db().execute(
        "SELECT weights FROM genre_cache WHERE cache_key = ?",
        (_cache_key(title, artist),),
    ).fetchone()
    return json.loads(row[0]) if row else None


def _store(title: str, artist: str, weights: dict) -> None:
    _get_db().execute(
        "INSERT OR REPLACE INTO genre_cache (cache_key, weights) VALUES (?,?)",
        (_cache_key(title, artist), json.dumps(weights)),
    )
    _get_db().commit()


# ── Public API ─────────────────────────────────────────────────────────────────

def analyze_genres(song: SongData) -> dict[str, float]:
    """Return genre weights (sum=100) for a song, cached after first call."""
    cached = _get_cached(song.title, song.artist)
    if cached:
        return cached

    model = _get_model()
    prompt = (
        f"Title: {song.title}\nArtist: {song.artist}\n"
        f"Year: {song.year or 'Unknown'}\nTagged genre: {song.genre or 'Unknown'}\n\n"
        f"Lyrics:\n\"\"\"\n{song.lyrics[:2500]}\n\"\"\""
    )
    resp = model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(max_output_tokens=200),
    )
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", resp.text.strip(), flags=re.IGNORECASE)
    try:
        weights = json.loads(raw)
        total = sum(weights.get(g, 0) for g in GENRES)
        if total <= 0:
            raise ValueError("zero total")
        weights = {g: round(weights.get(g, 0) * 100 / total) for g in GENRES}
        # Fix rounding to ensure exact sum of 100
        diff = 100 - sum(weights.values())
        if diff:
            top = max(weights, key=weights.get)
            weights[top] += diff
    except Exception:
        weights = {g: (100 if g == "Pop" else 0) for g in GENRES}

    _store(song.title, song.artist, weights)
    return weights


def calculate_position(weights: dict[str, float]) -> tuple[float, float]:
    """Weighted centroid of genre poles → (x, y) in [0,1]."""
    total = sum(weights.values()) or 1
    x = sum(GENRE_POLES[g]["x"] * weights.get(g, 0) / total for g in GENRES)
    y = sum(GENRE_POLES[g]["y"] * weights.get(g, 0) / total for g in GENRES)
    return round(x, 4), round(y, 4)


def temperature(dist: float) -> str:
    if dist > 0.40: return "cold"
    if dist > 0.25: return "cool"
    if dist > 0.15: return "warm"
    if dist > 0.07: return "hot"
    return "burning"
