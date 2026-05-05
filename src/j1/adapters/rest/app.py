import json
import uuid
from collections.abc import Awaitable, Callable
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
    HealthRecord,
    IngestRequest,
    JobActionRecord,
    JobEventRecord,
    JobEventsRecord,
    JobStartRecord,
    JobStatusRecord,
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
      * Anonymous paths (`/health` / `/version`) keep their `security: []`
        marker so Swagger doesn't pretend they need auth.

    Implementation: we wrap `app.openapi` (the bound method) in a
    function that post-processes the schema. Reassigning to
    `app.openapi` works as a Python instance-attribute override, but
    FastAPI's setup-time route closure for `/openapi.json` resolves
    `self.openapi` at request time too, so the wrapper takes effect
    on every spec fetch."""
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
    # Capture the original openapi-generation method up front. Storing
    # the bound method (rather than calling `app.openapi` at wrap time)
    # avoids any lookup ambiguity later — we always have a known-good
    # generator to defer to.
    _original_openapi = app.openapi

    def _custom_openapi() -> dict[str, object]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = _original_openapi()
        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        security_schemes["bearer"] = bearer_scheme
        security_schemes["api_key"] = api_key_scheme

        # Apply security globally, then strip it on anonymous paths.
        # Either scheme satisfies — Swagger renders both as choices.
        global_security = [{"bearer": []}, {"api_key": []}]
        schema["security"] = global_security
        paths = schema.get("paths", {})
        for path, operations in paths.items():
            if path in anonymous_paths:
                for op in operations.values():
                    if isinstance(op, dict):
                        op["security"] = []
        app.openapi_schema = schema
        return schema

    app.openapi = _custom_openapi  # type: ignore[method-assign]


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
            else frozenset({"/health", "/version"})
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
