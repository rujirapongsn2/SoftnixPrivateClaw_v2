import "@astryxdesign/core/reset.css";
import "@astryxdesign/core/astryx.css";
import "@astryxdesign/theme-neutral/theme.css";
import "./styles.css";

import { LayerProvider } from "@astryxdesign/core/Layer";
import { Theme } from "@astryxdesign/core/theme";
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { clawTheme } from "./theme";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Theme theme={clawTheme}>
      <LayerProvider>
        <App />
      </LayerProvider>
    </Theme>
  </React.StrictMode>,
);
