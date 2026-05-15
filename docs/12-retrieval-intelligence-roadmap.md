# 12. Retrieval Intelligence Roadmap

> Audience: engineers + technical product owners deciding what
> retrieval intelligence to add next.
> [Back to README](../README.md). See also
> [03-query-flow.md](03-query-flow.md),
> [10-domain-configuration.md](10-domain-configuration.md),
> [unified-memory-contract.md](unified-memory-contract.md).

This doc explains the staged levels of retrieval intelligence J1
plans to support, what each level adds, and what it costs. The
current default is **Level 1: deterministic alias broadening**.
Higher levels are documented here as **planned but not
implemented** — the seams exist, the heavy logic does not.

The path is deliberately staged so each level can be turned on
independently AND each level's contribution can be measured against
the level below it. We do not believe LLM-driven query rewrite is
useful until deterministic broadening is proven, and we do not
believe graph expansion is useful until LLM rewrite is proven on
top of broadening. **Evidence before intelligence.**

---

## Level 1 — Deterministic alias broadening (CURRENT DEFAULT)

**What it does.** When a user asks "What is BOQ?", the query
orchestrator consults two alias sources:

- **Pack-static aliases** from the configured domain pack (e.g.
  the civil-engineering pack maps `BOQ → bill of quantities`).
- **Enrichment-derived aliases** extracted from the document's own
  chunks at compile time (e.g. the doc itself defines `BOQ (bill
  of quantities)` and the alias producer persists that mapping
  scoped to the document's snapshot).

If either alias source has a hit, the orchestrator runs an
expanded retrieval pass: the original query + each alias variant,
deduplicated. Citations stay grounded in the chunks that survive
deduplication. **Alias text is never evidence.**

**Where it lives.**

- Alias producer: [src/j1/processing/enrichment_aliases.py](../src/j1/processing/enrichment_aliases.py).
- Alias loader (snapshot+document scoped): same file,
  `load_enrichment_aliases_for_snapshot`.
- Augmentation provider: [src/j1/memory/augmentation.py](../src/j1/memory/augmentation.py).
- Retrieval broadening seam: orchestrator's stage 1.5 in
  [src/j1/query/orchestrator.py](../src/j1/query/orchestrator.py).
- Master switch: `J1_QUERY_EXPANSION_ENABLED` (default `false`).

**How it's measured.** The retrieval broadening A/B harness
([src/j1/tools/evaluate_retrieval_broadening.py](../src/j1/tools/evaluate_retrieval_broadening.py))
runs every query in [evaluation/retrieval_broadening/sample_queries.json](../evaluation/retrieval_broadening/sample_queries.json)
twice — once with the flag off (baseline) and once with it on
(variant) — and produces a JSON report consumed by the
summarizer / comparator / validator tools.

**Cost.** Two retrievals per query (vs. one without broadening),
plus the dedup pass. No LLM calls. No new state.

**Risk.** The alias producer's stoplist
([src/j1/processing/enrichment_aliases.py](../src/j1/processing/enrichment_aliases.py))
guards against false positives like `PDF`, `HTTP`, `USA`, `API`.
A misfiring producer would broaden every query into noise. The
evaluation track is the regression net.

---

## Level 2 — LLM-assisted query rewrite (PLANNED)

**What it would do.** When deterministic broadening produces no
hits OR the answer is judged low-confidence, hand the original
query plus a small context preview to a FAST LLM and ask it to
generate one or two alternative phrasings. Run retrieval against
the rewrites and merge results.

**Why it's gated on Level 1.** If deterministic broadening
already covers the alias-shaped cases, LLM rewrite is only
valuable for shapes the alias producer cannot capture: phrasing
shifts, partial-concept paraphrase, intent-vs-keyword gaps. We
need the Level 1 report to tell us how often retrieval misses
*after* broadening before paying per-query LLM cost.

**Where it would slot in.** The orchestrator's stage 1.5 already
has the broadening seam. Level 2 adds a fallback branch *after*
stage 1.5 reports zero/low-confidence results — not a parallel
branch (Level 2 only fires when Level 1 didn't suffice). The
augmentation provider's interface
([src/j1/memory/augmentation.py](../src/j1/memory/augmentation.py))
extends to accept LLM-derived expansions alongside alias
expansions.

**Not yet wired.** No LLM-rewrite code exists. The FAST LLM role
is already configured (`J1_FAST_LLM_*` env), so the wiring would
be a thin shim plus prompt template + cost-budget cap.

**Decision gate.** Approve Level 2 only after the Level 1
evaluation report shows a measurable retrieval-miss rate that
deterministic broadening cannot close.

---

## Level 3 — Real graph expansion (PLANNED)

**What it would do.** Given the top-K retrieved chunks, walk the
knowledge graph N hops (default 1–2) to surface adjacent entities
and the chunks anchored to them. Adds graph-shaped recall for
queries about relationships rather than direct entities.

**Why it's gated on Levels 1+2.** Graph expansion is the most
expensive option (graph traversal + extra retrievals) and the
most prone to noise. We add it only after both keyword broadening
and LLM rewrite have been measured, so we can attribute marginal
gains correctly.

**Where it would slot in.** The graph-expansion contract already
exists at [src/j1/memory/graph_expansion.py](../src/j1/memory/graph_expansion.py).
The default impl is `UnsupportedGraphExpansion` — it reports
`supported=False` and returns no candidates. The orchestrator
consults the service at construction time via DI; a deployment
that registers a real adapter (Neo4j client, a stable LightRAG
n-hop API, etc.) gets graph expansion automatically without
touching the orchestrator.

**Honest default reaffirmed.** RAGAnything / LightRAG does not
currently expose a stable n-hop API J1 can call from outside the
compile/aquery flow. Pretending graph expansion works by joining
unrelated chunks would be a *correctness* regression, not a
quality boost. **`UnsupportedGraphExpansion` stays the default
until a real backend lands.**

**Decision gate.** Approve Level 3 only after Levels 1+2 are
measured, AND a graph backend with a stable n-hop API is
available.

---

## Level 4 — Retrieval scoring and LLM answer grading (PLANNED)

**What it would do.** Two related pieces:

- **Retrieval scoring** — replace today's coverage-based selection
  with a learned or LLM-judged scoring of (query, chunk) pairs.
  Picks the most relevant chunks instead of the most-overlapping
  ones.
- **LLM answer grading** — for evaluation runs, an LLM judges the
  synthesised answer against the retrieved evidence and the gold
  expected answer. The harness today does NOT do this — it
  measures retrieval *quantity* (counts, deltas), not answer
  *quality*.

**Why it's gated on Levels 1–3.** Retrieval scoring only matters
once we have multiple retrieval branches to choose between
(broadening, LLM rewrite, graph expansion) — that's why Levels
1+2+3 come first. LLM answer grading is a separate evaluation
discipline that should be a *separate, opt-in* surface, not part
of the production query path.

**Not implemented anywhere yet.** No scoring model exists. No
LLM-judge harness exists. The evaluation tools today
([summarize_retrieval_broadening_report.py](../src/j1/tools/summarize_retrieval_broadening_report.py))
deliberately stop at retrieval-count diagnostics for this reason.

**Decision gate.** Approve Level 4 only when the lower levels are
measured AND the team is ready to maintain an LLM-judge harness
(it has its own prompt-drift and cost concerns).

---

## What stays true at every level

- **Citations stay grounded in retrieved chunks.** No level above
  adds synthetic text to evidence. Alias text, LLM-rewritten
  queries, graph node names — none of these become citations.
- **Scope safety is non-negotiable.** Every retrieval branch
  filters by `(snapshot_id, document_id)` before merging. The
  alias loader's snapshot+document filter at
  [src/j1/processing/enrichment_aliases.py](../src/j1/processing/enrichment_aliases.py)
  is the contract every future level inherits.
- **Default is the honest default.** A level that cannot do its
  work (no graph backend, no LLM role configured, no alias
  artifact) reports the gap in diagnostics — it does not silently
  degrade to a different level. Operators see `supported=false` or
  the equivalent and decide.
- **Evidence before intelligence.** Each level is gated on the
  one below it being *measured*, not just implemented. The
  evaluation track ([evaluation/retrieval_broadening/](../evaluation/retrieval_broadening/))
  is the load-bearing surface that makes the gating real.
