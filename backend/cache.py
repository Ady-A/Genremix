"""
SQLite hint cache keyed on "title:artist". Thread-safe via WAL mode.
"""
import json
import sqlite3
from pathlib import Path
from typing import Optional

from .models import HintBundle, JargonTranslation

DB_PATH = Path("data/hint_cache.db")


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS hints (
            cache_key TEXT PRIMARY KEY,
            bundle    TEXT NOT NULL
        )
    """)
    con.commit()
    return con


_db: sqlite3.Connection | None = None


def _get_db() -> sqlite3.Connection:
    global _db
    if _db is None:
        _db = _conn()
    return _db


def cache_key(title: str, artist: str) -> str:
    return f"{title.lower()}:{artist.lower()}"


def get_cached(title: str, artist: str) -> Optional[HintBundle]:
    row = _get_db().execute(
        "SELECT bundle FROM hints WHERE cache_key = ?", (cache_key(title, artist),)
    ).fetchone()
    if row is None:
        return None
    d = json.loads(row[0])
    jt = d["jargon_translation"]
    return HintBundle(
        matrix_breakdown=d["matrix_breakdown"],
        jargon_translation=JargonTranslation(**jt),
        biographical_riddle=d["biographical_riddle"],
        obfuscated_snippet=d["obfuscated_snippet"],
    )


def store(title: str, artist: str, bundle: HintBundle) -> None:
    db = _get_db()
    db.execute(
        "INSERT OR REPLACE INTO hints (cache_key, bundle) VALUES (?, ?)",
        (cache_key(title, artist), bundle.model_dump_json()),
    )
    db.commit()
