import { LuExternalLink, LuPlay, LuSquare, LuX } from "react-icons/lu";
import { SiApplemusic, SiSpotify, SiYoutube } from "react-icons/si";
import type { Track } from "./api";

/* Listen to a single song from the list with the source platform's own embed player.
   Nothing third-party loads until the user presses play (privacy + page weight):
   YouTube uses its privacy-enhanced (nocookie) domain. */

export function embedFor(t: Track): { src: string; kind: "video" | "audio"; height?: number } | null {
  if (t.platform === "youtube" && t.youtube_video_id) {
    return {
      src: `https://www.youtube-nocookie.com/embed/${encodeURIComponent(t.youtube_video_id)}?autoplay=1&rel=0`,
      kind: "video",
    };
  }
  if (t.platform === "spotify" && t.source_id) {
    return { src: `https://open.spotify.com/embed/track/${encodeURIComponent(t.source_id)}?theme=0`, kind: "audio", height: 152 };
  }
  if (t.platform === "apple" && t.source_url?.startsWith("https://music.apple.com/")) {
    return { src: t.source_url.replace("https://music.apple.com/", "https://embed.music.apple.com/"), kind: "audio", height: 175 };
  }
  return null;
}

const ICON = { youtube: SiYoutube, spotify: SiSpotify, apple: SiApplemusic } as const;
const NAME = { youtube: "YouTube", spotify: "Spotify", apple: "Apple Music" } as const;

/** Small round play/stop button for a list row or card. */
export function PreviewButton({
  track,
  active,
  onToggle,
}: {
  track: Track;
  active: boolean;
  onToggle: (t: Track | null) => void;
}) {
  if (!embedFor(track)) return null;
  return (
    <button
      className={`preview-btn ${active ? "on" : ""}`}
      aria-pressed={active}
      aria-label={active ? `${track.title}: dinlemeyi durdur` : `${track.title}: dinle`}
      title={active ? "Dinlemeyi durdur" : `${NAME[track.platform]} üzerinden dinle`}
      onClick={(e) => {
        e.stopPropagation();
        onToggle(active ? null : track);
      }}
    >
      {active ? <LuSquare aria-hidden="true" /> : <LuPlay aria-hidden="true" />}
    </button>
  );
}

/** Player docked at the bottom of the page; one song at a time. */
export default function PreviewPlayer({ track, onClose }: { track: Track; onClose: () => void }) {
  const embed = embedFor(track);
  if (!embed) return null;
  const Icon = ICON[track.platform];
  return (
    <aside className={`preview-dock preview-${embed.kind}`} aria-label={`Önizleme: ${track.artist} – ${track.title}`}>
      <div className="preview-head">
        <Icon className={`preview-ic ex-${track.platform}`} aria-hidden="true" />
        <span className="preview-title">
          <b>{track.title}</b>
          <span className="muted">{track.artist}</span>
        </span>
        {track.source_url && (
          <a className="preview-link" href={track.source_url} target="_blank" rel="noreferrer" title={`${NAME[track.platform]}'da aç`}>
            <LuExternalLink aria-hidden="true" />
          </a>
        )}
        <button className="preview-close" onClick={onClose} aria-label="Önizlemeyi kapat" title="Kapat">
          <LuX aria-hidden="true" />
        </button>
      </div>
      <iframe
        key={embed.src}
        className="preview-frame"
        src={embed.src}
        title={`${NAME[track.platform]} oynatıcı: ${track.title}`}
        style={embed.height ? { height: embed.height } : undefined}
        allow="autoplay; encrypted-media; clipboard-write; picture-in-picture; fullscreen"
        loading="lazy"
        referrerPolicy="strict-origin-when-cross-origin"
      />
    </aside>
  );
}
