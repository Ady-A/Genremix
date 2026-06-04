from pydantic import BaseModel
from typing import Optional


class SongData(BaseModel):
    id: int
    title: str
    artist: str
    lyrics: Optional[str] = None
    year: Optional[int] = None
    genre: Optional[str] = None


class JargonTranslation(BaseModel):
    original_line: str
    translation: str
    jargon_type: str


class HintBundle(BaseModel):
    matrix_breakdown: str
    jargon_translation: JargonTranslation
    biographical_riddle: str
    obfuscated_snippet: str
