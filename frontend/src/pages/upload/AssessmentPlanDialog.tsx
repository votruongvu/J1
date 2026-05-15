/**
 * AssessmentPlanDialog — the two-step ingest flow's profile picker.
 *
 * Renders between "user picks a file" and "ingestion run starts."
 * Owner (`UploadPage`) is responsible for:
 *   1. Registering the document (`POST /documents`)
 *   2. Fetching the assessment plan (`client.getDocumentAssessmentPlan`)
 *   3. Passing the plan + state callbacks into this dialog
 *
 * The dialog itself is presentation-only — no fetches, no
 * navigation. This keeps it trivially testable and lets the
 * owner own the error-handling story.
 *
 * Backend contract: response shape is `AssessmentPlanResponse`
 * (see `@/types/execution-profile`); the selected profile is
 * a wire string passed through the existing `client.upload(...,
 * selectedProfile)` plumbing.
 *
 * Visual conventions follow the existing
 * [DocumentLifecycleDialogs](../documents/DocumentLifecycleDialogs.tsx):
 * vanilla `.modal-backdrop` + `.modal-card` with two-button
 * footer. No external dialog library — the rest of the FE is
 * also vanilla.
 */

import { useState } from "react";

import { Banner } from "@/components/Banner";
import { manualActionsEnabled } from "@/lib/constants/feature-flags";
import type {
  AssessmentPlanResponse,
  CompileOptionPreview,
  ExecutionProfileDetails,
  ExecutionProfileId,
} from "@/types/execution-profile";

import {
  capabilityBullets,
  defaultInitialSelection,
  FALLBACK_WARNING_BODY,
  orderedProfiles,
  profileLabel,
  profileTagline,
  recommendationSourceLabel,
} from "./assessment-plan-helpers";


interface AssessmentPlanDialogProps {
  /** Filename surfaced in the dialog header so the user knows
   * which document they're configuring (especially in re-index
   * flows where multiple are mid-flight). */
  filename: string;
  /** Backend recommendation + catalogue. When `null` the dialog
   * renders a loading state — owner passes the response once
   * the fetch resolves. */
  plan: AssessmentPlanResponse | null;
  /** Operator-readable error from the assessment-plan fetch.
   * When set, the dialog disables Start Indexing and surfaces
   * the message so the user can retry. */
  loadError: string | null;
  /** Called when the user clicks Start Indexing. Owner kicks
   * off `client.upload(file, ctx, selectedProfile)` from here. */
  onConfirm: (selectedProfile: ExecutionProfileId) => void;
  /** Called when the user clicks Cancel or the backdrop. */
  onCancel: () => void;
  /** Called when the user clicks "Run Advanced Assessment". The
   * owner is responsible for the API call + refreshing ``plan``
   * with the new recommendation. When omitted, the button is
   * hidden (deployments without LLM advanced assessment wired). */
  onRunAdvancedAssessment?: () => void;
  /** When ``true`` the Advanced Assessment button renders in a
   * busy state. Owners flip this while the API call is in
   * flight so the operator can't double-click. */
  advancedAssessmentRunning?: boolean;
}


export function AssessmentPlanDialog({
  filename,
  plan,
  loadError,
  onConfirm,
  onCancel,
  onRunAdvancedAssessment,
  advancedAssessmentRunning,
}: AssessmentPlanDialogProps) {
  // Pre-select the backend-recommended profile so the user sees
  // a sensible default. The radio group is fully active — one
  // click switches to any other profile.
  const initial = plan !== null ? defaultInitialSelection(plan) : "standard";
  const [selected, setSelected] = useState<ExecutionProfileId>(initial);

  // Owner-passed `plan` may arrive after the dialog mounts (while
  // the fetch resolves). Re-sync `selected` to the recommendation
  // the first time a plan shows up, but only when the user
  // hasn't already changed their pick — `selected` is the source
  // of truth otherwise.
  const [synced, setSynced] = useState(false);
  if (plan !== null && !synced) {
    setSynced(true);
    setSelected(defaultInitialSelection(plan));
  }

  return (
    <div
      className="modal-backdrop"
      onClick={onCancel}
      data-testid="assessment-plan-dialog"
    >
      <div
        className="modal-card assessment-plan-dialog"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="assessment-plan-dialog__title"
      >
        <h3 id="assessment-plan-dialog__title">
          How thorough should this ingest be?
        </h3>
        <p className="assessment-plan-dialog__filename">
          <strong>{filename}</strong>
        </p>

        {/* Recommendation banner */}
        {plan !== null && (
          <RecommendationBanner plan={plan} />
        )}

        {/* Fallback warning — surfaces only when no domain or
            general rule matched, so the operator knows the
            recommendation came from lightweight signals alone. */}
        {plan !== null && plan.fallbackUsed && (
          <div data-testid="assessment-plan-fallback-warning">
            <Banner kind="warn" title="No document rule matched">
              {FALLBACK_WARNING_BODY}
            </Banner>
          </div>
        )}

        {/* Compile-option preview — hedged hints under the picker. */}
        {plan !== null && (
          <CompileOptionPreviewPanel preview={plan.compileOptionPreview} />
        )}

        {/* Warnings from the profiler / resolver (file-size, env
            downgrades, etc.). We intentionally render the fallback
            banner above as its own block; this list catches
            anything else. */}
        {plan !== null
          && plan.warnings.filter(
              (w) => w !== FALLBACK_WARNING_BODY,
            ).length > 0 && (
          <div data-testid="assessment-plan-warnings">
            <Banner kind="warn" title="Notes on this document">
              <ul>
                {plan.warnings
                  .filter((w) => w !== FALLBACK_WARNING_BODY)
                  .map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
              </ul>
            </Banner>
          </div>
        )}

        {/* Load error */}
        {loadError !== null && (
          <div data-testid="assessment-plan-error">
            <Banner kind="err" title="Could not analyse document">
              {loadError}
            </Banner>
          </div>
        )}

        {/* Loading state */}
        {plan === null && loadError === null && (
          <p className="assessment-plan-dialog__loading">
            Analysing document…
          </p>
        )}

        {/* Profile picker */}
        {plan !== null && (
          <ProfilePicker
            profiles={orderedProfiles(plan)}
            recommended={plan.recommendedProfile}
            selected={selected}
            onSelect={setSelected}
          />
        )}

        {/* Sample-text status warning — surfaces only when the
            LLM result indicates extraction wasn't reliable. */}
        {plan !== null && (
          <SampleTextWarning plan={plan} />
        )}

        {/* Suggested next manual steps — operator-triggered, never
            auto-run. Disabled "Coming soon" until the manual-action
            endpoints are wired. */}
        {plan !== null && (
          <RecommendedNextStepsPanel plan={plan} />
        )}

        {/* Advanced Assessment trigger — operator-triggered ONLY.
            Hidden when the owner didn't provide a handler (i.e.
            deployments without the LLM service wired). Copy is
            explicit that each click is a NEW LLM call — we don't
            cache results in this build. */}
        {plan !== null && onRunAdvancedAssessment !== undefined && (
          <div
            className="assessment-plan-dialog__advanced"
            data-testid="assessment-plan-advanced-assessment"
          >
            <button
              type="button"
              className="btn btn--ghost"
              onClick={onRunAdvancedAssessment}
              disabled={advancedAssessmentRunning === true}
              data-testid="assessment-plan-advanced-assessment-button"
            >
              {advancedAssessmentRunning
                ? "Running Advanced Assessment…"
                : "Run Advanced Assessment"}
            </button>
            <small>
              Each click starts a NEW LLM assessment — results are
              not cached. The LLM estimates document complexity and
              suggests a profile + next manual steps. May cost more
              and take longer than the lightweight assessment.
            </small>
          </div>
        )}

        {/* Action buttons */}
        <div className="modal__actions">
          <button
            type="button"
            className="btn btn--ghost"
            onClick={onCancel}
            data-testid="assessment-plan-cancel"
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => onConfirm(selected)}
            disabled={plan === null || loadError !== null}
            data-testid="assessment-plan-confirm"
          >
            Start Indexing
          </button>
        </div>
      </div>
    </div>
  );
}


function RecommendationBanner({ plan }: { plan: AssessmentPlanResponse }) {
  return (
    <div
      className="assessment-plan-dialog__recommendation"
      data-testid="assessment-plan-recommendation"
    >
      <div>
        Recommended: <strong>{profileLabel(plan.recommendedProfile)}</strong>
      </div>
      <div
        className="assessment-plan-dialog__source"
        data-testid="assessment-plan-recommendation-source"
      >
        {recommendationSourceLabel(plan.recommendationSource)}
      </div>
      {plan.reasons.length > 0 && (
        <ul className="assessment-plan-dialog__reasons">
          {plan.reasons.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      )}
    </div>
  );
}


/** Compile-option preview — hedged hints, never claims. The note
 * text comes from the backend so future tweaks don't require
 * a coordinated FE release. */
function CompileOptionPreviewPanel({
  preview,
}: {
  preview: CompileOptionPreview;
}) {
  const hints: { label: string; on: boolean }[] = [
    { label: "Tables likely", on: preview.suspectedTables },
    { label: "Images likely", on: preview.suspectedImages },
    { label: "Scanned content suspected", on: preview.suspectedScanned },
    {
      label: "Requirements likely (rule-based hint)",
      on: preview.suspectedRequirements,
    },
    {
      label: "Long document suspected",
      on: preview.suspectedLongDocument,
    },
  ];
  const active = hints.filter((h) => h.on);
  if (active.length === 0) return null;
  return (
    <div
      className="assessment-plan-dialog__compile-preview"
      data-testid="assessment-plan-compile-preview"
    >
      <strong>What this document likely contains</strong>
      <ul>
        {active.map((h) => (
          <li key={h.label}>{h.label}</li>
        ))}
      </ul>
      <small>{preview.note}</small>
    </div>
  );
}


function ProfilePicker({
  profiles,
  recommended,
  selected,
  onSelect,
}: {
  profiles: readonly ExecutionProfileDetails[];
  recommended: ExecutionProfileId;
  selected: ExecutionProfileId;
  onSelect: (id: ExecutionProfileId) => void;
}) {
  return (
    <fieldset
      className="assessment-plan-dialog__profiles"
      data-testid="assessment-plan-picker"
    >
      <legend className="visually-hidden">Choose a profile</legend>
      {profiles.map((p) => (
        <ProfileCard
          key={p.id}
          details={p}
          recommended={p.id === recommended}
          checked={p.id === selected}
          onSelect={() => onSelect(p.id)}
        />
      ))}
    </fieldset>
  );
}


function ProfileCard({
  details,
  recommended,
  checked,
  onSelect,
}: {
  details: ExecutionProfileDetails;
  recommended: boolean;
  checked: boolean;
  onSelect: () => void;
}) {
  const bullets = capabilityBullets(details);
  return (
    <label
      className={
        "assessment-plan-dialog__profile-card"
        + (checked ? " assessment-plan-dialog__profile-card--checked" : "")
      }
      data-testid={`assessment-plan-card-${details.id}`}
    >
      <input
        type="radio"
        name="execution-profile"
        value={details.id}
        checked={checked}
        onChange={onSelect}
      />
      <div className="assessment-plan-dialog__profile-card-body">
        <div className="assessment-plan-dialog__profile-card-header">
          <strong>{profileLabel(details.id)}</strong>
          {recommended && (
            <span
              className="assessment-plan-dialog__recommended-pill"
              data-testid={`assessment-plan-recommended-pill-${details.id}`}
            >
              Recommended
            </span>
          )}
        </div>
        <p className="assessment-plan-dialog__profile-card-tagline">
          {profileTagline(details.id)}
        </p>
        <ul className="assessment-plan-dialog__profile-card-bullets">
          {bullets.map((b, i) => (
            <li key={i}>{b}</li>
          ))}
        </ul>
      </div>
    </label>
  );
}


/** Labels for the manual-action wire ids the LLM may suggest in
 * ``recommended_next_steps``. Stays in lockstep with the backend's
 * ``j1.processing.manual_actions`` vocabulary; a rename on one side
 * surfaces here as an "unknown action id" fallback. */
const _NEXT_STEP_LABELS: Record<string, string> = {
  run_domain_enrichment: "Run Domain Enrichment",
  build_knowledge_memory: "Build Knowledge Memory",
  normalize_entities: "Normalize Entities",
  build_deep_knowledge_index: "Build / Extend Deep Knowledge Index",
  run_multimodal_enrichment: "Run Multimodal Enrichment",
};


/** Render the LLM-suggested next manual steps as disabled
 * "Coming soon" buttons. Per the showcase spec the manual-action
 * endpoints don't exist yet — clicking these would 404, so we
 * disable them and add a clarifying note. ``manualActionsEnabled``
 * gates the entire panel: when the deployment runs in legacy mode
 * the LLM's suggestions are still in the decision payload but the
 * picker doesn't surface them. */
function RecommendedNextStepsPanel({
  plan,
}: {
  plan: AssessmentPlanResponse;
}) {
  const ids: string[] = Array.isArray(
    (plan as { recommendedNextSteps?: unknown }).recommendedNextSteps,
  )
    ? ((plan as unknown as { recommendedNextSteps: string[] })
        .recommendedNextSteps)
    : [];
  if (!manualActionsEnabled || ids.length === 0) return null;
  return (
    <div
      className="assessment-plan-dialog__next-steps"
      data-testid="assessment-plan-next-steps"
    >
      <strong>Suggested next steps</strong>
      <p className="muted">
        The LLM suggested these manual actions for after indexing
        completes. They never run automatically; you'll trigger
        them from the document detail page once their endpoints
        are wired.
      </p>
      <ul>
        {ids.map((id) => {
          const label = _NEXT_STEP_LABELS[id] ?? id;
          return (
            <li
              key={id}
              data-testid={`assessment-plan-next-step-${id}`}
            >
              <button
                type="button"
                className="btn btn--ghost"
                disabled
                aria-disabled="true"
                title="Manual-action endpoint not yet implemented."
              >
                {label}
              </button>
              <small className="muted"> · Coming soon</small>
            </li>
          );
        })}
      </ul>
    </div>
  );
}


/** Generic fallback copy per sample-text status, used ONLY when
 * the backend didn't include any warnings on the LLM assessment
 * payload. The backend's warnings are the source of truth — we
 * render them verbatim under the panel.
 */
function _fallbackSampleTextCopy(status: string): string {
  switch (status) {
    case "empty":
      return "The document appeared empty to the text extractor.";
    case "unsupported":
      return (
        "This file type has no text extractor; sampled text was "
        + "not produced."
      );
    case "garbled":
      return (
        "The text extractor ran but the output is mostly "
        + "non-printable bytes."
      );
    case "unreliable":
      return (
        "Sample text was sparse or the document looks mostly "
        + "scanned; the LLM's recommendation isn't anchored in "
        + "real text."
      );
    default:
      return `Sample text status: ${status}.`;
  }
}


/** Show the sample-text status surfaced by the LLM service. When
 * the extractor produced nothing usable, the operator needs to
 * know the LLM's recommendation came from filename + signals +
 * matched rules — not document content.
 *
 * Renders the BACKEND's warnings verbatim (source of truth). Only
 * falls back to FE-synthesized copy when the backend didn't ship
 * any warning for the non-``available`` status. This avoids
 * duplicating the ``SAMPLE_TEXT_UNRELIABLE_WARNING`` text the
 * backend already emits.
 */
function SampleTextWarning({
  plan,
}: {
  plan: AssessmentPlanResponse;
}) {
  const llm = (plan as { llmAssessment?: unknown }).llmAssessment as
    | {
        sampleTextStatus?: string;
        sampleTextSource?: string;
        warnings?: string[];
      }
    | undefined;
  const status = llm?.sampleTextStatus;
  if (!status || status === "available") return null;
  // Backend-supplied warnings are the source of truth. Filter out
  // empty strings just in case a stub service returns ``[""]``.
  const backendWarnings = (llm?.warnings ?? [])
    .map((w) => (typeof w === "string" ? w.trim() : ""))
    .filter((w) => w.length > 0);
  return (
    <div
      className="assessment-plan-dialog__sample-text-warning"
      data-testid="assessment-plan-sample-text-warning"
    >
      <Banner kind="warn" title="LLM sample text was not reliable">
        {backendWarnings.length > 0 ? (
          <ul>
            {backendWarnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        ) : (
          // Fallback ONLY when the backend didn't ship a warning.
          // Real deployments always do; this exists for legacy
          // service stubs that return an empty warnings array.
          <p>{_fallbackSampleTextCopy(status)}</p>
        )}
      </Banner>
    </div>
  );
}
