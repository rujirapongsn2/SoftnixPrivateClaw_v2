import "@astryxdesign/core/reset.css";
import "@astryxdesign/core/astryx.css";
import "@astryxdesign/theme-neutral/theme.css";
import "./styles.css";

import { LayerProvider } from "@astryxdesign/core/Layer";
import { Theme } from "@astryxdesign/core/theme";
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { BrandingProvider } from "./branding";
import { SharedView } from "./Shared";
import { clawTheme } from "./theme";

// Public share pages (/s/<token>) render a standalone, unauthenticated view —
// no app shell, no session. Everything else is the authenticated app.
const shareMatch = window.location.pathname.match(/^\/s\/([^/]+)$/);
const root = shareMatch ? (
  <SharedView token={decodeURIComponent(shareMatch[1])} />
) : (
  <App />
);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Theme theme={clawTheme}>
      <LayerProvider>
        <BrandingProvider>{root}</BrandingProvider>
      </LayerProvider>
    </Theme>
  </React.StrictMode>,
);
