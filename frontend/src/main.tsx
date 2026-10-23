import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
// Order matters: the visual preset ships layered rules, then kit.css bridges
// the app's tokens to it, then the app's own unlayered styles win.
import "ui-style-kit-css/presets/paper-editorial.css";
import "./kit.css";
import "./styles.css";
import "./report.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
