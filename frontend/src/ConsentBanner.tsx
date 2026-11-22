import { useEffect, useState } from "react";
import { LuCookie } from "react-icons/lu";
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
      <div className="consent-icon" aria-hidden="true">
        <LuCookie />
      </div>
      <div className="consent-body">
        <h2 id="consent-title">Çerezlere izin veriyor musunuz?</h2>
        <p id="consent-text">
          MoodFlow'u geliştirmek için <strong>Microsoft Clarity</strong> ile tıklama ve kaydırma gibi kullanım
          verilerini toplamak istiyoruz. Reddederseniz hiçbir analiz aracı yüklenmez; izninizi istediğiniz zaman
          sayfa altından geri alabilirsiniz. <a href="/privacy/">Gizlilik politikası</a>
        </p>
      </div>
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
