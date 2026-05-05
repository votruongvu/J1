import json
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
from fastapi.responses import JSONResponse, StreamingResponse
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
    FeedbackReceiptRecord,
    FeedbackRequest,
    GraphPathRecord,
    ExecutionPlanRecord,
    ExecutionPlanStep,
    HealthRecord,
    IngestRequest,
    IngestionRunConfirmRecord,
    IngestionRunCreatedRecord,
    IngestionRunRecord,
    JobActionRecord,
    JobEventRecord,
    JobEventsRecord,
    JobStartRecord,
    JobStatusRecord,
    ProgressEventRecord,
    ProgressEventsRecord,
    ProjectCreateRequest,
    ProjectIngestionRequest,
    ProjectRecord,
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
    VersionRecord,
)
from j1.artifacts.registry import ArtifactNotFoundError
from j1.runs import (
    ACTION_PROGRESS_PLAN_CONFIRMED,
    ACTION_PROGRESS_PLAN_GENERATED,
    IngestionRun,
    IngestionRunStore,
    PROGRESS_ACTION_PREFIX,
    ProgressReporter,
    RunStatus,
)
from j1.audit.sink import AUDIT_LOG_FILENAME
from j1.errors.exceptions import (
    DocumentNotFoundError,
    DuplicateDocumentError,
    InvalidIdentifierError,
    J1Error,
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
    SecurityContext,
)
from j1.integration.services import ApplicationFacade
from j1.projects.context import ProjectContext
from j1.review.queue import ReviewItemNotFoundError
from j1.workspace.resolver import WorkspaceResolver

TENANT_HEADER = "X-Tenant-Id"
PROJECT_HEADER = "X-Project-Id"
REQUEST_ID_HEADER = "X-Request-Id"

ContextResolver = Callable[[Request], ProjectContext]
JobStarter = Callable[
    [ProjectContext, str, IngestRequest], Awaitable[str]
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
        return ProjectContext(tenant_id=tenant_id, project_id=project_id)
    except InvalidIdentifierError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    `app.openapi()` method (instance-attribute shadowing has been
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
        # FastAPI's own `openapi()` caches into `app.openapi_schema`.
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
    # (tests, tooling) that call `app.openapi()`.
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
    workspace: WorkspaceResolver | None = None,
    authenticator: Authenticator | None = None,
    anonymous_paths: frozenset[str] | None = None,
    event_bus: ApplicationEventBus | None = None,
    bulk_export: BulkExportService | None = None,
    bulk_import: BulkImportService | None = None,
    processing_capabilities: ProcessingCapabilities | None = None,
    ingestion_run_store: "IngestionRunStore | None" = None,
    progress_reporter: "ProgressReporter | None" = None,
    version: str = "0.1.0",
    api_title: str = "J1 Knowledge Base API",
    description: str | None = None,
) -> FastAPI:
    """Build the standard REST adapter.

    Mandatory dependency: `facade` (an `ApplicationFacade`). Everything else
    is optional and the adapter degrades gracefully:
      * `job_starter=None`   → `POST /documents/{id}/ingest` returns 503
      * `workspace=None`     → retrieve endpoint omits artifact text content
      * `facade.search=None` / `.answer=None` / `.job_status=None` → those
        endpoints return 503 too

    `processing_capabilities` (when supplied — typically constructed
    from `BootstrapResult.to_processing_capabilities()`) lets the API:
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

    # Header-typed parameters surface in OpenAPI / Swagger UI as
    # editable per-endpoint inputs, so operators can test the API
    # interactively. The actual extraction logic still goes through
    # the (pluggable) `resolver`, so a deployment that overrides
    # `context_resolver=` keeps full control over how tenant /
    # project are resolved (e.g. from a JWT). The Header() bindings
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
        return envelope(
            _ingestion_run_to_record(run).model_dump(by_alias=True),
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

    @app.post(
        "/ingestion-runs/{run_id}/confirm",
        tags=["ingestion-runs"],
        summary="Confirm a generated execution plan",
        description=(
            "Acknowledges the plan and signals the workflow to "
            "continue. No-op when the run is already running (default "
            "deployment behaviour is auto-run; the confirmation gate "
            "is opt-in per-run)."
        ),
        dependencies=[Depends(scope_required(SCOPE_INGEST))],
    )
    def post_ingestion_run_confirm(
        request: Request,
        run_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> dict[str, Any]:
        store = _require_run_store()
        run = store.get(ctx, run_id)
        if run is None:
            raise HTTPException(404, f"ingestion run {run_id!r} not found")
        if run.status not in (
            RunStatus.PLAN_READY,
            RunStatus.WAITING_FOR_CONFIRMATION,
        ):
            # Already running / completed / failed — noop, but report
            # the current status so the caller can inspect.
            record = IngestionRunConfirmRecord(run_id=run_id, status=run.status.value)
            return envelope(record.model_dump(by_alias=True), _req_id(request))
        # Mark confirmed; the workflow that was waiting on the
        # confirmation gate will pick this up via its `get_status`
        # query / wait-condition. Until that integration ships the
        # status flip alone is what the UI consumes.
        from datetime import datetime, timezone
        run.status = RunStatus.RUNNING
        run.updated_at = datetime.now(timezone.utc)
        store.upsert(ctx, run)
        record = IngestionRunConfirmRecord(run_id=run_id, status=run.status.value)
        return envelope(record.model_dump(by_alias=True), _req_id(request))

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
        resolved_compiler = _resolve_compiler_kind(compiler_kind)
        resolved_enricher = _validate_optional_processor_kind(
            enricher_kind,
            (processing_capabilities.enricher_kinds
             if processing_capabilities else frozenset()),
            "enricherKind",
        )
        resolved_graph = _validate_optional_processor_kind(
            graph_builder_kind,
            (processing_capabilities.graph_builder_kinds
             if processing_capabilities else frozenset()),
            "graphBuilderKind",
        )
        resolved_indexer = _validate_optional_processor_kind(
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


def _ingestion_run_to_record(run) -> IngestionRunRecord:
    """Project an `IngestionRun` dataclass into the wire schema."""
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
        current_stage=run.current_stage,
        current_step=run.current_step,
        progress_percent=run.progress_percent,
        failure_code=run.failure_code,
        failure_message=run.failure_message,
        warning_count=run.warning_count,
        metadata=dict(run.metadata),
    )


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
    """Project one audit JSONL line into a `ProgressEventRecord`."""
    payload = data.get("payload") or {}
    action: str = data["action"]
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
        metadata={
            k: v for k, v in payload.items()
            if k not in {
                "severity", "stage", "step", "status", "progress_percent",
                "current", "total", "message", "engine", "provider",
            }
        },
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
            )
            for s in (plan_dict.get("steps") or [])
        ],
        profile=dict(plan_dict.get("profile") or {}),
    )


# ---- SSE streaming for progress -------------------------------------

# The streamer tails the audit JSONL and emits one SSE message per
# progress entry for the given run. Supports `Last-Event-Id` (resume)
# by skipping events until the cursor is found. Closes the stream
# when the run reaches a terminal status or the connection times out.

import asyncio as _asyncio

_SSE_TAIL_INTERVAL_SECONDS = 1.0
_SSE_MAX_DURATION_SECONDS = 60 * 60  # 1 hour — clients reconnect after.
_TERMINAL_PROGRESS_TYPES = frozenset({
    "run.completed", "run.failed",
})


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
