# Genremix

A multiplayer song-guessing game built on a 2D genre map. A secret song is placed on the map based on its lyrical DNA — analyzed by Gemini AI. Guess songs to place them near the target and close in using temperature hints.

![Game Modes](https://img.shields.io/badge/modes-Solo%20%7C%20Daily%20%7C%20Multiplayer-purple)
![Stack](https://img.shields.io/badge/stack-FastAPI%20%2B%20Vanilla%20JS-blue)

---

## How It Works

1. A secret song is picked from the dataset and its lyrics are fed to Gemini, which outputs percentage weights across 14 genre poles (Rock, Hip-Hop, Pop, Jazz, etc.)
2. The song's position on the map is the weighted centroid of those poles
3. You guess songs — each guess is analyzed the same way and placed on the map
4. Temperature feedback (❄️ Cold → 💥 Burning) tells you how close your guess landed to the secret
5. Find the secret song to win

### Game Modes

| Mode | Description |
|---|---|
| **Daily Challenge** | Same secret song for everyone on a given day (deterministic seed) |
| **Random Song** | Random song from the dataset, unlimited guesses |
| **Multiplayer** | 2–6 players, shared map, 60-second timer — closest guess wins |

---

## Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI + Uvicorn |
| AI | Google Gemini (`gemini-3.1-flash-lite`) |
| Data | SQLite + FTS5 (57k songs from Spotify dataset) |
| Lyrics | lyrics.ovh (with Genius scrape fallback) |
| Realtime | WebSockets (multiplayer) |
| Frontend | Vanilla JS + Tailwind CSS (single HTML file) |

---

## Deployment (Render)

1. Connect the GitHub repo in your [Render](https://render.com) dashboard
2. Render auto-detects `render.yaml` — the build and start commands are pre-configured
3. Add environment variables in the Render dashboard:
   - `GOOGLE_API_KEY` ← required
   - `GENIUS_ACCESS_TOKEN` ← optional
   - `LASTFM_API_KEY` ← optional

Render runs FastAPI as a persistent process so WebSockets (multiplayer) work out of the box.

---

## Local Setup

### 1. Clone & install

```bash
git clone git@github.com:Ady-A/Genremix.git
cd Genremix
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# fill in GOOGLE_API_KEY at minimum
```

### 3. Add a song dataset (optional)

Works out of the box with a built-in list of ~500 songs. For the full 57k-song experience, download the [Spotify Million Song Dataset](https://www.kaggle.com/datasets/notshrirang/spotify-million-song-dataset) from Kaggle and place it at `data/songs.csv`.

### 4. Run

```bash
./run.sh
```

Open `http://localhost:8000` in your browser.

On first run with a large CSV the import takes ~30 seconds. Subsequent starts are instant (data is cached in SQLite).

---

## Project Structure

```
├── backend/
│   ├── main.py          # FastAPI app, all REST + WebSocket routes
│   ├── genre_map.py     # Genre poles, Gemini analysis, position calculation
│   ├── multiplayer.py   # WebSocket queue, sessions, 60s timer
│   ├── song_db.py       # SQLite + FTS5 song database, lazy lyrics fetching
│   ├── ai_engine.py     # Hint generation (style matrix, jargon, riddle, snippet)
│   ├── lyrics.py        # lyrics.ovh + Genius fallback
│   ├── cache.py         # SQLite hint cache (keyed by title:artist)
│   └── top_songs.py     # Built-in ~500 song fallback list
├── frontend/
│   └── index.html       # Full SPA — map canvas, search, multiplayer UI
├── data/                # CSV dataset + SQLite databases (gitignored)
├── .env.example
├── requirements.txt
└── run.sh
```

---

## Genre Map

14 genre poles are fixed at coordinates on a unit square. Each song's position is computed as:

```
position = Σ (genre_weight × pole_coordinates)
```

Gemini analyzes the song's lyrics and outputs a percentage for each genre (summing to 100). The result is a weighted centroid — a pop-rock crossover lands between the Pop and Rock poles, not on either.

### Temperature Scale

| Temp | Distance |
|---|---|
| 💥 Burning | < 7% of map |
| 🔥 Hot | 7–15% |
| 🌡️ Warm | 15–25% |
| 🌊 Cool | 25–40% |
| ❄️ Cold | > 40% |

---

## Multiplayer

Players connect via WebSocket at `/ws/multiplayer`. The server:
- Queues players until 2+ are waiting
- Picks a random song, runs Gemini analysis, sends `secret_pos` to all players
- Broadcasts each guess's map position and temperature to all players (distance number hidden from opponents)
- After 60 seconds, sends `game_over` with a ranked leaderboard

Up to 6 players per session. Each player gets a unique color on the shared map.
