import React from "react";
import { createRoot } from "react-dom/client";
import { App, orderAccountsById } from "./App";
import "./styles.css";

declare const module:
  | { exports: { orderAccountsById?: typeof orderAccountsById } }
  | undefined;

if (typeof module !== "undefined" && module.exports) {
  module.exports.orderAccountsById = orderAccountsById;
} else {
  const legacySelectors = [
    ".app-shell",
    "[data-open-tasks]",
    "#task-drawer",
    "#drawer-backdrop",
    "#confirm-dialog",
    "#toast-region",
  ];
  document.querySelectorAll<HTMLElement>(legacySelectors.join(",")).forEach((element) => {
    element.hidden = true;
    element.style.display = "none";
  });
  let container = document.getElementById("react-root") ?? document.getElementById("root");
  if (!container) {
    container = document.createElement("div");
    container.id = "react-root";
    document.body.appendChild(container);
  }
  createRoot(container).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
}
