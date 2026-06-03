"""
Central song database backed by SQLite + FTS5.
Sources tried in order:
  1. CSV file at DATASET_PATH (Kaggle or custom, with or without lyrics)
  2. Last.fm API  (requires LASTFM_API_KEY env var)
  3. Built-in TOP_500 fallback list

Lyrics are stored in the DB when available (CSV with lyrics column).
For songs without lyrics, they are fetched from lyrics.ovh on first use and cached.
"""
import asyncio
import hashlib
import logging
import os
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional

import httpx

from .models import SongData
from .lyrics import fetch_lyrics

log = logging.getLogger("song_db")

DB_PATH = Path("data/songs.db")
LASTFM    = "https://ws.audioscrobbler.com/2.0/"
LASTFM_TAGS = [
    "rock", "pop", "hip-hop", "r-n-b", "electronic", "country",
    "folk", "jazz", "metal", "classical", "blues", "reggae", "indie", "soul",
]

# Recognised CSV column names (first match wins)
_COL = {
    "title":  ["title", "song", "song_title", "name", "track_name", "track"],
    "artist": ["artist", "artist_name", "singer", "performer", "artists"],
    "lyrics": ["lyrics", "text", "lyric", "lyrics_text"],
    "year":   ["year", "release_year", "date"],
    "genre":  ["genre", "tag", "tags", "genre_tags"],
}


# ── Database ───────────────────────────────────────────────────────────────────

_db: Optional[sqlite3.Connection] = None
_count = 0


def _get_db() -> sqlite3.Connection:
    global _db
    if _db is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("PRAGMA cache_size=-32000")
        _db.executescript("""
            CREATE TABLE IF NOT EXISTS songs (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                title   TEXT NOT NULL,
                artist  TEXT NOT NULL,
                year    INTEGER,
                genre   TEXT,
                lyrics  TEXT,
                UNIQUE(title COLLATE NOCASE, artist COLLATE NOCASE)
            );
            CREATE INDEX IF NOT EXISTS idx_artist ON songs(artist COLLATE NOCASE);
            CREATE VIRTUAL TABLE IF NOT EXISTS songs_fts USING fts5(
                title, artist,
                content='songs', content_rowid='id',
                tokenize='unicode61'
            );
        """)
        _db.commit()
    return _db


def _refresh_count() -> int:
    global _count
    _count = _get_db().execute("SELECT COUNT(*) FROM songs").fetchone()[0]
    return _count


def _rebuild_fts():
    _get_db().execute("INSERT INTO songs_fts(songs_fts) VALUES('rebuild')")
    _get_db().commit()


def _batch_insert(rows: list[tuple]):
    """rows = (title, artist, year, genre, lyrics)"""
    _get_db().executemany(
        "INSERT OR IGNORE INTO songs (title, artist, year, genre, lyrics) VALUES (?,?,?,?,?)",
        rows,
    )
    _get_db().commit()


# ── Initialisation ─────────────────────────────────────────────────────────────

async def initialize():
    """Called once at startup. Populates the DB if empty."""
    if _refresh_count() > 0:
        log.info("Song DB: %d songs ready", _count)
        return

    csv_path = Path(os.getenv("DATASET_PATH", "data/songs.csv"))
    if csv_path.exists():
        await asyncio.get_event_loop().run_in_executor(None, _import_csv, csv_path)
        _rebuild_fts()
        log.info("CSV import complete: %d songs", _refresh_count())
        return

    api_key = os.getenv("LASTFM_API_KEY", "")
    if api_key:
        await _seed_from_lastfm(api_key)
        _rebuild_fts()
        log.info("Last.fm seed complete: %d songs", _refresh_count())
        return

    log.info("No CSV or Last.fm key — using built-in song list")
    _seed_builtin()
    _rebuild_fts()
    log.info("Built-in seed: %d songs", _refresh_count())


def _find_col(df_cols: list[str], aliases: list[str]) -> Optional[str]:
    for a in aliases:
        if a in df_cols:
            return a
    return None


def _import_csv(path: Path):
    try:
        import pandas as pd
    except ImportError:
        log.error("pandas not installed — cannot import CSV")
        return

    log.info("Importing %s …", path)
    df = pd.read_csv(path, low_memory=False)
    df.columns = [c.strip().lower() for c in df.columns]

    tc = _find_col(list(df.columns), _COL["title"])
    ac = _find_col(list(df.columns), _COL["artist"])
    if not tc or not ac:
        log.error("CSV has no title/artist column. Columns found: %s", list(df.columns))
        return

    lc = _find_col(list(df.columns), _COL["lyrics"])
    yc = _find_col(list(df.columns), _COL["year"])
    gc = _find_col(list(df.columns), _COL["genre"])

    batch, n = [], 0
    for _, row in df.iterrows():
        title  = str(row[tc]).strip() if pd.notna(row.get(tc)) else None
        artist = str(row[ac]).strip() if pd.notna(row.get(ac)) else None
        if not title or not artist or title == "nan" or artist == "nan":
            continue
        lyrics = str(row[lc]).strip() if lc and pd.notna(row.get(lc)) and len(str(row.get(lc, ""))) > 80 else None
        year   = int(row[yc]) if yc and pd.notna(row.get(yc)) else None
        genre  = str(row[gc]).strip() if gc and pd.notna(row.get(gc)) else None
        batch.append((title, artist, year, genre, lyrics))
        n += 1
        if len(batch) >= 5000:
            _batch_insert(batch)
            batch = []
            log.info("  … %d rows processed", n)

    if batch:
        _batch_insert(batch)
    log.info("Imported %d rows", n)


async def _seed_from_lastfm(api_key: str):
    log.info("Fetching songs from Last.fm …")
    rows: list[tuple] = []

    async def fetch_tracks(params: dict):
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(LASTFM, params={**params, "api_key": api_key, "format": "json"})
                return r.json()
        except Exception:
            return {}

    # Global chart — 5 pages × 200
    for page in range(1, 6):
        data = await fetch_tracks({"method": "chart.gettoptracks", "limit": 200, "page": page})
        for t in data.get("tracks", {}).get("track", []):
            rows.append((t["name"], t["artist"]["name"], None, None, None))

    # Per-genre top tracks — 14 tags × 3 pages × 200
    tasks = []
    for tag in LASTFM_TAGS:
        for page in range(1, 4):
            tasks.append(fetch_tracks({"method": "tag.gettoptracks", "tag": tag, "limit": 200, "page": page}))
    results = await asyncio.gather(*tasks)
    for data in results:
        for t in data.get("tracks", {}).get("track", []):
            rows.append((t["name"], t["artist"]["name"], None, None, None))

    _batch_insert(rows)
    log.info("Last.fm: inserted up to %d tracks", len(rows))


def _seed_builtin():
    from .top_songs import TOP_500
    _batch_insert([(t, a, None, None, None) for t, a in TOP_500])


# ── Lyrics (lazy fetch + cache) ────────────────────────────────────────────────

async def ensure_lyrics(song_id: int, title: str, artist: str,
                         genius_token: Optional[str] = None) -> Optional[str]:
    """Return lyrics from DB or fetch and cache them."""
    row = _get_db().execute("SELECT lyrics FROM songs WHERE id=?", (song_id,)).fetchone()
    if row and row[0] and len(row[0]) > 100:
        return row[0]
    lyrics = await fetch_lyrics(title, artist, genius_token=genius_token)
    if lyrics:
        _get_db().execute("UPDATE songs SET lyrics=? WHERE id=?", (lyrics, song_id))
        _get_db().commit()
    return lyrics


# ── Public API ─────────────────────────────────────────────────────────────────

def search(query: str, limit: int = 12) -> list[dict]:
    if len(query.strip()) < 2:
        return []
    db = _get_db()
    try:
        rows = db.execute(
            """SELECT s.title, s.artist FROM songs_fts f
               JOIN songs s ON s.id = f.rowid
               WHERE songs_fts MATCH ? LIMIT ?""",
            (query.strip() + "*", limit),
        ).fetchall()
        if rows:
            return [{"title": r[0], "artist": r[1]} for r in rows]
    except Exception:
        pass
    q = f"%{query}%"
    rows = db.execute(
        "SELECT title, artist FROM songs WHERE title LIKE ? OR artist LIKE ? LIMIT ?",
        (q, q, limit),
    ).fetchall()
    return [{"title": r[0], "artist": r[1]} for r in rows]


async def get_song(title: str, artist: str,
                    genius_token: Optional[str] = None) -> Optional[SongData]:
    """Return a SongData with lyrics, fetching lyrics if missing."""
    db = _get_db()
    row = db.execute(
        "SELECT id, title, artist, year, genre, lyrics FROM songs "
        "WHERE title COLLATE NOCASE = ? AND artist COLLATE NOCASE = ?",
        (title, artist),
    ).fetchone()
    if not row:
        # partial match fallback
        row = db.execute(
            "SELECT id, title, artist, year, genre, lyrics FROM songs "
            "WHERE title COLLATE NOCASE LIKE ? AND artist COLLATE NOCASE LIKE ? LIMIT 1",
            (f"%{title}%", f"%{artist}%"),
        ).fetchone()
    if not row:
        return None

    sid, rtitle, rartist, ryear, rgenre, rlyrics = row
    if not rlyrics or len(rlyrics) < 100:
        rlyrics = await ensure_lyrics(sid, rtitle, rartist, genius_token=genius_token)
    if not rlyrics:
        return None
    return SongData(id=sid, title=rtitle, artist=rartist,
                    lyrics=rlyrics, year=ryear, genre=rgenre)


async def pick_random(genius_token: Optional[str] = None) -> Optional[SongData]:
    row = _get_db().execute("SELECT title, artist FROM songs ORDER BY RANDOM() LIMIT 1").fetchone()
    if not row:
        return None
    return await get_song(row[0], row[1], genius_token=genius_token)


async def pick_daily(genius_token: Optional[str] = None) -> Optional[SongData]:
    n = _count or 1
    seed = int(hashlib.md5(date.today().isoformat().encode()).hexdigest(), 16)
    row = _get_db().execute(
        "SELECT title, artist FROM songs LIMIT 1 OFFSET ?", (seed % n,)
    ).fetchone()
    if not row:
        return None
    return await get_song(row[0], row[1], genius_token=genius_token)


def total() -> int:
    return _count


def is_large() -> bool:
    """True when the DB is too big to send all songs to the frontend at once."""
    return _count > 5000
