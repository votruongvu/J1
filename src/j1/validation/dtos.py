"""DTOs for the post-ingestion validation surface.

After the 2026-05-14 product decision, this module ONLY carries the
shapes for the *Manual Test Query* surface — the detailed inspection
tool inside the Validation Tab. Imported test cases live in their
own module (``j1.validation.imported_test_cases``) and never share
shapes with manual query.

Architecture rule: this module is core (`j1.validation`), so it
cannot import from `j1.integration` or `j1.adapters`. Any citation
DTO the validation surface needs lives here, not in `j1.integration`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


# `executionStatus` and `validationStatus` are split deliberately:
# a manual test query that ran successfully (HTTP 200) can still
# report `validationStatus="failed"` when the deterministic checks
# failed. Callers must not collapse the two concepts into one field.
ValidationStatus = Literal[
    "passed",
    "passed_with_warnings",
    "failed",
    "inconclusive",
]


CheckSeverity = Literal["required", "optional"]


@dataclass(frozen=True)
class ValidationCheckDTO:
    """Outcome of a single deterministic check on a manual test query.

    Only ``required``-severity checks ship — failing any flips
    ``validationStatus`` to ``failed``. ``optional`` is reserved so
    future heuristic checks can downgrade to ``warning`` without
    failing the whole response.

    ``skipped=True`` means "this check did not run because its
    precondition wasn't met" (e.g. zero retrieved chunks for the
    chunks-belong-to-run check). Skipped checks never count toward
    pass or fail. When ``skipped=True``, ``passed`` is forced to
    ``False`` so a UI that only reads ``passed`` doesn't render a
    green check.
    """

    name: str
    severity: CheckSeverity
    passed: bool
    detail: str | None = None
    expected: Any | None = None
    actual: Any | None = None
    skipped: bool = False
    skipped_reason: str | None = None


@dataclass(frozen=True)
class ValidationCitationDTO:
    """Server-side citation projection used by manual-query checks.

    Mirrors the wire shape (``CitationRecord`` / ``CitationDTO``) but
    lives inside the validation module so checks/service can reason
    about citations without importing from ``j1.integration``. The
    REST layer translates this to its Pydantic record on the way
    out — see ``_citation_to_dict`` in ``service.py``.
    """

    artifact_id: str
    artifact_type: str
    source_document_id: str | None = None
    source_location: str | None = None
    chunk_id: str | None = None
    run_id: str | None = None
    # Body excerpt the groundedness judge uses to verify claims
    # against. Populated by the synthesizer so callers see the same
    # prose the LLM saw at synthesis time.
    preview: str | None = None


@dataclass(frozen=True)
class RetrievedChunkRefDTO:
    """Compact reference to one retrieved chunk in the response.

    ``chunk_id`` is ``None`` for non-chunk artifacts (e.g. graph_json
    hits surfaced through the same retrieval pipeline). ``run_id`` is
    the server-derived value the indexer wrote at index time —
    consumers can trust it for ownership checks.

    ``artifact_kind`` carries the matched artifact's ``kind`` string
    verbatim from the FTS row. Used by the modality-aware checks
    (e.g. ``evidence_flags.tables_used`` ⇔ at least one retrieved
    item has kind ``enriched.tables``) and by future UI surfaces.
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
class QueryScopeDTO:
    """Explicit query scope (internal representation).

    Mirrors the wire-side ``QueryScopeRecord``. Five valid shapes:

      * ``type="project_active"`` — every attached document's active
        snapshot. Project-active eligibility (attached + has active
        snapshot + lifecycle ok) gates the result.
      * ``type="document_active"`` — one document's active snapshot.
        Document-active eligibility gates the result.
      * ``type="snapshot_explicit"`` — a fixed snapshot allowlist
        (back-compat path). ``snapshot_ids`` required.
      * ``type="run"`` — query the snapshot produced by a specific
        run, regardless of whether it's promoted to active.
        ``run_id`` required. Run-scope resolution does NOT consult
        project-active or document-active eligibility — historical /
        candidate snapshots remain queryable.
      * ``type="document_run"`` — same as ``run`` but with an extra
        ``document_id`` guard: the resolver rejects cross-document
        runs. The Run Detail UI sends this for safety.
    """

    type: Literal[
        "project_active", "document_active", "snapshot_explicit",
        "run", "document_run",
    ]
    document_id: str | None = None
    snapshot_ids: tuple[str, ...] = ()
    run_id: str | None = None


@dataclass(frozen=True)
class ManualTestQueryRequest:
    """Inbound shape for ``POST /ingestion-runs/{run_id}/test-query``.

    ``mode`` is forwarded to the underlying query engine verbatim.
    ``citation_required`` toggles the conditional ``citation_present``
    check — when False, the check is skipped (not run, not in
    ``checks[]``), so it can't fail the validation.

    ``synthesize`` opts into the LLM answer-synthesis step. When True
    (default), the service runs the retrieved chunks through the
    configured ``TextLLMClient`` and exposes the result as
    ``synthesized_answer`` on the response.

    ``scope`` (preferred): explicit snapshot-centric scope. UI callers
    set this; the legacy ``validation_scope`` token is honoured only
    when ``scope`` is None.

    ``validation_scope`` (legacy). Kept for backward-compat with
    callers that haven't migrated to the typed ``scope`` field. UI
    paths set ``scope`` and never rely on ``validation_scope="run"``
    — the handler refuses run-keyed scope unless ``allow_run_scope``
    is explicitly true (diagnostic surfaces only).
    """

    question: str
    top_k: int = 10
    mode: str = "auto"
    citation_required: bool = False
    include_raw: bool = False
    synthesize: bool = True
    scope: QueryScopeDTO | None = None
    validation_scope: Literal["run", "active"] = "run"
    allow_run_scope: bool = False


@dataclass(frozen=True)
class EvidenceBlockDTO:
    """One block of evidence as actually sent to the LLM.

    Distinct from ``RetrievedChunkRefDTO`` (which is the engine's
    metadata-only hit projection) because the synthesizer needs the
    real chunk body. The service builds these by loading each
    retrieved chunk's body via the chunk projector / artifact
    registry, then deduplicating and budgeting before the LLM call.

    Returned verbatim on the response as ``evidenceSentToLlm[]`` so
    the FE can render "exactly what the model received".
    """

    artifact_id: str
    artifact_type: str
    text: str
    chunk_id: str | None = None
    score: float = 0.0
    page_start: int | None = None
    page_end: int | None = None
    section: str | None = None
    source_location: str | None = None


@dataclass(frozen=True)
class LLMTraceDTO:
    """Per-call LLM trace attached to manual test query responses.

    ``called=False`` means synthesis was disabled (request opt-out)
    or unavailable (no client wired). When ``called=True`` the
    remaining fields are populated best-effort.
    """

    called: bool
    provider: str | None = None
    model: str | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class NativeDebugQueryResponseDTO:
    """Outbound shape for ``POST /ingestion-runs/{run_id}/native-debug-query``.

    The native-debug surface is the audit-driven diagnostic: it calls
    LightRAG ``aquery`` directly, scoped to this run's workspace,
    with **no BM25 involvement**. Operators use it to answer "is the
    index actually working for this run?" without the confounding
    effect of BM25 + reranker + coverage selection that the regular
    ``test-query`` endpoint layers on top.
    """

    request_id: str
    run_id: str
    document_id: str | None
    question: str
    answer: str
    workspace_path: str | None
    workspace_id: str
    native_query_used: bool
    native_query_failed_reason: str | None
    native_latency_ms: int
    provider_wired: bool


@dataclass(frozen=True)
class ManualTestQueryResponseDTO:
    """Outbound shape. Body of the 200 response.

    ``validation_status`` aggregates ``checks[]`` per the rules in
    ``j1.validation.checks._aggregate_status``. It is NOT the HTTP
    outcome — a 200 with ``validation_status="failed"`` is the
    canonical "the job ran but the answer didn't pass" case.

    ``answer`` is the deterministic retrieval-preview snippet bundle
    — kept stable so existing checks keep their semantics.
    ``synthesized_answer`` is the LLM-generated final answer; the FE
    renders it in a "Final Answer" panel above the retrieval evidence.
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
    synthesized_answer: str | None = None
    llm: LLMTraceDTO | None = None
    # The clean evidence blocks (with real text) actually passed to
    # the LLM. Empty when synthesis was skipped.
    evidence_sent_to_llm: list[EvidenceBlockDTO] = field(default_factory=list)
    # Lineage-hardening debug surfaces. When a tester sees "Not in
    # retrieved evidence" they should be able to tell WHY at a
    # glance — was retrieval empty? Were all hits filtered out by
    # the knowledge-state gate? Did the synthesizer get evidence
    # but the LLM still abstained? These counters answer that.
    debug: dict[str, Any] = field(default_factory=dict)
