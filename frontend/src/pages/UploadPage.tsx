/**
 * Upload page — drag-and-drop / file-picker for the new run flow.
 */

import { useCallback, useRef, useState } from "react";
import type { ChangeEvent, DragEvent } from "react";
import { Icon } from "@/components/icons";
import { Banner } from "@/components/Banner";
import { useClient } from "@/lib/hooks/useClient";
import type { ProjectContext } from "@/types/ui";

interface UploadPageProps {
  ctx: ProjectContext;
  onUploaded: (runId: string) => void;
  onBack?: () => void;
}

const STAGES: ReadonlyArray<{ id: string; desc: string }> = [
  { id: "COMPILE", desc: "Extract text, layout, tables." },
  { id: "ENRICH", desc: "Link entities, summarize, redact." },
  { id: "GRAPH", desc: "Build & dedupe knowledge graph." },
  { id: "INDEX", desc: "Embed chunks for retrieval." },
];

export function UploadPage({ ctx, onUploaded, onBack }: UploadPageProps) {
  const client = useClient();
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const ready = !!ctx.tenant && !!ctx.project;

  const onPick = useCallback(() => {
    if (!ready) return;
    inputRef.current?.click();
  }, [ready]);

  const onFile = async (file: File) => {
    if (!ready) {
      setError("Tenant and Project are required. Please set them in the context bar.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const { runId } = await client.upload(file, ctx);
      onUploaded(runId);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Upload failed.");
    } finally {
      setBusy(false);
    }
  };

  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDrag(false);
    if (!ready) return;
    const f = e.dataTransfer.files?.[0];
    if (f) void onFile(f);
  };

  const handleChange = (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) void onFile(f);
  };

  return (
    <div>
      <div className="page-header">
        <div>
          {onBack && (
            <a
              href="#"
              onClick={(e) => {
                e.preventDefault();
                onBack();
              }}
              style={{
                fontSize: 12,
                color: "var(--text-muted)",
                textDecoration: "none",
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
                marginBottom: 6,
              }}
            >
              <Icon.ChevronLeft className="icon-sm" /> All runs
            </a>
          )}
          <h1>New ingestion run</h1>
          <p>
            Upload a document to generate an execution plan, then review and confirm before run.
          </p>
        </div>
      </div>

      {!ready && (
        <div style={{ marginBottom: 20 }}>
          <Banner kind="warn" title="Tenant and Project are required">
            Set Tenant ID and Project ID in the context bar above before uploading.
          </Banner>
        </div>
      )}

      {error && (
        <div style={{ marginBottom: 20 }}>
          <Banner kind="err" title="Upload failed">
            {error}
          </Banner>
        </div>
      )}

      <div className="upload">
        <div
          className={`dropzone ${drag ? "is-drag" : ""}`}
          onClick={onPick}
          onDragOver={(e) => {
            e.preventDefault();
            if (ready) setDrag(true);
          }}
          onDragLeave={() => setDrag(false)}
          onDrop={handleDrop}
          role="button"
          tabIndex={0}
          aria-disabled={!ready}
          style={{ opacity: ready ? 1 : 0.6, cursor: ready ? "pointer" : "not-allowed" }}
        >
          <input
            ref={inputRef}
            type="file"
            accept=".pdf,.docx,.html,.txt,.md"
            style={{ display: "none" }}
            onChange={handleChange}
          />
          <div className="dropzone__icon">{busy ? <Icon.Loader /> : <Icon.Upload />}</div>
          <p className="dropzone__title">{busy ? "Uploading…" : "Drop a document here"}</p>
          <p className="dropzone__hint">
            or click to browse · PDF, DOCX, HTML, TXT, MD up to 200 MB
          </p>
          <div className="dropzone__formats">
            <span className="badge badge--outline mono">.pdf</span>
            <span className="badge badge--outline mono">.docx</span>
            <span className="badge badge--outline mono">.html</span>
            <span className="badge badge--outline mono">.txt</span>
          </div>
        </div>

        <div className="upload-side">
          <h3>What happens next</h3>
          <ul>
            <li>J1 assesses the document and proposes an execution plan.</li>
            <li>
              Each step is marked <strong>RUN</strong>, <strong>SKIP</strong>, or{" "}
              <strong>CONDITIONAL</strong> with a reason.
            </li>
            <li>You confirm the plan, then watch live progress event-by-event.</li>
            <li>
              Final result includes warnings, failures, and human-review prompts when required.
            </li>
          </ul>
          <h3 style={{ marginTop: 24 }}>Stages</h3>
          <div className="stage-preview">
            {STAGES.map((s) => (
              <div key={s.id} className="stage-preview__row">
                <span
                  className={`stage-preview__chip stage-preview__chip--${s.id.toLowerCase()}`}
                >
                  {s.id}
                </span>
                <span style={{ color: "var(--text-muted)" }}>{s.desc}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
