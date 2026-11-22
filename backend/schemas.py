"""Pydantic contracts shared by the API and the pipeline modules."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Platform = Literal["youtube", "spotify", "apple"]
Mode = Literal["daily", "dj", "emotional", "custom"]
MatchStatus = Literal["pending", "matched", "unmatched", "failed"]


class Features(BaseModel):
    bpm: float | None = None
    bpm_alt: float | None = None
    bpm_confidence: float | None = None  # FreqBlog scale, not 0..1
    camelot: str | None = None
    key: str | None = None
    key_confidence: float | None = None  # 0..1
    mood: list[str] | None = None
    valence: float | None = None
    energy: float | None = None
    genre: list[str] | None = None
    sources: dict[str, str] = Field(default_factory=dict)  # feature → provider
    status: dict[str, str] = Field(default_factory=dict)  # feature → ok|derived|local|missing|error
    missing_features: list[str] = Field(default_factory=list)


class Track(BaseModel):
    occurrence_id: str
    platform: Platform
    source_id: str
    title: str
    artist: str
    duration_sec: float | None = None
    original_position: int
    features: Features = Field(default_factory=Features)
    youtube_video_id: str | None = None
    match_status: MatchStatus = "pending"
    source_url: str | None = None


class ImportRequest(BaseModel):
    url: str


class ImportResponse(BaseModel):
    playlist_title: str | None = None
    platform: Platform
    tracks: list[Track]
    warnings: list[str] = []
    # provider → {"calls": real API requests this import, "cached": answers reused from the cache}
    api_usage: dict[str, dict[str, int]] = {}


class Weights(BaseModel):
    bpm: float = 0
    key: float = 0
    mood: float = 0
    energy: float = 0
    genre: float = 0


class TargetCurve(BaseModel):
    """User-drawn target for Custom mode: values in the feature's own units (BPM, 0..1 energy/valence),
    as evenly spaced control points from the first to the last position of the order."""
    feature: Literal["bpm", "energy", "valence"]
    points: list[float] = Field(min_length=2, max_length=32)
    strength: float = Field(0.7, ge=0, le=1)  # 0 = only smooth transitions, 1 = only fit the curve


class RankRequest(BaseModel):
    tracks: list[Track]
    mode: Mode = "daily"
    weights: Weights | None = None
    target: TargetCurve | None = None  # used in Custom mode only
    first_id: str | None = None  # occurrence_id pinned as the first song (any mode)


class RankResponse(BaseModel):
    ordered_ids: list[str]
    score_before: float
    score_after: float
    kept_original: bool = False
    first_id: str | None = None
    curve_error_before: float | None = None
    curve_error_after: float | None = None
    # the curve used (Emotional arc or Custom target), playlist-relative 0..1:
    # {"feature", "share", "targets": [per position], "values": {occurrence_id: value}}
    curve: dict | None = None
    transition_costs: list[float] = []
    # per-step feature distances (0..1, null = not comparable) for the original and new order
    transition_breakdown: dict[str, list[dict[str, float | None]]] = {}


class MixRequest(BaseModel):
    tracks: list[Track]
    ordered_ids: list[str]


class MixStartResponse(BaseModel):
    mix_id: str
    status: str


MixStage = Literal["queued", "sourcing", "arranging", "rendering", "done", "failed"]
TrackStage = Literal[
    "waiting", "matching", "downloading", "analyzing", "ready",
    "unmatched", "failed", "skipped",
]


class MixTrackProgress(BaseModel):
    occurrence_id: str
    title: str
    artist: str
    stage: TrackStage = "waiting"
    message: str | None = None
    youtube_video_id: str | None = None


class MixStatus(BaseModel):
    mix_id: str
    status: Literal["processing", "done", "failed"]
    stage: MixStage = "queued"
    audio_url: str | None = None
    duration_sec: float | None = None
    warnings: list[str] = []
    error: str | None = None
    tracks: list[MixTrackProgress] = []
    plan: dict | None = None
    # features filled locally from the downloaded recordings (BPM / energy no provider had)
    feature_updates: dict[str, Features] = {}


# --------------------------------------------------------------------------- playlist export

ExportProvider = Literal["spotify", "youtube"]
ExportItemStatus = Literal["pending", "matching", "matched", "added", "not_found", "failed"]


class ExportRequest(BaseModel):
    tracks: list[Track]
    ordered_ids: list[str]
    name: str = Field("MoodFlow", max_length=150)
    description: str = Field("", max_length=300)
    public: bool = False


class ExportItem(BaseModel):
    occurrence_id: str
    title: str
    artist: str
    status: ExportItemStatus = "pending"
    target_id: str | None = None  # spotify:track:… URI or YouTube video id
    message: str | None = None


class ExportStatus(BaseModel):
    export_id: str
    provider: ExportProvider
    status: Literal["processing", "done", "failed"]
    stage: Literal["matching", "creating", "adding", "done", "failed"] = "matching"
    playlist_name: str
    playlist_url: str | None = None
    added: int = 0
    items: list[ExportItem] = []
    warnings: list[str] = []
    error: str | None = None
