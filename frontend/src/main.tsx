import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import ConsentBanner from "./ConsentBanner";
import { initConsent } from "./consent";
import "./styles.css";

initConsent();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
    <ConsentBanner />
  </React.StrictMode>,
);
