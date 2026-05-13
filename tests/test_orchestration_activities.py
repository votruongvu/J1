from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.documents.models import DocumentRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.orchestration.activities.payloads import (
    CompileActivityInput,
    EnrichActivityInput,
    GraphActivityInput,
    IndexActivityInput,
    ProjectScope,
    QueryActivityInput,
)
from j1.orchestration.activities.processing import (
    ACTIVITY_BUILD_GRAPH,
    ACTIVITY_COMPILE,
    ACTIVITY_ENRICH,
    ACTIVITY_INDEX,
    ACTIVITY_QUERY,
    ProcessingActivities,
    UnknownProcessorError,
)
from j1.processing.results import (
    ArtifactDraft,
    ArtifactProcessingResult,
    ProcessingResult,
    QueryResult,
    ResultStatus,
)


# Mock processors


class _Compiler:
    kind = "mock.compiler"

    def compile(self, ctx, document_id):
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[
                ArtifactDraft(
                    kind="compiled.text",
                    content=b"hello",
                    suggested_extension=".txt",
                )
            ],
        )


class _Enricher:
    kind = "mock.enricher"

    def enrich(self, ctx, artifact_id):
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[
                ArtifactDraft(
                    kind="enriched.entities",
                    content=b'{"e":1}',
                    suggested_extension=".json",
                )
            ],
        )


class _GraphBuilder:
    kind = "mock.graph"

    def build(self, ctx, artifact_ids):
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[
                ArtifactDraft(
                    kind="graph.entities",
                    content=b"<g/>",
                    suggested_extension=".xml",
                )
            ],
        )


class _Indexer:
    kind = "mock.index"

    def index(self, ctx, artifact_ids):
        return ProcessingResult(
            status=ResultStatus.SUCCEEDED,
            metadata={"indexed": str(len(artifact_ids))},
        )


class _QueryProvider:
    kind = "mock.query"

    def query(self, ctx, question, *, max_results=None):
        return QueryResult(
            status=ResultStatus.SUCCEEDED, answer="42", citations=["doc-1"]
        )


# Helpers


def _scope(ctx) -> ProjectScope:
    return ProjectScope.from_context(ctx)


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _document(ctx) -> DocumentRecord:
    return DocumentRecord(
        document_id="doc-1",
        project=ctx,
        original_filename="paper.pdf",
        stored_filename="doc-1.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum="sha256:doc",
        status=ProcessingStatus.PENDING,
        created_at=_now(),
    )


def _artifact_record(ctx, *, artifact_id="art-1") -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind="compiled.text",
        location=f"compiled/{artifact_id}.txt",
        content_hash="sha256:abc",
        byte_size=5,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_now(),
        updated_at=_now(),
    )


@pytest.fixture
def activities(processing_service, registry, artifact_registry):
    return ProcessingActivities(
        processing=processing_service,
        sources=registry,
        artifacts=artifact_registry,
        compilers={"mock.compiler": _Compiler()},
        enrichers={"mock.enricher": _Enricher()},
        graph_builders={"mock.graph": _GraphBuilder()},
        indexers={"mock.index": _Indexer()},
        query_providers={"mock.query": _QueryProvider()},
    )


# Activity-defn metadata


def test_each_activity_has_temporal_marker(activities):
    for func in activities.all_activities():
        assert hasattr(func, "__temporal_activity_definition")


def test_activity_names_are_namespaced(activities):
    names = [
        a.__temporal_activity_definition.name for a in activities.all_activities()
    ]
    assert ACTIVITY_COMPILE in names
    assert ACTIVITY_ENRICH in names
    assert ACTIVITY_BUILD_GRAPH in names
    assert ACTIVITY_INDEX in names
    assert ACTIVITY_QUERY in names


# Compile


def test_compile_activity_invokes_processing_service(
    activities, ctx, registry, artifact_registry
):
    registry.add(_document(ctx))
    result = activities.compile(
        CompileActivityInput(
            scope=_scope(ctx),
            document_id="doc-1",
            processor_kind="mock.compiler",
        )
    )
    assert result.status == "succeeded"
    assert len(result.artifact_ids) == 1
    # The artifact ended up in the registry.
    assert artifact_registry.list_artifacts(ctx)[0].artifact_id == result.artifact_ids[0]


def test_compile_activity_unknown_processor(activities, ctx, registry):
    registry.add(_document(ctx))
    with pytest.raises(UnknownProcessorError):
        activities.compile(
            CompileActivityInput(
                scope=_scope(ctx),
                document_id="doc-1",
                processor_kind="unregistered",
            )
        )


# ---- Compile idempotency cache --------------------------------------


def _activities_with_cache(
    *, processing_service, registry, artifact_registry, compiler, cache,
):
    """Build a ProcessingActivities instance wired to a cache + a
 counting compiler so tests can prove the second invocation
 bypasses the underlying compile call."""
    from j1.orchestration.activities.processing import ProcessingActivities
    return ProcessingActivities(
        processing=processing_service,
        sources=registry,
        artifacts=artifact_registry,
        compilers={"mock.compiler": compiler},
        enrichers={},
        graph_builders={},
        indexers={},
        query_providers={},
        result_cache=cache,
    )


class _CountingCompiler:
    """Compiler that tracks how many times its `compile` method runs.

 Real Temporal activity retries (heartbeat-timeout, worker crash)
 re-invoke the activity from scratch. The cache must short-circuit
 that re-invocation when a `completed` entry exists for the same
 input, so the underlying processor (here MinerU stand-in) is
 NOT re-run."""

    kind = "mock.compiler"

    def __init__(self) -> None:
        self.calls: int = 0

    def compile(self, ctx, document_id):  # noqa: ARG002 — real interface signature
        self.calls += 1
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[
                ArtifactDraft(
                    kind="compiled.text",
                    content=b"compiled bytes",
                    suggested_extension=".txt",
                ),
            ],
        )


def test_compile_activity_skips_processor_when_cache_hit(
    processing_service, registry, artifact_registry, ctx, workspace,
):
    """Second invocation of compile with the same inputs must NOT
 call the underlying compiler — the artifact ids from the cached
 `completed` entry are returned directly. This is what stops
 Temporal retries from re-running MinerU on a successful prior
 attempt."""
    from j1.processing.cache import JsonlProcessingResultCache

    registry.add(_document(ctx))
    compiler = _CountingCompiler()
    cache = JsonlProcessingResultCache(workspace)
    activities = _activities_with_cache(
        processing_service=processing_service,
        registry=registry,
        artifact_registry=artifact_registry,
        compiler=compiler,
        cache=cache,
    )
    inp = CompileActivityInput(
        scope=_scope(ctx),
        document_id="doc-1",
        processor_kind="mock.compiler",
    )

    first = activities.compile(inp)
    assert first.status == "succeeded"
    assert compiler.calls == 1

    # Second activity invocation simulates a Temporal retry. The
    # cache hit must take over — compile MUST NOT run again.
    second = activities.compile(inp)
    assert second.status == "succeeded"
    assert second.artifact_ids == first.artifact_ids
    assert compiler.calls == 1, (
        "compile re-ran despite a completed cache entry — the cache "
        "lookup is broken or the activity bypassed it"
    )


def test_compile_activity_records_failure_in_cache(
    processing_service, registry, artifact_registry, ctx, workspace,
):
    """Failures land in the cache as `failed` entries (audit trail
 for operators inspecting repeated processing). They DO NOT block
 retries — Temporal's retry policy is the source of truth for
 that — but they make the cache file self-explaining: 'this doc
 failed at compile, here's the message'.

 Note: `ProcessingService.compile` catches compiler exceptions
 and returns a `FAILED` `ArtifactProcessingResult`. The activity
 therefore sees a non-raising failed result — the cache must
 still record it."""
    from j1.processing.cache import (
        CACHE_STATUS_FAILED,
        JsonlProcessingResultCache,
    )

    class _BoomCompiler:
        kind = "mock.compiler"

        def compile(self, ctx, document_id):  # noqa: ARG002
            raise RuntimeError("compile blew up")

    registry.add(_document(ctx))
    cache = JsonlProcessingResultCache(workspace)
    activities = _activities_with_cache(
        processing_service=processing_service,
        registry=registry,
        artifact_registry=artifact_registry,
        compiler=_BoomCompiler(),
        cache=cache,
    )
    result = activities.compile(
        CompileActivityInput(
            scope=_scope(ctx),
            document_id="doc-1",
            processor_kind="mock.compiler",
        )
    )
    assert result.status == "failed"
    entry = cache.lookup(
        ctx,
        document_hash="sha256:doc",
        processor_kind="mock.compiler",
    )
    assert entry is not None
    assert entry.status == CACHE_STATUS_FAILED


def test_compile_activity_failed_cache_entry_does_not_short_circuit_retry(
    processing_service, registry, artifact_registry, ctx, workspace,
):
    """A `failed` cache entry MUST NOT short-circuit the next attempt
 — only `completed` entries do. Otherwise a transient first
 failure would lock the document out of all future processing."""
    from j1.processing.cache import (
        CACHE_STATUS_FAILED,
        JsonlProcessingResultCache,
        ProcessingCacheEntry,
        make_cache_key,
    )
    from datetime import datetime, timezone

    registry.add(_document(ctx))
    cache = JsonlProcessingResultCache(workspace)
    # Pre-seed a failed entry to simulate a previous failed attempt.
    now = datetime.now(timezone.utc)
    cache.upsert(ctx, ProcessingCacheEntry(
        cache_key=make_cache_key(
            document_hash="sha256:doc", processor_kind="mock.compiler",
        ),
        document_id="doc-1",
        document_hash="sha256:doc",
        processor_kind="mock.compiler",
        processor_version="",
        mode="",
        status=CACHE_STATUS_FAILED,
        artifact_ids=(),
        created_at=now,
        updated_at=now,
    ))
    compiler = _CountingCompiler()
    activities = _activities_with_cache(
        processing_service=processing_service,
        registry=registry,
        artifact_registry=artifact_registry,
        compiler=compiler,
        cache=cache,
    )
    result = activities.compile(
        CompileActivityInput(
            scope=_scope(ctx), document_id="doc-1",
            processor_kind="mock.compiler",
        )
    )
    assert result.status == "succeeded"
    assert compiler.calls == 1, "compile must run despite a prior failed cache entry"


def test_compile_activity_runs_when_cache_disabled(
    processing_service, registry, artifact_registry, ctx,
):
    """Sanity: with `result_cache=None` (the default for deployments
 that haven't migrated), the activity always invokes the
 underlying compiler — preserves legacy behaviour exactly."""
    registry.add(_document(ctx))
    compiler = _CountingCompiler()
    activities = _activities_with_cache(
        processing_service=processing_service,
        registry=registry,
        artifact_registry=artifact_registry,
        compiler=compiler,
        cache=None,
    )
    inp = CompileActivityInput(
        scope=_scope(ctx),
        document_id="doc-1",
        processor_kind="mock.compiler",
    )
    activities.compile(inp)
    activities.compile(inp)
    assert compiler.calls == 2


def test_compile_activity_writes_processing_marker_before_processor_call(
    processing_service, registry, artifact_registry, ctx, workspace,
):
    """Cache visibility: while the processor is running there should
 be a `processing` row, then a `completed` row after success.
 Operators inspecting the cache file mid-parse see the marker;
 latest-snapshot semantics mean the `completed` row supersedes
 once the call returns."""
    from j1.processing.cache import (
        CACHE_STATUS_COMPLETED,
        CACHE_STATUS_PROCESSING,
        JsonlProcessingResultCache,
    )

    seen_during_call: list[str] = []

    class _ObservingCompiler:
        kind = "mock.compiler"
        version = "1"

        def __init__(self, cache, ctx_):
            self._cache = cache
            self._ctx = ctx_
            self.calls = 0

        def compile(self, ctx, document_id):  # noqa: ARG002
            self.calls += 1
            # Snapshot the cache state at the moment the processor is
            # actually executing — this is when operators looking at
            # the cache file would see the "in flight" state.
            entry = self._cache.lookup(
                self._ctx, document_hash="sha256:doc",
                processor_kind="mock.compiler", processor_version="1",
            )
            seen_during_call.append(entry.status if entry else "<no-row>")
            return ArtifactProcessingResult(
                status=ResultStatus.SUCCEEDED,
                drafts=[ArtifactDraft(
                    kind="compiled.text", content=b"x",
                    suggested_extension=".txt",
                )],
            )

    registry.add(_document(ctx))
    cache = JsonlProcessingResultCache(workspace)
    compiler = _ObservingCompiler(cache, ctx)
    activities = _activities_with_cache(
        processing_service=processing_service,
        registry=registry,
        artifact_registry=artifact_registry,
        compiler=compiler,
        cache=cache,
    )
    activities.compile(CompileActivityInput(
        scope=_scope(ctx), document_id="doc-1",
        processor_kind="mock.compiler",
    ))
    # Compiler ran exactly once.
    assert compiler.calls == 1
    # While the processor was executing, the cache had a `processing` row.
    assert seen_during_call == [CACHE_STATUS_PROCESSING]
    # After completion, the visible state is `completed`.
    final = cache.lookup(
        ctx, document_hash="sha256:doc",
        processor_kind="mock.compiler", processor_version="1",
    )
    assert final is not None
    assert final.status == CACHE_STATUS_COMPLETED


def test_heartbeating_thread_propagates_contextvars(workspace, ctx):
    """Regression test for the heartbeat thread silently no-op'ing.

 `temporalio.activity.heartbeat` reads the current activity from
 a `contextvars.ContextVar`. Python `threading.Thread` does NOT
 propagate contextvars, so a naive daemon-thread call to
 `_safe_heartbeat` would silently swallow the resulting
 RuntimeError and never deliver a heartbeat — letting the
 `heartbeat_timeout` fire mid-parse on real documents.

 The fix is `contextvars.copy_context.run(...)` inside the
 ticker loop. This test simulates the activity context with a
 free contextvar and verifies the thread sees the same value the
 parent set."""
    import contextvars
    import threading
    import time

    from j1.orchestration.activities.processing import _heartbeating

    # Standalone contextvar simulating Temporal's activity contextvar.
    test_var: contextvars.ContextVar[str] = contextvars.ContextVar("test-var")
    test_var.set("activity-context-value")

    seen: list[str] = []
    barrier = threading.Event()

    # Monkeypatch `_safe_heartbeat` for the duration of the test so
    # we can record what the daemon thread sees instead of pretending
    # it's inside a real activity.
    from j1.orchestration.activities import processing as proc_mod
    original = proc_mod._safe_heartbeat

    def _record(_details):
        seen.append(test_var.get("<MISSING>"))
        barrier.set()

    proc_mod._safe_heartbeat = _record
    try:
        with _heartbeating({"x": 1}, interval_seconds=0.05):
            assert barrier.wait(timeout=2.0), "thread never produced a heartbeat"
            time.sleep(0.12)  # let the loop tick a few times
    finally:
        proc_mod._safe_heartbeat = original

    # Without contextvar propagation, `test_var.get("<MISSING>")`
    # would return the default. With propagation, it returns the
    # value the parent set.
    assert seen, "heartbeat thread never ran"
    assert all(v == "activity-context-value" for v in seen), (
        f"contextvar not propagated to heartbeat thread; saw {seen}"
    )


def test_compile_cache_key_includes_processor_version(
    processing_service, registry, artifact_registry, ctx, workspace,
):
    """Bumping the compiler's `version` attribute must invalidate
 the cache — otherwise an upgraded parser would silently reuse
 artifacts produced by the old version."""
    from j1.processing.cache import JsonlProcessingResultCache

    class _VersionedCompiler:
        kind = "mock.compiler"

        def __init__(self, version: str):
            self.version = version
            self.calls = 0

        def compile(self, ctx, document_id):  # noqa: ARG002
            self.calls += 1
            return ArtifactProcessingResult(
                status=ResultStatus.SUCCEEDED,
                drafts=[ArtifactDraft(
                    kind="compiled.text", content=b"v",
                    suggested_extension=".txt",
                )],
            )

    registry.add(_document(ctx))
    cache = JsonlProcessingResultCache(workspace)
    v1 = _VersionedCompiler("1")
    activities_v1 = _activities_with_cache(
        processing_service=processing_service,
        registry=registry,
        artifact_registry=artifact_registry,
        compiler=v1, cache=cache,
    )
    activities_v1.compile(CompileActivityInput(
        scope=_scope(ctx), document_id="doc-1",
        processor_kind="mock.compiler",
    ))
    assert v1.calls == 1

    v2 = _VersionedCompiler("2")
    activities_v2 = _activities_with_cache(
        processing_service=processing_service,
        registry=registry,
        artifact_registry=artifact_registry,
        compiler=v2, cache=cache,
    )
    activities_v2.compile(CompileActivityInput(
        scope=_scope(ctx), document_id="doc-1",
        processor_kind="mock.compiler",
    ))
    # Different version → fresh cache key → must run.
    assert v2.calls == 1, "version bump must invalidate the cache"


# Enrich


def test_enrich_activity_invokes_processing_service(
    activities, ctx, artifact_registry
):
    artifact_registry.add(_artifact_record(ctx))
    result = activities.enrich(
        EnrichActivityInput(
            scope=_scope(ctx),
            artifact_id="art-1",
            processor_kind="mock.enricher",
        )
    )
    assert result.status == "succeeded"
    assert len(result.artifact_ids) == 1


# Graph


def test_build_graph_activity(activities, ctx):
    result = activities.build_graph(
        GraphActivityInput(
            scope=_scope(ctx),
            artifact_ids=["a", "b"],
            processor_kind="mock.graph",
        )
    )
    assert result.status == "succeeded"
    assert len(result.artifact_ids) == 1


# Index


def test_index_activity(activities, ctx):
    result = activities.index(
        IndexActivityInput(
            scope=_scope(ctx),
            artifact_ids=["a", "b"],
            processor_kind="mock.index",
        )
    )
    assert result.status == "succeeded"


# Query


def test_query_activity(activities, ctx):
    """The Temporal query activity delegates to
    ``ProcessingService.query`` which delegates to the orchestrator.
    The activity stays Temporal-callable — the test verifies the
    ``QueryActivityResult`` shape, not the specific answer (the
    orchestrator is the source of truth for that)."""
    result = activities.query(
        QueryActivityInput(
            scope=_scope(ctx),
            question="what?",
            processor_kind="mock.query",
        )
    )
    assert result.status == "succeeded"


# Project scope round-trip (Temporal payload safety)


def test_project_scope_round_trips(ctx):
    scope = ProjectScope.from_context(ctx)
    rehydrated = scope.to_context()
    assert rehydrated == ctx
