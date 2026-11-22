"""MoodFlow FastAPI app: import → rank → auto mix."""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
import warnings
from contextlib import asynccontextmanager
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import arrangement, audio_sources, mixing
from .features import FeatureService, build_feature_service, fill_from_downloaded_audio
from .importers import PlaylistImportError, import_playlist
from .ranking import rank
from .schemas import (
    ImportRequest, ImportResponse, MixRequest, MixStartResponse, MixStatus,
    MixTrackProgress, RankRequest, RankResponse, Track,
)

logging.basicConfig(level=os.getenv("MOODFLOW_LOG", "INFO"))
log = logging.getLogger("moodflow")
logging.getLogger("httpx").setLevel(logging.WARNING)
warnings.filterwarnings("ignore", category=UserWarning, module="librosa")
warnings.filterwarnings("ignore", module="numba")

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")  # server-side developer API keys; never sent to the frontend
TMP = Path(os.getenv("MOODFLOW_TMP", ROOT / "tmp"))
CACHE_DIR = TMP / "cache"
MIX_DIR = TMP / "mixes"
MIX_TTL_SEC = int(os.getenv("MOODFLOW_MIX_TTL", 3600))
CACHE_TTL_SEC = int(os.getenv("MOODFLOW_CACHE_TTL", 6 * 3600))
MAX_MIX_TRACKS = 6
DOWNLOAD_WORKERS = 3

@asynccontextmanager
async def lifespan(_app: FastAPI):
    TMP.mkdir(parents=True, exist_ok=True)
    cleanup_tmp()
    yield


app = FastAPI(title="MoodFlow", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# per-provider answers persist across restarts so re-imports don't spend API credits again
feature_service: FeatureService = build_feature_service(cache_file=CACHE_DIR / "provider_cache.json")
jobs: dict[str, MixStatus] = {}
jobs_lock = threading.Lock()
job_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mix")


# --------------------------------------------------------------------------- housekeeping

def cleanup_tmp() -> None:
    now = time.time()
    if MIX_DIR.is_dir():
        for d in MIX_DIR.iterdir():
            if now - d.stat().st_mtime > MIX_TTL_SEC:
                shutil.rmtree(d, ignore_errors=True)
                with jobs_lock:
                    jobs.pop(d.name, None)
    audio_cache = CACHE_DIR / "audio"
    if audio_cache.is_dir():
        for f in audio_cache.iterdir():
            if f.is_file() and now - f.stat().st_mtime > CACHE_TTL_SEC:
                f.unlink(missing_ok=True)




# --------------------------------------------------------------------------- import / rank

@app.post("/api/import", response_model=ImportResponse)
def api_import(req: ImportRequest) -> ImportResponse:
    try:
        result = import_playlist(req.url)
    except PlaylistImportError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    warnings = list(result.warnings)
    warnings += feature_service.enrich(result.tracks)
    return ImportResponse(playlist_title=result.title, platform=result.platform,
                          tracks=result.tracks, warnings=warnings, api_usage=feature_service.last_usage)


@app.post("/api/rank", response_model=RankResponse)
def api_rank(req: RankRequest) -> RankResponse:
    if not req.tracks:
        raise HTTPException(status_code=422, detail="Sıralanacak şarkı yok.")
    return RankResponse(**rank(req.tracks, req.mode, req.weights))


# --------------------------------------------------------------------------- mix pipeline

def _update(mix_id: str, **changes) -> None:
    with jobs_lock:
        job = jobs[mix_id]
        jobs[mix_id] = job.model_copy(update=changes)


def _set_track(mix_id: str, oid: str, stage: str, message: str | None = None, video_id: str | None = None) -> None:
    with jobs_lock:
        job = jobs[mix_id]
        for t in job.tracks:
            if t.occurrence_id == oid:
                t.stage = stage  # type: ignore[assignment]
                t.message = message
                if video_id:
                    t.youtube_video_id = video_id


def _source_track(mix_id: str, track: Track) -> tuple[Path, arrangement.TrackAnalysis] | None:
    """Match → download → analyse one track. Returns None (with progress updated) on failure."""
    oid = track.occurrence_id
    video_id = track.youtube_video_id
    if not video_id:
        _set_track(mix_id, oid, "matching")
        video_id, score = audio_sources.find_youtube_match(track)
        if not video_id:
            _set_track(mix_id, oid, "unmatched", f"Güvenilir YouTube eşleşmesi yok (skor {score:.2f}).")
            return None
    _set_track(mix_id, oid, "downloading", video_id=video_id)
    try:
        wav = audio_sources.fetch_audio(video_id, CACHE_DIR / "audio")
    except audio_sources.AudioSourceError as err:
        _set_track(mix_id, oid, "failed", str(err))
        return None
    _set_track(mix_id, oid, "analyzing")
    try:
        analysis = arrangement.analyze_track(wav)
    except arrangement.AnalysisError as err:
        _set_track(mix_id, oid, "failed", f"Bozuk/uygunsuz ses: {err}")
        return None
    meta_bpm = track.features.bpm
    if meta_bpm and analysis.bpm and not arrangement.bpm_close(meta_bpm, analysis.bpm, tol=0.08):
        _set_track(mix_id, oid, "ready",
                   f"İndirilen kayıt {analysis.bpm:.0f} BPM ölçüldü (metadata {meta_bpm:.0f}); geçişlerde ölçülen değer kullanıldı.")
    else:
        _set_track(mix_id, oid, "ready")
    return wav, analysis


def run_mix_job(mix_id: str, ordered: list[Track]) -> None:
    try:
        _update(mix_id, stage="sourcing")
        ready: dict[int, tuple[Track, Path, arrangement.TrackAnalysis]] = {}
        pending: dict[Future, int] = {}
        next_idx = 0
        with ThreadPoolExecutor(DOWNLOAD_WORKERS, thread_name_prefix="dl") as pool:
            while True:
                # keep exactly as many in flight as we still need (failed ones get replaced)
                while next_idx < len(ordered) and len(ready) + len(pending) < MAX_MIX_TRACKS \
                        and len(pending) < DOWNLOAD_WORKERS:
                    pending[pool.submit(_source_track, mix_id, ordered[next_idx])] = next_idx
                    next_idx += 1
                if not pending:
                    break
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    idx = pending.pop(fut)
                    try:
                        res = fut.result()
                    except Exception as err:  # noqa: BLE001
                        log.exception("sourcing crashed")
                        _set_track(mix_id, ordered[idx].occurrence_id, "failed", str(err))
                        res = None
                    if res:
                        ready[idx] = (ordered[idx], *res)
        for t in ordered[next_idx:]:
            _set_track(mix_id, t.occurrence_id, "skipped", "Miks için gerekmedi.")

        chosen = [ready[i] for i in sorted(ready)]
        warnings = list(jobs[mix_id].warnings)
        for t in jobs[mix_id].tracks:
            if t.stage in ("unmatched", "failed", "ready") and t.message:
                warnings.append(f"{t.artist} – {t.title}: {t.message}")
        if len(chosen) < 2:
            raise RuntimeError("En az iki ses kaynağı edinilemedi; miks oluşturulamadı.")
        if len(chosen) < MAX_MIX_TRACKS:
            warnings.append(f"Yalnızca {len(chosen)} parça edinilebildi; miks daha kısa olacak.")

        updates = fill_from_downloaded_audio([(t, a.bpm, a.loudness_db) for t, _, a in chosen])
        _update(mix_id, stage="arranging", warnings=warnings, feature_updates=updates)
        plan = arrangement.build_plan([(t.occurrence_id, a) for t, _, a in chosen])

        _update(mix_id, stage="rendering", plan=plan)
        out = MIX_DIR / mix_id / "preview.mp3"
        mixing.render_mix(plan, {t.occurrence_id: wav for t, wav, _ in chosen}, out)
        duration = mixing.probe_duration(out)
        _update(mix_id, status="done", stage="done", audio_url=f"/api/audio/{mix_id}",
                duration_sec=round(duration, 1))
    except Exception as err:  # noqa: BLE001 — surface any failure to the UI
        log.exception("mix %s failed", mix_id)
        _update(mix_id, status="failed", stage="failed", error=str(err))


@app.post("/api/mix", response_model=MixStartResponse)
def api_mix(req: MixRequest) -> MixStartResponse:
    by_id = {t.occurrence_id: t for t in req.tracks}
    ordered = [by_id[i] for i in req.ordered_ids if i in by_id]
    if len(ordered) < 2:
        raise HTTPException(status_code=422, detail="Miks için en az iki şarkı gerekli.")

    # the same recording twice in a row of 6 is pointless; keep the first occurrence
    seen: set[str] = set()
    unique: list[Track] = []
    warnings: list[str] = []
    for t in ordered:
        key = t.youtube_video_id or f"{t.artist}|{t.title}".lower()
        if key in seen:
            warnings.append(f"{t.artist} – {t.title}: tekrar eden parça, miks içinde bir kez kullanıldı.")
            continue
        seen.add(key)
        unique.append(t)

    cleanup_tmp()
    mix_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[mix_id] = MixStatus(
            mix_id=mix_id, status="processing", warnings=warnings,
            tracks=[MixTrackProgress(occurrence_id=t.occurrence_id, title=t.title, artist=t.artist,
                                     youtube_video_id=t.youtube_video_id) for t in unique],
        )
    job_executor.submit(run_mix_job, mix_id, unique)
    return MixStartResponse(mix_id=mix_id, status="processing")


@app.get("/api/mix/{mix_id}", response_model=MixStatus)
def api_mix_status(mix_id: str) -> MixStatus:
    with jobs_lock:
        job = jobs.get(mix_id)
        if not job:
            raise HTTPException(status_code=404, detail="Miks bulunamadı veya süresi doldu.")
        return job.model_copy(deep=True)


@app.get("/api/audio/{mix_id}")
def api_audio(mix_id: str) -> FileResponse:
    if not mix_id.isalnum():
        raise HTTPException(status_code=404)
    path = MIX_DIR / mix_id / "preview.mp3"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Ses dosyası bulunamadı.")
    return FileResponse(path, media_type="audio/mpeg", filename="moodflow-mix.mp3")


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        # provider → null when active, otherwise why it is skipped (keys themselves are never exposed)
        "feature_providers": feature_service.status(),
        "provider_cache": {"entries": len(feature_service.cache), "usage_since_start": feature_service.total_usage},
    }


# serve the built frontend if present (single command demo: `npm run build` then uvicorn)
_dist = ROOT / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
