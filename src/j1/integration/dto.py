from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ---- Document / source DTOs -----------------------------------------------


@dataclass(frozen=True)
class DocumentDTO:
    document_id: str
    tenant_id: str
    project_id: str
    original_filename: str
    stored_filename: str
    mime_type: str | None
    file_size: int
    checksum: str
    status: str
    created_at: datetime
    # Document-centric refactor (Phase 1): exposes the knowledge-
    # layer state to API consumers + the REST guard that blocks
    # reindex/resume on detached or removed documents. Defaults
    # to "attached" so existing callers without these fields on
    # disk still behave as before.
    knowledge_state: str = "attached"
    # Phase 9: ``active_snapshot_id`` is the only visibility key.
    # ``active_run_id`` was deleted; consumers needing execution
    # trace read ``created_by_run_id`` on the snapshot record.
    active_snapshot_id: str | None = None
    latest_version_id: str | None = None


# ---- Job / workflow DTOs --------------------------------------------------


@dataclass(frozen=True)
class JobStatusDTO:
    job_id: str
    state: str
    current_operation: str | None = None
    documents_total: int = 0
    documents_completed: int = 0
    review_required: bool = False
    budget_approval_required: bool = False
    error: str | None = None


# ---- Search / retrieval / answer DTOs -------------------------------------


@dataclass(frozen=True)
class SearchHitDTO:
    artifact_id: str
    artifact_type: str
    title: str
    score: float
    source_document_id: str | None = None
    source_location: str | None = None
    confidence: float = 0.0
    review_status: str = "not_required"
    extracted_text: str = ""
    # Server-derived from the indexed artifact's metadata. Same trust
    # contract as CitationRecord — never echoed from request input.
    chunk_id: str | None = None
    run_id: str | None = None
    # Phase 4: snapshot lineage on the hit so the FE can deep-link
    # back to the snapshot and so the citation binder can verify
    # the hit came from the document's currently-active snapshot.
    snapshot_id: str | None = None


@dataclass(frozen=True)
class ArtifactDTO:
    artifact_id: str
    tenant_id: str
    project_id: str
    kind: str
    location: str  # workspace-relative (e.g. "compiled/<id>.txt")
    content_hash: str
    byte_size: int
    status: str
    review_status: str
    version: int
    created_at: datetime
    updated_at: datetime
    source_document_ids: list[str] = field(default_factory=list)
    source_artifact_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CitationDTO:
    artifact_id: str
    artifact_type: str
    source_document_id: str | None = None
    source_location: str | None = None
    # Server-derived from index/artifact metadata. See CitationRecord
    # (REST schema) for the trust contract — these come from the
    # matched FTS row, never from LLM output or client input.
    chunk_id: str | None = None
    run_id: str | None = None



# ---- Feedback / event DTOs -----------------------------------------------


@dataclass(frozen=True)
class FeedbackDTO:
    target_kind: str
    target_id: str
    rating: int | None = None
    comment: str | None = None
    actor: str | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FeedbackResultDTO:
    feedback_id: str
    submitted_at: datetime


@dataclass(frozen=True)
class EventDTO:
    actor: str
    action: str
    target_kind: str
    target_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None


@dataclass(frozen=True)
class EventResultDTO:
    event_id: str


# ---- Project / job-control DTOs -----------------------------------------


@dataclass(frozen=True)
class ProjectDTO:
    project_id: str
    tenant_id: str
    profile: str | None = None


@dataclass(frozen=True)
class ProjectCreateRequestDTO:
    project_id: str
    profile: str | None = None


@dataclass(frozen=True)
class ProjectIngestionRequestDTO:
    compiler_kind: str
    enricher_kind: str | None = None
    graph_builder_kind: str | None = None
    indexer_kind: str | None = None
    budget_limit_amount: str | None = None
    budget_currency: str = "USD"
    review_after: list[str] = field(default_factory=list)
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class ProcessingCapabilities:
    """Snapshot of which processor `kind`s the runtime accepts.

 The REST adapter consults this to:

 * Default an omitted `compilerKind` request field to
 `default_compiler_kind` (typically the bootstrap's
 `J1_DEFAULT_COMPILER` selection).
 * Reject a provided `compilerKind` / `graphBuilderKind` /
 `enricherKind` / `indexerKind` at the API boundary when the
 runtime has nothing registered for it — instead of letting it
 surface as a workflow failure 5 seconds later.

 Each `<kind>_kinds` set lists what the worker has wired. An empty
 set disables validation for that role (preserves backward
 compatibility for deployments that don't pass capabilities).
 `default_compiler_kind` is only consumed when `compiler_kinds`
 is non-empty AND contains the default — a defensive consistency
 check.
 """

    default_compiler_kind: str | None = None
    compiler_kinds: frozenset[str] = field(default_factory=frozenset)
    graph_builder_kinds: frozenset[str] = field(default_factory=frozenset)
    enricher_kinds: frozenset[str] = field(default_factory=frozenset)
    indexer_kinds: frozenset[str] = field(default_factory=frozenset)


def capabilities_from_bootstrap(
    boot: Any,
    *,
    enricher_kinds: frozenset[str] | None = None,
    indexer_kinds: frozenset[str] | None = None,
) -> ProcessingCapabilities:
    """Build a `ProcessingCapabilities` from a `BootstrapResult`-shaped object.

 `boot` is duck-typed (we accept anything with `.selection.compiler`,
 `.compilers`, `.graph_builders`) so this lives in the integration
 layer without importing `j1.compose` (which would invert the
 dependency arrow). Pass enricher / indexer kinds explicitly —
 those are wired by `build_worker_spec`, not by the bootstrap.
 """
    return ProcessingCapabilities(
        default_compiler_kind=boot.selection.compiler,
        compiler_kinds=frozenset(boot.compilers),
        graph_builder_kinds=frozenset(boot.graph_builders),
        enricher_kinds=enricher_kinds or frozenset(),
        indexer_kinds=indexer_kinds or frozenset(),
    )


@dataclass(frozen=True)
class JobActionResultDTO:
    job_id: str
    action: str


# ---- Cost / review DTOs --------------------------------------------------


@dataclass(frozen=True)
class CostSummaryDTO:
    project_id: str
    tenant_id: str
    total_amount: str
    currency: str = "USD"
    by_level: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewItemDTO:
    review_item_id: str
    tenant_id: str
    project_id: str
    target_kind: str
    target_id: str
    review_status: str
    requested_at: datetime
    actor: str | None = None
    notes: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewDecisionRequestDTO:
    decision: str
    actor: str
    notes: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class ReviewDecisionResultDTO:
    review_item_id: str
    review_status: str
    audit_event_id: str | None = None
