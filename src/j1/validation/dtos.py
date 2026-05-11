"""DTOs for the post-ingestion validation surface.

All cross-boundary objects live here so the service layer stays
pure-Python and the REST layer translates dataclasses → Pydantic
without leaking REST schema into the service.

Architecture rule: this module is core (`j1.validation`), so it
cannot import from `j1.integration` or `j1.adapters`. Any citation
DTO the validation surface needs lives here, not in `j1.integration`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


# `executionStatus` and `validationStatus` are split deliberately
# (per the implementation plan): a manual test query that ran
# successfully (HTTP 200) can still report `validationStatus="failed"`
# when the deterministic checks failed. Callers must not collapse
# the two concepts into one field.
ValidationStatus = Literal[
    "passed",
    "passed_with_warnings",
    "failed",
    "inconclusive",
]


CheckSeverity = Literal["required", "optional"]


@dataclass(frozen=True)
class ValidationCheckDTO:
    """Outcome of a single deterministic check.

 ships only `required`-severity checks (a failure of any
 of them flips `validationStatus` to `failed`). `optional` is
 reserved so future judge / heuristic checks can downgrade to
 `warning` instead of failing the whole result.
 """

    name: str
    severity: CheckSeverity
    passed: bool
    detail: str | None = None
    expected: Any | None = None
    actual: Any | None = None


@dataclass(frozen=True)
class ValidationCitationDTO:
    """Server-side citation projection used by the deterministic checks.

 Mirrors the wire shape (`CitationRecord` / `CitationDTO`) but
 lives inside the validation module so checks/service can reason
 about citations without importing from `j1.integration`. The
 REST layer translates this to its Pydantic record on the way
 out — see `_citation_to_dict` in `service.py`.
 """

    artifact_id: str
    artifact_type: str
    source_document_id: str | None = None
    source_location: str | None = None
    chunk_id: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class RetrievedChunkRefDTO:
    """Compact reference to one retrieved chunk in the response.

 `chunk_id` is `None` for non-chunk artifacts (e.g. graph_json
 hits surfaced through the same retrieval pipeline). `run_id`
 is the server-derived value the indexer wrote at index time —
 consumers can trust it for ownership checks.

 `artifact_kind` carries the matched artifact's `kind`
 string verbatim from the FTS row. Used by the modality-aware
 checks (e.g. `evidence_flags.tables_used` ⇔ at least one
 retrieved item has kind `enriched.tables`) and by future
 UI surfaces that need to colour-by-modality.
 """

    artifact_id: str
    chunk_id: str | None
    run_id: str | None
    document_id: str | None
    source_location: str | None
    score: float
    preview: str
    artifact_kind: str | None = None


@dataclass(frozen=True)
class ManualTestQueryRequest:
    """Inbound shape for `POST /ingestion-runs/{run_id}/test-query`.

 `mode` is forwarded to the underlying `HybridQueryEngine` verbatim.
 `citation_required` toggles the conditional `citation_present`
 check — when False, the check is skipped (not run, not in
 `checks[]`), so it can't fail the validation.
 """

    question: str
    top_k: int = 10
    mode: str = "auto"
    citation_required: bool = False
    include_raw: bool = False


@dataclass(frozen=True)
class ManualTestQueryResponseDTO:
    """Outbound shape. Body of the 200 response.

 `validation_status` aggregates `checks[]` per the rules in
 `j1.validation.checks._aggregate_status`. It is NOT the HTTP
 outcome — a 200 with `validation_status="failed"` is the
 canonical "the job ran but the answer didn't pass" case.
 """

    request_id: str
    run_id: str
    question: str
    answer: str
    mode_used: str
    retrieved_chunks: list[RetrievedChunkRefDTO]
    citations: list[dict[str, Any]]  # CitationDTO-shaped; serialised by the REST layer
    checks: list[ValidationCheckDTO]
    validation_status: ValidationStatus
    evidence_flags: dict[str, bool] = field(default_factory=dict)
    raw_response: dict[str, Any] | None = None


# ---- validation sets, runs, summaries ---------------------


# Source of a validation set. `generated` means the LLM produced it
# (treat as smoke/regression material, NOT gold truth). `manual`
# means a human authored it. `imported` means it came
# from a CSV/JSON upload. For only `generated`
# ships, but the field exists now so the wire shape doesn't churn.
ValidationSetSource = Literal["generated", "manual", "imported"]


# Lifecycle of a validation set. `draft` means generation just
# finished and a tester hasn't reviewed it yet; `ready` means it's
# considered approved-for-use (currently a no-op flag — editing /
# approval workflows arrive ). `archived` is a tombstone.
ValidationSetStatus = Literal["draft", "ready", "archived"]


# Type of test case. Used by both the generator (which kinds to
# emit) and the runner (which checks to apply). actively
# emits `retrieval` and `answer`. `negative` ships,
# `table` / `image` / `graph`. `citation` is reserved.
ValidationTestType = Literal[
    "retrieval", "answer", "citation",
    "negative", "table", "image", "graph",
]


# Test priority. `smoke` runs first, are guaranteed to be fast, and
# fail loudly on regressions. `normal` is the bulk. `deep` is the
# expensive long-tail (modality-aware checks, semantic judging).
ValidationPriority = Literal["smoke", "normal", "deep"]


# Expected behaviour the test asserts. The runner picks which
# checks to apply based on this. `answer_with_citations` is the
# default; the others land with later phases as their checks ship.
ExpectedBehavior = Literal[
    "answer_with_citations",
    "abstain",
    "retrieve_evidence",
    "validate_relationship",
]


# Execution status of a validation run — does NOT collapse with
# validationStatus. A run can be `executionStatus=completed` and
# `validationStatus=failed` (the runner finished but the test
# cases didn't all pass).
ExecutionStatus = Literal[
    "pending", "running", "completed", "failed", "cancelled",
]


@dataclass(frozen=True)
class ValidationTestCaseDTO:
    """One test case inside a validation set.

 Carries everything the runner needs to execute the case + judge
 the result. emits cases of type `retrieval` (does the
 expected chunk show up in topK?) and `answer` (does the engine
 produce a non-empty answer with valid citations?). Other types
 are reserved for later phases.

 `expected_*` fields are advisory — the deterministic check
 engine uses them to compute pass/fail. Empty lists mean "no
 check on this dimension," not "must be empty."
 """

    test_case_id: str
    question: str
    type: ValidationTestType
    priority: ValidationPriority
    expected_behavior: ExpectedBehavior
    expected_answer_points: list[str] = field(default_factory=list)
    expected_chunks: list[str] = field(default_factory=list)
    expected_pages: list[int] = field(default_factory=list)
    expected_artifacts: list[str] = field(default_factory=list)
    expected_graph_nodes: list[str] = field(default_factory=list)
    expected_graph_edges: list[str] = field(default_factory=list)
    citation_required: bool = False
    # IDs of the chunks/artifacts the GENERATOR consulted to author
    # this case. Lets a tester audit "where did this question come
    # from?" without re-running the generator.
    source_traceability: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationSetDTO:
    """A bundle of test cases produced for one ingestion run.

 generates these synchronously when the tester clicks
 "Generate test set." Idempotent on `(run_id, generator_version,
 artifacts_content_hash)` — same run + same artifacts returns
 the existing set unless `force=true`. The hash composition is
 decided by the generator; the store treats it as opaque.
 """

    validation_set_id: str
    run_id: str
    document_ids: list[str]
    source: ValidationSetSource
    status: ValidationSetStatus
    created_at: str  # ISO-8601 UTC
    created_by: str | None
    generator_version: str | None
    artifacts_content_hash: str | None
    test_cases: list[ValidationTestCaseDTO]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationCoverageDTO:
    """Coverage breakdown shown on the run summary.

 `by_type` and `by_priority` are simple counters. `by_section`
 is reserved for the generator will surface a
 `section` hint per case once it reads structured chunk
 metadata. For it stays empty rather than fabricating
 false-precision counts.
 """

    by_type: dict[str, int] = field(default_factory=dict)
    by_priority: dict[str, int] = field(default_factory=dict)
    by_section: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationSummaryDTO:
    """Roll-up shown on the Knowledge Readiness card.

 Counters are mutually-exclusive — every result lands in exactly
 one of `passed/warning/failed/skipped` so totals always reconcile
 with `total`. `recommended_action` is a human-readable string
 derived from the counts (FE renders it as the card subtitle).
 """

    total: int = 0
    passed: int = 0
    warning: int = 0
    failed: int = 0
    skipped: int = 0
    coverage: ValidationCoverageDTO = field(default_factory=ValidationCoverageDTO)
    main_issues: list[str] = field(default_factory=list)
    recommended_action: str | None = None


@dataclass(frozen=True)
class ValidationResultDTO:
    """Outcome of one test case execution.

 `status` is the per-case roll-up — same vocabulary as the
 summary counters. Distinct from `executionStatus` on the parent
 `ValidationRun`. `tester_verdict` / `tester_notes` are
 placeholders for (human override workflow).
 """

    result_id: str
    test_case_id: str
    status: Literal["passed", "warning", "failed", "skipped"]
    question: str
    answer: str
    retrieved_chunks: list[RetrievedChunkRefDTO]
    citations: list[ValidationCitationDTO]
    checks: list[ValidationCheckDTO]
    judge_notes: str | None = None
    failure_reason: str | None = None
    tester_verdict: Literal["pass", "warning", "fail"] | None = None
    tester_notes: str | None = None


@dataclass(frozen=True)
class ValidationRunDTO:
    """A single execution of a validation set against an ingestion run.

 Note the split: `execution_status` reports whether the runner
 job completed; `validation_status` reports the aggregate of
 test-case outcomes. `execution_status="completed"` +
 `validation_status="failed"` is the canonical "the job ran
 successfully but the document didn't pass" case.
 """

    validation_run_id: str
    validation_set_id: str
    run_id: str
    execution_status: ExecutionStatus
    validation_status: ValidationStatus
    started_at: str
    completed_at: str | None
    actor: str
    summary: ValidationSummaryDTO
    results: list[ValidationResultDTO] = field(default_factory=list)
    failure_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
