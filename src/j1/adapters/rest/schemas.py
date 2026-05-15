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
    # Phase 9: snapshot identity. The REST adapter allocates the
    # candidate ``DocumentSnapshot`` at run-creation time and
    # threads the id through here so the JobStarter can wire it
    # onto `ProjectProcessingRequest.target_snapshot_id`. None for
    # bulk-job dispatch that processes multiple documents in one
    # workflow; the workflow allocates per-document inside the
    # processing loop via the ``allocate_target_snapshot`` activity.
    target_snapshot_id: str | None = None
    # Assessment-decision id minted by
    # ``POST /documents/{id}/assessment-plan``. When set, the REST
    # adapter looks the decision up, validates it against the
    # current document, stamps the full payload onto
    # ``IngestionRun.metadata["assessment_decision"]``, and threads
    # it through ``ProjectProcessingRequest`` so the workflow uses
    # the SAME recommendation the FE picker showed. Missing or
    # invalid decisions degrade to the workflow's rebuild path —
    # see ``j1.processing.assessment_decision`` for the validation
    # contract.
    assessment_decision_id: str | None = None
    # Full validated ``AssessmentDecision`` payload. Populated by the
    # REST adapter (NOT supplied by external callers) once the id has
    # passed ``validate_decision_for_document``. The starter threads
    # it onto ``ProjectProcessingRequest.assessment_decision_payload``
    # so the workflow can short-circuit its rebuild without consulting
    # the store from inside Temporal. Internal contract — kept on the
    # request DTO instead of a side-channel so test wirings can pass
    # a synthetic payload directly.
    assessment_decision_payload: dict | None = None
    # Warnings produced by the REST adapter's decision lookup (id
    # missing / hash mismatch / store IO fault). Threaded through so
    # the workflow can stamp them on the final report even when the
    # decision wasn't usable.
    assessment_decision_warnings: tuple[str, ...] = ()
    # User-selected execution profile (wire-string value of
    # ``ExecutionProfile``: ``minimum_queryable`` / ``standard`` /
    # ``advanced``). When None (default), the workflow falls back
    # to the `DEFAULT_PROFILE` constant in
    # [`j1.processing.execution_profile`](../../processing/execution_profile.py).
    # When explicitly provided by the FE's profile picker, it
    # becomes the authoritative gate for every downstream
    # "should this stage run?" check.
    selected_profile: str | None = None


class AssessmentPlanRequest(CamelModel):
    """Optional JSON body for ``POST /documents/{id}/assessment-plan``.

    All fields optional. Tracks the assessment-layer
    ``RecommendationResolver`` contract:

      * ``selectedDomainId`` — caller's domain preference. Resolution
        order: user-selected > workspace default > general. When the
        requested id isn't registered, the resolver falls back to
        general and emits a warning.
      * ``selectedProfile`` — operator pick at recommend time. Goes
        through the same env / allow-list gating as the ingest
        endpoint; surfaces in the persisted ``AssessmentDecision``
        for audit.
    """
    selected_domain_id: str | None = None
    selected_profile: str | None = None


class DocumentReindexRequest(CamelModel):
    """Optional JSON body for ``POST /documents/{id}/reindex``.

    Mirrors the ``selectedProfile`` field from the upload flow so the
    same AssessmentPlanDialog picker can drive re-index dispatch. When
    the field is omitted (or the body itself is omitted), the
    deployment policy's default profile applies — same fallback chain
    as the upload-and-start path.
    """
    selected_profile: str | None = None


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
    # Snapshot this run produced / was building (Phase 9). Threaded
    # to the FE so the Run Detail "Validate Produced Snapshot" widget
    # defaults its query scope to ``snapshot_explicit=[targetSnapshotId]``.
    target_snapshot_id: str | None = None
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
    # Phase 5: snapshot lineage surfaced on the wire. The FE / API
    # consumer can deep-link the hit back to a specific document
    # snapshot; the citation binder can verify the hit came from
    # the document's currently-active snapshot. ``chunk_id`` +
    # ``created_by_run_id`` round out the lineage triple.
    snapshot_id: str | None = None
    chunk_id: str | None = None
    created_by_run_id: str | None = None
    # ``extracted_text`` is the chunk body that matched the query;
    # included so the FE can render a snippet without a second
    # round-trip to the artifact registry.
    extracted_text: str = ""


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


class QueryScopeRecord(CamelModel):
    """Explicit query scope contract (wire shape, FE → BE).

    Five valid shapes, each with its own eligibility contract:

      * ``type="project_active"`` — every attached document's active
        snapshot. The user's "Ask Knowledge Base" / Home flow.
        Project-active eligibility (attached + has active snapshot +
        lifecycle ok) applies; refusal text mentions attached
        documents.
      * ``type="document_active"`` — a single document's active
        snapshot. Backs the Document Detail "Test Active Knowledge"
        widget. Requires ``documentId``. Document-active eligibility
        applies; refusal text mentions the document.
      * ``type="snapshot_explicit"`` — query a fixed allowlist of
        snapshot ids. Back-compat path used before the typed run
        scopes existed. Requires ``snapshotIds``.
      * ``type="run"`` — query the snapshot the named run produced,
        regardless of promotion / active state. Requires ``runId``.
        Active-snapshot eligibility is INTENTIONALLY bypassed: this
        scope exists so historical / candidate snapshots remain
        queryable from Run Detail.
      * ``type="document_run"`` — same as ``"run"`` but with a
        ``documentId`` guard: the resolver rejects runs that don't
        belong to that document. The Run Detail UI sends this so
        an attacker can't shop a stranger's runId at a document
        endpoint.
    """

    type: Literal[
        "project_active", "document_active", "snapshot_explicit",
        "run", "document_run",
    ]
    document_id: str | None = None
    snapshot_ids: list[str] | None = None
    run_id: str | None = None


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

 ``scope`` is the explicit snapshot-centric query scope (see
 ``QueryScopeRecord``). When omitted the legacy ``validationScope``
 string is honoured for backward compat — but UI callers MUST send
 ``scope`` because the FE has stopped treating ``run`` as a scope.
 The legacy ``validation_scope="run"`` path is rejected at the
 boundary for any request that didn't explicitly set it (i.e.
 callers that simply didn't supply ``scope`` and accepted the
 legacy default fall through to scope inference described below).
 """

    question: str = Field(min_length=1)
    top_k: int = Field(default=10, ge=1, le=50)
    mode: str = "auto"
    citation_required: bool = False
    include_raw: bool = False
    synthesize: bool = True
    # Preferred (snapshot-centric) scope. When set, ``validation_scope``
    # below is ignored.
    scope: QueryScopeRecord | None = None
    # LEGACY: the pre-snapshot scope token. Kept for backward compat
    # while callers migrate to the typed ``scope`` field. The handler
    # rejects ``validation_scope="run"`` unless the caller also opts in
    # via an explicit ``allowRunScope`` flag for diagnostic paths.
    validation_scope: Literal["run", "active"] = "run"
    # Explicit opt-in to the diagnostic ``run`` scope. Surfaced as an
    # escape hatch for operators who want to inspect a specific run's
    # raw artifacts (e.g. a failed candidate snapshot). UI callers
    # never set this; it exists for ``/dev/*`` diagnostic surfaces.
    allow_run_scope: bool = False


class ValidationCheckRecord(CamelModel):
    """One deterministic check outcome on the validation response.

 `severity=required` failures flip the response's
 `validationStatus` to `failed`. `severity=optional` failures
 flip it to `passed_with_warnings`. `severity` and
 `passed` together are the canonical badge inputs.

 ``skipped=true`` means "this check did not run because its
 precondition wasn't met" (e.g. zero retrieved chunks for the
 chunks-belong-to-run check). FE should render a neutral
 "N/A" / "skipped" badge — not a green check. ``skipped`` checks
 never affect ``validationStatus``.
 """

    name: str
    severity: str
    passed: bool
    detail: str | None = None
    expected: Any | None = None
    actual: Any | None = None
    skipped: bool = False
    skipped_reason: str | None = None


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


class NativeDebugQueryRequestRecord(CamelModel):
    """Body for POST /ingestion-runs/{run_id}/native-debug-query.

    The native-debug endpoint takes a single ``question`` and calls
    LightRAG ``aquery`` directly against this run's per-run
    workspace. There are no BM25 / reranker knobs because the
    point of the endpoint is to bypass them entirely.
    """

    question: str = Field(min_length=1)


class NativeDebugQueryResponseRecord(CamelModel):
    """Body of the 200 response from native-debug-query.

    The HTTP 200 indicates the request was accepted (run scoped /
    audited). Whether native actually answered is reported by
    ``nativeQueryUsed`` + ``nativeQueryFailedReason``. The 200 +
    ``nativeQueryUsed=false`` shape is the canonical "native
    couldn't answer; here's why" outcome and is intentional —
    callers must inspect the body, not the HTTP code.
    """

    request_id: str
    run_id: str
    document_id: str | None = None
    question: str
    answer: str
    workspace_path: str | None = None
    workspace_id: str = ""
    native_query_used: bool
    native_query_failed_reason: str | None = None
    native_latency_ms: int = 0
    provider_wired: bool


# ---- Validation sets / runs -------------------------------


# ---- Imported test cases (auxiliary Validation Tab helper) ---------
#
# Generated test cases were deleted in the 2026-05-14 product change.
# The Validation Tab now hosts a compact Imported Test Cases section:
# the user uploads a CSV per document, the server replaces the prior
# set, and an Execute button runs each question against the document's
# latest succeeded run. The UI shows summary cards + per-question
# status; per-question detail routes through Manual Test Query.


class ImportedTestCaseRecord(CamelModel):
    """One question parsed from an uploaded CSV row."""

    test_case_id: str
    question: str
    expected_answer: str | None = None
    expected_sources: list[str] = Field(default_factory=list)
    test_type: str | None = None
    notes: str | None = None


class ImportedTestCaseSetRecord(CamelModel):
    """The current imported set for one document. Replaced on every
    import; never accumulates."""

    document_id: str
    imported_at: str
    source_filename: str | None = None
    cases: list[ImportedTestCaseRecord] = Field(default_factory=list)


class ImportedTestCaseResultRecord(CamelModel):
    """Per-question execution outcome.

    ``status`` vocabulary: ``not_run`` / ``answered`` / ``no_answer`` /
    ``no_sources`` / ``scope_error`` / ``error``.
    """

    test_case_id: str
    question: str
    status: Literal[
        "not_run", "answered", "no_answer", "no_sources",
        "scope_error", "error",
    ]
    has_sources: bool
    scope_ok: bool
    error: str | None = None
    run_id: str | None = None


class ImportedTestCaseSummaryRecord(CamelModel):
    """Aggregate counts the Validation Tab renders as summary cards."""

    total: int = 0
    answered: int = 0
    with_sources: int = 0
    scope_issues: int = 0
    errors: int = 0
    overall: Literal["good", "needs_review", "poor"] = "needs_review"


class ImportedTestCaseExecutionRecord(CamelModel):
    """Latest execution snapshot for one document's imported set."""

    document_id: str
    executed_at: str
    run_id: str | None = None
    summary: ImportedTestCaseSummaryRecord
    results: list[ImportedTestCaseResultRecord] = Field(default_factory=list)


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
