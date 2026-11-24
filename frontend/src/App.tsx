import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { IconType } from "react-icons";
import {
  LuActivity,
  LuArrowRight,
  LuAudioWaveform,
  LuBlend,
  LuCheck,
  LuCircleCheck,
  LuCircleHelp,
  LuCircleMinus,
  LuCircleX,
  LuDisc3,
  LuDownload,
  LuHeartPulse,
  LuKeyboardMusic,
  LuListMusic,
  LuListPlus,
  LuListOrdered,
  LuMinus,
  LuPiggyBank,
  LuPin,
  LuScissors,
  LuSearch,
  LuSlidersHorizontal,
  LuSmile,
  LuSun,
  LuTags,
  LuTriangleAlert,
  LuWandSparkles,
  LuZap,
} from "react-icons/lu";
import { SiApplemusic, SiSpotify, SiYoutube } from "react-icons/si";
import {
  api,
  type FeatureName,
  type Health,
  type ImportResponse,
  type MixPlan,
  type MixStatus,
  type Platform,
  type Mode,
  type RankResponse,
  type TargetCurve,
  type TransitionStep,
  type Track,
  type TrackStage,
  type Weights,
} from "./api";
import ExportPanel from "./ExportPanel";
import FlowChart from "./FlowChart";
import PreviewPlayer, { PreviewButton } from "./PreviewPlayer";
import WeightBar from "./WeightBar";
import { OPEN_CONSENT_EVENT, clarityProjectId } from "./consent";

const EXAMPLES: { platform: Platform; label: string; url: string }[] = [
  { platform: "youtube", label: "80s Synth-Pop", url: "https://www.youtube.com/playlist?list=PLGBuKfnErZlAKsNDquW0QoEYZle-NMmDL" },
  { platform: "spotify", label: "Today's Top Hits", url: "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M" },
  {
    platform: "apple",
    label: "Today's Hits",
    url: "https://music.apple.com/us/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb",
  },
];

const PLATFORM_ICON: Record<Platform, IconType> = { youtube: SiYoutube, spotify: SiSpotify, apple: SiApplemusic };

/** Little animated equalizer: the logo mark and our loading indicator. */
function EqBars({ playing = true, className = "" }: { playing?: boolean; className?: string }) {
  return (
    <span className={`eq ${playing ? "eq-on" : ""} ${className}`} aria-hidden="true">
      <i />
      <i />
      <i />
      <i />
    </span>
  );
}

const MODES: { id: Mode; label: string; hint: string; icon: IconType }[] = [
  { id: "daily", label: "Daily", hint: "Genel uyum ve tür sürekliliği", icon: LuSun },
  { id: "dj", label: "DJ Mix", hint: "Tempo + harmonik geçişler", icon: LuDisc3 },
  { id: "emotional", label: "Emotional", hint: "Sakin → zirve → sakin", icon: LuHeartPulse },
  { id: "custom", label: "Custom", hint: "Ağırlıkları sen seç", icon: LuSlidersHorizontal },
];

const STAGE_ICON: Partial<Record<TrackStage, IconType>> = {
  waiting: LuMinus,
  ready: LuCircleCheck,
  unmatched: LuCircleHelp,
  failed: LuCircleX,
  skipped: LuCircleMinus,
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
  { key: "sourcing", label: "İndirme", icon: LuDownload },
  { key: "analysis", label: "Ses analizi", icon: LuAudioWaveform },
  { key: "arranging", label: "Bölüm seçimi", icon: LuScissors },
  { key: "transitions", label: "Geçiş", icon: LuBlend },
  { key: "rendering", label: "Render", icon: LuDisc3 },
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

const PLATFORM_LABEL: Record<Platform, string> = { youtube: "YouTube", spotify: "Spotify", apple: "Apple Music" };
const PLATFORMS: Platform[] = ["youtube", "spotify", "apple"];

/** Which supported platform a typed URL points at, if any (used to highlight its icon). */
function urlPlatform(url: string): Platform | null {
  const u = url.toLowerCase();
  if (/(^|[./])(youtube\.com|youtu\.be)/.test(u)) return "youtube";
  if (/(^|[./])spotify\.com/.test(u)) return "spotify";
  if (/(^|[./])music\.apple\.com/.test(u)) return "apple";
  return null;
}

/** What the import is doing, cycled while it runs (the request reports no real progress, so no percentage). */
const ANALYZE_STEPS: { icon: IconType; text: string }[] = [
  { icon: LuListMusic, text: "Playlist ve şarkı listesi okunuyor" },
  { icon: LuSearch, text: "Şarkılar veri kaynaklarında aranıyor" },
  { icon: LuZap, text: "BPM ve ton bulunuyor" },
  { icon: LuSmile, text: "Mood ve enerji çıkarılıyor" },
  { icon: LuTags, text: "Türler etiketleniyor" },
];
const ANALYZE_BARS = Array.from({ length: 28 }, (_, i) => ({
  delay: `${-((i * 137) % 900) / 1000}s`,
  height: `${35 + ((i * 53) % 65)}%`,
}));

/** Big "analyzing" panel shown while a playlist is being imported. */
function AnalyzingPanel({ platform }: { platform: Platform | null }) {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const t0 = Date.now();
    const id = setInterval(() => setElapsed(Math.floor((Date.now() - t0) / 1000)), 250);
    return () => clearInterval(id);
  }, []);
  const step = ANALYZE_STEPS[Math.floor(elapsed / 4) % ANALYZE_STEPS.length];
  const StepIcon = step.icon;
  const PIcon = platform ? PLATFORM_ICON[platform] : null;
  return (
    <div className="analyzing" role="status">
      <div className="an-viz" aria-hidden="true">
        {ANALYZE_BARS.map((b, i) => (
          <i key={i} style={{ "--d": b.delay, "--h": b.height } as React.CSSProperties} />
        ))}
      </div>
      <b className="an-title">
        {PIcon && <PIcon className={`url-pf-${platform}`} aria-hidden="true" />}
        {platform ? `${PLATFORM_LABEL[platform]} playlisti` : "Playlist"} analiz ediliyor
      </b>
      <span key={step.text} className="an-step">
        <StepIcon aria-hidden="true" /> {step.text}…
      </span>
      <div className="an-bar" aria-hidden="true">
        <span />
      </div>
      <p className="muted small" aria-hidden="true">
        {elapsed} sn · genelde 10–60 sn sürer; tekrar okumalar önbellekten hızlı gelir.
      </p>
    </div>
  );
}

function PlatformBadge({ platform }: { platform: Platform }) {
  const Icon = PLATFORM_ICON[platform];
  return (
    <span className={`badge badge-${platform}`}>
      <Icon aria-hidden="true" /> {PLATFORM_LABEL[platform]}
    </span>
  );
}

const PROVIDER_LABEL: Record<string, string> = {
  freqblog: "FreqBlog",
  reccobeats: "ReccoBeats",
  getsongbpm: "GetSongBPM",
  lastfm: "Last.fm",
  musicbrainz: "MusicBrainz",
};

const providerLabel = (src: string) =>
  PROVIDER_LABEL[src] ??
  (src.startsWith("local_audio")
    ? "Yerel analiz (indirilen kayıt)"
    : src.startsWith("local_rms")
      ? "Yerel RMS (indirilen kayıt, playlist içi göreceli)"
      : src.startsWith("derived")
        ? "FreqBlog valence + energy'den türetildi"
        : src);

const STATUS_LABEL: Record<string, string> = {
  ok: "bulundu",
  derived: "türetildi",
  local: "yerel ölçüm",
  missing: "bulunamadı",
  error: "kaynak hatası (sonraki okumada tekrar denenir)",
};

const isLocal = (src?: string) => !!src && (src.startsWith("local") || src.startsWith("derived"));

function Src({ track, name, children }: { track: Track; name: FeatureName; children: React.ReactNode }) {
  const src = track.features.sources?.[name];
  if (!src) return <>{children}</>;
  const status = track.features.status?.[name];
  const conf =
    name === "key" && track.features.key_confidence != null
      ? ` · güven ${track.features.key_confidence.toFixed(2)}`
      : name === "bpm" && track.features.bpm_confidence != null
        ? ` · güven ${track.features.bpm_confidence.toFixed(1)} (FreqBlog ölçeği)`
        : "";
  const title = `Kaynak: ${providerLabel(src)}${status ? ` · durum: ${STATUS_LABEL[status] ?? status}` : ""}${conf}`;
  return (
    <span className={`src ${isLocal(src) ? "src-local" : ""}`} title={title}>
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
        // GetSongBPM's free API requires a visible backlink to getsongbpm.com
        name === "getsongbpm" && !reason ? (
          <a key={name} className="prov on" href="https://getsongbpm.com" target="_blank" rel="noreferrer" title="Aktif">
            <i />
            GetSongBPM
          </a>
        ) : (
        <span key={name} className={`prov ${reason ? "off" : "on"}`} title={reason ?? "Aktif"}>
          <i />
          {PROVIDER_LABEL[name] ?? name}
        </span>
        )
      ))}
    </div>
  );
}

function ApiUsage({ usage }: { usage?: Record<string, { calls: number; cached: number }> }) {
  const rows = Object.entries(usage ?? {}).filter(([, u]) => u.calls + u.cached > 0);
  if (!rows.length) return null;
  return (
    <p className="muted small api-usage" title="Daha önce alınan yanıtlar önbellekten gelir; API kredisi harcanmaz.">
      <LuPiggyBank className="inline-ic" aria-hidden="true" /> Bu okumada API kullanımı:{" "}
      {rows.map(([name, u], i) => (
        <span key={name}>
          {i > 0 && " · "}
          {PROVIDER_LABEL[name] ?? name} <b>{u.calls}</b> istek, {u.cached} önbellekten
        </span>
      ))}
    </p>
  );
}

/* ------------------------------------------------------------------ track table */

function TrackTable({
  tracks,
  excluded,
  previewId,
  onPreview,
}: {
  tracks: Track[];
  excluded?: Set<string>;
  previewId?: string | null;
  onPreview?: (t: Track | null) => void;
}) {
  return (
    <div className="table-wrap">
      <table className="tracks">
        <thead>
          <tr>
            <th>#</th>
            <th>Şarkı</th>
            <th>
              <LuActivity className="th-ic" aria-hidden="true" /> BPM
            </th>
            <th>
              <LuKeyboardMusic className="th-ic" aria-hidden="true" /> Key
            </th>
            <th>
              <LuSmile className="th-ic" aria-hidden="true" /> Mood
            </th>
            <th>
              <LuZap className="th-ic" aria-hidden="true" /> Energy
            </th>
            <th>
              <LuTags className="th-ic" aria-hidden="true" /> Genre
            </th>
            <th title="Bulunamayan özellik sayısı">Eksik</th>
          </tr>
        </thead>
        <tbody>
          {tracks.map((t) => (
            <tr
              key={t.occurrence_id}
              className={`${excluded?.has(t.occurrence_id) ? "excluded" : ""} ${previewId === t.occurrence_id ? "previewing" : ""}`}
            >
              <td className="muted">{t.original_position}</td>
              <td>
                <div className="song-cell">
                  {onPreview && (
                    <PreviewButton track={t} active={previewId === t.occurrence_id} onToggle={onPreview} />
                  )}
                <div className="song">
                  <span className="song-title">
                    {t.title}
                    {excluded?.has(t.occurrence_id) && <span className="excluded-tag">yok sayıldı</span>}
                  </span>
                  <span className="song-artist">
                    {t.artist} · {fmtTime(t.duration_sec)}
                  </span>
                </div>
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
                  <span
                    className={t.features.missing_features.some((f) => t.features.status?.[f] === "error") ? "miss-error" : ""}
                    title={t.features.missing_features
                      .map((f) => `${f}: ${STATUS_LABEL[t.features.status?.[f] ?? "missing"]}`)
                      .join("\n")}
                  >
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

function transitionQuality(total: number): { cls: string; label: string; icon: IconType } {
  if (total < 0.2) return { cls: "good", label: "Akıcı geçiş", icon: LuCheck };
  if (total < 0.4) return { cls: "ok", label: "Orta geçiş", icon: LuMinus };
  return { cls: "rough", label: "Sert geçiş", icon: LuTriangleAlert };
}

function keyRelation(d: number): string {
  if (d === 0) return "aynı ton";
  if (d <= 0.2) return "uyumlu ton";
  if (d <= 0.5) return "yakın ton";
  if (d < 1) return "uzak ton";
  return "uyumsuz ton";
}

const signed = (v: number, digits = 0) => `${v > 0 ? "+" : v < 0 ? "−" : "±"}${Math.abs(v).toFixed(digits)}`;

/** One line between two cards: how well song a flows into song b, in words. */
function TransitionRow({ a, b, step }: { a: Track; b: Track; step?: TransitionStep }) {
  if (!step) return null;
  const q = transitionQuality(step.total);
  const facts: string[] = [];
  const fa = a.features;
  const fb = b.features;
  if (fa.bpm && fb.bpm) {
    const diff = fb.bpm - fa.bpm;
    const ratio = Math.max(fa.bpm, fb.bpm) / Math.min(fa.bpm, fb.bpm);
    if (Math.abs(diff) < 1.5) facts.push("aynı tempo");
    else if (ratio > 1.6 && step.bpm != null && step.bpm < 0.4) facts.push(`tempo ${signed(diff)} BPM (yarım/çift tempo)`);
    else facts.push(`tempo ${signed(diff)} BPM`);
  }
  if (fa.camelot && fb.camelot && step.key != null) {
    facts.push(fa.camelot === fb.camelot ? `ton ${fa.camelot}, aynı` : `ton ${fa.camelot} → ${fb.camelot} ${keyRelation(step.key)}`);
  }
  if (fa.energy != null && fb.energy != null) {
    const de = fb.energy - fa.energy;
    facts.push(Math.abs(de) < 0.03 ? "enerji aynı" : `enerji ${de > 0 ? "↑" : "↓"}${Math.abs(de).toFixed(2)}`);
  }
  const detail = (["bpm", "key", "mood", "energy", "genre"] as const)
    .map((f) => `${f}: ${step[f] == null ? "karşılaştırılamaz" : step[f]!.toFixed(2)}`)
    .join(" · ");
  return (
    <div className={`trans trans-${q.cls}`} title={`Geçiş maliyeti ${step.total.toFixed(2)} (0 = kusursuz). Özellik uzaklıkları: ${detail}`}>
      <span className="trans-q">
        <q.icon className="trans-ic" aria-hidden="true" />
        {q.label}
      </span>
      {facts.length > 0 && <span className="trans-facts">{facts.join(" · ")}</span>}
    </div>
  );
}

const PinIcon = () => <LuPin aria-hidden="true" />;

function OrderList({
  title,
  tracks,
  steps,
  pinnedId,
  onPin,
  previewId,
  onPreview,
}: {
  title: string;
  tracks: Track[];
  steps?: TransitionStep[];
  pinnedId?: string | null;
  onPin?: (id: string | null) => void;
  previewId?: string | null;
  onPreview?: (t: Track | null) => void;
}) {
  return (
    <div className="order-col">
      <h3>{title}</h3>
      <ol className="order-list">
        {tracks.map((t, i) => {
          const f = t.features;
          return (
            <li key={t.occurrence_id}>
              {i > 0 && <TransitionRow a={tracks[i - 1]} b={t} step={steps?.[i - 1]} />}
              <div className={`order-item ${pinnedId === t.occurrence_id ? "pinned" : ""}`}>
                <span className="pos">{i + 1}</span>
                <span className="order-main">
                  <span className="song-title">
                    {pinnedId === t.occurrence_id && <span className="pin-badge">İlk şarkı</span>}
                    {t.title}
                  </span>
                  <span className="song-artist">
                    {t.artist}
                    {f.mood?.length ? ` · ${f.mood.join(", ")}` : ""}
                  </span>
                </span>
                <span className="order-stats">
                  <span className="stat">
                    <span className="stat-label">BPM</span>
                    <span className="stat-value">{f.bpm ? Math.round(f.bpm) : "—"}</span>
                  </span>
                  <span className="stat" title={f.key ?? undefined}>
                    <span className="stat-label">Ton</span>
                    <span className="stat-value">
                      {f.camelot ? <span className="camelot sm">{f.camelot}</span> : "—"}
                      {f.key && <span className="stat-sub">{f.key.replace("-", " ").replace("Major", "majör").replace("Minor", "minör")}</span>}
                    </span>
                  </span>
                  <span className="stat stat-energy">
                    <span className="stat-label">Enerji</span>
                    <span className="stat-value">
                      <EnergyBar value={f.energy} />
                      {f.energy != null && <span className="stat-num">{f.energy.toFixed(2)}</span>}
                    </span>
                  </span>
                </span>
                {onPreview && (
                  <PreviewButton track={t} active={previewId === t.occurrence_id} onToggle={onPreview} />
                )}
                {onPin && (
                  <button
                    className={`pin-btn ${pinnedId === t.occurrence_id ? "on" : ""}`}
                    aria-pressed={pinnedId === t.occurrence_id}
                    aria-label={pinnedId === t.occurrence_id ? `${t.title}: sabitlemeyi kaldır` : `${t.title}: ilk şarkı yap`}
                    title={pinnedId === t.occurrence_id ? "Sabitlemeyi kaldır" : "Bu şarkıyı ilk sıraya sabitle"}
                    onClick={() => onPin(pinnedId === t.occurrence_id ? null : t.occurrence_id)}
                  >
                    <PinIcon />
                  </button>
                )}
              </div>
            </li>
          );
        })}
      </ol>
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

function Timeline({
  plan,
  tracks,
  playTime,
  onSeek,
}: {
  plan: MixPlan;
  tracks: Track[];
  playTime: number | null;
  /** when set (mix ready), clicking / dragging / arrow keys on the timeline seek the player */
  onSeek?: (t: number) => void;
}) {
  const total = plan.total_duration_sec;
  const boxRef = useRef<HTMLDivElement>(null);
  const scrubbing = useRef(false);
  const [hover, setHover] = useState<number | null>(null); // time under the pointer
  const timeAt = (clientX: number) => {
    const r = boxRef.current!.getBoundingClientRect();
    return Math.min(total, Math.max(0, ((clientX - r.left) / r.width) * total));
  };
  const seekable = !!onSeek;
  const byId = Object.fromEntries(tracks.map((t) => [t.occurrence_id, t]));
  let cursor = 0;
  const blocks = plan.segments.map((s, i) => {
    const start = cursor;
    const fadeOut = plan.transitions[i]?.duration_sec ?? 0;
    cursor += s.duration_sec - fadeOut;
    return { s, start, fadeIn: plan.transitions[i - 1], fadeOut: plan.transitions[i] };
  });
  // the segment under the playhead: the latest one that has started (crossfades overlap)
  const active = playTime == null ? -1 : blocks.reduce((acc, b, i) => (playTime >= b.start ? i : acc), -1);
  const songAt = (t: number) => byId[blocks[blocks.reduce((acc, b, k) => (t >= b.start ? k : acc), 0)].s.id]?.title;
  return (
    <div
      ref={boxRef}
      className={`timeline ${seekable ? "seekable" : ""}`}
      {...(seekable && {
        role: "slider",
        tabIndex: 0,
        "aria-label": "Miks konumu (tıklayın veya sürükleyin; ←/→ 5 sn)",
        "aria-valuemin": 0,
        "aria-valuemax": Math.round(total),
        "aria-valuenow": Math.round(playTime ?? 0),
        "aria-valuetext": `${fmtTime(playTime ?? 0)} / ${fmtTime(total)}`,
        onPointerDown: (e: React.PointerEvent<HTMLDivElement>) => {
          e.currentTarget.setPointerCapture(e.pointerId);
          scrubbing.current = true;
          onSeek!(timeAt(e.clientX));
        },
        onPointerMove: (e: React.PointerEvent<HTMLDivElement>) => {
          const t = timeAt(e.clientX);
          setHover(t);
          if (scrubbing.current) onSeek!(t);
        },
        onPointerUp: () => (scrubbing.current = false),
        onPointerCancel: () => (scrubbing.current = false),
        onPointerLeave: () => setHover(null),
        onKeyDown: (e: React.KeyboardEvent) => {
          const now = playTime ?? 0;
          const to =
            e.key === "ArrowRight" ? now + 5 : e.key === "ArrowLeft" ? now - 5 : e.key === "Home" ? 0 : e.key === "End" ? total : null;
          if (to == null) return;
          e.preventDefault();
          onSeek!(Math.min(total, Math.max(0, to)));
        },
      })}
    >
      {blocks.map(({ s, start, fadeOut }, i) => (
        <div
          key={s.id}
          className={`tl-seg tl-${i % 6} ${i === active ? "tl-active" : ""} ${active >= 0 && i !== active ? "tl-dim" : ""}`}
          style={{ left: `${(start / total) * 100}%`, width: `${(s.duration_sec / total) * 100}%`, top: i % 2 ? 26 : 0 }}
          title={`${byId[s.id]?.title ?? s.id} · ${fmtTime(s.start_sec)}'dan ${s.duration_sec.toFixed(1)} sn`}
        >
          <span className="tl-label">{byId[s.id]?.title ?? s.id}</span>
          {fadeOut && (
            <span
              className={`tl-fade ${fadeOut.type}`}
              style={{ width: `${(fadeOut.duration_sec / s.duration_sec) * 100}%` }}
              title={
                fadeOut.type === "beat_crossfade"
                  ? `${fadeOut.beats ?? ""} vuruşluk crossfade (${fadeOut.duration_sec.toFixed(1)} sn) · sonraki parçanın temposu ` +
                    `${(fadeOut.tempo_change_pct ?? 0) >= 0 ? "+" : "−"}${Math.abs(fadeOut.tempo_change_pct ?? 0).toFixed(1)}% ` +
                    `(ton değişmeden) · vuruş hizalama ${Math.abs(fadeOut.align_ms ?? 0).toFixed(0)} ms`
                  : `Güvenli fade (${fadeOut.duration_sec.toFixed(1)} sn): tempolar vuruş eşleştirmeye uygun değil`
              }
            />
          )}
        </div>
      ))}
      {playTime != null && playTime > 0 && (
        <span className="tl-playhead" style={{ left: `${Math.min(100, (playTime / total) * 100)}%` }} aria-hidden="true" />
      )}
      {seekable && hover != null && (
        <span className="tl-ghost" style={{ left: `${(hover / total) * 100}%` }} aria-hidden="true">
          <span className="tl-ghost-time">
            {fmtTime(hover)}
            {songAt(hover) ? ` · ${songAt(hover)}` : ""}
          </span>
        </span>
      )}
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
  const [playing, setPlaying] = useState(false);
  const [playTime, setPlayTime] = useState(0);
  const audioRef = useRef<HTMLAudioElement>(null);
  const seek = (t: number) => {
    const a = audioRef.current;
    if (!a) return;
    a.currentTime = t;
    setPlayTime(t); // move the playhead immediately, even while paused
  };
  useEffect(() => {
    setPlaying(false);
    setPlayTime(0);
  }, [status?.mix_id]);
  const visibleTracks = status?.tracks.filter((t) => t.stage !== "skipped") ?? [];
  return (
    <section className="card mix">
      <div className="mix-head">
        <div>
          <h2>
            <LuAudioWaveform aria-hidden="true" /> Otomatik Miks
          </h2>
          <p className="muted">
            MoodFlow sırasındaki ilk 6 uygun şarkının YouTube sesi <code>yt-dlp</code> ile alınır, en güçlü bölümler
            seçilir ve uzun, vuruşa kilitli geçişlerle ~3 dakikalık bir miks üretilir.
          </p>
        </div>
        <button className="btn btn-primary btn-lg" onClick={onStart} disabled={disabled || busy}>
          {busy ? <EqBars /> : <LuWandSparkles aria-hidden="true" />} {busy ? "Miks hazırlanıyor…" : "Otomatik Miks Oluştur"}
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
                <p.icon className="pip-ic" aria-hidden="true" />
                {p.label}
              </li>
            ))}
          </ol>

          {visibleTracks.length > 0 && (
            <ul className="progress-list">
              {visibleTracks.map((t) => (
                <li key={t.occurrence_id} className={`st-${t.stage}`}>
                  <span className="st-pill">
                    {["matching", "downloading", "analyzing"].includes(t.stage) ? (
                      t.stage === "matching" ? (
                        <LuSearch className="pulse-ic" aria-hidden="true" />
                      ) : (
                        <EqBars />
                      )
                    ) : (
                      (() => {
                        const Icon = STAGE_ICON[t.stage];
                        return Icon ? <Icon aria-hidden="true" /> : null;
                      })()
                    )}
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
              <audio
                ref={audioRef}
                controls
                autoPlay
                src={status.audio_url}
                onPlay={() => {
                  setPlaying(true);
                  window.dispatchEvent(new Event("moodflow:mixplay")); // closes a song preview
                }}
                onPause={() => setPlaying(false)}
                onEnded={() => setPlaying(false)}
                onTimeUpdate={(e) => setPlayTime(e.currentTarget.currentTime)}
              />
              <span className="muted player-meta">
                <span className={`now-playing ${playing ? "on" : ""}`}>
                  <EqBars playing={playing} /> {playing ? "Çalıyor" : "Duraklatıldı"}
                </span>
                {fmtTime(status.duration_sec)} ·{" "}
                <a href={status.audio_url} download="moodflow-mix.mp3">
                  <LuDownload className="inline-ic" aria-hidden="true" /> MP3 indir
                </a>
              </span>
            </div>
          )}
          {status.plan && (
            <Timeline
              plan={status.plan}
              tracks={tracks}
              playTime={status.status === "done" ? playTime : null}
              onSeek={status.status === "done" && status.audio_url ? seek : undefined}
            />
          )}
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
  // user-drawn target curve (Custom mode only; kept while other modes are viewed)
  const [target, setTarget] = useState<TargetCurve | null>(null);
  const [curveStrength, setCurveStrength] = useState(0.7);
  const [firstId, setFirstId] = useState<string | null>(null); // song pinned as #1 of the result
  const [preview, setPreview] = useState<Track | null>(null); // song being listened to in the dock
  const onPreview = useCallback((t: Track | null) => {
    if (t) document.querySelectorAll("audio").forEach((a) => a.pause()); // one thing playing at a time
    setPreview(t);
  }, []);
  useEffect(() => {
    const close = () => setPreview(null);
    window.addEventListener("moodflow:mixplay", close);
    return () => window.removeEventListener("moodflow:mixplay", close);
  }, []);
  const rankSeq = useRef(0);
  const [ranking, setRanking] = useState<RankResponse | null>(null);
  const [rankError, setRankError] = useState<string | null>(null);

  const [mixStatus, setMixStatus] = useState<MixStatus | null>(null);
  const [mixStarting, setMixStarting] = useState(false);
  const [mixError, setMixError] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  // auto mix is on unless the server says otherwise (MOODFLOW_MIX=0, e.g. the VPS)
  const mixEnabled = health?.mix !== false;

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
  }, []);

  const tracks = useMemo(() => playlist?.tracks ?? [], [playlist]);
  // "ignore songs with missing data": anything with at least one of the five features empty
  const [ignoreMissing, setIgnoreMissing] = useState(true);
  const incomplete = useMemo(() => tracks.filter((t) => t.features.missing_features?.length), [tracks]);
  const canIgnore = incomplete.length > 0 && tracks.length - incomplete.length >= 3;
  const ignoring = ignoreMissing && canIgnore;
  const excludedIds = useMemo(
    () => new Set(ignoring ? incomplete.map((t) => t.occurrence_id) : []),
    [ignoring, incomplete],
  );
  // the tracks ranking, chart, order lists and mix work on
  const activeTracks = useMemo(
    () => (ignoring ? tracks.filter((t) => !excludedIds.has(t.occurrence_id)) : tracks),
    [ignoring, tracks, excludedIds],
  );
  const byId = useMemo(() => Object.fromEntries(tracks.map((t) => [t.occurrence_id, t])), [tracks]);
  // a pin only counts while that song takes part (e.g. not when ignored for missing data)
  const pinnedId = firstId && activeTracks.some((t) => t.occurrence_id === firstId) ? firstId : null;
  // values measured from the downloaded recordings during a mix: shown in the table only
  // (plan v1.8 §6.4: local analysis is for the mix), ranking keeps using provider data
  const displayTracks = useMemo(() => {
    const updates = mixStatus?.feature_updates ?? {};
    return tracks.map((t) => (updates[t.occurrence_id] ? { ...t, features: updates[t.occurrence_id] } : t));
  }, [tracks, mixStatus]);
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
    setPreview(null);
    setRanking(null);
    setFirstId(null);
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

  // re-rank whenever tracks / mode / custom weights / target curve change (debounced for sliders & dragging)
  useEffect(() => {
    if (!activeTracks.length) return;
    const seq = ++rankSeq.current;
    const handle = window.setTimeout(async () => {
      try {
        setRankError(null);
        const custom = mode === "custom";
        const res = await api.rank(
          activeTracks,
          mode,
          custom ? weights : undefined,
          custom && target ? { ...target, strength: curveStrength } : undefined,
          pinnedId,
        );
        if (seq === rankSeq.current) setRanking(res); // ignore responses that arrive out of order
      } catch (e) {
        if (seq === rankSeq.current) setRankError((e as Error).message);
      }
    }, mode === "custom" ? 200 : 0);
    return () => window.clearTimeout(handle);
  }, [activeTracks, mode, weights, target, curveStrength, pinnedId]);

  const onTarget = (t: TargetCurve | null) => {
    setTarget(t);
    if (t && mode !== "custom") setMode("custom"); // drawing a curve is a Custom-mode ranking
  };

  const startMix = useCallback(async () => {
    if (!ranking) return;
    stopPolling();
    setMixError(null);
    setMixStatus(null);
    setMixStarting(true);
    try {
      const { mix_id } = await api.startMix(activeTracks, ranking.ordered_ids);
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
  }, [ranking, activeTracks]);

  const improvement =
    ranking && ranking.score_before > 0
      ? Math.round(((ranking.score_before - ranking.score_after) / ranking.score_before) * 100)
      : 0;

  return (
    <div className={`app ${preview ? "has-preview" : ""}`}>
      <header className="hero">
        <div className="logo">
          <EqBars className="logo-mark" /> MoodFlow
        </div>
        <p className="tagline">
          {mixEnabled
            ? "Playlistini oku, ruh haline göre yeniden sırala, tek tıkla geçişli bir miks dinle."
            : "Playlistini oku, ruh haline göre yeniden sırala, yeni sırayı kendi hesabına kaydet."}
        </p>
      </header>

      <section className="card">
        <form
          className="url-form"
          onSubmit={(e) => {
            e.preventDefault();
            doImport();
          }}
        >
          <span className="url-platforms" role="img" aria-label="Desteklenen platformlar: YouTube, Spotify, Apple Music">
            {PLATFORMS.map((p) => {
              const Icon = PLATFORM_ICON[p];
              const typed = urlPlatform(url);
              return (
                <Icon
                  key={p}
                  className={`url-pf-${p} ${typed && typed !== p ? "dim" : ""}`}
                  title={PLATFORM_LABEL[p]}
                  aria-hidden="true"
                />
              );
            })}
          </span>
          <input
            type="url"
            placeholder="Herkese açık YouTube, Spotify veya Apple Music playlist URL'si"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            disabled={importing}
            aria-label="Playlist URL"
          />
          <button className="btn btn-primary" type="submit" disabled={importing || !url.trim()}>
            {importing ? <EqBars /> : <LuListMusic aria-hidden="true" />} {importing ? "Okunuyor…" : "Playlisti Oku"}
          </button>
        </form>
        <div className="examples">
          <span className="muted">Örnek:</span>
          {EXAMPLES.map((ex) => {
            const Icon = PLATFORM_ICON[ex.platform];
            return (
              <button
                key={ex.url}
                className={`btn btn-ghost btn-sm ex-${ex.platform}`}
                onClick={() => doImport(ex.url)}
                disabled={importing}
                title={`${PLATFORM_LABEL[ex.platform]} örneğini oku`}
              >
                <Icon className="ex-ic" aria-hidden="true" /> {ex.label}
              </button>
            );
          })}
        </div>
        <ProviderStatus health={health} />
        {importing && <AnalyzingPanel platform={urlPlatform(url)} />}
        {importError && <div className="alert alert-error">{importError}</div>}
      </section>

      {!playlist && !importing && (
        <ol className="steps" aria-label="Nasıl çalışır">
          <li>
            <LuListMusic aria-hidden="true" />
            <b>Oku</b>
            <span>Herkese açık bir playlist yapıştır; şarkıların BPM, ton, mood, enerji ve türü çıkarılır.</span>
          </li>
          <li className="step-arrow" aria-hidden="true">
            <LuArrowRight />
          </li>
          <li>
            <LuListOrdered aria-hidden="true" />
            <b>Sırala</b>
            <span>Bir mod seç ya da grafiğe kendi eğrini çiz; MoodFlow en akıcı sırayı bulur.</span>
          </li>
          <li className="step-arrow" aria-hidden="true">
            <LuArrowRight />
          </li>
          {mixEnabled ? (
            <li>
              <LuWandSparkles aria-hidden="true" />
              <b>Mixle</b>
              <span>Tek tıkla en iyi bölümler seçilir, geçişlerle bağlanır ve sayfada çalar.</span>
            </li>
          ) : (
            <li>
              <LuListPlus aria-hidden="true" />
              <b>Kaydet</b>
              <span>Yeni sırayı Spotify veya YouTube Music hesabına playlist olarak kaydet.</span>
            </li>
          )}
        </ol>
      )}

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
                <summary>
                  <LuTriangleAlert className="inline-ic" aria-hidden="true" /> {playlist.warnings.length} uyarı
                </summary>
                <ul className="warnings">
                  {playlist.warnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              </details>
            )}
            <ApiUsage usage={playlist.api_usage} />
            <div className="ignore-row">
              <button
                className={`chip-btn toggle ${ignoring ? "on" : ""}`}
                aria-pressed={ignoring}
                disabled={!canIgnore}
                title={
                  incomplete.length === 0
                    ? "Tüm şarkıların beş özelliği de var."
                    : !canIgnore
                      ? "Eksiksiz şarkı sayısı 3'ün altında; yok saymak sıralamayı anlamsız kılar."
                      : `Açıkken en az bir özelliği eksik olan şarkılar sıralamaya${mixEnabled ? ", grafiğe ve miks'e" : " ve grafiğe"} alınmaz.`
                }
                onClick={() => setIgnoreMissing((v) => !v)}
              >
                <span className="toggle-box">{ignoring ? "✓" : ""}</span>
                Eksik verili şarkıları yok say
              </button>
              <span className="muted small-inline">
                {incomplete.length === 0
                  ? "Eksik verili şarkı yok."
                  : ignoring
                    ? `${incomplete.length} şarkı sıralama${mixEnabled ? " ve miks" : ""} dışında (tabloda soluk gösteriliyor).`
                    : `${incomplete.length} şarkıda en az bir özellik eksik.`}
              </span>
            </div>
            <TrackTable
              tracks={displayTracks}
              excluded={excludedIds}
              previewId={preview?.occurrence_id ?? null}
              onPreview={onPreview}
            />
            <p className="muted small">
              Öncelik: FreqBlog → ReccoBeats / GetSongBPM (BPM/Key) → Last.fm / MusicBrainz (Genre).
              {mixEnabled && " Eksik BPM ve enerji, miks sırasında indirilen kayıttan yerel olarak ölçülür."} Kaynak, durum ve
              güven için değerin üzerine gelin; <b>≈</b> yerel/türetilmiş değerdir. Bulunamayan değerler boş (—) bırakılır.
            </p>
          </section>

          <section className="card">
            <div className="section-head">
              <h2>
                <LuListOrdered aria-hidden="true" /> Sıralama modu
              </h2>
              {ranking && (
                <span className="score">
                  {ranking.curve_error_after != null && (
                    <span className="curve-score" title="Hedef eğriden (Custom çizimi veya Emotional yayı) ortalama sapma; 0 = tam uyum, playlist aralığına göre">
                      Eğri sapması <b>{ranking.curve_error_before?.toFixed(2)}</b> →{" "}
                      <b className="good">{ranking.curve_error_after.toFixed(2)}</b>
                    </span>
                  )}
                  {ranking.curve_error_after != null ? "Toplam skor" : "Geçiş maliyeti"}{" "}
                  <b>{ranking.score_before.toFixed(2)}</b> → <b className="good">{ranking.score_after.toFixed(2)}</b>
                  {ranking.kept_original ? (
                    <span className="muted"> (orijinal sıra zaten en iyisi)</span>
                  ) : (
                    <span className="improve" key={ranking.score_after}>
                      −{improvement}%
                    </span>
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
                  <span className="mode-label">
                    <m.icon className="mode-ic" aria-hidden="true" /> {m.label}
                  </span>
                  <span className="mode-hint">{m.hint}</span>
                </button>
              ))}
            </div>
            {mode === "custom" && <WeightBar weights={weights} onChange={setWeights} />}
            {rankError && <div className="alert alert-error">{rankError}</div>}
            {ranking && (
              <>
                <FlowChart
                  original={activeTracks}
                  ranked={ranked}
                  breakdown={ranking.transition_breakdown}
                  target={mode === "custom" ? target : null}
                  onTarget={onTarget}
                  strength={curveStrength}
                  onStrength={setCurveStrength}
                  mode={mode}
                  modeCurve={mode === "emotional" ? ranking.curve : null}
                />
                <div className="pin-row">
                  {pinnedId ? (
                    <span className="pin-chip">
                      <PinIcon /> İlk şarkı: <b>{byId[pinnedId]?.title}</b>
                      <button className="chip-btn" onClick={() => setFirstId(null)}>
                        Kaldır
                      </button>
                    </span>
                  ) : (
                    <span className="muted small-inline">
                      İsterseniz bir kartın <PinIcon /> düğmesiyle bir şarkıyı ilk sıraya sabitleyin; geri kalanı ona göre
                      sıralanır.
                    </span>
                  )}
                </div>
                <p className="muted small orders-note">
                  Kartlar arasındaki satır geçişi özetler: etiket (akıcı / orta / sert) seçili modun ağırlıklarıyla
                  hesaplanan geçiş maliyetinden gelir; ayrıntı için üzerine gelin.
                </p>
                <div className="orders">
                  <OrderList
                    title="Orijinal sıra"
                    tracks={activeTracks}
                    steps={ranking.transition_breakdown?.before}
                    pinnedId={pinnedId}
                    onPin={setFirstId}
                    previewId={preview?.occurrence_id ?? null}
                    onPreview={onPreview}
                  />
                  <OrderList
                    title="MoodFlow sırası"
                    tracks={ranked}
                    steps={ranking.transition_breakdown?.after}
                    pinnedId={pinnedId}
                    onPin={setFirstId}
                    previewId={preview?.occurrence_id ?? null}
                    onPreview={onPreview}
                  />
                </div>
                <p className="muted small">Sıralama yalnızca uygulama içinde yapılır; kaynak playlist değişmez.</p>
              </>
            )}
          </section>

          {mixEnabled && (
            <MixPanel
              status={mixStatus}
              starting={mixStarting}
              error={mixError}
              tracks={tracks}
              onStart={startMix}
              disabled={!ranking}
            />
          )}

          <ExportPanel
            tracks={activeTracks}
            orderedIds={ranking?.ordered_ids ?? null}
            defaultName={`${playlist.playlist_title ?? "Playlist"} · MoodFlow ${MODES.find((m) => m.id === mode)?.label ?? ""}`.trim()}
            description={`MoodFlow ile ${MODES.find((m) => m.id === mode)?.label ?? ""} modunda yeniden sıralandı.`}
            mixStatus={mixStatus}
          />
        </>
      )}

      {preview && <PreviewPlayer track={preview} onClose={() => setPreview(null)} />}

      <footer className="muted small">
        MoodFlow demo · React + FastAPI + yt-dlp + librosa + FFmpeg ·{" "}
        <a href="https://getsongbpm.com" target="_blank" rel="noreferrer">BPM &amp; key data by GetSongBPM</a> ·{" "}
        <a href="/privacy/">Gizlilik</a> · <a href="/terms/">Kullanım şartları</a>
        {clarityProjectId() && (
          <>
            {" · "}
            <button type="button" className="link-btn" onClick={() => window.dispatchEvent(new Event(OPEN_CONSENT_EVENT))}>
              Çerez tercihleri
            </button>
          </>
        )}
      </footer>
    </div>
  );
}
