export type Platform = "youtube" | "spotify";
export type Mode = "daily" | "dj" | "emotional" | "custom";

export type FeatureName = "bpm" | "key" | "mood" | "energy" | "genre";

export interface Features {
  bpm: number | null;
  bpm_alt: number | null;
  bpm_confidence: number | null;
  camelot: string | null;
  key: string | null;
  key_confidence: number | null;
  mood: string[] | null;
  valence: number | null;
  energy: number | null;
  genre: string[] | null;
  sources: Partial<Record<FeatureName, string>>;
  status: Partial<Record<FeatureName, "ok" | "derived" | "local" | "missing" | "error">>;
  missing_features: FeatureName[];
}

export interface Health {
  ok: boolean;
  ffmpeg: boolean;
  ffprobe: boolean;
  feature_providers: Record<string, string | null>;
}

export interface Track {
  occurrence_id: string;
  platform: Platform;
  source_id: string;
  title: string;
  artist: string;
  duration_sec: number | null;
  original_position: number;
  features: Features;
  youtube_video_id: string | null;
  match_status: string;
  source_url: string | null;
}

export interface ImportResponse {
  playlist_title: string | null;
  platform: Platform;
  tracks: Track[];
  warnings: string[];
  api_usage: Record<string, { calls: number; cached: number }>;
}

export interface Weights {
  bpm: number;
  key: number;
  mood: number;
  energy: number;
  genre: number;
}

export interface TransitionStep {
  bpm: number | null;
  key: number | null;
  mood: number | null;
  energy: number | null;
  genre: number | null;
  total: number;
}

export type CurveFeature = "bpm" | "energy" | "valence";

/** User-drawn target: values in the feature's units, evenly spaced over the order. */
export interface TargetCurve {
  feature: CurveFeature;
  points: number[];
  strength: number; // 0 = smooth transitions only … 1 = fit the curve only
}

export interface RankResponse {
  ordered_ids: string[];
  score_before: number;
  score_after: number;
  kept_original: boolean;
  curve_error_before: number | null;
  curve_error_after: number | null;
  transition_costs: number[];
  transition_breakdown: { before: TransitionStep[]; after: TransitionStep[] };
}

export type TrackStage =
  | "waiting" | "matching" | "downloading" | "analyzing" | "ready" | "unmatched" | "failed" | "skipped";

export interface MixTrackProgress {
  occurrence_id: string;
  title: string;
  artist: string;
  stage: TrackStage;
  message: string | null;
  youtube_video_id: string | null;
}

export interface MixPlan {
  segments: { id: string; start_sec: number; duration_sec: number; gain_db: number; bpm: number | null }[];
  transitions: { from: string; to: string; type: "beat_crossfade" | "safe_fade"; duration_sec: number }[];
  total_duration_sec: number;
}

export interface MixStatus {
  mix_id: string;
  status: "processing" | "done" | "failed";
  stage: "queued" | "sourcing" | "arranging" | "rendering" | "done" | "failed";
  audio_url: string | null;
  duration_sec: number | null;
  warnings: string[];
  error: string | null;
  tracks: MixTrackProgress[];
  plan: MixPlan | null;
  feature_updates: Record<string, Features>;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    throw new Error("Sunucuya ulaşılamadı. Backend çalışıyor mu?");
  }
  if (!res.ok) {
    let detail = `İstek başarısız (HTTP ${res.status})`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* not json */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  importPlaylist: (url: string) =>
    request<ImportResponse>("/api/import", { method: "POST", body: JSON.stringify({ url }) }),
  rank: (tracks: Track[], mode: Mode, weights?: Weights, target?: TargetCurve) =>
    request<RankResponse>("/api/rank", { method: "POST", body: JSON.stringify({ tracks, mode, weights, target }) }),
  startMix: (tracks: Track[], ordered_ids: string[]) =>
    request<{ mix_id: string; status: string }>("/api/mix", {
      method: "POST",
      body: JSON.stringify({ tracks, ordered_ids }),
    }),
  mixStatus: (id: string) => request<MixStatus>(`/api/mix/${id}`),
  health: () => request<Health>("/api/health"),
};
