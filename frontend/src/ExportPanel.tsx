import { useEffect, useMemo, useRef, useState } from "react";
import type { IconType } from "react-icons";
import { LuCircleCheck, LuExternalLink, LuListPlus, LuLogOut, LuSave, LuTriangleAlert } from "react-icons/lu";
import { SiSpotify, SiYoutubemusic } from "react-icons/si";
import {
  api,
  ApiError,
  type ExportProvider,
  type ExportProviders,
  type ExportStatus,
  type MixStatus,
  type Track,
} from "./api";

const PROVIDERS: { id: ExportProvider; label: string; save: string; icon: IconType }[] = [
  { id: "spotify", label: "Spotify", save: "Spotify'a kaydet", icon: SiSpotify },
  { id: "youtube", label: "YouTube Music", save: "YouTube Music'e kaydet", icon: SiYoutubemusic },
];

const STORAGE_KEY = "moodflow.export.connections";
const POLL_MS = 1000;
const CONNECT_TIMEOUT_MS = 10 * 60 * 1000;

type Connections = Partial<Record<ExportProvider, string>>;
interface Account {
  connection: string;
  name: string | null;
}

// connection ids are opaque handles to tokens kept on the server; storage may be unavailable
function loadConnections(): Connections {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}") as Connections;
  } catch {
    return {};
  }
}
function saveConnections(c: Connections) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(c));
  } catch {
    /* private mode: the connection lasts for this page only */
  }
}

const sleep = (ms: number) => new Promise((r) => window.setTimeout(r, ms));

function stageText(job: ExportStatus): string {
  const n = job.items.length;
  const looked = job.items.filter((i) => !["pending", "matching"].includes(i.status)).length;
  switch (job.stage) {
    case "matching":
      return `Şarkılar eşleştiriliyor… ${looked}/${n}`;
    case "creating":
      return "Playlist oluşturuluyor…";
    case "adding":
      return `Şarkılar ekleniyor… ${job.items.filter((i) => i.status === "added").length}/${
        job.items.filter((i) => ["matched", "added"].includes(i.status)).length
      }`;
    default:
      return "";
  }
}

export default function ExportPanel({
  tracks,
  orderedIds,
  defaultName,
  description,
  mixStatus,
}: {
  tracks: Track[];
  orderedIds: string[] | null;
  defaultName: string;
  description: string;
  mixStatus: MixStatus | null;
}) {
  const [providers, setProviders] = useState<ExportProviders | null>(null);
  const [accounts, setAccounts] = useState<Partial<Record<ExportProvider, Account>>>({});
  const [connecting, setConnecting] = useState<ExportProvider | null>(null);
  const [connectLink, setConnectLink] = useState<string | null>(null); // popup blocked: open by hand
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState(defaultName);
  const [nameEdited, setNameEdited] = useState(false);
  const [isPublic, setIsPublic] = useState(false);
  const [job, setJob] = useState<ExportStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const connectRun = useRef(0); // bumping it cancels a pending connect loop
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    if (!nameEdited) setName(defaultName);
  }, [defaultName, nameEdited]);

  // which providers the server is configured for, and which stored connections are still valid
  useEffect(() => {
    api.exportProviders().then(setProviders).catch(() => setProviders(null));
    const stored = loadConnections();
    for (const p of PROVIDERS) {
      const cid = stored[p.id];
      if (!cid) continue;
      api
        .exportAccount(p.id, cid)
        .then((r) => setAccounts((a) => ({ ...a, [p.id]: { connection: cid, name: r.account } })))
        .catch((e) => {
          if (e instanceof ApiError && e.status === 401) forget(p.id);
        });
    }
    return () => {
      connectRun.current++;
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, []);

  const forget = (p: ExportProvider) => {
    const stored = loadConnections();
    delete stored[p];
    saveConnections(stored);
    setAccounts((a) => {
      const next = { ...a };
      delete next[p];
      return next;
    });
  };

  const connect = async (p: ExportProvider) => {
    setError(null);
    setConnectLink(null);
    const run = ++connectRun.current;
    // open the window inside the click handler (popup blockers), point it at the provider afterwards
    const popup = window.open("about:blank", `moodflow-${p}`, "width=520,height=720");
    setConnecting(p);
    try {
      const { state, auth_url } = await api.exportConnect(p);
      if (popup) popup.location.href = auth_url;
      else setConnectLink(auth_url);
      const deadline = Date.now() + CONNECT_TIMEOUT_MS;
      // the provider's page may cut window.opener, so the result is polled rather than messaged
      while (run === connectRun.current && Date.now() < deadline) {
        await sleep(POLL_MS);
        if (run !== connectRun.current) return;
        const r = await api.exportPoll(p, state);
        if (r.status === "pending") continue;
        if (r.status === "connected" && r.connection_id) {
          saveConnections({ ...loadConnections(), [p]: r.connection_id });
          setAccounts((a) => ({ ...a, [p]: { connection: r.connection_id!, name: r.account } }));
        } else if (r.status === "failed") {
          setError(r.error ?? "Bağlantı başarısız.");
        } else {
          setError("Bağlantı isteğinin süresi doldu; tekrar deneyin.");
        }
        break;
      }
    } catch (e) {
      popup?.close();
      setError((e as Error).message);
    } finally {
      if (run === connectRun.current) {
        setConnecting(null);
        setConnectLink(null);
      }
    }
  };

  const cancelConnect = () => {
    connectRun.current++;
    setConnecting(null);
    setConnectLink(null);
  };

  const disconnect = async (p: ExportProvider) => {
    const acc = accounts[p];
    forget(p);
    if (acc) await api.exportDisconnect(p, acc.connection).catch(() => undefined);
  };

  // reuse YouTube videos the mix already matched (saves a search per song)
  const exportTracks = useMemo(() => {
    const vids = new Map((mixStatus?.tracks ?? []).map((t) => [t.occurrence_id, t.youtube_video_id]));
    return tracks.map((t) => (!t.youtube_video_id && vids.get(t.occurrence_id) ? { ...t, youtube_video_id: vids.get(t.occurrence_id)! } : t));
  }, [tracks, mixStatus]);

  const startExport = async (p: ExportProvider) => {
    const acc = accounts[p];
    if (!acc || !orderedIds) return;
    setError(null);
    setStarting(true);
    if (pollRef.current) window.clearInterval(pollRef.current);
    try {
      const first = await api.startExport(p, acc.connection, {
        tracks: exportTracks,
        ordered_ids: orderedIds,
        name: name.trim() || defaultName,
        description,
        public: isPublic,
      });
      setJob(first);
      pollRef.current = window.setInterval(async () => {
        try {
          const s = await api.exportStatus(first.export_id);
          setJob(s);
          if (s.status !== "processing" && pollRef.current) window.clearInterval(pollRef.current);
        } catch (e) {
          if (pollRef.current) window.clearInterval(pollRef.current);
          setError((e as Error).message);
        }
      }, POLL_MS * 1.2);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) forget(p);
      setError((e as Error).message);
    } finally {
      setStarting(false);
    }
  };

  const busy = starting || job?.status === "processing";
  const anyConfigured = providers && (providers.spotify.configured || providers.youtube.configured);
  const missed = job?.items.filter((i) => i.status === "not_found" || i.status === "failed") ?? [];
  const jobLabel = PROVIDERS.find((p) => p.id === job?.provider)?.label;

  return (
    <section className="card export">
      <h2>
        <LuListPlus aria-hidden="true" /> Playlist olarak kaydet
      </h2>
      <p className="muted">
        MoodFlow sırasını kendi Spotify veya YouTube Music hesabınızda yeni bir playlist olarak kaydedin. Hesabınızı bir
        kez bağlarsınız; kaynak playlist değişmez.
      </p>

      {providers && !anyConfigured && (
        <p className="muted small">
          Dışa aktarma bu sunucuda henüz ayarlanmadı (Spotify / Google istemci kimlikleri .env'de yok).
        </p>
      )}

      {anyConfigured && (
        <>
          <div className="export-fields">
            <label className="export-name">
              <span className="muted small">Playlist adı</span>
              <input
                value={name}
                maxLength={100}
                onChange={(e) => {
                  setName(e.target.value);
                  setNameEdited(true);
                }}
              />
            </label>
            <label className="export-public">
              <input type="checkbox" checked={isPublic} onChange={(e) => setIsPublic(e.target.checked)} />
              Herkese açık
            </label>
          </div>

          <div className="export-providers">
            {PROVIDERS.map(({ id, label, save, icon: Icon }) => {
              const configured = providers?.[id].configured;
              const acc = accounts[id];
              return (
                <div key={id} className={`export-provider ep-${id} ${configured ? "" : "off"}`}>
                  <div className="ep-head">
                    <Icon className="ep-ic" aria-hidden="true" />
                    <b>{label}</b>
                    {acc && (
                      <span className="ep-account muted small">
                        <LuCircleCheck aria-hidden="true" /> {acc.name ?? "bağlı"}
                      </span>
                    )}
                  </div>
                  {!configured ? (
                    <span className="muted small">Sunucuda ayarlı değil</span>
                  ) : acc ? (
                    <div className="ep-actions">
                      <button className="btn btn-primary" disabled={busy || !orderedIds} onClick={() => startExport(id)}>
                        <LuSave aria-hidden="true" /> {save}
                      </button>
                      <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => disconnect(id)}>
                        <LuLogOut aria-hidden="true" /> Bağlantıyı kes
                      </button>
                    </div>
                  ) : connecting === id ? (
                    <div className="ep-actions">
                      <span className="muted small">
                        Açılan pencerede izin verin…
                        {connectLink && (
                          <>
                            {" "}
                            Pencere açılmadıysa{" "}
                            <a href={connectLink} target="_blank" rel="noreferrer">
                              buradan açın
                            </a>
                            .
                          </>
                        )}
                      </span>
                      <button className="btn btn-ghost btn-sm" onClick={cancelConnect}>
                        İptal
                      </button>
                    </div>
                  ) : (
                    <button className="btn" disabled={connecting !== null} onClick={() => connect(id)}>
                      <Icon aria-hidden="true" /> Hesabı bağla
                    </button>
                  )}
                </div>
              );
            })}
          </div>
          {providers?.youtube.configured && (
            <p className="muted small">
              YouTube Music: playlist YouTube hesabınıza kaydedilir ve YouTube Music kitaplığında görünür. Google API
              kotası nedeniyle günde birkaç uzun playlist aktarılabilir.
            </p>
          )}
        </>
      )}

      {error && <div className="alert alert-error">{error}</div>}

      {job && (
        <div className="export-job">
          {job.status === "processing" && <p className="export-stage">{stageText(job)}</p>}
          {job.status === "failed" && <div className="alert alert-error">{job.error}</div>}
          {job.status === "done" && (
            <p className="export-done">
              <LuCircleCheck className="good-ic" aria-hidden="true" /> <b>{job.playlist_name}</b> {jobLabel}{" "}
              hesabınıza kaydedildi: {job.added}/{job.items.length} şarkı.{" "}
              {job.playlist_url && (
                <a href={job.playlist_url} target="_blank" rel="noreferrer">
                  Playlist'i aç <LuExternalLink className="inline-ic" aria-hidden="true" />
                </a>
              )}
            </p>
          )}
          {job.status !== "processing" && missed.length > 0 && (
            <details className="warnings-box">
              <summary>
                <LuTriangleAlert className="inline-ic" aria-hidden="true" /> {missed.length} şarkı eklenmedi
              </summary>
              <ul className="warnings">
                {missed.map((i) => (
                  <li key={i.occurrence_id}>
                    {i.artist} – {i.title}
                    {i.message ? `: ${i.message}` : ""}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {job.warnings.length > 0 && (
            <ul className="warnings">
              {job.warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
