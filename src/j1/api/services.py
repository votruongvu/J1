from dataclasses import dataclass, field
from typing import Any

from j1.artifacts.registry import ArtifactRegistry, JsonArtifactRegistry
from j1.audit.recorder import AuditRecorder, DefaultAuditRecorder
from j1.audit.sink import AuditSink, JsonlAuditSink
from j1.config.settings import Settings
from j1.cost.aggregator import CostAggregator
from j1.cost.recorder import CostRecorder, DefaultCostRecorder
from j1.cost.sink import CostSink, JsonlCostSink
from j1.errors.exceptions import SearchIndexerError
from j1.intake.registry import JsonSourceRegistry, SourceRegistry
from j1.intake.service import DocumentIntakeService
from j1.orchestration.activities.review import ReviewActivities
from j1.processing.service import ProcessingService
from j1.profiles import DEFAULT_PROFILE_ID, Profile, ProfileLoader
from j1.query.classifier import QueryIntentClassifier
from j1.query.engine import HybridQueryEngine
from j1.query.providers import (
    ConsistencyProvider,
    EvidenceProvider,
    GraphQueryProvider,
    KnowledgeQueryProvider,
    ReportGenerator,
)
from j1.review.queue import JsonReviewQueue, ReviewQueue
from j1.search.indexer import SqliteSearchIndexer
from j1.workspace.resolver import WorkspaceResolver

DEFAULT_TASK_QUEUE = "j1-default"


@dataclass
class ServiceContainer:
    """Holds the wired-up J1 services that the API layer dispatches to.

    `temporal_client` is optional — endpoints that need it raise 503 when
    it's None, so the API works for everything except workflow start/control
    when no Temporal server is configured (useful for tests and local use).
    """

    settings: Settings
    workspace: WorkspaceResolver
    audit_sink: AuditSink
    audit_recorder: AuditRecorder
    cost_sink: CostSink
    cost_recorder: CostRecorder
    intake_service: DocumentIntakeService
    processing_service: ProcessingService
    source_registry: SourceRegistry
    artifact_registry: ArtifactRegistry
    review_queue: ReviewQueue
    review_activities: ReviewActivities
    cost_aggregator: CostAggregator
    profile: Profile | None = None
    search_indexer: SqliteSearchIndexer | None = None
    query_engine: HybridQueryEngine | None = None
    temporal_client: Any | None = None
    temporal_task_queue: str = DEFAULT_TASK_QUEUE
    extras: dict[str, Any] = field(default_factory=dict)


def build_default_container(
    settings: Settings,
    *,
    profile: Profile | None = None,
    temporal_client: Any | None = None,
    temporal_task_queue: str = DEFAULT_TASK_QUEUE,
) -> ServiceContainer:
    workspace = WorkspaceResolver(settings)

    audit_sink: AuditSink = JsonlAuditSink(workspace)
    audit_recorder: AuditRecorder = DefaultAuditRecorder(audit_sink)
    cost_sink: CostSink = JsonlCostSink(workspace)
    cost_recorder: CostRecorder = DefaultCostRecorder(cost_sink)

    source_registry: SourceRegistry = JsonSourceRegistry(workspace)
    artifact_registry: ArtifactRegistry = JsonArtifactRegistry(workspace)
    review_queue: ReviewQueue = JsonReviewQueue(workspace)

    intake_service = DocumentIntakeService(
        workspace=workspace,
        registry=source_registry,
        audit_sink=audit_sink,
    )
    processing_service = ProcessingService(
        workspace=workspace,
        artifact_registry=artifact_registry,
        audit=audit_recorder,
        cost=cost_recorder,
    )
    review_activities = ReviewActivities(
        review_queue=review_queue,
        audit=audit_recorder,
    )
    cost_aggregator = CostAggregator(workspace)

    if profile is None:
        try:
            profile = ProfileLoader().load(DEFAULT_PROFILE_ID)
        except Exception:
            profile = None

    search_indexer: SqliteSearchIndexer | None = None
    try:
        search_indexer = SqliteSearchIndexer(
            workspace=workspace,
            artifacts=artifact_registry,
            sources=source_registry,
        )
    except SearchIndexerError:
        search_indexer = None

    query_engine: HybridQueryEngine | None = None
    if search_indexer is not None and profile is not None:
        query_engine = HybridQueryEngine(
            classifier=QueryIntentClassifier(),
            knowledge_provider=KnowledgeQueryProvider(search_indexer),
            graph_provider=GraphQueryProvider(artifact_registry, workspace),
            evidence_provider=EvidenceProvider(search_indexer, source_registry),
            consistency_provider=ConsistencyProvider(artifact_registry, workspace),
            report_generator=ReportGenerator(search_indexer, profile),
        )

    return ServiceContainer(
        settings=settings,
        workspace=workspace,
        audit_sink=audit_sink,
        audit_recorder=audit_recorder,
        cost_sink=cost_sink,
        cost_recorder=cost_recorder,
        intake_service=intake_service,
        processing_service=processing_service,
        source_registry=source_registry,
        artifact_registry=artifact_registry,
        review_queue=review_queue,
        review_activities=review_activities,
        cost_aggregator=cost_aggregator,
        profile=profile,
        search_indexer=search_indexer,
        query_engine=query_engine,
        temporal_client=temporal_client,
        temporal_task_queue=temporal_task_queue,
    )
