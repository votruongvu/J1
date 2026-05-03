"""Tests for the External Integration Layer.

Verifies:
1. Default port implementations call the underlying J1 services.
2. DTO converters produce the right shapes.
3. Dependency direction stays clean — core modules don't import from
   `j1.api` or `j1.integration`.
"""

import ast
import io
from datetime import datetime, timezone
from pathlib import Path

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.audit.sink import AUDIT_LOG_FILENAME
from j1.documents.models import DocumentRecord
from j1.errors.exceptions import DocumentNotFoundError
from j1.integration import (
    AnswerRequestDTO,
    AnswerService,
    ApplicationFacade,
    CitationLookupService,
    DocumentIngestionService,
    EventDTO,
    EventPublisherService,
    FeedbackDTO,
    FeedbackRecord,
    FeedbackService,
    JsonlFeedbackStore,
    RetrievalService,
    SearchService,
    SourceLookupService,
)
from j1.integration.feedback import (
    FEEDBACK_FILENAME,
    TARGET_KIND_ARTIFACT,
)
from j1.integration.ports import (
    AnswerPort,
    CitationLookupPort,
    DocumentIngestionPort,
    EventPublisherPort,
    FeedbackPort,
    JobStatusPort,
    RetrievalPort,
    SearchPort,
    SourceLookupPort,
)
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.profiles import DEFAULT_PROFILE_ID, ProfileLoader
from j1.query.classifier import QueryIntentClassifier
from j1.query.engine import HybridQueryEngine
from j1.query.providers import (
    ConsistencyProvider,
    EvidenceProvider,
    GraphQueryProvider,
    KnowledgeQueryProvider,
    ReportGenerator,
)
from j1.search.indexer import SqliteSearchIndexer
from j1.workspace.layout import WorkspaceArea


# ---- Helpers --------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _stage_artifact(
    workspace,
    ctx,
    artifact_registry,
    *,
    artifact_id: str = "art-1",
    kind: str = "compiled.text",
    content: bytes = b"hello world",
    area: WorkspaceArea = WorkspaceArea.COMPILED,
    suffix: str = ".txt",
    source_document_ids: list[str] | None = None,
):
    area_dir = workspace.area(ctx, area)
    area_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{artifact_id}{suffix}"
    (area_dir / stored).write_bytes(content)
    record = ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=f"{area.value}/{stored}",
        content_hash=f"sha256:{artifact_id}",
        byte_size=len(content),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_now(),
        updated_at=_now(),
        source_document_ids=source_document_ids or [],
    )
    artifact_registry.add(record)
    return record


def _stage_document(ctx, registry, document_id: str = "doc-1") -> DocumentRecord:
    record = DocumentRecord(
        document_id=document_id,
        project=ctx,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.PENDING,
        created_at=_now(),
    )
    registry.add(record)
    return record


@pytest.fixture
def search_indexer(workspace, artifact_registry, registry):
    return SqliteSearchIndexer(workspace, artifact_registry, registry)


@pytest.fixture
def query_engine(workspace, artifact_registry, registry, search_indexer):
    profile = ProfileLoader().load(DEFAULT_PROFILE_ID)
    return HybridQueryEngine(
        classifier=QueryIntentClassifier(),
        knowledge_provider=KnowledgeQueryProvider(search_indexer),
        graph_provider=GraphQueryProvider(artifact_registry, workspace),
        evidence_provider=EvidenceProvider(search_indexer, registry),
        consistency_provider=ConsistencyProvider(artifact_registry, workspace),
        report_generator=ReportGenerator(search_indexer, profile),
    )


@pytest.fixture
def feedback_store(workspace) -> JsonlFeedbackStore:
    return JsonlFeedbackStore(workspace)


# ---- Port satisfaction (structural typing) -------------------------------


def test_default_implementations_satisfy_their_ports(
    intake_service,
    artifact_registry,
    registry,
    search_indexer,
    query_engine,
    feedback_store,
    audit_recorder,
):
    """The default service classes structurally implement their Protocols."""
    ingestion: DocumentIngestionPort = DocumentIngestionService(intake_service)
    search: SearchPort = SearchService(search_indexer)
    retrieval: RetrievalPort = RetrievalService(artifact_registry)
    answer: AnswerPort = AnswerService(query_engine)
    citations: CitationLookupPort = CitationLookupService(artifact_registry)
    sources: SourceLookupPort = SourceLookupService(registry)
    feedback: FeedbackPort = FeedbackService(feedback_store, audit_recorder)
    events: EventPublisherPort = EventPublisherService(audit_recorder)
    # No assert needed — failing structural check would raise at type-check
    # time. Runtime sanity: all are usable callables.
    assert callable(ingestion.register_document)
    assert callable(search.search)
    assert callable(retrieval.get_artifact)
    assert callable(answer.answer)
    assert callable(citations.get_citations)
    assert callable(sources.get_source)
    assert callable(feedback.submit_feedback)
    assert callable(events.publish_event)


# ---- DocumentIngestionService --------------------------------------------


def test_document_ingestion_service_delegates_to_intake(
    intake_service, ctx
):
    service = DocumentIngestionService(intake_service)
    dto = service.register_document(
        ctx,
        io.BytesIO(b"hello"),
        original_filename="paper.txt",
        mime_type="text/plain",
    )
    assert dto.document_id
    assert dto.tenant_id == "acme"
    assert dto.project_id == "alpha"
    assert dto.original_filename == "paper.txt"
    assert dto.checksum.startswith("sha256:")


# ---- SearchService -------------------------------------------------------


def test_search_service_returns_search_hits(
    workspace, ctx, artifact_registry, search_indexer
):
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"the schedule constraint is firm",
    )
    search_indexer.index(ctx, ["a-1"])
    service = SearchService(search_indexer)
    hits = service.search(ctx, "schedule")
    assert len(hits) == 1
    assert hits[0].artifact_id == "a-1"
    assert hits[0].artifact_type == "compiled.text"


def test_search_service_filter_by_kind(
    workspace, ctx, artifact_registry, search_indexer
):
    _stage_artifact(workspace, ctx, artifact_registry, artifact_id="c-1", content=b"x")
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="e-1", kind="enriched.requirements",
        content=b"x", area=WorkspaceArea.ENRICHED,
    )
    search_indexer.index(ctx, ["c-1", "e-1"])
    service = SearchService(search_indexer)
    hits = service.search(ctx, "x", artifact_types=["enriched.requirements"])
    assert {h.artifact_id for h in hits} == {"e-1"}


# ---- RetrievalService ----------------------------------------------------


def test_retrieval_service_returns_artifact_dto(
    workspace, ctx, artifact_registry
):
    _stage_artifact(workspace, ctx, artifact_registry, artifact_id="a-1")
    service = RetrievalService(artifact_registry)
    dto = service.get_artifact(ctx, "a-1")
    assert dto.artifact_id == "a-1"
    assert dto.location == "compiled/a-1.txt"
    assert dto.tenant_id == "acme"


def test_retrieval_service_list_filters_by_kind(
    workspace, ctx, artifact_registry
):
    _stage_artifact(workspace, ctx, artifact_registry, artifact_id="c-1")
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="e-1", kind="enriched.requirements",
        area=WorkspaceArea.ENRICHED,
    )
    service = RetrievalService(artifact_registry)
    listed = service.list_artifacts(ctx, kind="enriched.requirements")
    assert {a.artifact_id for a in listed} == {"e-1"}


# ---- AnswerService -------------------------------------------------------


def test_answer_service_returns_answer_dto(
    workspace, ctx, artifact_registry, search_indexer, query_engine
):
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"the schedule constraint is firm",
    )
    search_indexer.index(ctx, ["a-1"])
    service = AnswerService(query_engine)
    dto = service.answer(ctx, AnswerRequestDTO(question="schedule"))
    assert dto.answer
    assert dto.mode_used in {"knowledge_first", "graph_first"}
    assert dto.confidence_level in {"high", "medium", "low", "ambiguous"}
    assert isinstance(dto.warnings, list)
    assert isinstance(dto.warning_categories, list)


def test_answer_service_explicit_mode(
    workspace, ctx, artifact_registry, query_engine
):
    service = AnswerService(query_engine)
    dto = service.answer(
        ctx,
        AnswerRequestDTO(question="status", mode="consistency_check"),
    )
    assert dto.mode_used == "consistency_check"


def test_answer_service_invalid_mode_raises(query_engine, ctx):
    service = AnswerService(query_engine)
    with pytest.raises(ValueError):
        service.answer(ctx, AnswerRequestDTO(question="x", mode="bogus"))


# ---- CitationLookupService -----------------------------------------------


def test_citation_lookup_returns_source_documents(
    workspace, ctx, artifact_registry
):
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", source_document_ids=["doc-A", "doc-B"],
    )
    service = CitationLookupService(artifact_registry)
    citations = service.get_citations(ctx, "a-1")
    assert {c.source_document_id for c in citations} == {"doc-A", "doc-B"}
    assert all(c.artifact_id == "a-1" for c in citations)


def test_citation_lookup_returns_self_when_no_sources(
    workspace, ctx, artifact_registry
):
    """Artifact with no source documents → still returns one citation entry
    pointing at the artifact itself, so callers always get a non-empty list."""
    _stage_artifact(workspace, ctx, artifact_registry, artifact_id="a-1")
    service = CitationLookupService(artifact_registry)
    citations = service.get_citations(ctx, "a-1")
    assert len(citations) == 1
    assert citations[0].source_document_id is None


# ---- SourceLookupService -------------------------------------------------


def test_source_lookup_returns_document_dto(ctx, registry):
    _stage_document(ctx, registry, document_id="doc-1")
    service = SourceLookupService(registry)
    dto = service.get_source(ctx, "doc-1")
    assert dto.document_id == "doc-1"
    assert dto.original_filename == "doc-1.pdf"


def test_source_lookup_missing_raises(ctx, registry):
    service = SourceLookupService(registry)
    with pytest.raises(DocumentNotFoundError):
        service.get_source(ctx, "missing")


# ---- FeedbackService -----------------------------------------------------


def test_feedback_service_persists_and_audits(
    workspace, ctx, feedback_store, audit_recorder
):
    service = FeedbackService(feedback_store, audit_recorder)
    result = service.submit_feedback(
        ctx,
        FeedbackDTO(
            target_kind=TARGET_KIND_ARTIFACT,
            target_id="art-1",
            rating=1,
            comment="useful",
            actor="user@example.com",
        ),
    )
    assert result.feedback_id
    # Persisted to the JSONL store.
    stored = feedback_store.list_for(ctx, target_kind=TARGET_KIND_ARTIFACT)
    assert len(stored) == 1
    assert stored[0].rating == 1
    # Audit event recorded.
    import json
    line = (workspace.audit(ctx) / AUDIT_LOG_FILENAME).read_text().splitlines()[-1]
    assert json.loads(line)["action"] == "j1.feedback.submitted"


def test_feedback_service_works_without_audit(
    ctx, feedback_store
):
    service = FeedbackService(feedback_store, audit=None)
    result = service.submit_feedback(
        ctx,
        FeedbackDTO(
            target_kind=TARGET_KIND_ARTIFACT, target_id="x", rating=-1,
        ),
    )
    assert result.feedback_id


def test_feedback_store_filters_by_target(workspace, ctx, feedback_store):
    feedback_store.add(
        FeedbackRecord(
            feedback_id="f-1", project=ctx,
            target_kind="artifact", target_id="a-1",
            submitted_at=_now(),
        )
    )
    feedback_store.add(
        FeedbackRecord(
            feedback_id="f-2", project=ctx,
            target_kind="query", target_id="q-1",
            submitted_at=_now(),
        )
    )
    by_kind = feedback_store.list_for(ctx, target_kind="artifact")
    assert {r.feedback_id for r in by_kind} == {"f-1"}


def test_feedback_log_lives_in_runtime(workspace, ctx, feedback_store):
    feedback_store.add(
        FeedbackRecord(
            feedback_id="f-1", project=ctx,
            target_kind="artifact", target_id="a-1",
            submitted_at=_now(),
        )
    )
    expected = workspace.runtime(ctx) / FEEDBACK_FILENAME
    assert expected.is_file()


# ---- EventPublisherService -----------------------------------------------


def test_event_publisher_writes_audit(workspace, ctx, audit_recorder):
    service = EventPublisherService(audit_recorder)
    result = service.publish_event(
        ctx,
        EventDTO(
            actor="system",
            action="external.something_happened",
            target_kind="thing",
            target_id="t-1",
            payload={"k": "v"},
            correlation_id="run-1",
        ),
    )
    assert result.event_id
    import json
    line = (workspace.audit(ctx) / AUDIT_LOG_FILENAME).read_text().splitlines()[-1]
    parsed = json.loads(line)
    assert parsed["action"] == "external.something_happened"
    assert parsed["correlation_id"] == "run-1"


# ---- ApplicationFacade ---------------------------------------------------


def test_application_facade_holds_all_required_ports(
    intake_service, artifact_registry, registry, query_engine,
    feedback_store, audit_recorder, search_indexer,
):
    facade = ApplicationFacade(
        ingestion=DocumentIngestionService(intake_service),
        retrieval=RetrievalService(artifact_registry),
        citation_lookup=CitationLookupService(artifact_registry),
        source_lookup=SourceLookupService(registry),
        feedback=FeedbackService(feedback_store, audit_recorder),
        event_publisher=EventPublisherService(audit_recorder),
        search=SearchService(search_indexer),
        answer=AnswerService(query_engine),
        # job_status omitted — optional.
    )
    assert facade.ingestion is not None
    assert facade.retrieval is not None
    assert facade.search is not None
    assert facade.answer is not None
    assert facade.job_status is None


def test_application_facade_optional_ports_can_be_none(
    intake_service, artifact_registry, registry, feedback_store, audit_recorder,
):
    """Deployments without Temporal / FTS5 / profile drop the optional ports."""
    facade = ApplicationFacade(
        ingestion=DocumentIngestionService(intake_service),
        retrieval=RetrievalService(artifact_registry),
        citation_lookup=CitationLookupService(artifact_registry),
        source_lookup=SourceLookupService(registry),
        feedback=FeedbackService(feedback_store, audit_recorder),
        event_publisher=EventPublisherService(audit_recorder),
    )
    assert facade.job_status is None
    assert facade.search is None
    assert facade.answer is None


# ---- Dependency direction guard ------------------------------------------


_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "j1"
_FORBIDDEN_PREFIXES = ("j1.api", "j1.integration")
_BOUNDARY_PACKAGES = {"api", "integration"}


def _collect_imported_modules(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module)
    return found


def test_core_modules_do_not_import_external_layer():
    """Core J1 modules must not depend on `j1.api` or `j1.integration`.

    The dependency arrow points only outward: external adapters depend on
    integration ports, integration depends on core. A core file pulling
    something from `j1.api` or `j1.integration` would invert that.

    Exception: the top-level `src/j1/__init__.py` re-exports the public
    surface, which legitimately includes integration + api types. That's
    the package facade, not a core module.
    """
    offenders: list[tuple[Path, str]] = []
    for py_file in _SRC_ROOT.rglob("*.py"):
        rel = py_file.relative_to(_SRC_ROOT)
        if rel.parts and rel.parts[0] in _BOUNDARY_PACKAGES:
            continue
        if "__pycache__" in rel.parts:
            continue
        if rel == Path("__init__.py"):
            continue
        for imp in _collect_imported_modules(py_file):
            for forbidden in _FORBIDDEN_PREFIXES:
                if imp == forbidden or imp.startswith(forbidden + "."):
                    offenders.append((rel, imp))
    assert not offenders, (
        "core modules must not import from the external integration layer:\n"
        + "\n".join(f"  {p}: imports {imp}" for p, imp in offenders)
    )


def test_integration_does_not_import_protocol_adapters():
    """`j1.integration` defines ports — it must not import any protocol
    adapter (such as `j1.api`). Adapters depend on integration, not the reverse.
    """
    integration_root = _SRC_ROOT / "integration"
    offenders: list[tuple[Path, str]] = []
    for py_file in integration_root.rglob("*.py"):
        rel = py_file.relative_to(_SRC_ROOT)
        if "__pycache__" in rel.parts:
            continue
        for imp in _collect_imported_modules(py_file):
            if imp == "j1.api" or imp.startswith("j1.api."):
                offenders.append((rel, imp))
    assert not offenders, (
        "j1.integration must not import from j1.api:\n"
        + "\n".join(f"  {p}: imports {imp}" for p, imp in offenders)
    )
