from datetime import datetime
from typing import Any

from pydantic import Field

from j1.adapters.rest.envelope import CamelModel


# ---- Documents -------------------------------------------------------


class DocumentMetadataInput(CamelModel):
    """Optional metadata sent alongside a multipart upload (form fields)."""

    actor: str | None = None
    correlation_id: str | None = None


class DocumentRecord(CamelModel):
    document_id: str
    tenant_id: str
    project_id: str
    original_filename: str
    stored_filename: str
    mime_type: str | None = None
    file_size: int
    checksum: str
    status: str
    created_at: datetime


class DocumentStatusRecord(CamelModel):
    document_id: str
    status: str


# ---- Ingestion jobs --------------------------------------------------


class IngestRequest(CamelModel):
    # `compilerKind` is optional. When omitted, the REST adapter falls
    # back to the runtime's default compiler (`J1_DEFAULT_COMPILER`)
    # if `processing_capabilities=` was passed to `create_rest_api`.
    # Otherwise the request is rejected with `INVALID_ARGUMENT`.
    compiler_kind: str | None = None
    enricher_kind: str | None = None
    graph_builder_kind: str | None = None
    indexer_kind: str | None = None
    actor: str = "system"
    correlation_id: str | None = None


class JobStartRecord(CamelModel):
    job_id: str
    document_id: str
    status: str = "running"


class JobStatusRecord(CamelModel):
    job_id: str
    state: str
    current_operation: str | None = None
    documents_total: int = 0
    documents_completed: int = 0
    review_required: bool = False
    budget_approval_required: bool = False
    error: str | None = None


class JobEventRecord(CamelModel):
    event_id: str
    occurred_at: datetime
    actor: str
    action: str
    target_kind: str
    target_id: str
    payload: dict[str, Any] = Field(default_factory=dict)


class JobEventsRecord(CamelModel):
    job_id: str
    events: list[JobEventRecord]


# ---- Ingestion-run progress surface (frontend-facing) ----------------


class IngestionRunRecord(CamelModel):
    """One ingestion-run summary, as the frontend consumes it.

    `status` is one of `RunStatus` (see `j1.runs.models.RunStatus`).
    `progressPercent` is the most recently reported overall progress.
    `currentStage` / `currentStep` track the in-flight stage and
    step. Terminal runs carry `completedAt`, `failureCode`, and
    `failureMessage`."""

    run_id: str
    document_id: str
    workflow_id: str
    workflow_run_id: str | None = None
    status: str
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    workspace_id: str | None = None
    current_stage: str | None = None
    current_step: str | None = None
    progress_percent: int = 0
    failure_code: str | None = None
    failure_message: str | None = None
    warning_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionPlanStep(CamelModel):
    """One step in the execution plan as shown on the plan-review UI."""

    step_id: str
    stage: str
    name: str
    decision: str  # RUN / SKIP / CONDITIONAL
    reason: str | None = None
    required: bool = False
    source: str
    dependency_step_ids: list[str] = Field(default_factory=list)
    estimated_cost_tier: str = "NONE"
    expected_engine: str | None = None
    expected_provider: str | None = None
    risk_level: str = "low"
    warning: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionPlanRecord(CamelModel):
    """Full execution-plan view: profile + per-step decisions +
    operator-tunable knobs (mode, policy, FAST-LLM usage)."""

    run_id: str
    document_id: str
    mode: str
    policy: str
    confidence: float
    estimated_cost_level: str
    fast_llm_used: bool = False
    warnings: list[str] = Field(default_factory=list)
    steps: list[ExecutionPlanStep]
    profile: dict[str, Any] = Field(default_factory=dict)


class ProgressEventRecord(CamelModel):
    """Frontend representation of a single progress event.

    Compatible with the `event_type` taxonomy from
    `j1.runs.reporter` (action constants stripped of the
    `j1.progress.` prefix). Field names mirror what the SSE stream
    emits — clients can use the same parser for both `GET …/events`
    and `GET …/events/stream`."""

    event_id: str
    run_id: str
    event_type: str
    timestamp: datetime
    severity: str = "INFO"
    stage: str | None = None
    step: str | None = None
    status: str | None = None
    progress_percent: int | None = None
    current: int | None = None
    total: int | None = None
    message: str | None = None
    engine: str | None = None
    provider: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProgressEventsRecord(CamelModel):
    run_id: str
    events: list[ProgressEventRecord]


class IngestionRunConfirmRecord(CamelModel):
    run_id: str
    status: str


class IngestionRunCreatedRecord(CamelModel):
    """Response to `POST /ingestion-runs` — minimal handshake the
    frontend uses to navigate to the run-detail page and open the
    SSE stream. The run-record is already persisted server-side; the
    client should `GET /ingestion-runs/{runId}` for the full snapshot."""

    run_id: str
    document_id: str
    workflow_id: str
    workflow_run_id: str | None = None
    status: str


class IngestionRunListItem(CamelModel):
    """Compact projection of an `IngestionRun` for the All Runs view.

    Stays a strict subset of `IngestionRunRecord` so the list view
    can render the same status badge / progress bar / failure
    summary as the detail page without an extra round-trip."""

    run_id: str
    document_id: str
    document_name: str | None = None
    status: str
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    current_stage: str | None = None
    current_step: str | None = None
    progress_percent: int = 0
    warning_count: int = 0
    failure_code: str | None = None
    failure_message: str | None = None


class IngestionRunListRecord(CamelModel):
    """Paginated list response for `GET /ingestion-runs`.

    `total` counts items AFTER status filtering but BEFORE
    pagination, so the client can render a paging widget without a
    second round-trip. Listing reads through
    `IngestionRunStore.list()` — currently a JSONL scan, swappable
    to a SQL implementation per the store Protocol."""

    items: list[IngestionRunListItem]
    page: int = 1
    page_size: int
    total: int


# ---- Search / retrieve / answer --------------------------------------


class SearchRequest(CamelModel):
    query: str = Field(min_length=1)
    artifact_types: list[str] = Field(default_factory=list)
    max_results: int = Field(default=20, ge=1, le=200)


class SearchHitRecord(CamelModel):
    artifact_id: str
    artifact_type: str
    title: str
    score: float
    source_document_id: str | None = None
    source_location: str | None = None
    confidence: float = 0.0
    review_status: str = "not_required"


class SearchResultRecord(CamelModel):
    query: str
    hits: list[SearchHitRecord]


class RetrieveRequest(CamelModel):
    query: str = Field(min_length=1)
    artifact_types: list[str] = Field(default_factory=list)
    max_blocks: int = Field(default=10, ge=1, le=50)


class CitationRecord(CamelModel):
    artifact_id: str
    artifact_type: str
    source_document_id: str | None = None
    source_location: str | None = None


class ContextBlockRecord(CamelModel):
    artifact_id: str
    artifact_type: str
    text: str
    citation: CitationRecord


class RetrieveResultRecord(CamelModel):
    query: str
    blocks: list[ContextBlockRecord]


class AnswerRequest(CamelModel):
    question: str = Field(min_length=1)
    mode: str = "auto"
    artifact_types: list[str] = Field(default_factory=list)
    max_results: int = Field(default=10, ge=1, le=50)


class GraphPathRecord(CamelModel):
    nodes: list[str] = Field(default_factory=list)
    edges: list[str] = Field(default_factory=list)
    description: str | None = None


class AnswerRecord(CamelModel):
    question: str
    answer: str
    mode_used: str
    citations: list[CitationRecord]
    related_artifacts: list[str] = Field(default_factory=list)
    graph_paths: list[GraphPathRecord] = Field(default_factory=list)
    confidence: float = 0.0
    confidence_level: str = "ambiguous"
    review_required: bool = False
    warnings: list[str] = Field(default_factory=list)
    warning_categories: list[str] = Field(default_factory=list)


# ---- Citations / sources ---------------------------------------------


class CitationDetailRecord(CamelModel):
    citation_id: str
    artifact_id: str
    artifact_type: str
    source_document_id: str | None = None
    source_location: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceDetailRecord(CamelModel):
    source_id: str
    document_id: str
    tenant_id: str
    project_id: str
    original_filename: str
    mime_type: str | None = None
    file_size: int
    checksum: str
    status: str
    created_at: datetime


# ---- Feedback --------------------------------------------------------


class FeedbackRequest(CamelModel):
    target_kind: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    rating: int | None = Field(default=None, ge=-1, le=1)
    comment: str | None = None
    actor: str | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FeedbackReceiptRecord(CamelModel):
    feedback_id: str
    submitted_at: datetime


# ---- Health / version / capabilities ---------------------------------


class HealthRecord(CamelModel):
    status: str = "ok"


class VersionRecord(CamelModel):
    version: str


class CapabilityRecord(CamelModel):
    name: str
    available: bool
    description: str | None = None


class CapabilitiesRecord(CamelModel):
    api_version: str
    capabilities: list[CapabilityRecord]


# ---- Projects --------------------------------------------------------


class ProjectCreateRequest(CamelModel):
    project_id: str = Field(min_length=1)
    profile: str | None = None


class ProjectRecord(CamelModel):
    project_id: str
    tenant_id: str
    profile: str | None = None


# ---- Project ingestion jobs (workflow control) -----------------------


class ProjectIngestionRequest(CamelModel):
    # `compilerKind` is optional. See `IngestRequest` above for the
    # default-resolution + validation contract.
    compiler_kind: str | None = None
    enricher_kind: str | None = None
    graph_builder_kind: str | None = None
    indexer_kind: str | None = None
    budget_limit_amount: str | None = None
    budget_currency: str = "USD"
    review_after: list[str] = Field(default_factory=list)
    actor: str = "system"
    correlation_id: str | None = None


class JobActionRecord(CamelModel):
    job_id: str
    action: str


# ---- Artifacts -------------------------------------------------------


class ArtifactRecord(CamelModel):
    artifact_id: str
    tenant_id: str
    project_id: str
    kind: str
    location: str
    content_hash: str
    byte_size: int
    status: str
    review_status: str
    version: int
    created_at: datetime
    updated_at: datetime
    source_document_ids: list[str] = Field(default_factory=list)
    source_artifact_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactListRecord(CamelModel):
    artifacts: list[ArtifactRecord]


# ---- Cost ------------------------------------------------------------


class CostSummaryRecord(CamelModel):
    project_id: str
    tenant_id: str
    total_amount: str
    currency: str = "USD"
    by_level: dict[str, str] = Field(default_factory=dict)


# ---- Reviews ---------------------------------------------------------


class ReviewItemRecord(CamelModel):
    review_item_id: str
    tenant_id: str
    project_id: str
    target_kind: str
    target_id: str
    review_status: str
    requested_at: datetime
    actor: str | None = None
    notes: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewListRecord(CamelModel):
    items: list[ReviewItemRecord]


class ReviewDecisionRequest(CamelModel):
    decision: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    notes: str | None = None
    correlation_id: str | None = None


class ReviewDecisionRecord(CamelModel):
    review_item_id: str
    review_status: str
    audit_event_id: str | None = None


# ---- Bulk import / export -------------------------------------------


class BulkImportFailureRow(CamelModel):
    line_number: int
    record_id: str | None = None
    code: str
    message: str


class BulkImportResultRecord(CamelModel):
    """Wire shape for `POST /imports/*.ndjson` responses."""
    succeeded: int
    skipped_idempotent: int
    failures: list[BulkImportFailureRow] = Field(default_factory=list)
    total: int
