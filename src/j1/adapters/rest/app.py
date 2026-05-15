import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic.alias_generators import to_camel
from temporalio.exceptions import ApplicationError

from j1.adapters.rest.envelope import envelope, error_envelope, error_response
from j1.adapters.rest.events import (
    publish_document_ingestion_started,
    publish_document_uploaded,
    publish_query_completed,
)
from j1.adapters.rest.security import (
    API_KEY_HEADER,
    SecurityPolicy,
    authenticate_request,
    require_scope as _require_scope,
)
from j1.adapters.rest.sse import SSE_CONTENT_TYPE, SSE_HEADERS
from j1.adapters.rest.schemas import (
    ArtifactListRecord,
    ArtifactRecord,
    AssessmentPlanRequest,
    BulkImportFailureRow,
    BulkImportResultRecord,
    CapabilitiesRecord,
    CapabilityRecord,
    CitationDetailRecord,
    CitationRecord,
    ContextBlockRecord,
    CostSummaryRecord,
    DocumentRecord,
    DocumentReindexRequest,
    DocumentStatusRecord,
    EvidenceFlagsRecord,
    FeedbackReceiptRecord,
    FeedbackRequest,
    GraphPathRecord,
    ExecutionPlanRecord,
    ExecutionPlanStep,
    HealthRecord,
    IngestRequest,
    IngestionRunCompileRecord,
    IngestionRunConfirmRecord,
    IngestionRunControlRecord,
    IngestionRunCreatedRecord,
    IngestionRunListItem,
    IngestionRunListRecord,
    IngestionRunRecord,
    JobActionRecord,
    JobEventRecord,
    JobEventsRecord,
    JobStartRecord,
    JobStatusRecord,
    EvidenceBlockRecord,
    ImportedTestCaseExecutionRecord,
    ImportedTestCaseRecord,
    ImportedTestCaseResultRecord,
    ImportedTestCaseSetRecord,
    ImportedTestCaseSummaryRecord,
    LLMTraceRecord,
    ManualTestQueryRequestRecord,
    ManualTestQueryResponseRecord,
    NativeDebugQueryRequestRecord,
    NativeDebugQueryResponseRecord,
    ProgressEventRecord,
    ProgressEventsRecord,
    ProjectCreateRequest,
    ProjectIngestionRequest,
    ProjectRecord,
    RetrievedChunkRefRecord,
    RetrieveRequest,
    RetrieveResultRecord,
    ReviewDecisionRecord,
    ReviewDecisionRequest,
    ReviewItemRecord,
    ReviewListRecord,
    SearchHitRecord,
    SearchRequest,
    SearchResultRecord,
    SourceDetailRecord,
    ValidationCheckRecord,
    VersionRecord,
)
from j1.artifacts.registry import ArtifactNotFoundError
from j1.documents.service import (
    DocumentLifecycleError,
    DocumentLifecycleService,
)
from j1.ingestion_review import (
    IngestionResultReviewService,
    ReviewNotFound,
)
from j1.runs import (
    ACTION_PROGRESS_PLAN_CONFIRMED,
    ACTION_PROGRESS_PLAN_GENERATED,
    IngestionRun,
    IngestionRunStore,
    PROGRESS_ACTION_PREFIX,
    PROGRESS_TERMINAL_EVENT_TYPES,
    ProgressReporter,
    RunStatus,
    status_aliases,
)
from j1.audit.sink import AUDIT_LOG_FILENAME
from j1.errors.exceptions import (
    DocumentNotFoundError,
    DuplicateDocumentError,
    InvalidIdentifierError,
    J1Error,
    PathTraversalError,
    UnsupportedFileTypeError,
    UploadTooLargeError,
)
from j1.ingestion_review.audit_actions import (
    ACTION_OPS_BATCH_DISPATCHED,
    ACTION_OPS_RUN_DELETED,
    ACTION_OPS_RUN_REINDEXED,
    TARGET_KIND_INGESTION_BATCH,
    TARGET_KIND_INGESTION_RUN,
)
from j1.integration.dto import (
    EventDTO,
    FeedbackDTO,
    ProcessingCapabilities,
    ProjectCreateRequestDTO,
    ProjectIngestionRequestDTO,
    ReviewDecisionRequestDTO,
)
from j1.integration.bulk import (
    BulkExportService,
    BulkImportService,
)
from j1.integration.events import ApplicationEventBus
from j1.integration.security import (
    ANONYMOUS_CONTEXT,
    Authenticator,
    AuthorizationError,
    SCOPE_ADMIN,
    SCOPE_ANSWER,
    SCOPE_AUDIT_READ,
    SCOPE_FEEDBACK,
    SCOPE_INGEST,
    SCOPE_READ,
    SCOPE_RETRIEVE,
    SCOPE_SEARCH,
    SCOPE_VALIDATION_READ,
    SCOPE_VALIDATION_WRITE,
    SecurityContext,
)
from j1.integration.services import ApplicationFacade
from j1.processing.execution_profile import (
    DEFAULT_PROFILE as _EXECUTION_PROFILE_DEFAULT,
    ExecutionProfile,
)
from j1.processing.execution_profile_policy import (
    ExecutionProfilePolicy,
    ProfileNotAllowedError,
)
from j1.projects.context import ProjectContext
from j1.review.queue import ReviewItemNotFoundError
from j1.validation import (
    IngestionValidationService,
    ManualTestQueryRequest as ManualTestQueryRequestDTO,
)
from j1.workspace.resolver import WorkspaceResolver

_log = logging.getLogger("j1.adapters.rest")

TENANT_HEADER = "X-Tenant-Id"
PROJECT_HEADER = "X-Project-Id"
REQUEST_ID_HEADER = "X-Request-Id"

ContextResolver = Callable[[Request], ProjectContext]
# Hook the deployment can wire to forward `POST /ingestion-runs/{id}/
# confirm` to the workflow that's parked at the confirmation gate.
# Typical implementation is a thin wrapper around `temporalio.Client.
# get_workflow_handle(workflow_id).signal(...)`. When omitted, the
# REST adapter still flips the run record's status to RUNNING and
# emits the `plan.confirmed` progress event, but no Temporal signal
# is sent — appropriate for deployments that auto-run (no
# confirmation gate is configured per-run).
RunConfirmHandler = Callable[[ProjectContext, str], Awaitable[None]]
# Hook the deployment can wire to forward `POST /ingestion-runs/{id}/
# compile` to the workflow parked at `WorkflowState.WAITING_FOR_
# COMPILE_TRIGGER`. Typical implementation is a thin wrapper around
# `temporalio.Client.get_workflow_handle(workflow_id).signal(
# SIGNAL_TRIGGER_COMPILE)`. When omitted, the REST adapter still
# flips the run record's status COMPILE_PENDING → RUNNING but no
# Temporal signal is sent — appropriate for deployments running in
# the legacy single-call compile flow.
RunCompileHandler = Callable[[ProjectContext, str], Awaitable[None]]
JobStarter = Callable[
    [ProjectContext, str, IngestRequest], Awaitable[str]
]
# Parent-workflow starter for multi-upload batches. Distinct from
# `JobStarter` because the parent dispatches a different workflow
# class and its input shape is `(ctx, batch_run_id, child_specs)`
# rather than per-document. The dev wiring builds both via the same
# `client_provider`. When None, `POST /ingestion-batches` 503s.
BatchStarter = Callable[
    [ProjectContext, str, list],  # ctx, batch_run_id, child_specs
    Awaitable[str],  # returns the parent workflow_id
]


def _default_context_resolver(request: Request) -> ProjectContext:
    tenant_id = request.headers.get(TENANT_HEADER)
    project_id = request.headers.get(PROJECT_HEADER)
    if not tenant_id or not project_id:
        raise HTTPException(
            status_code=400,
            detail=f"{TENANT_HEADER} and {PROJECT_HEADER} headers required",
        )
    try:
        ctx = ProjectContext(tenant_id=tenant_id, project_id=project_id)
    except InvalidIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _enforce_tenant_binding(request, ctx)
    return ctx


def _enforce_tenant_binding(request: Request, ctx: ProjectContext) -> None:
    """Reject `X-Tenant-Id` headers that don't match an authenticated
 key's tenant binding.

 When a deployment configures `J1_AUTH_API_KEYS` and binds a key
 to a specific tenant, the request's `X-Tenant-Id` header MUST
 match that binding. Anonymous keys (`tenant_id=None`) and
 unauthenticated requests pass through — those code paths are
 governed by scope checks, not tenant-pinning.

 Without this check, an authenticated key bound to tenant A could
 pass `X-Tenant-Id: B` and access tenant B's data; the per-tenant
 workspace path scoping doesn't catch it because the path is
 derived from the (now-mismatched) header value.
 """
    security = getattr(request.state, "security_context", None)
    if security is None or security.tenant_id is None:
        return
    if security.tenant_id != ctx.tenant_id:
        raise HTTPException(
            status_code=403,
            detail=(
                f"credential is bound to tenant "
                f"{security.tenant_id!r}; cannot operate on tenant "
                f"{ctx.tenant_id!r}"
            ),
        )


def _install_openapi_security(
    app: FastAPI,
    *,
    anonymous_paths: frozenset[str],
) -> None:
    """Install Bearer + API-key security schemes on the OpenAPI doc.

 Auth is enforced by `_security_middleware`, not by FastAPI's
 dependency machinery — so we customise the OpenAPI document
 directly rather than declaring dependencies on every route.
 Effects in Swagger UI:

 * "Authorize" button appears top-right.
 * Each non-anonymous operation shows a lock icon and applies
 the chosen scheme on `Try it out` requests.
 * Anonymous paths (`/health` / `/version`) carry an empty
 `security: []` so Swagger doesn't pretend they need auth.

 Implementation: instead of trying to override FastAPI's
 `app.openapi` method (instance-attribute shadowing has been
 flaky across SDK versions when combined with Starlette's
 lazily-built middleware stack), we let FastAPI generate its
 schema as usual and post-process the cached `app.openapi_schema`
 on first call. The post-processing is idempotent: subsequent
 calls return the already-augmented cached value."""
    bearer_scheme = {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "Bearer token. Sent as `Authorization: Bearer <token>`. "
            "Configured via `J1_AUTH_API_KEYS` (or "
            "`J1_AUTH_API_KEYS_FILE`)."
        ),
    }
    api_key_scheme = {
        "type": "apiKey",
        "in": "header",
        "name": API_KEY_HEADER,
        "description": (
            "Opaque API key. Alternative to Bearer for clients that "
            "can't easily set the Authorization header."
        ),
    }
    _original_openapi = app.openapi

    def _augmented_openapi() -> dict[str, object]:
        # FastAPI's own `openapi` caches into `app.openapi_schema`.
        # We reuse that cache: first call generates + augments; later
        # calls short-circuit on the augmented marker.
        schema = _original_openapi()
        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        if "bearer" not in security_schemes:
            security_schemes["bearer"] = bearer_scheme
        if "api_key" not in security_schemes:
            security_schemes["api_key"] = api_key_scheme

        # Apply security globally, then strip it on anonymous paths.
        # Either scheme satisfies — Swagger renders both as choices.
        if "security" not in schema:
            schema["security"] = [{"bearer": []}, {"api_key": []}]
            paths = schema.get("paths", {})
            for path, operations in paths.items():
                if path in anonymous_paths:
                    for op in operations.values():
                        if isinstance(op, dict):
                            op["security"] = []
        return schema

    # FastAPI's openapi.json route handler resolves `self.openapi` at
    # request time, so a plain instance-attribute reassignment SHOULD
    # work — but we belt-and-brace it by also augmenting via response
    # middleware below. This assignment also covers direct callers
    # (tests, tooling) that call `app.openapi`.
    app.openapi = _augmented_openapi  # type: ignore[method-assign]

    # Belt-and-brace: intercept the OpenAPI response in middleware
    # and augment its body if the schemes aren't there yet. This
    # works regardless of how the route handler resolves
    # `self.openapi`.
    @app.middleware("http")
    async def _openapi_security_middleware(request: Request, call_next):
        response = await call_next(request)
        if request.url.path != app.openapi_url:
            return response
        # Read the response body, augment, re-emit.
        body_chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            body_chunks.append(
                chunk if isinstance(chunk, bytes) else chunk.encode()
            )
        body = b"".join(body_chunks)
        try:
            schema = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            return JSONResponse(
                content={"raw": body.decode("utf-8", errors="replace")},
                status_code=response.status_code,
            )

        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        if "bearer" not in security_schemes:
            security_schemes["bearer"] = bearer_scheme
        if "api_key" not in security_schemes:
            security_schemes["api_key"] = api_key_scheme
        if "security" not in schema:
            schema["security"] = [{"bearer": []}, {"api_key": []}]
            paths = schema.get("paths", {})
            for path, operations in paths.items():
                if path in anonymous_paths:
                    for op in operations.values():
                        if isinstance(op, dict):
                            op["security"] = []
        # Cache so subsequent fetches avoid the full regenerate.
        app.openapi_schema = schema
        return JSONResponse(content=schema, status_code=response.status_code)


def create_rest_api(
    facade: ApplicationFacade,
    *,
    context_resolver: ContextResolver | None = None,
    job_starter: JobStarter | None = None,
    batch_starter: BatchStarter | None = None,
    workspace: WorkspaceResolver | None = None,
    authenticator: Authenticator | None = None,
    anonymous_paths: frozenset[str] | None = None,
    event_bus: ApplicationEventBus | None = None,
    bulk_export: BulkExportService | None = None,
    bulk_import: BulkImportService | None = None,
    processing_capabilities: ProcessingCapabilities | None = None,
    ingestion_run_store: "IngestionRunStore | None" = None,
    batch_run_store: object | None = None,
    progress_reporter: "ProgressReporter | None" = None,
    review_service: IngestionResultReviewService | None = None,
    validation_service: IngestionValidationService | None = None,
    smart_query_orchestrator: "object | None" = None,
    document_lifecycle_service: "DocumentLifecycleService | None" = None,
    confirm_handler: RunConfirmHandler | None = None,
    compile_handler: RunCompileHandler | None = None,
    llm_registry: object | None = None,
    # Phase 5: when wired, every ``IngestionRun`` created via the
    # REST surface allocates a candidate ``DocumentSnapshot``
    # before the workflow starts. ``target_snapshot_id`` is then
    # stamped on the run record so the workflow + activities know
    # which snapshot they're building from the very first event.
    snapshot_service: "object | None" = None,
    # Persistent recommendation store. When wired, the
    # ``POST /documents/{id}/assessment-plan`` endpoint stamps each
    # outcome as an ``AssessmentDecision`` so the FE-shown
    # recommendation can be threaded through ``POST /ingestion-runs``
    # (via ``assessmentDecisionId``) and consumed by the workflow
    # instead of being silently recomputed downstream. When None,
    # the endpoint degrades gracefully — no ``assessmentDecisionId``
    # in the response and the workflow's existing rebuild path
    # runs as before.
    assessment_decision_store: "object | None" = None,
    # Optional LLM-based Advanced Assessment service. NEVER runs as
    # part of the default ingest path — operators trigger it
    # explicitly via ``POST /documents/{id}/advanced-assessment``.
    # When None, the endpoint returns a structured refusal saying
    # the deployment has no LLM advanced-assessment configured.
    llm_advanced_assessment_service: "object | None" = None,
    # Workspace default domain (per-deployment knob, not per-request).
    # Threaded into the assessment-plan domain resolution chain:
    # user-selected > workspace default > general. ``None`` falls
    # straight through to ``general``. Currently sourced from a
    # single deployment-level setting; per-project overrides live
    # under future work.
    workspace_default_domain_id: "str | None" = None,
    # Deployment-level execution-profile safety policy. When None,
    # the adapter constructs a permissive default (every profile
    # allowed, deployment-wide DEFAULT_PROFILE) — preserves the
    # pre-policy behaviour for existing test wiring + dev runs.
    # Production deployments should pass a policy loaded via
    # `load_execution_profile_policy()` so env-driven hard caps
    # (`J1_ALLOW_ADVANCED_INGEST=false`, etc.) actually fire.
    execution_profile_policy: "ExecutionProfilePolicy | None" = None,
    version: str = "0.1.0",
    api_title: str = "J1 Knowledge Base API",
    description: str | None = None,
) -> FastAPI:
    """Build the standard REST adapter.

 Mandatory dependency: `facade` (an `ApplicationFacade`). Everything else
 is optional and the adapter degrades gracefully:
 * `job_starter=None` → `POST /documents/{id}/ingest` returns 503
 * `workspace=None` → retrieve endpoint omits artifact text content
 * `facade.search=None` / `.answer=None` / `.job_status=None` → those
 endpoints return 503 too

 `processing_capabilities` (when supplied — typically constructed
 from `BootstrapResult.to_processing_capabilities`) lets the API:
 * Default an omitted `compilerKind` request field to the
 runtime's `J1_DEFAULT_COMPILER` selection so simple clients
 can omit it.
 * Reject unknown `compilerKind` / `graphBuilderKind` /
 `enricherKind` / `indexerKind` values at the API boundary
 with a clear `INVALID_ARGUMENT` 400, instead of letting them
 surface as a workflow `UnknownProcessorError` 5 seconds later.
 When omitted, validation + defaulting are skipped — callers
 MUST then provide `compilerKind` explicitly (or the request
 fails downstream as before).
 """
    app = FastAPI(
        title=api_title,
        version=version,
        description=description
        or "Standard REST surface over the J1 knowledge base.",
        openapi_tags=[
            {"name": "projects", "description": "Project provisioning"},
            {"name": "documents", "description": "Document upload and retrieval"},
            {"name": "ingestion-jobs", "description": "Processing-job lifecycle: start, status, signals, events"},
            {"name": "ingestion-runs", "description": "User-facing ingestion runs: status, execution plan, progress events, SSE stream"},
            {"name": "artifacts", "description": "Produced artifact lookup"},
            {"name": "search", "description": "Keyword search over indexed artifacts"},
            {"name": "retrieve", "description": "Context-block retrieval with citations"},
            {"name": "answer", "description": "Generated answers with citations"},
            {"name": "citations", "description": "Citation lookup"},
            {"name": "sources", "description": "Source document lookup"},
            {"name": "cost", "description": "Spend reporting"},
            {"name": "reviews", "description": "Human-review queue"},
            {"name": "feedback", "description": "User feedback capture"},
            {"name": "bulk", "description": "Bulk import / export (NDJSON)"},
            {"name": "system", "description": "Health, version, capabilities"},
        ],
    )

    resolver = context_resolver or _default_context_resolver

    # Permissive default policy when the caller didn't pass one —
    # preserves the pre-policy behaviour so existing tests + dev
    # wiring keep working unchanged. Production deployments thread
    # in a policy loaded from env (`load_execution_profile_policy()`)
    # so `J1_ALLOW_ADVANCED_INGEST=false` is actually honoured.
    _profile_policy: ExecutionProfilePolicy = (
        execution_profile_policy
        or ExecutionProfilePolicy(
            default_profile=_EXECUTION_PROFILE_DEFAULT,
            allowed=frozenset(ExecutionProfile),
        )
    )

    # Phase 5: Allocate a candidate ``DocumentSnapshot`` BEFORE the
    # Temporal workflow starts. Returns the new snapshot's id (or
    # ``None`` when the snapshot service isn't wired — bulk-job
    # flows allocate per-document inside the workflow via the
    # ``allocate_target_snapshot`` activity instead). Best-effort:
    # any failure here returns ``None`` so the run creation isn't
    # blocked by a snapshot-store hiccup.
    def _allocate_target_snapshot(
        ctx: ProjectContext, document_id: str, run_id: str,
    ) -> str | None:
        if snapshot_service is None or not document_id or not run_id:
            return None
        try:
            snap = snapshot_service.create_candidate(
                ctx,
                document_id=document_id,
                created_by_run_id=run_id,
            )
            return snap.snapshot_id
        except Exception:  # noqa: BLE001 — best-effort
            return None

    def _load_and_validate_assessment_decision(
        *,
        ctx: ProjectContext,
        decision_id: str | None,
        document_id: str,
        file_hash: str | None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Look up + validate a persisted ``AssessmentDecision``.

        Returns ``(metadata_payload | None, validation_warning | None)``.

        * Both ``None``  → caller didn't pass a decision id; the workflow
          will run its rebuild path as before.
        * ``(payload, None)`` → decision is valid; stamp it onto run
          metadata and thread the id to the workflow so it can
          short-circuit.
        * ``(None, warning)`` → caller passed an id but validation
          failed; stamp the warning so the final report reflects what
          happened. The workflow still rebuilds.

        Never raises — assessment is advisory, and a stale FE picker
        result must not block an upload.
        """
        if not decision_id or assessment_decision_store is None:
            return None, None
        try:
            from j1.processing.assessment_decision import (
                AssessmentDecisionValidationError,
                validate_decision_for_document,
            )
            decision = assessment_decision_store.get(ctx, decision_id)
        except Exception:  # noqa: BLE001 — store IO fault → fallback
            return None, (
                f"Assessment decision {decision_id!r} could not be "
                "loaded from the store; workflow will rebuild assessment."
            )
        if decision is None:
            return None, (
                f"Assessment decision {decision_id!r} was not found "
                "for this project; workflow will rebuild assessment."
            )
        try:
            validate_decision_for_document(
                decision,
                document_id=document_id,
                file_hash=file_hash,
            )
        except AssessmentDecisionValidationError as exc:
            return None, (
                f"{exc}; workflow will rebuild assessment."
            )
        return decision.to_payload(), None

    def _read_sampled_text(
        source_path, *, max_chars: int, profile: Any = None,
    ) -> dict[str, Any]:
        """Best-effort sampled-text extractor with structured status.

        Returns a dict matching the ``LLMAdvancedAssessmentInputs``
        sample-text fields::

            {
                "text": str | None,
                "status": "available" | "empty" | "unsupported"
                          | "garbled" | "unreliable",
                "source": "pypdf" | "plain_text" | "unavailable",
                "char_count": int,
                "page_count": int,
            }

        ``available``  — extractor returned usable text.
        ``empty``      — extractor ran but returned no content.
        ``unsupported``— file extension has no text extractor at all
                         (.docx / .xlsx / .png / binary uploads).
        ``garbled``    — extractor ran but the output is mostly
                         non-printable bytes (heuristic: <40 % ASCII
                         printables across the sample).
        ``unreliable`` — extractor produced text that DECODED but is
                         too sparse / too low-ratio to anchor layout
                         claims. Driven by the lightweight profile's
                         ``text_extractable_ratio`` signal + per-page
                         character density.

        When ``profile`` is supplied (the lightweight
        ``DocumentProfile``), an extra refinement pass promotes
        ``available`` → ``unreliable`` for scanned-document
        signatures. The LLM service hedges all ``likely`` verdicts
        on any non-``available`` status.
        """
        from j1.processing.llm_advanced_assessment import (
            SAMPLE_TEXT_SOURCE_PLAIN_TEXT,
            SAMPLE_TEXT_SOURCE_PYPDF,
            SAMPLE_TEXT_SOURCE_UNAVAILABLE,
            SAMPLE_TEXT_STATUS_AVAILABLE,
            SAMPLE_TEXT_STATUS_EMPTY,
            SAMPLE_TEXT_STATUS_GARBLED,
            SAMPLE_TEXT_STATUS_UNRELIABLE,
            SAMPLE_TEXT_STATUS_UNSUPPORTED,
        )

        # ---- Thresholds ----
        # These are intentionally conservative: a false-positive
        # ``unreliable`` is cheap (LLM still gets filename + rules)
        # while a false-negative makes the LLM claim layout detail
        # from a scanned doc. Tunable via env if a deployment needs
        # different thresholds.
        _MIN_EXTRACTABLE_RATIO = 0.20
        _MIN_CHARS_PER_PAGE_FOR_RELIABLE = 80
        # Documents shorter than this skip the density check —
        # short notes legitimately have low char counts.
        _MIN_PAGES_FOR_DENSITY_CHECK = 3

        _PLAIN_TEXT_EXTS = frozenset({
            ".txt", ".md", ".markdown", ".rst", ".log",
            ".csv", ".json", ".yaml", ".yml", ".html", ".htm",
        })

        def _classify_text(s: str) -> str:
            """Cheap garbled-text heuristic: count printable ASCII
            (incl. common whitespace) over the first 1024 chars and
            flag anything below 40 % as ``garbled``."""
            if not s:
                return SAMPLE_TEXT_STATUS_EMPTY
            sample = s[:1024]
            printable = sum(
                1 for ch in sample
                if 0x20 <= ord(ch) < 0x7F or ch in "\t\n\r"
            )
            ratio = printable / len(sample)
            if ratio < 0.40:
                return SAMPLE_TEXT_STATUS_GARBLED
            return SAMPLE_TEXT_STATUS_AVAILABLE

        try:
            ext = source_path.suffix.lower() if source_path else ""
        except Exception:  # noqa: BLE001
            ext = ""
        # ---- Plain-text family ----------------------------------
        if ext in _PLAIN_TEXT_EXTS:
            try:
                raw = source_path.read_text(
                    encoding="utf-8", errors="replace",
                )
            except Exception:  # noqa: BLE001
                return {
                    "text": None,
                    "status": SAMPLE_TEXT_STATUS_UNSUPPORTED,
                    "source": SAMPLE_TEXT_SOURCE_UNAVAILABLE,
                    "char_count": 0, "page_count": 0,
                }
            trimmed = raw[:max_chars]
            return _refine_with_profile({
                "text": trimmed,
                "status": _classify_text(trimmed),
                "source": SAMPLE_TEXT_SOURCE_PLAIN_TEXT,
                "char_count": len(trimmed),
                "page_count": 1,
            }, profile)
        # ---- PDF ------------------------------------------------
        if ext == ".pdf":
            try:
                from pypdf import PdfReader  # type: ignore[import-not-found]
            except Exception:  # noqa: BLE001
                return {
                    "text": None,
                    "status": SAMPLE_TEXT_STATUS_UNSUPPORTED,
                    "source": SAMPLE_TEXT_SOURCE_UNAVAILABLE,
                    "char_count": 0, "page_count": 0,
                }
            try:
                reader = PdfReader(str(source_path))
                pages = reader.pages
                n = len(pages)
                if n == 0:
                    return {
                        "text": None,
                        "status": SAMPLE_TEXT_STATUS_EMPTY,
                        "source": SAMPLE_TEXT_SOURCE_PYPDF,
                        "char_count": 0, "page_count": 0,
                    }
                indices = [0]
                if n >= 2:
                    indices.append(n - 1)
                if n >= 3:
                    indices.insert(1, n // 2)
                parts: list[str] = []
                remaining = max_chars
                sampled_pages = 0
                for idx in indices:
                    if remaining <= 0:
                        break
                    try:
                        text = pages[idx].extract_text() or ""
                    except Exception:  # noqa: BLE001
                        text = ""
                    chunk = text[:remaining]
                    if chunk:
                        parts.append(
                            f"[page {idx + 1}/{n}]\n{chunk}"
                        )
                        remaining -= len(chunk)
                        sampled_pages += 1
                joined = "\n\n".join(parts) if parts else ""
                return _refine_with_profile({
                    "text": joined or None,
                    "status": _classify_text(joined),
                    "source": SAMPLE_TEXT_SOURCE_PYPDF,
                    "char_count": len(joined),
                    "page_count": sampled_pages,
                }, profile)
            except Exception:  # noqa: BLE001
                return {
                    "text": None,
                    "status": SAMPLE_TEXT_STATUS_GARBLED,
                    "source": SAMPLE_TEXT_SOURCE_PYPDF,
                    "char_count": 0, "page_count": 0,
                }
        # ---- Unsupported file type ------------------------------
        return _refine_with_profile({
            "text": None,
            "status": SAMPLE_TEXT_STATUS_UNSUPPORTED,
            "source": SAMPLE_TEXT_SOURCE_UNAVAILABLE,
            "char_count": 0, "page_count": 0,
        }, profile)

    def _refine_with_profile(
        result: dict[str, Any], profile: Any,
    ) -> dict[str, Any]:
        """Final pass that promotes ``available`` → ``unreliable``
        when the lightweight profile signals + actual char density
        say the extracted text won't anchor real layout claims.

        Skipped for non-``available`` statuses (those are already
        more pessimistic) and when no profile was supplied (the
        legacy callers — they get the extractor's verdict
        verbatim).
        """
        from j1.processing.llm_advanced_assessment import (
            SAMPLE_TEXT_STATUS_AVAILABLE,
            SAMPLE_TEXT_STATUS_UNRELIABLE,
        )
        # Thresholds duplicated from ``_read_sampled_text`` so this
        # sibling helper is self-contained — Python closures don't
        # expose locals of one inner function to another inside the
        # same enclosing scope. A future refactor can lift these to
        # module-level constants once the function moves out of the
        # route factory.
        _MIN_EXTRACTABLE_RATIO = 0.20
        _MIN_CHARS_PER_PAGE_FOR_RELIABLE = 80
        _MIN_PAGES_FOR_DENSITY_CHECK = 3
        if profile is None:
            return result
        if result.get("status") != SAMPLE_TEXT_STATUS_AVAILABLE:
            return result
        ratio = getattr(profile, "text_extractable_ratio", None)
        page_count = getattr(profile, "page_count", None) or 0
        char_count = int(result.get("char_count") or 0)
        sampled_pages = int(result.get("page_count") or 0)
        # Signal 1: scanned-document signature. The lightweight
        # profiler reports the share of pages with embedded text;
        # a small share means most of the document is image-only.
        if ratio is not None and ratio < _MIN_EXTRACTABLE_RATIO:
            new = dict(result)
            new["status"] = SAMPLE_TEXT_STATUS_UNRELIABLE
            return new
        # Signal 2: sparse text across sampled pages. Skip the
        # check for short docs — a 2-page note legitimately has
        # low char counts. We use ``sampled_pages`` (not total
        # page_count) so the denominator matches what we actually
        # extracted from.
        if (
            page_count >= _MIN_PAGES_FOR_DENSITY_CHECK
            and sampled_pages > 0
            and char_count / sampled_pages
                < _MIN_CHARS_PER_PAGE_FOR_RELIABLE
        ):
            new = dict(result)
            new["status"] = SAMPLE_TEXT_STATUS_UNRELIABLE
            return new
        return result

    def _llm_profile_to_wire(llm_profile: str) -> str:
        """Map the LLM's own profile vocabulary to ExecutionProfile
        wire strings. Mirrors the table in
        :mod:`j1.processing.recommendation_resolver`.
        Anything unrecognised falls back to ``standard`` (the safe
        default the rest of the codebase already uses)."""
        mapping = {
            "quick_index": "minimum_queryable",
            "standard_index": "standard",
            "deep_knowledge_index": "advanced",
            "minimum_queryable": "minimum_queryable",
            "standard": "standard",
            "advanced": "advanced",
        }
        return mapping.get(
            (llm_profile or "").strip().lower(), "standard",
        )

    def _latest_succeeded_run_id(
        ctx: ProjectContext, document_id: str,
    ) -> str | None:
        """Phase 9: derive the previous-active run_id from the runs
        store. Documents no longer carry ``active_run_id``; the
        canonical visibility key is ``active_snapshot_id`` on the
        document and ``created_by_run_id`` on the snapshot. For
        reindex / refresh-enrich the previous-run lookup just needs
        the most recent succeeded run, which the projector's
        ``_find_active_run`` already computes the same way.

        WARNING: this is a heuristic. For *visibility* / *delete
        safety* the source of truth is the snapshot store — use
        ``_protected_run_id_for_document`` instead, which derives
        the protected run from ``DocumentSnapshot.created_by_run_id``
        of the document's ``active_snapshot_id``. The heuristic
        survives here only for the reindex parent-pointer + refresh-
        enrich settings-inheritance lookups, which don't need
        promotion correctness.
        """
        store = _require_run_store()
        prior = store.list_runs(ctx, document_id=document_id)
        sorted_runs = sorted(
            prior,
            key=lambda r: (r.started_at, r.updated_at),
            reverse=True,
        )
        for r in sorted_runs:
            if r.status in (
                RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS,
            ):
                return r.run_id
        return None

    def _protected_run_id_for_document(
        ctx: ProjectContext, document_id: str,
    ) -> str | None:
        """Phase 9 / audit fix: the run that produced the document's
        currently active snapshot. Delete-run guards MUST use this
        instead of the latest-succeeded-run heuristic — a later
        successful but non-promoting run (e.g. CAS conflict, refresh-
        enrich that hasn't yet promoted) would otherwise mask the
        producing run from the guard, leaving the active snapshot's
        producer deletable.

        Returns ``None`` when:
          * the snapshot service / source registry isn't wired
            (legacy / test harness paths);
          * the document has no ``active_snapshot_id``;
          * the snapshot lookup fails for any reason (treated as
            "no protected run" — the guard falls back to the
            heuristic; see the delete handler).
        """
        if snapshot_service is None:
            return None
        try:
            lookup = _require_source_registry()
            doc = lookup._sources.get(ctx, document_id)
        except Exception:  # noqa: BLE001 — registry transient → no guard hit
            return None
        active_snap_id = getattr(doc, "active_snapshot_id", None)
        if not active_snap_id:
            return None
        try:
            snap = snapshot_service.store.get(ctx, active_snap_id)
        except Exception:  # noqa: BLE001 — store transient → no guard hit
            return None
        if snap is None:
            return None
        return getattr(snap, "created_by_run_id", None)

    def _check_cleanup_eligibility(
        ctx: ProjectContext, run_id: str,
    ) -> "CleanUpEligibilityDTO":
        """Single source of truth for "can this run be cleaned up?".

        Consulted by:
          * ``POST /ingestion-runs/{id}/clean-up`` — refusal becomes
            ``{cleaned: false, reason, message}`` (200).
          * ``GET /ingestion-runs/{id}/cleanup-eligibility`` — same
            shape; lets the FE render the Clean Up Run button in the
            right state without trying the action.
          * ``DELETE /ingestion-runs/{id}`` (back-compat) — refusal
            becomes the legacy HTTP 409 with the same human message.

        Rules:
          * Run must exist (else ``RUN_NOT_FOUND``).
          * Must not be in flight (``RUNNING / PAUSED / CANCELLING /
            ASSESSING``) (``PROCESSING_RUN``).
          * Must not be the document's only run (``ONLY_RUN``;
            operators must use Remove Knowledge).
          * Must not be the producer of the document's currently
            active snapshot (``ACTIVE_RUN``; snapshot-store-keyed
            lookup, with the legacy latest-succeeded heuristic as a
            test-only fallback).
        """
        from j1.ingestion_review.dtos import (
            CleanUpEligibilityDTO,
            CLEANUP_REASON_ACTIVE_RUN,
            CLEANUP_REASON_OK,
            CLEANUP_REASON_ONLY_RUN,
            CLEANUP_REASON_PROCESSING_RUN,
            CLEANUP_REASON_RUN_NOT_FOUND,
        )
        store = _require_run_store()
        run = store.get(ctx, run_id)
        if run is None:
            return CleanUpEligibilityDTO(
                run_id=run_id,
                allowed=False,
                reason=CLEANUP_REASON_RUN_NOT_FOUND,
                message=f"Ingestion run {run_id!r} not found.",
            )
        active_states = {
            RunStatus.RUNNING.value, RunStatus.PAUSED.value,
            RunStatus.CANCELLING.value, RunStatus.ASSESSING.value,
        }
        if str(run.status) in active_states:
            return CleanUpEligibilityDTO(
                run_id=run_id,
                allowed=False,
                reason=CLEANUP_REASON_PROCESSING_RUN,
                message=(
                    f"This run is still {run.status}. Cancel it "
                    "before cleaning up."
                ),
                blocking_references={"status": str(run.status)},
            )
        if run.document_id:
            sibling_runs = [
                r for r in store.list_runs(
                    ctx, document_id=run.document_id,
                )
                if r.run_id != run_id
            ]
            if not sibling_runs:
                return CleanUpEligibilityDTO(
                    run_id=run_id,
                    allowed=False,
                    reason=CLEANUP_REASON_ONLY_RUN,
                    message=(
                        "This is the document's only run. Use "
                        "Remove Knowledge on the document instead."
                    ),
                    blocking_references={
                        "documentId": run.document_id,
                    },
                )
            protected_run_id = _protected_run_id_for_document(
                ctx, run.document_id,
            )
            if protected_run_id is None:
                protected_run_id = _latest_succeeded_run_id(
                    ctx, run.document_id,
                )
            if protected_run_id == run_id:
                return CleanUpEligibilityDTO(
                    run_id=run_id,
                    allowed=False,
                    reason=CLEANUP_REASON_ACTIVE_RUN,
                    message=(
                        "This run produced the active knowledge "
                        "snapshot. Re-index the document or use "
                        "Remove Knowledge to replace it before "
                        "cleaning up this run."
                    ),
                    blocking_references={
                        "documentId": run.document_id,
                        "activeRunId": protected_run_id,
                    },
                )
        return CleanUpEligibilityDTO(
            run_id=run_id,
            allowed=True,
            reason=CLEANUP_REASON_OK,
            message="Run is eligible for cleanup.",
        )

    policy = SecurityPolicy(
        authenticator=authenticator,
        anonymous_paths=(
            anonymous_paths
            if anonymous_paths is not None
            else frozenset({
                "/health",
                "/version",
                "/openapi.json",
                "/docs",
                "/docs/oauth2-redirect",
                "/redoc",
            })
        ),
    )

    # ---- OpenAPI / Swagger UI auth ----------------------------------
    # When authentication is enabled, declare the supported credential
    # schemes on the OpenAPI document so Swagger renders the
    # "Authorize" button and operators can paste a Bearer token /
    # API key once and have it sent on every test request. The
    # schemes are documentation-only — actual credential extraction
    # happens in `_security_middleware` below.
    if policy.enabled:
        _install_openapi_security(app, anonymous_paths=policy.anonymous_paths)
    else:
        # Insecure-by-default trap: when no `authenticator` is wired,
        # the scope-required dependency factory no-ops, so admin
        # endpoints (POST /projects, review decisions, control
        # signals) run anonymously. Surface a loud one-time warning at
        # startup so a missing `J1_AUTH_API_KEYS` doesn't sail past
        # operations review. Operators who deliberately want
        # anonymous mode (local dev, ephemeral test stacks) suppress
        # the warning by setting `J1_ALLOW_ANONYMOUS_ADMIN=1`.
        if os.environ.get("J1_ALLOW_ANONYMOUS_ADMIN", "").strip() not in (
            "1", "true", "True", "yes",
        ):
            _log.warning(
                "auth disabled: no authenticator configured; admin "
                "endpoints (POST /projects, /reviews/*/decision, "
                "ingestion-job control signals) run anonymously. "
                "Set J1_AUTH_API_KEYS to require credentials, or set "
                "J1_ALLOW_ANONYMOUS_ADMIN=1 to suppress this warning."
            )

    # ---- Middleware --------------------------------------------------
    # Registration order matters: the last-registered middleware is the
    # outermost wrap. We want request_id to wrap security so the
    # X-Request-Id header is present on early auth-failure responses too.

    @app.middleware("http")
    async def _security_middleware(request: Request, call_next):
        try:
            request.state.security_context = authenticate_request(
                request, policy
            )
        except HTTPException as exc:
            return error_response(
                status_code=exc.status_code,
                code="UNAUTHENTICATED",
                message=str(exc.detail),
                request_id=getattr(request.state, "request_id", uuid.uuid4().hex),
            )
        return await call_next(request)

    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request.state.request_id
        return response

    # ---- Error handlers (uniform envelope) --------------------------

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException) -> JSONResponse:
        return error_response(
            status_code=exc.status_code,
            code=f"HTTP_{exc.status_code}",
            message=str(exc.detail),
            request_id=_req_id(request),
        )

    @app.exception_handler(InvalidIdentifierError)
    async def _bad_id(request: Request, exc: InvalidIdentifierError) -> JSONResponse:
        return error_response(
            status_code=400,
            code="INVALID_IDENTIFIER",
            message=str(exc),
            request_id=_req_id(request),
        )

    @app.exception_handler(DocumentNotFoundError)
    async def _doc_missing(request, exc) -> JSONResponse:
        return error_response(
            status_code=404,
            code="DOCUMENT_NOT_FOUND",
            message=str(exc),
            request_id=_req_id(request),
        )

    @app.exception_handler(ArtifactNotFoundError)
    async def _artifact_missing(request, exc) -> JSONResponse:
        return error_response(
            status_code=404,
            code="ARTIFACT_NOT_FOUND",
            message=str(exc),
            request_id=_req_id(request),
        )

    @app.exception_handler(ReviewItemNotFoundError)
    async def _review_missing(request, exc) -> JSONResponse:
        return error_response(
            status_code=404,
            code="REVIEW_ITEM_NOT_FOUND",
            message=str(exc),
            request_id=_req_id(request),
        )

    @app.exception_handler(ReviewNotFound)
    async def _ingestion_review_missing(request, exc) -> JSONResponse:
        # Mapped to 404 — never 403 — so cross-tenant probing can't
        # distinguish "missing" from "you can't see it." Same shape
        # the rest of the not-found surface uses.
        return error_response(
            status_code=404,
            code="REVIEW_NOT_FOUND",
            message=str(exc),
            request_id=_req_id(request),
        )

    from j1.memory import MemoryNotQueryableError as _MemoryNotQueryableError

    @app.exception_handler(_MemoryNotQueryableError)
    async def _memory_not_queryable(
        request, exc: _MemoryNotQueryableError,
    ) -> JSONResponse:
        # The Unified Memory Resolver refused a project / document
        # active query because the scope isn't queryable yet
        # (compile not finished, document detached, artifacts missing,
        # etc.). The FE renders the structured payload directly —
        # ``queryableStatus`` drives the icon, ``queryableReason``
        # is the operator-facing copy.
        return error_response(
            status_code=409,
            code="MEMORY_NOT_QUERYABLE",
            message=str(exc),
            request_id=_req_id(request),
            details={
                "queryableStatus": exc.queryable_status.value,
                "queryableReason": exc.queryable_reason,
            },
        )

    @app.exception_handler(PathTraversalError)
    async def _path_traversal(request, exc) -> JSONResponse:
        # Defense-in-depth surface for the artifact-content endpoint:
        # if a registry has been tampered with so a `location` field
        # escapes its workspace area, surface the failure as a uniform
        # 404 (don't leak that the path was rejected) but log it
        # loudly so operators / monitoring can flag the registry.
        _log.warning("path traversal blocked: %s", exc)
        return error_response(
            status_code=404,
            code="REVIEW_NOT_FOUND",
            message="artifact content not found",
            request_id=_req_id(request),
        )

    @app.exception_handler(UploadTooLargeError)
    async def _upload_too_large(request, exc: UploadTooLargeError) -> JSONResponse:
        # Streaming intake stops writing as soon as the byte count
        # passes the cap. Surface a 413 with the limit so the FE can
        # tell the user what the cap is.
        return error_response(
            status_code=413,
            code="UPLOAD_TOO_LARGE",
            message=str(exc),
            request_id=_req_id(request),
            details={
                "sizeBytes": exc.size_bytes,
                "maxBytes": exc.max_bytes,
            },
        )

    @app.exception_handler(UnsupportedFileTypeError)
    async def _unsupported_file_type(
        request, exc: UnsupportedFileTypeError,
    ) -> JSONResponse:
        # Boundary check runs before the streaming copy — a rejected
        # type doesn't even get bytes written to disk. 415 carries
        # the offending suffix and the allow-list so the FE can show
        # the user a precise error.
        return error_response(
            status_code=415,
            code="UNSUPPORTED_FILE_TYPE",
            message=str(exc),
            request_id=_req_id(request),
            details={
                "extension": exc.extension,
                "allowedExtensions": list(exc.allowed_extensions),
            },
        )

    @app.exception_handler(AuthorizationError)
    async def _forbidden(request, exc: AuthorizationError) -> JSONResponse:
        details: dict[str, Any] = {}
        if exc.required_scope:
            details["required_scope"] = exc.required_scope
        return error_response(
            status_code=403,
            code="INSUFFICIENT_SCOPE",
            message=str(exc),
            request_id=_req_id(request),
            details=details or None,
        )

    @app.exception_handler(ApplicationError)
    async def _app_error(request, exc: ApplicationError) -> JSONResponse:
        return error_response(
            status_code=400,
            code="APPLICATION_ERROR",
            message=str(exc),
            request_id=_req_id(request),
        )

    @app.exception_handler(J1Error)
    async def _j1_error(request, exc: J1Error) -> JSONResponse:
        return error_response(
            status_code=400,
            code="J1_ERROR",
            message=str(exc),
            request_id=_req_id(request),
            details={"type": type(exc).__name__},
        )

    @app.exception_handler(ProfileNotAllowedError)
    async def _profile_not_allowed(
        request: Request, exc: ProfileNotAllowedError,
    ) -> JSONResponse:
        # Distinct from `ValueError` (which is the typo case) —
        # this is a policy violation, so the response carries the
        # current allow-list so the FE re-renders the picker
        # without re-fetching the assessment plan. 403 not 400
        # because the request itself is well-formed; the
        # deployment is refusing it.
        return error_response(
            status_code=403,
            code="PROFILE_NOT_ALLOWED",
            message=str(exc),
            request_id=_req_id(request),
            details={
                "requestedProfile": exc.requested.value,
                "allowedProfiles": sorted(p.value for p in exc.allowed),
            },
        )

    @app.exception_handler(ValueError)
    async def _value_error(request, exc: ValueError) -> JSONResponse:
        return error_response(
            status_code=400,
            code="INVALID_ARGUMENT",
            message=str(exc),
            request_id=_req_id(request),
        )

    # ---- Dependencies ------------------------------------------------

    def _resolve_profile_or_400(
        provided: str | None,
    ) -> tuple[ExecutionProfile, str]:
        """Resolve the caller's `selectedProfile` against the
        deployment policy.

        Two failure modes, each routed to its own exception
        handler so the JSON envelope carries a structured `code`:

          * Unknown wire string (typo) → raises `ValueError`
            with a message that names the field + lists valid
            wire strings. The global handler returns 400 with
            `code=INVALID_ARGUMENT`.
          * Recognised profile but not on the deployment
            allow-list → policy raises `ProfileNotAllowedError`,
            the dedicated handler returns 403 with
            `code=PROFILE_NOT_ALLOWED` and `allowedProfiles` in
            the details so the FE can re-render the picker.

        When `provided` is None, the policy's default profile
        applies and `source == "default"`. When `provided` is a
        valid + allowed profile, `source == "rest"`. Never
        silently swaps profiles — that's the policy's whole job.
        """
        try:
            return _profile_policy.resolve(provided)
        except ProfileNotAllowedError:
            # Re-raise verbatim so the dedicated handler picks
            # it up (the handler enriches the body with
            # `allowedProfiles`).
            raise
        except ValueError as exc:
            # The enum's stock message is "'foo' is not a valid
            # ExecutionProfile" — too generic for an FE error
            # banner. Re-wrap so the field name + allow-list
            # surface verbatim, matching the original handler.
            allowed_values = ", ".join(p.value for p in ExecutionProfile)
            raise ValueError(
                f"selectedProfile {provided!r} is not a recognised "
                f"execution profile (allowed: {allowed_values})"
            ) from exc

    def _resolve_compiler_kind(provided: str | None) -> str:
        """Resolve + validate `compilerKind` against the runtime.

 Three rules:
 1. If the caller provided a value AND `processing_capabilities`
 knows the registered set AND the value isn't in it → 400.
 2. If the caller omitted the value AND `processing_capabilities`
 carries a default → use the default.
 3. If the caller omitted the value AND no default is
 configured → 400 with a clear message naming the field.
 """
        caps = processing_capabilities
        if provided is not None:
            value = provided.strip()
            if not value:
                raise ValueError("compilerKind must be a non-empty string")
            if caps is not None and caps.compiler_kinds:
                if value not in caps.compiler_kinds:
                    raise ValueError(
                        f"unknown compilerKind {value!r}; the worker has "
                        f"registered: {sorted(caps.compiler_kinds)}"
                    )
            return value
        # Not provided — fall back to default.
        if caps is not None and caps.default_compiler_kind:
            return caps.default_compiler_kind
        raise ValueError(
            "compilerKind is required (the runtime did not configure a "
            "default — pass `processing_capabilities=` to create_rest_api "
            "with `default_compiler_kind=` set, or include `compilerKind` "
            "in the request body)."
        )

    def _validate_optional_processor_kind(
        provided: str | None,
        registered: frozenset[str],
        field_name: str,
    ) -> str | None:
        """Validate an optional processor-kind field.

 Unlike `_resolve_compiler_kind`, optional fields are NOT
 defaulted — `None` means "skip the stage". Validation only
 kicks in when the caller actually supplied a value AND the
 runtime has at least one registered kind for the role.
 """
        if provided is None:
            return None
        value = provided.strip()
        if not value:
            return None
        if registered and value not in registered:
            raise ValueError(
                f"unknown {field_name} {value!r}; the worker has "
                f"registered: {sorted(registered)}"
            )
        return value

    def _resolve_optional_processor_kind(
        provided: str | None,
        registered: frozenset[str],
        field_name: str,
    ) -> str | None:
        """Resolve an optional processor-kind field with auto-default
 semantics for the user-facing upload path.

 Three rules:
 1. Caller provided → validate against `registered` (same as
 `_validate_optional_processor_kind`).
 2. Caller omitted AND exactly one kind is registered for the
 role → return that kind. The user-facing FE upload sends
 only a file; without this, the workflow's `available_steps`
 collapses to `{compile}` and every other stage is silently
 skipped — even though the deployment had wired them.
 3. Caller omitted AND zero or multiple kinds registered →
 return None (the stage stays unrunnable; operator chooses
 explicitly when ambiguous).

 This is the bug fix for "uploaded runs only execute compile":
 the previous helper returned None on every omission regardless
 of what the deployment had registered. The validation path on
 provided values is unchanged.
 """
        if provided is not None:
            value = provided.strip()
            if value:
                if registered and value not in registered:
                    raise ValueError(
                        f"unknown {field_name} {value!r}; the worker has "
                        f"registered: {sorted(registered)}"
                    )
                return value
        # Caller omitted — auto-pick only when the choice is
        # unambiguous (one registered kind). Multiple registered =
        # operator must choose; none = stage stays skipped.
        if len(registered) == 1:
            return next(iter(registered))
        return None

    # Header-typed parameters surface in OpenAPI / Swagger UI as
    # editable per-endpoint inputs, so operators can test the API
    # interactively. The actual extraction logic still goes through
    # the (pluggable) `resolver`, so a deployment that overrides
    # `context_resolver=` keeps full control over how tenant /
    # project are resolved (e.g. from a JWT). The Header bindings
    # here are documentation only.
    def get_ctx(
        request: Request,
        x_tenant_id: str | None = Header(  # noqa: ARG001 — declared for OpenAPI; runtime read by `resolver`
            None,
            alias=TENANT_HEADER,
            description=(
                "Tenant identifier. Required for all tenant-scoped "
                "endpoints. The default resolver reads it from this "
                "header; deployments using `context_resolver=` may "
                "ignore it."
            ),
        ),
        x_project_id: str | None = Header(  # noqa: ARG001 — declared for OpenAPI
            None,
            alias=PROJECT_HEADER,
            description=(
                "Project identifier within the tenant. Required for "
                "endpoints that operate on a single project. The "
                "default resolver reads it from this header."
            ),
        ),
    ) -> ProjectContext:
        return resolver(request)

    def get_tenant(
        request: Request,
        x_tenant_id: str | None = Header(  # noqa: ARG001 — declared for OpenAPI
            None,
            alias=TENANT_HEADER,
            description="Tenant identifier — required.",
        ),
    ) -> str:
        tenant_id = request.headers.get(TENANT_HEADER)
        if not tenant_id:
            raise HTTPException(
                status_code=400, detail=f"{TENANT_HEADER} header required"
            )
        # Validate tenant via ProjectContext (raises InvalidIdentifierError →
        # handled by the global exception handler).
        ProjectContext(tenant_id=tenant_id, project_id="placeholder")
        return tenant_id

    def get_security(request: Request) -> SecurityContext:
        return getattr(request.state, "security_context", ANONYMOUS_CONTEXT)

    def scope_required(scope: str):
        """FastAPI dependency factory enforcing a single scope.

 No-op when the security context is anonymous (i.e. auth disabled or
 the route is in `anonymous_paths`) — that decision was already made
 upstream by the security middleware.
 """

        def _dep(request: Request) -> None:
            _require_scope(get_security(request), scope)

        return _dep

    def require_search():
        if facade.search is None:
            raise HTTPException(503, "search capability not configured")
        return facade.search

    def require_job_status():
        if facade.job_status is None:
            raise HTTPException(503, "job-status capability not configured")
        return facade.job_status

    def require_job_starter():
        if job_starter is None:
            raise HTTPException(503, "ingestion job starter not configured")
        return job_starter

    def require_project_admin():
        if facade.project_admin is None:
            raise HTTPException(503, "project admin capability not configured")
        return facade.project_admin

    def require_job_control():
        if facade.job_control is None:
            raise HTTPException(503, "job control capability not configured")
        return facade.job_control

    def require_cost_summary():
        if facade.cost_summary is None:
            raise HTTPException(503, "cost summary capability not configured")
        return facade.cost_summary

    def require_review():
        if facade.review is None:
            raise HTTPException(503, "review capability not configured")
        return facade.review

    def require_bulk_export():
        if bulk_export is None:
            raise HTTPException(503, "bulk export capability not configured")
        return bulk_export

    def require_bulk_import():
        if bulk_import is None:
            raise HTTPException(503, "bulk import capability not configured")
        return bulk_import

    # ---- Projects ----------------------------------------------------

    @app.post(
        "/projects",
        tags=["projects"],
        summary="Create a project workspace",
        description=(
            "Provisions the per-project filesystem layout under the resolved "
            "tenant. Idempotent — repeated calls return the same project."
        ),
        dependencies=[Depends(scope_required(SCOPE_ADMIN))],
    )
    def post_project(
        request: Request,
        body: ProjectCreateRequest,
        tenant_id: str = Depends(get_tenant),
        admin=Depends(require_project_admin),
    ) -> dict[str, Any]:
        result = admin.create_project(
            tenant_id,
            ProjectCreateRequestDTO(
                project_id=body.project_id,
                profile=body.profile,
            ),
        )
        record = ProjectRecord(
            project_id=result.project_id,
            tenant_id=result.tenant_id,
            profile=result.profile,
        )
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    # ---- Documents ---------------------------------------------------

    @app.post(
        "/documents",
        tags=["documents"],
        summary="Register a document",
        description=(
            "Upload a document into a project. Returns the registered "
            "document record. Duplicate uploads (by checksum) return the "
            "existing record."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    def post_document(
        request: Request,
        file: UploadFile = File(...),
        actor: str = Form("system"),
        correlation_id: str | None = Form(default=None),
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        try:
            dto = facade.ingestion.register_document(
                ctx,
                file.file,
                original_filename=file.filename or "upload.bin",
                mime_type=file.content_type,
                actor=actor,
                correlation_id=correlation_id,
            )
        except DuplicateDocumentError as exc:
            existing = facade.source_lookup.get_source(
                ctx, exc.existing_document_id
            )
            record = _document_record(existing)
            publish_document_uploaded(
                event_bus, security=security, request_id=_req_id(request),
                tenant_id=ctx.tenant_id, document_id=existing.document_id,
                checksum=existing.checksum, file_size=existing.file_size,
                mime_type=existing.mime_type, duplicate=True,
            )
            return envelope(
                record.model_dump(by_alias=True),
                _req_id(request),
                meta={"duplicate": True},
            )
        record = _document_record(dto)
        publish_document_uploaded(
            event_bus, security=security, request_id=_req_id(request),
            tenant_id=ctx.tenant_id, document_id=dto.document_id,
            checksum=dto.checksum, file_size=dto.file_size,
            mime_type=dto.mime_type, duplicate=False,
        )
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    @app.get(
        "/documents/{document_id}",
        tags=["documents"],
        summary="Get document metadata",
        dependencies=[Depends(scope_required(SCOPE_READ))],
    )
    def get_document(
        request: Request,
        document_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        dto = facade.source_lookup.get_source(ctx, document_id)
        record = _document_record(dto)
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    @app.post(
        "/documents/{document_id}/ingest",
        tags=["documents"],
        summary="Start an ingestion job for a document",
        description=(
            "Trigger the processing pipeline for a previously-registered "
            "document. Returns the job identifier — poll "
            "`/ingestion-jobs/{jobId}` for status."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def ingest_document(
        request: Request,
        document_id: str,
        body: IngestRequest,
        ctx: ProjectContext = Depends(get_ctx),
        starter: JobStarter = Depends(require_job_starter),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        # Resolve the execution profile against the deployment
        # policy FIRST (same code path as `POST /ingestion-runs`).
        # Unknown wire strings → 400; forbidden profiles → 403.
        # When the body omitted `selectedProfile`, the policy's
        # default kicks in and the resolved value is stamped back
        # onto `body` so the workflow always sees an explicit
        # profile rather than `None`.
        resolved_profile, _profile_source = _resolve_profile_or_400(
            body.selected_profile,
        )
        body.selected_profile = resolved_profile.value
        # Resolve / validate processor kinds at the boundary; mutate
        # the body so the deployment-supplied job_starter sees the
        # resolved values rather than re-implementing this logic.
        body.compiler_kind = _resolve_compiler_kind(body.compiler_kind)
        body.graph_builder_kind = _validate_optional_processor_kind(
            body.graph_builder_kind,
            (processing_capabilities.graph_builder_kinds
             if processing_capabilities else frozenset()),
            "graphBuilderKind",
        )
        body.enricher_kind = _validate_optional_processor_kind(
            body.enricher_kind,
            (processing_capabilities.enricher_kinds
             if processing_capabilities else frozenset()),
            "enricherKind",
        )
        body.indexer_kind = _validate_optional_processor_kind(
            body.indexer_kind,
            (processing_capabilities.indexer_kinds
             if processing_capabilities else frozenset()),
            "indexerKind",
        )
        # Verify the document exists before starting work.
        facade.source_lookup.get_source(ctx, document_id)
        job_id = await starter(ctx, document_id, body)
        publish_document_ingestion_started(
            event_bus, security=security, request_id=_req_id(request),
            tenant_id=ctx.tenant_id, job_id=job_id,
            document_id=document_id, project_wide=False,
        )
        record = JobStartRecord(
            job_id=job_id, document_id=document_id, status="running"
        )
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    @app.post(
        "/documents/{document_id}/assessment-plan",
        tags=["documents"],
        summary="Run pre-compile assessment + recommend execution profile",
        description=(
            "Synchronous, no workflow dispatch. Runs the deterministic "
            "profiler + rule-based assessment planner against the "
            "registered document and returns:\n\n"
            "* `recommendedProfile` — the planner's suggestion based "
            "on document signals (scanned pages, tables, images, "
            "length).\n"
            "* `availableProfiles` — the full profile catalogue with "
            "expected speed / LLM usage / capability flags so the FE "
            "can render the picker without re-deriving copy.\n"
            "* `reasons` — operator-readable strings explaining the "
            "recommendation.\n\n"
            "The user then chooses a profile and calls "
            "`POST /documents/{id}/ingest` (or `POST /ingestion-runs` "
            "for the upload-and-start path) with `selectedProfile=...` "
            "to dispatch the workflow."
        ),
        dependencies=[Depends(scope_required(SCOPE_READ))],
    )
    def post_document_assessment_plan(
        request: Request,
        document_id: str,
        body: AssessmentPlanRequest | None = Body(default=None),
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        from j1.domains import DOMAIN_GENERAL, default_registry
        from j1.processing.assessment import DefaultAssessmentPlanner
        from j1.processing.assessment_decision import (
            AssessmentDecision,
            new_decision_id,
        )
        from j1.processing.execution_profile import (
            ExecutionProfile,
            profile_details,
        )
        from j1.processing.profiling import DeterministicDocumentProfiler
        from j1.processing.recommendation_resolver import (
            ProfilerInputs,
            resolve_recommendation,
        )

        # 1. Resolve the document (404 when not registered).
        try:
            doc_dto = facade.source_lookup.get_source(ctx, document_id)
        except Exception:
            raise HTTPException(
                404, f"document {document_id!r} not found",
            )

        # 2. Resolve the source path. When the workspace seam isn't
        # wired (test harnesses), fall back to an empty path — the
        # profiler will return a degraded profile with warnings,
        # and the recommendation degrades to `standard` (the safe
        # default).
        profile = None
        source_path = None
        if workspace is not None and doc_dto.stored_filename:
            source_path = workspace.raw(ctx) / doc_dto.stored_filename
            if not source_path.exists():
                raise HTTPException(
                    409,
                    f"original uploaded file for document "
                    f"{document_id!r} is missing on disk "
                    f"({doc_dto.stored_filename}); cannot run "
                    "assessment.",
                )

        # 3. Run the profiler. Cheap + deterministic — typically
        # <100ms even for large PDFs (pypdf metadata only).
        if source_path is not None:
            try:
                profile = DeterministicDocumentProfiler().profile(
                    document_id, str(source_path),
                )
            except FileNotFoundError as exc:
                raise HTTPException(409, str(exc))

        # 4. Run the rule-based assessment planner (no LLM).
        assessment_plan_payload: dict[str, Any] | None = None
        if profile is not None:
            try:
                assessment_plan = DefaultAssessmentPlanner().assess(profile)
                assessment_plan_payload = (
                    assessment_plan.to_payload()
                    if hasattr(assessment_plan, "to_payload")
                    else None
                )
            except Exception:  # noqa: BLE001 — assessment is advisory
                assessment_plan_payload = None

        # 5. Resolve the recommendation via the precedence chain:
        # env hard-disable > user selection > active-domain rules >
        # general-domain rules > lightweight assessment fallback >
        # system default. The resolver itself is pure data — no
        # MinerU, no RAGAnything, no LLM, no PyMuPDF.
        try:
            registry = default_registry()
        except Exception:  # noqa: BLE001 — degrade gracefully without a registry
            registry = None
        # Domain selection (user > workspace default > general).
        # ``selectedDomainId`` is operator intent — it takes precedence
        # over the deployment's ``workspace_default_domain_id`` knob.
        # An unknown id falls back to ``general`` and surfaces a
        # warning so the operator sees the mismatch.
        domain_warnings: list[str] = []
        active_domain = None
        general_domain = None
        selected_domain_id_input = (
            body.selected_domain_id if body is not None else None
        )
        if registry is not None:
            general_domain = registry.get(DOMAIN_GENERAL)
            preferred_id = (
                selected_domain_id_input
                or workspace_default_domain_id
                or DOMAIN_GENERAL
            )
            active_domain = registry.get(preferred_id)
            if active_domain is None:
                # Fall through to general + warn so the FE knows the
                # operator's pick wasn't honoured.
                active_domain = general_domain
                if preferred_id != DOMAIN_GENERAL:
                    domain_warnings.append(
                        f"Requested domain {preferred_id!r} is not "
                        "registered; falling back to general."
                    )
        profiler_inputs = None
        if profile is not None:
            profiler_inputs = ProfilerInputs(
                has_images=bool(profile.has_images),
                has_tables=bool(profile.has_tables),
                has_scanned_pages=bool(profile.has_scanned_pages),
                text_extractable_ratio=(
                    profile.text_extractable_ratio
                    if profile.text_extractable_ratio is not None
                    else 1.0
                ),
                page_count=profile.page_count or 0,
                warnings=tuple(profile.warnings or ()),
            )
        user_selected_profile_input: str | None = (
            body.selected_profile if body is not None else None
        )
        outcome = resolve_recommendation(
            filename=doc_dto.original_filename,
            title=getattr(doc_dto, "title", None),
            active_domain=active_domain,
            general_domain=general_domain,
            profiler_inputs=profiler_inputs,
            user_selected_profile=user_selected_profile_input,
            policy=_profile_policy,
        )

        # 6. Build the profile catalogue. Stable shape — the FE
        # renders these as radio buttons + cost copy.
        available = [profile_details(p) for p in ExecutionProfile]

        # 7. Compile-option preview — a small set of hedged hints
        # the FE can render BELOW the picker without claiming exact
        # detection. The compile / RAGAnything / enrichment layers
        # remain the authority on what actually runs; this is for
        # operator transparency only.
        winner_hints = next(
            (r.hints for r in outcome.matched_rules if r.winner),
            None,
        )
        compile_option_preview = {
            "suspectedTables": bool(
                (winner_hints.likely_tables if winner_hints else False)
                or (profile.has_tables if profile is not None else False)
            ),
            "suspectedImages": bool(
                (winner_hints.likely_images if winner_hints else False)
                or (profile.has_images if profile is not None else False)
            ),
            "suspectedScanned": bool(
                (winner_hints.likely_scanned if winner_hints else False)
                or (profile.has_scanned_pages if profile is not None else False)
            ),
            "suspectedRequirements": bool(
                winner_hints.likely_requirements if winner_hints else False
            ),
            "suspectedLongDocument": bool(
                (winner_hints.likely_long_document if winner_hints else False)
                or (
                    profile is not None
                    and (profile.page_count or 0) > 100
                )
            ),
            "note": (
                "These are rule-based hints, not exact detection. "
                "The compile stage decides actual behaviour."
            ),
        }

        # 7b. Look up the most-recent persisted decision so the
        # picker can SURFACE (not re-run) a previous LLM Advanced
        # Assessment result. The LLM call itself is uncached — each
        # click of "Run Advanced Assessment" mints a fresh decision
        # — but once the user HAS run it, the picker carries the
        # result + suggested next steps through to any subsequent
        # refresh so the dialog state survives a re-fetch.
        prior_llm_payload: dict[str, Any] | None = None
        prior_next_steps: tuple[str, ...] = ()
        if assessment_decision_store is not None:
            try:
                prior = assessment_decision_store.latest_for_document(
                    ctx, document_id,
                )
            except Exception:  # noqa: BLE001 — best-effort lookup
                prior = None
            if prior is not None:
                prior_llm_payload = prior.llm_assessment_result
                prior_next_steps = tuple(prior.recommended_next_steps)

        # 8. Persist the decision so the ingest endpoint can consume
        # the same recommendation the FE just showed the operator.
        # When the store seam isn't wired (legacy/test deployments)
        # the endpoint still works — the caller just won't get an
        # ``assessmentDecisionId`` to thread through ingest, and the
        # workflow falls back to its rebuild path.
        active_domain_id = (
            active_domain.id
            if active_domain is not None
            else (general_domain.id if general_domain is not None
                  else DOMAIN_GENERAL)
        )
        decision_warnings = list(outcome.warnings) + domain_warnings
        active_rule_payloads = [
            r.to_payload()
            for r in outcome.matched_rules
            if r.domain_id == active_domain_id
        ]
        general_rule_payloads = [
            r.to_payload()
            for r in outcome.matched_rules
            if r.domain_id != active_domain_id
            and general_domain is not None
            and r.domain_id == general_domain.id
        ]
        decision: AssessmentDecision | None = None
        if assessment_decision_store is not None:
            decision = AssessmentDecision(
                assessment_decision_id=new_decision_id(),
                document_id=document_id,
                document_version_id=getattr(
                    doc_dto, "current_version_id", None,
                ),
                file_hash=getattr(doc_dto, "checksum", None),
                selected_domain_id=active_domain_id,
                lightweight_assessment=assessment_plan_payload,
                matched_domain_rules=tuple(active_rule_payloads),
                matched_general_rules=tuple(general_rule_payloads),
                recommended_profile=outcome.profile.value,
                selected_profile=user_selected_profile_input,
                effective_profile=outcome.profile.value,
                recommendation_source=outcome.source,
                fallback_used=outcome.fallback_used,
                compile_option_preview=compile_option_preview,
                warnings=tuple(decision_warnings),
                # Carry-forward the previous LLM result + suggested
                # next steps so the picker keeps surfacing them
                # across refreshes. NOT a cache of the LLM call:
                # every Run Advanced Assessment click mints a fresh
                # decision via ``/advanced-assessment``; this just
                # avoids losing the previous outcome on the picker's
                # next re-fetch.
                llm_assessment_result=prior_llm_payload,
                recommended_next_steps=prior_next_steps,
            )
            try:
                assessment_decision_store.upsert(ctx, decision)
            except Exception:  # noqa: BLE001 — store IO is best-effort
                decision = None

        response_body: dict[str, Any] = {
            "documentId": document_id,
            "assessmentDecisionId": (
                decision.assessment_decision_id
                if decision is not None else None
            ),
            "selectedDomainId": active_domain_id,
            "recommendedProfile": outcome.profile.value,
            "recommendationSource": outcome.source,
            "fallbackUsed": outcome.fallback_used,
            "matchedRules": [r.to_payload() for r in outcome.matched_rules],
            "availableProfiles": available,
            "reasons": list(outcome.reasons),
            "assessment": assessment_plan_payload,
            "compileOptionPreview": compile_option_preview,
            "warnings": decision_warnings,
            # Surfaced so the FE picker can render the LLM-driven
            # recommendation, sample-text status, and suggested
            # manual steps after the operator has run Advanced
            # Assessment. ``None`` when the operator hasn't run it.
            "llmAssessment": prior_llm_payload,
            "recommendedNextSteps": list(prior_next_steps),
        }
        return envelope(response_body, _req_id(request))

    @app.post(
        "/documents/{document_id}/advanced-assessment",
        tags=["documents"],
        summary="Run LLM Advanced Assessment (manual, opt-in)",
        description=(
            "Operator-triggered. Sends sampled text + lightweight "
            "signals to a configured LLM and returns a structured "
            "complexity / profile recommendation. NEVER runs "
            "automatically — the default Index path is lightweight "
            "and only uses pypdf-based signals.\n\n"
            "Guardrails (size, page count, sampled-text cap) refuse "
            "expensive inputs with a structured payload so the FE "
            "asks the user to pick manually. Output is strict JSON "
            "matching ``LLMAdvancedAssessmentResult`` — the LLM is "
            "NOT asked to answer document questions."
        ),
        dependencies=[Depends(scope_required(SCOPE_READ))],
    )
    def post_document_advanced_assessment(
        request: Request,
        document_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        from j1.domains import DOMAIN_GENERAL
        from j1.processing.assessment import DefaultAssessmentPlanner
        from j1.processing.llm_advanced_assessment import (
            LLMAdvancedAssessmentInputs,
            REFUSAL_LLM_UNAVAILABLE,
            STATUS_OK,
            STATUS_REFUSED,
        )
        from j1.processing.profiling import DeterministicDocumentProfiler
        # 1. Resolve the document.
        try:
            doc_dto = facade.source_lookup.get_source(ctx, document_id)
        except Exception:
            raise HTTPException(
                404, f"document {document_id!r} not found",
            )
        # 2. If no service is wired, return a structured refusal —
        # the endpoint is intentionally NON-fatal because the rest
        # of the Index path doesn't need this to function.
        if llm_advanced_assessment_service is None:
            return envelope(
                {
                    "documentId": document_id,
                    "assessmentDecisionId": None,
                    "result": {
                        "status": STATUS_REFUSED,
                        "refusalReason": REFUSAL_LLM_UNAVAILABLE,
                        "message": (
                            "Advanced Assessment is not configured "
                            "in this deployment."
                        ),
                        "documentComplexity": None,
                        "recommendedProfile": None,
                        "confidence": None,
                        "detectedSignals": {},
                        "recommendedNextSteps": [],
                        "reasoningSummary": [],
                        "warnings": [
                            "Advanced Assessment is not configured "
                            "in this deployment."
                        ],
                    },
                },
                _req_id(request),
            )
        # 3. Build profile + sampled text. Cheap — same path the
        # standard /assessment-plan uses. We DELIBERATELY don't
        # invoke MinerU / vision / full parse here.
        from j1.processing.llm_advanced_assessment import (
            SAMPLE_TEXT_SOURCE_UNAVAILABLE,
            SAMPLE_TEXT_STATUS_AVAILABLE,
            SAMPLE_TEXT_STATUS_UNSUPPORTED,
        )
        profile = None
        sample = {
            "text": None,
            "status": SAMPLE_TEXT_STATUS_UNSUPPORTED,
            "source": SAMPLE_TEXT_SOURCE_UNAVAILABLE,
            "char_count": 0,
            "page_count": 0,
        }
        if workspace is not None and doc_dto.stored_filename:
            source_path = workspace.raw(ctx) / doc_dto.stored_filename
            if source_path.exists():
                try:
                    profile = DeterministicDocumentProfiler().profile(
                        document_id, str(source_path),
                    )
                except FileNotFoundError:
                    profile = None
                # Structured sampled-text read. Always returns a
                # dict — the LLM service inspects ``status`` to
                # decide whether to make layout claims at all.
                try:
                    sample = _read_sampled_text(
                        source_path, max_chars=60_000, profile=profile,
                    )
                except Exception:  # noqa: BLE001 — sampling is advisory
                    sample = {
                        "text": None,
                        "status": SAMPLE_TEXT_STATUS_UNSUPPORTED,
                        "source": SAMPLE_TEXT_SOURCE_UNAVAILABLE,
                        "char_count": 0,
                        "page_count": 0,
                    }
        lightweight_payload: dict[str, Any] | None = None
        if profile is not None:
            try:
                lightweight_payload = (
                    DefaultAssessmentPlanner().assess(profile).to_payload()
                )
            except Exception:  # noqa: BLE001 — advisory
                lightweight_payload = None
        # 4. Run the service. Threads sample-text provenance through
        # so the prompt can hedge + the result can surface a
        # warning when extraction wasn't reliable.
        inputs = LLMAdvancedAssessmentInputs(
            document_id=document_id,
            filename=getattr(doc_dto, "original_filename", None),
            title=getattr(doc_dto, "title", None),
            file_size_bytes=getattr(doc_dto, "file_size", None),
            page_count=(profile.page_count if profile is not None else None),
            sampled_text=sample["text"],
            sample_text_status=sample["status"],
            sample_text_source=sample["source"],
            sampled_text_char_count=int(sample["char_count"] or 0),
            sampled_page_count=int(sample["page_count"] or 0),
            lightweight_assessment_payload=lightweight_payload,
        )
        result = llm_advanced_assessment_service.run(inputs)
        # 4b. Stamp the classifier's verdict onto the result. The
        # REST handler is the authority on sample-text provenance
        # (it runs the extractor); the service uses these fields
        # for prompt-building and warning-hedging, but stub /
        # legacy service implementations might not propagate them.
        # We rewrite the result here so the wire response is
        # ALWAYS consistent with what the classifier observed,
        # and we layer the unreliable-text warning on top when
        # appropriate.
        from j1.processing.llm_advanced_assessment import (
            LLMAdvancedAssessmentResult as _LLMResult,
            SAMPLE_TEXT_STATUS_AVAILABLE,
            SAMPLE_TEXT_UNRELIABLE_WARNING,
        )
        if (
            result.status == STATUS_OK
            and (
                result.sample_text_status != sample["status"]
                or result.sample_text_source != sample["source"]
                or result.sampled_text_char_count != int(sample["char_count"] or 0)
                or result.sampled_page_count != int(sample["page_count"] or 0)
            )
        ):
            existing_warnings = list(result.warnings)
            if sample["status"] != SAMPLE_TEXT_STATUS_AVAILABLE:
                if SAMPLE_TEXT_UNRELIABLE_WARNING not in existing_warnings:
                    existing_warnings.insert(0, SAMPLE_TEXT_UNRELIABLE_WARNING)
                hedged_signals = {
                    k: ("suspected" if v == "likely" else v)
                    for k, v in dict(result.detected_signals).items()
                }
            else:
                hedged_signals = dict(result.detected_signals)
            result = _LLMResult(
                status=result.status,
                refusal_reason=result.refusal_reason,
                message=result.message,
                document_complexity=result.document_complexity,
                recommended_profile=result.recommended_profile,
                confidence=result.confidence,
                detected_signals=hedged_signals,
                recommended_next_steps=result.recommended_next_steps,
                reasoning_summary=result.reasoning_summary,
                warnings=tuple(existing_warnings),
                sample_text_status=sample["status"],
                sample_text_source=sample["source"],
                sampled_text_char_count=int(sample["char_count"] or 0),
                sampled_page_count=int(sample["page_count"] or 0),
            )
        # 5. Persist the result onto a NEW AssessmentDecision when
        # the store is wired AND the result was OK. Refusals are
        # surfaced to the FE but never written — the picker keeps
        # whatever decision it already had.
        decision_id: str | None = None
        if (
            assessment_decision_store is not None
            and result.status != STATUS_REFUSED
        ):
            from j1.processing.assessment_decision import (
                AssessmentDecision, new_decision_id,
            )
            decision = AssessmentDecision(
                assessment_decision_id=new_decision_id(),
                document_id=document_id,
                file_hash=getattr(doc_dto, "checksum", None),
                selected_domain_id=DOMAIN_GENERAL,
                lightweight_assessment=lightweight_payload,
                recommended_profile=_llm_profile_to_wire(
                    result.recommended_profile or "standard_index",
                ),
                effective_profile=_llm_profile_to_wire(
                    result.recommended_profile or "standard_index",
                ),
                recommendation_source="llm_advanced_assessment",
                fallback_used=False,
                compile_option_preview={
                    "note": (
                        "These are LLM-based hints, not exact "
                        "detection. The compile stage decides "
                        "actual behaviour."
                    ),
                },
                warnings=tuple(result.warnings),
                llm_assessment_result=result.to_payload(),
                recommended_next_steps=tuple(
                    result.recommended_next_steps,
                ),
            )
            try:
                assessment_decision_store.upsert(ctx, decision)
                decision_id = decision.assessment_decision_id
            except Exception:  # noqa: BLE001 — advisory
                decision_id = None
        return envelope(
            {
                "documentId": document_id,
                "assessmentDecisionId": decision_id,
                "result": result.to_payload(),
            },
            _req_id(request),
        )

    @app.get(
        "/documents/{document_id}/manual-actions",
        tags=["documents"],
        summary="List post-index manual actions (advanced steps)",
        description=(
            "Vocabulary of operator-triggered actions exposed AFTER "
            "the default Index path completes (LLM Advanced "
            "Assessment, domain enrichment, knowledge memory, "
            "entity normalization, deep knowledge index, multimodal "
            "enrichment).\n\n"
            "Each entry carries an ``id``, FE-rendered ``label``, "
            "operator-readable ``description``, ``costNote``, and "
            "``status`` (``available`` / ``not_implemented`` / "
            "``disabled``). The FE renders one button per action."
        ),
        dependencies=[Depends(scope_required(SCOPE_READ))],
    )
    def get_document_manual_actions(
        request: Request,
        document_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        # The document must exist so a stranger can't enumerate
        # actions against an unknown id (preserves the 404 contract
        # the rest of the document surface uses).
        try:
            facade.source_lookup.get_source(ctx, document_id)
        except Exception:
            raise HTTPException(
                404, f"document {document_id!r} not found",
            )
        from j1.processing.manual_actions import list_manual_actions
        return envelope(
            {
                "documentId": document_id,
                "actions": [
                    a.to_payload() for a in list_manual_actions()
                ],
            },
            _req_id(request),
        )

    @app.post(
        "/documents/{document_id}/manual-actions/run-domain-enrichment",
        tags=["documents"],
        summary="Run Domain Enrichment (manual action)",
        description=(
            "Operator-triggered post-index enrichment. Reuses the "
            "active snapshot's compile artifacts (no MinerU / "
            "RAGAnything re-parse) and runs the active domain pack's "
            "enricher overlay against them — appending fresh "
            "``enriched.*`` artifacts under a candidate snapshot.\n\n"
            "Rejects (HTTP 409) when:\n"
            "  * the document is detached / removed,\n"
            "  * the document has no active snapshot (no successful "
            "baseline run yet),\n"
            "  * another manual action / ingest run is already in "
            "flight against the document.\n\n"
            "Returns the new ``manualActionRunId`` so the FE can poll "
            "``GET /ingestion-runs/{id}`` for live status. The "
            "candidate snapshot only becomes active when the "
            "enrichment workflow reaches a usable terminal state — "
            "a failed manual action preserves the previous active."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def post_document_manual_action_run_domain_enrichment(
        request: Request,
        document_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        starter: JobStarter = Depends(require_job_starter),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        from j1.processing.manual_actions import (
            ACTION_RUN_DOMAIN_ENRICHMENT,
            is_manual_action_enabled,
        )

        # 1. Feature-flag gate. 403 mirrors the spec's "disabled by
        # deployment settings" path so the FE can render an
        # actionable message instead of a 5xx.
        if not is_manual_action_enabled(ACTION_RUN_DOMAIN_ENRICHMENT):
            raise HTTPException(
                403,
                "Manual action 'run_domain_enrichment' is disabled "
                "by deployment settings.",
            )

        store = _require_run_store()

        # 2. Resolve document + attached-state guard.
        try:
            doc_dto = facade.source_lookup.get_source(ctx, document_id)
        except Exception:
            raise HTTPException(
                404, f"document {document_id!r} not found",
            )
        _enforce_document_attached_for_action(
            ctx, document_id, "manual action 'run_domain_enrichment'",
        )

        # 3. Active-snapshot gate — the manual action operates on
        # the document's current active snapshot. Without one there
        # is no baseline compile to enrich against.
        active_snap_id = getattr(doc_dto, "active_snapshot_id", None)
        if not active_snap_id:
            raise HTTPException(
                409,
                f"document {document_id!r} has no active snapshot — "
                "run an initial ingest (or reindex) first before "
                "triggering Run Domain Enrichment.",
            )

        # 4. Resolve the producing run of the active snapshot. That
        # is the source of compile artifacts the manual action will
        # reuse. ``_protected_run_id_for_document`` reads the
        # snapshot store; the latest-succeeded heuristic is the
        # fallback for test harnesses that don't wire the snapshot
        # service.
        source_run_id = (
            _protected_run_id_for_document(ctx, document_id)
            or _latest_succeeded_run_id(ctx, document_id)
        )
        if not source_run_id:
            raise HTTPException(
                409,
                f"document {document_id!r} has no successful baseline "
                "ingest run to enrich; run an initial ingest first.",
            )

        # 5. Idempotency / concurrency guard. Two kinds:
        #
        #   (a) Soft check on the runs store — refuse when ANY run
        #       on this document is still in-flight (initial /
        #       reindex / a previous manual action). This catches
        #       the common case + ships a clear error message.
        #
        #   (b) Atomic CAS on ``DocumentRecord.pending_operation``.
        #       Two near-concurrent POSTs that both pass (a) still
        #       race here; the loser sees HTTP 409. The workflow's
        #       ``report_run_terminal`` activity releases the lock
        #       on every terminal status.
        active_states = {
            RunStatus.RUNNING.value, RunStatus.PAUSED.value,
            RunStatus.CANCELLING.value, RunStatus.ASSESSING.value,
            RunStatus.CREATED.value,
        }
        all_runs = store.list_runs(ctx, document_id=document_id)
        in_flight = next(
            (r for r in all_runs if str(r.status) in active_states),
            None,
        )
        if in_flight is not None:
            raise HTTPException(
                409,
                f"another run {in_flight.run_id!r} is currently "
                f"{in_flight.status} on document {document_id!r} — "
                "wait for it to complete before triggering "
                "Run Domain Enrichment.",
            )

        actor = security.subject if security else "system"
        new_run_id = uuid.uuid4().hex

        try:
            lookup_for_lock = _require_source_registry()
            sources_raw = getattr(lookup_for_lock, "_sources", None)
            try_acquire = (
                getattr(sources_raw, "try_acquire_operation_lock", None)
                if sources_raw is not None else None
            )
            if try_acquire is not None:
                acquired = try_acquire(
                    ctx, document_id,
                    operation="run_domain_enrichment",
                    run_id=new_run_id,
                )
                if acquired is None:
                    raise HTTPException(
                        409,
                        f"document {document_id!r} already has a "
                        "pending operation. Wait for it to complete "
                        "before starting Run Domain Enrichment.",
                    )
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 — lock unavailable → no atomic guard
            pass

        # 6. Allocate the candidate snapshot + new run record. The
        # candidate model exists because the snapshot store treats
        # snapshots as immutable: appending enrichment artifacts to
        # the *current* active snapshot would mutate a promoted /
        # CAS-locked record. We allocate a candidate, write the new
        # enrichment artifacts under it, and promote on success.
        now = datetime.now(timezone.utc)
        previous = store.get(ctx, source_run_id)
        previous_meta = dict(previous.metadata or {}) if previous else {}
        previous_version_id = (
            previous.document_version_id if previous else None
        )
        from j1.runs.models import allocate_display_version
        display_version = allocate_display_version(
            started_at=now,
            existing_runs=all_runs,
            document_id=document_id,
        )
        new_run = IngestionRun(
            run_id=new_run_id,
            document_id=document_id,
            workflow_id=None,
            workflow_run_id=None,
            status=RunStatus.CREATED,
            started_at=now,
            updated_at=now,
            metadata={
                "policy": previous_meta.get("policy", "auto"),
                "mode": previous_meta.get("mode", "STANDARD"),
                "document_name": previous_meta.get(
                    "document_name", doc_dto.original_filename,
                ),
                # Drives the compile-reuse short-circuit on the
                # activity layer (see
                # ProcessingActivities._maybe_reuse_compile_artifacts).
                "reused_compile_from_run_id": source_run_id,
                # Manual-action provenance — the run record is the
                # canonical status surface (queued / running /
                # succeeded / failed); these metadata keys let the
                # FE filter the run history for manual-action rows.
                "manual_action": ACTION_RUN_DOMAIN_ENRICHMENT,
                "manual_action_source_snapshot_id": active_snap_id,
                "manual_action_source_run_id": source_run_id,
            },
            run_type="run_domain_enrichment",
            parent_run_id=source_run_id,
            document_version_id=previous_version_id,
            display_version=display_version,
            target_snapshot_id=_allocate_target_snapshot(
                ctx, document_id, new_run_id,
            ),
        )
        store.upsert(ctx, new_run)
        if progress_reporter is not None:
            progress_reporter.report_run_created(
                ctx, run_id=new_run_id, document_id=document_id,
                actor=actor,
            )

        body = IngestRequest(
            compiler_kind=_resolve_compiler_kind(None),
            enricher_kind=_resolve_optional_processor_kind(
                None,
                (processing_capabilities.enricher_kinds
                 if processing_capabilities else frozenset()),
                "enricherKind",
            ),
            graph_builder_kind=_resolve_optional_processor_kind(
                None,
                (processing_capabilities.graph_builder_kinds
                 if processing_capabilities else frozenset()),
                "graphBuilderKind",
            ),
            indexer_kind=_resolve_optional_processor_kind(
                None,
                (processing_capabilities.indexer_kinds
                 if processing_capabilities else frozenset()),
                "indexerKind",
            ),
            actor=actor,
            correlation_id=new_run_id,
            reindex_of=source_run_id,
            target_snapshot_id=new_run.target_snapshot_id,
        )
        try:
            workflow_id = await starter(ctx, document_id, body)
        except Exception as dispatch_exc:
            # Workflow dispatch failed before the activity layer
            # could release the lock at a terminal status. Best-
            # effort release here so the document doesn't stay
            # wedged in ``pending_operation=run_domain_enrichment``.
            # We log — not swallow — release failures so an operator
            # can diagnose a stuck lock from `events.jsonl`.
            try:
                sources_raw = getattr(
                    _require_source_registry(), "_sources", None,
                )
                release = getattr(
                    sources_raw, "release_operation_lock", None,
                ) if sources_raw is not None else None
                if release is not None:
                    release(ctx, document_id, expected_run_id=new_run_id)
            except Exception as release_exc:  # noqa: BLE001 — release is best-effort
                _log.error(
                    "Failed to release pending operation after "
                    "workflow dispatch failure",
                    extra={
                        "tenant_id": getattr(ctx, "tenant_id", None),
                        "project_id": getattr(ctx, "project_id", None),
                        "document_id": document_id,
                        "run_id": new_run_id,
                        "operation_type": "run_domain_enrichment",
                        "action_type": (
                            "manual_action.run_domain_enrichment"
                        ),
                        "dispatch_error": repr(dispatch_exc),
                        "release_error": repr(release_exc),
                    },
                    exc_info=True,
                )
            raise
        new_run.workflow_id = workflow_id
        new_run.updated_at = datetime.now(timezone.utc)
        store.upsert(ctx, new_run)

        return envelope(
            {
                "documentId": document_id,
                "manualAction": ACTION_RUN_DOMAIN_ENRICHMENT,
                "manualActionRunId": new_run_id,
                "runType": "run_domain_enrichment",
                "parentRunId": source_run_id,
                "sourceRunId": source_run_id,
                "sourceSnapshotId": active_snap_id,
                "targetSnapshotId": new_run.target_snapshot_id,
                "workflowId": workflow_id,
                "status": "queued",
            },
            _req_id(request),
        )

    @app.get(
        "/documents/{document_id}/status",
        tags=["documents"],
        summary="Get document processing status",
        dependencies=[Depends(scope_required(SCOPE_READ))],
    )
    def get_document_status(
        request: Request,
        document_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        dto = facade.source_lookup.get_source(ctx, document_id)
        record = DocumentStatusRecord(
            document_id=dto.document_id, status=dto.status
        )
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    # ---- Ingestion jobs ---------------------------------------------

    @app.get(
        "/ingestion-jobs/{job_id}",
        tags=["ingestion-jobs"],
        summary="Get ingestion job status",
        dependencies=[Depends(scope_required(SCOPE_READ))],
    )
    async def get_job(
        request: Request,
        job_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        job_status_port=Depends(require_job_status),
    ) -> dict[str, Any]:
        dto = await job_status_port.get_job_status(ctx, job_id)
        record = JobStatusRecord(
            job_id=dto.job_id,
            state=dto.state,
            current_operation=dto.current_operation,
            documents_total=dto.documents_total,
            documents_completed=dto.documents_completed,
            review_required=dto.review_required,
            budget_approval_required=dto.budget_approval_required,
            error=dto.error,
        )
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    @app.get(
        "/ingestion-jobs/{job_id}/events",
        tags=["ingestion-jobs"],
        summary="Get audit events for an ingestion job",
        description=(
            "Returns audit events whose `correlation_id` matches the job ID. "
            "Requires `workspace` to be configured at adapter construction."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_job_events(
        request: Request,
        job_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        if workspace is None:
            raise HTTPException(503, "audit-event lookup not configured")
        events = _read_job_events(workspace, ctx, job_id)
        record = JobEventsRecord(job_id=job_id, events=events)
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    # ---- Ingestion-run progress surface (frontend-facing) -----------
    # These endpoints sit alongside `/ingestion-jobs/*` rather than
    # replace them. `/ingestion-jobs/*` exposes the technical Temporal
    # surface (workflow IDs, signals, raw audit events). The new
    # `/ingestion-runs/*` surface is the user-facing view: status,
    # execution plan, structured progress events, SSE stream.

    def _require_run_store() -> IngestionRunStore:
        if ingestion_run_store is None:
            raise HTTPException(
                503,
                "ingestion-run store not configured "
                "(pass `ingestion_run_store=` to create_rest_api)",
            )
        return ingestion_run_store

    def _emit_ops_event(
        ctx: ProjectContext,
        *,
        action: str,
        target_id: str,
        actor: str,
        payload: dict[str, Any],
        target_kind: str = TARGET_KIND_INGESTION_RUN,
        correlation_id: str | None = None,
    ) -> None:
        """Best-effort `j1.ops.*` audit-event emission for the
 operator-initiated ingestion ops (delete / purge / resume /
 rebuild / reindex / batch). Routes through the existing
 `EventPublisherService` so the event lands in the same
 `events.jsonl` audit log every other action writes to.

 Failures here MUST NOT block the operator's request — the
 underlying mutation already succeeded and rolling it back
 because we couldn't write a log line would be far worse.
 Swallow + carry on; an audit gap is recoverable, a half-
 applied mutation is not."""
        try:
            facade.event_publisher.publish_event(
                ctx,
                EventDTO(
                    actor=actor,
                    action=action,
                    target_kind=target_kind,
                    target_id=target_id,
                    payload=dict(payload),
                    correlation_id=correlation_id,
                ),
            )
        except Exception:  # noqa: BLE001 — telemetry never blocks ops
            pass

    @app.get(
        "/ingestion-runs",
        tags=["ingestion-runs"],
        summary="List ingestion runs in the current tenant/project",
        description=(
            "Paginated list of ingestion runs for the current tenant/"
            "project, ordered by `startedAt` descending. Optional "
            "`status` query repeats narrow to specific run states "
            "(e.g. `?status=running&status=plan_ready`). The `q` "
            "parameter does a case-insensitive contains match against "
            "`runId` and the `documentName` metadata field. Counts in "
            "`total` reflect the filtered set BEFORE pagination."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def list_ingestion_runs(
        request: Request,
        ctx: ProjectContext = Depends(get_ctx),
        page: int = Query(default=1, ge=1),
        # Aliased to match the camelCase wire convention used by the
        # rest of the API. FastAPI does not auto-camelize query
        # params; without the explicit `alias=` the FE would have to
        # send `?page_size=` which clashes with the JSON body
        # convention.
        page_size: int = Query(default=20, ge=1, le=200, alias="pageSize"),
        status: list[str] | None = Query(default=None),
        q: str | None = None,
    ) -> dict[str, Any]:
        store = _require_run_store()
        # Page bounds are enforced by FastAPI via `Query(ge=, le=)`
        # above; we only get here with sane values.

        # Translate the optional `?status=` repeats into RunStatus
        # values. Unknown strings are dropped silently — the FE drives
        # the list of valid statuses, and a typo from a hand-rolled
        # cURL shouldn't 400 the whole call.
        #
        # Canonical/legacy expansion: a `?status=received` query also
        # matches runs persisted with the legacy `created` value, and
        # `?status=assessment_ready` also matches `plan_ready`. See
        # `status_aliases` for the table. This lets the FE migrate
        # to the canonical names without breaking existing JSONL data.
        status_filter: list[RunStatus] | None = None
        if status:
            parsed: list[RunStatus] = []
            seen: set[str] = set()
            for raw in status:
                for alias in status_aliases(raw):
                    if alias in seen:
                        continue
                    try:
                        parsed.append(RunStatus(alias))
                        seen.add(alias)
                    except ValueError:
                        continue
            status_filter = parsed or None

        # The store's `list` already deduplicates by run_id and sorts
        # by `started_at desc`. We pull the full filtered set, then
        # apply the `q` substring + page slicing here. JSONL scans
        # stay cheap up to thousands of runs; switch to a SQL store
        # before paginating across millions.
        runs = store.list(ctx, statuses=status_filter)
        if q:
            needle = q.strip().lower()
            if needle:
                runs = [
                    r
                    for r in runs
                    if needle in r.run_id.lower()
                    or needle in str(r.metadata.get("document_name", "")).lower()
                ]
        total = len(runs)
        start = (page - 1) * page_size
        items = runs[start : start + page_size]
        record = IngestionRunListRecord(
            items=[_ingestion_run_to_list_item(r) for r in items],
            page=page,
            page_size=page_size,
            total=total,
        )
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    @app.post(
        "/ingestion-runs/{run_id}/full-reindex",
        tags=["ingestion-runs"],
        summary="(GONE) Run-level re-index is no longer supported",
        description=(
            "Removed: a run is an immutable execution record. To "
            "re-process a document, call "
            "`POST /documents/{document_id}/reindex` — this allocates "
            "a brand-new run + snapshot and starts the pipeline from "
            "the original uploaded file."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def full_reindex_ingestion_run(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        raise HTTPException(
            status_code=410,
            detail=(
                "Run-level re-index is no longer supported. Start a "
                "new document re-index instead: "
                "POST /documents/{document_id}/reindex."
            ),
        )

    @app.post(
        "/ingestion-runs/{run_id}/resume-from-checkpoint",
        tags=["ingestion-runs"],
        summary="(GONE) Run-level resume is no longer supported",
        description=(
            "Removed: a run is an immutable execution record. To "
            "re-process a document — even when a previous run failed "
            "partway through — call `POST /documents/{document_id}/"
            "reindex`. It allocates a brand-new run + snapshot and "
            "starts the pipeline from the original uploaded file. "
            "Reusing prior compile/enrich/graph outputs is no longer "
            "supported on the user-facing surface."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def resume_ingestion_run_from_checkpoint(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        raise HTTPException(
            status_code=410,
            detail=(
                "Run-level resume/re-index is no longer supported. "
                "Start a new document re-index instead: "
                "POST /documents/{document_id}/reindex."
            ),
        )

    @app.post(
        "/ingestion-runs/{run_id}/rebuild-index",
        tags=["ingestion-runs"],
        summary="(GONE) Run-level index rebuild is no longer supported",
        description=(
            "Removed: re-using prior-run chunks as the source for a "
            "new index goes against the rule that every active "
            "result must trace back to a single immutable run. Use "
            "`POST /documents/{document_id}/reindex` — it re-runs the "
            "whole pipeline (parse → compile → enrich → graph → "
            "index) from the original uploaded file."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def rebuild_ingestion_run_index(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        raise HTTPException(
            status_code=410,
            detail=(
                "Run-level index rebuild is no longer supported. "
                "Start a new document re-index instead: "
                "POST /documents/{document_id}/reindex."
            ),
        )

    def _execute_run_cleanup(
        ctx: ProjectContext, run_id: str, *, actor: str,
    ) -> tuple["CleanUpEligibilityDTO", dict[str, Any] | None, dict[str, int] | None]:
        """Run the eligibility check; if allowed, perform the
        hard-cleanup and return ``(eligibility, report,
        validation_cascade)``. On refusal returns
        ``(eligibility, None, None)`` so the caller can shape the
        response (200+structured-reason vs HTTP 409 for legacy
        DELETE).

        The intent of "Clean Up Run": permanently remove every
        record this run owns — registry artifacts + files on disk,
        run-store snapshot, validation history — so the run no
        longer pollutes storage, UI, query scope, or audit history.
        See the cleanup helper docstring for the full guarantee set.
        """
        from j1.ingestion_review.exceptions import (
            ReviewNotFound, RunStillActive,
        )
        eligibility = _check_cleanup_eligibility(ctx, run_id)
        if not eligibility.allowed:
            return eligibility, None, None
        service = _require_review_service()
        try:
            report = service.delete_run(ctx, run_id, actor=actor)
        except ReviewNotFound:
            # Race with concurrent cleanup: the eligibility check
            # said the run was present, but it's gone by the time
            # we tried to remove it. Surface as RUN_NOT_FOUND.
            from j1.ingestion_review.dtos import (
                CleanUpEligibilityDTO,
                CLEANUP_REASON_RUN_NOT_FOUND,
            )
            return (
                CleanUpEligibilityDTO(
                    run_id=run_id,
                    allowed=False,
                    reason=CLEANUP_REASON_RUN_NOT_FOUND,
                    message=f"Ingestion run {run_id!r} not found.",
                ),
                None, None,
            )
        except RunStillActive:
            # Same race shape — eligibility said terminal, but the
            # service raised. Surface as PROCESSING_RUN.
            from j1.ingestion_review.dtos import (
                CleanUpEligibilityDTO,
                CLEANUP_REASON_PROCESSING_RUN,
            )
            return (
                CleanUpEligibilityDTO(
                    run_id=run_id,
                    allowed=False,
                    reason=CLEANUP_REASON_PROCESSING_RUN,
                    message=(
                        "This run transitioned to processing while "
                        "cleanup was being prepared. Try again."
                    ),
                ),
                None, None,
            )
        validation_cascade = {"sets_removed": 0, "runs_removed": 0}
        if validation_service is not None:
            try:
                validation_cascade = validation_service.purge_for_run(
                    ctx, run_id,
                )
            except Exception:  # noqa: BLE001 — best-effort
                pass
        _emit_ops_event(
            ctx,
            action=ACTION_OPS_RUN_DELETED,
            target_id=run_id,
            actor=actor,
            correlation_id=run_id,
            payload={
                "artifacts_deleted": report["artifacts_deleted"],
                "files_deleted": report["files_deleted"],
                "files_missing": report["files_missing"],
                "snapshots_removed": report["snapshots_removed"],
                "validation_sets_removed": validation_cascade["sets_removed"],
                "validation_runs_removed": validation_cascade["runs_removed"],
                "deleted_at": report["deleted_at"],
                "action_label": "clean_up_run",
            },
        )
        return eligibility, report, validation_cascade

    @app.get(
        "/ingestion-runs/{run_id}/cleanup-eligibility",
        tags=["ingestion-runs"],
        summary="Whether this run can be cleaned up",
        description=(
            "Snapshot-centric pre-flight check for the Clean Up "
            "Run action. Returns a structured eligibility result so "
            "the FE can render the button + tooltip + confirmation "
            "modal in the right state without trying the action. "
            "The same check is consulted server-side by "
            "POST /ingestion-runs/{id}/clean-up — UI and API can't "
            "drift on the rules."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_run_cleanup_eligibility(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        eligibility = _check_cleanup_eligibility(ctx, run_id)
        return envelope(
            eligibility.model_dump(by_alias=True), _req_id(request),
        )

    @app.post(
        "/ingestion-runs/{run_id}/clean-up",
        tags=["ingestion-runs"],
        summary="Clean up all data produced by a non-active run",
        description=(
            "Renamed + structured replacement for the legacy "
            "`DELETE /ingestion-runs/{id}`. Hard-removes every "
            "record the run owns — registry artifacts + their files "
            "on disk, every JSONL snapshot of the run, validation "
            "history that references this run_id. The audit log is "
            "preserved for compliance.\n\n"
            "Always returns HTTP 200. The body's `cleaned` flag "
            "carries the outcome:\n"
            "  * `cleaned=true` → run is gone; `deletedCounts` "
            "tallies what was removed.\n"
            "  * `cleaned=false` → cleanup was refused; `reason` "
            "is one of `PROCESSING_RUN`, `ACTIVE_RUN`, `ONLY_RUN`, "
            "`RUN_NOT_FOUND`. The same `reason` is returned by "
            "GET /cleanup-eligibility."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    def post_run_clean_up(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        from j1.ingestion_review.dtos import (
            CleanUpDeletedCountsDTO, CleanUpRunResultDTO,
            CLEANUP_REASON_OK,
        )
        actor = security.subject if security else "system"
        eligibility, report, validation_cascade = _execute_run_cleanup(
            ctx, run_id, actor=actor,
        )
        if report is None:
            result = CleanUpRunResultDTO(
                run_id=run_id,
                cleaned=False,
                reason=eligibility.reason,
                message=eligibility.message,
                deleted_counts=CleanUpDeletedCountsDTO(),
                deleted_at=None,
            )
            return envelope(
                result.model_dump(by_alias=True), _req_id(request),
            )
        validation_cascade = validation_cascade or {
            "sets_removed": 0, "runs_removed": 0,
        }
        # Map the internal report → structured deleted_counts. Each
        # field is a "kind" of run-owned data; counts are best-effort
        # tallies (a partial-failure cleanup still emits what it
        # succeeded on).
        #
        # PLACEHOLDER: ``chunks`` and ``enrichments`` are intentionally
        # returned as 0 today. The artifact registry hard-deletes them
        # alongside every other artifact this run owns (they roll up
        # into ``artifacts``), but the registry doesn't expose a
        # reliable per-kind tally on the delete path yet. The fields
        # are in the wire shape so future per-kind tallies land here
        # without breaking callers. See
        # ``j1.artifacts.registry.JsonArtifactRegistry.delete_by_artifact_id``
        # — adding a per-kind breakdown requires the registry to
        # surface the kind of each deleted record back to the caller.
        deleted = CleanUpDeletedCountsDTO(
            artifacts=int(report.get("artifacts_deleted", 0) or 0),
            workspace_files=int(report.get("files_deleted", 0) or 0),
            snapshots=int(report.get("snapshots_removed", 0) or 0),
            validation_results=int(
                (validation_cascade.get("runs_removed", 0) or 0)
                + (validation_cascade.get("sets_removed", 0) or 0)
            ),
            # chunks / enrichments left at 0 — see placeholder note above.
        )
        result = CleanUpRunResultDTO(
            run_id=report["run_id"],
            cleaned=True,
            reason=CLEANUP_REASON_OK,
            message="Run cleaned up successfully.",
            deleted_counts=deleted,
            deleted_at=report.get("deleted_at"),
        )
        return envelope(
            result.model_dump(by_alias=True), _req_id(request),
        )

    @app.get(
        "/ingestion-runs/{run_id}",
        tags=["ingestion-runs"],
        summary="Get ingestion-run status",
        description=(
            "Returns the latest snapshot of an ingestion run: status, "
            "current stage / step, progress percent, warning count, "
            "and any failure details. Frontend polls this for "
            "non-streaming status updates; for live progress use "
            "`GET /ingestion-runs/{id}/events/stream`."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_ingestion_run(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        store = _require_run_store()
        run = store.get(ctx, run_id)
        if run is None:
            raise HTTPException(404, f"ingestion run {run_id!r} not found")
        # Worker activities don't update the run-store record today
        # (the run-store is API-side state; the worker emits audit
        # progress events). Derive `currentStage` / `currentStep` /
        # `lastEventType` from the most recent progress event so the
        # detail endpoint reflects the in-flight step without
        # requiring the FE to subscribe to SSE.
        latest_progress = (
            _latest_progress_snapshot(workspace, ctx, run_id)
            if workspace is not None else None
        )
        return envelope(
            _ingestion_run_to_record(
                run, latest_progress=latest_progress,
            ).model_dump(by_alias=True),
            _req_id(request),
        )

    @app.get(
        "/ingestion-runs/{run_id}/plan",
        tags=["ingestion-runs"],
        summary="Get the execution plan for an ingestion run",
        description=(
            "Returns the `IngestPlan` generated by the planner for the "
            "run. Includes per-step decisions (RUN / SKIP / "
            "CONDITIONAL), reasons, dependencies, cost tier, expected "
            "engine, and risk level. Empty plan when the planner is "
            "disabled for this run."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_ingestion_run_plan(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        if workspace is None:
            raise HTTPException(503, "audit-event lookup not configured")
        plan = _read_run_plan(workspace, ctx, run_id)
        if plan is None:
            raise HTTPException(404, f"no plan recorded for run {run_id!r}")
        return envelope(plan.model_dump(by_alias=True), _req_id(request))

    # ---- Ingestion Result Review --------------------------------------
    # Read-only review surface for completed runs. Powered by
    # `IngestionResultReviewService` (j1.ingestion_review). Endpoints
    # here MUST stay thin — no projection logic in the route handler;
    # everything funnels through the service so tenant/project/run
    # ownership is enforced uniformly.

    def _require_review_service() -> IngestionResultReviewService:
        if review_service is None:
            raise HTTPException(
                503,
                "ingestion-review service not configured "
                "(pass `review_service=` to create_rest_api)",
            )
        return review_service

    def _enforce_document_attached_for_action(
        ctx: ProjectContext, document_id: str, action: str,
    ) -> None:
        """Reject `action` (reindex / resume / etc.) when the
 document isn't currently attached to the knowledge base.

 Reads `knowledge_state` off the source-lookup DTO. When the
 facade can't return the document we don't raise here — the
 calling endpoint has its own 404 path right after the lookup
 — so a missing document doesn't double-fail at this layer.
 """
        try:
            dto = facade.source_lookup.get_source(ctx, document_id)
        except Exception:
            return
        state = getattr(dto, "knowledge_state", "attached") or "attached"
        if state == "attached":
            return
        if state == "removed":
            raise HTTPException(
                409,
                f"document {document_id!r} has been removed from the "
                f"knowledge base; re-upload to enable {action}",
            )
        # "detached" or any future not-attached state.
        raise HTTPException(
            409,
            f"document {document_id!r} is {state}; attach it before "
            f"calling {action}",
        )

    def _require_document_lifecycle_service() -> DocumentLifecycleService:
        """503 when the deployment didn't wire the lifecycle service.
 Same degradation pattern as `_require_review_service` — the
 endpoint exists in the API surface but returns 503 until a
 wiring layer constructs the service."""
        if document_lifecycle_service is None:
            raise HTTPException(
                503,
                "document-lifecycle service not configured "
                "(pass `document_lifecycle_service=` to create_rest_api)",
            )
        return document_lifecycle_service

    def _doc_record_to_payload(record) -> dict[str, Any]:
        """Project a `DocumentRecord` into a wire-friendly dict.

 Used by all three lifecycle endpoints so the FE gets the same
 shape regardless of which action it called. Phase 6 will
 supersede this with a proper Pydantic schema + a richer
 projector; this minimal shape is enough for the FE to refresh
 its document-state cache after a mutating call.
 """
        return {
            "documentId": record.document_id,
            "knowledgeState": record.knowledge_state,
            "activeSnapshotId": record.active_snapshot_id,
            "latestVersionId": record.latest_version_id,
            "removedAt": (
                record.removed_at.isoformat() if record.removed_at else None
            ),
            "updatedAt": (
                record.updated_at.isoformat() if record.updated_at else None
            ),
        }

    # ---- Document lifecycle (attach / detach / remove) ----------------
    #
    # These are the document-centric replacements for the old
    # run-level `DELETE /ingestion-runs/{id}` and purge actions. They
    # operate on the document — the user's mental model — rather than
    # on a specific run attempt. The retrieval gate at
    # `j1.documents.lifecycle.filter_to_attached_artifacts` activates
    # immediately on success; downstream retrieval/validation/answer
    # paths see the change without any cache busting.

    @app.post(
        "/documents/{document_id}/attach",
        tags=["documents"],
        summary="Attach a document to the active knowledge base",
        description=(
            "Restore the document so retrieval / search / validation "
            "/ answer generation can use it again. Idempotent — calling "
            "on an already-attached document returns the same record "
            "without emitting a duplicate audit event. Rejected with "
            "HTTP 409 when the document has previously been `removed` "
            "— removed documents must be re-uploaded to come back."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    def post_document_attach(
        request: Request,
        document_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        svc = _require_document_lifecycle_service()
        try:
            updated = svc.attach(ctx, document_id, actor=security.subject)
        except DocumentNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except DocumentLifecycleError as exc:
            # 409 Conflict — the document exists but is in a state
            # that disallows this transition (e.g. removed). Distinct
            # from 404 (doesn't exist) so the FE can render different
            # operator-friendly copy.
            raise HTTPException(409, str(exc))
        return envelope(_doc_record_to_payload(updated), _req_id(request))

    @app.post(
        "/documents/{document_id}/detach",
        tags=["documents"],
        summary="Detach a document from the active knowledge base",
        description=(
            "Stop using the document for retrieval / search / "
            "validation / answer generation. The document, its run "
            "history, and its artifacts are preserved on disk so the "
            "user can re-attach later. Idempotent. Rejected with 409 "
            "when the document was previously removed."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    def post_document_detach(
        request: Request,
        document_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        svc = _require_document_lifecycle_service()
        try:
            updated = svc.detach(ctx, document_id, actor=security.subject)
        except DocumentNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except DocumentLifecycleError as exc:
            raise HTTPException(409, str(exc))
        return envelope(_doc_record_to_payload(updated), _req_id(request))

    @app.post(
        "/documents/{document_id}/remove",
        tags=["documents"],
        summary="Remove a document's generated knowledge from the knowledge base",
        description=(
            "Disown the document from the active knowledge layer. "
            "Clears `active_snapshot_id`, sets `removed_at`, and "
            "stamps every artifact tied to the document as `removed` so "
            "retrieval / search / validation / answer generation can "
            "no longer surface it. Idempotent — calling on an "
            "already-removed document is a no-op. Run history remains "
            "on disk as a minimal tombstone for audit; this is NOT a "
            "hard purge of files. Re-attaching a removed document "
            "requires a fresh upload."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    def post_document_remove(
        request: Request,
        document_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        svc = _require_document_lifecycle_service()
        try:
            updated = svc.remove(ctx, document_id, actor=security.subject)
        except DocumentNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except DocumentLifecycleError as exc:
            raise HTTPException(409, str(exc))
        return envelope(_doc_record_to_payload(updated), _req_id(request))

    @app.post(
        "/documents/{document_id}/repair",
        tags=["documents"],
        summary="Invalidate orphan artifacts (no run_id) for a document",
        description=(
            "Admin / debug helper. Sweeps the artifact registry for "
            "artifacts tied to this document that have no `run_id` "
            "stamped (orphans from before the lineage fail-fast guard "
            "landed) and marks them `search_state=invalid` so "
            "retrieval and validation stop surfacing them. Artifacts "
            "stay on disk for audit. The reindex flow also runs this "
            "sweep automatically; this endpoint exposes it on-demand "
            "for corpora repair without dispatching a new run."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    def post_document_repair(
        request: Request,
        document_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        # No 404 on missing document — the sweep is purely an
        # artifact-side operation and can run even if the document
        # record itself is malformed. Returns 0 invalidated for
        # the no-op case, which is the right contract for an idempotent
        # repair tool.
        from j1.documents.artifact_state import (
            invalidate_lineage_missing_artifacts,
            invalidate_orphan_artifacts,
        )
        registry = getattr(facade.retrieval, "_artifacts", None)
        if registry is None:
            raise HTTPException(
                503, "artifact registry not configured for repair",
            )
        # Document-scoped sweep: any artifact tied to this document
        # via ``source_document_ids`` with no ``run_id``.
        invalidated_doc = invalidate_orphan_artifacts(
            ctx=ctx,
            artifacts=registry,
            document_id=document_id,
        )
        # Project-wide sweep: graph_json artifacts produced by
        # LightRAG are workspace-aggregate and frequently have
        # empty ``source_document_ids``, so the document-scoped
        # sweep above misses them. This second pass catches
        # lineage-required kinds project-wide. Runs after the
        # per-doc sweep so the per-doc count stays meaningful in
        # the response.
        invalidated_lineage = invalidate_lineage_missing_artifacts(
            ctx=ctx,
            artifacts=registry,
        )
        return envelope(
            {
                "documentId": document_id,
                "invalidatedArtifactCount": invalidated_doc + invalidated_lineage,
                "invalidatedDocumentScoped": invalidated_doc,
                "invalidatedLineageOrphans": invalidated_lineage,
            },
            _req_id(request),
        )

    # ---- Document-centric re-index (Phase 4) -----------------------
    #
    # Document-centric replacement for `POST /ingestion-runs/{id}/
    # full-reindex`. The user manages documents, not runs — this
    # endpoint takes a document_id and dispatches a fresh
    # `run_type="reindex"` attempt under it. The previous active
    # run is preserved unchanged; the new run only becomes "active"
    # if it reaches a usable terminal state (see the promotion
    # hook in `RunsActivities._maybe_promote_to_active`). A failed
    # reindex therefore does NOT clobber the previous successful
    # active — same data on disk, just no FE flip.

    @app.post(
        "/documents/{document_id}/reindex",
        tags=["documents"],
        summary="Re-index a document (create a new ingestion run)",
        description=(
            "Start a fresh ingestion attempt for this document. The "
            "new run carries `runType=reindex` and "
            "`parentRunId=<document.activeRunId>` so the run-history "
            "UI can render the relationship. The document's "
            "`activeRunId` does NOT flip immediately — it only "
            "updates if the new run reaches a usable terminal state "
            "(succeeded / succeeded-with-warnings), guaranteeing "
            "that a failed reindex preserves the previous good "
            "result for retrieval. Returns 409 when the document is "
            "detached or removed (attach it first via "
            "`POST /documents/{id}/attach`).\n\n"
            "Optional JSON body accepts `selectedProfile` "
            "(`minimum_queryable` / `standard` / `advanced`) so the "
            "FE's AssessmentPlanDialog can drive re-index dispatch "
            "the same way it drives the upload-and-start path. Omit "
            "the body to fall back to the deployment policy's "
            "default profile."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def post_document_reindex(
        request: Request,
        document_id: str,
        body: DocumentReindexRequest | None = Body(default=None),
        ctx: ProjectContext = Depends(get_ctx),
        starter: JobStarter = Depends(require_job_starter),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        from datetime import datetime, timezone
        from j1.ingestion_review.exceptions import RunStillActive
        store = _require_run_store()
        # Resolve the execution profile against the deployment policy
        # BEFORE touching the run store so a typo / disallowed profile
        # fails fast with a structured 400 / 403 — same fail-fast
        # contract as `POST /ingestion-runs`. Omitted body or omitted
        # field → policy default applies.
        provided_profile = body.selected_profile if body is not None else None
        resolved_profile, profile_source = _resolve_profile_or_400(
            provided_profile,
        )
        # State guard: refuse on detached / removed documents per
        # the spec's section-8 action matrix. The lifecycle service
        # would also refuse, but rejecting at the API edge gives
        # the FE a faster + more actionable error path.
        _enforce_document_attached_for_action(ctx, document_id, "reindex")

        # Pre-reindex repair: invalidate any orphan artifacts tied
        # to this document that lack a `run_id` stamp (leftovers
        # from before the lineage fail-fast guard landed). Without
        # this sweep, retrieval after the new reindex would still
        # surface the broken artifacts alongside the new run's
        # output — exactly the failure mode the latest validation
        # reports flag. Best-effort: a failure here logs but doesn't
        # block the reindex dispatch.
        try:
            from j1.documents.artifact_state import invalidate_orphan_artifacts
            invalidated = invalidate_orphan_artifacts(
                ctx=ctx,
                artifacts=facade.retrieval._artifacts,  # noqa — direct registry access
                document_id=document_id,
            )
            if invalidated:
                import logging as _logging
                _logging.getLogger("j1.adapters.rest").info(
                    "reindex pre-sweep invalidated %d orphan artifact(s) "
                    "for document %s",
                    invalidated, document_id,
                )
        except Exception:  # noqa: BLE001
            pass

        # Resolve the document so we can derive the workflow name +
        # the parent-run pointer + the version id to inherit.
        try:
            doc_dto = facade.source_lookup.get_source(ctx, document_id)
        except Exception:
            raise HTTPException(
                404, f"document {document_id!r} not found",
            )

        # Re-index ALWAYS re-parses the original uploaded file. If it
        # is missing on disk we fail clearly: silently falling back to
        # cached parse / compile / enrichment from a prior run would
        # produce a "successful" run that doesn't actually re-process
        # the document. Skipped only when workspace isn't wired (test
        # harnesses that stub the upload path).
        if workspace is not None and doc_dto.stored_filename:
            source_path = workspace.raw(ctx) / doc_dto.stored_filename
            if not source_path.exists():
                raise HTTPException(
                    409,
                    (
                        f"original uploaded file for document "
                        f"{document_id!r} is missing on disk "
                        f"({doc_dto.stored_filename}); cannot "
                        "re-index. Re-upload the document or contact "
                        "an operator to restore the file."
                    ),
                )

        # Phase 9: ``active_run_id`` was deleted from the document
        # contract. The in-flight guard inspects every run on this
        # document (not just the historically-active one) so a
        # mid-flight reindex can't kick off a second concurrent
        # attempt. Settings inheritance pulls from the latest
        # succeeded run.
        #
        # The list-and-check below is the user-friendly soft guard
        # (better error message when an obvious in-flight run
        # exists). The *atomic* guard is
        # ``try_acquire_operation_lock`` below — two near-concurrent
        # reindex POSTs that both pass the soft check still race on
        # the lock CAS, and the loser gets HTTP 409.
        active_states = {
            RunStatus.RUNNING.value, RunStatus.PAUSED.value,
            RunStatus.CANCELLING.value, RunStatus.ASSESSING.value,
        }
        all_runs = store.list_runs(ctx, document_id=document_id)
        in_flight = next(
            (r for r in all_runs if str(r.status) in active_states),
            None,
        )
        if in_flight is not None:
            raise HTTPException(
                409,
                f"previous run {in_flight.run_id!r} is currently "
                f"{in_flight.status} — cancel it before "
                "starting a reindex",
            )
        active_run_id = _latest_succeeded_run_id(ctx, document_id)

        # Inherit settings from the latest succeeded run when one
        # exists. New documents (no prior run) get the deployment
        # defaults — same fallback chain as the upload flow.
        previous_meta: dict[str, Any] = {}
        previous_version_id: str | None = None
        if active_run_id:
            previous = store.get(ctx, active_run_id)
            if previous is not None:
                previous_meta = dict(previous.metadata or {})
                previous_version_id = previous.document_version_id

        actor = security.subject if security else "system"
        new_run_id = uuid.uuid4().hex

        # Atomic reindex lock (audit fix). Two concurrent reindex
        # POSTs that both pass the soft in-flight check above still
        # race here — the CAS on ``DocumentRecord.pending_operation``
        # picks exactly one winner. The loser sees HTTP 409 instead
        # of silently dispatching a parallel run. The workflow's
        # ``report_run_terminal`` activity releases the lock at every
        # terminal status; see
        # ``RunsActivities._release_operation_lock_for_run``.
        try:
            lookup_for_lock = _require_source_registry()
            sources_raw = getattr(lookup_for_lock, "_sources", None)
            try_acquire = (
                getattr(sources_raw, "try_acquire_operation_lock", None)
                if sources_raw is not None else None
            )
            if try_acquire is not None:
                acquired = try_acquire(
                    ctx, document_id,
                    operation="reindex", run_id=new_run_id,
                )
                if acquired is None:
                    raise HTTPException(
                        409,
                        f"document {document_id!r} already has a "
                        "pending operation (reindex / refresh-enrich "
                        "/ attach / detach / remove). Wait for it to "
                        "complete before starting a new reindex.",
                    )
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 — lock unavailable → no atomic guard
            # Best-effort: legacy / test wirings without the lock
            # helper fall back to the soft in-flight check above.
            pass

        now = datetime.now(timezone.utc)
        from j1.runs.models import allocate_display_version
        prior_runs = store.list_runs(ctx, document_id=document_id)
        display_version = allocate_display_version(
            started_at=now,
            existing_runs=prior_runs,
            document_id=document_id,
        )
        new_run = IngestionRun(
            run_id=new_run_id,
            document_id=document_id,
            workflow_id=None,
            workflow_run_id=None,
            status=RunStatus.CREATED,
            started_at=now,
            updated_at=now,
            metadata={
                # Keep the `reindex_of` key for backward-compat with
                # any FE that learned to read it from the legacy
                # endpoint. The structured `parent_run_id` field
                # below is the canonical one.
                "reindex_of": active_run_id or "",
                "policy": previous_meta.get("policy", "auto"),
                "mode": previous_meta.get("mode", "STANDARD"),
                "document_name": previous_meta.get(
                    "document_name", doc_dto.original_filename,
                ),
                "selected_execution_profile": resolved_profile.value,
                "profile_selected_by": (
                    "user" if profile_source == "rest" else "system"
                ),
                "profile_selection_source": profile_source,
            },
            run_type="reindex",
            parent_run_id=active_run_id,
            document_version_id=previous_version_id,
            display_version=display_version,
            # Phase 5: up-front snapshot allocation. See full-reindex.
            target_snapshot_id=_allocate_target_snapshot(
                ctx, document_id, new_run_id,
            ),
        )
        store.upsert(ctx, new_run)

        # Dedicated ingest-trace surface (no-op when disabled). Emitted
        # at the REST edge so operators can correlate the moment of
        # run creation with the workflow-start latency below.
        from j1.observability.ingest_trace import (
            TraceContext as _TraceCtx,
            trace_event as _trace_event,
        )
        _ingest_trace_ctx = _TraceCtx(
            tenant_id=getattr(ctx, "tenant_id", None),
            project_id=getattr(ctx, "project_id", None),
            document_id=document_id,
            run_id=new_run_id,
            target_snapshot_id=new_run.target_snapshot_id,
        )
        _trace_event(
            trace_event="ingest.run.created",
            stage="run",
            status="started",
            context=_ingest_trace_ctx,
            metadata={
                "run_type": "reindex",
                "parent_run_id": active_run_id,
                "fresh_run": True,
                "reused_existing_compile": False,
                "reused_existing_chunks": False,
                "reused_existing_enrichment": False,
            },
        )
        if new_run.target_snapshot_id:
            _trace_event(
                trace_event="ingest.snapshot.allocated",
                stage="snapshot",
                status="completed",
                context=_ingest_trace_ctx,
                metadata={"snapshot_id": new_run.target_snapshot_id},
            )

        if progress_reporter is not None:
            progress_reporter.report_run_created(
                ctx, run_id=new_run_id, document_id=document_id,
                actor=actor,
            )

        body = IngestRequest(
            compiler_kind=_resolve_compiler_kind(None),
            enricher_kind=_resolve_optional_processor_kind(
                None,
                (processing_capabilities.enricher_kinds
                 if processing_capabilities else frozenset()),
                "enricherKind",
            ),
            graph_builder_kind=_resolve_optional_processor_kind(
                None,
                (processing_capabilities.graph_builder_kinds
                 if processing_capabilities else frozenset()),
                "graphBuilderKind",
            ),
            indexer_kind=_resolve_optional_processor_kind(
                None,
                (processing_capabilities.indexer_kinds
                 if processing_capabilities else frozenset()),
                "indexerKind",
            ),
            actor=actor,
            correlation_id=new_run_id,
            reindex_of=active_run_id or None,
            target_snapshot_id=new_run.target_snapshot_id,
            selected_profile=resolved_profile.value,
        )
        try:
            workflow_id = await starter(ctx, document_id, body)
        except Exception:
            # Workflow dispatch failed before the activity layer
            # could release the lock on a terminal status. Release
            # here so the document doesn't stay wedged in
            # ``pending_operation=reindex`` forever.
            try:
                sources_raw = getattr(
                    _require_source_registry(), "_sources", None,
                )
                release = getattr(
                    sources_raw, "release_operation_lock", None,
                ) if sources_raw is not None else None
                if release is not None:
                    release(ctx, document_id, expected_run_id=new_run_id)
            except Exception:  # noqa: BLE001 — release is best-effort
                pass
            raise
        new_run.workflow_id = workflow_id
        new_run.updated_at = datetime.now(timezone.utc)
        store.upsert(ctx, new_run)
        _trace_event(
            trace_event="ingest.workflow.started",
            stage="workflow",
            status="started",
            context=_TraceCtx(
                tenant_id=getattr(ctx, "tenant_id", None),
                project_id=getattr(ctx, "project_id", None),
                document_id=document_id,
                run_id=new_run_id,
                target_snapshot_id=new_run.target_snapshot_id,
                workflow_id=workflow_id,
            ),
            metadata={"run_type": "reindex"},
        )
        _emit_ops_event(
            ctx,
            action=ACTION_OPS_RUN_REINDEXED,
            target_id=new_run_id,
            actor=actor,
            correlation_id=new_run_id,
            payload={
                "document_id": document_id,
                "parent_run_id": active_run_id,
                "workflow_id": workflow_id,
                "run_type": "reindex",
            },
        )
        return envelope(
            {
                "documentId": document_id,
                "reindexRunId": new_run_id,
                "parentRunId": active_run_id,
                "workflowId": workflow_id,
                "runType": "reindex",
            },
            _req_id(request),
        )

    # ---- Document-centric refresh-enrich (Phase 5b) -----------------
    #
    # ``POST /documents/{id}/refresh-enrich`` kicks a candidate run
    # that REUSES the previous active run's compile output and re-
    # runs only enrichment + graph + index. The compile stage is by
    # far the most expensive (MinerU OCR + LLM passes); skipping it
    # lets operators iterate on enricher / graph builder choices
    # without paying for the parse again.
    #
    # Wire-on-disk contract:
    #   * ``run_type="refresh_enrich"``
    #   * ``parent_run_id=<latest succeeded run_id from runs store>``
    #   * ``metadata.reused_compile_from_run_id=<parent_run_id>``
    #     — the workflow's compile stage reads this and short-circuits
    #     to "reuse compile artifacts from that run".
    #
    # CAS promotion still applies on terminal success — same gate as
    # reindex, so a failed refresh leaves the active snapshot intact.

    @app.post(
        "/ingestion-runs/{run_id}/refresh-enrichment",
        tags=["ingestion-runs"],
        summary=(
            "(Deprecated) Refresh enrichment on the active run"
        ),
        description=(
            "**Deprecated.** This endpoint is being replaced by the "
            "explicit post-index Manual Actions surface (see "
            "``GET /documents/{id}/manual-actions``). The default "
            "Index path should remain lightweight; richer behaviour "
            "(domain enrichment, knowledge memory, normalisation, "
            "deep knowledge index, multimodal enrichment) belongs "
            "in operator-triggered actions with clear cost notes.\n\n"
            "Kept available for in-flight deployments and tests. "
            "New FE surfaces SHOULD NOT render a primary 'Refresh "
            "Enrich' button.\n\n"
            "Behavior unchanged: starts a new candidate ingestion "
            "run that REUSES this run's compile output and re-runs "
            "only enrichment + graph + index. The new run carries "
            "`runType=refresh_enrich` and "
            "`metadata.reusedCompileFromRunId=<this run id>`. "
            "Promotion to `activeSnapshotId` is gated on terminal "
            "success — a failed refresh preserves the previous "
            "active.\n\n"
            "Refuses (HTTP 409) when:\n"
            "  * `run_id` is not the document's currently active "
            "run (refresh-enrichment is an active-run-only action);\n"
            "  * the run is still in-flight;\n"
            "  * the document is detached or removed."
        ),
        deprecated=True,
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def post_run_refresh_enrichment(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        starter: JobStarter = Depends(require_job_starter),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        from datetime import datetime, timezone
        store = _require_run_store()

        previous = store.get(ctx, run_id)
        if previous is None:
            raise HTTPException(404, f"ingestion run {run_id!r} not found")
        if not previous.document_id:
            raise HTTPException(
                400,
                f"run {run_id!r} has no document_id; cannot "
                "refresh-enrich",
            )
        document_id = previous.document_id
        _enforce_document_attached_for_action(
            ctx, document_id, "refresh-enrichment",
        )
        try:
            doc_dto = facade.source_lookup.get_source(ctx, document_id)
        except Exception:
            raise HTTPException(
                404, f"document {document_id!r} not found",
            )

        # Active-run guard: refresh-enrichment only operates on the
        # document's currently active run.
        active_run_id = _latest_succeeded_run_id(ctx, document_id)
        if active_run_id != run_id:
            if not active_run_id:
                raise HTTPException(
                    409,
                    f"document {document_id!r} has no active run yet; "
                    "run an initial reindex first via "
                    "POST /documents/{id}/reindex",
                )
            raise HTTPException(
                409,
                f"run {run_id!r} is not the active run for document "
                f"{document_id!r} (active is {active_run_id!r}). "
                "Refresh-enrichment is only valid on the active run.",
            )

        # In-flight guard mirrors what the active-run lookup would
        # have already excluded, but kept explicit so the message is
        # clear for ASSESSING / CANCELLING edge cases.
        active_states = {
            RunStatus.RUNNING.value, RunStatus.PAUSED.value,
            RunStatus.CANCELLING.value, RunStatus.ASSESSING.value,
        }
        if str(previous.status) in active_states:
            raise HTTPException(
                409,
                f"active run {run_id!r} is currently "
                f"{previous.status} — wait for it to complete before "
                "refresh-enrichment",
            )

        actor = security.subject if security else "system"
        new_run_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        previous_meta = dict(previous.metadata or {})
        from j1.runs.models import allocate_display_version
        prior_runs = store.list_runs(ctx, document_id=document_id)
        display_version = allocate_display_version(
            started_at=now,
            existing_runs=prior_runs,
            document_id=document_id,
        )
        new_run = IngestionRun(
            run_id=new_run_id,
            document_id=document_id,
            workflow_id=None,
            workflow_run_id=None,
            status=RunStatus.CREATED,
            started_at=now,
            updated_at=now,
            metadata={
                "policy": previous_meta.get("policy", "auto"),
                "mode": previous_meta.get("mode", "STANDARD"),
                "document_name": previous_meta.get(
                    "document_name", doc_dto.original_filename,
                ),
                # The load-bearing hint: the compile stage reads
                # this and reuses compile artifacts from that run.
                "reused_compile_from_run_id": run_id,
            },
            run_type="refresh_enrich",
            parent_run_id=run_id,
            document_version_id=previous.document_version_id,
            display_version=display_version,
            target_snapshot_id=_allocate_target_snapshot(
                ctx, document_id, new_run_id,
            ),
        )
        store.upsert(ctx, new_run)
        if progress_reporter is not None:
            progress_reporter.report_run_created(
                ctx, run_id=new_run_id, document_id=document_id,
                actor=actor,
            )
        body = IngestRequest(
            compiler_kind=_resolve_compiler_kind(None),
            enricher_kind=_resolve_optional_processor_kind(
                None,
                (processing_capabilities.enricher_kinds
                 if processing_capabilities else frozenset()),
                "enricherKind",
            ),
            graph_builder_kind=_resolve_optional_processor_kind(
                None,
                (processing_capabilities.graph_builder_kinds
                 if processing_capabilities else frozenset()),
                "graphBuilderKind",
            ),
            indexer_kind=_resolve_optional_processor_kind(
                None,
                (processing_capabilities.indexer_kinds
                 if processing_capabilities else frozenset()),
                "indexerKind",
            ),
            actor=actor,
            correlation_id=new_run_id,
            reindex_of=run_id,
            target_snapshot_id=new_run.target_snapshot_id,
        )
        workflow_id = await starter(ctx, document_id, body)
        new_run.workflow_id = workflow_id
        new_run.updated_at = datetime.now(timezone.utc)
        store.upsert(ctx, new_run)
        return envelope(
            {
                "documentId": document_id,
                "refreshRunId": new_run_id,
                "parentRunId": run_id,
                "workflowId": workflow_id,
                "runType": "refresh_enrich",
                "reusedCompileFromRunId": run_id,
            },
            _req_id(request),
        )

    # ---- Document-centric read API (Phase 6) ----------------------
    #
    # Read-side surface the FE consumes to render document-centric
    # views. Server computes `availableActions` per the spec's
    # state matrix so the FE never has to infer action rules from
    # `knowledgeState` + run status. All three endpoints degrade to
    # 503 when the source registry isn't wired (consistent with the
    # existing degradation pattern across the adapter).

    def _require_source_registry():
        """503 when `facade.source_lookup` isn't available. The
 read endpoints depend on the source registry because they
 list/get documents directly."""
        lookup = getattr(facade, "source_lookup", None)
        if lookup is None:
            raise HTTPException(
                503,
                "source-lookup service not configured "
                "(pass it via the ApplicationFacade)",
            )
        return lookup

    def _runs_for_document(ctx: ProjectContext, document_id: str):
        """All runs tied to a document. Returns an empty list when
 the run store isn't wired — the projector handles that
 gracefully (active run becomes None, history is empty)."""
        if ingestion_run_store is None:
            return []
        all_runs = ingestion_run_store.list(ctx)
        return [r for r in all_runs if r.document_id == document_id]

    def _document_summary_to_payload(dto) -> dict[str, Any]:
        """Camel-case projection. Keeps the wire shape consistent
 with the rest of the adapter; the FE consumes JSON only."""
        return {
            "documentId": dto.document_id,
            "displayName": dto.display_name,
            "knowledgeState": dto.knowledge_state,
            "activeSnapshotId": dto.active_snapshot_id,
            "latestVersionId": dto.latest_version_id,
            "createdAt": dto.created_at.isoformat() if dto.created_at else None,
            "updatedAt": dto.updated_at.isoformat() if dto.updated_at else None,
            "removedAt": dto.removed_at.isoformat() if dto.removed_at else None,
            "currentResultSummary": _result_summary_payload(dto.current_result_summary),
            "availableActions": list(dto.available_actions),
            "runHistorySummary": [
                _run_summary_payload(r) for r in dto.run_history_summary
            ],
        }

    def _document_detail_to_payload(dto) -> dict[str, Any]:
        """Detail view differs from the summary only in that it
 carries the full run history (no cap)."""
        return {
            "documentId": dto.document_id,
            "displayName": dto.display_name,
            "knowledgeState": dto.knowledge_state,
            "activeSnapshotId": dto.active_snapshot_id,
            "latestVersionId": dto.latest_version_id,
            "createdAt": dto.created_at.isoformat() if dto.created_at else None,
            "updatedAt": dto.updated_at.isoformat() if dto.updated_at else None,
            "removedAt": dto.removed_at.isoformat() if dto.removed_at else None,
            "currentResultSummary": _result_summary_payload(dto.current_result_summary),
            "availableActions": list(dto.available_actions),
            "runHistory": [_run_summary_payload(r) for r in dto.run_history],
        }

    def _result_summary_payload(summary) -> dict[str, Any]:
        return {
            "status": summary.status,
            "compileStatus": summary.compile_status,
            "enrichmentStatus": summary.enrichment_status,
            "validationStatus": summary.validation_status,
            "failureCode": summary.failure_code,
        }

    def _snapshot_summary_payload(s, active_snapshot_id: str | None) -> dict[str, Any]:
        """One row of the snapshot-history endpoint. Mirrors the
        DocumentSnapshot dataclass to the wire camelCase shape and
        marks the active snapshot for FE highlighting."""
        state = getattr(s, "state", None)
        state_value = (
            state.value if hasattr(state, "value") else str(state) if state else None
        )
        index_kinds = []
        try:
            for ref in (getattr(s, "index_refs", ()) or ()):
                kind = getattr(ref, "kind", None)
                index_kinds.append(
                    kind.value if hasattr(kind, "value") else str(kind)
                )
        except Exception:  # noqa: BLE001 — defensive against shape drift
            pass
        return {
            "snapshotId": s.snapshot_id,
            "documentId": getattr(s, "document_id", None),
            "createdByRunId": getattr(s, "created_by_run_id", None),
            "state": state_value,
            "createdAt": (
                s.created_at.isoformat()
                if getattr(s, "created_at", None) else None
            ),
            "promotedAt": (
                s.promoted_at.isoformat()
                if getattr(s, "promoted_at", None) else None
            ),
            "supersededAt": (
                s.superseded_at.isoformat()
                if getattr(s, "superseded_at", None) else None
            ),
            "isActive": (
                active_snapshot_id is not None
                and s.snapshot_id == active_snapshot_id
            ),
            "indexKinds": index_kinds,
        }

    def _run_summary_payload(r) -> dict[str, Any]:
        return {
            "runId": r.run_id,
            "runType": r.run_type,
            "status": r.status,
            "startedAt": r.started_at.isoformat() if r.started_at else None,
            "completedAt": (
                r.completed_at.isoformat() if r.completed_at else None
            ),
            "failureCode": r.failure_code,
            "isActive": r.is_active,
            # The snapshot this run produced (or was allocated to
            # build). Renders as the "Produced snapshot" column on
            # the run-history table so the snapshot boundary is
            # visible directly. ``None`` on legacy runs predating
            # the snapshot model — the FE renders an em-dash.
            "targetSnapshotId": r.target_snapshot_id,
            "displayVersion": r.display_version,
            # Run-level capability flags (the FE renders Run Detail
            # action buttons off these — never recomputed locally).
            "isOnlyRun": r.is_only_run,
            "canDeleteRun": r.can_delete_run,
            "canRefreshEnrichment": r.can_refresh_enrichment,
            "canRunEnrichment": r.can_run_enrichment,
        }

    @app.get(
        "/documents",
        tags=["documents"],
        summary="List documents in the project with knowledge-state + actions",
        description=(
            "Document-centric replacement for the run-list. Each row "
            "carries the document's `knowledgeState`, server-computed "
            "`availableActions`, current-result summary derived from "
            "the active run, and a capped run-history tail (most "
            "recent 3 attempts). Removed documents are excluded by "
            "default — pass `?includeRemoved=true` to surface them "
            "in audit/admin views."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_documents(
        request: Request,
        ctx: ProjectContext = Depends(get_ctx),
        include_removed: bool = Query(default=False, alias="includeRemoved"),
    ) -> dict[str, Any]:
        from j1.documents.projector import project_document_summary
        # We use the underlying registry (via facade.source_lookup's
        # backing store) to LIST all documents in this project.
        # SourceLookupService doesn't expose `list_documents` on its
        # public interface, so we read the registry directly via
        # the ApplicationFacade-provided service's internals. Phase
        # 9 could add a proper public method.
        lookup = _require_source_registry()
        sources_registry = getattr(lookup, "_sources", None)
        if sources_registry is None:
            raise HTTPException(
                503, "source registry not exposed for listing",
            )
        documents = sources_registry.list_documents(ctx)
        # Build a run map once so each document's projection is a
        # filtered slice — O(D + R) total vs. O(D * R) if we called
        # the store per document.
        all_runs = (
            ingestion_run_store.list(ctx)
            if ingestion_run_store is not None else []
        )
        runs_by_doc: dict[str, list] = {}
        for run in all_runs:
            runs_by_doc.setdefault(run.document_id, []).append(run)

        rows = []
        for document in documents:
            if document.knowledge_state == "removed" and not include_removed:
                continue
            dto = project_document_summary(
                document=document,
                runs=runs_by_doc.get(document.document_id, []),
            )
            rows.append(_document_summary_to_payload(dto))
        return envelope({"documents": rows}, _req_id(request))

    @app.get(
        "/documents/{document_id}/detail",
        tags=["documents"],
        summary="Get a single document with full run history + actions",
        description=(
            "Document-centric detail projection: knowledge state + "
            "active-run pointer + server-computed `availableActions` "
            "+ the full `runHistory` (un-capped). Distinct path from "
            "the existing `GET /documents/{id}` (which returns just "
            "the upload metadata) so the two shapes can coexist "
            "during the document-centric migration. Returns 404 when "
            "the document doesn't exist in the caller's tenant/project."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_document_detail(
        request: Request,
        document_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        from j1.documents.projector import project_document_detail
        lookup = _require_source_registry()
        try:
            record = lookup._sources.get(ctx, document_id)
        except Exception:
            raise HTTPException(404, f"document {document_id!r} not found")
        runs = _runs_for_document(ctx, document_id)
        dto = project_document_detail(document=record, runs=runs)
        return envelope(_document_detail_to_payload(dto), _req_id(request))

    @app.get(
        "/documents/{document_id}/runs",
        tags=["documents"],
        summary="Run history for a document, most recent first",
        description=(
            "Compact per-run rows: `runId`, `runType`, `status`, "
            "timestamps, `isActive` flag. Use this when you only need "
            "the history without the full document detail (e.g. the "
            "run-history panel pagination)."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_document_runs(
        request: Request,
        document_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        from j1.documents.projector import project_run_history
        lookup = _require_source_registry()
        try:
            record = lookup._sources.get(ctx, document_id)
        except Exception:
            raise HTTPException(404, f"document {document_id!r} not found")
        runs = _runs_for_document(ctx, document_id)
        history = project_run_history(document=record, runs=runs)
        return envelope(
            {"runs": [_run_summary_payload(r) for r in history]},
            _req_id(request),
        )

    @app.get(
        "/documents/{document_id}/snapshots",
        tags=["documents"],
        summary="Snapshot history for a document, most recent first",
        description=(
            "Per-snapshot rows backing the snapshot-centric Document "
            "Detail UI: ``snapshotId``, ``state`` "
            "(building/ready/superseded/failed), ``createdByRunId``, "
            "promotion timestamps, and the ``isActive`` flag (snapshot "
            "id == document's ``activeSnapshotId``). Lets the FE "
            "render snapshot state badges on candidate-knowledge "
            "entries without a separate per-snapshot detail call."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_document_snapshots(
        request: Request,
        document_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        if snapshot_service is None:
            raise HTTPException(
                503,
                "snapshot service not configured (pass it via "
                "create_rest_api(snapshot_service=...))",
            )
        lookup = _require_source_registry()
        try:
            record = lookup._sources.get(ctx, document_id)
        except Exception:
            raise HTTPException(404, f"document {document_id!r} not found")
        active_snapshot_id = getattr(record, "active_snapshot_id", None)
        try:
            snapshots = snapshot_service.store.list_for_document(
                ctx, document_id=document_id,
            )
        except Exception:  # noqa: BLE001 — store transient → empty list
            snapshots = []
        rows = [
            _snapshot_summary_payload(s, active_snapshot_id)
            for s in snapshots
        ]
        return envelope(
            {"documentId": document_id, "snapshots": rows},
            _req_id(request),
        )

    @app.get(
        "/ingestion-runs/{run_id}/summary",
        tags=["ingestion-runs"],
        summary="Get the review summary for an ingestion run",
        description=(
            "Aggregate projection over the run: status, duration, step "
            "results, artifact counts, total bytes, warnings, and a "
            "compact quality summary. The `availableViews` field tells "
            "the frontend which Results tabs to enable for this run "
            "(and why a tab is disabled when it isn't available). "
            "Returns 404 if the run does not exist in the caller's "
            "tenant/project."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_ingestion_run_summary(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        service = _require_review_service()
        summary = service.summarize_run(ctx, run_id)
        return envelope(summary.model_dump(by_alias=True), _req_id(request))

    @app.get(
        "/ingestion-runs/{run_id}/artifacts",
        tags=["ingestion-runs"],
        summary="List artifacts produced by an ingestion run",
        description=(
            "Paginated list of artifacts the run produced. Filtering "
            "by `kind` happens AFTER run-scoping — the page count "
            "reflects only this run's artifacts (matched by tagged "
            "`metadata.run_id` first, then by lineage on "
            "`source_document_ids` for legacy artifacts). Returns 404 "
            "if the run does not exist in the caller's tenant/project."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def list_ingestion_run_artifacts(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        kind: str | None = Query(default=None),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200, alias="pageSize"),
    ) -> dict[str, Any]:
        service = _require_review_service()
        result = service.list_run_artifacts(
            ctx, run_id, kind=kind, page=page, page_size=page_size,
        )
        return envelope(result.model_dump(by_alias=True), _req_id(request))

    @app.get(
        "/ingestion-runs/{run_id}/artifacts/{artifact_id}/content",
        tags=["ingestion-runs"],
        summary="Read the content bytes of one run-scoped artifact",
        description=(
            "Returns the artifact's raw bytes. Verifies the full "
            "ownership chain (tenant + project + run + artifact) — "
            "any break returns 404. `Content-Type` is derived from "
            "the artifact's filename extension. Inline-renderable "
            "types (json/text/image/pdf/...) are served with their "
            "media type; everything else is `application/octet-stream` "
            "with `Content-Disposition: attachment`. `ETag` carries "
            "the artifact's content hash so the FE can cache safely "
            "across tab switches."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_ingestion_run_artifact_content(
        request: Request,
        run_id: str,
        artifact_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> Response:
        service = _require_review_service()
        content = service.read_run_artifact_content(ctx, run_id, artifact_id)
        headers: dict[str, str] = {
            "ETag": f'"{content.content_hash}"',
            "Cache-Control": "private, max-age=3600",
            REQUEST_ID_HEADER: _req_id(request),
        }
        if not content.is_inline:
            # Force download for unknown / binary types so the browser
            # never renders something we don't trust to display safely
            # in-page.
            headers["Content-Disposition"] = (
                f'attachment; filename="{content.filename}"'
            )
        return Response(
            content=content.bytes,
            media_type=content.media_type,
            headers=headers,
        )

    @app.get(
        "/ingestion-runs/{run_id}/chunks",
        tags=["ingestion-runs"],
        summary="List the run's chunks (paginated, JSON)",
        description=(
            "Paginated chunk previews suitable for the Results > "
            "Chunks tab. Each item carries a short text preview plus "
            "page / section / token / confidence fields when "
            "producers populated them. Optional filters: `status` "
            "(case-insensitive match against `metadata.status`), "
            "`minConfidence` (strict floor — chunks without a score "
            "are excluded when the filter is active). Returns 404 if "
            "the run does not exist in the caller's tenant/project."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def list_ingestion_run_chunks(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200, alias="pageSize"),
        status: str | None = Query(default=None),
        min_confidence: float | None = Query(
            default=None, alias="minConfidence", ge=0.0, le=1.0,
        ),
    ) -> dict[str, Any]:
        service = _require_review_service()
        result = service.list_run_chunks(
            ctx, run_id,
            page=page, page_size=page_size,
            status=status, min_confidence=min_confidence,
        )
        return envelope(result.model_dump(by_alias=True), _req_id(request))

    @app.get(
        "/ingestion-runs/{run_id}/chunks/{chunk_id}",
        tags=["ingestion-runs"],
        summary="Get one chunk in detail view",
        description=(
            "Returns the full chunk body plus lineage (document ids, "
            "source artifact id, stage). Used by the Results > "
            "Chunks drawer's readable / raw views. Returns 404 if "
            "the chunk does not exist for the given run in the "
            "caller's tenant/project."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_ingestion_run_chunk(
        request: Request,
        run_id: str,
        chunk_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        service = _require_review_service()
        chunk = service.get_run_chunk(ctx, run_id, chunk_id)
        return envelope(chunk.model_dump(by_alias=True), _req_id(request))

    @app.get(
        "/ingestion-runs/{run_id}/exports/chunks.ndjson",
        tags=["ingestion-runs"],
        summary="Stream this run's chunks as NDJSON",
        description=(
            "Streams `application/x-ndjson` — one chunk preview per "
            "line. Same content the JSON list endpoint returns, "
            "without pagination, suitable for download / offline "
            "analysis. Run-scoped (does NOT include chunks from "
            "other runs in the project). Returns 404 if the run "
            "does not exist."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def export_ingestion_run_chunks(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> StreamingResponse:
        service = _require_review_service()
        # `iter_run_chunks_ndjson` validates eagerly — a missing run /
        # cross-tenant access raises `ReviewNotFound` here, which the
        # exception handler maps to a clean 404 BEFORE StreamingResponse
        # has a chance to commit a 200 status.
        iterator = service.iter_run_chunks_ndjson(ctx, run_id)
        return StreamingResponse(
            iterator,
            media_type="application/x-ndjson",
            headers={
                REQUEST_ID_HEADER: _req_id(request),
                "Cache-Control": "private, no-store",
            },
        )

    @app.get(
        "/ingestion-runs/{run_id}/quality-report",
        tags=["ingestion-runs"],
        summary="Get the neutral quality report for an ingestion run",
        description=(
            "Composed projection over the run's enrichment + audit + "
            "step-result data: overall confidence, per-modality "
            "breakdown, warnings, skipped steps, failed-optional "
            "steps, and low-confidence findings (with page / chunk / "
            "artifact references when available). Vendor-shaped "
            "JSON never appears in this response by default. Pass "
            "`?includeRaw=true` to additionally receive the "
            "unprojected source JSON under `rawDebug` — for "
            "debugging only. Returns 404 if the run does not exist "
            "in the caller's tenant/project."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_ingestion_run_quality_report(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        include_raw: bool = Query(default=False, alias="includeRaw"),
    ) -> dict[str, Any]:
        service = _require_review_service()
        report = service.get_run_quality_report(
            ctx, run_id, include_raw=include_raw,
        )
        return envelope(report.model_dump(by_alias=True), _req_id(request))

    @app.get(
        "/ingestion-runs/{run_id}/parsed-content",
        tags=["ingestion-runs"],
        summary="Get the parsed-content manifest (Content Inventory)",
        description=(
            "Returns a normalized projection of the run's "
            "`parsed_content_manifest` artifact — what the parser "
            "actually found in the source document (text blocks, "
            "tables, images, formulas, page count, per-image triage "
            "decisions). The Content Inventory tab in the FE consumes "
            "this directly. Available as soon as the compile activity "
            "emits the manifest, even while downstream stages are "
            "still running. Returns `status=\"unavailable\"` with an "
            "operator-readable reason when no manifest artifact exists "
            "(legacy runs, mid-compile, or compile-failed runs). "
            "Returns 404 if the run does not exist in the caller's "
            "tenant/project."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_ingestion_run_parsed_content(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        service = _require_review_service()
        inventory = service.get_run_content_inventory(ctx, run_id)
        return envelope(inventory.model_dump(by_alias=True), _req_id(request))

    @app.get(
        "/ingestion-runs/{run_id}/planning",
        tags=["ingestion-runs"],
        summary="Get the Planning Report for an ingestion run",
        description=(
            "Returns the Planning Report — a richer projection over "
            "the raw `IngestPlan` returned by `/plan`. Composes the "
            "planner's per-step decisions, a rule-based assessment "
            "summary, a privacy-capped digest of the parsed-content "
            "manifest, and (when `J1_LLM_PLANNING_ENABLED=true`) the "
            "LLM-assisted recommendation block. Available as soon as "
            "the planner emits a `plan.generated` event, even while "
            "downstream stages are still running. Returns "
            "`status=\"unavailable\"` with an operator-readable reason "
            "when no plan exists yet (legacy run, planner disabled, "
            "or run hasn't reached the assessment stage). Returns 404 "
            "if the run does not exist in the caller's tenant/project."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_ingestion_run_planning(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        service = _require_review_service()
        report = service.get_run_planning(ctx, run_id)
        return envelope(report.model_dump(by_alias=True), _req_id(request))

    @app.get(
        "/ingestion-runs/{run_id}/enrich-plan",
        tags=["ingestion-runs"],
        summary="Get the post-compile enrich plan for an ingestion run",
        description=(
            "Returns the post-compile rule-based enrich plan "
            "(`PostCompileEnrichPlan`): an `overall_recommendation` "
            "(skip/optional/recommended/required), the per-task "
            "recommended/skipped lists, blocking issues, source "
            "signals, and the decision_source. Read by the FE's "
            "Enrich Plan card. Returns `status=\"unavailable\"` with "
            "a reason when the run hasn't reached post-compile or "
            "the artifact wasn't persisted. Returns 404 if the run "
            "does not exist in the caller's tenant/project."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_ingestion_run_enrich_plan(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        service = _require_review_service()
        plan = service.get_run_enrich_plan(ctx, run_id)
        return envelope(plan, _req_id(request))

    @app.get(
        "/ingestion-runs/{run_id}/enrichment-result",
        tags=["ingestion-runs"],
        summary="Get the typed enrichment overlay for an ingestion run",
        description=(
            "Returns the typed enrichment overlay "
            "(`EnrichmentResult`): per-module outcomes with run/skip "
            "reasons + provenance, document-metadata overlay, "
            "terminology map, classification result, table / image "
            "summaries, validation findings, retrieval hints, and "
            "aggregate model usage. The raw vendor compile output + "
            "per-enricher artifacts stay where they are; this is the "
            "AGGREGATED typed view downstream consumers branch on. "
            "Returns `status=\"unavailable\"` with a reason when the "
            "enrichment stage was skipped or the artifact wasn't "
            "persisted. Returns 404 if the run does not exist in "
            "the caller's tenant/project."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_ingestion_run_enrichment_result(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        service = _require_review_service()
        result = service.get_run_enrichment_result(ctx, run_id)
        return envelope(result, _req_id(request))

    @app.get(
        "/ingestion-runs/{run_id}/compile-result",
        tags=["ingestion-runs"],
        summary="Get the typed normalized compile result for an ingestion run",
        description=(
            "Returns the typed `NormalizedCompileResult` for the "
            "run's compile output: chunks_count, "
            "extracted_text_chars, page_count, detected_tables / "
            "detected_images (structured), quality_signals, retry "
            "history, warnings + errors, and raw_artifact_refs "
            "pointing at the vendor's preserved output. Read by the "
            "FE's Compile Result panel and by post-compile "
            "consumers that need typed access to compile signals. "
            "Returns `status=\"unavailable\"` with a reason when "
            "the artifact wasn't persisted. Returns 404 if the run "
            "does not exist in the caller's tenant/project."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_ingestion_run_compile_result(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        service = _require_review_service()
        result = service.get_run_compile_result(ctx, run_id)
        return envelope(result, _req_id(request))

    @app.get(
        "/ingestion-runs/{run_id}/initial-execution-plan",
        tags=["ingestion-runs"],
        summary="Get the pre-compile initial execution plan for an ingestion run",
        description=(
            "Returns the pre-compile initial execution plan "
            "(`InitialExecutionPlan`): the selected domain profile, "
            "the enrichment policy, the candidate enrichment "
            "modules, the cheap signals snapshot, resource hints, "
            "operator-readable reasons + warnings, and the wrapped "
            "compile-stage plan. Built by the workflow's pre-compile "
            "`build_initial_execution_plan` activity, persisted as an "
            "`initial_execution_plan` artifact. Returns "
            "`status=\"unavailable\"` with a reason when the run "
            "hasn't reached pre-compile build or the artifact wasn't "
            "persisted. Returns 404 if the run does not exist in the "
            "caller's tenant/project."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_ingestion_run_initial_execution_plan(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        service = _require_review_service()
        plan = service.get_run_initial_execution_plan(ctx, run_id)
        return envelope(plan, _req_id(request))

    @app.get(
        "/ingestion-runs/{run_id}/final-ingestion-report",
        tags=["ingestion-runs"],
        summary="Get the aggregated end-to-end final ingestion report",
        description=(
            "Returns the typed `FinalIngestionReport` that "
            "aggregates the entire ingestion pipeline (initial "
            "execution plan, compile result, post-compile enrich "
            "plan, enrichment overlay, finalize) into a single "
            "FE-facing payload. The report is the single source of "
            "truth for the run-detail page when present; the FE "
            "falls back to per-artifact endpoints when not. Returns "
            "`status=\"unavailable\"` with reason "
            "`final_ingestion_report_not_available` for legacy runs "
            "(no aggregate report persisted) and in-flight runs that "
            "haven't reached terminal yet. Returns 404 if the run "
            "does not exist in the caller's tenant/project."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_ingestion_run_final_ingestion_report(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        service = _require_review_service()
        report = service.get_run_final_ingestion_report(ctx, run_id)
        return envelope(report, _req_id(request))

    @app.get(
        "/ingestion-runs/{run_id}/graph",
        tags=["ingestion-runs"],
        summary="Get the neutral graph snapshot for an ingestion run",
        description=(
            "Returns the run's graph as `entities[]` + `relations[]` "
            "in a vendor-neutral shape — LightRAG / RAGAnything "
            "internal field names are mapped to the standard DTO. "
            "Per-list caps (`maxNodes`, `maxEdges`, default 5000 each) "
            "keep the response bounded; per-list `truncated` flags "
            "tell the FE when the table-fallback view is needed. When "
            "the run produced no graph (skipped by policy / planner / "
            "failed), `unavailable.reason` is populated with the same "
            "copy used by `availableViews.graph.reason` in the run "
            "summary. Returns 404 if the run does not exist in the "
            "caller's tenant/project."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_ingestion_run_graph(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        max_nodes: int = Query(
            default=5000, ge=1, le=50_000, alias="maxNodes",
        ),
        max_edges: int = Query(
            default=5000, ge=1, le=50_000, alias="maxEdges",
        ),
    ) -> dict[str, Any]:
        service = _require_review_service()
        snapshot = service.get_run_graph(
            ctx, run_id, max_nodes=max_nodes, max_edges=max_edges,
        )
        return envelope(snapshot.model_dump(by_alias=True), _req_id(request))

    # ---- Validation: manual test query ---------------------
    # Stateless tester surface: ask one question against an ingested
    # run, get answer + retrieved chunks + citations + deterministic
    # check results scoped to that run. No persistence yet.

    def _require_validation_service() -> IngestionValidationService:
        if validation_service is None:
            raise HTTPException(
                503,
                "ingestion-validation service not configured "
                "(pass `validation_service=` to create_rest_api)",
            )
        return validation_service

    def _test_query_response_record(result) -> "ManualTestQueryResponseRecord":
        """Project a ``ManualTestQueryResponseDTO`` into the wire
        record. Shared by the three test-query endpoints
        (run-keyed legacy, document-level, project-level)."""
        return ManualTestQueryResponseRecord(
            request_id=result.request_id,
            run_id=result.run_id,
            question=result.question,
            answer=result.answer,
            mode_used=result.mode_used,
            retrieved_chunks=[
                RetrievedChunkRefRecord(
                    artifact_id=c.artifact_id,
                    chunk_id=c.chunk_id,
                    run_id=c.run_id,
                    document_id=c.document_id,
                    source_location=c.source_location,
                    score=c.score,
                    preview=c.preview,
                    artifact_kind=c.artifact_kind,
                )
                for c in result.retrieved_chunks
            ],
            citations=[
                CitationRecord(
                    artifact_id=c["artifactId"],
                    artifact_type=c["artifactType"],
                    source_document_id=c.get("sourceDocumentId"),
                    source_location=c.get("sourceLocation"),
                    chunk_id=c.get("chunkId"),
                    run_id=c.get("runId"),
                )
                for c in result.citations
            ],
            checks=[
                ValidationCheckRecord(
                    name=chk.name,
                    severity=chk.severity,
                    passed=chk.passed,
                    detail=chk.detail,
                    expected=chk.expected,
                    actual=chk.actual,
                    skipped=chk.skipped,
                    skipped_reason=chk.skipped_reason,
                )
                for chk in result.checks
            ],
            validation_status=result.validation_status,
            evidence_flags=EvidenceFlagsRecord(
                graph_used=result.evidence_flags.get("graphUsed", False),
                tables_used=result.evidence_flags.get("tablesUsed", False),
                images_used=result.evidence_flags.get("imagesUsed", False),
            ),
            raw_response=result.raw_response,
            synthesized_answer=result.synthesized_answer,
            llm=LLMTraceRecord(
                called=result.llm.called,
                provider=result.llm.provider,
                model=result.llm.model,
                latency_ms=result.llm.latency_ms,
                prompt_tokens=result.llm.prompt_tokens,
                completion_tokens=result.llm.completion_tokens,
                error=result.llm.error,
            ) if result.llm is not None else None,
            evidence_sent_to_llm=[
                EvidenceBlockRecord(
                    artifact_id=b.artifact_id,
                    artifact_type=b.artifact_type,
                    text=b.text,
                    chunk_id=b.chunk_id,
                    score=b.score,
                    page_start=b.page_start,
                    page_end=b.page_end,
                    section=b.section,
                    source_location=b.source_location,
                )
                for b in result.evidence_sent_to_llm
            ],
            debug=dict(getattr(result, "debug", None) or {}),
        )

    def _build_manual_dto(body: ManualTestQueryRequestRecord):
        """Shared shape: REST record → service DTO. The doc/project
        endpoints don't accept ``validation_scope="run"`` (they refuse
        even with ``allowRunScope`` since their URL doesn't carry a
        run); the run-keyed endpoint keeps the diagnostic opt-in."""
        from j1.validation.dtos import QueryScopeDTO as _ScopeDTO
        scope_dto: _ScopeDTO | None = None
        if body.scope is not None:
            scope_dto = _ScopeDTO(
                type=body.scope.type,
                document_id=body.scope.document_id,
                snapshot_ids=tuple(body.scope.snapshot_ids or ()),
                run_id=body.scope.run_id,
            )
        return ManualTestQueryRequestDTO(
            question=body.question,
            top_k=body.top_k,
            mode=body.mode,
            citation_required=body.citation_required,
            include_raw=body.include_raw,
            synthesize=body.synthesize,
            scope=scope_dto,
            validation_scope=body.validation_scope,
            allow_run_scope=body.allow_run_scope,
        )

    @app.post(
        "/ingestion-runs/{run_id}/test-query",
        tags=["ingestion-runs"],
        summary="Run a manual test query against this ingestion run",
        description=(
            "Sends a single tester-supplied question through the "
            "answer engine with retrieval restricted to artifacts "
            "produced by this run. Returns the engine's answer + "
            "retrieved chunks + citations + a deterministic check "
            "report. The HTTP 200 indicates the QUERY ran "
            "successfully; the body's `validationStatus` field "
            "reports whether the answer PASSED the deterministic "
            "checks. The two are independent — a 200 with "
            "`validationStatus=\"failed\"` is the canonical 'job ran "
            "but the answer didn't pass' case.\n\n"
            "Returns 404 if the run does not exist in the caller's "
            "tenant/project."
        ),
        dependencies=[Depends(scope_required(SCOPE_VALIDATION_WRITE))],
    )
    def post_ingestion_run_test_query(
        request: Request,
        run_id: str,
        body: ManualTestQueryRequestRecord,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        # Snapshot-centric scope contract. UI callers send the typed
        # ``scope`` field; legacy callers fall through to the string
        # ``validation_scope`` token. The diagnostic ``"run"`` scope
        # is REFUSED on this endpoint unless the caller explicitly
        # opts in via ``allowRunScope=true`` — Run is execution
        # metadata, not a knowledge unit. The guard runs BEFORE
        # ``_require_validation_service`` so a misconfigured
        # deployment returns the actionable 400 (which the caller
        # can fix) instead of a 503 that hides the real cause.
        if (
            body.scope is None
            and body.validation_scope == "run"
            and not body.allow_run_scope
        ):
            raise HTTPException(
                400,
                "validation_scope=\"run\" is no longer accepted on "
                "this endpoint. Pass a typed `scope` instead: "
                "{ type: 'document_active', documentId } or "
                "{ type: 'snapshot_explicit', snapshotIds: [...] }. "
                "Set `allowRunScope=true` only for diagnostic "
                "tooling that needs raw run-keyed artifact lookup.",
            )
        service = _require_validation_service()
        dto_request = _build_manual_dto(body)
        # `_load_run` inside the service raises `ReviewNotFound` on
        # cross-tenant / cross-project access — caught by the
        # existing exception handler at the top of the app and
        # converted to a uniform 404. We don't need to translate
        # here; just let it propagate.
        result = service.run_manual_test_query(
            ctx, run_id, dto_request,
            actor=security.subject,
        )
        record = _test_query_response_record(result)
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    # ---- Snapshot-centric query endpoints (post-Phase-9) -------------
    #
    # The run-keyed endpoint above remains for legacy + diagnostic
    # use. UI / API callers SHOULD route knowledge queries through
    # these endpoints instead — they don't need a run id, and the
    # routing surface itself enforces "Run is not knowledge".

    @app.post(
        "/documents/{document_id}/test-query",
        tags=["documents"],
        summary="Manual query against this document's active snapshot",
        description=(
            "Snapshot-centric replacement for "
            "`/ingestion-runs/{run_id}/test-query` on the Document "
            "Detail surface. The default scope is "
            "`{ type: 'document_active', documentId }` — the "
            "backend resolves to `document.active_snapshot_id` "
            "without the caller having to know which run produced "
            "the active snapshot. Callers MAY override via "
            "`scope = { type: 'snapshot_explicit', snapshotIds }` "
            "to validate a specific candidate snapshot tied to this "
            "document.\n\n"
            "Refuses `validation_scope=\"run\"` and "
            "`scope = { type: 'snapshot_candidate' / ... }` — those "
            "go through the run endpoint when the use case is "
            "operator-side diagnostic. Returns 404 if the document "
            "doesn't exist in the caller's tenant/project."
        ),
        dependencies=[Depends(scope_required(SCOPE_VALIDATION_WRITE))],
    )
    def post_document_test_query(
        request: Request,
        document_id: str,
        body: ManualTestQueryRequestRecord,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        # This endpoint is snapshot-centric by construction. The
        # legacy `validation_scope` string token never reaches the
        # service — we always send a typed `scope` (either the
        # caller's, or the URL-implied `document_active`).
        if body.allow_run_scope:
            raise HTTPException(
                400,
                "allowRunScope is not accepted on the document-level "
                "test-query endpoint. Use "
                "/ingestion-runs/{run_id}/test-query for the "
                "diagnostic run-keyed surface.",
            )
        # Reject project-wide scope under a document URL — the path
        # promises document-scoped behaviour; honouring
        # ``project_active`` here would silently widen the query and
        # make the URL misleading. Operators wanting project-wide
        # scope MUST use ``POST /projects/{id}/query``.
        if body.scope is not None and body.scope.type == "project_active":
            raise HTTPException(
                400,
                "scope.type='project_active' is not accepted on the "
                "document-level test-query endpoint. Use "
                "POST /projects/{project_id}/query for project-wide "
                "queries; this URL is document-scoped.",
            )
        # Refuse 404 / 503 for unknown document before doing work.
        lookup = _require_source_registry()
        try:
            lookup._sources.get(ctx, document_id)
        except Exception:
            raise HTTPException(404, f"document {document_id!r} not found")
        service = _require_validation_service()
        dto_request = _build_manual_dto(body)
        result = service.run_document_test_query(
            ctx, document_id, dto_request,
            actor=security.subject,
        )
        record = _test_query_response_record(result)
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    @app.post(
        "/projects/{project_id}/query",
        tags=["projects"],
        summary="Query the project's active knowledge",
        description=(
            "Snapshot-centric global query: every attached document's "
            "active snapshot. Default scope is "
            "`{ type: 'project_active' }`; callers MAY override via "
            "an explicit `scope = { type: 'snapshot_explicit', "
            "snapshotIds }` when they want to query a fixed set of "
            "snapshots across the project. The path's `project_id` "
            "MUST match `X-Project-Id`; mismatch returns 400.\n\n"
            "Excludes detached / removed documents and "
            "building / failed / superseded snapshots. Run lineage "
            "is irrelevant — the eligibility resolver is the "
            "visibility key."
        ),
        dependencies=[Depends(scope_required(SCOPE_VALIDATION_WRITE))],
    )
    def post_project_query(
        request: Request,
        project_id: str,
        body: ManualTestQueryRequestRecord,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        # Project-id consistency. The header carries the real
        # tenant/project context; the URL path is operator-friendly
        # but MUST agree.
        if project_id != ctx.project_id:
            raise HTTPException(
                400,
                f"URL project_id {project_id!r} does not match "
                f"X-Project-Id {ctx.project_id!r}",
            )
        if body.allow_run_scope:
            raise HTTPException(
                400,
                "allowRunScope is not accepted on the project-level "
                "query endpoint. Use "
                "/ingestion-runs/{run_id}/test-query for the "
                "diagnostic run-keyed surface.",
            )
        service = _require_validation_service()
        dto_request = _build_manual_dto(body)
        result = service.run_project_query(
            ctx, dto_request,
            actor=security.subject,
        )
        record = _test_query_response_record(result)
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    # ---- Raw orchestrator trace surface (developer / operator) ----
    #
    # The new ``SmartQueryOrchestrator`` produces a full
    # ``QueryTrace`` per query — plan, routes, candidates, dropped
    # blocks with reasons, gate results, final status. This endpoint
    # exposes the trace verbatim so operators can answer "why did
    # the query fail" without instrumentation gymnastics. The
    # response is intentionally NOT shaped like
    # ``ManualTestQueryResponseDTO`` — that DTO is the production
    # shape the frontend reads; this one is the diagnostic shape.
    #
    # Endpoint is OPTIONAL: when no orchestrator was wired into
    # ``create_rest_api``, it returns 503 with a hint pointing to
    # the wiring helper.

    @app.post(
        "/dev/query-trace",
        tags=["system"],
        summary="Diagnostic: run a question through SmartQueryOrchestrator",
        description=(
            "Developer/operator surface — runs a single question "
            "through the new SmartQueryOrchestrator and returns the "
            "full ``QueryTrace`` JSON. Includes the plan, every "
            "route execution, all candidates with kept/dropped "
            "reasons, evidence groups covered/missing, the exact "
            "blocks sent to the LLM, the answer, citations, and "
            "every gate result. Use this when the production "
            "``/ingestion-runs/{id}/test-query`` answer looks "
            "wrong: the trace shows WHY.\n\n"
            "Returns 503 when the orchestrator isn't wired."
        ),
        dependencies=[Depends(scope_required(SCOPE_VALIDATION_WRITE))],
    )
    def post_dev_query_trace(
        request: Request,
        body: dict,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        if smart_query_orchestrator is None:
            raise HTTPException(
                503,
                "smart_query_orchestrator not configured "
                "(pass `smart_query_orchestrator=` to create_rest_api)",
            )
        question = str(body.get("question") or "").strip()
        if not question:
            raise HTTPException(
                400, "request body must contain a non-empty 'question'",
            )
        run_id = body.get("run_id") or body.get("runId")
        document_id = body.get("document_id") or body.get("documentId")
        # Resolve ``document_id`` from the run record when the
        # caller didn't supply one. The RAGAnything per-run
        # workspace path is
        # ``{workdir}/runs/{tenant}/{project}/{document}/{run_id}`` —
        # without ``document_id`` the path resolves to ``None`` and
        # LightRAG silently falls back to the GLOBAL workdir (which
        # has no per-run data → "Sorry, I'm not able to provide an
        # answer to that question" for every query).
        if run_id and not document_id and ingestion_run_store is not None:
            try:
                run = ingestion_run_store.get(ctx, str(run_id))
                if run is not None:
                    document_id = run.document_id
            except Exception:  # noqa: BLE001 — best-effort resolution
                pass
        from j1.query.orchestrator import OrchestratorRequest
        from j1.query.scope import RunScope, default_scope
        scope = (
            RunScope(run_id=str(run_id))
            if run_id else default_scope()
        )
        result = smart_query_orchestrator.run(OrchestratorRequest(
            ctx=ctx,
            question=question,
            scope=scope,
            run_id=str(run_id) if run_id else None,
            document_id=str(document_id) if document_id else None,
        ))
        # The response is the trace JSON verbatim PLUS a tiny header.
        # Frontends consuming this endpoint render the trace fields
        # directly; no DTO mapping required.
        return envelope({
            "final_status": result.final_status,
            "answer": result.answer,
            "message": result.message,
            "trace": result.trace.to_dict(),
        }, _req_id(request))

    @app.post(
        "/ingestion-runs/{run_id}/native-debug-query",
        tags=["ingestion-runs"],
        summary=(
            "Direct LightRAG-native diagnostic query against this run"
        ),
        description=(
            "Calls LightRAG `aquery` against this run's workspace "
            "directly, with **no BM25 involvement** at any layer. "
            "Operators use it to isolate native indexing problems "
            "without the confounding effects of the BM25 + "
            "reranker + coverage pipeline used by the regular "
            "test-query endpoint. The response includes the "
            "resolved per-run workspace path so the operator can "
            "confirm scoping at a glance.\n\n"
            "Returns 200 even when the native call fails — "
            "`nativeQueryUsed=false` + `nativeQueryFailedReason` "
            "report the outcome. 404 if the run does not exist in "
            "the caller's tenant/project; 503 if the validation "
            "service isn't configured."
        ),
        dependencies=[Depends(scope_required(SCOPE_VALIDATION_WRITE))],
    )
    def post_ingestion_run_native_debug_query(
        request: Request,
        run_id: str,
        body: NativeDebugQueryRequestRecord,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        service = _require_validation_service()
        result = service.run_native_debug_query(
            ctx, run_id, body.question,
            actor=security.subject,
        )
        record = NativeDebugQueryResponseRecord(
            request_id=result.request_id,
            run_id=result.run_id,
            document_id=result.document_id,
            question=result.question,
            answer=result.answer,
            workspace_path=result.workspace_path,
            workspace_id=result.workspace_id,
            native_query_used=result.native_query_used,
            native_query_failed_reason=result.native_query_failed_reason,
            native_latency_ms=result.native_latency_ms,
            provider_wired=result.provider_wired,
        )
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    # ---- Validation: imported test cases (auxiliary helper) ----
    #
    # Per the 2026-05-14 product decision, generated test cases were
    # deleted entirely. The Validation Tab now hosts a small
    # "Imported Test Cases" section: upload a CSV per document,
    # execute against the document's latest succeeded run, render
    # only summary cards + compact statuses. Per-question detail is
    # the existing Manual Test Query surface.

    @app.post(
        "/documents/{document_id}/imported-test-cases/import",
        status_code=201,
        tags=["documents"],
        summary="Import test cases from a CSV (replaces prior set)",
        description=(
            "Replaces the document's current imported test case set "
            "with rows parsed from the uploaded CSV. Every import "
            "wipes the previous set AND the previous execution "
            "snapshot — imported cases never accumulate.\n\n"
            "Required CSV column: `question`. Optional columns: "
            "`expected_answer`, `expected_sources`, `test_type`, "
            "`notes`. UTF-8 BOMs are tolerated.\n\n"
            "Returns 400 on malformed CSV; 503 when the validation "
            "service isn't wired."
        ),
        dependencies=[Depends(scope_required(SCOPE_VALIDATION_WRITE))],
    )
    async def post_imported_test_cases_import(
        request: Request,
        document_id: str,
        file: UploadFile = File(...),
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        from j1.validation.imported_test_cases import CSVImportError
        service = _require_validation_service()
        raw = await file.read()
        try:
            imported_set = service.import_test_cases(
                ctx, document_id, raw,
                source_filename=file.filename,
                actor=security.subject,
            )
        except CSVImportError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        return envelope(
            _imported_set_to_record(imported_set).model_dump(by_alias=True),
            _req_id(request),
        )

    @app.get(
        "/documents/{document_id}/imported-test-cases",
        tags=["documents"],
        summary="Get the imported test case set for this document",
        description=(
            "Returns the current imported set (cases plus import "
            "metadata) for the document, or 404 when no set has "
            "been imported."
        ),
        dependencies=[Depends(scope_required(SCOPE_VALIDATION_READ))],
    )
    def get_imported_test_cases(
        request: Request,
        document_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        service = _require_validation_service()
        imported_set = service.get_imported_test_cases(ctx, document_id)
        if imported_set is None:
            raise HTTPException(
                404,
                f"document {document_id!r} has no imported test cases",
            )
        return envelope(
            _imported_set_to_record(imported_set).model_dump(by_alias=True),
            _req_id(request),
        )

    @app.delete(
        "/documents/{document_id}/imported-test-cases",
        tags=["documents"],
        summary="Delete the imported test case set for this document",
        description=(
            "Clears the imported set (and its execution snapshot) "
            "so the Validation Tab returns to a clean slate. "
            "Idempotent: a 204 is returned regardless of whether a "
            "set existed."
        ),
        status_code=204,
        dependencies=[Depends(scope_required(SCOPE_VALIDATION_WRITE))],
    )
    def delete_imported_test_cases(
        request: Request,
        document_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> Response:
        service = _require_validation_service()
        service.delete_imported_test_cases(ctx, document_id)
        return Response(status_code=204)

    @app.post(
        "/documents/{document_id}/imported-test-cases/execute",
        status_code=201,
        tags=["documents"],
        summary="Run the imported test cases against the active run",
        description=(
            "Runs every imported question through the normal J1 "
            "query path against the document's latest succeeded "
            "run. Returns a compact summary + per-question status "
            "the Validation Tab renders directly.\n\n"
            "Does NOT affect ingestion / compile / enrichment / "
            "document active status — results are observational "
            "only.\n\n"
            "Returns 404 when no imported set exists or no "
            "succeeded run is available; 503 when the validation "
            "service isn't wired."
        ),
        dependencies=[Depends(scope_required(SCOPE_VALIDATION_WRITE))],
    )
    def post_imported_test_cases_execute(
        request: Request,
        document_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        service = _require_validation_service()
        try:
            execution = service.execute_imported_test_cases(
                ctx, document_id, actor=security.subject,
            )
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        return envelope(
            _imported_execution_to_record(execution).model_dump(
                by_alias=True,
            ),
            _req_id(request),
        )

    @app.get(
        "/documents/{document_id}/imported-test-cases/execution",
        tags=["documents"],
        summary="Get the latest imported-test-case execution snapshot",
        description=(
            "Returns the most recent execution snapshot for this "
            "document's imported set, or 404 when no execution has "
            "happened yet. The Validation Tab calls this on load to "
            "render summary cards without re-running."
        ),
        dependencies=[Depends(scope_required(SCOPE_VALIDATION_READ))],
    )
    def get_imported_test_cases_execution(
        request: Request,
        document_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        service = _require_validation_service()
        execution = service.get_latest_imported_execution(ctx, document_id)
        if execution is None:
            raise HTTPException(
                404,
                f"document {document_id!r} has no execution snapshot",
            )
        return envelope(
            _imported_execution_to_record(execution).model_dump(
                by_alias=True,
            ),
            _req_id(request),
        )

    @app.post(
        "/ingestion-runs/{run_id}/confirm",
        tags=["ingestion-runs"],
        summary="Confirm a generated execution plan",
        description=(
            "Acknowledges the plan and signals the workflow to "
            "continue. Three things happen, in order:\n"
            "  1. The run record's status flips to RUNNING (so any "
            "client polling `GET /ingestion-runs/{id}` sees the new "
            "state immediately).\n"
            "  2. A `plan.confirmed` progress event is emitted so the "
            "SSE timeline shows the operator action.\n"
            "  3. The deployment's `confirm_handler` is invoked with "
            "(ctx, run_id) — typical implementation forwards a "
            "Temporal signal to the workflow that was parked at the "
            "confirmation gate. When no handler is wired, the status "
            "flip + progress event are still authoritative.\n"
            "No-op when the run is already running (default "
            "deployment behaviour is auto-run; the confirmation gate "
            "is opt-in per-run)."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def post_ingestion_run_confirm(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        store = _require_run_store()
        run = store.get(ctx, run_id)
        if run is None:
            raise HTTPException(404, f"ingestion run {run_id!r} not found")
        if run.status not in (
            RunStatus.PLAN_READY,
            RunStatus.ASSESSMENT_READY,
            RunStatus.WAITING_FOR_CONFIRMATION,
        ):
            # Already running / completed / failed — noop, but report
            # the current status so the caller can inspect.
            record = IngestionRunConfirmRecord(run_id=run_id, status=run.status.value)
            return envelope(record.model_dump(by_alias=True), _req_id(request))

        from datetime import datetime, timezone

        # 1. Persist the transition + audit-trail metadata. The
        # `confirmed_at` / `confirmed_by` fields let downstream tools
        # (and the workflow when it polls the store) tell a "true"
        # confirm from a status flip due to e.g. a manual edit.
        now = datetime.now(timezone.utc)
        actor = security.subject if security and security.subject else "system"
        run.status = RunStatus.RUNNING
        run.updated_at = now
        run.metadata = dict(run.metadata)
        run.metadata["confirmed_at"] = now.isoformat()
        run.metadata["confirmed_by"] = actor
        store.upsert(ctx, run)

        # 2. Emit the timeline event. The reporter is optional — the
        # `/ingestion-runs/*` surface can run without it (run record
        # is still authoritative; the timeline just won't be
        # populated). Failures here must NEVER block the confirmation.
        if progress_reporter is not None:
            try:
                progress_reporter.report_plan_confirmed(
                    ctx, run_id=run_id, actor=actor,
                )
            except Exception:  # noqa: BLE001 — observability never blocks
                _log.exception("plan.confirmed event emission failed")

        # 3. Forward to the deployment's signal hook. Same failure
        # rule as above: confirmation is acknowledged at the REST
        # boundary even if the downstream signal can't be delivered
        # (the workflow can pick up the persisted status on its next
        # heartbeat / poll). Operators see the failure in logs.
        if confirm_handler is not None:
            try:
                await confirm_handler(ctx, run_id)
            except Exception:  # noqa: BLE001
                _log.exception(
                    "confirm_handler failed for run_id=%s; "
                    "run record is RUNNING but downstream signal "
                    "was not delivered", run_id,
                )

        record = IngestionRunConfirmRecord(run_id=run_id, status=run.status.value)
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    @app.post(
        "/ingestion-runs/{run_id}/compile",
        tags=["ingestion-runs"],
        summary="Trigger the compile phase of a two-phase ingestion",
        description=(
            "Releases a run that's parked at `compile_pending` (the "
            "two-phase compile gate) into the compile activity. The "
            "run must have been started with `two_phase_compile=True` "
            "for this gate to exist; otherwise the workflow already "
            "dispatched compile inline and this endpoint is a no-op.\n"
            "Three things happen, in order:\n"
            "  1. The run record's status flips to `running` so "
            "polling clients see the trigger landed.\n"
            "  2. A `compile.trigger` audit / progress event is "
            "emitted (best-effort).\n"
            "  3. The deployment's `compile_handler` is invoked with "
            "(ctx, run_id) — typical implementation forwards "
            "`SIGNAL_TRIGGER_COMPILE` to the workflow handle. When no "
            "handler is wired, the status flip is still authoritative "
            "and the workflow will pick up the persisted status on its "
            "next poll.\n"
            "No-op when the run is already past the gate "
            "(`running` / terminal); the caller gets the current "
            "status in the response."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def post_ingestion_run_compile(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        store = _require_run_store()
        run = store.get(ctx, run_id)
        if run is None:
            raise HTTPException(404, f"ingestion run {run_id!r} not found")
        if run.status != RunStatus.COMPILE_PENDING:
            # Already running / completed / failed / never parked —
            # surface the current status so the caller can inspect.
            record = IngestionRunCompileRecord(
                run_id=run_id, status=run.status.value,
            )
            return envelope(record.model_dump(by_alias=True), _req_id(request))

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        actor = security.subject if security and security.subject else "system"
        run.status = RunStatus.RUNNING
        run.updated_at = now
        run.metadata = dict(run.metadata)
        run.metadata["compile_triggered_at"] = now.isoformat()
        run.metadata["compile_triggered_by"] = actor
        store.upsert(ctx, run)

        # Best-effort progress event so the SSE timeline shows the
        # operator action. The reporter is optional; failure here
        # never blocks the trigger.
        if progress_reporter is not None:
            try:
                progress_reporter.report_step_started(
                    ctx, run_id=run_id,
                    stage="COMPILE", step="compile_trigger",
                    engine=None, actor=actor,
                )
            except Exception:  # noqa: BLE001 — observability never blocks
                _log.exception("compile.trigger event emission failed")

        # Forward to the deployment's signal hook. Same failure
        # contract as `/confirm`: REST returns success on the status
        # flip even when the downstream signal can't be delivered.
        if compile_handler is not None:
            try:
                await compile_handler(ctx, run_id)
            except Exception:  # noqa: BLE001
                _log.exception(
                    "compile_handler failed for run_id=%s; "
                    "run record is RUNNING but downstream signal "
                    "was not delivered", run_id,
                )

        record = IngestionRunCompileRecord(run_id=run_id, status=run.status.value)
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    # ---- Run-level control endpoints --------------------------------
    # Mirror the existing `/ingestion-jobs/{id}/{pause,resume,cancel}`
    # admin signals but expose them under the `/ingestion-runs/`
    # surface the FE already uses, AND flip the run record's status
    # so polling clients see the operator action immediately. The
    # `/ingestion-jobs/` raw-signal variants stay in place for CLI /
    # script use.

    def _signal_meta(
        delivered: bool, error: str | None,
    ) -> dict[str, Any] | None:
        """Build the response envelope's `meta.signal` block.

 Returns None on success so untouched response shape is
 preserved when the signal landed cleanly. On failure carries
 `delivered=False` plus the exception class + message so the
 FE can render a "run record updated, but the workflow may
 not have caught the signal" advisory instead of a misleading
 success toast.
 """
        if delivered:
            return None
        return {
            "signal": {
                "delivered": False,
                "error": error or "signal delivery failed",
            },
        }

    def _control_action(
        action: str,
        target_status: RunStatus,
        allowed_from: tuple[RunStatus, ...],
        signal_name: str,
        request: Request,
        run_id: str,
        ctx: ProjectContext,
        security: SecurityContext,
    ):
        """Shared body for pause / resume / cancel.

 1. Look up the run record (404 if missing).
 2. Validate the transition (409 if from a status that disallows it).
 3. Flip the run record (FE polling sees the new state immediately).
 4. Emit a progress event so the SSE timeline reflects the action.
 5. Forward to the job-control facade so the workflow signal fires.
 Returns the populated `IngestionRunControlRecord`."""
        from datetime import datetime, timezone

        store = _require_run_store()
        run = store.get(ctx, run_id)
        if run is None:
            raise HTTPException(404, f"ingestion run {run_id!r} not found")
        if run.is_terminal():
            raise HTTPException(
                409,
                f"cannot {action} a terminal run (status={run.status.value})",
            )
        if run.status not in allowed_from:
            raise HTTPException(
                409,
                f"cannot {action} run in status {run.status.value!r}; "
                f"allowed from: {[s.value for s in allowed_from]}",
            )
        now = datetime.now(timezone.utc)
        actor = security.subject if security and security.subject else "system"
        run.status = target_status
        run.updated_at = now
        run.metadata = dict(run.metadata)
        run.metadata[f"{action}_at"] = now.isoformat()
        run.metadata[f"{action}_by"] = actor
        store.upsert(ctx, run)
        if progress_reporter is not None:
            try:
                # Reuse the existing step.warning channel (severity=INFO
                # via the optional `metadata`) to surface the operator
                # action in the timeline. We don't add a dedicated
                # `run.paused` event type because the FE doesn't render
                # it — the run record's `status` flip is already the
                # authoritative signal for the panel/badge update.
                progress_reporter.report_step_warning(
                    ctx, run_id=run_id,
                    stage=run.current_stage or "control",
                    step=run.current_step or action,
                    message=f"operator {action}",
                    actor=actor,
                )
            except Exception:  # noqa: BLE001 — observability never blocks
                _log.exception(
                    "%s control event emission failed", action,
                )
        signal_workflow_id = run.workflow_id or run_id
        return IngestionRunControlRecord(
            run_id=run_id,
            action=action,
            status=run.status.value,
            stage=run.current_stage,
            message=f"{action.capitalize()} requested.",
            updated_at=now.isoformat(),
        ), signal_workflow_id

    @app.post(
        "/ingestion-runs/{run_id}/pause",
        tags=["ingestion-runs"],
        summary="Pause a running ingestion run",
        description=(
            "Operator-driven pause. Flips the run record to PAUSED so "
            "polling clients see the action immediately, then forwards "
            "the `pause` signal to the workflow which will stop at the "
            "next pause-checkpoint."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def post_ingestion_run_pause(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
        control=Depends(require_job_control),
    ) -> dict[str, Any]:
        record, signal_id = _control_action(
            action="pause",
            target_status=RunStatus.PAUSED,
            allowed_from=(RunStatus.RUNNING, RunStatus.ASSESSING),
            signal_name="pause",
            request=request, run_id=run_id, ctx=ctx, security=security,
        )
        signal_delivered = True
        signal_error: str | None = None
        try:
            await control.pause_job(ctx, signal_id)
        except Exception as exc:  # noqa: BLE001
            signal_delivered = False
            signal_error = f"{type(exc).__name__}: {exc}"
            _log.exception(
                "pause signal failed for run_id=%s; run record is "
                "PAUSED but workflow may not be paused yet", run_id,
            )
        return envelope(
            record.model_dump(by_alias=True),
            _req_id(request),
            meta=_signal_meta(signal_delivered, signal_error),
        )

    @app.post(
        "/ingestion-runs/{run_id}/resume",
        tags=["ingestion-runs"],
        summary="Resume a paused ingestion run",
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def post_ingestion_run_resume(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
        control=Depends(require_job_control),
    ) -> dict[str, Any]:
        record, signal_id = _control_action(
            action="resume",
            target_status=RunStatus.RUNNING,
            allowed_from=(RunStatus.PAUSED,),
            signal_name="resume",
            request=request, run_id=run_id, ctx=ctx, security=security,
        )
        signal_delivered = True
        signal_error: str | None = None
        try:
            await control.resume_job(ctx, signal_id)
        except Exception as exc:  # noqa: BLE001
            signal_delivered = False
            signal_error = f"{type(exc).__name__}: {exc}"
            _log.exception(
                "resume signal failed for run_id=%s; run record is "
                "RUNNING but workflow may not have resumed", run_id,
            )
        return envelope(
            record.model_dump(by_alias=True),
            _req_id(request),
            meta=_signal_meta(signal_delivered, signal_error),
        )

    @app.post(
        "/ingestion-runs/{run_id}/cancel",
        tags=["ingestion-runs"],
        summary="Cancel a running ingestion run",
        description=(
            "Flips the run record to CANCELLING and forwards the "
            "`cancel` signal. The workflow will land at CANCELLED "
            "once any in-flight activity finishes."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def post_ingestion_run_cancel(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
        control=Depends(require_job_control),
    ) -> dict[str, Any]:
        record, signal_id = _control_action(
            action="cancel",
            target_status=RunStatus.CANCELLING,
            allowed_from=(
                RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.ASSESSING,
                RunStatus.PLAN_READY, RunStatus.WAITING_FOR_CONFIRMATION,
            ),
            signal_name="cancel",
            request=request, run_id=run_id, ctx=ctx, security=security,
        )
        signal_delivered = True
        signal_error: str | None = None
        try:
            await control.cancel_job(ctx, signal_id)
        except Exception as exc:  # noqa: BLE001
            signal_delivered = False
            signal_error = f"{type(exc).__name__}: {exc}"
            _log.exception(
                "cancel signal failed for run_id=%s; run record is "
                "CANCELLING but workflow signal may not have arrived", run_id,
            )
        return envelope(
            record.model_dump(by_alias=True),
            _req_id(request),
            meta=_signal_meta(signal_delivered, signal_error),
        )

    @app.get(
        "/ingestion-runs/{run_id}/events",
        tags=["ingestion-runs"],
        summary="Get historical progress events for a run",
        description=(
            "Returns the structured progress timeline for a run, "
            "ordered by timestamp. Backed by the same audit log used "
            "by `GET /ingestion-jobs/{id}/events`, but filtered to "
            "`j1.progress.*` actions and reshaped into the frontend's "
            "ProgressEvent schema."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_ingestion_run_events(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        if workspace is None:
            raise HTTPException(503, "audit-event lookup not configured")
        events = _read_progress_events(workspace, ctx, run_id)
        record = ProgressEventsRecord(run_id=run_id, events=events)
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    @app.get(
        "/ingestion-runs/{run_id}/events/stream",
        tags=["ingestion-runs"],
        summary="Stream live progress events (Server-Sent Events)",
        description=(
            "Streams structured progress events as Server-Sent Events. "
            "Each event carries the same shape as `GET .../events`. "
            "Send `Last-Event-Id: <evt_id>` to resume from a known "
            "position; the server tails the audit log starting after "
            "that ID. The stream closes when the run reaches a "
            "terminal state (succeeded / failed / cancelled)."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_ingestion_run_events_stream(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> StreamingResponse:
        if workspace is None:
            raise HTTPException(503, "audit-event lookup not configured")
        last_event_id = request.headers.get("Last-Event-Id") or None
        return StreamingResponse(
            _stream_progress_events(workspace, ctx, run_id, last_event_id),
            media_type=SSE_CONTENT_TYPE,
            headers=SSE_HEADERS,
        )

    @app.post(
        "/ingestion-runs",
        status_code=201,
        tags=["ingestion-runs"],
        summary="Upload a document and start an ingestion run",
        description=(
            "Composite entry point for the user-facing execution console: "
            "registers the document, allocates a run record, emits the "
            "`run.created` and `document.received` progress events, and "
            "starts the workflow in a single call. Returns the run "
            "identifier the frontend uses to poll status / fetch the "
            "execution plan / subscribe to the SSE stream."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def post_ingestion_run(
        request: Request,
        file: UploadFile = File(...),
        actor: str = Form("system"),
        correlation_id: str | None = Form(default=None),
        compiler_kind: str | None = Form(default=None, alias="compilerKind"),
        enricher_kind: str | None = Form(default=None, alias="enricherKind"),
        graph_builder_kind: str | None = Form(
            default=None, alias="graphBuilderKind",
        ),
        indexer_kind: str | None = Form(default=None, alias="indexerKind"),
        policy: str | None = Form(default=None),
        # User-selected execution profile from the FE's two-step
        # picker. None falls back to the deployment's safe default
        # (see `j1.processing.execution_profile.DEFAULT_PROFILE`).
        # When supplied, becomes the authoritative gate — overrides
        # caller-supplied processor kinds AND the post-compile enrich
        # plan when it disables a stage. Profile wire strings:
        # `minimum_queryable` / `standard` / `advanced`. Unknown
        # values are rejected at the boundary with 400 so a typo
        # fails fast.
        selected_profile: str | None = Form(default=None, alias="selectedProfile"),
        # Assessment-decision id minted by
        # ``POST /documents/{id}/assessment-plan``. When supplied,
        # the REST adapter looks it up, validates it against the
        # current document, and stamps the full payload onto the
        # run's metadata so the workflow consumes the same
        # recommendation the FE picker showed. A missing /
        # mismatched / unsupported decision degrades to the
        # workflow's rebuild path (with a stamped warning).
        assessment_decision_id: str | None = Form(
            default=None, alias="assessmentDecisionId",
        ),
        ctx: ProjectContext = Depends(get_ctx),
        starter: JobStarter = Depends(require_job_starter),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        # 0. Resolve the selected profile against the deployment
        # policy. Two failure modes, distinct HTTP codes:
        #
        #   * Unknown wire string (typo) → 400 INVALID_ARGUMENT
        #   * Known profile, but not on the deployment allow-list
        #     (e.g. `advanced` requested when
        #     `J1_ALLOW_ADVANCED_INGEST=false`) → 403 PROFILE_NOT_ALLOWED
        #
        # `_resolve_profile_or_400` returns the resolved
        # `(profile, source)` pair OR raises one of the two
        # HTTPExceptions. When the caller omitted `selectedProfile`,
        # the policy's default kicks in and `source == "default"`.
        # Runs BEFORE the run-store check so request-shape errors
        # fail fast regardless of deployment wiring.
        resolved_profile, profile_source = _resolve_profile_or_400(
            selected_profile,
        )

        store = _require_run_store()

        # 1. Register the document (existing service; idempotent on
        # checksum). We reuse the same DTO + duplicate handling as
        # `POST /documents` so the frontend can re-upload safely.
        try:
            doc_dto = facade.ingestion.register_document(
                ctx,
                file.file,
                original_filename=file.filename or "upload.bin",
                mime_type=file.content_type,
                actor=actor,
                correlation_id=correlation_id,
            )
            duplicate = False
        except DuplicateDocumentError as exc:
            doc_dto = facade.source_lookup.get_source(
                ctx, exc.existing_document_id,
            )
            duplicate = True

        # 2. Validate / resolve processor kinds at the boundary so a
        # typo fails fast rather than as a workflow failure 5s later.
        # The optional kinds use `_resolve_optional_processor_kind`
        # (NOT `_validate_*`): the FE upload omits all kinds, so
        # without auto-defaulting from the deployment registry the
        # workflow's `available_steps` collapses to `{compile}` and
        # every other stage gets silently skipped. Auto-pick when the
        # deployment has exactly one registered kind for the role; on
        # ambiguity (multiple registered) leave None for the operator
        # to choose explicitly.
        resolved_compiler = _resolve_compiler_kind(compiler_kind)
        resolved_enricher = _resolve_optional_processor_kind(
            enricher_kind,
            (processing_capabilities.enricher_kinds
             if processing_capabilities else frozenset()),
            "enricherKind",
        )
        resolved_graph = _resolve_optional_processor_kind(
            graph_builder_kind,
            (processing_capabilities.graph_builder_kinds
             if processing_capabilities else frozenset()),
            "graphBuilderKind",
        )
        resolved_indexer = _resolve_optional_processor_kind(
            indexer_kind,
            (processing_capabilities.indexer_kinds
             if processing_capabilities else frozenset()),
            "indexerKind",
        )

        # 3. Allocate run_id. By convention `run_id == correlation_id ==
        # workflow_id` so the audit log, SSE cursor, and Temporal
        # search-attributes share one identifier — the frontend never
        # has to map between them.
        run_id = correlation_id or uuid.uuid4().hex

        # 3b. Look up + validate the assessment decision (if any).
        # The validated decision is stamped onto run metadata so the
        # workflow consumes the same recommendation the FE picker
        # showed. Validation failure does NOT 4xx — we degrade to
        # the workflow's rebuild path with a stamped warning, so
        # the user's upload still completes.
        (
            assessment_decision_metadata,
            assessment_decision_warning,
        ) = _load_and_validate_assessment_decision(
            ctx=ctx,
            decision_id=assessment_decision_id,
            document_id=doc_dto.document_id,
            file_hash=getattr(doc_dto, "checksum", None),
        )

        # 4. Persist initial run record with status=CREATED. Subsequent
        # writes (status transitions) append fresh snapshots; the
        # latest one wins on read.
        now = datetime.now(timezone.utc)
        run_metadata: dict[str, Any] = {
            "duplicate_upload": duplicate,
            "policy": policy or "auto",
            # `mode` is the human-readable label the FE shows
            # alongside `policy` (e.g., STANDARD / FAST / THOROUGH).
            # The upload form doesn't yet collect a value, so we
            # default to STANDARD — when a mode selector lands in
            # the UI thread it through here.
            "mode": "STANDARD",
            # Persisted so `GET /ingestion-runs` can render the
            # uploaded filename without joining on the documents
            # store. `original_filename` falls back to the multi-
            # part `filename` parameter, which the FE always sends.
            "document_name": doc_dto.original_filename,
            # Execution profile audit trail. The profile here is
            # the deployment-policy-resolved final value — caller's
            # request when allowed, deployment default when the
            # caller omitted `selectedProfile`. Refused requests
            # never reach this point (they raise 403 above).
            # Wire-string values match
            # `j1.processing.execution_profile.ExecutionProfile`.
            "selected_execution_profile": resolved_profile.value,
            "profile_selected_by": (
                "user" if profile_source == "rest" else "system"
            ),
            "profile_selection_source": profile_source,
        }
        if assessment_decision_metadata is not None:
            run_metadata["assessment_decision_id"] = (
                assessment_decision_metadata["assessmentDecisionId"]
            )
            run_metadata["assessment_decision"] = (
                assessment_decision_metadata
            )
            # Final-report indicator. ``persisted`` only fires when
            # the REST adapter validated the decision AND it carries
            # a usable ``lightweightAssessment``. Anything else falls
            # through to ``rebuilt_fallback`` (caller passed an id
            # that didn't validate, or no id at all).
            if assessment_decision_metadata.get("lightweightAssessment"):
                run_metadata["assessment_decision_source"] = "persisted"
            else:
                run_metadata["assessment_decision_source"] = (
                    "rebuilt_fallback"
                )
        elif assessment_decision_id:
            # Caller passed an id but validation failed. The workflow
            # will rebuild — record the expected source so the final
            # report reflects what happened.
            run_metadata["assessment_decision_source"] = "rebuilt_fallback"
        if assessment_decision_warning is not None:
            # Stamp the validation warning so the final report can
            # surface it even when the decision was unusable.
            run_metadata.setdefault(
                "assessment_decision_warnings", [],
            ).append(assessment_decision_warning)
        run = IngestionRun(
            run_id=run_id,
            document_id=doc_dto.document_id,
            workflow_id=run_id,
            workflow_run_id=None,
            status=RunStatus.CREATED,
            started_at=now,
            updated_at=now,
            metadata=run_metadata,
            # Phase 5: initial-ingest path also allocates the
            # candidate snapshot UP-FRONT so the workflow sees
            # ``target_snapshot_id`` on its first activity.
            target_snapshot_id=_allocate_target_snapshot(
                ctx, doc_dto.document_id, run_id,
            ),
        )
        store.upsert(ctx, run)

        # 5. Emit progress events (no-op when no reporter is wired).
        if progress_reporter is not None:
            progress_reporter.report_run_created(
                ctx, run_id=run_id, document_id=doc_dto.document_id,
                actor=actor,
            )
            progress_reporter.report_document_received(
                ctx, run_id=run_id, document_id=doc_dto.document_id,
                actor=actor,
            )

        # 6. Start the workflow. Use the existing JobStarter contract
        # so this endpoint stays compatible with deployments that
        # have already wired job control.
        body = IngestRequest(
            compiler_kind=resolved_compiler,
            enricher_kind=resolved_enricher,
            graph_builder_kind=resolved_graph,
            indexer_kind=resolved_indexer,
            actor=actor,
            correlation_id=run_id,
            target_snapshot_id=run.target_snapshot_id,
            # Profile threaded through to the workflow so every
            # `_stage_enabled` check and the compile activity's
            # `disable_entity_extraction` plumbing see the same
            # authoritative value. Always the policy-resolved
            # value — even when the caller omitted `selectedProfile`,
            # we explicitly thread the deployment default so the
            # workflow's audit log captures what actually ran
            # instead of leaving the field None.
            selected_profile=resolved_profile.value,
            # Pass the validated decision id + full payload so the
            # workflow can short-circuit its rebuild path. When None,
            # the workflow falls back to its existing assessment-plan
            # activity and stamps the source as ``rebuilt_fallback``.
            assessment_decision_id=(
                assessment_decision_metadata["assessmentDecisionId"]
                if assessment_decision_metadata is not None else None
            ),
            assessment_decision_payload=(
                dict(assessment_decision_metadata)
                if assessment_decision_metadata is not None else None
            ),
            assessment_decision_warnings=(
                (assessment_decision_warning,)
                if assessment_decision_warning is not None else ()
            ),
        )
        workflow_id = await starter(ctx, doc_dto.document_id, body)

        # 7. Update the run record with the workflow_id (which by
        # convention equals run_id, but starters that allocate their
        # own ID can override).
        run.workflow_id = workflow_id
        run.updated_at = datetime.now(timezone.utc)
        store.upsert(ctx, run)

        # 8. Existing event publisher (webhooks / event bus).
        publish_document_uploaded(
            event_bus, security=security, request_id=_req_id(request),
            tenant_id=ctx.tenant_id, document_id=doc_dto.document_id,
            checksum=doc_dto.checksum, file_size=doc_dto.file_size,
            mime_type=doc_dto.mime_type, duplicate=duplicate,
        )
        publish_document_ingestion_started(
            event_bus, security=security, request_id=_req_id(request),
            tenant_id=ctx.tenant_id, job_id=workflow_id,
            document_id=doc_dto.document_id, project_wide=False,
        )

        record = IngestionRunCreatedRecord(
            run_id=run_id,
            document_id=doc_dto.document_id,
            workflow_id=workflow_id,
            workflow_run_id=None,
            status=RunStatus.CREATED.value,
        )
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    # ---- Multi-upload batches ---------------------------------------

    INGESTION_BATCH_MAX_FILES = int(
        os.environ.get("J1_INGESTION_BATCH_MAX_FILES", "5"),
    )

    @app.post(
        "/ingestion-batches",
        status_code=201,
        tags=["ingestion-batches"],
        summary="Upload up to N documents as a batch",
        description=(
            "Multi-upload entry point. Accepts up to "
            "`J1_INGESTION_BATCH_MAX_FILES` (default 5) files, "
            "registers each as its own document + ingestion run, "
            "and returns a `batchRunId` operators poll via "
            "`GET /ingestion-batches/{id}`. Each child run executes "
            "as its own per-document workflow; the existing per-doc "
            "workflow_id derivation gives sequential execution per "
            "document (a re-upload of the same checksum still "
            "USE_EXISTING-attaches as today). The batch view "
            "aggregates child statuses at read-time."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def post_ingestion_batch(
        request: Request,
        files: list[UploadFile] = File(...),
        actor: str = Form("system"),
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        from datetime import datetime, timezone
        if batch_run_store is None:
            raise HTTPException(
                503,
                "batch run store not configured (pass `batch_run_store=` "
                "to create_rest_api)",
            )
        if batch_starter is None:
            raise HTTPException(
                503,
                "batch starter not configured (pass `batch_starter=` "
                "to create_rest_api so the parent workflow can dispatch "
                "child workflows sequentially)",
            )
        if not files:
            raise HTTPException(400, "at least one file is required")
        if len(files) > INGESTION_BATCH_MAX_FILES:
            raise HTTPException(
                400,
                f"batch upload supports up to {INGESTION_BATCH_MAX_FILES} "
                f"files; got {len(files)}",
            )
        store = _require_run_store()
        batch_run_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        child_run_ids: list[str] = []
        # Resolve processor kinds ONCE for the whole batch. Every
        # child run inherits the same recipe; per-file overrides
        # aren't a v1 affordance (operators who need them should
        # upload one file at a time via `POST /ingestion-runs`).
        compiler_kind = _resolve_compiler_kind(None)
        enricher_kind = _resolve_optional_processor_kind(
            None,
            (processing_capabilities.enricher_kinds
             if processing_capabilities else frozenset()),
            "enricherKind",
        )
        graph_builder_kind = _resolve_optional_processor_kind(
            None,
            (processing_capabilities.graph_builder_kinds
             if processing_capabilities else frozenset()),
            "graphBuilderKind",
        )
        indexer_kind = _resolve_optional_processor_kind(
            None,
            (processing_capabilities.indexer_kinds
             if processing_capabilities else frozenset()),
            "indexerKind",
        )

        # Register each file + create the run record. We DON'T
        # dispatch child workflows here; we hand the specs to the
        # parent workflow which dispatches them sequentially via
        # `execute_child_workflow`. The deterministic per-document
        # workflow_id stays the same as the single-file path so
        # readers (Temporal UI, debug commands) see a uniform shape.
        child_specs: list[dict[str, Any]] = []
        for upload in files:
            try:
                doc_dto = facade.ingestion.register_document(
                    ctx,
                    upload.file,
                    original_filename=upload.filename or "upload.bin",
                    mime_type=upload.content_type,
                    actor=actor,
                    correlation_id=batch_run_id,
                )
                duplicate = False
            except DuplicateDocumentError as exc:
                doc_dto = facade.source_lookup.get_source(
                    ctx, exc.existing_document_id,
                )
                duplicate = True

            child_run_id = uuid.uuid4().hex
            # Per-document workflow_id is deterministic — same shape
            # the single-upload starter would have used so a
            # USE_EXISTING re-attach behaves identically.
            child_workflow_id = (
                f"j1-{ctx.tenant_id}-{ctx.project_id}-{doc_dto.document_id}"
            )
            child_run = IngestionRun(
                run_id=child_run_id,
                document_id=doc_dto.document_id,
                workflow_id=child_workflow_id,
                workflow_run_id=None,
                status=RunStatus.CREATED,
                started_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                metadata={
                    "duplicate_upload": duplicate,
                    "policy": "auto",
                    "mode": "STANDARD",
                    "document_name": doc_dto.original_filename,
                    "batch_run_id": batch_run_id,
                },
                # Phase 5: batch-child runs also get up-front snapshot.
                target_snapshot_id=_allocate_target_snapshot(
                    ctx, doc_dto.document_id, child_run_id,
                ),
            )
            store.upsert(ctx, child_run)
            if progress_reporter is not None:
                progress_reporter.report_run_created(
                    ctx, run_id=child_run_id,
                    document_id=doc_dto.document_id, actor=actor,
                )

            child_specs.append({
                "workflow_id": child_workflow_id,
                "document_id": doc_dto.document_id,
                "correlation_id": child_run_id,
                "compiler_kind": compiler_kind,
                "enricher_kind": enricher_kind,
                "graph_builder_kind": graph_builder_kind,
                "indexer_kind": indexer_kind,
                "actor": actor,
                "target_snapshot_id": child_run.target_snapshot_id,
            })
            child_run_ids.append(child_run_id)

        # Dispatch the parent workflow. It will fan out children
        # sequentially via `execute_child_workflow` — workflow IDs +
        # request fields are passed on the spec list. This replaces
        # the previous "fan out from REST" pattern that relied on
        # `J1_WORKER_MAX_CONCURRENT_ACTIVITIES=1` to serialize.
        parent_workflow_id = await batch_starter(
            ctx, batch_run_id, child_specs,
        )

        # Persist the batch record. Status is derived at read-time
        # from child runs; we never persist a stale aggregate here.
        from j1.runs.batch_store import BatchRun
        batch = BatchRun(
            batch_run_id=batch_run_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            run_ids=child_run_ids,
            file_count=len(child_run_ids),
            started_at=now,
            actor=actor,
            metadata={"parent_workflow_id": parent_workflow_id},
        )
        batch_run_store.upsert(ctx, batch)  # type: ignore[union-attr]
        _emit_ops_event(
            ctx,
            action=ACTION_OPS_BATCH_DISPATCHED,
            target_id=batch_run_id,
            target_kind=TARGET_KIND_INGESTION_BATCH,
            actor=actor,
            correlation_id=batch_run_id,
            payload={
                "file_count": len(child_run_ids),
                "run_ids": child_run_ids,
                "parent_workflow_id": parent_workflow_id,
            },
        )
        return envelope(
            {
                "batchRunId": batch_run_id,
                "fileCount": len(child_run_ids),
                "runIds": child_run_ids,
                "status": "running",
                "startedAt": now.isoformat(),
                "parentWorkflowId": parent_workflow_id,
            },
            _req_id(request),
        )

    @app.get(
        "/ingestion-batches/{batch_run_id}",
        tags=["ingestion-batches"],
        summary="Get batch status + per-file run details",
        description=(
            "Aggregate view: batch-level status (derived from child "
            "runs), per-file `{runId, documentId, filename, status, "
            "currentStage}` rows. The FE batch table reads this on a "
            "polling cadence."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_ingestion_batch(
        request: Request,
        batch_run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        if batch_run_store is None:
            raise HTTPException(503, "batch run store not configured")
        batch = batch_run_store.get(ctx, batch_run_id)  # type: ignore[union-attr]
        if batch is None:
            raise HTTPException(
                404, f"batch run {batch_run_id!r} not found"
            )
        store = _require_run_store()
        from j1.runs.batch_store import derive_batch_status
        runs_payload = []
        child_statuses: list[str] = []
        completed_count = 0
        failed_count = 0
        current_run_id: str | None = None
        active_states = {
            "created", "assessing", "plan_ready", "running", "paused",
            "cancelling", "waiting_for_confirmation",
        }
        for child_id in batch.run_ids:
            child = store.get(ctx, child_id)
            if child is None:
                continue
            child_status = str(child.status)
            child_statuses.append(child_status)
            if child_status in active_states and current_run_id is None:
                current_run_id = child_id
            if child_status in {"succeeded", "succeeded_with_warnings"}:
                completed_count += 1
            elif child_status in {"failed", "cancelled"}:
                failed_count += 1
            runs_payload.append({
                "runId": child.run_id,
                "documentId": child.document_id,
                "filename": (child.metadata or {}).get("document_name"),
                "status": child_status,
                "currentStage": child.current_stage,
                "currentStep": child.current_step,
                "progressPercent": child.progress_percent,
            })
        return envelope(
            {
                "batchRunId": batch.batch_run_id,
                "status": derive_batch_status(child_statuses),
                "startedAt": batch.started_at.isoformat(),
                "fileCount": batch.file_count,
                "completedCount": completed_count,
                "failedCount": failed_count,
                "currentRunId": current_run_id,
                "runs": runs_payload,
            },
            _req_id(request),
        )

    @app.post(
        "/ingestion-jobs",
        tags=["ingestion-jobs"],
        summary="Start a project-wide ingestion job",
        description=(
            "Starts a `ProjectProcessingWorkflow` covering every pending "
            "document in the project. Returns the assigned `jobId` — poll "
            "`/ingestion-jobs/{jobId}` for status."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def post_ingestion_job(
        request: Request,
        body: ProjectIngestionRequest,
        ctx: ProjectContext = Depends(get_ctx),
        control=Depends(require_job_control),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        # Resolve / validate processor kinds at the API boundary so a
        # bad value surfaces as a 400 here instead of a workflow
        # failure 5s later.
        compiler_kind = _resolve_compiler_kind(body.compiler_kind)
        graph_builder_kind = _validate_optional_processor_kind(
            body.graph_builder_kind,
            (processing_capabilities.graph_builder_kinds
             if processing_capabilities else frozenset()),
            "graphBuilderKind",
        )
        enricher_kind = _validate_optional_processor_kind(
            body.enricher_kind,
            (processing_capabilities.enricher_kinds
             if processing_capabilities else frozenset()),
            "enricherKind",
        )
        indexer_kind = _validate_optional_processor_kind(
            body.indexer_kind,
            (processing_capabilities.indexer_kinds
             if processing_capabilities else frozenset()),
            "indexerKind",
        )
        result = await control.start_project_job(
            ctx,
            ProjectIngestionRequestDTO(
                compiler_kind=compiler_kind,
                enricher_kind=enricher_kind,
                graph_builder_kind=graph_builder_kind,
                indexer_kind=indexer_kind,
                budget_limit_amount=body.budget_limit_amount,
                budget_currency=body.budget_currency,
                review_after=list(body.review_after),
                actor=body.actor,
                correlation_id=body.correlation_id,
            ),
        )
        publish_document_ingestion_started(
            event_bus, security=security, request_id=_req_id(request),
            tenant_id=ctx.tenant_id, job_id=result.job_id,
            document_id=None, project_wide=True,
        )
        record = JobActionRecord(job_id=result.job_id, action=result.action)
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    @app.post(
        "/ingestion-jobs/{job_id}/pause",
        tags=["ingestion-jobs"],
        summary="Pause a running ingestion job",
        dependencies=[Depends(scope_required(SCOPE_ADMIN))],
    )
    async def pause_ingestion_job(
        request: Request,
        job_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        control=Depends(require_job_control),
    ) -> dict[str, Any]:
        result = await control.pause_job(ctx, job_id)
        record = JobActionRecord(job_id=result.job_id, action=result.action)
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    @app.post(
        "/ingestion-jobs/{job_id}/resume",
        tags=["ingestion-jobs"],
        summary="Resume a paused ingestion job",
        dependencies=[Depends(scope_required(SCOPE_ADMIN))],
    )
    async def resume_ingestion_job(
        request: Request,
        job_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        control=Depends(require_job_control),
    ) -> dict[str, Any]:
        result = await control.resume_job(ctx, job_id)
        record = JobActionRecord(job_id=result.job_id, action=result.action)
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    @app.post(
        "/ingestion-jobs/{job_id}/cancel",
        tags=["ingestion-jobs"],
        summary="Cancel an ingestion job",
        dependencies=[Depends(scope_required(SCOPE_ADMIN))],
    )
    async def cancel_ingestion_job(
        request: Request,
        job_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        control=Depends(require_job_control),
    ) -> dict[str, Any]:
        result = await control.cancel_job(ctx, job_id)
        record = JobActionRecord(job_id=result.job_id, action=result.action)
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    # ---- Artifacts ---------------------------------------------------

    @app.get(
        "/artifacts",
        tags=["artifacts"],
        summary="List artifacts in a project",
        dependencies=[Depends(scope_required(SCOPE_READ))],
    )
    def list_artifacts(
        request: Request,
        kind: str | None = Query(default=None),
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        records = facade.retrieval.list_artifacts(ctx, kind=kind)
        record = ArtifactListRecord(
            artifacts=[_artifact_record(r) for r in records]
        )
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    @app.get(
        "/artifacts/{artifact_id}",
        tags=["artifacts"],
        summary="Get a single artifact",
        dependencies=[Depends(scope_required(SCOPE_READ))],
    )
    def get_artifact(
        request: Request,
        artifact_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        dto = facade.retrieval.get_artifact(ctx, artifact_id)
        record = _artifact_record(dto)
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    # ---- Search / retrieve / answer ---------------------------------

    @app.post(
        "/search",
        tags=["search"],
        summary="Keyword search over indexed artifacts",
        description=(
            "Returns ranked search hits. Use `/retrieve` to get context "
            "blocks suitable for grounding an LLM, or `/answer` to get a "
            "generated answer with citations."
        ),
        dependencies=[Depends(scope_required(SCOPE_SEARCH))],
    )
    def post_search(
        request: Request,
        body: SearchRequest,
        ctx: ProjectContext = Depends(get_ctx),
        search_port=Depends(require_search),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        hits = search_port.search(
            ctx,
            body.query,
            artifact_types=list(body.artifact_types) or None,
            max_results=body.max_results,
        )
        publish_query_completed(
            event_bus, security=security, request_id=_req_id(request),
            tenant_id=ctx.tenant_id, query=body.query,
            result_count=len(hits), surface="search",
        )
        record = SearchResultRecord(
            query=body.query,
            hits=[
                SearchHitRecord(
                    artifact_id=h.artifact_id,
                    artifact_type=h.artifact_type,
                    title=h.title,
                    score=h.score,
                    source_document_id=h.source_document_id,
                    source_location=h.source_location,
                    confidence=h.confidence,
                    review_status=h.review_status,
                    # Phase 5: snapshot lineage on every hit.
                    snapshot_id=h.snapshot_id,
                    chunk_id=h.chunk_id,
                    created_by_run_id=h.run_id,
                    extracted_text=h.extracted_text,
                )
                for h in hits
            ],
        )
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    @app.post(
        "/retrieve",
        tags=["retrieve"],
        summary="Retrieve context blocks with citations",
        description=(
            "Returns ranked text blocks with their source citations — the "
            "shape an answer-generation pipeline expects to consume."
        ),
        dependencies=[Depends(scope_required(SCOPE_RETRIEVE))],
    )
    def post_retrieve(
        request: Request,
        body: RetrieveRequest,
        ctx: ProjectContext = Depends(get_ctx),
        search_port=Depends(require_search),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        hits = search_port.search(
            ctx,
            body.query,
            artifact_types=list(body.artifact_types) or None,
            max_results=body.max_blocks,
        )
        publish_query_completed(
            event_bus, security=security, request_id=_req_id(request),
            tenant_id=ctx.tenant_id, query=body.query,
            result_count=len(hits), surface="retrieve",
        )
        # Hits already carry the indexed text — that's the block body.
        blocks: list[ContextBlockRecord] = [
            ContextBlockRecord(
                artifact_id=h.artifact_id,
                artifact_type=h.artifact_type,
                text=h.extracted_text,
                citation=CitationRecord(
                    artifact_id=h.artifact_id,
                    artifact_type=h.artifact_type,
                    source_document_id=h.source_document_id,
                    source_location=h.source_location,
                    chunk_id=h.chunk_id,
                    run_id=h.run_id,
                ),
            )
            for h in hits
        ]
        record = RetrieveResultRecord(query=body.query, blocks=blocks)
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    # ---- Citations / sources ----------------------------------------

    @app.get(
        "/citations/{citation_id}",
        tags=["citations"],
        summary="Look up a citation by artifact ID",
        description=(
            "`citationId` is interpreted as the underlying `artifactId` — "
            "the citation surface is a thin view over an artifact's lineage."
        ),
        dependencies=[Depends(scope_required(SCOPE_READ))],
    )
    def get_citation(
        request: Request,
        citation_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        citations = facade.citation_lookup.get_citations(ctx, citation_id)
        # Return the first (representative) citation; details has the full set.
        primary = citations[0]
        record = CitationDetailRecord(
            citation_id=citation_id,
            artifact_id=primary.artifact_id,
            artifact_type=primary.artifact_type,
            source_document_id=primary.source_document_id,
            source_location=primary.source_location,
            metadata={"citation_count": len(citations)},
        )
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    @app.get(
        "/sources/{source_id}",
        tags=["sources"],
        summary="Look up a source document",
        dependencies=[Depends(scope_required(SCOPE_READ))],
    )
    def get_source(
        request: Request,
        source_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        dto = facade.source_lookup.get_source(ctx, source_id)
        record = SourceDetailRecord(
            source_id=source_id,
            document_id=dto.document_id,
            tenant_id=dto.tenant_id,
            project_id=dto.project_id,
            original_filename=dto.original_filename,
            mime_type=dto.mime_type,
            file_size=dto.file_size,
            checksum=dto.checksum,
            status=dto.status,
            created_at=dto.created_at,
        )
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    # ---- Feedback ---------------------------------------------------

    @app.post(
        "/feedback",
        tags=["feedback"],
        summary="Submit user feedback",
        dependencies=[Depends(scope_required(SCOPE_FEEDBACK))],
    )
    def post_feedback(
        request: Request,
        body: FeedbackRequest,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        # If the caller didn't supply an actor, attribute the feedback to the
        # authenticated subject — keeps audit logs honest without leaking
        # raw auth headers into the application services.
        actor = body.actor or (
            security.subject if not security.is_anonymous else None
        )
        result = facade.feedback.submit_feedback(
            ctx,
            FeedbackDTO(
                target_kind=body.target_kind,
                target_id=body.target_id,
                rating=body.rating,
                comment=body.comment,
                actor=actor,
                correlation_id=body.correlation_id,
                metadata=dict(body.metadata),
            ),
        )
        record = FeedbackReceiptRecord(
            feedback_id=result.feedback_id,
            submitted_at=result.submitted_at,
        )
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    # ---- Cost --------------------------------------------------------

    @app.get(
        "/cost",
        tags=["cost"],
        summary="Get aggregated project spend",
        description=(
            "Optionally scope the aggregation by `correlationId`, "
            "`documentId`, or `queryId` query parameters."
        ),
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def get_cost(
        request: Request,
        correlation_id: str | None = Query(default=None, alias="correlationId"),
        document_id: str | None = Query(default=None, alias="documentId"),
        query_id: str | None = Query(default=None, alias="queryId"),
        ctx: ProjectContext = Depends(get_ctx),
        cost=Depends(require_cost_summary),
    ) -> dict[str, Any]:
        dto = cost.get_cost_summary(
            ctx,
            correlation_id=correlation_id,
            document_id=document_id,
            query_id=query_id,
        )
        record = CostSummaryRecord(
            project_id=dto.project_id,
            tenant_id=dto.tenant_id,
            total_amount=dto.total_amount,
            currency=dto.currency,
            by_level=dict(dto.by_level),
        )
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    # ---- Reviews -----------------------------------------------------

    @app.get(
        "/reviews",
        tags=["reviews"],
        summary="List review queue items",
        dependencies=[Depends(scope_required(SCOPE_READ))],
    )
    def list_reviews(
        request: Request,
        pending_only: bool = Query(default=True, alias="pendingOnly"),
        ctx: ProjectContext = Depends(get_ctx),
        review=Depends(require_review),
    ) -> dict[str, Any]:
        items = review.list_reviews(ctx, pending_only=pending_only)
        record = ReviewListRecord(items=[_review_record(i) for i in items])
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    @app.post(
        "/reviews/{review_id}/decision",
        tags=["reviews"],
        summary="Apply a decision to a review item",
        dependencies=[Depends(scope_required(SCOPE_ADMIN))],
    )
    def post_review_decision(
        request: Request,
        review_id: str,
        body: ReviewDecisionRequest,
        ctx: ProjectContext = Depends(get_ctx),
        review=Depends(require_review),
    ) -> dict[str, Any]:
        result = review.apply_decision(
            ctx,
            review_id,
            ReviewDecisionRequestDTO(
                decision=body.decision,
                actor=body.actor,
                notes=body.notes,
                correlation_id=body.correlation_id,
            ),
        )
        record = ReviewDecisionRecord(
            review_item_id=result.review_item_id,
            review_status=result.review_status,
            audit_event_id=result.audit_event_id,
        )
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    # ---- Bulk export ------------------------------------------------

    NDJSON_CONTENT_TYPE = "application/x-ndjson"

    def _ndjson_response(byte_iter, *, scope_label: str):
        # Wrap the synchronous generator in a StreamingResponse so the
        # outbound bytes are flushed to the client as the registry yields
        # rows — important for large projects.
        return StreamingResponse(
            byte_iter,
            media_type=NDJSON_CONTENT_TYPE,
            headers={"Content-Disposition": f'attachment; filename="{scope_label}.ndjson"'},
        )

    @app.get(
        "/exports/documents.ndjson",
        tags=["bulk"],
        summary="Export every document as NDJSON",
        dependencies=[Depends(scope_required(SCOPE_READ))],
    )
    def export_documents(
        ctx: ProjectContext = Depends(get_ctx),
        svc: BulkExportService = Depends(require_bulk_export),
    ):
        return _ndjson_response(svc.export_documents(ctx), scope_label="documents")

    @app.get(
        "/exports/sources.ndjson",
        tags=["bulk"],
        summary="Export every source as NDJSON (alias of /exports/documents.ndjson)",
        dependencies=[Depends(scope_required(SCOPE_READ))],
    )
    def export_sources(
        ctx: ProjectContext = Depends(get_ctx),
        svc: BulkExportService = Depends(require_bulk_export),
    ):
        return _ndjson_response(svc.export_sources(ctx), scope_label="sources")

    @app.get(
        "/exports/chunks.ndjson",
        tags=["bulk"],
        summary="Export every artifact (chunk) as NDJSON",
        dependencies=[Depends(scope_required(SCOPE_READ))],
    )
    def export_chunks(
        ctx: ProjectContext = Depends(get_ctx),
        svc: BulkExportService = Depends(require_bulk_export),
    ):
        return _ndjson_response(svc.export_artifacts(ctx), scope_label="chunks")

    @app.get(
        "/exports/citations.ndjson",
        tags=["bulk"],
        summary="Export every citation (artifact → source-document edge) as NDJSON",
        dependencies=[Depends(scope_required(SCOPE_READ))],
    )
    def export_citations(
        ctx: ProjectContext = Depends(get_ctx),
        svc: BulkExportService = Depends(require_bulk_export),
    ):
        return _ndjson_response(svc.export_citations(ctx), scope_label="citations")

    @app.get(
        "/exports/metadata.ndjson",
        tags=["bulk"],
        summary="Export per-document metadata as NDJSON",
        dependencies=[Depends(scope_required(SCOPE_READ))],
    )
    def export_metadata(
        ctx: ProjectContext = Depends(get_ctx),
        svc: BulkExportService = Depends(require_bulk_export),
    ):
        return _ndjson_response(svc.export_metadata(ctx), scope_label="metadata")

    @app.get(
        "/exports/feedback.ndjson",
        tags=["bulk"],
        summary="Export every feedback record as NDJSON",
        dependencies=[Depends(scope_required(SCOPE_AUDIT_READ))],
    )
    def export_feedback(
        ctx: ProjectContext = Depends(get_ctx),
        svc: BulkExportService = Depends(require_bulk_export),
    ):
        return _ndjson_response(svc.export_feedback(ctx), scope_label="feedback")

    # ---- Bulk import ------------------------------------------------

    @app.post(
        "/imports/documents.ndjson",
        tags=["bulk"],
        summary="Bulk-import documents (idempotent by checksum)",
        description=(
            "Body must be NDJSON whose lines match `DocumentExportRecord`. "
            "Existing documents (by checksum) are counted as "
            "`skippedIdempotent`; invalid lines are reported in `failures` "
            "and the rest of the file is still processed."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def import_documents(
        request: Request,
        ctx: ProjectContext = Depends(get_ctx),
        svc: BulkImportService = Depends(require_bulk_import),
    ) -> dict[str, Any]:
        body = await request.body()
        result = svc.import_documents(ctx, body.splitlines())
        return envelope(
            _bulk_result_record(result).model_dump(by_alias=True),
            _req_id(request),
        )

    @app.post(
        "/imports/sources.ndjson",
        tags=["bulk"],
        summary="Bulk-import sources (alias of /imports/documents.ndjson)",
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def import_sources(
        request: Request,
        ctx: ProjectContext = Depends(get_ctx),
        svc: BulkImportService = Depends(require_bulk_import),
    ) -> dict[str, Any]:
        body = await request.body()
        result = svc.import_sources(ctx, body.splitlines())
        return envelope(
            _bulk_result_record(result).model_dump(by_alias=True),
            _req_id(request),
        )

    @app.post(
        "/imports/metadata.ndjson",
        tags=["bulk"],
        summary="Verify document metadata (round-trip integrity check)",
        description=(
            "Each row must match an existing document and its declared "
            "fields must equal the registry's stored values. No state is "
            "mutated — failures are reported per-row in `failures`."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def import_metadata(
        request: Request,
        ctx: ProjectContext = Depends(get_ctx),
        svc: BulkImportService = Depends(require_bulk_import),
    ) -> dict[str, Any]:
        body = await request.body()
        result = svc.verify_metadata(ctx, body.splitlines())
        return envelope(
            _bulk_result_record(result).model_dump(by_alias=True),
            _req_id(request),
        )

    # ---- Health / version / capabilities ----------------------------

    @app.get("/health", tags=["system"], summary="Liveness check")
    def get_health(request: Request) -> dict[str, Any]:
        record = HealthRecord(status="ok")
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    @app.get(
        "/healthz/llm",
        tags=["system"],
        summary="LLM connectivity status",
        description=(
            "Returns the most recent LLM startup-probe results. The "
            "FE polls this to render an 'LLM unreachable' banner and "
            "disable uploads when any required role is down. Cached "
            "in-process; no upstream LLM call per request."
        ),
    )
    def get_health_llm(request: Request) -> dict[str, Any]:
        from j1.llm.probe import current_health
        snapshot = current_health()
        return envelope(
            {
                "healthy": snapshot.healthy,
                "checkedAt": snapshot.checked_at,
                "results": [
                    {
                        "role": r.role,
                        "ok": r.ok,
                        "provider": r.provider,
                        "model": r.model,
                        "error": r.error,
                    }
                    for r in snapshot.results
                ],
            },
            _req_id(request),
        )

    @app.post(
        "/healthz/llm/refresh",
        tags=["system"],
        summary="Re-probe LLM connectivity now",
        description=(
            "Runs the LLM connectivity probe synchronously (bounded "
            "by the configured per-call deadline, default 5s) and "
            "returns the fresh snapshot. The FE banner offers this "
            "as a 'Retry now' button so admins can verify the fix "
            "immediately after restarting LM Studio / vLLM, instead "
            "of waiting for the next 30s background tick."
        ),
    )
    def post_health_llm_refresh(request: Request) -> dict[str, Any]:
        from j1.llm.probe import (
            cache_probe_results,
            current_health,
            probe_registry,
        )
        if llm_registry is None:
            # No registry wired (mock / test deployments). Fall through
            # to the cached snapshot — still serves an honest answer
            # rather than 503-ing the FE retry button.
            snapshot = current_health()
        else:
            results = probe_registry(llm_registry)  # type: ignore[arg-type]
            cache_probe_results(results)
            snapshot = current_health()
        return envelope(
            {
                "healthy": snapshot.healthy,
                "checkedAt": snapshot.checked_at,
                "results": [
                    {
                        "role": r.role,
                        "ok": r.ok,
                        "provider": r.provider,
                        "model": r.model,
                        "error": r.error,
                    }
                    for r in snapshot.results
                ],
            },
            _req_id(request),
        )

    @app.get("/version", tags=["system"], summary="API version")
    def get_version(request: Request) -> dict[str, Any]:
        record = VersionRecord(version=version)
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    @app.get(
        "/capabilities",
        tags=["system"],
        summary="Available capabilities",
        description="Reports which optional ports the deployment has wired up.",
        dependencies=[Depends(scope_required(SCOPE_READ))],
    )
    def get_capabilities(request: Request) -> dict[str, Any]:
        caps = [
            CapabilityRecord(
                name="documents.upload",
                available=True,
                description="POST /documents",
            ),
            CapabilityRecord(
                name="documents.ingest",
                available=job_starter is not None,
                description="POST /documents/{id}/ingest",
            ),
            CapabilityRecord(
                name="search",
                available=facade.search is not None,
                description="POST /search",
            ),
            CapabilityRecord(
                name="job_status",
                available=facade.job_status is not None,
                description="GET /ingestion-jobs/{id}",
            ),
            CapabilityRecord(
                name="job_events",
                available=workspace is not None,
                description="GET /ingestion-jobs/{id}/events",
            ),
            CapabilityRecord(
                name="feedback", available=True, description="POST /feedback"
            ),
            CapabilityRecord(
                name="citations",
                available=True,
                description="GET /citations/{id}",
            ),
            CapabilityRecord(
                name="projects.create",
                available=facade.project_admin is not None,
                description="POST /projects",
            ),
            CapabilityRecord(
                name="ingestion-jobs.control",
                available=facade.job_control is not None,
                description="POST /ingestion-jobs[/{id}/{pause,resume,cancel}]",
            ),
            CapabilityRecord(
                name="artifacts",
                available=True,
                description="GET /artifacts[/{id}]",
            ),
            CapabilityRecord(
                name="cost",
                available=facade.cost_summary is not None,
                description="GET /cost",
            ),
            CapabilityRecord(
                name="reviews",
                available=facade.review is not None,
                description="GET /reviews, POST /reviews/{id}/decision",
            ),
            CapabilityRecord(
                name="bulk.export",
                available=bulk_export is not None,
                description="GET /exports/{documents,sources,chunks,citations,metadata,feedback}.ndjson",
            ),
            CapabilityRecord(
                name="bulk.import",
                available=bulk_import is not None,
                description="POST /imports/{documents,sources,metadata}.ndjson",
            ),
        ]
        record = CapabilitiesRecord(api_version=version, capabilities=caps)
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    return app


# ---- Helpers ---------------------------------------------------------


def _req_id(request: Request) -> str:
    return getattr(request.state, "request_id", uuid.uuid4().hex)


def _bulk_result_record(result) -> BulkImportResultRecord:
    return BulkImportResultRecord(
        succeeded=result.succeeded,
        skipped_idempotent=result.skipped_idempotent,
        failures=[
            BulkImportFailureRow(
                line_number=f.line_number,
                record_id=f.record_id,
                code=f.code,
                message=f.message,
            )
            for f in result.failures
        ],
        total=result.total,
    )


def _document_record(dto) -> DocumentRecord:
    return DocumentRecord(
        document_id=dto.document_id,
        tenant_id=dto.tenant_id,
        project_id=dto.project_id,
        original_filename=dto.original_filename,
        stored_filename=dto.stored_filename,
        mime_type=dto.mime_type,
        file_size=dto.file_size,
        checksum=dto.checksum,
        status=dto.status,
        created_at=dto.created_at,
    )


def _artifact_record(dto) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=dto.artifact_id,
        tenant_id=dto.tenant_id,
        project_id=dto.project_id,
        kind=dto.kind,
        location=dto.location,
        content_hash=dto.content_hash,
        byte_size=dto.byte_size,
        status=dto.status,
        review_status=dto.review_status,
        version=dto.version,
        created_at=dto.created_at,
        updated_at=dto.updated_at,
        source_document_ids=list(dto.source_document_ids),
        source_artifact_ids=list(dto.source_artifact_ids),
        metadata=dict(dto.metadata),
    )


def _review_record(dto) -> ReviewItemRecord:
    return ReviewItemRecord(
        review_item_id=dto.review_item_id,
        tenant_id=dto.tenant_id,
        project_id=dto.project_id,
        target_kind=dto.target_kind,
        target_id=dto.target_id,
        review_status=dto.review_status,
        requested_at=dto.requested_at,
        actor=dto.actor,
        notes=dto.notes,
        metadata=dict(dto.metadata),
    )


def _read_job_events(
    workspace: WorkspaceResolver,
    ctx: ProjectContext,
    job_id: str,
) -> list[JobEventRecord]:
    path = workspace.audit(ctx) / AUDIT_LOG_FILENAME
    if not path.exists():
        return []
    events: list[JobEventRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("correlation_id") != job_id:
            continue
        events.append(
            JobEventRecord(
                event_id=data["event_id"],
                occurred_at=data["occurred_at"],
                actor=data["actor"],
                action=data["action"],
                target_kind=data["target_kind"],
                target_id=data["target_id"],
                payload=data.get("payload") or {},
            )
        )
    return events


# ---- Ingestion-run helpers --------------------------------------


def _ingestion_run_to_list_item(run) -> IngestionRunListItem:
    """Compact projection used by `GET /ingestion-runs`. Pulls
 `documentName` / `mode` / `policy` from the run's metadata bag
 (populated by the upload handler) so the All Runs view can render
 the same display fields as the upload page."""
    metadata = run.metadata or {}
    return IngestionRunListItem(
        run_id=run.run_id,
        document_id=run.document_id,
        document_name=_optional_str(metadata.get("document_name")),
        mode=_optional_str(metadata.get("mode")),
        policy=_optional_str(metadata.get("policy")),
        status=run.status.value,
        started_at=run.started_at,
        updated_at=run.updated_at,
        completed_at=run.completed_at,
        current_stage=run.current_stage,
        current_step=run.current_step,
        progress_percent=run.progress_percent,
        warning_count=run.warning_count,
        failure_code=run.failure_code,
        failure_message=run.failure_message,
    )


def _optional_str(value: object) -> str | None:
    """Coerce a metadata-bag value into a non-empty str, or None.

 Run metadata is `dict[str, object]` (raw JSONL passthrough), so
 callers shouldn't trust the type. We accept anything that
 stringifies and treat empty / missing as None so Pydantic can
 omit the field from the wire payload entirely."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _ingestion_run_to_record(
    run, *, latest_progress: "dict[str, Any] | None" = None,
) -> IngestionRunRecord:
    """Project an `IngestionRun` dataclass into the wire schema.

 `latest_progress` (optional) is a small dict produced by
 `_latest_progress_snapshot` carrying the most recent
 `j1.progress.*` audit event for the run. When supplied AND the
 run record's own `current_*` fields are empty, we backfill them
 from the event so the wire payload reflects what the worker is
 actually doing right now. The run record itself isn't mutated —
 it stays the source of truth for terminal state."""
    current_stage = run.current_stage
    current_step = run.current_step
    progress_percent = run.progress_percent
    last_event_type: str | None = None
    if latest_progress is not None:
        last_event_type = latest_progress.get("event_type")
        if not current_stage:
            current_stage = latest_progress.get("stage")
        if not current_step:
            current_step = latest_progress.get("step")
        # Only fall back to event-derived percent when the run
        # record didn't carry one — keeps the run-store as the
        # source of truth when it is updated.
        if not progress_percent and latest_progress.get("progress_percent"):
            progress_percent = int(latest_progress["progress_percent"])
    return IngestionRunRecord(
        run_id=run.run_id,
        document_id=run.document_id,
        workflow_id=run.workflow_id,
        workflow_run_id=run.workflow_run_id,
        status=run.status.value,
        started_at=run.started_at,
        updated_at=run.updated_at,
        completed_at=run.completed_at,
        workspace_id=run.workspace_id,
        current_stage=current_stage,
        current_step=current_step,
        progress_percent=progress_percent,
        failure_code=run.failure_code,
        failure_message=run.failure_message,
        warning_count=run.warning_count,
        last_event_type=last_event_type,
        target_snapshot_id=getattr(run, "target_snapshot_id", None),
        metadata=dict(run.metadata),
    )


def _latest_progress_snapshot(
    workspace: WorkspaceResolver,
    ctx: ProjectContext,
    run_id: str,
) -> dict[str, Any] | None:
    """Return a small dict for the most recent `j1.progress.*` audit
 entry for the run, or None if no event has been recorded.

 The run-store record isn't currently updated by the worker (the
 worker emits audit progress events; only the API process writes
 to the run-store). Without this lookup the detail endpoint's
 `currentStage`/`currentStep` would stay None for the lifetime of
 the run. We tail the audit log here on each detail GET — same
 cost as the existing events endpoint's full read but we stop at
 the first match scanning backwards.

 Step events (`step.*`) are preferred over plan/run-level events
 because they carry the `stage` / `step` the FE wants. When no
 `step.*` event exists yet (very early in a run) we fall back to
 whatever the most recent `j1.progress.*` entry was so
 `lastEventType` is at least populated."""
    path = workspace.audit(ctx) / AUDIT_LOG_FILENAME
    if not path.exists():
        return None
    fallback: dict[str, Any] | None = None
    # Scan once front-to-back; keep the latest matching record. The
    # JSONL audit log is append-only, so the last matching line wins.
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    latest_step: dict[str, Any] | None = None
    latest_any: dict[str, Any] | None = None
    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("correlation_id") != run_id:
            continue
        action = data.get("action") or ""
        if not action.startswith(PROGRESS_ACTION_PREFIX):
            continue
        event_type = action[len(PROGRESS_ACTION_PREFIX):]
        payload = data.get("payload") or {}
        snapshot = {
            "event_type": event_type,
            "stage": payload.get("stage"),
            "step": payload.get("step"),
            "progress_percent": payload.get("progress_percent"),
        }
        latest_any = snapshot
        if event_type.startswith("step."):
            latest_step = snapshot
    return latest_step or latest_any or fallback


def _read_progress_events(
    workspace: WorkspaceResolver,
    ctx: ProjectContext,
    run_id: str,
) -> list[ProgressEventRecord]:
    """Read the audit log and project `j1.progress.*` entries for the
 given run into the frontend's `ProgressEvent` shape. Backed by the
 same JSONL audit store used by `_read_job_events` so there's no
 second source of truth."""
    path = workspace.audit(ctx) / AUDIT_LOG_FILENAME
    if not path.exists():
        return []
    out: list[ProgressEventRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("correlation_id") != run_id:
            continue
        action = data.get("action") or ""
        if not action.startswith(PROGRESS_ACTION_PREFIX):
            continue
        out.append(_progress_event_from_audit(data))
    return out


def _progress_event_from_audit(data: dict[str, Any]) -> ProgressEventRecord:
    """Project one audit JSONL line into a `ProgressEventRecord`.

 Pydantic's `alias_generator=to_camel` only camel-cases declared
 model fields; dict CONTENTS pass through verbatim. The reporter
 writes payloads as snake_case (Python convention), so we camelize
 `metadata` keys HERE — that way the SSE / events-history wire
 format is uniformly camelCase, and frontends never have to read
 snake_case keys for `failure_code`, `error_type`, `final_status`,
 etc."""
    payload = data.get("payload") or {}
    action: str = data["action"]
    typed_keys = {
        "severity", "stage", "step", "status", "progress_percent",
        "current", "total", "message", "engine", "provider",
    }
    metadata = {
        to_camel(k): v for k, v in payload.items() if k not in typed_keys
    }
    return ProgressEventRecord(
        event_id=data["event_id"],
        run_id=data.get("correlation_id") or "",
        event_type=action[len(PROGRESS_ACTION_PREFIX):],
        timestamp=data["occurred_at"],
        severity=str(payload.get("severity") or "INFO"),
        stage=payload.get("stage"),
        step=payload.get("step"),
        status=payload.get("status"),
        progress_percent=payload.get("progress_percent"),
        current=payload.get("current"),
        total=payload.get("total"),
        message=payload.get("message"),
        engine=payload.get("engine"),
        provider=payload.get("provider"),
        metadata=metadata,
    )




def _read_run_plan(
    workspace: WorkspaceResolver,
    ctx: ProjectContext,
    run_id: str,
) -> ExecutionPlanRecord | None:
    """Find the most recent `plan.generated` audit entry for the run
 and reshape it into the frontend's execution-plan record."""
    path = workspace.audit(ctx) / AUDIT_LOG_FILENAME
    if not path.exists():
        return None
    latest_payload: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("correlation_id") != run_id:
            continue
        if data.get("action") != ACTION_PROGRESS_PLAN_GENERATED:
            continue
        latest_payload = data.get("payload") or {}
    if latest_payload is None:
        return None
    plan_dict = latest_payload.get("plan") or {}
    return ExecutionPlanRecord(
        run_id=run_id,
        document_id=str(plan_dict.get("document_id") or ""),
        mode=str(plan_dict.get("mode") or ""),
        policy=str(plan_dict.get("policy") or ""),
        confidence=float(plan_dict.get("confidence") or 0.0),
        estimated_cost_level=str(plan_dict.get("estimated_cost_level") or "low"),
        fast_llm_used=bool(plan_dict.get("fast_llm_used")),
        warnings=list(plan_dict.get("warnings") or []),
        steps=[
            ExecutionPlanStep(
                step_id=str(s.get("step_id") or s.get("name") or ""),
                stage=str(s.get("stage") or ""),
                name=str(s.get("name") or ""),
                decision=str(s.get("decision") or "RUN"),
                reason=s.get("reason"),
                required=bool(s.get("required") or False),
                source=str(s.get("source") or "default"),
                dependency_step_ids=list(s.get("dependency_step_ids") or []),
                estimated_cost_tier=str(s.get("estimated_cost_tier") or "NONE"),
                expected_engine=s.get("expected_engine"),
                expected_provider=s.get("expected_provider"),
                risk_level=str(s.get("risk_level") or "low"),
                warning=s.get("warning"),
                metadata=dict(s.get("metadata") or {}),
                llm_class=str(s.get("llm_class") or "none"),
            )
            for s in (plan_dict.get("steps") or [])
        ],
        profile=dict(plan_dict.get("profile") or {}),
        requires_vision=bool(plan_dict.get("requires_vision") or False),
        requires_premium_llm=bool(plan_dict.get("requires_premium_llm") or False),
        vision_decisions=list(plan_dict.get("vision_decisions") or []),
    )


# ---- SSE streaming for progress -------------------------------------

# The streamer tails the audit JSONL and emits one SSE message per
# progress entry for the given run. Supports `Last-Event-Id` (resume)
# by skipping events until the cursor is found. Closes the stream
# when the run reaches a terminal status or the connection times out.

import asyncio as _asyncio

_SSE_TAIL_INTERVAL_SECONDS = 1.0
_SSE_MAX_DURATION_SECONDS = 60 * 60  # 1 hour — clients reconnect after.
# Stream closes when the run reaches one of these. Mirrors the set of
# `RunStatus` terminal values so the SSE doesn't idle-loop for an hour
# after a non-success terminal:
#  run.completed → SUCCEEDED / SUCCEEDED_WITH_WARNINGS
#  run.failed → FAILED
#  run.cancelled → CANCELLED
#  human_review.required → REQUIRES_HUMAN_REVIEW (terminal per the run
#  state machine; user continues via the review API)
_TERMINAL_PROGRESS_TYPES = PROGRESS_TERMINAL_EVENT_TYPES


async def _stream_progress_events(
    workspace: WorkspaceResolver,
    ctx: ProjectContext,
    run_id: str,
    last_event_id: str | None,
):
    path = workspace.audit(ctx) / AUDIT_LOG_FILENAME
    seen: set[str] = set()
    yielded_after_cursor = last_event_id is None
    started_at = _asyncio.get_event_loop().time()
    while True:
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                content = ""
            for line in content.splitlines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("correlation_id") != run_id:
                    continue
                action = data.get("action") or ""
                if not action.startswith(PROGRESS_ACTION_PREFIX):
                    continue
                eid = data.get("event_id")
                if not eid or eid in seen:
                    continue
                if not yielded_after_cursor:
                    if eid == last_event_id:
                        yielded_after_cursor = True
                        seen.add(eid)
                    continue
                seen.add(eid)
                event = _progress_event_from_audit(data)
                yield _format_progress_sse(event)
                if event.event_type in _TERMINAL_PROGRESS_TYPES:
                    return
        if _asyncio.get_event_loop().time() - started_at > _SSE_MAX_DURATION_SECONDS:
            return
        await _asyncio.sleep(_SSE_TAIL_INTERVAL_SECONDS)


def _format_progress_sse(event: ProgressEventRecord) -> bytes:
    """Format one ProgressEvent as an SSE message.

 Output shape:

 id: <event_id>
 event: <event_type>
 data: <json>
 \\n

 `id:` lets the client reconnect with `Last-Event-Id`. `event:`
 lets the client subscribe to specific event types.
 """
    payload = event.model_dump(by_alias=True, mode="json")
    body = json.dumps(payload, separators=(",", ":"))
    return (
        f"id: {event.event_id}\n"
        f"event: {event.event_type}\n"
        f"data: {body}\n\n"
    ).encode("utf-8")


# ---- Imported test cases DTO → record translators ----------
# The validation service deals in dataclasses; these helpers project
# the imported-test-case shape into the REST wire schema.


def _imported_set_to_record(imported_set) -> ImportedTestCaseSetRecord:
    return ImportedTestCaseSetRecord(
        document_id=imported_set.document_id,
        imported_at=imported_set.imported_at.isoformat(),
        source_filename=imported_set.source_filename,
        cases=[
            ImportedTestCaseRecord(
                test_case_id=c.test_case_id,
                question=c.question,
                expected_answer=c.expected_answer,
                expected_sources=list(c.expected_sources),
                test_type=c.test_type,
                notes=c.notes,
            )
            for c in imported_set.cases
        ],
    )


def _imported_execution_to_record(
    execution,
) -> ImportedTestCaseExecutionRecord:
    summary = execution.summary
    return ImportedTestCaseExecutionRecord(
        document_id=execution.document_id,
        executed_at=execution.executed_at.isoformat(),
        run_id=execution.run_id,
        summary=ImportedTestCaseSummaryRecord(
            total=summary.total,
            answered=summary.answered,
            with_sources=summary.with_sources,
            scope_issues=summary.scope_issues,
            errors=summary.errors,
            overall=summary.overall,
        ),
        results=[
            ImportedTestCaseResultRecord(
                test_case_id=r.test_case_id,
                question=r.question,
                status=r.status,
                has_sources=r.has_sources,
                scope_ok=r.scope_ok,
                error=r.error,
                run_id=r.run_id,
            )
            for r in execution.results
        ],
    )
