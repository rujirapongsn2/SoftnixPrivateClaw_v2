import { useEffect, useState } from "react";
import { ArrowLeftRight } from "lucide-react";

export function ModeSwitcher() {
  const sbot = window.location.pathname.startsWith("/chat/sbot");
  const [enabled, setEnabled] = useState(false);
  useEffect(() => { fetch("/api/modes").then(r => r.json()).then(r => { setEnabled(Boolean(r.sbot)); if (sbot && !r.sbot) window.location.replace("/chat/privateclaw"); }).catch(() => undefined); }, []);
  if (!enabled) return null;
  const href = sbot ? "/chat/privateclaw" : "/chat/sbot";
  const label = sbot ? "Switch to PrivateClaw Mode" : "Switch to Bot Mode";
  return <a className="mode-switcher" href={href} aria-label={label} data-tooltip={label}>
    <ArrowLeftRight size={18} aria-hidden="true" />
  </a>;
}
