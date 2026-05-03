from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.errors.exceptions import SearchIndexerError
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.processing.status import ResultStatus
from j1.search import (
    DEFAULT_DB_FILENAME,
    MAX_INDEXED_BYTES,
    SearchHit,
    SqliteSearchIndexer,
)
from j1.workspace.layout import WorkspaceArea


# ---- Helpers -----------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _stage(
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
) -> ArtifactRecord:
    area_dir = workspace.area(ctx, area)
    area_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{artifact_id}.txt"
    (area_dir / stored).write_bytes(content)
    metadata = {}
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


@pytest.fixture
def indexer(workspace, artifact_registry, registry):
    return SqliteSearchIndexer(workspace, artifact_registry, registry)


# ---- Construction / FTS5 availability ----------------------------------


def test_indexer_constructs(workspace, artifact_registry):
    SqliteSearchIndexer(workspace, artifact_registry)


# ---- Indexing ----------------------------------------------------------


def test_index_returns_processing_result(indexer, workspace, ctx, artifact_registry):
    _stage(
        workspace,
        ctx,
        artifact_registry,
        artifact_id="a-1",
        content=b"hello world",
    )
    result = indexer.index(ctx, ["a-1"])
    assert isinstance(result, type(result))
    assert result.status is ResultStatus.SUCCEEDED
    assert result.metadata["indexed_count"] == "1"


def test_index_writes_db_under_search_area(
    indexer, workspace, ctx, artifact_registry
):
    _stage(workspace, ctx, artifact_registry, artifact_id="a-1", content=b"x")
    indexer.index(ctx, ["a-1"])
    assert (workspace.search(ctx) / DEFAULT_DB_FILENAME).is_file()


def test_index_failure_returns_failed_result(indexer, ctx):
    # Unknown artifact_id → registry lookup raises → FAILED result
    result = indexer.index(ctx, ["does-not-exist"])
    assert result.status is ResultStatus.FAILED
    assert "does-not-exist" in (result.error or "")


def test_index_is_idempotent(indexer, workspace, ctx, artifact_registry):
    _stage(workspace, ctx, artifact_registry, artifact_id="a-1", content=b"hello")
    indexer.index(ctx, ["a-1"])
    indexer.index(ctx, ["a-1"])
    indexed = indexer.list_indexed(ctx)
    assert len(indexed) == 1
    assert indexed[0].artifact_id == "a-1"


def test_build_full_index_indexes_everything(
    indexer, workspace, ctx, artifact_registry
):
    _stage(workspace, ctx, artifact_registry, artifact_id="a-1", content=b"alpha text")
    _stage(workspace, ctx, artifact_registry, artifact_id="a-2", content=b"beta text")
    _stage(workspace, ctx, artifact_registry, artifact_id="a-3", content=b"gamma text")
    result = indexer.build_full_index(ctx)
    assert result.status is ResultStatus.SUCCEEDED
    assert result.metadata["indexed_count"] == "3"
    assert {h.artifact_id for h in indexer.list_indexed(ctx)} == {"a-1", "a-2", "a-3"}


# ---- Retrieval ---------------------------------------------------------


def test_search_finds_indexed_artifact(indexer, workspace, ctx, artifact_registry):
    _stage(
        workspace,
        ctx,
        artifact_registry,
        artifact_id="a-1",
        content=b"the quick brown fox jumps over the lazy dog",
    )
    indexer.index(ctx, ["a-1"])
    hits = indexer.search(ctx, "brown fox")
    assert len(hits) == 1
    assert hits[0].artifact_id == "a-1"
    assert "brown" in hits[0].extracted_text


def test_search_empty_query_returns_empty(indexer, workspace, ctx, artifact_registry):
    _stage(workspace, ctx, artifact_registry, artifact_id="a-1", content=b"text")
    indexer.index(ctx, ["a-1"])
    assert indexer.search(ctx, "") == []
    assert indexer.search(ctx, "   ") == []


def test_search_against_missing_db_returns_empty(indexer, ctx):
    assert indexer.search(ctx, "anything") == []


def test_search_filters_by_artifact_type(
    indexer, workspace, ctx, artifact_registry
):
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="c-1", kind="compiled.text", content=b"shared keyword text",
    )
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="e-1", kind="enriched.requirements",
        content=b"shared keyword text", area=WorkspaceArea.ENRICHED,
    )
    indexer.index(ctx, ["c-1", "e-1"])

    compiled_only = indexer.search(
        ctx, "keyword", artifact_types=["compiled.text"]
    )
    assert {h.artifact_id for h in compiled_only} == {"c-1"}

    both = indexer.search(ctx, "keyword")
    assert {h.artifact_id for h in both} == {"c-1", "e-1"}


def test_search_returns_source_references(
    indexer, workspace, ctx, artifact_registry
):
    _stage(
        workspace,
        ctx,
        artifact_registry,
        artifact_id="a-1",
        content=b"section content here",
        source_document_ids=["doc-1"],
        source_location="page-5",
    )
    indexer.index(ctx, ["a-1"])
    hit = indexer.search(ctx, "section")[0]
    assert hit.source_document_id == "doc-1"
    assert hit.source_location == "page-5"


def test_search_returns_metadata_fields(
    indexer, workspace, ctx, artifact_registry
):
    _stage(
        workspace,
        ctx,
        artifact_registry,
        artifact_id="a-1",
        content=b"some content",
        title="Custom Title",
        confidence=0.85,
        review_status=ReviewStatus.PENDING,
    )
    indexer.index(ctx, ["a-1"])
    hit = indexer.search(ctx, "content")[0]
    assert hit.title == "Custom Title"
    assert hit.confidence == pytest.approx(0.85)
    assert hit.review_status == ReviewStatus.PENDING.value
    assert hit.checksum.startswith("sha256:")
    assert hit.created_at.startswith("2026-01-01")


def test_search_max_results(indexer, workspace, ctx, artifact_registry):
    for i in range(5):
        _stage(
            workspace, ctx, artifact_registry,
            artifact_id=f"a-{i}", content=b"keyword shared",
        )
    indexer.index(ctx, [f"a-{i}" for i in range(5)])
    hits = indexer.search(ctx, "keyword", max_results=2)
    assert len(hits) == 2


def test_retrieve_by_id(indexer, workspace, ctx, artifact_registry):
    _stage(workspace, ctx, artifact_registry, artifact_id="a-1", content=b"hello")
    indexer.index(ctx, ["a-1"])
    hit = indexer.retrieve_by_id(ctx, "a-1")
    assert hit is not None
    assert hit.artifact_id == "a-1"


def test_retrieve_missing_returns_none(indexer, workspace, ctx, artifact_registry):
    _stage(workspace, ctx, artifact_registry, artifact_id="a-1", content=b"hello")
    indexer.index(ctx, ["a-1"])
    assert indexer.retrieve_by_id(ctx, "missing") is None


def test_list_indexed_filters_by_type(indexer, workspace, ctx, artifact_registry):
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="c-1", kind="compiled.text", content=b"x",
    )
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="e-1", kind="enriched.requirements",
        content=b"y", area=WorkspaceArea.ENRICHED,
    )
    indexer.index(ctx, ["c-1", "e-1"])
    enriched = indexer.list_indexed(ctx, artifact_types=["enriched.requirements"])
    assert {h.artifact_id for h in enriched} == {"e-1"}


# ---- Edge cases --------------------------------------------------------


def test_index_handles_binary_content(
    indexer, workspace, ctx, artifact_registry
):
    """Binary content (e.g. PDF bytes) is stored with empty extracted_text."""
    _stage(
        workspace,
        ctx,
        artifact_registry,
        artifact_id="a-bin",
        content=b"\xff\xfe\x00\x01\x02PK\x03\x04binary",
    )
    indexer.index(ctx, ["a-bin"])
    hit = indexer.retrieve_by_id(ctx, "a-bin")
    assert hit is not None
    assert hit.extracted_text == ""


def test_index_handles_missing_file_with_empty_text(
    indexer, artifact_registry, ctx
):
    record = ArtifactRecord(
        artifact_id="a-no-file",
        project=ctx,
        kind="compiled.text",
        location="compiled/a-no-file.txt",
        content_hash="sha256:x",
        byte_size=0,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_now(),
        updated_at=_now(),
    )
    artifact_registry.add(record)
    result = indexer.index(ctx, ["a-no-file"])
    assert result.status is ResultStatus.SUCCEEDED
    hit = indexer.retrieve_by_id(ctx, "a-no-file")
    assert hit is not None
    assert hit.extracted_text == ""


def test_default_title_when_metadata_lacks_title(
    indexer, workspace, ctx, artifact_registry
):
    _stage(workspace, ctx, artifact_registry, artifact_id="a-1", content=b"hi")
    indexer.index(ctx, ["a-1"])
    hit = indexer.retrieve_by_id(ctx, "a-1")
    assert hit.title == "compiled.text/a-1"


# ---- Integration with ProcessingService --------------------------------


def test_indexer_works_via_processing_service(
    indexer, processing_service, workspace, ctx, artifact_registry
):
    _stage(workspace, ctx, artifact_registry, artifact_id="a-1", content=b"hello")
    result = processing_service.index(ctx, indexer, ["a-1"])
    assert result.status is ResultStatus.SUCCEEDED
    # And the index is queryable.
    hits = indexer.search(ctx, "hello")
    assert len(hits) == 1
