/**
 * Results > Manual Query Trace tab.
 *
 * Operator surface for the SmartQueryOrchestrator pipeline. Renders
 * the full QueryTrace JSON returned by ``POST /dev/query-trace`` so
 * an operator can answer "why did this query fail" at a glance:
 *
 *   * Status banner (pass / fail / insufficient)
 *   * Detected intent + retrieval plan
 *   * Routes executed with timings
 *   * Candidate table (kept vs dropped, with reasons)
 *   * Evidence groups covered / missing
 *   * Exact blocks sent to the LLM
 *   * Final answer + cited subset
 *   * Gate-by-gate verdict
 *
 * Visual conventions (matches other Results tabs):
 *   * Top-level wrapper is ``.results-manual-trace`` — sits inside
 *     ``.results-section__body`` which already provides padding.
 *     Tabs don't double-wrap in ``.card``.
 *   * Error / empty states use ``.results__empty`` (same as the
 *     Raw / Assets tabs).
 *   * Pipeline-specific structure uses the ``.mqt-*`` namespace.
 */

import { useCallback, useState } from "react";

import { ApiError } from "@/lib/api/client";
import { useClient } from "@/lib/hooks/useClient";
import type {
  EvidenceBlockShape,
  EvidenceCandidateShape,
  GateResultShape,
  QueryTracePayload,
  RouteExecutionRecordShape,
} from "@/types/review";

interface ManualQueryTraceViewTabProps {
  runId: string;
}

type SectionKey =
  | "plan"
  | "routes"
  | "candidates"
  | "pack"
  | "llmInput"
  | "gates";

export function ManualQueryTraceViewTab(
  { runId }: ManualQueryTraceViewTabProps,
) {
  const client = useClient();
  const [question, setQuestion] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [payload, setPayload] = useState<QueryTracePayload | null>(null);
  // Diagnostics are collapsed by default — the answer is the primary
  // surface; the pipeline trace is the "why did it fail" zoom-in.
  const [expanded, setExpanded] = useState<Record<SectionKey, boolean>>({
    plan: false,
    routes: false,
    candidates: false,
    pack: false,
    llmInput: false,
    gates: false,
  });

  const submit = useCallback(async () => {
    const trimmed = question.trim();
    if (!trimmed) {
      setError("Enter a question.");
      return;
    }
    setRunning(true);
    setError(null);
    try {
      const result = await client.runQueryTrace(runId, trimmed);
      setPayload(result);
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? e.status === 503
            ? "Trace endpoint not wired (the backend was started without smart_query_orchestrator)."
            : `Trace query failed (${e.status}): ${e.message}`
          : e instanceof Error
            ? e.message
            : "Trace query failed.";
      setError(msg);
      setPayload(null);
    } finally {
      setRunning(false);
    }
  }, [client, question, runId]);

  const toggle = (key: SectionKey) =>
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));

  return (
    <div
      className="results-manual-trace"
      data-testid="manual-query-trace-view"
    >
      <header className="results-manual-trace__header">
        <h3 className="results-manual-trace__title">
          SmartQueryOrchestrator trace
        </h3>
        <p className="results-manual-trace__subtitle">
          Drives the question through the orchestrator and renders the
          full pipeline trace. Use this when an answer looks wrong —
          the trace shows <em>why</em>.
        </p>
      </header>

      <section className="results-manual-trace__input">
        <label className="results-manual-trace__label">
          Question
          <textarea
            aria-label="Question"
            className="input mqt-textarea"
            rows={3}
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            disabled={running}
            placeholder="Ask anything about this run's indexed content…"
          />
        </label>
        <div className="results-manual-trace__actions">
          <button
            type="button"
            className="btn btn--primary"
            onClick={submit}
            disabled={running}
            aria-busy={running}
          >
            {running ? "Running…" : "Run trace"}
          </button>
          {payload && (
            <span
              className="results-manual-trace__duration"
              title="End-to-end orchestrator time"
            >
              {formatDuration(payload.trace.duration_ms)}
            </span>
          )}
        </div>
        {error && (
          <div
            className="results__empty"
            role="alert"
            data-testid="trace-error"
            style={{ marginTop: 12 }}
          >
            <strong>Trace failed.</strong>
            <div style={{ color: "var(--text-muted)", marginTop: 4 }}>
              {error}
            </div>
          </div>
        )}
        {!payload && !error && !running && (
          <p className="results-manual-trace__hint">
            Enter a question above and click <strong>Run trace</strong>{" "}
            to see the orchestrator pipeline.
          </p>
        )}
      </section>

      {payload && (
        <div className="mqt-output" data-testid="trace-output">
          <StatusBanner payload={payload} />
          <AnswerHero payload={payload} />
          <MetricsStrip payload={payload} />

          <div className="mqt-diagnostics">
            <div className="mqt-diagnostics__heading">
              <span className="mqt-diagnostics__label">
                Pipeline diagnostics
              </span>
              <span className="mqt-muted mqt-diagnostics__hint">
                Expand a stage to inspect why the orchestrator
                produced this answer.
              </span>
            </div>

            <Section
              id="plan"
              title="1 · Query plan"
              summary={planSummary(payload)}
              open={expanded.plan}
              onToggle={() => toggle("plan")}
              testId="trace-plan-section"
            >
              <PlanView payload={payload} />
            </Section>

            <Section
              id="routes"
              title="2 · Routes executed"
              badge={`${payload.trace.routes_executed.length}`}
              summary={routesSummary(payload)}
              open={expanded.routes}
              onToggle={() => toggle("routes")}
              testId="trace-routes-section"
            >
              <RoutesView routes={payload.trace.routes_executed} />
            </Section>

            <Section
              id="candidates"
              title="3 · All candidates"
              badge={`${payload.trace.all_candidates.length}`}
              summary={candidatesSummary(payload)}
              open={expanded.candidates}
              onToggle={() => toggle("candidates")}
              testId="trace-candidates-section"
            >
              <CandidatesTable
                all={payload.trace.all_candidates}
                dropped={payload.trace.dropped}
              />
            </Section>

            <Section
              id="pack"
              title="4 · Evidence pack"
              summary={packSummary(payload)}
              open={expanded.pack}
              onToggle={() => toggle("pack")}
              testId="trace-pack-section"
            >
              <GroupsView
                covered={payload.trace.groups_covered}
                missing={payload.trace.groups_missing}
              />
              <SelectedBlocks blocks={payload.trace.selected} />
            </Section>

            <Section
              id="llmInput"
              title="5 · LLM input"
              badge={`${payload.trace.llm_evidence.length} blocks`}
              summary={
                payload.trace.llm_evidence.length === 0
                  ? "LLM was NOT called"
                  : null
              }
              open={expanded.llmInput}
              onToggle={() => toggle("llmInput")}
              testId="trace-llm-input-section"
            >
              <LLMInputBlocks blocks={payload.trace.llm_evidence} />
            </Section>

            <Section
              id="gates"
              title="6 · Gate results"
              badge={`${payload.trace.gate_results.length}`}
              summary={gatesSummary(payload)}
              open={expanded.gates}
              onToggle={() => toggle("gates")}
              testId="trace-gates-section"
            >
              <GatesTable gates={payload.trace.gate_results} />
            </Section>
          </div>
        </div>
      )}
    </div>
  );
}

// ---- Status banner --------------------------------------------

function StatusBanner({ payload }: { payload: QueryTracePayload }) {
  const status = payload.final_status;
  const tone = statusTone(status);
  return (
    <div className={`mqt-banner mqt-banner--${tone}`}>
      <div className="mqt-banner__row">
        <span
          className={`badge badge--${badgeVariant(tone)} mqt-banner__badge`}
          data-testid="trace-final-status"
        >
          <span className="dot" />
          {humanStatus(status)}
        </span>
        <span className="mqt-banner__intent" title="Detected intent">
          intent: <code>{payload.trace.plan.intent}</code>
          <span className="mqt-banner__confidence">
            conf {payload.trace.plan.intent_confidence.toFixed(2)}
          </span>
        </span>
        <span className="mqt-banner__duration">
          {formatDuration(payload.trace.duration_ms)}
        </span>
      </div>
      {payload.message && (
        <p
          className="mqt-banner__message"
          data-testid="trace-message"
        >
          {payload.message}
        </p>
      )}
    </div>
  );
}

// ---- Collapsible section --------------------------------------

function Section({
  title,
  badge,
  summary,
  open,
  onToggle,
  children,
  testId,
}: {
  id: SectionKey;
  title: string;
  badge?: string;
  summary?: string | null;
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
  testId?: string;
}) {
  return (
    <section className="mqt-section" data-testid={testId}>
      <button
        type="button"
        className="mqt-section__header"
        onClick={onToggle}
        aria-expanded={open}
      >
        <span className="mqt-section__caret">{open ? "▾" : "▸"}</span>
        <span className="mqt-section__title">{title}</span>
        {badge != null && (
          <span className="mqt-section__count">{badge}</span>
        )}
        {summary && (
          <span className="mqt-section__summary">{summary}</span>
        )}
      </button>
      {open && (
        <div className="mqt-section__body">{children}</div>
      )}
    </section>
  );
}

// ---- Plan view ------------------------------------------------

function PlanView({ payload }: { payload: QueryTracePayload }) {
  const plan = payload.trace.plan;
  return (
    <dl className="mqt-kv">
      <KV label="Anchors">
        {plan.anchors.length === 0 ? (
          <span className="mqt-muted">none</span>
        ) : (
          <ChipRow values={plan.anchors} />
        )}
      </KV>
      <KV label="Requested fields">
        {plan.requested_fields.length === 0 ? (
          <span className="mqt-muted">none</span>
        ) : (
          <ChipRow values={plan.requested_fields} tone="info" />
        )}
      </KV>
      <KV label="Answer shape">
        <code className="mqt-code">{plan.answer_shape}</code>
      </KV>
      <KV label="Synthesis mode">
        <code className="mqt-code">{plan.synthesis_mode}</code>
      </KV>
      <KV label="Required groups">
        <ul className="mqt-list">
          {plan.required_groups.map((g) => (
            <li key={g.name}>
              <code className="mqt-code">{g.name}</code>
              {g.description && (
                <span className="mqt-muted"> — {g.description}</span>
              )}
              {!g.required && (
                <span className="mqt-tag">optional</span>
              )}
            </li>
          ))}
        </ul>
      </KV>
      <KV label="Sufficiency thresholds">
        <span className="mqt-muted">
          <code className="mqt-code">
            min_required_groups={plan.sufficiency.min_required_groups}
          </code>
          {", "}
          <code className="mqt-code">
            min_total_blocks={plan.sufficiency.min_total_blocks}
          </code>
        </span>
      </KV>
    </dl>
  );
}

function KV({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </>
  );
}

function ChipRow({
  values,
  tone = "default",
}: {
  values: string[];
  tone?: "default" | "info" | "ok" | "fail";
}) {
  return (
    <div className="mqt-chips">
      {values.map((v) => (
        <span key={v} className={`mqt-chip mqt-chip--${tone}`}>
          {v}
        </span>
      ))}
    </div>
  );
}

// ---- Routes view ----------------------------------------------

function RoutesView({ routes }: { routes: RouteExecutionRecordShape[] }) {
  if (routes.length === 0) {
    return <p className="mqt-muted">No routes executed.</p>;
  }
  return (
    <div className="mqt-table-wrap">
      <table className="mqt-table">
        <thead>
          <tr>
            <th>Route</th>
            <th>Label</th>
            <th>Query</th>
            <th className="mqt-table__num">Candidates</th>
            <th className="mqt-table__num">Duration</th>
            <th>Error</th>
          </tr>
        </thead>
        <tbody>
          {routes.map((r, i) => (
            <tr key={i} className={r.error ? "mqt-row--error" : undefined}>
              <td>
                <code className="mqt-code mqt-code--route">{r.route}</code>
              </td>
              <td>{r.label}</td>
              <td className="mqt-ellipsis" title={r.query}>
                {r.query}
              </td>
              <td className="mqt-table__num">
                <strong>{r.candidates.length}</strong>
              </td>
              <td className="mqt-table__num">
                {formatDuration(r.duration_ms)}
              </td>
              <td className="mqt-muted">{r.error ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---- Candidates table -----------------------------------------

function CandidatesTable({
  all,
  dropped,
}: {
  all: EvidenceCandidateShape[];
  dropped: { candidate: EvidenceCandidateShape; reason: string }[];
}) {
  if (all.length === 0) {
    return <p className="mqt-muted">No candidates surfaced.</p>;
  }
  const dropReasons = new Map(
    dropped.map((d) => [
      `${d.candidate.artifact_id}|${d.candidate.chunk_id ?? ""}`,
      d.reason,
    ]),
  );
  return (
    <div className="mqt-table-wrap">
      <table className="mqt-table">
        <thead>
          <tr>
            <th>Route</th>
            <th>Artifact</th>
            <th>Kind</th>
            <th className="mqt-table__num">Score</th>
            <th>Preview</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {all.map((c, i) => {
            const key = `${c.artifact_id}|${c.chunk_id ?? ""}`;
            const dropReason = dropReasons.get(key);
            return (
              <tr
                key={i}
                className={dropReason ? "mqt-row--dropped" : undefined}
              >
                <td>
                  <code className="mqt-code mqt-code--route">{c.route}</code>
                </td>
                <td>
                  <code className="mqt-code">{c.artifact_id}</code>
                  {c.chunk_id && (
                    <span className="mqt-muted"> · {c.chunk_id}</span>
                  )}
                </td>
                <td className="mqt-muted">{c.artifact_kind}</td>
                <td className="mqt-table__num mqt-mono">
                  {c.score.toFixed(3)}
                </td>
                <td className="mqt-ellipsis" title={c.text_preview}>
                  {c.text_preview}
                </td>
                <td>
                  {dropReason ? (
                    <span
                      className="mqt-pill mqt-pill--drop"
                      title={dropReason}
                    >
                      dropped
                    </span>
                  ) : (
                    <span className="mqt-pill mqt-pill--kept">kept</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---- Groups view ----------------------------------------------

function GroupsView({
  covered,
  missing,
}: {
  covered: string[];
  missing: string[];
}) {
  return (
    <div className="mqt-groups">
      <div className="mqt-groups__row">
        <span className="mqt-groups__label">Covered</span>
        {covered.length === 0 ? (
          <span className="mqt-muted">none</span>
        ) : (
          <ChipRow values={covered} tone="ok" />
        )}
      </div>
      <div className="mqt-groups__row">
        <span className="mqt-groups__label">Missing</span>
        {missing.length === 0 ? (
          <span className="mqt-muted">none</span>
        ) : (
          <ChipRow values={missing} tone="fail" />
        )}
      </div>
    </div>
  );
}

// ---- Block list (selected / LLM input) -----------------------

function SelectedBlocks({ blocks }: { blocks: EvidenceBlockShape[] }) {
  if (blocks.length === 0) {
    return <p className="mqt-muted">No blocks selected.</p>;
  }
  return (
    <ol className="mqt-blocks">
      {blocks.map((b, i) => (
        <li key={i} className="mqt-block">
          <header className="mqt-block__header">
            <span className="mqt-block__index">#{i + 1}</span>
            {b.group && (
              <span className="mqt-tag mqt-tag--group">{b.group}</span>
            )}
            <span className="mqt-muted">
              rank {b.rank_in_group} · {b.candidate.artifact_kind}
            </span>
            <span className="mqt-block__id mqt-muted">
              {b.candidate.artifact_id}
              {b.candidate.chunk_id && ` · ${b.candidate.chunk_id}`}
            </span>
          </header>
          <pre className="mqt-block__body">{b.body.slice(0, 600)}</pre>
        </li>
      ))}
    </ol>
  );
}

function LLMInputBlocks({ blocks }: { blocks: EvidenceBlockShape[] }) {
  if (blocks.length === 0) {
    return (
      <p className="mqt-empty">
        LLM was <strong>NOT</strong> called. The sufficiency gate failed
        before synthesis.
      </p>
    );
  }
  return <SelectedBlocks blocks={blocks} />;
}

// ---- Answer hero ----------------------------------------------

function AnswerHero({ payload }: { payload: QueryTracePayload }) {
  const hasAnswer = (payload.answer || "").trim().length > 0;
  const citations = payload.trace.citations;
  const status = payload.final_status;
  const tone = statusTone(status);

  return (
    <section
      className={`mqt-hero mqt-hero--${tone}`}
      data-testid="trace-answer-hero"
    >
      <header className="mqt-hero__header">
        <h4 className="mqt-hero__title">Final answer</h4>
        {hasAnswer && (
          <span
            className="mqt-hero__copy"
            title="Citations and evidence are detailed below"
          >
            {citations.length} citation{citations.length === 1 ? "" : "s"}
            {" · "}
            {payload.trace.llm_evidence.length} block
            {payload.trace.llm_evidence.length === 1 ? "" : "s"} to LLM
          </span>
        )}
      </header>

      {hasAnswer ? (
        <pre
          className="mqt-hero__answer"
          data-testid="trace-answer-body"
        >
          {payload.answer}
        </pre>
      ) : (
        <div className="mqt-hero__empty" data-testid="trace-answer-empty">
          <strong className="mqt-hero__empty-title">
            No answer produced.
          </strong>
          <p className="mqt-hero__empty-reason">
            {emptyAnswerReason(payload)}
          </p>
        </div>
      )}

      {citations.length > 0 && (
        <div className="mqt-hero__citations">
          <span className="mqt-hero__citations-label">Cited:</span>
          <ul className="mqt-citations">
            {citations.map((c, i) => (
              <li key={i}>
                <code className="mqt-code">{c.candidate.artifact_id}</code>
                {c.candidate.chunk_id && (
                  <span className="mqt-muted"> · {c.candidate.chunk_id}</span>
                )}
                {c.group && (
                  <span className="mqt-tag mqt-tag--group">{c.group}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

// ---- Metrics strip --------------------------------------------

function MetricsStrip({ payload }: { payload: QueryTracePayload }) {
  const trace = payload.trace;
  const totalCandidates = trace.routes_executed.reduce(
    (acc, r) => acc + r.candidates.length,
    0,
  );
  const requiredGates = trace.gate_results.filter(
    (g) => g.severity === "required",
  );
  const passedGates = requiredGates.filter((g) => g.passed).length;

  const metrics: Array<{
    label: string;
    value: string;
    title: string;
    tone?: "ok" | "warn" | "fail";
  }> = [
    {
      label: "Duration",
      value: formatDuration(trace.duration_ms),
      title: "End-to-end orchestrator time",
    },
    {
      label: "Routes",
      value: `${trace.routes_executed.length}`,
      title: "Retrieval routes executed",
    },
    {
      label: "Candidates",
      value: `${totalCandidates}`,
      title: "Total candidates surfaced across routes",
    },
    {
      label: "Selected",
      value: `${trace.selected.length}`,
      title: "Candidates kept in the evidence pack",
    },
    {
      label: "LLM blocks",
      value: `${trace.llm_evidence.length}`,
      title: "Evidence blocks sent to the synthesizer",
      tone: trace.llm_evidence.length === 0 ? "warn" : "ok",
    },
    {
      label: "Citations",
      value: `${trace.citations.length}`,
      title: "Distinct artifacts cited in the answer",
    },
    {
      label: "Gates",
      value: `${passedGates}/${requiredGates.length}`,
      title: "Required gates passed",
      tone:
        requiredGates.length === 0
          ? undefined
          : passedGates === requiredGates.length
            ? "ok"
            : "fail",
    },
  ];

  return (
    <div className="mqt-metrics" data-testid="trace-metrics">
      {metrics.map((m) => (
        <div
          key={m.label}
          className={`mqt-metric${m.tone ? ` mqt-metric--${m.tone}` : ""}`}
          title={m.title}
        >
          <span className="mqt-metric__label">{m.label}</span>
          <span className="mqt-metric__value">{m.value}</span>
        </div>
      ))}
    </div>
  );
}

function emptyAnswerReason(payload: QueryTracePayload): string {
  const status = payload.final_status;
  if (status === "retrieval_insufficient") {
    return (
      "Retrieval surfaced no usable candidates — see Routes and " +
      "Candidates below."
    );
  }
  if (status === "evidence_insufficient") {
    const missing = payload.trace.groups_missing;
    if (missing.length > 0) {
      return (
        `Sufficiency gate failed before synthesis. Missing groups: ` +
        `${missing.join(", ")}.`
      );
    }
    return (
      "Sufficiency gate failed before synthesis — see Evidence pack " +
      "below."
    );
  }
  if (status === "failed") {
    return (
      payload.message ||
      "An answer-quality gate rejected the synthesized answer — see " +
        "Gate results below."
    );
  }
  return (
    "The synthesizer returned an empty body. See LLM input and " +
    "Gate results below."
  );
}

// ---- Gates ----------------------------------------------------

function GatesTable({ gates }: { gates: GateResultShape[] }) {
  if (gates.length === 0) {
    return <p className="mqt-muted">No gates evaluated.</p>;
  }
  return (
    <div className="mqt-table-wrap">
      <table className="mqt-table">
        <thead>
          <tr>
            <th>Gate</th>
            <th>Severity</th>
            <th>Result</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {gates.map((g, i) => {
            const failed = !g.passed && g.severity === "required";
            return (
              <tr
                key={i}
                className={failed ? "mqt-row--error" : undefined}
                data-testid={`gate-row-${g.name}`}
              >
                <td>
                  <code className="mqt-code">{g.name}</code>
                </td>
                <td className="mqt-muted">{g.severity}</td>
                <td>
                  <span
                    className={`mqt-pill ${
                      g.passed ? "mqt-pill--kept" : "mqt-pill--drop"
                    }`}
                  >
                    {g.passed ? "passed" : "failed"}
                  </span>
                </td>
                <td className="mqt-muted">{g.reason ?? "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---- Helpers --------------------------------------------------

function statusTone(status: string): "ok" | "warning" | "fail" {
  if (status === "passed") return "ok";
  if (status === "retrieval_insufficient") return "warning";
  return "fail";
}

function badgeVariant(tone: "ok" | "warning" | "fail"): string {
  if (tone === "ok") return "success";
  if (tone === "warning") return "warning";
  return "error";
}

function humanStatus(status: string): string {
  switch (status) {
    case "passed":
      return "Passed";
    case "failed":
      return "Failed";
    case "evidence_insufficient":
      return "Evidence insufficient";
    case "retrieval_insufficient":
      return "Retrieval insufficient";
    default:
      return status;
  }
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  const remSeconds = Math.round(seconds % 60);
  return `${minutes}m ${remSeconds}s`;
}

function planSummary(payload: QueryTracePayload): string {
  const plan = payload.trace.plan;
  const parts = [
    `${plan.anchors.length} anchors`,
    `${plan.requested_fields.length} fields`,
    `${plan.required_groups.length} groups`,
  ];
  return parts.join(" · ");
}

function routesSummary(payload: QueryTracePayload): string {
  const total = payload.trace.routes_executed.reduce(
    (acc, r) => acc + r.candidates.length,
    0,
  );
  const errors = payload.trace.routes_executed.filter(
    (r) => r.error,
  ).length;
  const parts = [`${total} candidates`];
  if (errors > 0) parts.push(`${errors} errored`);
  return parts.join(" · ");
}

function candidatesSummary(payload: QueryTracePayload): string {
  const kept = payload.trace.selected.length;
  const dropped = payload.trace.dropped.length;
  return `${kept} kept · ${dropped} dropped`;
}

function packSummary(payload: QueryTracePayload): string {
  const covered = payload.trace.groups_covered.length;
  const missing = payload.trace.groups_missing.length;
  return `${covered} covered · ${missing} missing`;
}

function gatesSummary(payload: QueryTracePayload): string {
  const failed = payload.trace.gate_results.filter(
    (g) => !g.passed && g.severity === "required",
  ).length;
  if (failed === 0) return "all passed";
  return `${failed} failed`;
}
