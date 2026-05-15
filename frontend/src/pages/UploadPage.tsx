/**
 * Upload page — drag-and-drop / file-picker for the new run flow.
 *
 * Single-file uploads dispatch via the existing
 * `POST /ingestion-runs` endpoint and immediately route the user to
 * the new run-detail page. Multi-file selections (up to 5) dispatch
 * via `POST /ingestion-batches` and route to the new batch-detail
 * view (rendered inline below the dropzone for now — no separate
 * batch page yet).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { ChangeEvent, DragEvent } from "react";
import { Icon } from "@/components/icons";
import { StatusBadge } from "@/components/badges";
import { Banner } from "@/components/Banner";
import { useClient } from "@/lib/hooks/useClient";
import type { BatchDetail, BatchUploadResult } from "@/lib/api/client";
import type { ProjectContext } from "@/types/ui";
import type {
  AssessmentPlanResponse,
  ExecutionProfileId,
} from "@/types/execution-profile";
import { AssessmentPlanDialog } from "./upload/AssessmentPlanDialog";

const MAX_BATCH_FILES = 5;

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
  const [batch, setBatch] = useState<BatchUploadResult | null>(null);
  const [batchDetail, setBatchDetail] = useState<BatchDetail | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Two-step single-file ingest state: when the user picks one file,
  // we open the AssessmentPlanDialog and fetch the recommendation
  // in parallel. Confirming the dialog hands control back to the
  // normal upload path with `selectedProfile` threaded through.
  // Cancelling discards everything and returns to the dropzone.
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [pendingDocumentId, setPendingDocumentId] = useState<string | null>(
    null,
  );
  const [planResponse, setPlanResponse] =
    useState<AssessmentPlanResponse | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [advancedRunning, setAdvancedRunning] = useState(false);

  const ready = !!ctx.tenant && !!ctx.project;

  const onPick = useCallback(() => {
    if (!ready || busy) return;
    inputRef.current?.click();
  }, [ready, busy]);

  const onFiles = async (files: File[]) => {
    if (!ready) {
      setError("Tenant and Project are required. Please set them in the context bar.");
      return;
    }
    if (files.length === 0) return;
    if (files.length > MAX_BATCH_FILES) {
      setError(`Up to ${MAX_BATCH_FILES} files per batch. You selected ${files.length}.`);
      return;
    }
    setError(null);
    setBatch(null);
    setBatchDetail(null);
    if (files.length === 1) {
      // Two-step flow: stash the file, open the dialog, fetch the
      // assessment plan in the background. The dialog renders a
      // "Analysing…" state until `planResponse` resolves.
      const single = files[0]!;
      setPendingFile(single);
      setPendingDocumentId(null);
      setPlanResponse(null);
      setPlanError(null);
      void (async () => {
        try {
          const { documentId } = await client.registerDocument(single, ctx);
          setPendingDocumentId(documentId);
          const plan = await client.getDocumentAssessmentPlan(documentId);
          setPlanResponse(plan);
        } catch (e) {
          setPlanError(
            e instanceof Error
              ? e.message
              : "Could not analyse this document.",
          );
        }
      })();
      return;
    }
    setBusy(true);
    try {
      const result = await client.uploadBatch(files, ctx);
      setBatch(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Upload failed.");
    } finally {
      setBusy(false);
    }
  };

  const onAssessmentConfirm = async (
    selectedProfile: ExecutionProfileId,
  ) => {
    if (pendingFile === null) return;
    const file = pendingFile;
    // Capture the decision id BEFORE clearing the plan — the
    // backend uses it to consume the same recommendation the picker
    // just showed instead of re-running the resolver.
    const decisionId = planResponse?.assessmentDecisionId ?? null;
    // Close the dialog optimistically — the dropzone shows its
    // own busy spinner while the upload runs.
    setPendingFile(null);
    setPlanResponse(null);
    setPlanError(null);
    setBusy(true);
    try {
      const { runId } = await client.upload(
        file, ctx, selectedProfile, decisionId,
      );
      onUploaded(runId);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Upload failed.");
    } finally {
      setBusy(false);
    }
  };

  const onAssessmentCancel = () => {
    setPendingFile(null);
    setPendingDocumentId(null);
    setPlanResponse(null);
    setPlanError(null);
    setAdvancedRunning(false);
  };

  const onRunAdvancedAssessment = async () => {
    if (pendingDocumentId === null) return;
    setAdvancedRunning(true);
    try {
      // The endpoint NEVER 4xxs the FE — refusals come back as a
      // structured ``result.status='refused'`` payload. Re-fetch
      // the assessment plan afterwards so the picker re-renders
      // with the new ``recommendationSource='llm_advanced_assessment'``
      // (or the unchanged source on refusal).
      await client.runAdvancedAssessment(pendingDocumentId);
      try {
        const refreshed = await client.getDocumentAssessmentPlan(
          pendingDocumentId,
        );
        setPlanResponse(refreshed);
      } catch (e) {
        // Picker stays on the prior plan; surface the fetch error
        // so the operator can retry.
        setPlanError(
          e instanceof Error
            ? e.message
            : "Could not refresh assessment after Advanced Assessment.",
        );
      }
    } catch (e) {
      setPlanError(
        e instanceof Error
          ? e.message
          : "Advanced Assessment failed.",
      );
    } finally {
      setAdvancedRunning(false);
    }
  };

  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDrag(false);
    if (!ready) return;
    const files = Array.from(e.dataTransfer.files ?? []);
    if (files.length > 0) void onFiles(files);
  };

  const handleChange = (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    if (files.length > 0) void onFiles(files);
    // Reset so picking the same files again still fires onChange.
    e.target.value = "";
  };

  // Poll the batch detail every 3s while any child run is still
  // active. Once every child is terminal, stop polling.
  useEffect(() => {
    if (!batch) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const d = await client.getBatch(batch.batchRunId);
        if (cancelled) return;
        setBatchDetail(d);
        const allTerminal = d.runs.every((r) =>
          ["completed", "completed_with_warnings", "succeeded", "succeeded_with_warnings", "failed", "cancelled", "deleted"].includes(
            r.status.toLowerCase(),
          ),
        );
        if (allTerminal) return;
        setTimeout(() => void tick(), 3000);
      } catch {
        // Surface transient errors as a dim banner but keep polling.
        if (!cancelled) setTimeout(() => void tick(), 5000);
      }
    };
    void tick();
    return () => {
      cancelled = true;
    };
  }, [batch, client]);

  return (
    <div>
      {pendingFile !== null && (
        <AssessmentPlanDialog
          filename={pendingFile.name}
          plan={planResponse}
          loadError={planError}
          onConfirm={(profile) => void onAssessmentConfirm(profile)}
          onCancel={onAssessmentCancel}
          onRunAdvancedAssessment={() => void onRunAdvancedAssessment()}
          advancedAssessmentRunning={advancedRunning}
        />
      )}
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
            Upload one document — or select up to {MAX_BATCH_FILES} for a
            sequential batch. J1 plans, runs, and surfaces results per
            file.
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
            multiple
            accept=".pdf,.docx,.html,.txt,.md"
            style={{ display: "none" }}
            onChange={handleChange}
          />
          <div className="dropzone__icon">{busy ? <Icon.Loader /> : <Icon.Upload />}</div>
          <p className="dropzone__title">
            {busy ? "Uploading…" : "Drop one or more documents here"}
          </p>
          <p className="dropzone__hint">
            or click to browse · PDF, DOCX, HTML, TXT, MD · up to{" "}
            {MAX_BATCH_FILES} files, 200 MB each
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
            <li>
              Multi-file selections run sequentially (one at a time) so they
              don't fight for shared workers.
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

      {batch && (
        <div style={{ marginTop: 24 }}>
          <h2 style={{ fontSize: 16, marginBottom: 4 }}>
            Batch <span className="mono">{batch.batchRunId.slice(0, 8)}</span>{" "}
            <span style={{ color: "var(--text-muted)", fontSize: 13 }}>
              · {batch.fileCount} file{batch.fileCount === 1 ? "" : "s"}
              {batchDetail && (
                <>
                  {" · "}
                  {batchDetail.completedCount} done · {batchDetail.failedCount} failed
                </>
              )}
            </span>
          </h2>
          <p style={{ color: "var(--text-muted)", fontSize: 13, marginBottom: 12 }}>
            Each file becomes its own ingestion run. Click a row to open it.
          </p>
          <div className="card" style={{ padding: 0 }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--text-muted)", fontSize: 12 }}>
                  <th style={{ padding: "8px 12px" }}>File</th>
                  <th style={{ padding: "8px 12px" }}>Run</th>
                  <th style={{ padding: "8px 12px" }}>Status</th>
                  <th style={{ padding: "8px 12px" }}>Stage</th>
                  <th style={{ padding: "8px 12px", textAlign: "right" }}>Progress</th>
                </tr>
              </thead>
              <tbody>
                {(batchDetail?.runs ?? batch.runIds.map((id) => ({
                  runId: id,
                  documentId: null,
                  filename: null,
                  status: "created",
                  currentStage: null,
                  currentStep: null,
                  progressPercent: 0,
                }))).map((r) => (
                  <tr
                    key={r.runId}
                    style={{ borderTop: "1px solid var(--border)", cursor: "pointer" }}
                    onClick={() => onUploaded(r.runId)}
                  >
                    <td style={{ padding: "10px 12px" }}>
                      {r.filename ?? <span className="mono" style={{ color: "var(--text-muted)" }}>—</span>}
                    </td>
                    <td style={{ padding: "10px 12px" }}>
                      <span className="mono" style={{ fontSize: 12 }}>{r.runId.slice(0, 8)}</span>
                    </td>
                    <td style={{ padding: "10px 12px" }}>
                      <StatusBadge status={r.status} />
                    </td>
                    <td style={{ padding: "10px 12px", color: "var(--text-muted)", fontSize: 13 }}>
                      {r.currentStage ?? "—"}
                      {r.currentStep && <span> · {r.currentStep}</span>}
                    </td>
                    <td style={{ padding: "10px 12px", textAlign: "right", fontSize: 13 }}>
                      {r.progressPercent}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
