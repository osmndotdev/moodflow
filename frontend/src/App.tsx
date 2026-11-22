import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type FeatureName,
  type Health,
  type ImportResponse,
  type MixPlan,
  type MixStatus,
  type Mode,
  type RankResponse,
  type Track,
  type TrackStage,
  type Weights,
} from "./api";

const EXAMPLES = [
  { label: "YouTube · 80s Synth-Pop", url: "https://www.youtube.com/playlist?list=PLGBuKfnErZlAKsNDquW0QoEYZle-NMmDL" },
  { label: "Spotify · Today's Top Hits", url: "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M" },
];

const MODES: { id: Mode; label: string; hint: string }[] = [
  { id: "daily", label: "Daily", hint: "Genel uyum ve tür sürekliliği" },
  { id: "dj", label: "DJ Mix", hint: "Tempo + harmonik geçişler" },
  { id: "emotional", label: "Emotional", hint: "Sakin → zirve → sakin" },
  { id: "custom", label: "Custom", hint: "Ağırlıkları sen seç" },
];

const FEATURE_LABELS: Record<keyof Weights, string> = {
  bpm: "BPM",
  key: "Key",
  mood: "Mood",
  energy: "Energy",
  genre: "Genre",
};

const STAGE_LABEL: Record<TrackStage, string> = {
  waiting: "Sırada",
  matching: "YouTube eşleşmesi",
  downloading: "İndiriliyor",
  analyzing: "Analiz",
  ready: "Hazır",
  unmatched: "Eşleşmedi",
  failed: "Atlandı",
  skipped: "Gerekmedi",
};

const PIPELINE = [
  { key: "sourcing", label: "İndirme" },
  { key: "analysis", label: "Ses analizi" },
  { key: "arranging", label: "Bölüm seçimi" },
  { key: "transitions", label: "Geçiş" },
  { key: "rendering", label: "Render" },
] as const;

const fmtTime = (s: number | null | undefined) => {
  if (s == null) return "—";
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.round(s % 60)).padStart(2, "0")}`;
};

/* ------------------------------------------------------------------ small pieces */

function Dash() {
  return <span className="muted">—</span>;
}

function EnergyBar({ value }: { value: number | null }) {
  if (value == null) return <Dash />;
  return (
    <span className="energy" title={value.toFixed(2)}>
      <span className="energy-fill" style={{ width: `${Math.round(value * 100)}%` }} />
    </span>
  );
}

function Chips({ items, kind }: { items: string[] | null; kind: "mood" | "genre" }) {
  if (!items?.length) return <Dash />;
  return (
    <span className="chips">
      {items.map((i) => (
        <span key={i} className={`chip chip-${kind}`}>
          {i}
        </span>
      ))}
    </span>
  );
}

function PlatformBadge({ platform }: { platform: "youtube" | "spotify" }) {
  return <span className={`badge badge-${platform}`}>{platform === "youtube" ? "YouTube" : "Spotify"}</span>;
}

const PROVIDER_LABEL: Record<string, string> = {
  freqblog: "FreqBlog",
  reccobeats: "ReccoBeats",
  getsongbpm: "GetSongBPM",
  lastfm: "Last.fm",
  musicbrainz: "MusicBrainz",
  local_preview: "Yerel analiz (önizleme)",
};

const isLocal = (src?: string) => !!src && (src.startsWith("local") || src.startsWith("derived"));

function Src({ track, name, children }: { track: Track; name: FeatureName; children: React.ReactNode }) {
  const src = track.features.sources?.[name];
  if (!src) return <>{children}</>;
  const label = PROVIDER_LABEL[src] ?? src;
  return (
    <span className={`src ${isLocal(src) ? "src-local" : ""}`} title={`Kaynak: ${label}`}>
      {isLocal(src) && <span className="approx">≈</span>}
      {children}
    </span>
  );
}

function ProviderStatus({ health }: { health: Health | null }) {
  if (!health) return null;
  return (
    <div className="providers">
      <span className="muted">Özellik kaynakları:</span>
      {Object.entries(health.feature_providers).map(([name, reason]) => (
        <span key={name} className={`prov ${reason ? "off" : "on"}`} title={reason ?? "Aktif"}>
          <i />
          {PROVIDER_LABEL[name] ?? name}
        </span>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ track table */

function TrackTable({ tracks }: { tracks: Track[] }) {
  return (
    <div className="table-wrap">
      <table className="tracks">
        <thead>
          <tr>
            <th>#</th>
            <th>Şarkı</th>
            <th>BPM</th>
            <th>Key</th>
            <th>Mood</th>
            <th>Energy</th>
            <th>Genre</th>
            <th title="Bulunamayan özellik sayısı">Eksik</th>
          </tr>
        </thead>
        <tbody>
          {tracks.map((t) => (
            <tr key={t.occurrence_id}>
              <td className="muted">{t.original_position}</td>
              <td>
                <div className="song">
                  <span className="song-title">{t.title}</span>
                  <span className="song-artist">
                    {t.artist} · {fmtTime(t.duration_sec)}
                  </span>
                </div>
              </td>
              <td className="num">
                {t.features.bpm ? (
                  <Src track={t} name="bpm">
                    {Math.round(t.features.bpm)}
                  </Src>
                ) : (
                  <Dash />
                )}
              </td>
              <td>
                {t.features.camelot ? (
                  <Src track={t} name="key">
                    <span className="camelot" title={t.features.key ?? ""}>
                      {t.features.camelot}
                    </span>
                  </Src>
                ) : (
                  <Dash />
                )}
              </td>
              <td>
                <Src track={t} name="mood">
                  <Chips items={t.features.mood} kind="mood" />
                </Src>
              </td>
              <td>
                <Src track={t} name="energy">
                  <EnergyBar value={t.features.energy} />
                </Src>
              </td>
              <td>
                <Src track={t} name="genre">
                  <Chips items={t.features.genre} kind="genre" />
                </Src>
              </td>
              <td className="num muted">
                {t.features.missing_features?.length ? (
                  <span title={`Eksik: ${t.features.missing_features.join(", ")}`}>
                    {t.features.missing_features.length}/5
                  </span>
                ) : (
                  "✓"
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ------------------------------------------------------------------ ordering */

function OrderList({ title, tracks, costs }: { title: string; tracks: Track[]; costs?: number[] }) {
  return (
    <div className="order-col">
      <h3>{title}</h3>
      <ol className="order-list">
        {tracks.map((t, i) => (
          <li key={t.occurrence_id}>
            <div className="order-item">
              <span className="pos">{i + 1}</span>
              <span className="order-main">
                <span className="song-title">{t.title}</span>
                <span className="song-artist">{t.artist}</span>
              </span>
              <span className="order-meta">
                {costs && i > 0 && costs[i - 1] != null && (
                  <span className="tcost" title="Önceki şarkıdan geçiş maliyeti (düşük = daha uyumlu)">
                    <span className="tdot" style={{ background: costColor(costs[i - 1]) }} />
                    {costs[i - 1].toFixed(2)}
                  </span>
                )}
                {t.features.bpm ? <span>{Math.round(t.features.bpm)}</span> : null}
                {t.features.camelot ? <span className="camelot sm">{t.features.camelot}</span> : null}
                <EnergyBar value={t.features.energy} />
              </span>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

function costColor(c: number) {
  // green → amber → red
  const hue = Math.round(130 - Math.min(1, c) * 130);
  return `hsl(${hue} 70% 50%)`;
}

function EnergyCurve({ original, ranked }: { original: Track[]; ranked: Track[] }) {
  const W = 560;
  const H = 120;
  const known = original.map((t) => t.features.energy).filter((e): e is number => e != null);
  const lo = known.length ? Math.max(0, Math.min(...known) - 0.05) : 0;
  const hi = known.length ? Math.min(1, Math.max(...known) + 0.05) : 1;
  const span = Math.max(0.1, hi - lo);
  const pts = (list: Track[]) =>
    list
      .map((t, i) => {
        const e = t.features.energy;
        if (e == null) return null;
        const x = list.length > 1 ? (i / (list.length - 1)) * (W - 20) + 10 : W / 2;
        const y = H - 10 - ((e - lo) / span) * (H - 20);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .filter(Boolean)
      .join(" ");
  return (
    <div className="curve">
      <div className="curve-legend">
        <span>
          <i className="lg lg-orig" /> Orijinal enerji
        </span>
        <span>
          <i className="lg lg-mf" /> MoodFlow enerji
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="Enerji eğrisi">
        <polyline points={pts(original)} className="line-orig" />
        <polyline points={pts(ranked)} className="line-mf" />
      </svg>
    </div>
  );
}

/* ------------------------------------------------------------------ mix panel */

function pipelineIndex(status: MixStatus | null): number {
  if (!status) return -1;
  if (status.stage === "done") return PIPELINE.length;
  if (status.stage === "sourcing") return status.tracks.some((t) => t.stage === "analyzing" || t.stage === "ready") ? 1 : 0;
  if (status.stage === "arranging") return 2;
  if (status.stage === "rendering") return 4;
  return 0;
}

function Timeline({ plan, tracks }: { plan: MixPlan; tracks: Track[] }) {
  const total = plan.total_duration_sec;
  const byId = Object.fromEntries(tracks.map((t) => [t.occurrence_id, t]));
  let cursor = 0;
  const blocks = plan.segments.map((s, i) => {
    const start = cursor;
    const fadeOut = plan.transitions[i]?.duration_sec ?? 0;
    cursor += s.duration_sec - fadeOut;
    return { s, start, fadeIn: plan.transitions[i - 1], fadeOut: plan.transitions[i] };
  });
  return (
    <div className="timeline">
      {blocks.map(({ s, start, fadeOut }, i) => (
        <div
          key={s.id}
          className={`tl-seg tl-${i % 6}`}
          style={{ left: `${(start / total) * 100}%`, width: `${(s.duration_sec / total) * 100}%`, top: i % 2 ? 26 : 0 }}
          title={`${byId[s.id]?.title ?? s.id} · ${fmtTime(s.start_sec)}'dan ${s.duration_sec.toFixed(1)} sn`}
        >
          <span className="tl-label">{byId[s.id]?.title ?? s.id}</span>
          {fadeOut && (
            <span className={`tl-fade ${fadeOut.type}`} style={{ width: `${(fadeOut.duration_sec / s.duration_sec) * 100}%` }} />
          )}
        </div>
      ))}
    </div>
  );
}

function MixPanel({
  status,
  starting,
  error,
  tracks,
  onStart,
  disabled,
}: {
  status: MixStatus | null;
  starting: boolean;
  error: string | null;
  tracks: Track[];
  onStart: () => void;
  disabled: boolean;
}) {
  const busy = starting || status?.status === "processing";
  const idx = pipelineIndex(status);
  const visibleTracks = status?.tracks.filter((t) => t.stage !== "skipped") ?? [];
  return (
    <section className="card mix">
      <div className="mix-head">
        <div>
          <h2>Otomatik Miks</h2>
          <p className="muted">
            MoodFlow sırasındaki ilk 6 uygun şarkının YouTube sesi <code>yt-dlp</code> ile alınır, en güçlü bölümler
            seçilir ve geçişlerle ~60–100 sn'lik bir miks üretilir.
          </p>
        </div>
        <button className="btn btn-primary btn-lg" onClick={onStart} disabled={disabled || busy}>
          {busy ? <span className="spinner" /> : "✨"} {busy ? "Miks hazırlanıyor…" : "Otomatik Miks Oluştur"}
        </button>
      </div>

      {error && <div className="alert alert-error">{error}</div>}

      {status && (
        <>
          <ol className="pipeline">
            {PIPELINE.map((p, i) => (
              <li
                key={p.key}
                className={
                  status.status === "failed" && i === idx ? "failed" : i < idx ? "done" : i === idx ? "active" : ""
                }
              >
                <span className="pip" />
                {p.label}
              </li>
            ))}
          </ol>

          {visibleTracks.length > 0 && (
            <ul className="progress-list">
              {visibleTracks.map((t) => (
                <li key={t.occurrence_id} className={`st-${t.stage}`}>
                  <span className="st-pill">
                    {["matching", "downloading", "analyzing"].includes(t.stage) && <span className="spinner sm" />}
                    {STAGE_LABEL[t.stage]}
                  </span>
                  <span className="progress-title">
                    {t.artist} – {t.title}
                  </span>
                  {t.message && <span className="progress-msg">{t.message}</span>}
                </li>
              ))}
            </ul>
          )}

          {status.status === "failed" && <div className="alert alert-error">{status.error}</div>}

          {status.status === "done" && status.audio_url && (
            <div className="player">
              <audio controls autoPlay src={status.audio_url} />
              <span className="muted">
                {fmtTime(status.duration_sec)} ·{" "}
                <a href={status.audio_url} download="moodflow-mix.mp3">
                  MP3 indir
                </a>
              </span>
            </div>
          )}
          {status.plan && <Timeline plan={status.plan} tracks={tracks} />}
          {status.plan && (
            <p className="legend-row muted">
              <i className="lg lg-beat" /> vuruşa hizalı crossfade <i className="lg lg-safe" /> güvenli fade
            </p>
          )}
          {status.warnings.length > 0 && (
            <ul className="warnings">
              {status.warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ app */

export default function App() {
  const [url, setUrl] = useState("");
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [playlist, setPlaylist] = useState<ImportResponse | null>(null);

  const [mode, setMode] = useState<Mode>("daily");
  const [weights, setWeights] = useState<Weights>({ bpm: 20, key: 20, mood: 20, energy: 20, genre: 20 });
  const [ranking, setRanking] = useState<RankResponse | null>(null);
  const [rankError, setRankError] = useState<string | null>(null);

  const [mixStatus, setMixStatus] = useState<MixStatus | null>(null);
  const [mixStarting, setMixStarting] = useState(false);
  const [mixError, setMixError] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
  }, []);

  const tracks = playlist?.tracks ?? [];
  const byId = useMemo(() => Object.fromEntries(tracks.map((t) => [t.occurrence_id, t])), [tracks]);
  const ranked = useMemo(
    () => (ranking ? ranking.ordered_ids.map((id) => byId[id]).filter(Boolean) : []),
    [ranking, byId],
  );

  const stopPolling = () => {
    if (pollRef.current) window.clearInterval(pollRef.current);
    pollRef.current = null;
  };
  useEffect(() => stopPolling, []);

  const doImport = async (target = url) => {
    if (!target.trim()) return;
    setUrl(target);
    setImporting(true);
    setImportError(null);
    setPlaylist(null);
    setRanking(null);
    setMixStatus(null);
    setMixError(null);
    stopPolling();
    try {
      setPlaylist(await api.importPlaylist(target));
    } catch (e) {
      setImportError((e as Error).message);
    } finally {
      setImporting(false);
    }
  };

  // re-rank whenever tracks / mode / custom weights change (debounced for sliders)
  useEffect(() => {
    if (!tracks.length) return;
    const handle = window.setTimeout(async () => {
      try {
        setRankError(null);
        setRanking(await api.rank(tracks, mode, mode === "custom" ? weights : undefined));
      } catch (e) {
        setRankError((e as Error).message);
      }
    }, mode === "custom" ? 250 : 0);
    return () => window.clearTimeout(handle);
  }, [tracks, mode, weights]);

  const startMix = useCallback(async () => {
    if (!ranking) return;
    stopPolling();
    setMixError(null);
    setMixStatus(null);
    setMixStarting(true);
    try {
      const { mix_id } = await api.startMix(tracks, ranking.ordered_ids);
      const poll = async () => {
        try {
          const s = await api.mixStatus(mix_id);
          setMixStatus(s);
          if (s.status !== "processing") stopPolling();
        } catch (e) {
          setMixError((e as Error).message);
          stopPolling();
        }
      };
      await poll();
      pollRef.current = window.setInterval(poll, 1200);
    } catch (e) {
      setMixError((e as Error).message);
    } finally {
      setMixStarting(false);
    }
  }, [ranking, tracks]);

  const improvement =
    ranking && ranking.score_before > 0
      ? Math.round(((ranking.score_before - ranking.score_after) / ranking.score_before) * 100)
      : 0;

  return (
    <div className="app">
      <header className="hero">
        <div className="logo">
          <span className="logo-mark">◐</span> MoodFlow
        </div>
        <p className="tagline">Playlistini oku, ruh haline göre yeniden sırala, tek tıkla geçişli bir miks dinle.</p>
      </header>

      <section className="card">
        <form
          className="url-form"
          onSubmit={(e) => {
            e.preventDefault();
            doImport();
          }}
        >
          <input
            type="url"
            placeholder="Herkese açık YouTube veya Spotify playlist URL'si"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            disabled={importing}
            aria-label="Playlist URL"
          />
          <button className="btn btn-primary" type="submit" disabled={importing || !url.trim()}>
            {importing ? <span className="spinner" /> : null} {importing ? "Okunuyor…" : "Playlisti Oku"}
          </button>
        </form>
        <div className="examples">
          <span className="muted">Örnek:</span>
          {EXAMPLES.map((ex) => (
            <button key={ex.url} className="btn btn-ghost btn-sm" onClick={() => doImport(ex.url)} disabled={importing}>
              {ex.label}
            </button>
          ))}
        </div>
        <ProviderStatus health={health} />
        {importing && (
          <p className="muted small">Şarkılar okunuyor ve BPM / Key / Mood / Energy / Genre analiz ediliyor (≈10–30 sn)…</p>
        )}
        {importError && <div className="alert alert-error">{importError}</div>}
      </section>

      {playlist && (
        <>
          <section className="card">
            <div className="section-head">
              <h2>
                {playlist.playlist_title ?? "Playlist"} <PlatformBadge platform={playlist.platform} />
              </h2>
              <span className="muted">{tracks.length} şarkı analiz edildi</span>
            </div>
            {playlist.warnings.length > 0 && (
              <details className="warnings-box">
                <summary>{playlist.warnings.length} uyarı</summary>
                <ul className="warnings">
                  {playlist.warnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              </details>
            )}
            <TrackTable tracks={tracks} />
            <p className="muted small">
              Öncelik: FreqBlog → ReccoBeats / GetSongBPM (BPM/Key) → Last.fm / MusicBrainz (Genre) → yerel analiz. Kaynağı görmek için
              değerin üzerine gelin; <b>≈</b> yerel/türetilmiş değerdir (enerji bu durumda playlist içinde görecelidir).
              Bulunamayan değerler boş (—) bırakılır.
            </p>
          </section>

          <section className="card">
            <div className="section-head">
              <h2>Sıralama modu</h2>
              {ranking && (
                <span className="score">
                  Geçiş maliyeti <b>{ranking.score_before.toFixed(2)}</b> → <b className="good">{ranking.score_after.toFixed(2)}</b>
                  {ranking.kept_original ? (
                    <span className="muted"> (orijinal sıra zaten en iyisi)</span>
                  ) : (
                    <span className="improve">−{improvement}%</span>
                  )}
                </span>
              )}
            </div>
            <div className="modes" role="tablist">
              {MODES.map((m) => (
                <button
                  key={m.id}
                  role="tab"
                  aria-selected={mode === m.id}
                  className={`mode ${mode === m.id ? "active" : ""}`}
                  onClick={() => setMode(m.id)}
                >
                  <span className="mode-label">{m.label}</span>
                  <span className="mode-hint">{m.hint}</span>
                </button>
              ))}
            </div>
            {mode === "custom" && (
              <div className="sliders">
                {(Object.keys(FEATURE_LABELS) as (keyof Weights)[]).map((k) => (
                  <label key={k}>
                    <span>
                      {FEATURE_LABELS[k]} <b>{weights[k]}</b>
                    </span>
                    <input
                      type="range"
                      min={0}
                      max={100}
                      value={weights[k]}
                      onChange={(e) => setWeights({ ...weights, [k]: Number(e.target.value) })}
                    />
                  </label>
                ))}
              </div>
            )}
            {rankError && <div className="alert alert-error">{rankError}</div>}
            {ranking && (
              <>
                <EnergyCurve original={tracks} ranked={ranked} />
                <div className="orders">
                  <OrderList title="Orijinal sıra" tracks={tracks} />
                  <OrderList title="MoodFlow sırası" tracks={ranked} costs={ranking.transition_costs} />
                </div>
                <p className="muted small">Sıralama yalnızca uygulama içinde yapılır; kaynak playlist değişmez.</p>
              </>
            )}
          </section>

          <MixPanel
            status={mixStatus}
            starting={mixStarting}
            error={mixError}
            tracks={tracks}
            onStart={startMix}
            disabled={!ranking}
          />
        </>
      )}

      <footer className="muted small">MoodFlow demo · React + FastAPI + yt-dlp + librosa + FFmpeg</footer>
    </div>
  );
}
