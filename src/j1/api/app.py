import uuid
from collections.abc import Callable

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile

from j1.api.schemas import (
    ArtifactListResponse,
    ArtifactSummary,
    CostSummaryResponse,
    DocumentResponse,
    GraphPathResponse,
    ProjectCreateRequest,
    ProjectResponse,
    QueryEndpointRequest,
    QueryEndpointResponse,
    ReviewDecisionRequest,
    ReviewDecisionResponse,
    ReviewItemResponse,
    ReviewListResponse,
    SourceReferenceResponse,
    StartProcessingRequest,
    StartProcessingResponse,
    WorkflowActionResponse,
    WorkflowStatusResponse,
)
from temporalio.exceptions import ApplicationError

from j1.api.services import ServiceContainer
from j1.artifacts.registry import ArtifactNotFoundError
from j1.cost.aggregator import CostAggregator
from j1.errors.exceptions import (
    DuplicateDocumentError,
    InvalidIdentifierError,
)
from j1.orchestration.activities.payloads import (
    ApplyReviewDecisionInput,
    ProjectScope,
)
from j1.orchestration.workflows.project_processing import (
    ProjectProcessingRequest,
    ProjectProcessingWorkflow,
)
from j1.projects.context import ProjectContext
from j1.query.models import QueryMode, QueryRequest
from j1.review.queue import ReviewItemNotFoundError

TENANT_HEADER = "X-Tenant-Id"
WORKFLOW_QUERY_NAME = "get_status"


TenantResolver = Callable[[Request], str]


def _default_tenant_resolver(request: Request) -> str:
    tenant_id = request.headers.get(TENANT_HEADER)
    if not tenant_id:
        raise HTTPException(
            status_code=400, detail=f"{TENANT_HEADER} header required"
        )
    return tenant_id


def create_api(
    container: ServiceContainer,
    *,
    tenant_resolver: TenantResolver | None = None,
) -> FastAPI:
    app = FastAPI(title="J1 Knowledge API", version="0.1.0")
    resolver = tenant_resolver or _default_tenant_resolver

    def get_tenant(request: Request) -> str:
        return resolver(request)

    def get_ctx(project_id: str, tenant_id: str = Depends(get_tenant)) -> ProjectContext:
        try:
            return ProjectContext(tenant_id=tenant_id, project_id=project_id)
        except InvalidIdentifierError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def require_temporal():
        if container.temporal_client is None:
            raise HTTPException(
                status_code=503,
                detail="Temporal client not configured for this API",
            )
        return container.temporal_client

    # ---- Projects ---------------------------------------------------

    @app.post("/api/projects", response_model=ProjectResponse)
    def create_project(
        request: ProjectCreateRequest,
        tenant_id: str = Depends(get_tenant),
    ) -> ProjectResponse:
        try:
            ctx = ProjectContext(
                tenant_id=tenant_id,
                project_id=request.project_id,
                profile=request.profile,
            )
        except InvalidIdentifierError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        container.workspace.ensure(ctx)
        return ProjectResponse(
            project_id=ctx.project_id,
            tenant_id=ctx.tenant_id,
            profile=ctx.profile,
        )

    # ---- Documents --------------------------------------------------

    @app.post(
        "/api/projects/{project_id}/documents",
        response_model=DocumentResponse,
    )
    def upload_document(
        project_id: str,
        file: UploadFile = File(...),
        tenant_id: str = Depends(get_tenant),
    ) -> DocumentResponse:
        try:
            ctx = ProjectContext(tenant_id=tenant_id, project_id=project_id)
        except InvalidIdentifierError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            record = container.intake_service.register_from_stream(
                ctx,
                file.file,
                original_filename=file.filename or "upload.bin",
                mime_type=file.content_type,
            )
        except DuplicateDocumentError as exc:
            existing = container.source_registry.get(ctx, exc.existing_document_id)
            return DocumentResponse.from_record(existing, duplicate=True)
        return DocumentResponse.from_record(record)

    # ---- Processing -------------------------------------------------

    @app.post(
        "/api/projects/{project_id}/processing",
        response_model=StartProcessingResponse,
    )
    async def start_processing(
        project_id: str,
        request: StartProcessingRequest,
        ctx: ProjectContext = Depends(get_ctx),
        client=Depends(require_temporal),
    ) -> StartProcessingResponse:
        scope = ProjectScope.from_context(ctx)
        workflow_request = ProjectProcessingRequest(
            scope=scope,
            compiler_kind=request.compiler_kind,
            enricher_kind=request.enricher_kind,
            graph_builder_kind=request.graph_builder_kind,
            indexer_kind=request.indexer_kind,
            budget_limit_amount=request.budget_limit_amount,
            budget_currency=request.budget_currency,
            review_after=tuple(request.review_after),
            actor=request.actor,
            correlation_id=request.correlation_id,
        )
        workflow_id = (
            f"j1-{ctx.tenant_id}-{ctx.project_id}-{uuid.uuid4().hex[:12]}"
        )
        await client.start_workflow(
            ProjectProcessingWorkflow.run,
            workflow_request,
            id=workflow_id,
            task_queue=container.temporal_task_queue,
        )
        return StartProcessingResponse(
            workflow_id=workflow_id, project_id=ctx.project_id
        )

    @app.get(
        "/api/projects/{project_id}/processing/{workflow_id}",
        response_model=WorkflowStatusResponse,
    )
    async def get_workflow_status(
        project_id: str,
        workflow_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        client=Depends(require_temporal),
    ) -> WorkflowStatusResponse:
        handle = client.get_workflow_handle(workflow_id)
        status = await handle.query(WORKFLOW_QUERY_NAME)
        return _status_to_response(workflow_id, ctx.project_id, status)

    @app.post(
        "/api/projects/{project_id}/processing/{workflow_id}/pause",
        response_model=WorkflowActionResponse,
    )
    async def pause_workflow(
        project_id: str,
        workflow_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        client=Depends(require_temporal),
    ) -> WorkflowActionResponse:
        await _signal(client, workflow_id, "pause")
        return WorkflowActionResponse(workflow_id=workflow_id, action="pause")

    @app.post(
        "/api/projects/{project_id}/processing/{workflow_id}/resume",
        response_model=WorkflowActionResponse,
    )
    async def resume_workflow(
        project_id: str,
        workflow_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        client=Depends(require_temporal),
    ) -> WorkflowActionResponse:
        await _signal(client, workflow_id, "resume")
        return WorkflowActionResponse(workflow_id=workflow_id, action="resume")

    @app.post(
        "/api/projects/{project_id}/processing/{workflow_id}/cancel",
        response_model=WorkflowActionResponse,
    )
    async def cancel_workflow(
        project_id: str,
        workflow_id: str,
        ctx: ProjectContext = Depends(get_ctx),
        client=Depends(require_temporal),
    ) -> WorkflowActionResponse:
        await _signal(client, workflow_id, "cancel")
        return WorkflowActionResponse(workflow_id=workflow_id, action="cancel")

    # ---- Artifacts --------------------------------------------------

    @app.get(
        "/api/projects/{project_id}/artifacts",
        response_model=ArtifactListResponse,
    )
    def list_artifacts(
        project_id: str,
        kind: str | None = Query(default=None),
        ctx: ProjectContext = Depends(get_ctx),
    ) -> ArtifactListResponse:
        records = container.artifact_registry.list_artifacts(ctx, kind=kind)
        return ArtifactListResponse(
            artifacts=[ArtifactSummary.from_record(r) for r in records],
        )

    @app.get(
        "/api/projects/{project_id}/artifacts/{artifact_id}",
        response_model=ArtifactSummary,
    )
    def get_artifact(
        project_id: str,
        artifact_id: str,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> ArtifactSummary:
        try:
            record = container.artifact_registry.get(ctx, artifact_id)
        except ArtifactNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return ArtifactSummary.from_record(record)

    # ---- Query ------------------------------------------------------

    @app.post(
        "/api/projects/{project_id}/query",
        response_model=QueryEndpointResponse,
    )
    def query_project(
        project_id: str,
        request: QueryEndpointRequest,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> QueryEndpointResponse:
        if container.query_engine is None:
            raise HTTPException(
                status_code=503,
                detail="Query engine not configured (search index unavailable?)",
            )
        try:
            mode = QueryMode(request.mode)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"unknown query mode: {request.mode!r}"
            ) from exc
        query_request = QueryRequest(
            question=request.question,
            mode=mode,
            max_results=request.max_results,
            artifact_types=list(request.artifact_types),
        )
        response = container.query_engine.query(ctx, query_request)
        return QueryEndpointResponse(
            answer=response.answer,
            mode_used=response.mode_used,
            sources=[
                SourceReferenceResponse(
                    artifact_id=s.artifact_id,
                    artifact_type=s.artifact_type,
                    title=s.title,
                    source_document_id=s.source_document_id,
                    source_location=s.source_location,
                )
                for s in response.sources
            ],
            related_artifacts=list(response.related_artifacts),
            graph_paths=[
                GraphPathResponse(
                    nodes=list(p.nodes),
                    edges=list(p.edges),
                    description=p.description,
                )
                for p in response.graph_paths
            ],
            confidence=response.confidence,
            confidence_level=response.confidence_level.value,
            review_required=response.review_required,
            warnings=list(response.warnings),
            warning_categories=[c.value for c in response.warning_categories],
        )

    # ---- Cost -------------------------------------------------------

    @app.get(
        "/api/projects/{project_id}/cost",
        response_model=CostSummaryResponse,
    )
    def get_cost_summary(
        project_id: str,
        correlation_id: str | None = Query(default=None),
        document_id: str | None = Query(default=None),
        query_id: str | None = Query(default=None),
        ctx: ProjectContext = Depends(get_ctx),
    ) -> CostSummaryResponse:
        agg: CostAggregator = container.cost_aggregator
        total = agg.aggregate(
            ctx,
            correlation_id=correlation_id,
            document_id=document_id,
            query_id=query_id,
        )
        by_level = agg.by_levels(
            ctx,
            correlation_id=correlation_id,
            document_id=document_id,
            query_id=query_id,
        )
        return CostSummaryResponse(
            project_id=ctx.project_id,
            tenant_id=ctx.tenant_id,
            total_amount=str(total),
            by_level={
                level.value: str(amount) for level, amount in by_level.items()
            },
        )

    # ---- Reviews ----------------------------------------------------

    @app.get(
        "/api/projects/{project_id}/reviews",
        response_model=ReviewListResponse,
    )
    def list_reviews(
        project_id: str,
        pending_only: bool = Query(default=True),
        ctx: ProjectContext = Depends(get_ctx),
    ) -> ReviewListResponse:
        if pending_only:
            items = container.review_queue.list_pending(ctx)
        else:
            items = container.review_queue.list_items(ctx)
        return ReviewListResponse(
            items=[ReviewItemResponse.from_item(i) for i in items],
        )

    @app.post(
        "/api/projects/{project_id}/reviews/{review_id}/decision",
        response_model=ReviewDecisionResponse,
    )
    def submit_review_decision(
        project_id: str,
        review_id: str,
        request: ReviewDecisionRequest,
        ctx: ProjectContext = Depends(get_ctx),
    ) -> ReviewDecisionResponse:
        try:
            result = container.review_activities.apply_review_decision_activity(
                ApplyReviewDecisionInput(
                    scope=ProjectScope.from_context(ctx),
                    review_item_id=review_id,
                    decision=request.decision,
                    actor=request.actor,
                    notes=request.notes,
                    correlation_id=request.correlation_id,
                )
            )
        except ReviewItemNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ApplicationError as exc:
            # Activity raises ApplicationError(non_retryable=True) for unknown
            # decision strings — that's a client error, not a server error.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ReviewDecisionResponse(
            review_item_id=result.review_item_id,
            review_status=result.review_status,
            audit_event_id=result.audit_event_id,
        )

    return app


# ---- Helpers ----------------------------------------------------------


async def _signal(client, workflow_id: str, signal_name: str) -> None:
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal(signal_name)


def _status_to_response(
    workflow_id: str, project_id: str, status
) -> WorkflowStatusResponse:
    """Translate WorkflowStatus into the API-facing response shape."""
    return WorkflowStatusResponse(
        workflow_id=workflow_id,
        project_id=project_id,
        state=getattr(status, "state", "unknown"),
        current_operation=getattr(status, "current_operation", None),
        pending_operation=getattr(status, "pending_operation", None),
        completed_operations=list(getattr(status, "completed_operations", [])),
        documents_total=int(getattr(status, "documents_total", 0)),
        documents_completed=int(getattr(status, "documents_completed", 0)),
        produced_artifact_ids=list(
            getattr(status, "produced_artifact_ids", [])
        ),
        review_required=bool(getattr(status, "review_required", False)),
        review_gate=getattr(status, "review_gate", None),
        budget_approval_required=bool(
            getattr(status, "budget_approval_required", False)
        ),
        error=getattr(status, "error", None),
    )
