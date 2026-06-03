import json
import os
import re

import google.generativeai as genai

from .models import SongData, HintBundle, JargonTranslation

_model: genai.GenerativeModel | None = None

SYSTEM_PROMPT = """\
You are a music analysis engine powering a puzzle game. Output ONLY valid JSON — no preamble, no markdown.

Given song metadata, produce exactly this object:
{
  "matrix_breakdown": "X% [Evocative Compound Theme], Y% [Evocative Compound Theme]",
  "jargon_translation": {
    "original_line": "<exact memorable lyric line>",
    "translation": "<that line rewritten in sterile jargon>",
    "jargon_type": "Corporate | Academic | Legal | Technical"
  },
  "biographical_riddle": "<2–3 sentence riddle using only era/genre/decade clues — NEVER include artist name or song title>"
}

Rules:
• matrix_breakdown — two thematic moods summing to 100%; use evocative compound noun phrases.
• jargon_translation — pick the most recognisable sing-along line; translate faithfully but kill the poetry.
• biographical_riddle — clues about era, sonic movement, cultural context. End with "Who am I?"\
"""

USER_TEMPLATE = """\
Title: {title}
Artist: {artist}
Year: {year}
Genre: {genre}

Lyrics:
\"\"\"
{lyrics}
\"\"\"\
"""

STOPWORDS = {
    "i","me","my","you","your","we","the","a","an","and","or","but","is","are",
    "was","were","be","been","have","has","had","do","does","did","will","would",
    "could","should","can","may","of","in","on","at","to","for","with","by","from",
    "that","this","it","he","she","they","all","so","if","not","no","what","when",
    "where","how","oh","just","like","as","up","out","get","got","let","go","know",
    "think","said","say","im","its","ive","dont","cant","wont","ill","thats","cause",
}


def _build_obfuscated_snippet(lyrics: str) -> str:
    words = re.findall(r"[a-zA-Z']+", lyrics.lower())
    freq: dict[str, int] = {}
    for w in words:
        clean = w.strip("'")
        if clean not in STOPWORDS and len(clean) > 2:
            freq[clean] = freq.get(clean, 0) + 1
    top = {w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:12]}

    lines = [l.strip() for l in lyrics.split("\n") if l.strip()]
    start = len(lines) // 3
    snippet = lines[start: start + 4] or lines[:4]

    def redact(m: re.Match) -> str:
        return "█" * len(m.group(0)) if m.group(0).lower().strip("'") in top else m.group(0)

    return "\n".join(re.sub(r"[a-zA-Z']+", redact, line) for line in snippet)


def _get_model() -> genai.GenerativeModel:
    global _model
    if _model is None:
        genai.configure(api_key=os.environ["GOOGLE_API_KEY"])
        _model = genai.GenerativeModel(
            model_name="gemini-3.1-flash-lite",
            system_instruction=SYSTEM_PROMPT,
        )
    return _model


def _extract_json(raw: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip())
    return json.loads(cleaned)


def generate_hints(song: SongData) -> HintBundle:
    model = _get_model()
    prompt = USER_TEMPLATE.format(
        title=song.title,
        artist=song.artist,
        year=song.year or "Unknown",
        genre=song.genre or "Unknown",
        lyrics=song.lyrics[:3000],
    )
    response = model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(max_output_tokens=600),
    )
    try:
        data = _extract_json(response.text)
    except (json.JSONDecodeError, IndexError) as exc:
        raise ValueError(f"LLM returned non-JSON: {response.text[:200]}") from exc

    jt = data["jargon_translation"]
    return HintBundle(
        matrix_breakdown=data["matrix_breakdown"],
        jargon_translation=JargonTranslation(
            original_line=jt["original_line"],
            translation=jt["translation"],
            jargon_type=jt.get("jargon_type", "Corporate"),
        ),
        biographical_riddle=data["biographical_riddle"],
        obfuscated_snippet=_build_obfuscated_snippet(song.lyrics),
    )
