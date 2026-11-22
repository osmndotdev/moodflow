import { useEffect, useMemo, useRef, useState } from "react";
import type { Track, TransitionStep } from "./api";

/* Original vs MoodFlow order, one metric at a time (one y-axis per chart, never two).
   Values: per position. Transition fit: per step, the 0..1 distance the ranker minimises. */

type Metric =
  | { id: string; group: "value"; label: string; get: (t: Track) => number | null; fmt: (v: number) => string; hint: string }
  | { id: string; group: "step"; label: string; key: keyof TransitionStep; hint: string };

const fmt2 = (v: number) => v.toFixed(2);

const METRICS: Metric[] = [
  { id: "energy", group: "value", label: "Energy", get: (t) => t.features.energy, fmt: fmt2, hint: "Enerji (0–1), çalma sırasına göre" },
  { id: "bpm", group: "value", label: "BPM", get: (t) => t.features.bpm, fmt: (v) => `${Math.round(v)}`, hint: "Tempo (BPM), çalma sırasına göre" },
  { id: "valence", group: "value", label: "Valence", get: (t) => t.features.valence, fmt: fmt2, hint: "Valence: duygunun pozitifliği (0–1), mood'un sayısal karşılığı" },
  { id: "s-total", group: "step", label: "Toplam", key: "total", hint: "Ağırlıklı geçiş maliyeti — düşük = daha uyumlu geçiş" },
  { id: "s-bpm", group: "step", label: "BPM", key: "bpm", hint: "Tempo farkı (0 = aynı, 1 = %25+ fark; yarım/çift tempo uyumlu sayılır)" },
  { id: "s-key", group: "step", label: "Key", key: "key", hint: "Camelot uzaklığı (0 = aynı ton, 0.2 = komşu, 1 = uyumsuz)" },
  { id: "s-mood", group: "step", label: "Mood", key: "mood", hint: "Mood etiketi ve valence farkı (0 = aynı)" },
  { id: "s-energy", group: "step", label: "Energy", key: "energy", hint: "Enerji farkı (0 = aynı)" },
  { id: "s-genre", group: "step", label: "Genre", key: "genre", hint: "Tür etiketi farkı (0 = ortak türler, 1 = hiç ortak yok)" },
];

interface Point {
  v: number | null;
  label: string;
}

function niceTicks(min: number, max: number, count = 4): number[] {
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const ticks = [];
  for (let t = Math.floor(min / step) * step; t <= max + step * 1e-6; t += step) ticks.push(+t.toFixed(10));
  if (ticks[ticks.length - 1] < max) ticks.push(ticks[ticks.length - 1] + step);
  return ticks;
}

function useWidth<T extends HTMLElement>(): [React.RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(640);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(280, Math.floor(e.contentRect.width))));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

const SERIES = [
  { id: "orig", name: "Orijinal", cls: "s-orig" },
  { id: "mf", name: "MoodFlow", cls: "s-mf" },
] as const;

export default function FlowChart({
  original,
  ranked,
  breakdown,
}: {
  original: Track[];
  ranked: Track[];
  breakdown?: { before: TransitionStep[]; after: TransitionStep[] };
}) {
  const [metricId, setMetricId] = useState("energy");
  const metric = METRICS.find((m) => m.id === metricId)!;
  const [boxRef, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);

  const series: Point[][] = useMemo(() => {
    if (metric.group === "value") {
      return [original, ranked].map((list) => list.map((t) => ({ v: metric.get(t), label: t.title })));
    }
    const steps = [breakdown?.before ?? [], breakdown?.after ?? []];
    return [original, ranked].map((list, s) =>
      steps[s].map((st, i) => ({ v: st[metric.key] ?? null, label: `${list[i]?.title} → ${list[i + 1]?.title}` })),
    );
  }, [metric, original, ranked, breakdown]);

  const n = Math.max(series[0].length, series[1].length);
  const values = series.flat().map((p) => p.v).filter((v): v is number => v != null);
  const missing = series[1].filter((p) => p.v == null).length;

  // y domain: transitions share a fixed 0..1 scale; values are padded around the data
  let ticks: number[];
  if (metric.group === "step") ticks = [0, 0.25, 0.5, 0.75, 1];
  else if (!values.length) ticks = [0, 1];
  else {
    const lo = Math.min(...values);
    const hi = Math.max(...values);
    const pad = (hi - lo || Math.abs(hi) || 1) * 0.08;
    // energy / valence live in 0..1: never draw an axis past those bounds
    const bounded = metric.id !== "bpm";
    ticks = niceTicks(bounded ? Math.max(0, lo - pad) : lo - pad, bounded ? Math.min(1, hi + pad) : hi + pad);
  }
  const d0 = ticks[0];
  const d1 = ticks[ticks.length - 1];

  const H = 200;
  const pad = { l: 40, r: 78, t: 12, b: 26 };
  const iw = width - pad.l - pad.r;
  const ih = H - pad.t - pad.b;
  const x = (i: number) => pad.l + (n > 1 ? (i / (n - 1)) * iw : iw / 2);
  const y = (v: number) => pad.t + (1 - (v - d0) / (d1 - d0 || 1)) * ih;

  const paths = series.map((pts) => {
    let d = "";
    let pen = false;
    pts.forEach((p, i) => {
      if (p.v == null) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`;
      pen = true;
    });
    return d;
  });

  // direct labels at each series' last value, nudged apart so they never collide
  const ends = series.map((pts) => {
    for (let i = pts.length - 1; i >= 0; i--) if (pts[i].v != null) return { i, y: y(pts[i].v!) };
    return null;
  });
  if (ends[0] && ends[1] && Math.abs(ends[0].y - ends[1].y) < 16) {
    const mid = Math.max(pad.t + 8, Math.min(pad.t + ih - 8, (ends[0].y + ends[1].y) / 2));
    const up = ends[0].y <= ends[1].y ? 0 : 1;
    ends[up]!.y = mid - 8;
    ends[1 - up]!.y = mid + 8;
  }

  const fmt = metric.group === "value" ? metric.fmt : fmt2;
  const xLabel = (i: number) => (metric.group === "value" ? `${i + 1}` : `${i + 1}→${i + 2}`);
  const xTickEvery = Math.ceil(n / Math.max(2, Math.floor(iw / 44)));

  const onMove = (e: React.PointerEvent<SVGRectElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const rel = (e.clientX - r.left) / r.width;
    setHover(Math.max(0, Math.min(n - 1, Math.round(rel * (n - 1)))));
  };
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowRight") setHover((h) => Math.min(n - 1, (h ?? -1) + 1));
    else if (e.key === "ArrowLeft") setHover((h) => Math.max(0, (h ?? 1) - 1));
    else return;
    e.preventDefault();
  };

  return (
    <div className="flow">
      <div className="flow-controls" role="group" aria-label="Grafik ölçüsü">
        <span className="flow-group-label">Değer</span>
        {METRICS.filter((m) => m.group === "value").map((m) => (
          <button key={m.id} className={`chip-btn ${m.id === metricId ? "on" : ""}`} onClick={() => setMetricId(m.id)}>
            {m.label}
          </button>
        ))}
        <span className="flow-group-label">Geçiş uyumu</span>
        {METRICS.filter((m) => m.group === "step").map((m) => (
          <button key={m.id} className={`chip-btn ${m.id === metricId ? "on" : ""}`} onClick={() => setMetricId(m.id)}>
            {m.label}
          </button>
        ))}
      </div>

      <div className="flow-head">
        <span className="flow-hint">{metric.hint}</span>
        <span className="flow-legend">
          {SERIES.map((s) => (
            <span key={s.id}>
              <i className={`key ${s.cls}`} /> {s.name}
            </span>
          ))}
        </span>
      </div>

      <div className="flow-plot" ref={boxRef}>
        {n === 0 || !values.length ? (
          <p className="muted small">Bu ölçü için veri yok.</p>
        ) : (
          <svg width={width} height={H} role="img" aria-label={`${metric.hint}: orijinal ve MoodFlow sırası`}>
            {ticks.map((t) => (
              <g key={t}>
                <line className="grid" x1={pad.l} x2={pad.l + iw} y1={y(t)} y2={y(t)} />
                <text className="tick" x={pad.l - 8} y={y(t)} dy="0.32em" textAnchor="end">
                  {metric.id === "bpm" ? Math.round(t) : t.toFixed(t % 1 ? 2 : 0)}
                </text>
              </g>
            ))}
            {Array.from({ length: n }, (_, i) =>
              i % xTickEvery === 0 || i === n - 1 ? (
                <text key={i} className="tick" x={x(i)} y={H - 8} textAnchor="middle">
                  {xLabel(i)}
                </text>
              ) : null,
            )}
            {hover != null && <line className="crosshair" x1={x(hover)} x2={x(hover)} y1={pad.t} y2={pad.t + ih} />}
            {SERIES.map((s, si) => (
              <g key={s.id} className={s.cls}>
                <path d={paths[si]} className="line" />
                {series[si].map((p, i) =>
                  p.v == null ? null : (
                    <circle key={i} cx={x(i)} cy={y(p.v)} r={(s.id === "orig" ? 3 : 4) + (hover === i ? 1 : 0)} className="dot" />
                  ),
                )}
                {ends[si] && (
                  <text className="direct" x={pad.l + iw + 10} y={ends[si]!.y} dy="0.32em">
                    {s.name}
                  </text>
                )}
              </g>
            ))}
            <rect
              className="hit"
              x={pad.l - 12}
              y={pad.t}
              width={iw + 24}
              height={ih}
              tabIndex={0}
              aria-label="Değerleri okumak için sağ/sol ok tuşlarını kullanın"
              onPointerMove={onMove}
              onPointerLeave={() => setHover(null)}
              onFocus={() => setHover((h) => h ?? 0)}
              onBlur={() => setHover(null)}
              onKeyDown={onKey}
            />
          </svg>
        )}
        {hover != null && values.length > 0 && (
          <div
            className="flow-tip"
            style={x(hover) + 300 > width ? { right: width - x(hover) + 12, top: 8 } : { left: x(hover) + 12, top: 8 }}
            role="status"
          >
            <div className="tip-x">{metric.group === "value" ? `${hover + 1}. sıra` : `${hover + 1}. geçiş`}</div>
            {SERIES.map((s, si) => {
              const p = series[si][hover];
              return (
                <div key={s.id} className="tip-row">
                  <i className={`key ${s.cls}`} />
                  <b>{p?.v != null ? fmt(p.v) : "—"}</b>
                  <span className="tip-name">
                    {s.name} · {p?.label ?? ""}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </div>
      {missing > 0 && (
        <p className="muted small">
          MoodFlow sırasında {missing} {metric.group === "value" ? "şarkıda" : "geçişte"} bu ölçü yok; çizgide boşluk olarak
          gösterilir.
        </p>
      )}

      <details className="flow-table">
        <summary>Tablo görünümü</summary>
        <div className="table-wrap">
          <table className="tracks">
            <thead>
              <tr>
                <th>{metric.group === "value" ? "Sıra" : "Geçiş"}</th>
                <th>Orijinal</th>
                <th className="num">{metric.label}</th>
                <th>MoodFlow</th>
                <th className="num">{metric.label}</th>
              </tr>
            </thead>
            <tbody>
              {Array.from({ length: n }, (_, i) => (
                <tr key={i}>
                  <td className="muted">{xLabel(i)}</td>
                  <td>{series[0][i]?.label}</td>
                  <td className="num">{series[0][i]?.v != null ? fmt(series[0][i].v!) : "—"}</td>
                  <td>{series[1][i]?.label}</td>
                  <td className="num">{series[1][i]?.v != null ? fmt(series[1][i].v!) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
