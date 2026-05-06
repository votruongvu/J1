/**
 * Plan card — groups steps by stage, shows the confirmation gate when
 * the run is in PLAN_READY / WAITING_FOR_CONFIRMATION, and overlays
 * runtime status (running / completed / failed) on each step.
 */

import type { ExecutionPlan, IngestionRun, PlanStep, Stage } from "@/types/ingestion";
import type { RuntimeStepStatus } from "@/types/ui";
import { Icon } from "@/components/icons";
import { CostBadge, DecisionBadge, EngineBadge, RiskBadge } from "@/components/badges";

interface PlanCardProps {
  plan: ExecutionPlan | null;
  run: IngestionRun | null;
  runtimeStepStatus?: Record<string, RuntimeStepStatus>;
  onConfirm: () => void;
  confirming: boolean;
}

export function PlanCard({
  plan,
  run,
  runtimeStepStatus,
  onConfirm,
  confirming,
}: PlanCardProps) {
  if (!plan) {
    return (
      <div className="card">
        <div className="card__header">
          <div>
            <h3 className="card__title">Execution plan</h3>
            <p className="card__subtitle">Generating plan…</p>
          </div>
        </div>
        <div className="card__body">
          <div style={{ display: "grid", gap: 10 }}>
            {[0, 1, 2, 3].map((i) => (
              <div
                key={i}
                style={{ height: 56, borderRadius: 8, background: "var(--bg-sunken)" }}
              />
            ))}
          </div>
        </div>
      </div>
    );
  }

  const showConfirm =
    !!run && (run.status === "PLAN_READY" || run.status === "WAITING_FOR_CONFIRMATION");
  const stages = plan.summary.stages;
  const grouped = stages.map((stage) => ({
    stage,
    steps: plan.steps.filter((s) => s.stage === stage),
  }));

  return (
    <div className="card">
      <div className="card__header">
        <div>
          <h3 className="card__title">Execution plan</h3>
          <p className="card__subtitle">
            {plan.summary.total} steps · {plan.summary.run} run · {plan.summary.skip} skip ·{" "}
            {plan.summary.conditional} conditional
          </p>
        </div>
        <span className="badge badge--outline mono">{plan.runId}</span>
      </div>

      <div className="card__body">
        {showConfirm && (
          <div className="confirm-bar">
            <div className="confirm-bar__text">
              <strong>Plan is ready.</strong> Review the steps below and confirm to begin
              execution.
            </div>
            <button className="btn btn--primary" onClick={onConfirm} disabled={confirming}>
              {confirming ? (
                <>
                  <Icon.Loader className="icon-sm" /> Confirming…
                </>
              ) : (
                <>
                  <Icon.Check className="icon-sm" /> Confirm & run
                </>
              )}
            </button>
          </div>
        )}

        <div className="plan-summary">
          <div className="plan-summary__item">
            <span className="plan-summary__num">{plan.summary.run}</span> Run
          </div>
          <div className="plan-summary__divider" />
          <div className="plan-summary__item">
            <span className="plan-summary__num">{plan.summary.skip}</span> Skip
          </div>
          <div className="plan-summary__divider" />
          <div className="plan-summary__item">
            <span className="plan-summary__num">{plan.summary.conditional}</span> Conditional
          </div>
          <div className="plan-summary__divider" />
          <div className="plan-summary__item">
            <Icon.Layers className="icon-sm" /> {stages.length} stages
          </div>
        </div>

        {/* Vision / premium badges. Vision is OFF by default — when
            it flips on, operators need to see *why* (the per-step
            reason chips downstream answer that). */}
        <div className="plan-flags" style={{ display: "flex", gap: 8, marginBottom: 12 }}>
          <span
            className={`badge ${plan.requires_vision ? "badge--warning" : "badge--outline"}`}
            title={
              plan.requires_vision
                ? "At least one enabled step needs the vision LLM."
                : "No vision LLM calls planned for this run."
            }
          >
            <Icon.Eye className="icon-sm" /> Vision LLM:{" "}
            {plan.requires_vision ? "On" : "Off"}
          </span>
          <span
            className={`badge ${
              plan.requires_premium_llm ? "badge--warning" : "badge--outline"
            }`}
            title={
              plan.requires_premium_llm
                ? "At least one enabled step uses the premium LLM class."
                : "Run uses fast/standard models only."
            }
          >
            <Icon.Spark className="icon-sm" /> Premium LLM:{" "}
            {plan.requires_premium_llm ? "On" : "Off"}
          </span>
        </div>

        {grouped.map((g) => (
          <StageGroup
            key={g.stage}
            stage={g.stage}
            steps={g.steps}
            runtime={runtimeStepStatus}
          />
        ))}
      </div>
    </div>
  );
}

interface StageGroupProps {
  stage: Stage;
  steps: PlanStep[];
  runtime?: Record<string, RuntimeStepStatus>;
}

function StageGroup({ stage, steps, runtime }: StageGroupProps) {
  return (
    <div className="stage-group">
      <div className="stage-group__head">
        <span className={`stage-group__chip stage-group__chip--${stage.toLowerCase()}`}>
          {stage}
        </span>
        <div className="stage-group__line" />
        <span className="stage-group__count">{steps.length} STEPS</span>
      </div>
      <div className="steps-grid">
        {steps.map((step) => (
          <PlanStepCard key={step.id} step={step} runtimeStatus={runtime?.[step.id]} />
        ))}
      </div>
    </div>
  );
}

interface PlanStepCardProps {
  step: PlanStep;
  runtimeStatus?: RuntimeStepStatus;
}

function PlanStepCard({ step, runtimeStatus }: PlanStepCardProps) {
  const isHighRiskSkip = step.decision === "SKIP" && step.risk_level === "HIGH";
  const cls = [
    "step-card",
    step.decision === "RUN" && "step-card--run",
    step.decision === "SKIP" && "step-card--skip",
    step.decision === "CONDITIONAL" && "step-card--conditional",
    isHighRiskSkip && "step-card--high-risk-skip",
    runtimeStatus === "running" && "step-card--running",
    runtimeStatus === "completed" && "step-card--completed",
    runtimeStatus === "failed" && "step-card--failed",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={cls}>
      <div className="step-card__head">
        <div className="step-card__name">{step.name}</div>
        <DecisionBadge decision={step.decision} />
      </div>
      <div className="step-card__reason">{step.reason}</div>
      <div className="step-card__meta">
        <RiskBadge level={step.risk_level} />
        <CostBadge tier={step.estimated_cost_tier} />
        <EngineBadge engine={step.expected_engine} provider={step.expected_provider} />
        {step.llm_class && step.llm_class !== "none" && (
          <span
            className={`badge ${
              step.llm_class === "premium" ? "badge--warning" : "badge--outline"
            }`}
            title={`LLM class: ${step.llm_class}`}
          >
            LLM · {step.llm_class}
          </span>
        )}
        {runtimeStatus === "running" && (
          <span className="badge badge--info badge--running">
            <span className="dot" /> Running
          </span>
        )}
        {runtimeStatus === "completed" && (
          <span className="badge badge--success">
            <span className="dot" /> Done
          </span>
        )}
        {runtimeStatus === "failed" && (
          <span className="badge badge--error">
            <span className="dot" /> Failed
          </span>
        )}
      </div>
      {step.warning && (
        <div className="step-card__warn">
          <Icon.Alert className="icon-sm" /> {step.warning}
        </div>
      )}
    </div>
  );
}
