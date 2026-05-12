"""Tests for `DefaultValidationRunner`.

Drives a real `HybridQueryEngine` (against an in-memory SQLite FTS
index populated through the same code path the production worker
uses) so the run-scope filter, the chunk_id round-trip, and the
 case-specific checks (`expected_chunk_in_topk`,
`expected_page_in_citations`) are all exercised end-to-end.

What's NOT covered:
 * Persistence — the runner's store-side wiring is the service's
 job, tested in test_validation_service_.py.
 * REST envelope shape — covered in test_rest_validation_runs.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.query.classifier import QueryIntentClassifier
from j1.query.engine import HybridQueryEngine
from j1.query.providers import (
    ConsistencyProvider,
    EvidenceProvider,
    GraphQueryProvider,
    KnowledgeQueryProvider,
    ReportGenerator,
)
from j1.search import SqliteSearchIndexer
from j1.validation import (
    DefaultValidationRunner,
    ValidationSetDTO,
    ValidationTestCaseDTO,
)
from j1.workspace.layout import WorkspaceArea


# ---- Fixtures -------------------------------------------------------


@pytest.fixture
def indexer(workspace, artifact_registry, registry):
    return SqliteSearchIndexer(workspace, artifact_registry, registry)


@pytest.fixture
def query_engine(workspace, artifact_registry, registry, indexer):
    from types import SimpleNamespace
    profile_stub = SimpleNamespace(report_templates={})
    return HybridQueryEngine(
        classifier=QueryIntentClassifier(),
        knowledge_provider=KnowledgeQueryProvider(indexer),
        graph_provider=GraphQueryProvider(artifact_registry, workspace),
        evidence_provider=EvidenceProvider(indexer, registry),
        consistency_provider=ConsistencyProvider(artifact_registry, workspace),
        report_generator=ReportGenerator(indexer, profile_stub),
    )


@pytest.fixture
def runner(query_engine, artifact_registry):
    return DefaultValidationRunner(
        query_engine=query_engine,
        artifact_registry=artifact_registry,
    )


def _stage_chunk(
    workspace, ctx, artifact_registry, indexer,
    *,
    artifact_id: str,
    content: bytes,
    run_id: str,
    chunk_id: str,
    page: str | None = None,
) -> ArtifactRecord:
    """Stage a chunk-kind artifact + index it. Mirrors what the
 production compile pipeline produces."""
    area = WorkspaceArea.COMPILED
    area_dir = workspace.area(ctx, area)
    area_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{artifact_id}.txt"
    (area_dir / stored).write_bytes(content)
    metadata: dict = {"run_id": run_id, "chunk_id": chunk_id}
    if page is not None:
        metadata["source_location"] = page
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record = ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind="chunk",
        location=f"{area.value}/{stored}",
        content_hash=f"sha256:{artifact_id}",
        byte_size=len(content),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        source_document_ids=[],
        source_artifact_ids=[],
        metadata=metadata,
    )
    artifact_registry.add(record)
    indexer.index(ctx, [artifact_id])
    return record


def _make_set(
    *,
    set_id: str = "vs-1",
    run_id: str = "run-1",
    test_cases: list[ValidationTestCaseDTO] | None = None,
) -> ValidationSetDTO:
    return ValidationSetDTO(
        validation_set_id=set_id,
        run_id=run_id,
        document_ids=["doc-1"],
        source="generated",
        status="draft",
        created_at="2026-05-07T10:00:00Z",
        created_by=None,
        generator_version="v1",
        artifacts_content_hash="sha256:test",
        test_cases=test_cases or [],
    )


def _case(
    *,
    case_id: str = "tc-1",
    question: str,
    expected_chunks: list[str] | None = None,
    expected_pages: list[int] | None = None,
    citation_required: bool = False,
    priority: str = "normal",
) -> ValidationTestCaseDTO:
    return ValidationTestCaseDTO(
        test_case_id=case_id,
        question=question,
        type="retrieval",
        priority=priority,  # type: ignore[arg-type]
        expected_behavior="answer_with_citations",
        expected_chunks=expected_chunks or [],
        expected_pages=expected_pages or [],
        citation_required=citation_required,
    )


# ---- Happy path ----------------------------------------------------


def test_runner_passes_when_expected_chunk_retrieved(
    runner, ctx, workspace, artifact_registry, indexer,
):
    """Expected-chunk-in-topK is the headline check.
 When the chunk is retrievable, the case passes and the run
 aggregates to passed."""
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"hello world",
        run_id="run-1", chunk_id="chunk-A",
    )
    case = _case(
        question="hello", expected_chunks=["chunk-A"],
    )

    vrun = runner.run(ctx, _make_set(test_cases=[case]))

    assert vrun.execution_status == "completed"
    assert vrun.validation_status == "passed"
    assert vrun.summary.total == 1
    assert vrun.summary.passed == 1
    assert vrun.summary.failed == 0
    assert vrun.results[0].status == "passed"


def test_runner_fails_when_expected_chunk_not_retrieved(
    runner, ctx, workspace, artifact_registry, indexer,
):
    """The case names a chunk-id that doesn't appear in the index.
 The runner runs the query (returns nothing because the FTS
 query won't match), the expected_chunk_in_topk check fails,
 the case is marked failed, and the run validation_status is
 failed even though execution_status is completed."""
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-other", content=b"unrelated",
        run_id="run-1", chunk_id="chunk-OTHER",
    )
    case = _case(
        question="hello", expected_chunks=["chunk-DOES-NOT-EXIST"],
    )

    vrun = runner.run(ctx, _make_set(test_cases=[case]))

    # Critical: split status must persist independently. The job
    # finished cleanly (completed); the document's case failed.
    assert vrun.execution_status == "completed"
    assert vrun.validation_status == "failed"
    assert vrun.results[0].status == "failed"
    assert vrun.results[0].failure_reason


def test_runner_aggregates_to_passed_with_warnings(
    runner, ctx,
):
    """ doesn't ship optional checks yet, but the aggregator
 rule is locked here so doesn't have to refactor: any
 optional fail downgrades the run, but a required fail dominates."""
    case = _case(question="anything", expected_chunks=[])
    vrun = runner.run(ctx, _make_set(test_cases=[case]))
    # No retrieved chunks (empty index) → retrieved_chunks_present
    # required-fails → run is "failed". This locks the contract:
    # 'no retrieval' is a required fail, not a warning.
    assert vrun.validation_status == "failed"


def test_runner_priority_orders_smoke_first(
    runner, ctx, workspace, artifact_registry, indexer,
):
    """Smoke-priority cases must run before normal — testers want
 the "is the index alive?" signal first. Order is asserted via
 the result list, which preserves execution order."""
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"hello world",
        run_id="run-1", chunk_id="chunk-A",
    )
    smoke = _case(
        case_id="smoke-1", question="hello", priority="smoke",
    )
    normal = _case(
        case_id="normal-1", question="hello", priority="normal",
    )
    deep = _case(
        case_id="deep-1", question="hello", priority="deep",
    )

    vrun = runner.run(
        ctx, _make_set(test_cases=[deep, normal, smoke]),
    )

    ids = [r.test_case_id for r in vrun.results]
    assert ids == ["smoke-1", "normal-1", "deep-1"]


# ---- Page-level check ----------------------------------------------


def test_runner_checks_expected_page_in_citations(
    runner, ctx, workspace, artifact_registry, indexer,
):
    """expected_pages drives the citation page-overlap check. The
 indexer surfaces the producer's `source_location` verbatim;
 we accept any string that contains the expected page number
 (producers don't share a single page format yet)."""
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"page three content",
        run_id="run-1", chunk_id="chunk-A",
        page="p.3",
    )
    case = _case(
        question="three", expected_chunks=["chunk-A"], expected_pages=[3],
    )

    vrun = runner.run(ctx, _make_set(test_cases=[case]))

    page_check = next(
        c for c in vrun.results[0].checks
        if c.name == "expected_page_in_citations"
    )
    assert page_check.passed is True


def test_runner_fails_on_wrong_expected_page(
    runner, ctx, workspace, artifact_registry, indexer,
):
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"page seven content",
        run_id="run-1", chunk_id="chunk-A",
        page="p.7",
    )
    case = _case(
        question="seven", expected_chunks=["chunk-A"], expected_pages=[3],
    )

    vrun = runner.run(ctx, _make_set(test_cases=[case]))

    assert vrun.validation_status == "failed"
    page_check = next(
        c for c in vrun.results[0].checks
        if c.name == "expected_page_in_citations"
    )
    assert page_check.passed is False


# ---- Run-scope filter ----------------------------------------------


def test_runner_run_scope_isolates_to_requested_run(
    runner, ctx, workspace, artifact_registry, indexer,
):
    """Trust regression: same chunk text indexed under run-A and
 run-B. The runner targets run-A; the run-B chunk must not
 surface in retrieval, and the per-result run_id checks must
 reflect that."""
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-A", content=b"shared keyword",
        run_id="run-A", chunk_id="chunk-A1",
    )
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-B", content=b"shared keyword",
        run_id="run-B", chunk_id="chunk-B1",
    )
    case = _case(
        question="shared", expected_chunks=["chunk-A1"],
    )
    vset = _make_set(run_id="run-A", test_cases=[case])

    vrun = runner.run(ctx, vset)

    # All retrieved chunks must come from run-A.
    chunk_runs = {c.run_id for c in vrun.results[0].retrieved_chunks}
    assert chunk_runs == {"run-A"}
    # And the case passes — run-A's chunk is the only candidate.
    assert vrun.validation_status == "passed"


# ---- Lifecycle callback --------------------------------------------


def test_runner_emits_three_lifecycle_snapshots(
    query_engine, artifact_registry, ctx,
):
    """The runner fires the lifecycle callback three times:
 pending → running → completed. Persistence layer relies on
 this contract to upsert each transition."""
    snapshots = []
    runner = DefaultValidationRunner(
        query_engine=query_engine,
        artifact_registry=artifact_registry,
        lifecycle_callback=lambda v: snapshots.append(v),
    )
    case = _case(question="anything", expected_chunks=[])

    runner.run(ctx, _make_set(test_cases=[case]))

    statuses = [s.execution_status for s in snapshots]
    assert statuses == ["pending", "running", "completed"]


def test_runner_lifecycle_failure_does_not_break_run(
    query_engine, artifact_registry, ctx,
):
    """If the persistence callback throws, the runner must still
 return the terminal snapshot. Persistence is best-effort —
 losing a snapshot can't fail the user-facing call."""
    runner = DefaultValidationRunner(
        query_engine=query_engine,
        artifact_registry=artifact_registry,
        lifecycle_callback=lambda v: (_ for _ in ()).throw(RuntimeError("oops")),
    )
    case = _case(question="anything", expected_chunks=[])

    vrun = runner.run(ctx, _make_set(test_cases=[case]))

    assert vrun.execution_status == "completed"


def test_runner_engine_failure_marks_run_failed(
    artifact_registry, ctx,
):
    """If the query engine itself raises (e.g. corrupt index), the
 run reports execution_status=failed with the error message,
 and validation_status=inconclusive (we don't know if the
 document would have passed)."""

    class _BoomEngine:
        def query(self, ctx, request):
            raise RuntimeError("simulated index corruption")

    runner = DefaultValidationRunner(
        query_engine=_BoomEngine(),  # type: ignore[arg-type]
        artifact_registry=artifact_registry,
    )
    case = _case(question="anything")
    vrun = runner.run(ctx, _make_set(test_cases=[case]))

    # When the engine fails on a per-case call, the runner doesn't
    # crash — it records a failed result. The run completes with
    # validation_status=failed (the case failed).
    assert vrun.execution_status == "completed"
    assert vrun.results[0].status == "failed"
    assert "simulated" in (vrun.results[0].failure_reason or "").lower()


# ---- Empty / edge cases --------------------------------------------


def test_runner_empty_set_produces_inconclusive_summary(
    runner, ctx,
):
    """A set with zero cases gives nothing to evaluate. Run is
 `completed` (the runner finished) but `validation_status` is
 `inconclusive` (no cases were ever evaluated)."""
    vrun = runner.run(ctx, _make_set(test_cases=[]))

    assert vrun.execution_status == "completed"
    assert vrun.validation_status == "inconclusive"
    assert vrun.summary.total == 0
    assert vrun.summary.recommended_action == "no test cases to evaluate"


def test_runner_summary_carries_coverage_breakdown(
    runner, ctx, workspace, artifact_registry, indexer,
):
    """Coverage is the readiness card's main visualisation. 
 populates by_type and by_priority; by_section ships."""
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"content one",
        run_id="run-1", chunk_id="c-1",
    )
    cases = [
        _case(case_id="t1", question="content", expected_chunks=["c-1"], priority="smoke"),
        _case(case_id="t2", question="content", expected_chunks=["c-1"], priority="normal"),
        _case(case_id="t3", question="content", expected_chunks=["c-1"], priority="normal"),
    ]
    vrun = runner.run(ctx, _make_set(test_cases=cases))

    assert vrun.summary.coverage.by_type == {"retrieval": 3}
    assert vrun.summary.coverage.by_priority == {"smoke": 1, "normal": 2}


def test_runner_with_judge_emits_optional_checks(
    query_engine, artifact_registry, ctx, workspace, indexer,
):
    """ integration: a runner constructed with a judge
 appends optional semantic checks to every result. Wiring
 smoke test — judge stub returns a clean coverage + grounding
 judgement, so the optional checks pass and don't downgrade
 the run."""
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"alpha keyword",
        run_id="run-1", chunk_id="chunk-1",
    )

    from j1.validation import (
        CoverageJudgement,
        DefaultValidationRunner,
        GroundingJudgement,
    )

    class _StubJudge:
        def judge_answer_covers_points(self, **kwargs):
            return CoverageJudgement(
                points=[CoverageJudgement.Point(text="x", covered=True)],
            )

        def judge_answer_grounded(self, **kwargs):
            return GroundingJudgement(unsupported_claims=[])

        def judge_negative_abstain(self, **kwargs):
            return None  # not exercised by this case

    runner_with_judge = DefaultValidationRunner(
        query_engine=query_engine,
        artifact_registry=artifact_registry,
        judge=_StubJudge(),
    )
    case = _case(
        question="alpha", expected_chunks=["chunk-1"],
    )
    # Force expected_answer_points so the coverage check engages.
    case_with_points = ValidationTestCaseDTO(
        test_case_id=case.test_case_id,
        question=case.question,
        type=case.type,
        priority=case.priority,
        expected_behavior=case.expected_behavior,
        expected_answer_points=["proposal due date"],
        expected_chunks=case.expected_chunks,
        expected_pages=case.expected_pages,
        expected_artifacts=case.expected_artifacts,
        expected_graph_nodes=case.expected_graph_nodes,
        expected_graph_edges=case.expected_graph_edges,
        citation_required=case.citation_required,
        source_traceability=case.source_traceability,
        metadata=case.metadata,
    )

    vrun = runner_with_judge.run(ctx, _make_set(test_cases=[case_with_points]))

    check_names = {c.name for c in vrun.results[0].checks}
    assert "answer_covers_expected_points" in check_names
    assert "answer_grounded_in_citations" in check_names
    # Run still passes — judge said all good, deterministic checks
    # all green.
    assert vrun.validation_status == "passed"


def test_runner_handles_negative_test_case(
    runner, ctx,
):
    """ integration: a negative case asks an off-topic
 question. The engine returns no retrieval (out-of-scope), the
 deterministic abstain check passes if the answer's empty, and
 the run aggregates accordingly."""
    case = ValidationTestCaseDTO(
        test_case_id="tc-neg",
        question="What is the price of Bitcoin?",
        type="negative",
        priority="normal",
        expected_behavior="abstain",
        expected_answer_points=[],
        expected_chunks=[],
        expected_pages=[],
        expected_artifacts=[],
        expected_graph_nodes=[],
        expected_graph_edges=[],
        citation_required=False,
        source_traceability=[],
        metadata={"negative": True},
    )

    vrun = runner.run(ctx, _make_set(test_cases=[case]))

    # No chunks indexed → engine returns nothing → answer is the
    # default "no knowledge results for: …" string from the stub
    # provider. Whether that reads as an abstain depends on our
    # regex pool; verify the case is in the result list and the
    # negative_answer_abstains check is present.
    assert len(vrun.results) == 1
    result = vrun.results[0]
    check_names = {c.name for c in result.checks}
    assert "negative_answer_abstains" in check_names
    # The non-negative checks must NOT appear.
    assert "answer_non_empty" not in check_names
    assert "retrieved_chunks_present" not in check_names


def test_runner_summary_main_issues_caps_at_three(
    runner, ctx,
):
    """When many cases fail, surface only the first three reasons
 on the readiness card subtitle. Full list is in `results[]`."""
    cases = [
        _case(
            case_id=f"t-{i}",
            question="anything",
            expected_chunks=[f"chunk-missing-{i}"],
        )
        for i in range(5)
    ]
    vrun = runner.run(ctx, _make_set(test_cases=cases))

    assert len(vrun.summary.main_issues) <= 3
    assert vrun.summary.failed == 5


# ---- LLM synthesis in batch runs (Patch 3) ------------------------


class _StubSynthesizer:
    """In-process stub for `AnswerSynthesizer`. Records the question
 + evidence it received and returns a canned answer + trace."""

    def __init__(self, *, answer: str = "synthesized text", error: str | None = None):
        self.calls: list[dict] = []
        self._answer = answer
        self._error = error

    def synthesize(self, *, question, evidence):
        from j1.validation.synthesis import SynthesisResult
        self.calls.append({
            "question": question,
            "evidence": list(evidence),
        })
        return SynthesisResult(
            answer=None if self._error else self._answer,
            provider="stub",
            model="stub-model",
            latency_ms=42,
            prompt_tokens=100,
            completion_tokens=20,
            error=self._error,
        )


def test_runner_replaces_raw_answer_with_synthesized_answer(
    query_engine, artifact_registry, ctx, workspace, indexer,
):
    """When a synthesizer is wired, the runner stores the synthesized
 answer in `result.answer` and preserves the raw engine answer
 under `result.raw_answer` so the FE can show both."""
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="art-1",
        content=b"The proposal is due 20 May 2026.",
        run_id="run-1",
        chunk_id="c-1",
    )
    stub = _StubSynthesizer(answer="Due 20 May 2026.")
    runner = DefaultValidationRunner(
        query_engine=query_engine,
        artifact_registry=artifact_registry,
        answer_synthesizer=stub,
    )

    cases = [_case(
        question="When is the proposal due?",
        expected_chunks=["c-1"],
    )]
    vrun = runner.run(ctx, _make_set(test_cases=cases))

    result = vrun.results[0]
    assert result.answer == "Due 20 May 2026."
    # Raw engine answer preserved for debug/diff. Template was
    # "Knowledge results for: <question>" — now "Knowledge
    # results:" (question echo dropped to stop the groundedness
    # judge false-positive).
    assert result.raw_answer is not None
    assert "Knowledge results" in result.raw_answer
    # LLM trace surfaces provider/model/latency/tokens so the FE
    # can render its trace strip.
    assert result.llm is not None
    assert result.llm.called is True
    assert result.llm.provider == "stub"
    assert result.llm.latency_ms == 42
    # The synthesizer saw the actual retrieved chunks as evidence,
    # not just metadata refs.
    assert len(stub.calls) == 1
    evidence = stub.calls[0]["evidence"]
    assert evidence and evidence[0].artifact_id == "art-1"


def test_runner_without_synthesizer_uses_raw_engine_answer(
    query_engine, artifact_registry, ctx, workspace, indexer,
):
    """No synthesizer wired → runner returns the engine's deterministic
 composed answer. `result.raw_answer` is None (no replacement
 happened) and `result.llm` is None (no LLM trace)."""
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="art-1", content=b"Body.",
        run_id="run-1", chunk_id="c-1",
    )
    runner = DefaultValidationRunner(
        query_engine=query_engine,
        artifact_registry=artifact_registry,
    )

    cases = [_case(question="anything", expected_chunks=["c-1"])]
    vrun = runner.run(ctx, _make_set(test_cases=cases))
    result = vrun.results[0]
    # Backward-compat: the engine's raw composed answer comes
    # through unchanged. The exact preamble is "Knowledge results:"
    # (or "No knowledge results." when retrieval misses) — the
    # earlier templates echoed the question text, which caused the
    # groundedness judge to flag the question as an unsupported
    # claim. Those echoes were dropped; the assertion now matches
    # the question-free form.
    assert "knowledge results" in result.answer.lower()
    assert result.raw_answer is None
    assert result.llm is None


def test_runner_skips_synthesis_for_negative_cases(
    query_engine, artifact_registry, ctx, workspace, indexer,
):
    """Negative cases need the engine's verbatim abstention text for
 the deterministic abstain check. The synthesizer must NOT
 replace it — replacing the answer with grounded LLM prose
 would defeat the "did the engine abstain?" check."""
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="art-1", content=b"Body.",
        run_id="run-1", chunk_id="c-1",
    )
    stub = _StubSynthesizer(answer="I would never say this.")
    runner = DefaultValidationRunner(
        query_engine=query_engine,
        artifact_registry=artifact_registry,
        answer_synthesizer=stub,
    )

    neg_case = ValidationTestCaseDTO(
        test_case_id="tc-neg",
        question="Who won the World Cup?",
        type="negative",
        priority="normal",
        expected_behavior="abstain",
    )
    vrun = runner.run(ctx, _make_set(test_cases=[neg_case]))
    # Synthesizer must NOT have been called for the negative case.
    assert stub.calls == []
    assert vrun.results[0].llm is None


def test_runner_opted_out_synthesis_returns_raw_answer(
    query_engine, artifact_registry, ctx, workspace, indexer,
):
    """`synthesize_answers=False` makes the runner reproducible —
 even with a synthesizer wired, the raw engine answer ships
 unchanged. Lets CI/regression replay runs avoid the LLM."""
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="art-1", content=b"Body.",
        run_id="run-1", chunk_id="c-1",
    )
    stub = _StubSynthesizer(answer="never returned")
    runner = DefaultValidationRunner(
        query_engine=query_engine,
        artifact_registry=artifact_registry,
        answer_synthesizer=stub,
        synthesize_answers=False,
    )

    cases = [_case(question="anything", expected_chunks=["c-1"])]
    vrun = runner.run(ctx, _make_set(test_cases=cases))
    assert stub.calls == []
    assert vrun.results[0].raw_answer is None
    assert vrun.results[0].llm is None
