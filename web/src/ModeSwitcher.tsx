import { useEffect, useState } from "react";
export function ModeSwitcher() {
  const sbot = window.location.pathname.startsWith("/chat/sbot");
  const [enabled, setEnabled] = useState(false);
  useEffect(() => { fetch("/api/modes").then(r => r.json()).then(r => { setEnabled(Boolean(r.sbot)); if (sbot && !r.sbot) window.location.replace("/chat/privateclaw"); }).catch(() => undefined); }, []);
  if (!enabled) return null;
  return <nav className="mode-switcher" aria-label="Chat mode">
    <a href="/chat/privateclaw" aria-current={!sbot ? "page" : undefined}>PrivateClaw</a>
    <a href="/chat/sbot" aria-current={sbot ? "page" : undefined}>Sbot</a>
  </nav>;
}
