import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import (
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
    publish_answer_generated,
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
from j1.adapters.rest.sse import SSE_CONTENT_TYPE, SSE_HEADERS, format_sse
from j1.integration.streaming import (
    AnswerStreamingService,
    BufferingStreamHandler,
    STREAM_EVENT_ANSWER_COMPLETED,
    STREAM_EVENT_ANSWER_FAILED,
    STREAM_EVENT_ANSWER_STARTED,
    STREAM_EVENT_RETRIEVAL_STARTED,
)
from j1.adapters.rest.schemas import (
    AnswerRecord,
    AnswerRequest,
    ArtifactListRecord,
    ArtifactRecord,
    BulkImportFailureRow,
    BulkImportResultRecord,
    CapabilitiesRecord,
    CapabilityRecord,
    CitationDetailRecord,
    CitationRecord,
    ContextBlockRecord,
    CostSummaryRecord,
    DocumentRecord,
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
    GenerateValidationSetRequestRecord,
    LLMTraceRecord,
    ManualTestQueryRequestRecord,
    ManualTestQueryResponseRecord,
    NativeDebugQueryRequestRecord,
    NativeDebugQueryResponseRecord,
    StartValidationRunRequestRecord,
    TesterVerdictRequestRecord,
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
    ValidationCitationRecord,
    ValidationCoverageRecord,
    ValidationResultRecord,
    ValidationRunListItem,
    ValidationRunListRecord,
    ValidationRunRecord,
    ValidationSetListItem,
    ValidationSetListRecord,
    ValidationSetRecord,
    ValidationSummaryRecord,
    ValidationTestCaseRecord,
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
    ACTION_OPS_RUN_INDEX_REBUILT,
    ACTION_OPS_RUN_PURGED,
    ACTION_OPS_RUN_REINDEXED,
    ACTION_OPS_RUN_RESUMED,
    TARGET_KIND_INGESTION_BATCH,
    TARGET_KIND_INGESTION_RUN,
)
from j1.integration.dto import (
    AnswerRequestDTO,
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
    document_lifecycle_service: "DocumentLifecycleService | None" = None,
    confirm_handler: RunConfirmHandler | None = None,
    compile_handler: RunCompileHandler | None = None,
    llm_registry: object | None = None,
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

    @app.exception_handler(ValueError)
    async def _value_error(request, exc: ValueError) -> JSONResponse:
        return error_response(
            status_code=400,
            code="INVALID_ARGUMENT",
            message=str(exc),
            request_id=_req_id(request),
        )

    # ---- Dependencies ------------------------------------------------

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

    def require_answer():
        if facade.answer is None:
            raise HTTPException(503, "answer capability not configured")
        return facade.answer

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
        summary="Reprocess a document from its original source",
        description=(
            "Starts a NEW ingestion run for the same document_id as "
            "the referenced run. Allocates a fresh run_id + workflow "
            "(suffixed `-reindex-{run_id}` so it doesn't collide "
            "with the original under USE_EXISTING). The new run's "
            "metadata carries `reindex_of=<original-run-id>` so the "
            "FE can render the relationship. Refuses to operate on "
            "a run that's still active (HTTP 409). The original run "
            "is preserved unchanged — operators delete it explicitly "
            "via the soft-delete endpoint when ready."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def full_reindex_ingestion_run(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        starter: JobStarter = Depends(require_job_starter),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        from datetime import datetime, timezone
        from j1.ingestion_review.exceptions import RunStillActive
        store = _require_run_store()
        original = store.get(ctx, run_id)
        if original is None:
            raise HTTPException(404, f"ingestion run {run_id!r} not found")
        # Same active-state guard as soft-delete; can't kick off a
        # new attempt while the original workflow might still be
        # writing artifacts.
        active_states = {
            RunStatus.RUNNING.value, RunStatus.PAUSED.value,
            RunStatus.CANCELLING.value, RunStatus.ASSESSING.value,
        }
        if str(original.status) in active_states:
            raise HTTPException(
                409,
                f"run {run_id!r} is currently {original.status} — "
                "cancel it before starting a full-reindex",
            )
        # Validate the original document is still resolvable.
        if not original.document_id:
            raise HTTPException(
                400, f"run {run_id!r} has no document_id; cannot full-reindex"
            )
        try:
            doc_dto = facade.source_lookup.get_source(ctx, original.document_id)
        except Exception:
            raise HTTPException(
                404,
                f"original document {original.document_id!r} not found "
                "in this project; cannot full-reindex",
            )
        # Document-centric guard (Phase 3): a re-index against a
        # detached document doesn't make sense — the result wouldn't
        # be visible to retrieval anyway. A re-index against a
        # removed document is even more nonsensical because the
        # knowledge has been disowned. Forces the user to attach
        # first via the new lifecycle action. 409 (not 404) because
        # the document exists; the state just disallows the action.
        _enforce_document_attached_for_action(ctx, original.document_id, "full-reindex")
        actor = security.subject if security else "system"
        new_run_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        # Inherit processor selection from the original run's metadata
        # when present, so a re-index repeats with the same recipe.
        original_meta = dict(original.metadata or {})
        new_run = IngestionRun(
            run_id=new_run_id,
            document_id=original.document_id,
            workflow_id=None,
            workflow_run_id=None,
            status=RunStatus.CREATED,
            started_at=now,
            updated_at=now,
            metadata={
                "reindex_of": run_id,
                "policy": original_meta.get("policy", "auto"),
                "mode": original_meta.get("mode", "STANDARD"),
                "document_name": original_meta.get(
                    "document_name", doc_dto.original_filename,
                ),
            },
            # Document-centric classification (Phase 4). The
            # `reindex_of` metadata key stays for backward compat
            # with FE consumers that learned to read it earlier;
            # `parent_run_id` is the new structured field that
            # downstream surfaces (projector, run-history UI) read.
            run_type="reindex",
            parent_run_id=run_id,
            document_version_id=original.document_version_id,
        )
        store.upsert(ctx, new_run)

        if progress_reporter is not None:
            progress_reporter.report_run_created(
                ctx, run_id=new_run_id, document_id=original.document_id,
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
            reindex_of=run_id,  # signals starter to use suffixed workflow_id
        )
        workflow_id = await starter(ctx, original.document_id, body)
        new_run.workflow_id = workflow_id
        new_run.updated_at = datetime.now(timezone.utc)
        store.upsert(ctx, new_run)
        _emit_ops_event(
            ctx,
            action=ACTION_OPS_RUN_REINDEXED,
            target_id=new_run_id,
            actor=actor,
            correlation_id=new_run_id,
            payload={
                "original_run_id": run_id,
                "document_id": original.document_id,
                "workflow_id": workflow_id,
            },
        )
        return envelope(
            {
                "originalRunId": run_id,
                "reindexRunId": new_run_id,
                "workflowId": workflow_id,
                "documentId": original.document_id,
                "status": RunStatus.CREATED.value,
            },
            _req_id(request),
        )

    @app.post(
        "/ingestion-runs/{run_id}/resume-from-checkpoint",
        tags=["ingestion-runs"],
        summary="Resume a failed run from its last checkpoint",
        description=(
            "Starts a NEW ingestion run for the same document_id as "
            "the referenced run, carrying forward the prior run's "
            "produced artifacts and skipping the LLM-cost stages "
            "(enrich + graph) that already completed. Refuses to "
            "operate on an in-flight run (HTTP 409). Returns HTTP 412 "
            "when the prior run has no resume snapshot (terminated "
            "before the snapshot machinery landed, or via a path "
            "that doesn't snapshot — e.g. cancelled), and HTTP 412 "
            "with a structured `diff` when settings have drifted "
            "since the prior run finished. The new run's metadata "
            "carries `resume_of=<original-run-id>` so the FE can "
            "render the relationship."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def resume_ingestion_run_from_checkpoint(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        starter: JobStarter = Depends(require_job_starter),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        from datetime import datetime, timezone
        from j1.ingestion_review.exceptions import (
            ResumeIncompatible, ResumeNotPossible, ReviewNotFound,
            RunStillActive,
        )
        store = _require_run_store()
        review = _require_review_service()
        original = store.get(ctx, run_id)
        if original is None:
            raise HTTPException(404, f"ingestion run {run_id!r} not found")
        if not original.document_id:
            raise HTTPException(
                400, f"run {run_id!r} has no document_id; cannot resume"
            )
        try:
            doc_dto = facade.source_lookup.get_source(ctx, original.document_id)
        except Exception:
            raise HTTPException(
                404,
                f"original document {original.document_id!r} not found "
                "in this project; cannot resume",
            )
        # Document-centric guard (Phase 3): resume requires the
        # document to still be attached. Detached → user should
        # attach first; removed → knowledge has been disowned and
        # resume from old runs is disabled permanently per spec.
        _enforce_document_attached_for_action(ctx, original.document_id, "resume")
        # Resolve the new run's settings up-front so the compatibility
        # check sees the same dict the workflow will receive. The
        # resume endpoint inherits processor selections from the
        # deployment defaults — operators who want to change them
        # should full-reindex instead.
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
        # Build the candidate-settings dict mirroring the workflow's
        # `ProjectProcessingRequest`. The fields that participate in
        # the compatibility hash live in `RESUME_SETTINGS_FIELDS`;
        # any field absent here defaults to whatever the workflow
        # uses, which is what the prior run would also have used.
        candidate_settings: dict[str, Any] = {
            "compiler_kind": compiler_kind,
            "enricher_kind": enricher_kind,
            "graph_builder_kind": graph_builder_kind,
            "indexer_kind": indexer_kind,
            # Operators currently can't override these per-run; they
            # come from the API process's env, which is also where
            # the prior run got them. If the operator restarted the
            # API with different env between runs the snapshot hash
            # will catch it.
            "planner_enabled": (
                getattr(starter, "_planner_enabled", None)
                # Fallback when the closure didn't expose it.
                if hasattr(starter, "_planner_enabled") else None
            ),
            "policy": "auto",
            "domain_override": None,
            "workspace_default_domain": None,
            "failure_policy": "fail_fast",
        }
        # When the candidate-settings keys above don't get filled
        # because the starter doesn't expose them, fall back to the
        # snapshot's own values. This keeps the comparison effective
        # against drift that operators CAN cause (changing a
        # processor kind via env), while ignoring drift in fields
        # neither end can vary today.
        snap = (original.metadata or {}).get("resume_snapshot") or {}
        prior_settings = (
            snap.get("settings_snapshot") if isinstance(snap, dict) else {}
        ) or {}
        for k, v in candidate_settings.items():
            if v is None and k in prior_settings:
                candidate_settings[k] = prior_settings[k]

        try:
            plan = review.resume_from_checkpoint(
                ctx, run_id, candidate_settings=candidate_settings,
            )
        except ReviewNotFound:
            raise HTTPException(404, f"ingestion run {run_id!r} not found")
        except RunStillActive as exc:
            raise HTTPException(409, str(exc))
        except ResumeNotPossible as exc:
            raise HTTPException(412, str(exc))
        except ResumeIncompatible as exc:
            # Surface the structured diff via the standard J1 error
            # envelope's `details` so the FE can render exactly which
            # settings changed. Returning the JSONResponse directly
            # bypasses the generic HTTPException handler that would
            # stringify our dict into the `message` field.
            return error_response(
                status_code=412,
                code="RESUME_INCOMPATIBLE",
                message=str(exc),
                request_id=_req_id(request),
                details={"diff": exc.diff},
            )

        actor = security.subject if security else "system"
        new_run_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        original_meta = dict(original.metadata or {})
        new_run = IngestionRun(
            run_id=new_run_id,
            document_id=original.document_id,
            workflow_id=None,
            workflow_run_id=None,
            status=RunStatus.CREATED,
            started_at=now,
            updated_at=now,
            metadata={
                "resume_of": run_id,
                "resumed_steps": plan["resumable_steps"],
                "carry_forward_artifact_ids": plan["carry_forward_artifact_ids"],
                "policy": original_meta.get("policy", "auto"),
                "mode": original_meta.get("mode", "STANDARD"),
                "document_name": original_meta.get(
                    "document_name", doc_dto.original_filename,
                ),
            },
        )
        store.upsert(ctx, new_run)

        if progress_reporter is not None:
            progress_reporter.report_run_created(
                ctx, run_id=new_run_id, document_id=original.document_id,
                actor=actor,
            )

        body = IngestRequest(
            compiler_kind=compiler_kind,
            enricher_kind=enricher_kind,
            graph_builder_kind=graph_builder_kind,
            indexer_kind=indexer_kind,
            actor=actor,
            correlation_id=new_run_id,
            resume_of=run_id,
            resume_completed_steps=tuple(plan["resumable_steps"]),
            resume_artifact_ids=tuple(plan["carry_forward_artifact_ids"]),
            resume_artifact_kinds=tuple(plan["carry_forward_artifact_kinds"]),
        )
        workflow_id = await starter(ctx, original.document_id, body)
        new_run.workflow_id = workflow_id
        new_run.updated_at = datetime.now(timezone.utc)
        store.upsert(ctx, new_run)
        _emit_ops_event(
            ctx,
            action=ACTION_OPS_RUN_RESUMED,
            target_id=new_run_id,
            actor=actor,
            correlation_id=new_run_id,
            payload={
                "original_run_id": run_id,
                "document_id": original.document_id,
                "workflow_id": workflow_id,
                "resumed_steps": list(plan["resumable_steps"]),
                "carry_forward_artifact_count":
                    len(plan["carry_forward_artifact_ids"]),
            },
        )
        return envelope(
            {
                "originalRunId": run_id,
                "resumeRunId": new_run_id,
                "workflowId": workflow_id,
                "documentId": original.document_id,
                "status": RunStatus.CREATED.value,
                "resumedSteps": plan["resumable_steps"],
                "carryForwardArtifactCount": len(plan["carry_forward_artifact_ids"]),
            },
            _req_id(request),
        )

    @app.post(
        "/ingestion-runs/{run_id}/rebuild-index",
        tags=["ingestion-runs"],
        summary="Rebuild the retrieval index from existing chunks",
        description=(
            "Starts a NEW ingestion run for the same document_id that "
            "skips compile / enrich / graph entirely and only runs "
            "the index activity against the prior run's chunk "
            "artifacts. Useful when the vector store was cleared, the "
            "embedding model upgraded, or the index got corrupted "
            "while the chunks themselves are still valid. Refuses to "
            "operate on an in-flight run (HTTP 409). Returns HTTP 412 "
            "when the prior run has no resume snapshot or never "
            "produced chunk artifacts (use full-reindex instead). The "
            "new run's metadata carries `rebuild_of=<original-run-id>` "
            "so the FE can render the relationship."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def rebuild_ingestion_run_index(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        starter: JobStarter = Depends(require_job_starter),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        from datetime import datetime, timezone
        from j1.ingestion_review.exceptions import (
            ResumeNotPossible, ReviewNotFound, RunStillActive,
        )
        store = _require_run_store()
        review = _require_review_service()
        original = store.get(ctx, run_id)
        if original is None:
            raise HTTPException(404, f"ingestion run {run_id!r} not found")
        if not original.document_id:
            raise HTTPException(
                400,
                f"run {run_id!r} has no document_id; cannot rebuild index",
            )
        try:
            doc_dto = facade.source_lookup.get_source(ctx, original.document_id)
        except Exception:
            raise HTTPException(
                404,
                f"original document {original.document_id!r} not found "
                "in this project; cannot rebuild index",
            )
        try:
            plan = review.rebuild_index_only(ctx, run_id)
        except ReviewNotFound:
            raise HTTPException(404, f"ingestion run {run_id!r} not found")
        except RunStillActive as exc:
            raise HTTPException(409, str(exc))
        except ResumeNotPossible as exc:
            raise HTTPException(412, str(exc))
        # Resolve the indexer kind: prefer the snapshot's value (so
        # the rebuild repeats with the same recipe). Fall back to
        # the deployment default — covers the case where an operator
        # updated the worker between runs and the prior indexer is
        # no longer registered.
        indexer_kind = plan.get("indexer_kind") or _resolve_optional_processor_kind(
            None,
            (processing_capabilities.indexer_kinds
             if processing_capabilities else frozenset()),
            "indexerKind",
        )
        if not indexer_kind:
            raise HTTPException(
                412,
                f"run {run_id!r} has no indexer_kind on snapshot and no "
                "default is registered — nothing to rebuild against",
            )
        # Compile is mandatory in normal flows; rebuild-index-only
        # short-circuits past it. Pass a non-empty `compiler_kind`
        # anyway because `ProjectProcessingRequest` requires the
        # field — the workflow won't dispatch the activity in this
        # mode.
        compiler_kind = _resolve_compiler_kind(None)

        actor = security.subject if security else "system"
        new_run_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        original_meta = dict(original.metadata or {})
        new_run = IngestionRun(
            run_id=new_run_id,
            document_id=original.document_id,
            workflow_id=None,
            workflow_run_id=None,
            status=RunStatus.CREATED,
            started_at=now,
            updated_at=now,
            metadata={
                "rebuild_of": run_id,
                "carry_forward_artifact_ids": plan["chunk_artifact_ids"],
                "policy": original_meta.get("policy", "auto"),
                "mode": original_meta.get("mode", "STANDARD"),
                "document_name": original_meta.get(
                    "document_name", doc_dto.original_filename,
                ),
            },
        )
        store.upsert(ctx, new_run)

        if progress_reporter is not None:
            progress_reporter.report_run_created(
                ctx, run_id=new_run_id, document_id=original.document_id,
                actor=actor,
            )

        # Re-use the resume_* fields to thread the carry-forward
        # artifact ids (the workflow seeds `_produced_artifact_ids`
        # from them at startup). The rebuild-index-only flag tells
        # the workflow to skip the per-document loop entirely.
        body = IngestRequest(
            compiler_kind=compiler_kind,
            indexer_kind=indexer_kind,
            actor=actor,
            correlation_id=new_run_id,
            resume_of=run_id,
            resume_artifact_ids=tuple(plan["chunk_artifact_ids"]),
            resume_artifact_kinds=tuple(plan["chunk_artifact_kinds"]),
            rebuild_index_only=True,
        )
        workflow_id = await starter(ctx, original.document_id, body)
        new_run.workflow_id = workflow_id
        new_run.updated_at = datetime.now(timezone.utc)
        store.upsert(ctx, new_run)
        _emit_ops_event(
            ctx,
            action=ACTION_OPS_RUN_INDEX_REBUILT,
            target_id=new_run_id,
            actor=actor,
            correlation_id=new_run_id,
            payload={
                "original_run_id": run_id,
                "document_id": original.document_id,
                "workflow_id": workflow_id,
                "carry_forward_chunk_count":
                    len(plan["chunk_artifact_ids"]),
                "indexer_kind": indexer_kind,
            },
        )
        return envelope(
            {
                "originalRunId": run_id,
                "rebuildRunId": new_run_id,
                "workflowId": workflow_id,
                "documentId": original.document_id,
                "status": RunStatus.CREATED.value,
                "carryForwardChunkCount": len(plan["chunk_artifact_ids"]),
                "indexerKind": indexer_kind,
            },
            _req_id(request),
        )

    @app.delete(
        "/ingestion-runs/{run_id}",
        tags=["ingestion-runs"],
        summary="Soft-delete an ingestion run",
        description=(
            "Tombstones the run + every artifact tagged with this "
            "run_id. Tombstoned records stay on disk for audit; the "
            "FE listing surface excludes them. Idempotent — calling "
            "twice returns the same envelope with "
            "`wasAlreadyDeleted=true`. Refuses to operate on a run "
            "that's still RUNNING / PAUSED / CANCELLING / ASSESSING "
            "with HTTP 409 — operators must `cancel` first."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    def delete_ingestion_run(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        from j1.ingestion_review.exceptions import (
            ReviewNotFound, RunStillActive,
        )
        service = _require_review_service()
        actor = security.subject if security else "system"
        try:
            report = service.delete_run(ctx, run_id, actor=actor)
        except ReviewNotFound:
            raise HTTPException(404, f"ingestion run {run_id!r} not found")
        except RunStillActive as exc:
            raise HTTPException(409, str(exc))
        _emit_ops_event(
            ctx,
            action=ACTION_OPS_RUN_DELETED,
            target_id=run_id,
            actor=actor,
            correlation_id=run_id,
            payload={
                "tombstoned_artifact_count":
                    report["tombstoned_artifact_count"],
                "was_already_deleted": report["was_already_deleted"],
                "deleted_at": report["deleted_at"],
            },
        )
        return envelope(
            {
                "runId": report["run_id"],
                "status": report["status"],
                "tombstonedArtifactCount": report["tombstoned_artifact_count"],
                "wasAlreadyDeleted": report["was_already_deleted"],
                "deletedAt": report["deleted_at"],
            },
            _req_id(request),
        )

    @app.post(
        "/ingestion-runs/{run_id}/purge",
        tags=["ingestion-runs"],
        summary="Hard-delete (purge) an ingestion run",
        description=(
            "Physically removes the run record + every artifact "
            "(file on disk + registry record) + cascades to "
            "validation sets / runs that reference this run_id. "
            "The audit log stays intact for compliance. Refuses "
            "to operate on an in-flight run (HTTP 409). By default "
            "requires the run to already be soft-deleted (HTTP 409 "
            "if not — operator must `DELETE` first); set "
            "`?force=true` to bypass that gate for admin tooling. "
            "Idempotent: calling twice returns "
            "`{snapshotsRemoved: 0, ...}` on the second call."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    def purge_ingestion_run(
        request: Request,
        run_id: str,
        force: bool = False,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        from j1.ingestion_review.exceptions import (
            ResumeNotPossible, ReviewNotFound, RunNotTerminal,
            RunStillActive,
        )
        service = _require_review_service()
        actor = security.subject if security else "system"
        try:
            report = service.purge_run(
                ctx, run_id, actor=actor,
                require_already_deleted=not force,
            )
        except ReviewNotFound:
            raise HTTPException(404, f"ingestion run {run_id!r} not found")
        except RunStillActive as exc:
            raise HTTPException(409, str(exc))
        except RunNotTerminal as exc:
            raise HTTPException(409, str(exc))
        # Cascade-delete validation history. Best-effort — failures
        # here don't roll back the artifact/run purge above (which
        # already physically removed bytes from disk; rollback isn't
        # available). The validation cascade is purely operational
        # cleanup; a stale validation record pointing at a missing
        # run is ugly but not dangerous.
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
            action=ACTION_OPS_RUN_PURGED,
            target_id=run_id,
            actor=actor,
            correlation_id=run_id,
            payload={
                "artifacts_purged": report["artifacts_purged"],
                "files_deleted": report["files_deleted"],
                "files_missing": report["files_missing"],
                "snapshots_removed": report["snapshots_removed"],
                "validation_sets_removed": validation_cascade["sets_removed"],
                "validation_runs_removed": validation_cascade["runs_removed"],
                "purged_at": report["purged_at"],
            },
        )
        return envelope(
            {
                "runId": report["run_id"],
                "artifactsPurged": report["artifacts_purged"],
                "filesDeleted": report["files_deleted"],
                "filesMissing": report["files_missing"],
                "snapshotsRemoved": report["snapshots_removed"],
                "validationSetsRemoved": validation_cascade["sets_removed"],
                "validationRunsRemoved": validation_cascade["runs_removed"],
                "purgedAt": report["purged_at"],
            },
            _req_id(request),
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
            "activeRunId": record.active_run_id,
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
            "Clears `active_run_id`, sets `removed_at`, and stamps "
            "every artifact tied to the document as `removed` so "
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
            "`POST /documents/{id}/attach`)."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    async def post_document_reindex(
        request: Request,
        document_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        starter: JobStarter = Depends(require_job_starter),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        from datetime import datetime, timezone
        from j1.ingestion_review.exceptions import RunStillActive
        store = _require_run_store()
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
        active_run_id = getattr(doc_dto, "active_run_id", None)

        # Inherit settings from the previous active run when one
        # exists. New documents (no prior run) get the deployment
        # defaults — same fallback chain as the upload flow.
        previous_meta: dict[str, Any] = {}
        previous_version_id: str | None = None
        if active_run_id:
            previous = store.get(ctx, active_run_id)
            if previous is not None:
                # Same active-state guard the run-level full-reindex
                # uses: don't kick off a new attempt while the
                # previous workflow might still be writing artifacts.
                active_states = {
                    RunStatus.RUNNING.value, RunStatus.PAUSED.value,
                    RunStatus.CANCELLING.value, RunStatus.ASSESSING.value,
                }
                if str(previous.status) in active_states:
                    raise HTTPException(
                        409,
                        f"previous run {active_run_id!r} is currently "
                        f"{previous.status} — cancel it before "
                        "starting a reindex",
                    )
                previous_meta = dict(previous.metadata or {})
                previous_version_id = previous.document_version_id

        actor = security.subject if security else "system"
        new_run_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
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
            },
            run_type="reindex",
            parent_run_id=active_run_id,
            document_version_id=previous_version_id,
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
            reindex_of=active_run_id or None,
        )
        workflow_id = await starter(ctx, document_id, body)
        new_run.workflow_id = workflow_id
        new_run.updated_at = datetime.now(timezone.utc)
        store.upsert(ctx, new_run)
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
            "activeRunId": dto.active_run_id,
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
            "activeRunId": dto.active_run_id,
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
        service = _require_validation_service()
        dto_request = ManualTestQueryRequestDTO(
            question=body.question,
            top_k=body.top_k,
            mode=body.mode,
            citation_required=body.citation_required,
            include_raw=body.include_raw,
            synthesize=body.synthesize,
            validation_scope=body.validation_scope,
        )
        # `_load_run` inside the service raises `ReviewNotFound` on
        # cross-tenant / cross-project access — caught by the
        # existing exception handler at the top of the app and
        # converted to a uniform 404. We don't need to translate
        # here; just let it propagate.
        result = service.run_manual_test_query(
            ctx, run_id, dto_request,
            actor=security.subject,
        )
        record = ManualTestQueryResponseRecord(
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
        return envelope(record.model_dump(by_alias=True), _req_id(request))

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

    # ---- Validation: generated sets + runs ----------------

    @app.post(
        "/ingestion-runs/{run_id}/validation-sets/generate",
        status_code=201,
        tags=["ingestion-runs"],
        summary="Generate a validation set from this run's chunks",
        description=(
            "Generates a draft validation set: smoke + retrieval / "
            "answer cases authored from sampled chunks. Idempotent "
            "on `(runId, generatorVersion, artifactsContentHash)` — "
            "re-calling with the same chunks returns the existing "
            "set unless `force=true`. Generation is synchronous "
            "(small case counts, ≤50). Returns 404 if the run "
            "does not exist in the caller's tenant/project, or "
            "503 if the validation service isn't configured."
        ),
        dependencies=[Depends(scope_required(SCOPE_VALIDATION_WRITE))],
    )
    def post_validation_set_generate(
        request: Request,
        run_id: str,
        body: GenerateValidationSetRequestRecord,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        service = _require_validation_service()
        try:
            vset = service.generate_validation_set(
                ctx, run_id,
                max_cases=body.max_cases,
                citation_required=body.citation_required,
                force=body.force,
                actor=security.subject,
            )
        except RuntimeError as exc:
            #  dependencies not wired (set store / generator
            # missing). Surfaces as 503 — uniform with the rest of
            # the optional-service degradation pattern.
            raise HTTPException(503, str(exc)) from exc
        return envelope(_set_to_record(vset).model_dump(by_alias=True), _req_id(request))

    @app.get(
        "/ingestion-runs/{run_id}/validation-sets",
        tags=["ingestion-runs"],
        summary="List validation sets for this run",
        dependencies=[Depends(scope_required(SCOPE_VALIDATION_READ))],
    )
    def get_validation_sets(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        service = _require_validation_service()
        sets = service.list_validation_sets(ctx, run_id)
        items = [
            ValidationSetListItem(
                validation_set_id=v.validation_set_id,
                run_id=v.run_id,
                source=v.source,
                status=v.status,
                created_at=v.created_at,
                created_by=v.created_by,
                case_count=len(v.test_cases),
            )
            for v in sets
        ]
        record = ValidationSetListRecord(items=items)
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    @app.get(
        "/ingestion-runs/{run_id}/validation-sets/{validation_set_id}",
        tags=["ingestion-runs"],
        summary="Get a validation set with its test cases",
        dependencies=[Depends(scope_required(SCOPE_VALIDATION_READ))],
    )
    def get_validation_set(
        request: Request,
        run_id: str,
        validation_set_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        service = _require_validation_service()
        vset = service.get_validation_set(ctx, run_id, validation_set_id)
        return envelope(_set_to_record(vset).model_dump(by_alias=True), _req_id(request))

    @app.post(
        "/ingestion-runs/{run_id}/validation-runs",
        status_code=201,
        tags=["ingestion-runs"],
        summary="Execute a validation set against this run",
        description=(
            "Runs every test case in the named set against the run "
            "under test. Synchronous in v1 — blocks until every "
            "case has executed. Persists three lifecycle snapshots "
            "(pending → running → terminal) so a polling client "
            "sees progressive state.\n\n"
            "HTTP 201 means the runner job FINISHED (it always "
            "does in v1 — caps + synchronous). The body's "
            "`validationStatus` reports whether the document "
            "PASSED. The two are independent — a 201 with "
            "`validationStatus=\"failed\"` is the canonical "
            "'job ran but the document didn't pass' case."
        ),
        dependencies=[Depends(scope_required(SCOPE_VALIDATION_WRITE))],
    )
    def post_validation_run(
        request: Request,
        run_id: str,
        body: StartValidationRunRequestRecord,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        service = _require_validation_service()
        try:
            vrun = service.run_validation(
                ctx, run_id, body.validation_set_id,
                actor=security.subject,
            )
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        return envelope(_run_to_record(vrun).model_dump(by_alias=True), _req_id(request))

    @app.get(
        "/ingestion-runs/{run_id}/validation-runs",
        tags=["ingestion-runs"],
        summary="List validation runs for this ingestion run",
        dependencies=[Depends(scope_required(SCOPE_VALIDATION_READ))],
    )
    def get_validation_runs(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        service = _require_validation_service()
        vruns = service.list_validation_runs(ctx, run_id)
        items = [
            ValidationRunListItem(
                validation_run_id=v.validation_run_id,
                validation_set_id=v.validation_set_id,
                run_id=v.run_id,
                execution_status=v.execution_status,
                validation_status=v.validation_status,
                started_at=v.started_at,
                completed_at=v.completed_at,
                summary=_summary_to_record(v.summary),
            )
            for v in vruns
        ]
        record = ValidationRunListRecord(items=items)
        return envelope(record.model_dump(by_alias=True), _req_id(request))

    @app.get(
        "/ingestion-runs/{run_id}/validation-runs/{validation_run_id}",
        tags=["ingestion-runs"],
        summary="Get a validation run with full per-case results",
        dependencies=[Depends(scope_required(SCOPE_VALIDATION_READ))],
    )
    def get_validation_run(
        request: Request,
        run_id: str,
        validation_run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        service = _require_validation_service()
        vrun = service.get_validation_run(ctx, run_id, validation_run_id)
        return envelope(_run_to_record(vrun).model_dump(by_alias=True), _req_id(request))

    # ---- Tester verdict -----------------------------------

    @app.post(
        "/ingestion-runs/{run_id}/validation-runs/{validation_run_id}/results/{result_id}/verdict",
        tags=["ingestion-runs"],
        summary="Record a tester verdict on a validation result",
        description=(
            "Layered human override on top of the deterministic "
            "`status`. The auto status NEVER changes — the verdict "
            "is a separate signal recorded on the result. Operators "
            "see both side-by-side; downstream tooling picks the "
            "axis it prefers.\n\n"
            "Valid `verdict` values: `pass` / `warning` / `fail`. "
            "Returns the full updated validation run snapshot."
        ),
        dependencies=[Depends(scope_required(SCOPE_VALIDATION_WRITE))],
    )
    def post_validation_result_verdict(
        request: Request,
        run_id: str,
        validation_run_id: str,
        result_id: str,
        body: TesterVerdictRequestRecord,
        ctx: ProjectContext = Depends(get_ctx),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
        service = _require_validation_service()
        try:
            vrun = service.record_tester_verdict(
                ctx, run_id, validation_run_id, result_id,
                verdict=body.verdict,
                notes=body.notes,
                actor=security.subject,
            )
        except ValueError as exc:
            # Service raises ValueError on an unknown verdict
            # string. Pydantic should already block this at the
            # boundary, but the service guard exists for stand-
            # alone callers; surface as 422 so the wire shape
            # stays consistent.
            raise HTTPException(422, str(exc)) from exc
        return envelope(_run_to_record(vrun).model_dump(by_alias=True), _req_id(request))

    # ---- Validation report export -------------------------

    @app.get(
        "/ingestion-runs/{run_id}/validation-runs/{validation_run_id}/report",
        tags=["ingestion-runs"],
        summary="Export a validation run report (Markdown or JSON)",
        description=(
            "Renders the validation run as a tester-friendly "
            "report. Default `format=markdown` for copy-paste; "
            "`format=json` for downstream automation. The "
            "Markdown surface explicitly distinguishes "
            "executionStatus from validationStatus and surfaces "
            "tester-verdict overrides next to the auto status."
        ),
        dependencies=[Depends(scope_required(SCOPE_VALIDATION_READ))],
    )
    def get_validation_run_report(
        request: Request,
        run_id: str,
        validation_run_id: str,
        format: str = Query(default="markdown", alias="format"),
        ctx: ProjectContext = Depends(get_ctx),
    ) -> Response:
        service = _require_validation_service()
        try:
            content, media = service.export_validation_run_report(
                ctx, run_id, validation_run_id, format=format,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        # Plain Response (not envelope-wrapped) so the body stays
        # downloadable as `report.md` / `report.json` directly.
        # The Content-Disposition header gives the FE a sensible
        # default filename when the user clicks "Download report."
        ext = "md" if media.startswith("text/markdown") else "json"
        return Response(
            content=content,
            media_type=media,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="validation-{validation_run_id}.{ext}"'
                ),
            },
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
        ctx: ProjectContext = Depends(get_ctx),
        starter: JobStarter = Depends(require_job_starter),
        security: SecurityContext = Depends(get_security),
    ) -> dict[str, Any]:
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

        # 4. Persist initial run record with status=CREATED. Subsequent
        # writes (status transitions) append fresh snapshots; the
        # latest one wins on read.
        now = datetime.now(timezone.utc)
        run = IngestionRun(
            run_id=run_id,
            document_id=doc_dto.document_id,
            workflow_id=run_id,
            workflow_run_id=None,
            status=RunStatus.CREATED,
            started_at=now,
            updated_at=now,
            metadata={
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
            },
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

    @app.post(
        "/answer",
        tags=["answer"],
        summary="Generate an answer with citations",
        description=(
            "Returns a single JSON envelope by default. Pass "
            "`?stream=true` for an SSE (`text/event-stream`) stream of "
            "incremental events. Authentication, scope requirements, "
            "validation, and tenant scoping are identical in both modes."
        ),
        dependencies=[Depends(scope_required(SCOPE_ANSWER))],
    )
    async def post_answer(
        request: Request,
        body: AnswerRequest,
        stream: bool = Query(False, description="Stream SSE events instead of one JSON response"),
        ctx: ProjectContext = Depends(get_ctx),
        answer_port=Depends(require_answer),
        security: SecurityContext = Depends(get_security),
    ):
        dto_request = AnswerRequestDTO(
            question=body.question,
            mode=body.mode,
            max_results=body.max_results,
            artifact_types=list(body.artifact_types),
        )
        if stream:
            return _build_answer_stream_response(
                request=request, body=body, ctx=ctx,
                answer_port=answer_port, security=security,
                dto_request=dto_request,
            )

        dto = answer_port.answer(ctx, dto_request)
        publish_answer_generated(
            event_bus, security=security, request_id=_req_id(request),
            tenant_id=ctx.tenant_id, question=body.question,
            mode_used=dto.mode_used, citation_count=len(dto.sources),
            confidence=dto.confidence, review_required=dto.review_required,
        )
        record = AnswerRecord(
            question=body.question,
            answer=dto.answer,
            mode_used=dto.mode_used,
            citations=[
                CitationRecord(
                    artifact_id=c.artifact_id,
                    artifact_type=c.artifact_type,
                    source_document_id=c.source_document_id,
                    source_location=c.source_location,
                    chunk_id=c.chunk_id,
                    run_id=c.run_id,
                )
                for c in dto.sources
            ],
            related_artifacts=list(dto.related_artifacts),
            graph_paths=[
                GraphPathRecord(
                    nodes=list(p.nodes),
                    edges=list(p.edges),
                    description=p.description,
                )
                for p in dto.graph_paths
            ],
            confidence=dto.confidence,
            confidence_level=dto.confidence_level,
            review_required=dto.review_required,
            warnings=list(dto.warnings),
            warning_categories=list(dto.warning_categories),
        )
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
                name="answer",
                available=facade.answer is not None,
                description="POST /answer",
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


def _build_answer_stream_response(
    *,
    request: Request,
    body,
    ctx: ProjectContext,
    answer_port,
    security: SecurityContext,
    dto_request: AnswerRequestDTO,
) -> StreamingResponse:
    """Drive `AnswerStreamingService` and surface its events as SSE.

 Auth/scope/tenant resolution have already happened by the time we
 reach this function — they're enforced by FastAPI dependencies on
 the route. So no extra security checks here, but no bypass either:
 the same `Depends(scope_required(SCOPE_ANSWER))` covers both modes.
 """
    request_id = _req_id(request)
    streaming_service = AnswerStreamingService(answer_port)

    async def event_iter():
        # Drain into a buffering handler synchronously, then stream the
        # collected SSE bytes. Today's `AnswerService.answer` is
        # synchronous and not cancellable mid-flight; once a
        # token-streaming `ModelProvider` is wired in, this loop becomes
        # a true async generator emitting bytes as deltas arrive. The
        # adapter contract (event types + payload shape) doesn't change.
        buffer = BufferingStreamHandler()
        try:
            streaming_service.stream(
                ctx, dto_request,
                request_id=request_id,
                handler=buffer,
                security=security,
            )
        except Exception:
            # AnswerStreamingService.stream already masks failures into
            # an `answer.failed` event; this branch is defence in depth
            # against an unexpected error in the service itself.
            from j1.integration.streaming import (
                AnswerStreamEvent,
                SAFE_GENERATION_FAILED_PAYLOAD,
            )
            yield format_sse(AnswerStreamEvent(
                request_id=request_id,
                event=STREAM_EVENT_ANSWER_FAILED,
                data=dict(SAFE_GENERATION_FAILED_PAYLOAD),
            ))
            return

        for ev in buffer.events:
            # Stop emitting if the client has disconnected — Starlette
            # exposes this even though the underlying call has already
            # completed (limitation: we can't cancel the synchronous
            # answer call mid-flight).
            if await request.is_disconnected():
                return
            yield format_sse(ev)

    return StreamingResponse(
        event_iter(),
        media_type=SSE_CONTENT_TYPE,
        headers=SSE_HEADERS,
    )


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


# ---- Validation DTO → Pydantic record translators ---------
# Kept at module scope so both the GET and POST handlers reuse the
# same projection. The validation service deals in dataclasses; the
# REST adapter's job is to translate to the wire schema.


def _set_to_record(vset) -> ValidationSetRecord:
    return ValidationSetRecord(
        validation_set_id=vset.validation_set_id,
        run_id=vset.run_id,
        document_ids=list(vset.document_ids),
        source=vset.source,
        status=vset.status,
        created_at=vset.created_at,
        created_by=vset.created_by,
        generator_version=vset.generator_version,
        artifacts_content_hash=vset.artifacts_content_hash,
        test_cases=[
            ValidationTestCaseRecord(
                test_case_id=tc.test_case_id,
                question=tc.question,
                type=tc.type,
                priority=tc.priority,
                expected_behavior=tc.expected_behavior,
                expected_answer_points=list(tc.expected_answer_points),
                expected_chunks=list(tc.expected_chunks),
                expected_pages=list(tc.expected_pages),
                expected_artifacts=list(tc.expected_artifacts),
                expected_graph_nodes=list(tc.expected_graph_nodes),
                expected_graph_edges=list(tc.expected_graph_edges),
                citation_required=tc.citation_required,
                source_traceability=list(tc.source_traceability),
                metadata=dict(tc.metadata),
                expected_answer=tc.expected_answer,
                evidence_quote=tc.evidence_quote,
                source_artifact_id=tc.source_artifact_id,
                source_artifact_type=tc.source_artifact_type,
                question_type=tc.question_type,
                validation_scope=tc.validation_scope,
                difficulty=tc.difficulty,
                domain_id=tc.domain_id,
            )
            for tc in vset.test_cases
        ],
        metadata=dict(vset.metadata),
        domain_id=vset.domain_id,
        llm=LLMTraceRecord(
            called=vset.llm.called,
            provider=vset.llm.provider,
            model=vset.llm.model,
            latency_ms=vset.llm.latency_ms,
            prompt_tokens=vset.llm.prompt_tokens,
            completion_tokens=vset.llm.completion_tokens,
            error=vset.llm.error,
        ) if vset.llm is not None else None,
        context_summary=dict(vset.context_summary or {}),
    )


def _summary_to_record(summary) -> ValidationSummaryRecord:
    return ValidationSummaryRecord(
        total=summary.total,
        passed=summary.passed,
        warning=summary.warning,
        failed=summary.failed,
        skipped=summary.skipped,
        coverage=ValidationCoverageRecord(
            by_type=dict(summary.coverage.by_type),
            by_priority=dict(summary.coverage.by_priority),
            by_section=dict(summary.coverage.by_section),
        ),
        main_issues=list(summary.main_issues),
        recommended_action=summary.recommended_action,
    )


def _run_to_record(vrun) -> ValidationRunRecord:
    return ValidationRunRecord(
        validation_run_id=vrun.validation_run_id,
        validation_set_id=vrun.validation_set_id,
        run_id=vrun.run_id,
        execution_status=vrun.execution_status,
        validation_status=vrun.validation_status,
        started_at=vrun.started_at,
        completed_at=vrun.completed_at,
        actor=vrun.actor,
        summary=_summary_to_record(vrun.summary),
        results=[
            ValidationResultRecord(
                result_id=r.result_id,
                test_case_id=r.test_case_id,
                status=r.status,
                question=r.question,
                answer=r.answer,
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
                    for c in r.retrieved_chunks
                ],
                citations=[
                    ValidationCitationRecord(
                        artifact_id=c.artifact_id,
                        artifact_type=c.artifact_type,
                        source_document_id=c.source_document_id,
                        source_location=c.source_location,
                        chunk_id=c.chunk_id,
                        run_id=c.run_id,
                    )
                    for c in r.citations
                ],
                checks=[
                    ValidationCheckRecord(
                        name=chk.name,
                        severity=chk.severity,
                        passed=chk.passed,
                        detail=chk.detail,
                        expected=chk.expected,
                        actual=chk.actual,
                    )
                    for chk in r.checks
                ],
                judge_notes=r.judge_notes,
                failure_reason=r.failure_reason,
                tester_verdict=r.tester_verdict,
                tester_notes=r.tester_notes,
            )
            for r in vrun.results
        ],
        failure_message=vrun.failure_message,
        metadata=dict(vrun.metadata),
    )
