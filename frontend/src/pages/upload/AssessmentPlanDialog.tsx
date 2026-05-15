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
}


export function AssessmentPlanDialog({
  filename,
  plan,
  loadError,
  onConfirm,
  onCancel,
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
