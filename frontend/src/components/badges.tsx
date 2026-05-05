/**
 * Compact badge primitives used across the run-detail and list views.
 *
 * Every label / colour decision is delegated to `lib/display.ts` —
 * components only consume the typed lookup results.
 */

import type { CostTier, Decision, RiskLevel, RunStatus } from "@/types/ingestion";
import { decisionMeta, statusMeta } from "@/lib/display";

export function StatusBadge({ status }: { status: RunStatus | string }) {
  const meta = statusMeta(status);
  const cls = `badge badge--${meta.tone}` + (meta.pulse ? " badge--running" : "");
  return (
    <span className={cls}>
      <span className="dot" /> {meta.label}
    </span>
  );
}

export function DecisionBadge({ decision }: { decision: Decision | string }) {
  const meta = decisionMeta(decision);
  return <span className={`decision ${meta.className}`}>{meta.label}</span>;
}

export function RiskBadge({ level }: { level: RiskLevel | string }) {
  const tone = level === "HIGH" ? "warning" : level === "MEDIUM" ? "info" : "neutral";
  const text = typeof level === "string" ? level.toLowerCase() : "";
  return <span className={`badge badge--${tone}`}>Risk · {text}</span>;
}

const COST_LABELS: Record<string, string> = {
  S: "Cost · S",
  M: "Cost · M",
  L: "Cost · L",
  LOW: "Cost · S",
  MEDIUM: "Cost · M",
  HIGH: "Cost · L",
  NONE: "Cost · —",
};

export function CostBadge({ tier }: { tier: CostTier | string }) {
  const label = COST_LABELS[tier] ?? `Cost · ${tier}`;
  return <span className="badge badge--outline">{label}</span>;
}

export function EngineBadge({
  engine,
  provider,
}: {
  engine?: string | null;
  provider?: string | null;
}) {
  if (!engine && !provider) return null;
  const text = provider && engine ? `${engine} · ${provider}` : (engine ?? provider ?? "");
  return <span className="badge badge--outline mono">{text}</span>;
}

export function ProgressBar({
  value,
  current,
  total,
}: {
  value: number;
  current?: number | null;
  total?: number | null;
}) {
  return (
    <div className="tl-item__progress">
      <div className="tl-item__progress-bar">
        <div
          className="tl-item__progress-fill"
          style={{ width: `${Math.round(value * 100)}%` }}
        />
      </div>
      <div className="tl-item__progress-text">
        {current != null && total != null
          ? `${current}/${total}`
          : `${Math.round(value * 100)}%`}
      </div>
    </div>
  );
}
