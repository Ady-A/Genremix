"""
Fetches song lyrics from lyrics.ovh — free REST API, no key required.
Falls back to a direct Genius page scrape if lyrics.ovh returns nothing.
"""
import re
from typing import Optional

import httpx

LYRICS_OVH = "https://api.lyrics.ovh/v1/{artist}/{title}"
GENIUS_SEARCH = "https://api.genius.com/search"


def _clean(raw: str) -> str:
    """Strip metadata headers/footers and normalise whitespace."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    # Strip leading contributor/translation noise (e.g. "180 ContributorsTranslations...")
    text = re.sub(r"^\d+\s*Contributors?\s*\S.*?\n", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^Translations?.*?\n", "", text, flags=re.IGNORECASE)
    # Strip trailing embed footer
    text = re.sub(r"\d*Embed\b.*$", "", text.strip(), flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def _from_lyrics_ovh(title: str, artist: str, client: httpx.AsyncClient) -> Optional[str]:
    try:
        url = LYRICS_OVH.format(
            artist=artist.replace("/", " "),
            title=title.replace("/", " "),
        )
        resp = await client.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            lyrics = data.get("lyrics", "").strip()
            return _clean(lyrics) if lyrics else None
    except Exception:
        pass
    return None


async def _from_genius(title: str, artist: str, client: httpx.AsyncClient, token: str) -> Optional[str]:
    """Search Genius API for the URL, then scrape the lyrics page."""
    try:
        resp = await client.get(
            GENIUS_SEARCH,
            params={"q": f"{title} {artist}"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        hits = resp.json().get("response", {}).get("hits", [])
        if not hits:
            return None
        page_url = hits[0]["result"]["url"]

        page = await client.get(page_url, timeout=10, follow_redirects=True)
        if page.status_code != 200:
            return None

        # Lyrics live in <div data-lyrics-container="true"> tags
        blocks = re.findall(
            r'data-lyrics-container="true"[^>]*>(.*?)</div>',
            page.text,
            re.DOTALL,
        )
        if not blocks:
            return None
        combined = "\n".join(blocks)
        # Strip HTML tags, convert <br/> to newlines
        text = re.sub(r"<br\s*/?>", "\n", combined, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        return _clean(text) if text.strip() else None
    except Exception:
        pass
    return None


async def fetch_lyrics(title: str, artist: str, genius_token: Optional[str] = None) -> Optional[str]:
    """Return cleaned lyrics, trying lyrics.ovh then Genius scrape."""
    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; GenremixBot/1.0)"},
        follow_redirects=True,
    ) as client:
        lyrics = await _from_lyrics_ovh(title, artist, client)
        if lyrics and len(lyrics) > 100:
            return lyrics

        if genius_token:
            lyrics = await _from_genius(title, artist, client, genius_token)
            if lyrics and len(lyrics) > 100:
                return lyrics

    return None
