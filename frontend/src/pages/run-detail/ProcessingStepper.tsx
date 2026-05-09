/**
 * Run Detail > Processing Stepper.
 *
 * Renders the seven canonical user-facing steps as a horizontal
 * stepper with per-step status (pending / running / completed /
 * skipped / failed). Status is derived from a combination of:
 *
 *   * SSE step.* events (running / completed / failed / skipped)
 *   * `availableViews.*` from the run summary (artifacts present)
 *   * `executionPlan.steps[*].enabled` from the post-compile plan
 *
 * Internal step names (`compile`, `enrich`, …) are mapped to the
 * canonical user-facing ids via `internalStepToUserFacing`. The
 * stepper never shows the legacy "Initial Plan" — `Create Execution
 * Plan` is the planning step the user sees.
 */

import { useEffect, useMemo, useState } from "react";
import { IngestionStepIcon } from "@/components/ingestion-icons";
import { useClient } from "@/lib/hooks/useClient";
import { EVENT_TYPES, isTerminalEvent } from "@/lib/constants/events";
import {
  PROCESSING_STEPS,
  internalStepToUserFacing,
  type ProcessingStepId,
  type ProcessingStepStatus,
} from "@/lib/processing-steps";
import type { ProgressEvent } from "@/types/ingestion";
import type { PlanningResult, ReviewRunSummary } from "@/types/review";

interface ProcessingStepperProps {
  runId: string;
  events: ProgressEvent[];
  /** Most recent SSE event, used to drive a summary refresh. */
  latestEvent: ProgressEvent | null;
}

export function ProcessingStepper({
  runId,
  events,
  latestEvent,
}: ProcessingStepperProps) {
  const client = useClient();
  const [summary, setSummary] = useState<ReviewRunSummary | null>(null);
  const [planning, setPlanning] = useState<PlanningResult | null>(null);

  // Reset when the run id changes — guards against showing stale
  // status when the user navigates between runs.
  useEffect(() => {
    setSummary(null);
    setPlanning(null);
  }, [runId]);

  // Cheap refresh on every "interesting" SSE event. Summary is a
  // small response; the cost of refetching is negligible and the
  // step statuses depend on `availableViews.*` from the summary.
  useEffect(() => {
    let cancelled = false;
    const refreshOn = new Set<string>([
      EVENT_TYPES.STEP_COMPLETED,
      EVENT_TYPES.STEP_FAILED,
      EVENT_TYPES.STEP_SKIPPED,
      EVENT_TYPES.PLAN_GENERATED,
      EVENT_TYPES.PLAN_REVISED,
    ]);
    const shouldRefresh =
      latestEvent === null ||
      refreshOn.has(latestEvent.event) ||
      isTerminalEvent(latestEvent.event);
    if (!shouldRefresh) return;
    void (async () => {
      try {
        const next = await client.getRunSummary(runId);
        if (!cancelled) setSummary(next);
      } catch {
        /* tolerable — the stepper still works off events alone. */
      }
      try {
        const plan = await client.getRunPlanning(runId);
        if (!cancelled) setPlanning(plan);
      } catch {
        /* legacy run / endpoint missing — stepper degrades gracefully. */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, runId, latestEvent]);

  const statusByStep = useMemo(
    () => deriveStatuses(events, summary, planning),
    [events, summary, planning],
  );

  return (
    <section className="card processing-stepper">
      <div className="card__header">
        <div>
          <h3 className="card__title">Processing journey</h3>
          <p className="card__subtitle">
            Each step unlocks its result tab as soon as it completes.
          </p>
        </div>
      </div>
      <div className="card__body">
        <ol className="processing-stepper__list">
          {PROCESSING_STEPS.map((step) => {
            const status = statusByStep[step.id] ?? "pending";
            return (
              <li
                key={step.id}
                className={`processing-stepper__item processing-stepper__item--${status}`}
              >
                <div className="processing-stepper__icon">
                  <IngestionStepIcon
                    step={step.id}
                    status={status}
                    size="md"
                    ariaLabel={`${step.label} — ${status}`}
                  />
                </div>
                <div className="processing-stepper__body">
                  <div className="processing-stepper__title">{step.label}</div>
                  <div className="processing-stepper__desc">
                    {step.description}
                  </div>
                  <StatusPill status={status} />
                </div>
              </li>
            );
          })}
        </ol>
      </div>
    </section>
  );
}

function StatusPill({ status }: { status: ProcessingStepStatus }) {
  const labels: Record<ProcessingStepStatus, string> = {
    pending: "Pending",
    running: "Running",
    completed: "Completed",
    skipped: "Skipped",
    failed: "Failed",
  };
  return (
    <span className={`processing-stepper__pill processing-stepper__pill--${status}`}>
      {labels[status]}
    </span>
  );
}

// ---- Status derivation ----------------------------------------------

function deriveStatuses(
  events: ProgressEvent[],
  summary: ReviewRunSummary | null,
  planning: PlanningResult | null,
): Record<ProcessingStepId, ProcessingStepStatus> {
  const out: Record<ProcessingStepId, ProcessingStepStatus> = {
    parse_source_content: "pending",
    build_content_inventory: "pending",
    create_execution_plan: "pending",
    generate_knowledge_chunks: "pending",
    enrich_extracted_content: "pending",
    build_knowledge_graph: "pending",
    finalize_ingestion: "pending",
  };

  // 1. Walk events and project step.* events onto the user-facing
  // step ids. Last event wins per step (failure > completion >
  // running > pending).
  for (const e of events) {
    const stepRaw = (e.data?.step as string | undefined) ?? null;
    const id = internalStepToUserFacing(stepRaw);
    if (!id) {
      // Plan events drive the planning step regardless of `step`.
      if (e.event === EVENT_TYPES.PLAN_REVISED) {
        out.create_execution_plan = "completed";
      }
      if (e.event === EVENT_TYPES.RUN_COMPLETED) {
        out.finalize_ingestion = "completed";
      }
      if (e.event === EVENT_TYPES.RUN_FAILED) {
        out.finalize_ingestion = "failed";
      }
      continue;
    }
    if (e.event === EVENT_TYPES.STEP_STARTED) {
      // Don't downgrade `completed` → `running` on retries.
      if (out[id] === "pending") out[id] = "running";
    } else if (e.event === EVENT_TYPES.STEP_COMPLETED) {
      out[id] = "completed";
    } else if (e.event === EVENT_TYPES.STEP_FAILED) {
      out[id] = "failed";
    } else if (e.event === EVENT_TYPES.STEP_SKIPPED) {
      out[id] = "skipped";
    }
  }

  // 2. Artifact-driven completions for steps that aren't fired as
  // independent step.* events:
  //   * Build Content Inventory → parsed_content_manifest artifact
  //     present (== `availableViews.parsedContent.available`).
  //   * Generate Knowledge Chunks → chunk artifacts present.
  //   * Create Execution Plan → planning artifact present (audit-log
  //     fallback also counts).
  const views = summary?.availableViews;
  if (views?.parsedContent?.available) {
    out.build_content_inventory = "completed";
  }
  if (views?.chunks?.available) {
    out.generate_knowledge_chunks = "completed";
  }
  if (views?.planning?.available || planning?.status === "completed") {
    out.create_execution_plan = "completed";
  }
  if (views?.assets?.available) {
    out.enrich_extracted_content = "completed";
  }
  if (views?.graph?.available) {
    out.build_knowledge_graph = "completed";
  }

  // 3. Plan-driven `skipped` propagation. When the Execution Plan
  // explicitly disables a step, render it as `skipped` rather than
  // leaving it `pending` indefinitely.
  const planSteps = (planning?.executionPlan?.steps ?? null) as
    | Record<string, { enabled?: boolean; reason?: string }>
    | null;
  if (planSteps) {
    if (planSteps.vision_enrichment?.enabled === false ||
        planSteps.image_captioning?.enabled === false) {
      // Use enrich step as the proxy: enrichment is skipped only
      // when ALL enrich-shaped sub-steps are disabled.
      const allOff =
        (planSteps.vision_enrichment?.enabled ?? true) === false &&
        (planSteps.image_captioning?.enabled ?? true) === false &&
        (planSteps.table_enrichment?.enabled ?? true) === false &&
        (planSteps.requirement_extraction?.enabled ?? true) === false &&
        (planSteps.risk_extraction?.enabled ?? true) === false &&
        (planSteps.quality_assessment?.enabled ?? true) === false;
      if (allOff && out.enrich_extracted_content === "pending") {
        out.enrich_extracted_content = "skipped";
      }
    }
    if (
      planSteps.graph_extraction?.enabled === false &&
      out.build_knowledge_graph === "pending"
    ) {
      out.build_knowledge_graph = "skipped";
    }
  }

  // 4. Terminal-status fallbacks. When the run is terminal but an
  // intermediate step never reported, mark it `skipped` so the
  // stepper doesn't show an active spinner forever.
  const isTerminal =
    summary?.status === "succeeded" ||
    summary?.status === "succeeded_with_warnings" ||
    summary?.status === "failed" ||
    summary?.status === "cancelled";
  if (isTerminal) {
    for (const id of Object.keys(out) as ProcessingStepId[]) {
      if (out[id] === "running" || out[id] === "pending") {
        out[id] = id === "finalize_ingestion"
          ? (summary?.status === "failed" ? "failed" : "completed")
          : "skipped";
      }
    }
  }

  return out;
}
