/* Cookie consent for Microsoft Clarity (analytics).

   The server injects window.MOODFLOW_CLARITY_ID only on the VPS deployment (backend/analytics.py).
   Nothing from Clarity is loaded until the visitor accepts in the banner; the choice is kept in
   localStorage and asked again after a year. Withdrawing deletes Clarity's first-party cookies and
   reloads the page, so the tag stops running. */

declare global {
  interface Window {
    MOODFLOW_CLARITY_ID?: string;
    clarity?: ((...args: unknown[]) => void) & { q?: unknown[] };
  }
}

export type Consent = "granted" | "denied";

const STORAGE_KEY = "moodflow.analytics-consent";
const MAX_AGE_MS = 365 * 24 * 3600 * 1000;
const LOCAL_HOST = /^(localhost|127(\.\d+){3}|\[::1\])$/;
/** window event that reopens the banner (footer "Çerez tercihleri" link) */
export const OPEN_CONSENT_EVENT = "moodflow:consent-open";
/** /#cerezler opens the banner too (linked from the privacy page) */
export const CONSENT_HASH = "#cerezler";

/** Clarity project ID when analytics can run here at all, else null (no banner either). */
export function clarityProjectId(): string | null {
  const id = window.MOODFLOW_CLARITY_ID;
  return id && !LOCAL_HOST.test(location.hostname) ? id : null;
}

export function storedConsent(): Consent | null {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null") as { value?: string; at?: number } | null;
    if (!saved || (saved.value !== "granted" && saved.value !== "denied")) return null;
    if (typeof saved.at !== "number" || Date.now() - saved.at > MAX_AGE_MS) return null;
    return saved.value;
  } catch {
    return null;
  }
}

let loaded = false;

function loadClarity(id: string) {
  if (loaded) return;
  loaded = true;
  // Microsoft's queueing stub: calls made before the tag arrives are replayed by it
  window.clarity =
    window.clarity ||
    function () {
      // eslint-disable-next-line prefer-rest-params
      (window.clarity!.q = window.clarity!.q || []).push(arguments);
    };
  // explicit consent signal (enforced for EEA/UK/CH visitors); MoodFlow has no ads
  window.clarity("consentv2", { ad_Storage: "denied", analytics_Storage: "granted" });
  const script = document.createElement("script");
  script.async = true;
  script.src = `https://www.clarity.ms/tag/${encodeURIComponent(id)}`;
  document.head.appendChild(script);
}

function deleteClarityCookies() {
  // _clck / _clsk sit on the site's registrable domain; try every parent domain of this host
  const parts = location.hostname.split(".");
  const domains = [""];
  for (let i = 0; i < parts.length - 1; i++) domains.push(`; domain=.${parts.slice(i).join(".")}`);
  for (const name of ["_clck", "_clsk"]) {
    for (const domain of domains) document.cookie = `${name}=; Max-Age=0; path=/${domain}`;
  }
}

/** Call once at startup: loads Clarity if the visitor already accepted. */
export function initConsent() {
  const id = clarityProjectId();
  if (id && storedConsent() === "granted") loadClarity(id);
}

export function setConsent(value: Consent) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ value, at: Date.now() }));
  } catch {
    /* storage blocked: the choice lasts for this page only */
  }
  const id = clarityProjectId();
  if (!id) return;
  if (value === "granted") {
    loadClarity(id);
  } else if (loaded) {
    window.clarity?.("consentv2", { ad_Storage: "denied", analytics_Storage: "denied" });
    deleteClarityCookies();
    location.reload();
  } else {
    deleteClarityCookies();
  }
}
