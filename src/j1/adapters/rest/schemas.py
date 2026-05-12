from datetime import datetime
from typing import Any, Literal

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
    # When set, the JobStarter MUST construct a fresh workflow id
    # (typically `j1-{tenant}-{project}-{doc_id}-reindex-{run_id}`)
    # rather than the deterministic `j1-{tenant}-{project}-{doc_id}`.
    # Used by the full-reindex endpoint so the new attempt doesn't
    # collide with the original workflow under USE_EXISTING.
    reindex_of: str | None = None
    # Resume-from-checkpoint context. When set, the JobStarter uses
    # a `-resume-{run_id}` workflow-id suffix (distinct from
    # `-reindex-` so operators can tell them apart in the Temporal
    # UI) and threads the carry-forward state into
    # `ProjectProcessingRequest.resume_*`.
    resume_of: str | None = None
    resume_completed_steps: tuple[str, ...] = ()
    resume_artifact_ids: tuple[str, ...] = ()
    resume_artifact_kinds: tuple[str, ...] = ()
    # Rebuild-index-only flag. When True, the workflow skips the
    # per-document loop (compile / chunks / enrich / graph) and
    # only runs the index activity against `resume_artifact_ids`.
    # Used by `POST /ingestion-runs/{id}/rebuild-index`.
    rebuild_index_only: bool = False


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
    # Most recent progress-event type observed for this run (e.g.
    # `step.started`, `step.progress`, `step.completed`,
    # `step.failed`, `step.skipped`). The run record itself isn't
    # mutated by the worker today, so callers that don't subscribe
    # to SSE can use this field to know what stage the workflow is
    # at without reading the raw event timeline. Derived server-side
    # from the audit log on read.
    last_event_type: str | None = None
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
    # LLM model class chosen for this step (none|fast|standard|premium).
    # Defaults to "none" so callers/clients that don't read it see
    # the safe-default value.
    llm_class: str = "none"


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
    # High-level LLM/vision flags computed by the planner. Surfaced
    # on the FE plan card so operators can see "vision off by default"
    # / "premium opt-in" guarantees at a glance.
    requires_vision: bool = False
    requires_premium_llm: bool = False
    vision_decisions: list[dict[str, Any]] = Field(default_factory=list)


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


class IngestionRunCompileRecord(CamelModel):
    """Response to `POST /ingestion-runs/{run_id}/compile`.

 Returned shape mirrors `IngestionRunConfirmRecord` — `status` is
 the post-trigger run status (`running` on first trigger, the
 current status on a no-op repeat trigger)."""

    run_id: str
    status: str


class IngestionRunControlRecord(CamelModel):
    """Response to `POST /ingestion-runs/{run_id}/{pause|resume|cancel}`.

 Carries the post-action status so the FE can update its cache
 without a follow-up GET, plus a short human-readable message
 suitable for a toast and the new `updated_at` timestamp the FE
 can render in its "last updated" line."""

    run_id: str
    action: str
    status: str
    stage: str | None = None
    message: str | None = None
    updated_at: str | None = None


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
 summary as the detail page without an extra round-trip. `mode`
 and `policy` are sourced from the run's metadata bag (populated
 by the upload handler) so the list rows show the same values
 the run-detail page does."""

    run_id: str
    document_id: str
    document_name: str | None = None
    status: str
    mode: str | None = None
    policy: str | None = None
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
 `IngestionRunStore.list` — currently a JSONL scan, swappable
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
    # Server-derived from the matched artifact's metadata at index
    # time. NEVER echoed from request input or LLM output — the FE
    # and downstream validators can trust these for ownership /
    # grounding checks. `chunk_id` is None for non-chunk artifacts
    # (e.g. graph_json hits); `run_id` is None for any artifact
    # that wasn't tagged with one (legacy / cross-tenant test data).
    chunk_id: str | None = None
    run_id: str | None = None


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


# ---- Validation (post-ingestion manual test query) -------------------


class ManualTestQueryRequestRecord(CamelModel):
    """Body for POST /ingestion-runs/{run_id}/test-query.

 `mode` is forwarded verbatim to the underlying answer engine
 (accepts `auto`, `knowledge_first`, `graph_first`, etc.).
 `topK` is hard-capped to 50 server-side; FastAPI clamps to the
 same cap to fail fast on out-of-range input. `citationRequired`
 flips the conditional `citation_present` deterministic check
 on/off. `synthesize` opts into LLM-backed answer synthesis on
 top of the retrieval preview (default on; set false for fast
 retrieval-only debug runs).
 """

    question: str = Field(min_length=1)
    top_k: int = Field(default=10, ge=1, le=50)
    mode: str = "auto"
    citation_required: bool = False
    include_raw: bool = False
    synthesize: bool = True
    # Validation scope (spec section 9). Default ``"run"`` keeps
    # behaviour unchanged for legacy callers. ``"active"`` switches
    # the engine to ``ActiveScope(document_id)`` — useful for
    # testing what users can search RIGHT NOW (post-reindex
    # promotion).
    validation_scope: Literal["run", "active"] = "run"


class ValidationCheckRecord(CamelModel):
    """One deterministic check outcome on the validation response.

 `severity=required` failures flip the response's
 `validationStatus` to `failed`. `severity=optional` failures
 flip it to `passed_with_warnings`. `severity` and
 `passed` together are the canonical badge inputs.
 """

    name: str
    severity: str
    passed: bool
    detail: str | None = None
    expected: Any | None = None
    actual: Any | None = None


class RetrievedChunkRefRecord(CamelModel):
    """Compact server-side projection of one retrieved chunk.

 `artifact_kind` lets the FE branch on modality —
 e.g. show a table icon for `enriched.tables`, etc. Optional
 so older runs (/2) without the field still serialise
 correctly."""

    artifact_id: str
    chunk_id: str | None = None
    run_id: str | None = None
    document_id: str | None = None
    source_location: str | None = None
    score: float = 0.0
    preview: str = ""
    artifact_kind: str | None = None


class EvidenceFlagsRecord(CamelModel):
    """Hints to the FE for which evidence rails to render.

 only populates `graphUsed` honestly — table/image
 detection lands with the artifact-registry probe.
 The other flags are present in the schema today so the FE can
 bind them once and not need a contract change later.
 """

    graph_used: bool = False
    tables_used: bool = False
    images_used: bool = False


class EvidenceBlockRecord(CamelModel):
    """One evidence block as actually sent to the LLM.

 Distinct from `RetrievedChunkRefRecord` (which carries retrieval
 metadata + a truncated preview from the artifact title). Each
 block here carries the chunk/artifact's REAL body text — what
 the model saw — plus optional page/section hints so the FE can
 render a "look at the source" link."""

    artifact_id: str
    artifact_type: str
    text: str
    chunk_id: str | None = None
    score: float = 0.0
    page_start: int | None = None
    page_end: int | None = None
    section: str | None = None
    source_location: str | None = None


class LLMTraceRecord(CamelModel):
    """Per-call LLM trace attached to manual test query responses.

 `called=False` means synthesis was skipped (opt-out OR no client
 wired — `error` distinguishes). When `called=True` the remaining
 fields are populated best-effort; `error` is non-None iff the
 LLM client failed (`provider`/`model` are still set so the FE
 can render which client failed)."""

    called: bool
    provider: str | None = None
    model: str | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None


class ManualTestQueryResponseRecord(CamelModel):
    """Body of the 200 response.

 HTTP status = execution outcome (200 = the query ran).
 `validationStatus` field = the answer's outcome, aggregated from
 `checks[]`. Callers MUST not collapse the two — a 200 with
 `validationStatus="failed"` is the canonical 'job ran but the
 answer didn't pass' case.

 `answer` is the deterministic retrieval preview (top chunks'
 titles + snippets). `synthesizedAnswer` is the LLM's grounded
 final answer when synthesis ran; `null` when synthesis was
 skipped or failed (see `llm.error`).
 """

    request_id: str
    run_id: str
    question: str
    answer: str
    mode_used: str
    retrieved_chunks: list[RetrievedChunkRefRecord]
    citations: list[CitationRecord]
    checks: list[ValidationCheckRecord]
    validation_status: str
    evidence_flags: EvidenceFlagsRecord
    raw_response: dict[str, Any] | None = None
    synthesized_answer: str | None = None
    llm: LLMTraceRecord | None = None
    # The clean evidence (with real body text) actually sent to the
    # LLM. The FE renders this as the "Evidence Sent to LLM" panel
    # — distinct from `retrievedChunks[]` which is the engine's
    # metadata-only projection (preview is the artifact title).
    evidence_sent_to_llm: list[EvidenceBlockRecord] = Field(default_factory=list)
    # Lineage-hardening diagnostic surface. Counters + reason
    # codes the FE renders when synthesis falls back. Free-form
    # dict because the shape is for debug rendering only — never
    # part of the behavior contract.
    debug: dict[str, Any] = Field(default_factory=dict)


# ---- Validation sets / runs -------------------------------


class ValidationTestCaseRecord(CamelModel):
    """One generated/imported test case. Wire shape mirrors
 `ValidationTestCaseDTO` field-for-field.
 """

    test_case_id: str
    question: str
    type: str
    priority: str
    expected_behavior: str
    expected_answer_points: list[str] = Field(default_factory=list)
    expected_chunks: list[str] = Field(default_factory=list)
    expected_pages: list[int] = Field(default_factory=list)
    expected_artifacts: list[str] = Field(default_factory=list)
    expected_graph_nodes: list[str] = Field(default_factory=list)
    expected_graph_edges: list[str] = Field(default_factory=list)
    citation_required: bool = False
    source_traceability: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # New fields from the evidence-grounded generator. The FE uses
    # these to render the scope badge ("Generic / Domain-aware /
    # Negative-check"), the expected-answer block, and the evidence
    # quote that supported the answer.
    expected_answer: str | None = None
    evidence_quote: str | None = None
    source_artifact_id: str | None = None
    source_artifact_type: str | None = None
    question_type: str | None = None
    validation_scope: str = "generic"
    difficulty: str | None = None
    domain_id: str | None = None


class ValidationSetRecord(CamelModel):
    validation_set_id: str
    run_id: str
    document_ids: list[str]
    source: str
    status: str
    created_at: str
    created_by: str | None = None
    generator_version: str | None = None
    artifacts_content_hash: str | None = None
    test_cases: list[ValidationTestCaseRecord]
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Domain pack used while generating (None when generic mode).
    domain_id: str | None = None
    # LLM trace from the single whole-document call. None when the
    # generator fell back to the heuristic path (no LLM wired).
    llm: LLMTraceRecord | None = None
    # What was actually sent to the LLM. Operator-readable summary
    # so testers can verify the generator wasn't fed garbage.
    context_summary: dict[str, Any] = Field(default_factory=dict)


class GenerateValidationSetRequestRecord(CamelModel):
    """POST /ingestion-runs/{id}/validation-sets/generate body.

 `force` bypasses the (run, hash) idempotency cache.
 `maxCases` is server-clamped to MAX_CASES_PER_RUN (50).
 """

    max_cases: int = Field(default=25, ge=1, le=50)
    citation_required: bool = False
    force: bool = False


class ValidationSetListItem(CamelModel):
    """Lightweight projection for the list endpoint — drops the full
 test_cases array so a project with many sets doesn't pay the
 full payload on each list call."""

    validation_set_id: str
    run_id: str
    source: str
    status: str
    created_at: str
    created_by: str | None = None
    case_count: int


class ValidationSetListRecord(CamelModel):
    items: list[ValidationSetListItem]


class ValidationCoverageRecord(CamelModel):
    by_type: dict[str, int] = Field(default_factory=dict)
    by_priority: dict[str, int] = Field(default_factory=dict)
    by_section: dict[str, int] = Field(default_factory=dict)


class ValidationSummaryRecord(CamelModel):
    total: int = 0
    passed: int = 0
    warning: int = 0
    failed: int = 0
    skipped: int = 0
    coverage: ValidationCoverageRecord = Field(default_factory=ValidationCoverageRecord)
    main_issues: list[str] = Field(default_factory=list)
    recommended_action: str | None = None


class ValidationCitationRecord(CamelModel):
    """Citation projection on validation results. Same wire shape
 as the manual-query CitationRecord but lives here to keep the
 REST schemas internally consistent (no cross-references
 to other regions of the schema file)."""

    artifact_id: str
    artifact_type: str
    source_document_id: str | None = None
    source_location: str | None = None
    chunk_id: str | None = None
    run_id: str | None = None


class ValidationResultRecord(CamelModel):
    result_id: str
    test_case_id: str
    status: str
    question: str
    answer: str
    retrieved_chunks: list[RetrievedChunkRefRecord]
    citations: list[ValidationCitationRecord]
    checks: list[ValidationCheckRecord]
    judge_notes: str | None = None
    failure_reason: str | None = None
    tester_verdict: str | None = None
    tester_notes: str | None = None


class ValidationRunRecord(CamelModel):
    """Body of GET /ingestion-runs/{id}/validation-runs/{vrunId}.

 Carries every per-case result inline. For runs with many cases
 this can be large; the FE caches per-`vrunId` since validation
 runs are immutable once terminal."""

    validation_run_id: str
    validation_set_id: str
    run_id: str
    execution_status: str
    validation_status: str
    started_at: str
    completed_at: str | None = None
    actor: str
    summary: ValidationSummaryRecord
    results: list[ValidationResultRecord] = Field(default_factory=list)
    failure_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ValidationRunListItem(CamelModel):
    """Lightweight projection for the list endpoint."""

    validation_run_id: str
    validation_set_id: str
    run_id: str
    execution_status: str
    validation_status: str
    started_at: str
    completed_at: str | None = None
    summary: ValidationSummaryRecord


class ValidationRunListRecord(CamelModel):
    items: list[ValidationRunListItem]


class StartValidationRunRequestRecord(CamelModel):
    validation_set_id: str = Field(min_length=1)


class TesterVerdictRequestRecord(CamelModel):
    """Body for POST /validation-results/{id}/verdict.

 `verdict` is constrained at the boundary so a typo fails fast.
 `notes` is free-form, capped at a sensible 4 KB so a tester
 pasting a wall of text can't blow up audit-log lines."""

    verdict: Literal["pass", "warning", "fail"]
    notes: str | None = Field(default=None, max_length=4096)


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
