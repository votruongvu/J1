import json
from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.documents.models import DocumentRecord
from j1.enrichers import ARTIFACT_TYPE_CONSISTENCY_FINDINGS
from j1.errors.exceptions import QueryRoutingError
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.profiles import DEFAULT_PROFILE_ID, Profile, ProfileLoader
from j1.query import (
    ConsistencyProvider,
    EvidenceProvider,
    GraphPath,
    GraphQueryProvider,
    HybridQueryEngine,
    KnowledgeQueryProvider,
    QueryIntentClassifier,
    QueryMode,
    QueryRequest,
    QueryResponse,
    ReportGenerator,
    SourceReference,
)
from j1.search import SqliteSearchIndexer
from j1.workspace.layout import WorkspaceArea


# ---- Helpers -----------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _stage_artifact(
    workspace,
    ctx,
    artifact_registry,
    *,
    artifact_id: str,
    kind: str = "compiled.text",
    content: bytes = b"",
    area: WorkspaceArea = WorkspaceArea.COMPILED,
    title: str | None = None,
    source_document_ids: list[str] | None = None,
    source_location: str | None = None,
    confidence: float | None = None,
    review_status: ReviewStatus = ReviewStatus.NOT_REQUIRED,
    metadata_extras: dict | None = None,
    suffix: str = ".txt",
) -> ArtifactRecord:
    area_dir = workspace.area(ctx, area)
    area_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{artifact_id}{suffix}"
    (area_dir / stored).write_bytes(content)
    metadata = dict(metadata_extras or {})
    if title:
        metadata["title"] = title
    if source_location:
        metadata["source_location"] = source_location
    if confidence is not None:
        metadata["confidence"] = confidence
    record = ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=f"{area.value}/{stored}",
        content_hash=f"sha256:{artifact_id}",
        byte_size=len(content),
        status=ProcessingStatus.SUCCEEDED,
        review_status=review_status,
        version=1,
        created_at=_now(),
        updated_at=_now(),
        source_document_ids=source_document_ids or [],
        metadata=metadata,
    )
    artifact_registry.add(record)
    return record


def _stage_document(ctx, registry, document_id="doc-1"):
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
def default_profile() -> Profile:
    return ProfileLoader().load(DEFAULT_PROFILE_ID)


@pytest.fixture
def query_engine(
    workspace,
    artifact_registry,
    registry,
    search_indexer,
    default_profile,
):
    return HybridQueryEngine(
        classifier=QueryIntentClassifier(),
        knowledge_provider=KnowledgeQueryProvider(search_indexer),
        graph_provider=GraphQueryProvider(artifact_registry, workspace),
        evidence_provider=EvidenceProvider(search_indexer, registry),
        consistency_provider=ConsistencyProvider(artifact_registry, workspace),
        report_generator=ReportGenerator(search_indexer, default_profile),
    )


# ---- Models -----------------------------------------------------------


def test_query_mode_values():
    assert {m.value for m in QueryMode} == {
        "auto",
        "knowledge_first",
        "graph_first",
        "evidence_first",
        "consistency_check",
        "report_generation",
    }


def test_query_request_defaults_to_auto():
    req = QueryRequest(question="anything")
    assert req.mode is QueryMode.AUTO
    assert req.max_results == 10


# ---- Classifier -------------------------------------------------------


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Give me a summary of the requirements", QueryMode.KNOWLEDGE_FIRST),
        ("What are the project requirements?", QueryMode.KNOWLEDGE_FIRST),
        ("List the risks", QueryMode.KNOWLEDGE_FIRST),
        ("What is the scope?", QueryMode.KNOWLEDGE_FIRST),
        ("Show relationship between A and B", QueryMode.GRAPH_FIRST),
        ("Which dependencies exist?", QueryMode.GRAPH_FIRST),
        ("Find a path from X to Y", QueryMode.GRAPH_FIRST),
        ("How does X connect to Y?", QueryMode.GRAPH_FIRST),
        ("Where is the constraint stated?", QueryMode.EVIDENCE_FIRST),
        ("What is the source for this claim?", QueryMode.EVIDENCE_FIRST),
        ("Verify the requirement", QueryMode.EVIDENCE_FIRST),
        ("Are there any conflicts?", QueryMode.CONSISTENCY_CHECK),
        ("Look for mismatches", QueryMode.CONSISTENCY_CHECK),
        ("Check consistency across documents", QueryMode.CONSISTENCY_CHECK),
        ("Generate a report", QueryMode.REPORT_GENERATION),
        ("Build a matrix of items", QueryMode.REPORT_GENERATION),
        ("Outline the deliverables", QueryMode.REPORT_GENERATION),
        ("Tell me something random", QueryMode.KNOWLEDGE_FIRST),
    ],
)
def test_classifier_routes(question, expected):
    assert QueryIntentClassifier().classify(question) == expected


# ---- KnowledgeQueryProvider -------------------------------------------


def test_knowledge_provider_returns_sources(
    search_indexer, workspace, ctx, artifact_registry
):
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"the schedule constraint is firm",
        title="Schedule constraint",
    )
    search_indexer.index(ctx, ["a-1"])
    provider = KnowledgeQueryProvider(search_indexer)
    response = provider.query(ctx, QueryRequest(question="schedule"))
    assert response.mode_used == QueryMode.KNOWLEDGE_FIRST.value
    assert len(response.sources) == 1
    assert response.sources[0].artifact_id == "a-1"
    assert "a-1" in response.related_artifacts


def test_knowledge_provider_sets_review_required_when_pending(
    search_indexer, workspace, ctx, artifact_registry
):
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"keyword content",
        review_status=ReviewStatus.PENDING,
    )
    search_indexer.index(ctx, ["a-1"])
    provider = KnowledgeQueryProvider(search_indexer)
    response = provider.query(ctx, QueryRequest(question="keyword"))
    assert response.review_required is True
    assert any("review" in w.lower() for w in response.warnings)


def test_knowledge_provider_empty_index(search_indexer, ctx):
    provider = KnowledgeQueryProvider(search_indexer)
    response = provider.query(ctx, QueryRequest(question="anything"))
    assert response.sources == []
    assert response.confidence == 0.0


# ---- GraphQueryProvider -----------------------------------------------


def _stage_graph(workspace, ctx, artifact_registry, *, edges):
    payload = json.dumps({"nodes": [], "edges": edges}).encode("utf-8")
    return _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="g-1", kind="graph_json", content=payload,
        area=WorkspaceArea.GRAPH, suffix=".json",
    )


def test_graph_provider_returns_paths(workspace, ctx, artifact_registry):
    _stage_graph(
        workspace, ctx, artifact_registry,
        edges=[
            {"from": "A", "to": "B", "type": "depends_on"},
            {"from": "B", "to": "C", "type": "blocks"},
        ],
    )
    provider = GraphQueryProvider(artifact_registry, workspace)
    response = provider.query(ctx, QueryRequest(question="dependencies"))
    assert response.mode_used == QueryMode.GRAPH_FIRST.value
    assert len(response.graph_paths) == 2
    assert response.graph_paths[0].nodes == ["A", "B"]
    assert response.graph_paths[0].edges == ["depends_on"]
    assert "g-1" in response.related_artifacts


def test_graph_provider_with_no_artifacts(workspace, ctx, artifact_registry):
    provider = GraphQueryProvider(artifact_registry, workspace)
    response = provider.query(ctx, QueryRequest(question="dependencies"))
    assert response.graph_paths == []
    assert any("no graph" in w.lower() for w in response.warnings)


def test_graph_provider_response_does_not_expose_paths(
    workspace, ctx, artifact_registry
):
    _stage_graph(workspace, ctx, artifact_registry, edges=[
        {"from": "A", "to": "B", "type": "rel"}
    ])
    provider = GraphQueryProvider(artifact_registry, workspace)
    response = provider.query(ctx, QueryRequest(question="anything"))
    # No filesystem path leaks anywhere in the rendered fields.
    rendered = response.answer + " ".join(s.title for s in response.sources)
    assert "/" not in response.answer.split("\n")[0]
    assert ".json" not in rendered.split("\n")[0]


# ---- EvidenceProvider --------------------------------------------------


def test_evidence_provider_returns_source_references(
    search_indexer, workspace, ctx, registry, artifact_registry
):
    _stage_document(ctx, registry, document_id="doc-1")
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"important section text",
        source_document_ids=["doc-1"],
        source_location="page-7",
    )
    search_indexer.index(ctx, ["a-1"])
    provider = EvidenceProvider(search_indexer, registry)
    response = provider.query(ctx, QueryRequest(question="important section"))
    assert response.mode_used == QueryMode.EVIDENCE_FIRST.value
    assert len(response.sources) == 1
    assert response.sources[0].source_document_id == "doc-1"
    assert response.sources[0].source_location == "page-7"


def test_evidence_provider_skips_hits_without_documents(
    search_indexer, workspace, ctx, registry, artifact_registry
):
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-no-doc", content=b"orphan section",
    )
    search_indexer.index(ctx, ["a-no-doc"])
    provider = EvidenceProvider(search_indexer, registry)
    response = provider.query(ctx, QueryRequest(question="orphan"))
    assert response.sources == []
    assert any("no evidence" in w.lower() for w in response.warnings)


# ---- ConsistencyProvider ----------------------------------------------


def _stage_consistency_finding(workspace, ctx, artifact_registry, findings):
    payload = json.dumps({"findings": findings}).encode("utf-8")
    return _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="c-1",
        kind=ARTIFACT_TYPE_CONSISTENCY_FINDINGS,
        content=payload,
        area=WorkspaceArea.ENRICHED,
        review_status=ReviewStatus.PENDING,
        suffix=".json",
        metadata_extras={"format": "json"},
    )


def test_consistency_provider_returns_warnings(
    workspace, ctx, artifact_registry
):
    _stage_consistency_finding(
        workspace, ctx, artifact_registry,
        findings=[
            {"description": "Section 4.1 contradicts Section 7.2"},
            {"description": "Requirement R-12 has two different deadlines"},
        ],
    )
    provider = ConsistencyProvider(artifact_registry, workspace)
    response = provider.query(ctx, QueryRequest(question="conflicts"))
    assert response.mode_used == QueryMode.CONSISTENCY_CHECK.value
    assert response.review_required is True
    assert any("section 4.1" in w.lower() for w in response.warnings)


def test_consistency_provider_with_no_findings_warns(
    workspace, ctx, artifact_registry
):
    provider = ConsistencyProvider(artifact_registry, workspace)
    response = provider.query(ctx, QueryRequest(question="conflicts"))
    assert response.review_required is True
    assert any("no consistency" in w.lower() for w in response.warnings)


# ---- ReportGenerator --------------------------------------------------


def test_report_generator_uses_profile_template(
    search_indexer, workspace, ctx, artifact_registry, default_profile
):
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"x", title="Alpha",
    )
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-2", content=b"y", title="Beta",
    )
    search_indexer.index(ctx, ["a-1", "a-2"])
    gen = ReportGenerator(search_indexer, default_profile)
    response = gen.query(ctx, QueryRequest(question="overall report"))
    assert response.mode_used == QueryMode.REPORT_GENERATION.value
    assert response.answer.startswith("# overall report")
    assert "Alpha" in response.answer
    assert "Beta" in response.answer


def test_report_generator_falls_back_when_no_template(
    search_indexer, workspace, ctx, artifact_registry
):
    profile = Profile(profile_id="empty", metadata={})
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"x", title="Alpha",
    )
    search_indexer.index(ctx, ["a-1"])
    gen = ReportGenerator(search_indexer, profile)
    response = gen.query(ctx, QueryRequest(question="anything"))
    assert "# Report:" in response.answer
    assert any("no report template" in w.lower() for w in response.warnings)


# ---- HybridQueryEngine ------------------------------------------------


def test_engine_uses_classifier_in_auto_mode(query_engine, ctx):
    response = query_engine.query(ctx, QueryRequest(question="generate report"))
    assert response.mode_used == QueryMode.REPORT_GENERATION.value


def test_engine_respects_explicit_mode(
    query_engine, search_indexer, workspace, ctx, artifact_registry
):
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"content here",
    )
    search_indexer.index(ctx, ["a-1"])
    response = query_engine.query(
        ctx, QueryRequest(question="anything", mode=QueryMode.KNOWLEDGE_FIRST)
    )
    assert response.mode_used == QueryMode.KNOWLEDGE_FIRST.value


def test_engine_falls_back_to_graph_when_knowledge_empty(
    query_engine, workspace, ctx, artifact_registry
):
    # No indexed knowledge; supply a graph artifact.
    _stage_graph(workspace, ctx, artifact_registry, edges=[
        {"from": "A", "to": "B", "type": "depends_on"}
    ])
    response = query_engine.query(
        ctx, QueryRequest(question="show me the project status")
    )
    assert response.mode_used == QueryMode.KNOWLEDGE_FIRST.value
    assert response.graph_paths
    assert any("fallback" in w.lower() for w in response.warnings)


def test_engine_no_fallback_when_explicit_knowledge_mode(
    query_engine, workspace, ctx, artifact_registry
):
    _stage_graph(workspace, ctx, artifact_registry, edges=[
        {"from": "A", "to": "B", "type": "depends_on"}
    ])
    response = query_engine.query(
        ctx,
        QueryRequest(
            question="anything", mode=QueryMode.KNOWLEDGE_FIRST
        ),
    )
    # Explicit mode → no graph fallback even if knowledge returns nothing.
    assert response.graph_paths == []
    assert not any("fallback" in w.lower() for w in response.warnings)


def test_engine_dispatches_to_graph_first(query_engine, workspace, ctx, artifact_registry):
    _stage_graph(workspace, ctx, artifact_registry, edges=[
        {"from": "A", "to": "B", "type": "depends_on"}
    ])
    response = query_engine.query(
        ctx, QueryRequest(question="show me the dependency")
    )
    assert response.mode_used == QueryMode.GRAPH_FIRST.value
    assert response.graph_paths


def test_engine_dispatches_to_evidence_first(
    query_engine, search_indexer, workspace, ctx, registry, artifact_registry
):
    _stage_document(ctx, registry, document_id="doc-1")
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"the requirement is stated in this section",
        source_document_ids=["doc-1"], source_location="page-3",
    )
    search_indexer.index(ctx, ["a-1"])
    response = query_engine.query(
        ctx, QueryRequest(question="where is the requirement stated?")
    )
    assert response.mode_used == QueryMode.EVIDENCE_FIRST.value
    assert response.sources[0].source_document_id == "doc-1"


def test_engine_dispatches_to_consistency_check(
    query_engine, workspace, ctx, artifact_registry
):
    _stage_consistency_finding(
        workspace, ctx, artifact_registry,
        findings=[{"description": "Mismatch in section 4.1"}],
    )
    response = query_engine.query(
        ctx, QueryRequest(question="any conflicts?")
    )
    assert response.mode_used == QueryMode.CONSISTENCY_CHECK.value
    assert response.review_required


def test_engine_dispatches_to_report_generation(
    query_engine, search_indexer, workspace, ctx, artifact_registry
):
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"x", title="Alpha",
    )
    search_indexer.index(ctx, ["a-1"])
    response = query_engine.query(
        ctx, QueryRequest(question="generate a project report")
    )
    assert response.mode_used == QueryMode.REPORT_GENERATION.value
    assert "Alpha" in response.answer


def test_engine_raises_when_provider_missing(default_profile, search_indexer):
    """Construct an engine missing a provider for one mode → routing error."""
    engine = HybridQueryEngine(
        classifier=QueryIntentClassifier(),
        knowledge_provider=KnowledgeQueryProvider(search_indexer),
        graph_provider=None,  # type: ignore[arg-type]
        evidence_provider=None,  # type: ignore[arg-type]
        consistency_provider=None,  # type: ignore[arg-type]
        report_generator=None,  # type: ignore[arg-type]
    )
    # Bypass the mapping for one mode.
    del engine._providers[QueryMode.GRAPH_FIRST]
    from j1.projects.context import ProjectContext

    ctx = ProjectContext(tenant_id="acme", project_id="alpha")
    with pytest.raises(QueryRoutingError):
        engine.query(ctx, QueryRequest(question="any", mode=QueryMode.GRAPH_FIRST))


# ---- Response shape ---------------------------------------------------


def test_response_includes_all_required_fields(
    query_engine, search_indexer, workspace, ctx, artifact_registry
):
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"content", title="Alpha",
    )
    search_indexer.index(ctx, ["a-1"])
    response = query_engine.query(
        ctx, QueryRequest(question="content", mode=QueryMode.KNOWLEDGE_FIRST)
    )
    assert isinstance(response, QueryResponse)
    assert response.answer
    assert response.mode_used
    assert isinstance(response.sources, list)
    assert isinstance(response.related_artifacts, list)
    assert isinstance(response.graph_paths, list)
    assert isinstance(response.confidence, float)
    assert isinstance(response.review_required, bool)
    assert isinstance(response.warnings, list)
