"""Pydantic contracts shared by the API and the pipeline modules."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Platform = Literal["youtube", "spotify"]
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


class Weights(BaseModel):
    bpm: float = 0
    key: float = 0
    mood: float = 0
    energy: float = 0
    genre: float = 0


class RankRequest(BaseModel):
    tracks: list[Track]
    mode: Mode = "daily"
    weights: Weights | None = None


class RankResponse(BaseModel):
    ordered_ids: list[str]
    score_before: float
    score_after: float
    kept_original: bool = False
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
