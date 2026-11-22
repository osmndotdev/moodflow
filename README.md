# MoodFlow — mini demo

Paste a **public YouTube, Spotify or Apple Music playlist URL** → MoodFlow reads up to 50 tracks, extracts
**BPM, Key/Camelot, Mood, Energy, Genre**, re-orders them in one of four modes
(**Daily / DJ Mix / Emotional / Custom**) and, with **"Otomatik Miks Oluştur"**, fetches the audio of the
first 6 suitable tracks from YouTube with **yt-dlp**, picks the strongest sections, joins them with long
beat-locked crossfades (tempo-matched, kick-aligned) and renders a ~3 min MP3 you can play and scrub in the page.

No login, no OAuth, nothing is written back to YouTube/Spotify, no uploads.

## Requirements

- Python 3.11+ (tested on 3.12 and 3.14: the full test suite and a live import → rank → mix give identical
  results on both; every dependency installs from prebuilt wheels on 3.14), Node 18+ (tested on 24)
- `ffmpeg` and `ffprobe` on `PATH` (`brew install ffmpeg`)
- Recommended on servers: [Deno](https://deno.com) on `PATH`. yt-dlp uses a JavaScript runtime for some
  YouTube player checks; downloads currently also work without one, but YouTube changes often.
- Internet access (YouTube, Spotify embed page, feature APIs)
- **API keys (server-side, developer-owned; no end-user auth):** copy `.env.example` → `.env` and fill in
  - `FREQBLOG_API_KEY` — primary source for all five features. Free key (1,000 lookups/month, no card): https://freqblog.com/#pricing
  - `GETSONGBPM_API_KEY` (missing BPM/key) and `LASTFM_API_KEY` (missing genre) — optional fallbacks, skipped when empty.
    The page footer always carries the "BPM & key data by GetSongBPM" backlink its free tier requires.

  Without keys the app still runs: BPM and key come from ReccoBeats (keyless) where it knows the track and
  genre from MusicBrainz where it answers in time. Energy and mood stay empty before the mix, since v1.8
  forbids inventing them; missing BPM/energy is measured from the downloaded audio once a mix runs.

## Run

One-time setup (creates `.venv` if missing, installs the Python and frontend dependencies, builds the
frontend, creates `.env` from `.env.example` if there is none, and warns if ffmpeg is missing):

```bash
just setup
```

It uses `python3`; pick another interpreter with `just python=python3.14 setup`. Re-run it after
pulling changes. If [uv](https://docs.astral.sh/uv/) is installed it creates the environment and installs
with uv; otherwise it uses `venv` + pip, and repairs a `.venv` that has no pip. On Debian/Ubuntu the latter
needs `sudo apt install python3-venv` (or `python3.14-venv` for a versioned Python); setup says so if it's
missing. Without [just](https://github.com/casey/just), run the same steps by hand:
`python3 -m venv .venv`, `.venv/bin/pip install -r requirements.txt`, `npm --prefix frontend ci`,
`npm --prefix frontend run build`.

Dev mode, both servers in one terminal with auto-reload (Ctrl-C stops both):

```bash
just dev
```

(`./dev.sh` does the same without just. `just` alone lists the other recipes:
`setup`, `backend`, `frontend`, `test`.)

Open http://localhost:5173 (Vite proxies `/api` to the backend). The backend port is `MOODFLOW_PORT` in `.env` (default 8000).

- Frontend edits hot-reload in the browser (Vite).
- Backend edits to `backend/*.py` restart the API automatically. Only `backend/` is watched, so mix
  downloads in `tmp/` don't trigger restarts. A restart drops any mix in progress (jobs are in memory),
  and changes to `.env` still need a manual restart. Set `MOODFLOW_RELOAD=0` to turn reloading off.
- Separately: `.venv/bin/python -m backend` and `npm --prefix frontend run dev`.

Single-port mode (also how a server runs it): after `just setup`, start the backend alone with
`MOODFLOW_RELOAD=0` and open http://localhost:8000 (or your `MOODFLOW_PORT`); it serves the built page
and the API on that one port, so a reverse proxy only needs to forward everything to it.

Analytics: the VPS deployment uses [Microsoft Clarity](https://clarity.microsoft.com). Put
`MOODFLOW_CLARITY_ID=zhr0drdbro` in the **VPS** `.env` only and restart; the backend then adds the Clarity
tag to the page it serves (`backend/analytics.py`). With the variable empty (the default) no tag is sent,
so local dev (Vite on :5173) and local single-port runs collect nothing; the tag also skips itself when
the page is opened as `localhost` / `127.x` / `[::1]` (e.g. through an SSH tunnel).

Tests (offline, ~10 s; downloads and network are faked, audio is synthesized):

```bash
.venv/bin/python -m pytest -q
```

## How it works

| Step | Module | Notes |
|---|---|---|
| Import | `backend/importers.py` | Up to 50 items per playlist (`MOODFLOW_MAX_TRACKS`). YouTube: `yt_dlp.YoutubeDL(extract_flat=in_playlist)`. Spotify: `SpotifyPublicAdapter` reads the public **embed page** JSON (no API key / OAuth). Apple Music: `AppleMusicPublicAdapter` reads the public playlist page's `serialized-server-data` JSON (no login, no developer token; any storefront, catalog `pl.…` and shared user `pl.u-…` playlists). Spotify and Apple Music items are matched to YouTube for the mix, with their recording durations used in identity checks. Private / missing lists → clear error. |
| Features | `backend/features.py` + `backend/feature_providers.py` | Per-field priority, fallbacks only for fields still missing: **BPM** FreqBlog → ReccoBeats → GetSongBPM → (mix only) beat tracking on the downloaded recording · **Key** FreqBlog → ReccoBeats → GetSongBPM (Open Key → Camelot) · **Mood** FreqBlog, or coarse mood derived from FreqBlog valence+energy · **Energy** FreqBlog → (mix only) downloaded-audio loudness scaled among the mix tracks · **Genre** FreqBlog → Last.fm tags (non-genre tags filtered) → MusicBrainz artist tags (1 req/s, 15 s budget per import). ReccoBeats (keyless, not in plan v1.8) answers Spotify items in one batch by Spotify ID and other items via title search. Every hit is checked against artist/title/version, plus ±20 s duration for Spotify and Apple Music items (YouTube lengths are video lengths, so they aren't compared); FreqBlog 202 is retried, 429 honours `Retry-After`. Each feature has a `sources` entry, a `status` (`ok` / `derived` / `local` / `missing` / `error` — error means a provider failed and the field is retried on the next import, since failed lookups aren't cached) and, where the provider gives one, a confidence (`bpm_confidence` on FreqBlog's own scale, `key_confidence` 0–1, kept apart). `missing_features` lists the gaps. The UI shows source, status and confidence on hover and marks local/derived values with ≈. Values measured during a mix come back as `feature_updates` in `GET /api/mix/{id}` and are shown in the table only; ranking keeps using provider data (§6.4: local analysis is for the mix). `GET /api/health` reports which providers are active. |
| Ranking | `backend/ranking.py` | `C(i,j) = Σ w_f·d_f(i,j)`, missing features re-normalise weights. 5 greedy starts + swap/relocate local search, scored incrementally (only what a move changes), so 50 tracks rank in well under 0.1 s even with a curve; if nothing beats the original order, the original is kept. Emotional mode adds a calm → peak → calm target curve (intensity is min-max scaled within the playlist). **Custom mode target curve:** on the chart's Energy / BPM / Valence view you can draw a target with 5 draggable points (presets: rising, falling, peak, valley, flat). The objective becomes `(1−s)·transition cost + s·mean |value − target(position)|`, both scaled by the playlist's range, with `s` the "Eğriye uyum" slider. A sorted-values-to-sorted-targets assignment (the exact optimum of the curve term alone) seeds the local search. `POST /api/rank` takes `target: {feature, points, strength}` and returns `curve_error_before/after` plus `curve` (the curve actually optimised: per-position targets and per-track values on a playlist-relative 0–1 scale). In Emotional mode the chart's Intensity view (`0.7·energy + 0.3·valence`) shows that arc read-only; Daily and DJ Mix have no target curve (they only optimise neighbouring transitions), and the chart says so. **Pinned first song:** `first_id` (the pin button on any order card) fixes that song at position 1 in every mode; only positions 2..n are optimised, and the "keep original" fallback becomes the original order with the pinned song moved to the front. |
| Audio | `backend/audio_sources.py` | yt-dlp Python API only. YouTube items use their `video_id`; Spotify and Apple Music items search `ytsearch3:<artist> - <track> audio`, score title/artist/duration/version tags (`live`, `cover`, `remix`, `slowed`…) and **never download a weak match**. WAV 44.1 kHz stereo, cached per video id, per-id lock, duration/filesize limits. |
| Arrangement | `backend/arrangement.py` | librosa RMS, beat and bass-onset tracking on the downloaded recording (if its BPM disagrees with the metadata, the measured value is used and a warning is shown). Target mix length ~170 s: each segment is the strongest ~25–45 s window (skips silence/intros, starts on a beat). **Beat-locked crossfade** when tempos are within 8 % (half/double time allowed; each song's tempo is a line fit through its beats, since librosa's overall estimate snaps to a grid 3–7 % coarse at fast tempos) and beats are steady: the overlap is a musical phrase of the outgoing song, 16 beats (32 for fast, 8 for slow tempos, 7–12 s), starting and ending exactly on its beats; the incoming segment is tempo-matched with a pitch-preserving stretch (≤ ±8 %) fitted over exactly the overlapping beats; both edges move up to ±2 beats onto the strongest bass hit (rough bar-start stand-in); a bass-band onset cross-correlation (~6 ms resolution) lines up the kicks, and the drift between the overlap's two halves corrects the stretch. Otherwise a 4 s **safe fade**. Per-segment gain toward a common RMS. Transitions carry `beats`, `tempo_change_pct` and `align_ms` (shown in the timeline tooltip). Measured on 58 real song pairs (rendered audio, two independent measures): typical beat offset ~19 ms, kick offset ~12 ms, ~81 % of transitions aligned within 30 ms. |
| Render | `backend/mixing.py` | One FFmpeg `filter_complex`: trim → `atempo` stretch (pitch preserved) → exact sample-length trim → gain → `acrossfade` chain (lengths in samples, so nothing drifts) → fade in/out → `alimiter` (-2 dBFS) → `preview.mp3`. |
| API | `backend/main.py` | `POST /api/import`, `POST /api/rank`, `POST /api/mix` (background thread), `GET /api/mix/{id}` (poll; per-track progress + plan JSON), `GET /api/audio/{id}`. Failed/unmatched/corrupt tracks are replaced by the next track in order; fewer than 2 sources → clear failure. |
| UI | `frontend/src/App.tsx`, `FlowChart.tsx`, `WeightBar.tsx` | Single page (icons: `react-icons`): a "Oku → Sırala → Mixle" strip before the first import, replaced during an import by an "analyzing" panel (large equalizer, sweeping bar, rotating step captions and an elapsed-seconds counter; the import reports no real progress, so there is no percentage) → URL box with the supported platforms' icons in front (the one matching the typed URL stays lit) + example playlists (YouTube / Spotify / Apple Music) → feature table (source, status and confidence on hover, ≈ for local values, API usage line, **"Eksik verili şarkıları yok say"** toggle (on by default) that leaves incomplete tracks out of ranking, chart and mix) → mode picker; Custom sets weights on **one bar split into five parts** (drag the boundaries, click a legend chip to switch a feature off, presets, plain-language summary) → chart (Energy / BPM / Valence per position or per-feature transition fit, original vs MoodFlow, Intensity view with the Emotional arc, draggable target curve with presets) → original vs MoodFlow order cards (labelled BPM / key / energy, a plain-language transition row between cards, pin button for the first song) → mix progress (pipeline steps, per-track stages) → player with "now playing" and a segment timeline you can **click or drag to seek** (←/→ ±5 s), with a playhead that highlights the current section. Motion is turned off under `prefers-reduced-motion`. |

### API cache (saves credits)

Every provider answer is cached per provider and track (artist + title + version tags, plus the
Spotify id for ReccoBeats) in `tmp/cache/provider_cache.json`, so a track's data is paid for once:

- **Hits** are reused for 30 days, **"not found" / mismatched** answers for 7 days (catalogues grow).
  Tune with `MOODFLOW_PROVIDER_CACHE_DAYS` / `MOODFLOW_PROVIDER_CACHE_NEGATIVE_DAYS`.
- **Never cached:** "try again later" outcomes: FreqBlog 202 (track still being analysed; re-polling it
  is free), rate limits, MusicBrainz's time budget, network errors and rejected keys. Those fields show
  status `error` and are retried on the next import.
- Each provider is cached separately: one failing provider never makes another one get called (and
  billed) again, and adding a key later only calls the new provider. Mode switches never call any API.
- The cache file holds only feature values, never API keys. Writes are atomic, and expired entries are
  pruned on save. Bump a provider's `cache_version` when its normalisation changes.
- Each import response carries `api_usage` (real requests vs. cache hits per provider), which the UI
  shows above the table; `GET /api/health` has the totals since start.

Delete `tmp/cache/provider_cache.json` to force fresh lookups.

Temporary files live in `tmp/` (mixes expire after 1 h, cached WAVs 48 h after their last use; see `.env.example`).
The provider cache is not touched by that cleanup.

## Known limits (demo scope)

- Without `FREQBLOG_API_KEY` there is no mood or energy data before the mix, and key comes only from
  ReccoBeats, which misses many new releases (8/20 on a current Top Hits playlist) and sometimes reports the
  relative minor/major.
- Locally measured energy is relative to the mix's tracks and isn't on FreqBlog's scale.
- ReccoBeats is an addition the v1.8 plan doesn't list; turn it off with `MOODFLOW_RECCOBEATS=0`.
- Apple Music page reading is unofficial in the same way; if Apple changes the page, the import shows an
  error. The official MusicKit API would need an Apple Developer account and a signed developer token.
- Spotify embed-page reading is unofficial and may break if Spotify changes the page; the app then shows
  an error instead of asking for OAuth. The YouTube flow works on its own.
- MusicBrainz often answers 503 (rate limit); genres it didn't return in time are retried on the next import.
- Up to 50 tracks per import, deliberately more than the plan's 20; a first import of a new 50-track
  playlist costs up to 50 FreqBlog lookups (free tier: 1,000/month; repeats come from the cache).
- The mix is ~3 min, deliberately longer than the plan's 60–100 s, to leave room for phrase-length transitions.
- Not every transition can be beat-locked: in real 6-track mixes of three test playlists about 2 in 3 DJ Mix
  transitions and 1 in 4 Daily ones are; the rest fall back to the 4 s fade. Causes: neighbours more than
  8 % apart in tempo (the ranking judges metadata tempos), songs whose tempo drifts near the cut, and songs
  where the beat tracker and the metadata disagree on the tempo.
- Beat-locking is measured, not perfect: ~16 % of beat crossfades are still audibly off (unusual rhythms,
  tempo gaps near the ±8 % stretch limit). There is no true downbeat detection (the strongest nearby bass
  hit stands in for bar starts) and no bass swap / EQ during the long overlaps, so basslines or vocals can
  clash even when the beats line up.
