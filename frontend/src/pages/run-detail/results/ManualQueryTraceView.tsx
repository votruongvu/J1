/**
 * ManualQueryTraceView — raw operator surface for the new
 * SmartQueryOrchestrator pipeline.
 *
 * Renders the full QueryTrace JSON returned by
 * ``POST /dev/query-trace`` so a developer/operator can answer
 * "why did the query fail" without instrumentation. Every stage
 * of the pipeline is shown:
 *
 *   * Question / final status / message
 *   * Detected intent + retrieval plan
 *   * Retrieval routes executed (with timings + per-route candidates)
 *   * Candidate table — kept vs dropped, with reasons
 *   * Evidence groups covered / missing
 *   * LLM input (the exact blocks the synthesizer saw)
 *   * Final answer + cited subset
 *   * Gate-by-gate verdict
 *
 * Intentionally ugly. This view exposes TRUTH, not polish. The
 * production manual-query path renders via ManualQueryConsole;
 * this one is the developer-only diagnostic surface.
 */

import { useCallback, useState } from "react";

import { ApiError } from "@/lib/api/client";
import { useClient } from "@/lib/hooks/useClient";
import type {
  EvidenceBlockShape,
  EvidenceCandidateShape,
  QueryTracePayload,
  RouteExecutionRecordShape,
} from "@/types/review";

interface ManualQueryTraceViewProps {
  runId: string;
}

export function ManualQueryTraceView({ runId }: ManualQueryTraceViewProps) {
  const client = useClient();
  const [question, setQuestion] = useState(
    "How do the deliverables evolve from conceptual engineering " +
      "through 60%, 90%, and 100% design, and which cost estimate " +
      "class is associated with each design stage?",
  );
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [payload, setPayload] = useState<QueryTracePayload | null>(null);
  const [expanded, setExpanded] = useState({
    plan: true,
    routes: true,
    candidates: true,
    pack: true,
    llmInput: true,
    answer: true,
    gates: true,
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

  const toggle = (key: keyof typeof expanded) =>
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));

  return (
    <div className="manual-query-trace-view" data-testid="manual-query-trace-view">
      <section className="trace-input">
        <h3>Manual query trace</h3>
        <p className="muted">
          Drives the question through SmartQueryOrchestrator and renders the
          full pipeline trace. Use this when an answer looks wrong — the
          trace shows WHY.
        </p>
        <textarea
          aria-label="Question"
          rows={4}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          disabled={running}
          style={{ width: "100%", fontFamily: "monospace" }}
        />
        <div className="actions">
          <button type="button" onClick={submit} disabled={running}>
            {running ? "Running…" : "Run trace"}
          </button>
        </div>
        {error && (
          <div className="error" role="alert" data-testid="trace-error">
            {error}
          </div>
        )}
      </section>

      {payload && (
        <section className="trace-output" data-testid="trace-output">
          <Header payload={payload} />

          <CollapsibleSection
            title="Query plan"
            open={expanded.plan}
            onToggle={() => toggle("plan")}
            testId="trace-plan-section"
          >
            <PlanView payload={payload} />
          </CollapsibleSection>

          <CollapsibleSection
            title={`Routes executed (${payload.trace.routes_executed.length})`}
            open={expanded.routes}
            onToggle={() => toggle("routes")}
            testId="trace-routes-section"
          >
            <RoutesView routes={payload.trace.routes_executed} />
          </CollapsibleSection>

          <CollapsibleSection
            title={`All candidates (${payload.trace.all_candidates.length})`}
            open={expanded.candidates}
            onToggle={() => toggle("candidates")}
            testId="trace-candidates-section"
          >
            <CandidatesTable
              all={payload.trace.all_candidates}
              dropped={payload.trace.dropped}
            />
          </CollapsibleSection>

          <CollapsibleSection
            title="Evidence pack (groups + selected blocks)"
            open={expanded.pack}
            onToggle={() => toggle("pack")}
            testId="trace-pack-section"
          >
            <GroupsView
              covered={payload.trace.groups_covered}
              missing={payload.trace.groups_missing}
            />
            <SelectedBlocks blocks={payload.trace.selected} />
          </CollapsibleSection>

          <CollapsibleSection
            title={`LLM input (${payload.trace.llm_evidence.length} blocks)`}
            open={expanded.llmInput}
            onToggle={() => toggle("llmInput")}
            testId="trace-llm-input-section"
          >
            <LLMInputBlocks blocks={payload.trace.llm_evidence} />
          </CollapsibleSection>

          <CollapsibleSection
            title="Final answer + citations"
            open={expanded.answer}
            onToggle={() => toggle("answer")}
            testId="trace-answer-section"
          >
            <AnswerView payload={payload} />
          </CollapsibleSection>

          <CollapsibleSection
            title={`Gate results (${payload.trace.gate_results.length})`}
            open={expanded.gates}
            onToggle={() => toggle("gates")}
            testId="trace-gates-section"
          >
            <GatesTable gates={payload.trace.gate_results} />
          </CollapsibleSection>
        </section>
      )}
    </div>
  );
}

// ---- Subcomponents ----------------------------------------

function Header({ payload }: { payload: QueryTracePayload }) {
  const status = payload.final_status;
  const tone =
    status === "passed"
      ? "ok"
      : status === "retrieval_insufficient"
        ? "warning"
        : "fail";
  return (
    <header className="trace-header">
      <span
        className={`status-badge status-${tone}`}
        data-testid="trace-final-status"
      >
        {status}
      </span>
      <span className="duration muted">
        {payload.trace.duration_ms} ms
      </span>
      {payload.message && (
        <p className="message" data-testid="trace-message">
          <strong>Message:</strong> {payload.message}
        </p>
      )}
    </header>
  );
}

function CollapsibleSection({
  title,
  open,
  onToggle,
  children,
  testId,
}: {
  title: string;
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
  testId?: string;
}) {
  return (
    <section className="trace-collapsible" data-testid={testId}>
      <button
        type="button"
        className="collapsible-header"
        onClick={onToggle}
        aria-expanded={open}
      >
        <span className="caret">{open ? "▾" : "▸"}</span> {title}
      </button>
      {open && <div className="collapsible-body">{children}</div>}
    </section>
  );
}

function PlanView({ payload }: { payload: QueryTracePayload }) {
  const plan = payload.trace.plan;
  return (
    <dl className="kv-grid">
      <dt>Intent</dt>
      <dd>
        <code>{plan.intent}</code>{" "}
        <span className="muted">(confidence {plan.intent_confidence})</span>
      </dd>
      <dt>Anchors</dt>
      <dd>
        {plan.anchors.length === 0 ? (
          <span className="muted">none</span>
        ) : (
          plan.anchors.map((a) => (
            <span key={a} className="chip">
              {a}
            </span>
          ))
        )}
      </dd>
      <dt>Requested fields</dt>
      <dd>
        {plan.requested_fields.length === 0 ? (
          <span className="muted">none</span>
        ) : (
          plan.requested_fields.map((f) => (
            <span key={f} className="chip chip-field">
              {f}
            </span>
          ))
        )}
      </dd>
      <dt>Answer shape</dt>
      <dd>
        <code>{plan.answer_shape}</code>
      </dd>
      <dt>Synthesis mode</dt>
      <dd>
        <code>{plan.synthesis_mode}</code>
      </dd>
      <dt>Required groups</dt>
      <dd>
        <ul className="muted">
          {plan.required_groups.map((g) => (
            <li key={g.name}>
              <code>{g.name}</code> — {g.description}
              {g.required ? "" : " (optional)"}
            </li>
          ))}
        </ul>
      </dd>
      <dt>Sufficiency thresholds</dt>
      <dd>
        <code>
          min_required_groups={plan.sufficiency.min_required_groups},
          min_total_blocks={plan.sufficiency.min_total_blocks}
        </code>
      </dd>
    </dl>
  );
}

function RoutesView({ routes }: { routes: RouteExecutionRecordShape[] }) {
  if (routes.length === 0)
    return <p className="muted">No routes executed.</p>;
  return (
    <table className="trace-table">
      <thead>
        <tr>
          <th>Route</th>
          <th>Label</th>
          <th>Query</th>
          <th>Candidates</th>
          <th>Duration</th>
          <th>Error</th>
        </tr>
      </thead>
      <tbody>
        {routes.map((r, i) => (
          <tr key={i} className={r.error ? "row-error" : undefined}>
            <td>
              <code>{r.route}</code>
            </td>
            <td>{r.label}</td>
            <td className="ellipsis" title={r.query}>
              {r.query}
            </td>
            <td>{r.candidates.length}</td>
            <td>{r.duration_ms} ms</td>
            <td className="muted">{r.error ?? "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function CandidatesTable({
  all,
  dropped,
}: {
  all: EvidenceCandidateShape[];
  dropped: { candidate: EvidenceCandidateShape; reason: string }[];
}) {
  const dropReasons = new Map(
    dropped.map((d) => [
      `${d.candidate.artifact_id}|${d.candidate.chunk_id ?? ""}`,
      d.reason,
    ]),
  );
  return (
    <table className="trace-table">
      <thead>
        <tr>
          <th>Route</th>
          <th>Artifact</th>
          <th>Kind</th>
          <th>Score</th>
          <th>Preview</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        {all.map((c, i) => {
          const key = `${c.artifact_id}|${c.chunk_id ?? ""}`;
          const dropReason = dropReasons.get(key);
          return (
            <tr key={i} className={dropReason ? "row-dropped" : undefined}>
              <td>
                <code>{c.route}</code>
              </td>
              <td>
                <code>{c.artifact_id}</code>
                {c.chunk_id && (
                  <span className="muted">#{c.chunk_id}</span>
                )}
              </td>
              <td>{c.artifact_kind}</td>
              <td>{c.score.toFixed(3)}</td>
              <td className="ellipsis" title={c.text_preview}>
                {c.text_preview}
              </td>
              <td>
                {dropReason ? (
                  <span className="badge badge-drop" title={dropReason}>
                    dropped: {dropReason}
                  </span>
                ) : (
                  <span className="badge badge-kept">kept</span>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function GroupsView({
  covered,
  missing,
}: {
  covered: string[];
  missing: string[];
}) {
  return (
    <div className="groups-view">
      <div>
        <strong>Covered:</strong>{" "}
        {covered.length === 0 ? (
          <span className="muted">none</span>
        ) : (
          covered.map((g) => (
            <span key={g} className="chip chip-ok">
              {g}
            </span>
          ))
        )}
      </div>
      <div>
        <strong>Missing:</strong>{" "}
        {missing.length === 0 ? (
          <span className="muted">none</span>
        ) : (
          missing.map((g) => (
            <span key={g} className="chip chip-fail">
              {g}
            </span>
          ))
        )}
      </div>
    </div>
  );
}

function SelectedBlocks({ blocks }: { blocks: EvidenceBlockShape[] }) {
  if (blocks.length === 0)
    return <p className="muted">No blocks selected.</p>;
  return (
    <ol className="blocks-list">
      {blocks.map((b, i) => (
        <li key={i}>
          <div className="block-header">
            <code>#{i + 1}</code>{" "}
            <span className="muted">
              group={b.group ?? "—"} · rank={b.rank_in_group} ·{" "}
              {b.candidate.artifact_kind}
            </span>
          </div>
          <pre className="block-body">{b.body.slice(0, 600)}</pre>
        </li>
      ))}
    </ol>
  );
}

function LLMInputBlocks({ blocks }: { blocks: EvidenceBlockShape[] }) {
  if (blocks.length === 0)
    return (
      <p className="muted">
        LLM was NOT called. The sufficiency gate failed before synthesis.
      </p>
    );
  return <SelectedBlocks blocks={blocks} />;
}

function AnswerView({ payload }: { payload: QueryTracePayload }) {
  return (
    <div className="answer-view">
      <h4>Answer</h4>
      <pre className="answer-body" data-testid="trace-answer-body">
        {payload.answer || "(empty)"}
      </pre>
      <h4>Citations ({payload.trace.citations.length})</h4>
      {payload.trace.citations.length === 0 ? (
        <p className="muted">No citations.</p>
      ) : (
        <ul>
          {payload.trace.citations.map((c, i) => (
            <li key={i}>
              <code>{c.candidate.artifact_id}</code>
              {c.candidate.chunk_id && (
                <span className="muted">#{c.candidate.chunk_id}</span>
              )}{" "}
              <span className="muted">(group: {c.group ?? "—"})</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function GatesTable({
  gates,
}: {
  gates: { name: string; passed: boolean; severity: string; reason: string | null }[];
}) {
  return (
    <table className="trace-table">
      <thead>
        <tr>
          <th>Gate</th>
          <th>Severity</th>
          <th>Result</th>
          <th>Reason</th>
        </tr>
      </thead>
      <tbody>
        {gates.map((g, i) => (
          <tr
            key={i}
            className={!g.passed && g.severity === "required" ? "row-fail" : undefined}
            data-testid={`gate-row-${g.name}`}
          >
            <td>
              <code>{g.name}</code>
            </td>
            <td>{g.severity}</td>
            <td>
              <span className={`badge ${g.passed ? "badge-kept" : "badge-drop"}`}>
                {g.passed ? "passed" : "failed"}
              </span>
            </td>
            <td className="muted">{g.reason ?? "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
