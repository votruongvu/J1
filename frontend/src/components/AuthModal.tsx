/**
 * Auth modal — captures Bearer token / API key and the API base URL.
 * State is local until "Save"; on save the parent persists via the
 * `useLocalStorage` hooks in `App.tsx`.
 */

import { useEffect, useState } from "react";
import type { AuthConfig, AuthKind } from "@/types/ui";
import { Icon } from "./icons";
import { Modal } from "./Modal";

interface AuthModalProps {
  open: boolean;
  onClose: () => void;
  auth: AuthConfig;
  onSave: (next: AuthConfig) => void;
  apiBase: string;
  onApiBaseChange: (next: string) => void;
}

export function AuthModal({
  open,
  onClose,
  auth,
  onSave,
  apiBase,
  onApiBaseChange,
}: AuthModalProps) {
  const [kind, setKind] = useState<AuthKind>(auth.kind || "bearer");
  const [value, setValue] = useState<string>(auth.value || "");
  const [reveal, setReveal] = useState(false);

  useEffect(() => {
    if (open) {
      setKind(auth.kind || "bearer");
      setValue(auth.value || "");
      setReveal(false);
    }
  }, [open, auth.kind, auth.value]);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Authorize"
      footer={
        <>
          <button
            className="btn btn--ghost"
            onClick={() => {
              onSave({ kind: "bearer", value: "" });
              onClose();
            }}
          >
            Clear
          </button>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn btn--primary"
            onClick={() => {
              onSave({ kind, value: value.trim() });
              onClose();
            }}
          >
            <Icon.Check className="icon-sm" /> Save
          </button>
        </>
      }
    >
      <div className="field-group">
        <div className="field">
          <label>API base URL</label>
          <input
            className="input"
            value={apiBase}
            onChange={(e) => onApiBaseChange(e.target.value)}
            placeholder="https://api.j1.example.com"
          />
          <span className="help">
            Used for all requests; mock mode runs entirely in the browser.
          </span>
        </div>

        <div className="field">
          <label>Authentication scheme</label>
          <div className="tabs">
            <button
              className={`tab ${kind === "bearer" ? "is-active" : ""}`}
              onClick={() => setKind("bearer")}
            >
              Bearer token
            </button>
            <button
              className={`tab ${kind === "apiKey" ? "is-active" : ""}`}
              onClick={() => setKind("apiKey")}
            >
              X-API-Key
            </button>
          </div>
        </div>

        <div className="field">
          <label>{kind === "bearer" ? "Bearer token" : "API key"}</label>
          <div style={{ position: "relative" }}>
            <input
              className="input"
              type={reveal ? "text" : "password"}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder={kind === "bearer" ? "eyJhbGciOi…" : "sk-…"}
              style={{ width: "100%", paddingRight: 38, fontFamily: "var(--font-mono)" }}
              autoComplete="off"
              spellCheck={false}
            />
            <button
              className="btn btn--ghost btn--sm"
              onClick={() => setReveal((r) => !r)}
              style={{ position: "absolute", right: 4, top: 2, height: 28, padding: "0 8px" }}
              aria-label={reveal ? "Hide" : "Show"}
            >
              {reveal ? <Icon.EyeOff className="icon-sm" /> : <Icon.Eye className="icon-sm" />}
            </button>
          </div>
          <span className="help">
            Stored in localStorage for local development. Cleared by the Clear button.
          </span>
        </div>
      </div>
    </Modal>
  );
}
