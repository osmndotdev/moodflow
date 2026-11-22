import { useEffect, useState } from "react";
import { CONSENT_HASH, OPEN_CONSENT_EVENT, clarityProjectId, setConsent, storedConsent, type Consent } from "./consent";

/* Shown only where analytics is configured (the VPS) and the visitor hasn't chosen yet, or when
   reopened from the footer / privacy page. Accept and reject are equally easy, as consent rules require. */
export default function ConsentBanner() {
  const enabled = clarityProjectId() !== null;
  const [open, setOpen] = useState(() => enabled && (storedConsent() === null || location.hash === CONSENT_HASH));

  useEffect(() => {
    const show = () => setOpen(true);
    const onHash = () => location.hash === CONSENT_HASH && show();
    window.addEventListener(OPEN_CONSENT_EVENT, show);
    window.addEventListener("hashchange", onHash);
    return () => {
      window.removeEventListener(OPEN_CONSENT_EVENT, show);
      window.removeEventListener("hashchange", onHash);
    };
  }, []);

  if (!enabled || !open) return null;

  const choose = (value: Consent) => {
    setConsent(value);
    setOpen(false);
    if (location.hash === CONSENT_HASH) history.replaceState(null, "", location.pathname + location.search);
  };

  return (
    <section className="consent" role="dialog" aria-labelledby="consent-title" aria-describedby="consent-text">
      <h2 id="consent-title">Çerez tercihi</h2>
      <p id="consent-text">
        İzin verirseniz MoodFlow'u geliştirmek için <strong>Microsoft Clarity</strong> analiz çerezlerini kullanırız:
        tıklama, kaydırma ve fare hareketleriniz oturum kaydı ve ısı haritası olarak Microsoft'a gönderilir.
        Reddederseniz hiçbir analiz aracı yüklenmez ve uygulama aynen çalışır. Kararınızı sayfanın altındaki
        "Çerez tercihleri" bağlantısından istediğiniz zaman değiştirebilirsiniz.{" "}
        <a href="/privacy/">Gizlilik politikası</a>
      </p>
      <div className="consent-actions">
        <button type="button" className="btn" onClick={() => choose("denied")}>
          Reddet
        </button>
        <button type="button" className="btn" onClick={() => choose("granted")}>
          Kabul et
        </button>
      </div>
    </section>
  );
}
