# MoodFlow — mini demo

Paste a **public YouTube or Spotify playlist URL** → MoodFlow reads up to 20 tracks, extracts
**BPM, Key/Camelot, Mood, Energy, Genre**, re-orders them in one of four modes
(**Daily / DJ Mix / Emotional / Custom**) and, with **"Otomatik Miks Oluştur"**, fetches the audio of the
first 6 suitable tracks from YouTube with **yt-dlp**, picks beat-aligned sections, plans transitions and
renders a ~60–100 s MP3 you can play in the page.

No login, no OAuth, nothing is written back to YouTube/Spotify, no uploads.

## Requirements

- Python 3.11+ (tested on 3.12), Node 18+ (tested on 24)
- `ffmpeg` and `ffprobe` on `PATH` (`brew install ffmpeg`)
- Internet access (YouTube, Spotify embed page, feature APIs)
- **API keys (server-side, developer-owned; no end-user auth):** copy `.env.example` → `.env` and fill in
  - `FREQBLOG_API_KEY` — primary source for all five features. Free key (1,000 lookups/month, no card): https://freqblog.com/#pricing
  - `GETSONGBPM_API_KEY` (missing BPM/key) and `LASTFM_API_KEY` (missing genre) — optional fallbacks, skipped when empty

  Without keys the app still runs: BPM and key come from ReccoBeats (keyless) where it knows the track, a
  playlist-relative energy from local analysis, and genre from MusicBrainz where it answers in time. Mood stays
  empty, since v1.8 forbids inventing it.

## Run

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
npm --prefix frontend install
```

Dev mode (two terminals):

```bash
.venv/bin/uvicorn backend.main:app --port 8000
```

```bash
npm --prefix frontend run dev
```

Open http://localhost:5173 (Vite proxies `/api` to :8000).

Single-port mode: `npm --prefix frontend run build`, then start uvicorn alone and open http://localhost:8000.

Tests (offline, ~5 s; downloads and network are faked, audio is synthesized):

```bash
.venv/bin/python -m pytest -q
```

## How it works

| Step | Module | Notes |
|---|---|---|
| Import | `backend/importers.py` | YouTube: `yt_dlp.YoutubeDL(extract_flat=in_playlist)`, first 20 items. Spotify: `SpotifyPublicAdapter` reads the public **embed page** JSON (no API key / OAuth). Private / missing lists → clear error. |
| Features | `backend/features.py` + `backend/feature_providers.py` | Per-field priority, fallbacks only for fields still missing: **BPM** FreqBlog → ReccoBeats → GetSongBPM → local preview beat tracking · **Key** FreqBlog → ReccoBeats → GetSongBPM (Open Key → Camelot) · **Mood** FreqBlog, or coarse mood derived from FreqBlog valence+energy · **Energy** FreqBlog → local loudness scaled within the playlist · **Genre** FreqBlog → Last.fm tags (non-genre tags filtered) → MusicBrainz artist tags (1 req/s, 15 s budget per import). ReccoBeats (keyless, not in plan v1.8) answers Spotify items in one batch by Spotify ID and other items via title search. Every hit is checked against artist/title/version, plus ±20 s duration for Spotify items (YouTube lengths are video lengths, so they aren't compared); FreqBlog 202 is retried, 429 honours `Retry-After`. Each track carries `sources` (feature → provider) and `missing_features`; the UI shows the source on hover and marks local/derived values with ≈. Cached by artist+title+version in `tmp/cache/features.json`. `GET /api/health` reports which providers are active. |
| Ranking | `backend/ranking.py` | `C(i,j) = Σ w_f·d_f(i,j)`, missing features re-normalise weights. 5 greedy starts + swap/relocate local search; if nothing beats the original order, the original is kept. Emotional mode adds a calm → peak → calm target curve (intensity is min-max scaled within the playlist). |
| Audio | `backend/audio_sources.py` | yt-dlp Python API only. YouTube items use their `video_id`; Spotify items search `ytsearch3:<artist> - <track> audio`, score title/artist/duration/version tags (`live`, `cover`, `remix`, `slowed`…) and **never download a weak match**. WAV 44.1 kHz stereo, cached per video id, per-id lock, duration/filesize limits. |
| Arrangement | `backend/arrangement.py` | librosa RMS + beat tracking on the downloaded recording (if its BPM disagrees with the metadata, the measured value is used and a warning is shown); chooses the strongest ~15–20 s window (skips silence/intros, snaps to beats). BPM within 6 % and steady beats → 3–6 s **beat crossfade** (8/16/4 beats); otherwise 2 s **safe fade**. Per-segment gain toward a common RMS. |
| Render | `backend/mixing.py` | One FFmpeg `filter_complex`: trim → gain → `acrossfade` chain → fade in/out → `alimiter` (-2 dBFS) → `preview.mp3`. |
| API | `backend/main.py` | `POST /api/import`, `POST /api/rank`, `POST /api/mix` (background thread), `GET /api/mix/{id}` (poll; per-track progress + plan JSON), `GET /api/audio/{id}`. Failed/unmatched/corrupt tracks are replaced by the next track in order; fewer than 2 sources → clear failure. |
| UI | `frontend/src/App.tsx` | Single page: URL → feature table → mode picker (+ 5 sliders for Custom) → original vs MoodFlow order with energy curve and transition costs → mix progress → player + segment timeline. |

Temporary files live in `tmp/` (mixes expire after 1 h, cached WAVs after 6 h; see `.env.example`).

## Known limits (demo scope)

- Without `FREQBLOG_API_KEY` there is no mood data, and key comes only from ReccoBeats, which misses many
  new releases (8/20 on a current Top Hits playlist) and sometimes reports the relative minor/major.
- Local energy (keyless fallback) is relative to the playlist and isn't on FreqBlog's scale; mixing both
  sources in one playlist is approximate.
- The local fallback finds its 30 s preview through Deezer's public search. That's the only Deezer use.
- Spotify embed-page reading is unofficial and may break if Spotify changes the page; the app then shows
  an error instead of asking for OAuth. The YouTube flow works on its own.
- MusicBrainz often answers 503 (rate limit); genres it didn't return in time are retried on the next import.
- No tempo-stretching: the beat crossfade aligns on beat boundaries but doesn't time-stretch.
