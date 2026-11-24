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
from html import escape
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import arrangement, audio_sources, exporters, mixing
from .analytics import clarity_id, inject_clarity
from .features import FeatureService, build_feature_service, fill_from_downloaded_audio
from .importers import PlaylistImportError, import_playlist
from .ranking import rank
from .schemas import (
    ExportProvider, ExportRequest, ExportStatus, ImportRequest, ImportResponse, MixRequest,
    MixStartResponse, MixStatus, MixTrackProgress, RankRequest, RankResponse, Track,
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
CACHE_TTL_SEC = int(os.getenv("MOODFLOW_CACHE_TTL", 48 * 3600))  # since last use
# Auto mix (downloads songs with yt-dlp). On by default; MOODFLOW_MIX=0 turns it off, e.g. on a server
# whose IP YouTube blocks. Import, ranking, previews and playlist export keep working without it.
MIX_ENABLED = os.getenv("MOODFLOW_MIX", "1").strip().lower() not in ("0", "false", "no", "off")
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
# playlist export: OAuth tokens of connected accounts persist across reloads (0600 file in tmp/)
oauth = exporters.OAuthManager(exporters.load_configs(), exporters.ConnectionStore(TMP / "oauth" / "connections.json"))
export_service = exporters.ExportService(oauth)


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
    try:
        return RankResponse(**rank(req.tracks, req.mode, req.weights, req.target, req.first_id))
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err


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
    if not MIX_ENABLED:
        raise HTTPException(status_code=403, detail="Otomatik miks bu sunucuda kapalı.")
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


# --------------------------------------------------------------------------- playlist export

def _export_http_error(err: exporters.ExportError) -> HTTPException:
    return HTTPException(status_code=401 if isinstance(err, exporters.NotConnected) else 422, detail=str(err))


@app.get("/api/export/providers")
def api_export_providers() -> dict:
    return oauth.providers()


@app.post("/api/export/{provider}/connect")
def api_export_connect(provider: ExportProvider) -> dict:
    """Start OAuth: the page opens ``auth_url`` in a popup and polls ``state``."""
    try:
        return oauth.begin(provider)
    except exporters.ExportError as err:
        raise _export_http_error(err) from err


@app.get("/api/export/{provider}/callback", response_class=HTMLResponse)
def api_export_callback(provider: ExportProvider, state: str | None = None, code: str | None = None,
                        error: str | None = None) -> HTMLResponse:
    try:
        entry = oauth.complete(provider, state, code, error)
    except exporters.ExportError as err:
        entry = {"status": "failed", "error": str(err)}
    label = exporters.PROVIDER_LABEL[provider]
    if entry["status"] == "connected":
        title, text = f"{label} bağlandı", f"{entry.get('account') or 'Hesap'} bağlandı. Bu pencere kapanabilir."
    else:
        title, text = f"{label} bağlanamadı", entry.get("error") or "Bağlantı başarısız."
    # no tokens or ids in this page: the opener learns the result by polling its state
    page = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{escape(title)}</title>
<body style="font:16px system-ui,sans-serif;display:grid;place-items:center;min-height:90vh;text-align:center">
<div><h2>{escape(title)}</h2><p>{escape(text)}</p></div>
<script>try {{ window.opener && window.opener.postMessage("moodflow-oauth", "*"); }} catch (e) {{}}
setTimeout(function () {{ window.close(); }}, {1200 if entry["status"] == "connected" else 6000});</script>"""
    return HTMLResponse(page)


@app.get("/api/export/{provider}/connect/{state}")
def api_export_poll(provider: ExportProvider, state: str) -> dict:
    return oauth.poll(provider, state)


@app.get("/api/export/{provider}/account")
def api_export_account(provider: ExportProvider,
                       x_export_connection: str | None = Header(default=None)) -> dict:
    try:
        conn = oauth.connection(provider, x_export_connection)
    except exporters.ExportError as err:
        raise _export_http_error(err) from err
    return {"connected": True, "account": conn.get("account")}


@app.delete("/api/export/{provider}/account")
def api_export_disconnect(provider: ExportProvider,
                          x_export_connection: str | None = Header(default=None)) -> dict:
    oauth.disconnect(provider, x_export_connection)
    return {"connected": False}


@app.post("/api/export/{provider}", response_model=ExportStatus)
def api_export(provider: ExportProvider, req: ExportRequest,
               x_export_connection: str | None = Header(default=None)) -> ExportStatus:
    by_id = {t.occurrence_id: t for t in req.tracks}
    ordered = [by_id[i] for i in req.ordered_ids if i in by_id]
    try:
        return export_service.start(provider, x_export_connection, ordered, req.name, req.description, req.public)
    except exporters.ExportError as err:
        raise _export_http_error(err) from err


@app.get("/api/export/jobs/{export_id}", response_model=ExportStatus)
def api_export_status(export_id: str) -> ExportStatus:
    job = export_service.status(export_id)
    if not job:
        raise HTTPException(status_code=404, detail="Dışa aktarma bulunamadı.")
    return job


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "mix": MIX_ENABLED,  # the UI hides the auto mix when false
        # provider → null when active, otherwise why it is skipped (keys themselves are never exposed)
        "feature_providers": feature_service.status(),
        "export_providers": {p: c["configured"] for p, c in oauth.providers().items()},
        "provider_cache": {"entries": len(feature_service.cache), "usage_since_start": feature_service.total_usage},
    }


# serve the built frontend if present (single command demo: `npm run build` then uvicorn)
_dist = ROOT / "frontend" / "dist"
if not MIX_ENABLED:
    log.info("Auto mix is off (MOODFLOW_MIX=0)")
# Microsoft Clarity: only the VPS .env sets MOODFLOW_CLARITY_ID, so nothing else collects data
_raw_clarity = os.getenv("MOODFLOW_CLARITY_ID", "")
CLARITY_ID = clarity_id(_raw_clarity)
if _raw_clarity.strip() and not CLARITY_ID:
    log.warning("MOODFLOW_CLARITY_ID %r is not a valid Clarity project ID; analytics stay off", _raw_clarity)
if _dist.is_dir():
    if CLARITY_ID:
        log.info("Microsoft Clarity enabled (project %s)", CLARITY_ID)

        @app.get("/", include_in_schema=False)
        @app.get("/index.html", include_in_schema=False)
        def index_with_clarity() -> HTMLResponse:
            # read per request so a rebuilt frontend is picked up without a restart
            html = (_dist / "index.html").read_text(encoding="utf-8")
            return HTMLResponse(inject_clarity(html, CLARITY_ID), headers={"Cache-Control": "no-cache"})

    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
