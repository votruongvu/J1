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

    Phase 1 ships only `required`-severity checks (a failure of any
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
    """

    artifact_id: str
    chunk_id: str | None
    run_id: str | None
    document_id: str | None
    source_location: str | None
    score: float
    preview: str


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
