import "@astryxdesign/core/reset.css";
import "@astryxdesign/core/astryx.css";
import "@astryxdesign/theme-neutral/theme.css";
import "./styles.css";

import { LayerProvider } from "@astryxdesign/core/Layer";
import { Theme } from "@astryxdesign/core/theme";
import React, { lazy, Suspense } from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
const SbotApp = lazy(() => import("./sbot/App"));
import { SharedView as SbotSharedView } from "./sbot/Shared";
import { BrandingProvider } from "./branding";
import { SharedView } from "./Shared";
import { clawTheme } from "./theme";

// Public share pages (/s/<token>) render a standalone, unauthenticated view —
// no app shell, no session. Everything else is the authenticated app.
const shareMatch = window.location.pathname.match(/^\/s\/([^/]+)$/);
const sbotShare = window.location.pathname.match(/^\/sbot\/s\/([^/]+)$/);
const root = sbotShare ? <SbotSharedView token={decodeURIComponent(sbotShare[1])} /> : shareMatch ? (
  <SharedView token={decodeURIComponent(shareMatch[1])} />
) : (
  window.location.pathname.startsWith("/chat/sbot") ? <SbotApp /> : <App />
);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {/* Light-only palette (styles.css): following an OS dark preference flips
        components like Toast to dark-mode text on our still-dark surfaces. */}
    {/* Outermost so toasts, which render in LayerProvider's tree, can translate. */}
    <BrandingProvider>
      <Theme theme={clawTheme} mode="light">
        <LayerProvider>
          <Suspense fallback={<div role="status">Loading…</div>}>{root}</Suspense>
        </LayerProvider>
      </Theme>
    </BrandingProvider>
  </React.StrictMode>,
);
