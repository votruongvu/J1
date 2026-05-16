/**
 * Assessment Plan panel — pre-compile contract surface.
 *
 * The AssessmentPlan is built BEFORE compile by the
 * `build_initial_execution_plan` activity. The plan + domain pack +
 * enrichment policy are persisted to the `initial_execution_plan`
 * artifact synchronously inside that activity, so this panel can
 * render its primary content the moment the workflow starts — it
 * does NOT have to wait for compile to finish.
 *
 * Primary source: `GET /ingestion-runs/{id}/initial-execution-plan`
 * — the pre-compile artifact. Carries the AssessmentPlan as
 * `compile_plan` (mode + confidence + capabilities + reason) plus
 * the resolved domain profile + enrichment policy + candidate
 * modules.
 *
 * Secondary source: `compile_strategy_report` artifact (POST-compile).
 * When present, the panel additionally renders the post-compile
 * sections: extraction evidence, mode escalation hint, resolved
 * compile config, unhandled capabilities, plan warnings. These all
 * appear under a clearly-labelled "After compile" group so reviewers
 * can tell pre-compile intent from post-compile observation at a
 * glance.
 *
 * Why two fetches: the primary surface MUST not stall behind compile.
 * Earlier versions of this panel read the AssessmentPlan out of the
 * post-compile `compile_strategy_report` exclusively, which left the
 * panel stuck in its "missing" state for the entire compile duration
 * — a bug operators correctly flagged as broken pre-compile rendering.
 */

import { useCallback, useEffect, useState } from "react";
import { Typewriter } from "@/components/Typewriter";
import { useClient } from "@/lib/hooks/useClient";
import { EVENT_TYPES, isTerminalEvent } from "@/lib/constants/events";
import type { ProgressEvent } from "@/types/ingestion";
import type { InitialExecutionPlanPayload } from "@/types/review";
import {
  COMPILE_STRATEGY_REPORT_KIND,
  canonicalRecommendedPath,
  capabilityLabel,
  confidenceBucket,
  contentTypeLabel,
  formatConfidence,
  hasModeEscalation,
  modeDescription,
  recommendedPathDescription,
  recommendedPathFromAssessmentPlan,
  recommendedPathLabel,
  resolvedCompileConfig,
  type AssessmentPlanPayload,
  type CompileStrategyReport,
  type ExtractionEvidence,
} from "./compile-strategy-helpers";

interface AssessmentPlanPanelProps {
  runId: string;
  /**
 * Latest SSE event from the parent page. When relevant events
 * arrive (step lifecycle, run terminal) both fetches re-run so a
 * panel that mounted BEFORE the artifacts existed eventually picks
 * them up when they land. Without this the panel sticks at its
 * mount-time state forever for runs the user navigated into
 * mid-flight.
 */
  latestEvent?: ProgressEvent | null;
}

type PrePlanState =
  | { kind: "loading" }
  | { kind: "missing"; reason: string | null }
  | { kind: "ready"; plan: InitialExecutionPlanPayload }
  | { kind: "error"; message: string };

export function AssessmentPlanPanel({
  runId, latestEvent,
}: AssessmentPlanPanelProps) {
  const client = useClient();
  // Primary: pre-compile initial_execution_plan artifact. This is the
  // source of truth for the AssessmentPlan + domain pack + enrichment
  // policy. Always rendered when available, regardless of compile
  // status.
  const [preState, setPreState] = useState<PrePlanState>({ kind: "loading" });
  // Secondary: post-compile compile_strategy_report. Provides the
  // "after compile" enrichments (extraction evidence, mode escalation,
  // resolved compile config). null when compile hasn't finished or the
  // report hasn't been written yet — that's expected, not an error.
  const [postReport, setPostReport] = useState<CompileStrategyReport | null>(
    null,
  );

  const loadPrePlan = useCallback(() => {
    let cancelled = false;
    void (async () => {
      try {
        const resp = await client.getRunInitialExecutionPlan(runId);
        if (cancelled) return;
        if (resp.status === "completed" && resp.plan) {
          setPreState({ kind: "ready", plan: resp.plan });
        } else {
          // Keep prior ready state across transient refetch misses.
          setPreState((prev) =>
            prev.kind === "ready"
              ? prev
              : { kind: "missing", reason: resp.unavailableReason ?? null },
          );
        }
      } catch (e) {
        if (!cancelled) {
          setPreState((prev) =>
            prev.kind === "ready"
              ? prev
              : {
                  kind: "error",
                  message: e instanceof Error ? e.message : "load failed",
                },
          );
        }
      }
    })();
    return () => { cancelled = true; };
  }, [client, runId]);

  const loadPostReport = useCallback(() => {
    let cancelled = false;
    void (async () => {
      try {
        const page = await client.listRunArtifacts(runId, {
          kind: COMPILE_STRATEGY_REPORT_KIND,
        });
        const artifact = page.items[0];
        if (!artifact) {
          // Don't downgrade an already-loaded report on a transient
          // refetch miss — only clear if we never had one.
          setPostReport((prev) => prev ?? null);
          return;
        }
        const content = await client.getRunArtifactContent(
          runId, artifact.artifactId,
        );
        const text = await content.blob.text();
        const report = JSON.parse(text) as CompileStrategyReport;
        if (!cancelled) setPostReport(report);
      } catch {
        // Compile-strategy report is best-effort enrichment; failing
        // to load it must NOT degrade the pre-compile primary surface.
        // Silently leave `postReport` at its current value.
      }
    })();
    return () => { cancelled = true; };
  }, [client, runId]);

  useEffect(() => {
    setPreState({ kind: "loading" });
    setPostReport(null);
    const cancelPre = loadPrePlan();
    const cancelPost = loadPostReport();
    return () => {
      cancelPre();
      cancelPost();
    };
  }, [loadPrePlan, loadPostReport]);

  // Short retry-polling for the freshly-started-run case. When the
  // panel mounts mid-flight and the SSE event for the assess-compile
  // step has ALREADY fired before subscription (race), the panel
  // would otherwise stick on `missing` until the next event arrives.
  // Poll every 1.5 s for up to 15 s while we're in `missing` state
  // and the latest event indicates the run isn't terminal — the
  // artifact lands within a second of the activity completing in
  // practice, so this catches the eventual-consistency window
  // without becoming a steady-state poll.
  useEffect(() => {
    if (preState.kind !== "missing") return;
    if (latestEvent && isTerminalEvent(latestEvent.event)) return;
    let attempts = 0;
    const MAX_ATTEMPTS = 10;
    const timer = setInterval(() => {
      attempts += 1;
      if (attempts > MAX_ATTEMPTS) {
        clearInterval(timer);
        return;
      }
      loadPrePlan();
    }, 1500);
    return () => clearInterval(timer);
  }, [preState.kind, latestEvent, loadPrePlan]);

  // SSE-driven refresh: re-fetch both artifacts when lifecycle events
  // arrive. The pre-compile plan lands within a second of upload; the
  // post-compile report lands when compile completes. Refreshing on
  // every step.completed / step.failed / step.skipped / run.* covers
  // both timings without a polling loop.
  useEffect(() => {
    if (!latestEvent) return;
    // Refresh on STEP_STARTED too — the NEXT stage's start event
    // fires AFTER the previous stage's workflow-level silent persists
    // complete (compile_strategy_report, compile_result_summary,
    // post_compile_enrich_plan, enrichment_result are all persisted
    // by separate activities that emit no progress signal of their
    // own). Without STEP_STARTED here, each panel only sees its
    // artifact land when the workflow eventually emits some later
    // step.completed — they all populate at once near terminal.
    const refreshOn = new Set<string>([
      EVENT_TYPES.STEP_STARTED,
      EVENT_TYPES.STEP_COMPLETED,
      EVENT_TYPES.STEP_FAILED,
      EVENT_TYPES.STEP_SKIPPED,
    ]);
    if (
      refreshOn.has(latestEvent.event)
      || isTerminalEvent(latestEvent.event)
    ) {
      loadPrePlan();
      loadPostReport();
    }
  }, [latestEvent, loadPrePlan, loadPostReport]);

  if (preState.kind === "loading") {
    return (
      <div className="card" data-testid="assessment-plan-loading">
        <div className="card__body">
          <h3>Assessment Plan</h3>
          <p style={{ color: "var(--text-muted)" }}>
            Profiling document and building AssessmentPlan…
          </p>
        </div>
      </div>
    );
  }
  if (preState.kind === "missing") {
    return (
      <div
        className="card assessment-plan-panel--missing"
        data-testid="assessment-plan-missing"
      >
        <div className="card__body">
          <h3>Assessment Plan</h3>
          <p>
            <span className="muted">
              Waiting for the pre-compile planner to persist the
              initial execution plan for this run.
            </span>
          </p>
          <p style={{ color: "var(--text-muted)", fontSize: 12.5 }}>
            The panel refreshes automatically when each pipeline
            stage completes. If the run is still indexing this is
            expected — give it a few seconds. If the run has
            terminated and this panel stays empty, the
            <code style={{ margin: "0 4px" }}>
              build_initial_execution_plan
            </code>
            activity may have failed to persist —{" "}
            <strong>Technical details</strong> shows the workflow
            history.
          </p>
          {preState.reason && (
            <details
              style={{
                marginTop: 8,
                fontSize: 12,
                color: "var(--text-muted)",
              }}
              data-testid="assessment-plan-missing-reason"
            >
              <summary>Server reason</summary>
              <p style={{ marginTop: 4 }}>{preState.reason}</p>
            </details>
          )}
        </div>
      </div>
    );
  }
  if (preState.kind === "error") {
    return (
      <div className="card" data-testid="assessment-plan-error">
        <div className="card__body">
          <h3>Assessment Plan</h3>
          <p style={{ color: "var(--error-fg)" }}>
            Couldn't load assessment plan: {preState.message}
          </p>
        </div>
      </div>
    );
  }
  return <AssessmentPlanContent plan={preState.plan} report={postReport} />;
}

function AssessmentPlanContent({
  plan, report,
}: {
  plan: InitialExecutionPlanPayload;
  report: CompileStrategyReport | null;
}) {
  // Pre-compile: AssessmentPlan + domain context come from the
  // `initial_execution_plan` payload's `compile_plan` and top-level
  // fields. Always rendered.
  const assessmentPlan = (plan.compile_plan ?? null) as
    | AssessmentPlanPayload
    | null;
  const required = assessmentPlan?.required_capabilities ?? [];
  const optional = assessmentPlan?.optional_capabilities ?? [];
  const risk = assessmentPlan?.risk_flags ?? [];
  const confBucket = confidenceBucket(assessmentPlan?.confidence);
  const recommendedPath = recommendedPathFromAssessmentPlan(assessmentPlan);
  const badgeVariant =
    canonicalRecommendedPath(recommendedPath) ?? recommendedPath;

  // Post-compile: extraction evidence + escalation + resolved config +
  // unhandled capabilities + plan warnings come from the
  // `compile_strategy_report` when present. Each block guards on
  // `report` so it only renders once the post-compile artifact exists.
  const escalated = report ? hasModeEscalation(report) : false;
  const mappedConfig = report ? resolvedCompileConfig(report) : null;
  const extraction: ExtractionEvidence | null =
    report?.extraction_evidence ?? null;
  const unhandled = report?.unhandled_capabilities ?? [];
  const planWarnings = report?.plan_warnings ?? [];

  return (
    <div
      className="card assessment-plan-panel"
      data-testid="assessment-plan-panel"
    >
      <div className="assessment-plan-panel__head">
        <h3>Assessment Plan</h3>
        <span
          className="assessment-plan-panel__source"
          data-testid="assessment-plan-source"
        >
          {assessmentPlan?.mode
            ? "rule-based assessor"
            : "pre-compile (no compile_plan attached)"}
        </span>
      </div>

      {/* Hero: recommended path. Derived from the pre-compile
 AssessmentPlan, so it's available the moment the workflow
 finishes building the InitialExecutionPlan — long before
 compile starts. */}
      <div
        className="assessment-plan-panel__recommendation"
        data-testid="assessment-plan-recommendation"
      >
        <div className="assessment-plan-panel__label">Recommended path</div>
        <span
          className={`badge recommended-path-badge recommended-path-badge--${badgeVariant}`}
          data-testid="assessment-plan-recommended-path"
        >
          {recommendedPathLabel(recommendedPath)}
        </span>
        <div className="assessment-plan-panel__hint">
          <Typewriter
            text={recommendedPathDescription(recommendedPath)}
            speed={120}
            cursor
          />
        </div>
      </div>

      {/* Domain pack + enrichment policy. Top-level fields on the
 initial_execution_plan payload — operator-facing context the
 panel showed before the InitialExecutionPlanPanel was folded in. */}
      <dl className="kv assessment-plan-panel__kv">
        <dt>Domain profile</dt>
        <dd data-testid="assessment-plan-domain">
          <span className="badge">{plan.domain_profile_id ?? "general"}</span>
        </dd>
        <dt>Post-compile domain enrichment policy</dt>
        <dd data-testid="assessment-plan-policy">
          <span
            className={`badge enrich-policy enrich-policy--${plan.enrichment_policy ?? "auto"}`}
          >
            {policyLabel(plan.enrichment_policy)}
          </span>
        </dd>
        <dt>Require post-compile enrichment success</dt>
        <dd data-testid="assessment-plan-require-success">
          {plan.require_enrichment_success
            ? "Yes — a failed post-compile enrichment will fail the run"
            : "No — post-compile enrichment is best-effort"}
        </dd>
      </dl>

      {/* Hero: mode + confidence side by side. From the AssessmentPlan
 in the pre-compile artifact — never gated on compile. */}
      {assessmentPlan && (
        <div className="assessment-plan-panel__hero">
          <div className="assessment-plan-panel__hero-item">
            <div className="assessment-plan-panel__label">Selected mode</div>
            <span
              className={`badge mode-badge mode-badge--lg mode-badge--${assessmentPlan.mode ?? "unknown"}`}
              data-testid="assessment-plan-mode"
            >
              {assessmentPlan.mode ?? "—"}
            </span>
            <div
              className="assessment-plan-panel__hint"
              data-testid="assessment-plan-mode-description"
            >
              {modeDescription(assessmentPlan.mode)}
            </div>
          </div>
          <div className="assessment-plan-panel__hero-item">
            <div className="assessment-plan-panel__label">Confidence</div>
            <span
              className={`badge confidence-badge confidence-badge--${confBucket}`}
              data-testid="assessment-plan-confidence"
            >
              {formatConfidence(assessmentPlan.confidence)}
            </span>
            <div className="assessment-plan-panel__hint">
              {confBucket === "low" && "Operator review recommended."}
              {confBucket === "medium" && "Some signals were ambiguous."}
              {confBucket === "high" && "Strong signals; planner is confident."}
              {confBucket === "unknown" && "Confidence not reported."}
            </div>
          </div>
        </div>
      )}

      {/* Profile signals + capabilities. Derived from the AssessmentPlan;
 only rendered when the plan is actually attached. */}
      {assessmentPlan && (
        <>
          <dl className="kv assessment-plan-panel__kv">
            <dt>Document type</dt>
            <dd>{assessmentPlan.document_type ?? "—"}</dd>
            <dt>Complexity</dt>
            <dd>{assessmentPlan.complexity ?? "—"}</dd>
            <dt>Fallback policy</dt>
            <dd className="mono">{assessmentPlan.fallback_policy ?? "—"}</dd>
            <dt>Reason</dt>
            <dd data-testid="assessment-plan-reason">
              {assessmentPlan.reason || "—"}
            </dd>
          </dl>

          <div className="assessment-plan-panel__caps">
            <div>
              <div className="assessment-plan-panel__label">
                Required capabilities ({required.length})
              </div>
              {required.length === 0 ? (
                <span className="muted">—</span>
              ) : (
                <CapabilityPills caps={required} variant="required" />
              )}
            </div>
            <div>
              <div className="assessment-plan-panel__label">
                Optional capabilities ({optional.length})
              </div>
              {optional.length === 0 ? (
                <span className="muted">—</span>
              ) : (
                <CapabilityPills caps={optional} variant="optional" />
              )}
            </div>
          </div>

          {risk.length > 0 && (
            <div
              className="assessment-plan-panel__risks"
              data-testid="assessment-plan-risks"
            >
              <div className="assessment-plan-panel__label">
                Risk flags ({risk.length})
              </div>
              <ul className="bullet-list">
                {risk.map((r, i) => <li key={i}>{capabilityLabel(r)}</li>)}
              </ul>
            </div>
          )}
        </>
      )}

      {/* ───────── After compile (secondary) ─────────────────────────
   Everything below depends on the `compile_strategy_report`
   artifact — only renders once compile has completed. Reviewers
   can tell pre-compile intent (above) from post-compile
   observation (here) at a glance. */}
      {report && (escalated || mappedConfig || extraction
                  || unhandled.length > 0 || planWarnings.length > 0) && (
        <div
          className="assessment-plan-panel__post-compile"
          data-testid="assessment-plan-post-compile"
        >
          <div className="assessment-plan-panel__label">After compile</div>

          {escalated && (
            <div
              className="assessment-plan-panel__escalation"
              data-testid="assessment-plan-escalation"
            >
              <strong>Compile-safety retry escalated mode:</strong>{" "}
              <span className="mono">{report!.initial_mode}</span> →{" "}
              <span className="mono">{report!.final_mode}</span>
            </div>
          )}

          {extraction && (
            <div
              className="assessment-plan-panel__extraction"
              data-testid="assessment-plan-extraction"
            >
              <div className="assessment-plan-panel__label">
                Extraction evidence
              </div>
              <dl className="kv">
                <dt>Parser</dt>
                <dd className="mono">{extraction.parser ?? "raganything"}</dd>
                <dt>Parser method</dt>
                <dd className="mono">{extraction.parser_method ?? "—"}</dd>
                <dt>Text characters</dt>
                <dd data-testid="assessment-plan-text-char-count">
                  {extraction.text_char_count == null
                    ? "—"
                    : extraction.text_char_count.toLocaleString()}
                </dd>
                <dt>Content blocks</dt>
                <dd data-testid="assessment-plan-content-block-count">
                  {extraction.content_block_count == null
                    ? "—"
                    : extraction.content_block_count.toLocaleString()}
                </dd>
                <dt>Pages</dt>
                <dd>
                  {extraction.page_count == null
                    ? "—"
                    : extraction.page_count.toLocaleString()}
                </dd>
                <dt>Detected content</dt>
                <dd data-testid="assessment-plan-detected-types">
                  {(extraction.detected_content_types ?? []).length === 0 ? (
                    <span className="muted">—</span>
                  ) : (
                    <div className="cap-pills">
                      {(extraction.detected_content_types ?? []).map((c) => (
                        <span key={c} className="badge cap-pill">
                          {contentTypeLabel(c)}
                        </span>
                      ))}
                    </div>
                  )}
                </dd>
                <dt>Chunking status</dt>
                <dd data-testid="assessment-plan-chunking-status">
                  <span
                    className="badge chunking-status-badge"
                    title="Chunks are verified separately during compile/index — not by this probe."
                  >
                    {(extraction.chunking_status === "pending_verification")
                      ? "Pending — verified during compile/index"
                      : (extraction.chunking_status ?? "Pending verification")}
                  </span>
                </dd>
              </dl>
            </div>
          )}

          {mappedConfig && (mappedConfig.parse_method || mappedConfig.assessment_mode) && (
            <div
              className="assessment-plan-panel__config"
              data-testid="assessment-plan-config"
            >
              <div className="assessment-plan-panel__label">
                Resolved compile config
              </div>
              <dl className="kv">
                <dt>parse_method</dt>
                <dd className="mono">{mappedConfig.parse_method ?? "—"}</dd>
                <dt>assessment_mode</dt>
                <dd className="mono">{mappedConfig.assessment_mode ?? "—"}</dd>
              </dl>
            </div>
          )}

          {unhandled.length > 0 && (
            <div
              className="assessment-plan-panel__unhandled"
              data-testid="assessment-plan-unhandled"
            >
              <div className="assessment-plan-panel__label">
                Unhandled capabilities ({unhandled.length})
              </div>
              <p className="muted">
                The deployment couldn't honour these requested capabilities.
                Compile may have produced lower-quality output for these areas.
              </p>
              <CapabilityPills caps={unhandled} variant="unhandled" />
            </div>
          )}

          {planWarnings.length > 0 && (
            <div
              className="assessment-plan-panel__warnings"
              data-testid="assessment-plan-warnings"
            >
              <div className="assessment-plan-panel__label">
                Plan warnings ({planWarnings.length})
              </div>
              <ul className="bullet-list">
                {planWarnings.map((w, i) => <li key={i}>{w}</li>)}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function CapabilityPills({
  caps,
  variant,
}: {
  caps: string[];
  variant: "required" | "optional" | "unhandled";
}) {
  return (
    <div className="cap-pills">
      {caps.map((c) => (
        <span
          key={c}
          className={`badge cap-pill cap-pill--${variant}`}
        >
          {capabilityLabel(c)}
        </span>
      ))}
    </div>
  );
}

function policyLabel(policy: string | null | undefined): string {
  if (!policy) return "auto";
  switch (policy) {
    case "auto":
      return "Auto — decide per-run from compile signals";
    case "always":
      return (
        "Always — run post-compile domain enrichment whenever "
        + "compile succeeds"
      );
    case "never":
      return "Never — post-compile domain enrichment is disabled";
    default:
      return policy;
  }
}

// Re-export the panel type so tests can reference it.
export type { AssessmentPlanPayload };
