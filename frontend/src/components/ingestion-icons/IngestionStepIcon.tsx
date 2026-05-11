/**
 * `<IngestionStepIcon />` — single drop-in component for every
 * place the J1 UI shows an ingestion step's progress.
 *
 * <IngestionStepIcon
 * step="parse_source_content"
 * status="running"
 * size="md"
 * />
 *
 * The `step` prop accepts the canonical user-facing id
 * (`parse_source_content`, …) or any internal synonym that
 * `internalStepToUserFacing` knows how to fold (`compile`,
 * `plan.revised`, `chunks`, etc.). Unknown ids fall back to a
 * neutral pixel-frame with a status badge so the UI never breaks.
 *
 * Status drives both the per-icon animation and the frame's tint
 * + corner badge:
 * * `running` — animated icon, no badge
 * * `completed` — paused icon, green ✓ badge
 * * `pending` — paused, desaturated, no badge
 * * `skipped` — paused, dimmer than pending
 * * `failed` — paused, sepia, amber ! badge
 */

import { type CSSProperties, type ComponentType } from "react";
import {
  internalStepToUserFacing,
  type ProcessingStepId,
} from "@/lib/processing-steps";
import {
  ChunkIcon,
  EnrichIcon,
  FinalizeIcon,
  GraphIcon,
  type IconState,
  type IngestionIconProps,
  InventoryIcon,
  ParseIcon,
  PlanIcon,
} from "./icons";

export type { IconState, IconSize } from "./icons";

/**
 * Canonical step id → icon component. The `Record` typing means a
 * new step id added to `ProcessingStepId` is a TypeScript error
 * here until the icon mapping is filled in.
 */
export const INGESTION_STEP_ICONS: Record<
  ProcessingStepId,
  ComponentType<IngestionIconProps>
> = {
  // Pre-compile assessment stage — reuses the planning glyph since
  // the AssessmentPlan IS what used to be called the plan.
  assess_compile_strategy: PlanIcon,
  parse_source_content: ParseIcon,
  build_content_inventory: InventoryIcon,
  // Post-compile rule-based enrich assessment — reuses the planning
  // glyph for visual continuity with the pre-compile assessment.
  assess_enrichment: PlanIcon,
  generate_knowledge_chunks: ChunkIcon,
  enrich_extracted_content: EnrichIcon,
  build_knowledge_graph: GraphIcon,
  finalize_ingestion: FinalizeIcon,
};

/** Status vocabulary the UI passes in. Mirrors `ProcessingStepStatus`
 * from `@/lib/processing-steps`. */
export type StepStatus =
  | "running"
  | "completed"
  | "pending"
  | "skipped"
  | "failed";

interface IngestionStepIconProps {
  /**
 * Canonical id (e.g. `parse_source_content`) OR any internal
 * synonym (e.g. `compile`, `plan.revised`, `chunks`). The
 * normaliser folds synonyms; unknown strings fall back to a
 * neutral frame.
 */
  step: string | ProcessingStepId | null | undefined;
  status?: StepStatus;
  size?: IngestionIconProps["size"];
  flat?: boolean;
  className?: string;
  style?: CSSProperties;
  /** Override accessible label. Defaults derive from the step id. */
  ariaLabel?: string;
}

export function IngestionStepIcon({
  step,
  status = "pending",
  size = "md",
  flat = false,
  className,
  style,
  ariaLabel,
}: IngestionStepIconProps) {
  const id = internalStepToUserFacing(step ?? null);
  const Icon = id ? INGESTION_STEP_ICONS[id] : null;
  const frameState: IconState = mapStatus(status);

  if (Icon === null) {
    // Unknown step name — render a neutral pixel frame with the
    // status badge so the UI degrades gracefully without crashing.
    return (
      <UnknownStepIcon
        state={frameState}
        size={size}
        flat={flat}
        className={className}
        style={style}
        ariaLabel={ariaLabel ?? `Step state: ${status}`}
      />
    );
  }

  return (
    <Icon
      state={frameState}
      size={size}
      flat={flat}
      className={className}
      style={style}
      ariaLabel={ariaLabel}
    />
  );
}

function mapStatus(status: StepStatus): IconState {
  // Status vocabulary maps 1:1 onto the design's icon-frame state
  // names — pinning the mapping here means future additions to the
  // status set must edit one place.
  switch (status) {
    case "running":
      return "running";
    case "completed":
      return "completed";
    case "failed":
      return "failed";
    case "skipped":
      return "skipped";
    case "pending":
    default:
      return "pending";
  }
}

/**
 * Neutral fallback frame for unknown step names. Renders the
 * standard pixel-CRT shell + state badge, but leaves the SVG
 * intentionally empty — the badge alone conveys status, which is
 * the right behaviour for "we don't know this step yet".
 */
function UnknownStepIcon({
  state,
  size,
  flat,
  className,
  style,
  ariaLabel,
}: {
  state: IconState;
  size: IngestionIconProps["size"];
  flat?: boolean;
  className?: string;
  style?: CSSProperties;
  ariaLabel: string;
}) {
  // Reuse the exact frame markup from the icon module by importing
  // a "blank" icon — it's just an empty SVG inside the IconFrame.
  // Cheaper than duplicating the frame styles here, and stays in
  // sync if the design ever updates the frame chrome.
  const sizeClass = typeof size === "number" ? "" : `size-${size}`;
  const inlineStyle: CSSProperties =
    typeof size === "number"
      ? { ...style, ["--pix-size" as string]: `${size}px` }
      : style ?? {};
  const cls = [
    "pix-icon-frame",
    sizeClass,
    `state-${state}`,
    flat ? "flat" : "",
    className ?? "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div className={cls} style={inlineStyle} role="img" aria-label={ariaLabel}>
      <svg viewBox="0 0 32 32" aria-hidden="true">
        {/* Soft "?" pixel glyph — neutral fallback content. */}
        <rect x="13" y="9" width="6" height="2" fill="#3a3f55" />
        <rect x="11" y="11" width="2" height="2" fill="#3a3f55" />
        <rect x="19" y="11" width="2" height="2" fill="#3a3f55" />
        <rect x="19" y="13" width="2" height="3" fill="#3a3f55" />
        <rect x="17" y="15" width="2" height="2" fill="#3a3f55" />
        <rect x="15" y="17" width="2" height="3" fill="#3a3f55" />
        <rect x="15" y="22" width="2" height="2" fill="#3a3f55" />
      </svg>
      {state === "completed" && (
        <div className="pix-state-badge" aria-hidden="true">
          <svg viewBox="0 0 12 12">
            <rect x="3" y="6" width="1" height="1" fill="#fff" />
            <rect x="4" y="7" width="1" height="1" fill="#fff" />
            <rect x="5" y="8" width="1" height="1" fill="#fff" />
            <rect x="6" y="7" width="1" height="1" fill="#fff" />
            <rect x="7" y="6" width="1" height="1" fill="#fff" />
            <rect x="8" y="5" width="1" height="1" fill="#fff" />
          </svg>
        </div>
      )}
      {state === "failed" && (
        <div className="pix-state-badge warn" aria-hidden="true">
          <svg viewBox="0 0 12 12">
            <rect x="5" y="3" width="2" height="4" fill="#fff" />
            <rect x="5" y="8" width="2" height="2" fill="#fff" />
          </svg>
        </div>
      )}
    </div>
  );
}
