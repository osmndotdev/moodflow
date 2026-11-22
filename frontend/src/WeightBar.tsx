import { useEffect, useRef, useState } from "react";
import type { IconType } from "react-icons";
import { LuActivity, LuKeyboardMusic, LuSmile, LuTags, LuZap } from "react-icons/lu";
import type { Weights } from "./api";

/* Custom-mode weights as one whole split into parts (a tall segmented bar).
   Drag the boundary between two blocks to trade importance between them; click a legend
   chip to switch a feature off/on (its share is redistributed proportionally).
   Fill colours: validated categorical slots on the dark surface (adjacent CVD ΔE ≥ 8.4);
   labels inside the fills are white. */

type Key = keyof Weights;

const PARTS: { key: Key; name: string; lower: string; color: string; icon: IconType }[] = [
  { key: "bpm", name: "Tempo", lower: "tempo", color: "#3987e5", icon: LuActivity },
  { key: "key", name: "Ton", lower: "ton", color: "#d95926", icon: LuKeyboardMusic },
  { key: "mood", name: "Mood", lower: "mood", color: "#199e70", icon: LuSmile },
  { key: "energy", name: "Enerji", lower: "enerji", color: "#c98500", icon: LuZap },
  { key: "genre", name: "Tür", lower: "tür", color: "#d55181", icon: LuTags },
];

// same numbers as backend/ranking.py MODE_WEIGHTS
const PRESETS: { id: string; label: string; w: Weights }[] = [
  { id: "equal", label: "Eşit", w: { bpm: 20, key: 20, mood: 20, energy: 20, genre: 20 } },
  { id: "daily", label: "Daily gibi", w: { bpm: 20, key: 10, mood: 20, energy: 25, genre: 25 } },
  { id: "dj", label: "DJ Mix gibi", w: { bpm: 30, key: 30, mood: 10, energy: 20, genre: 10 } },
  { id: "emotional", label: "Emotional gibi", w: { bpm: 15, key: 5, mood: 30, energy: 30, genre: 20 } },
];

const MIN = 2; // an enabled part never shrinks below this by dragging; switch it off instead

/** Integer percentages summing to 100 (largest remainder). */
function toShares(w: Weights): Record<Key, number> {
  const total = PARTS.reduce((s, p) => s + Math.max(0, w[p.key]), 0);
  const out = Object.fromEntries(PARTS.map((p) => [p.key, 0])) as Record<Key, number>;
  if (total <= 0) return out;
  const raw = PARTS.map((p) => ({ k: p.key, v: (Math.max(0, w[p.key]) / total) * 100 }));
  raw.forEach((r) => (out[r.k] = Math.floor(r.v)));
  let left = 100 - raw.reduce((s, r) => s + Math.floor(r.v), 0);
  [...raw].sort((a, b) => (b.v % 1) - (a.v % 1)).forEach((r) => {
    if (left > 0 && r.v > 0) {
      out[r.k] += 1;
      left -= 1;
    }
  });
  return out;
}

function summary(shares: Record<Key, number>): string {
  const on = PARTS.filter((p) => shares[p.key] > 0).sort((a, b) => shares[b.key] - shares[a.key]);
  const off = PARTS.filter((p) => shares[p.key] === 0);
  if (!on.length) return "";
  let text: string;
  const top = on[0];
  if (on.every((p) => Math.abs(shares[p.key] - shares[top.key]) <= 2)) {
    text = `${on.map((p) => p.lower).join(", ")} eşit önemde`;
  } else if (on.length > 1 && shares[on[1].key] >= shares[top.key] - 5) {
    text = `En çok ${top.lower} (%${shares[top.key]}) ve ${on[1].lower} (%${shares[on[1].key]}) önemli`;
  } else {
    text = `En çok ${top.lower} önemli (%${shares[top.key]})`;
  }
  if (off.length) text += `; ${off.map((p) => p.lower).join(", ")} hesaba katılmıyor`;
  return text.charAt(0).toLocaleUpperCase("tr") + text.slice(1) + ".";
}

export default function WeightBar({ weights, onChange }: { weights: Weights; onChange: (w: Weights) => void }) {
  const shares = toShares(weights);
  const enabled = PARTS.filter((p) => shares[p.key] > 0);
  const barRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<number | null>(null);
  const [barWidth, setBarWidth] = useState(600);

  useEffect(() => {
    if (!barRef.current) return;
    const ro = new ResizeObserver(([e]) => setBarWidth(e.contentRect.width));
    ro.observe(barRef.current);
    return () => ro.disconnect();
  }, []);

  const emit = (next: Record<Key, number>) => onChange({ ...next });

  // boundary between enabled[i] and enabled[i+1], as a % from the left edge
  const boundary = (i: number) => enabled.slice(0, i + 1).reduce((s, p) => s + shares[p.key], 0);

  const moveBoundary = (i: number, pct: number) => {
    const a = enabled[i].key;
    const b = enabled[i + 1].key;
    const lo = boundary(i) - shares[a] + MIN;
    const hi = boundary(i) + shares[b] - MIN;
    const at = Math.round(Math.min(hi, Math.max(lo, pct)));
    const delta = at - boundary(i);
    if (!delta) return;
    emit({ ...shares, [a]: shares[a] + delta, [b]: shares[b] - delta });
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const i = dragRef.current;
    if (i == null || !barRef.current) return;
    const r = barRef.current.getBoundingClientRect();
    moveBoundary(i, ((e.clientX - r.left) / r.width) * 100);
  };

  const onKey = (i: number, e: React.KeyboardEvent) => {
    const step = e.shiftKey ? 5 : 1;
    if (e.key === "ArrowLeft" || e.key === "ArrowDown") moveBoundary(i, boundary(i) - step);
    else if (e.key === "ArrowRight" || e.key === "ArrowUp") moveBoundary(i, boundary(i) + step);
    else return;
    e.preventDefault();
  };

  const toggle = (k: Key) => {
    const isOn = shares[k] > 0;
    if (isOn && enabled.length === 1) return; // keep at least one feature
    const next = { ...shares };
    if (isOn) {
      // switch off: hand its share to the others in proportion
      const freed = next[k];
      next[k] = 0;
      const rest = enabled.filter((p) => p.key !== k);
      const restTotal = rest.reduce((s, p) => s + next[p.key], 0);
      rest.forEach((p) => (next[p.key] += (freed * next[p.key]) / restTotal));
    } else {
      // switch on: give it an equal share, taken from the others in proportion
      const give = 100 / (enabled.length + 1);
      enabled.forEach((p) => (next[p.key] -= (give * next[p.key]) / 100));
      next[k] = give;
    }
    emit(toShares(next));
  };

  const activePreset = PRESETS.find((p) => {
    const ps = toShares(p.w);
    return PARTS.every((x) => ps[x.key] === shares[x.key]);
  })?.id;

  return (
    <div className="weights">
      <div className="weights-head">
        <span className="weights-title">Sıralamada neye önem verilsin?</span>
        <span className="weights-presets" role="group" aria-label="Hazır dağılımlar">
          {PRESETS.map((p) => (
            <button
              key={p.id}
              className={`chip-btn ${activePreset === p.id ? "on" : ""}`}
              onClick={() => emit(toShares(p.w))}
            >
              {p.label}
            </button>
          ))}
        </span>
      </div>

      <div
        className="wbar"
        ref={barRef}
        role="group"
        aria-label={`Ağırlık dağılımı: ${PARTS.map((p) => `${p.name} %${shares[p.key]}`).join(", ")}`}
        onPointerMove={onPointerMove}
        onPointerUp={() => (dragRef.current = null)}
        onPointerCancel={() => (dragRef.current = null)}
      >
        {enabled.map((p) => {
          const px = (shares[p.key] / 100) * barWidth;
          const label = `%${shares[p.key]}`;
          const fits = px >= label.length * 8 + 14; // measured-ish: only print when it fits with padding
          return (
            <div
              key={p.key}
              className="wseg"
              style={{ flexGrow: shares[p.key], background: p.color }}
              title={`${p.name}: %${shares[p.key]}`}
            >
              {fits && <span className="wseg-label">{label}</span>}
            </div>
          );
        })}
        {enabled.slice(0, -1).map((p, i) => (
          <span
            key={`d-${p.key}`}
            className="wdiv"
            style={{ left: `${boundary(i)}%` }}
            role="slider"
            tabIndex={0}
            aria-label={`${p.name} ile ${enabled[i + 1].name} arasındaki sınır`}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={boundary(i)}
            aria-valuetext={`${p.name} %${shares[p.key]}, ${enabled[i + 1].name} %${shares[enabled[i + 1].key]}`}
            onPointerDown={(e) => {
              e.currentTarget.setPointerCapture(e.pointerId);
              dragRef.current = i;
            }}
            onPointerMove={onPointerMove}
            onPointerUp={() => (dragRef.current = null)}
            onKeyDown={(e) => onKey(i, e)}
          >
            <i />
          </span>
        ))}
      </div>

      <div className="wlegend" role="group" aria-label="Özellikleri aç / kapat">
        {PARTS.map((p) => {
          const on = shares[p.key] > 0;
          const Icon = p.icon;
          return (
            <button
              key={p.key}
              className={`wchip ${on ? "on" : "off"}`}
              aria-pressed={on}
              title={on ? `${p.name}: %${shares[p.key]} — tıkla: hesaba katma` : `${p.name}: kapalı — tıkla: ekle`}
              onClick={() => toggle(p.key)}
              disabled={on && enabled.length === 1}
            >
              <span className="wswatch" style={{ background: on ? p.color : "transparent", borderColor: p.color }} />
              <Icon aria-hidden="true" />
              {p.name}
              {!on && <span className="woff">kapalı</span>}
            </button>
          );
        })}
      </div>
      <p className="weights-summary muted">
        {summary(shares)} Bloklar arasındaki çizgiyi sürükleyerek önemi paylaştırın; bir özelliği kapatmak için adına
        tıklayın.
      </p>
    </div>
  );
}
