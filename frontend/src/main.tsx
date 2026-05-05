/**
 * Entry point — mounts the App into `#root` and pulls in the global
 * stylesheet that ports verbatim from the prototype.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("Mount node `#root` not found in index.html");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
