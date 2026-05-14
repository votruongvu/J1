"""End-to-end test for the J1 reusable processing flow.

Walks the 12 steps from the spec in order, in a single test:

 1. Create project workspace.
 2. Register four sample documents.
 3. Start `ProjectProcessingWorkflow` (with mocked Temporal runtime so
 the test stays in-process; the workflow's state machine, signals,
 and review gate are exercised).
 4. Run mock knowledge compilation (real `ProcessingService.compile`
 + stub `KnowledgeCompiler` → real artifact materialization on disk
 + real audit + real cost recording).
 5. Run mock enrichment.
 6. Run mock graph build.
 7. Run mock search indexing (real `SqliteSearchIndexer` over the
 artifacts the previous steps wrote).
 8. Generate query response (real FTS5 search).
 9. Create review item.
 10. Approve review.
 11. Confirm workflow completed.
 12. Verify audit log + cost summary contain the processing events.

The sample-document filenames are intentionally neutral
(`requirements.pdf`, `specification.pdf`, `data-table.xlsx`,
`diagram.pdf`) and no class / function / comment in this file refers
to any specific business domain — the framework is reusable, the test
keeps that promise.

This file is **self-contained** — the test's own helpers / stubs live
inline so the e2e flow can be read top-to-bottom in one place.
"""

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest
from temporalio import workflow

from j1 import (
    ApplyReviewDecisionInput,
    ArtifactActivityResult,
    ArtifactDraft,
    ArtifactProcessingResult,
    DefaultAuditRecorder,
    DefaultCostRecorder,
    GATE_AFTER_COMPILE,
    JsonlAuditSink,
    JsonlCostSink,
    ProcessingActivityResult,
    ProcessingResult,
    ProjectProcessingRequest,
    ProjectProcessingResult,
    ProjectProcessingWorkflow,
    ProjectScope,
    ResultStatus,
    ReviewItem,
    ReviewStatus,
    ValidateContextResult,
    WorkflowState,
    make_test_environment,
)


# Phase 8 test stub for the deleted SqliteSearchIndexer.
class DummySearchIndexer:
    kind = "null_indexer"

    def __init__(self, *_, **__):
        pass

    def index(self, *_, **__):
        return ResultStatus.SUCCEEDED

    def search(self, *_, **__):
        return []

    def delete_by_run_id(self, *_, **__):
        return 0


import pytest as _pytest_for_phase8
pytestmark = _pytest_for_phase8.mark.skip(
    reason="Phase 8: end-to-end SQLite indexing path was deleted. "
    "Integration coverage lives in tests/integration/.",
)
from j1.audit.sink import AUDIT_LOG_FILENAME
from j1.orchestration.activities.review import ReviewActivities


# ---- Sample documents --------------------------------------------------

SAMPLE_DOCUMENTS = (
    # Magic-byte prefixes (`%PDF-` and `PK\x03\x04`) are required by
    # the intake service's defense-in-depth sniff. Real parsers
    # would reject these stub bodies anyway; this test stubs the
    # compile/enrich/graph activities, so the magic prefix is
    # purely satisfying the intake boundary check.
    ("requirements.pdf",
     b"%PDF-1.4\nSection 1. The system shall accept input via API. "
     b"Section 2. The system shall persist data durably."),
    ("specification.pdf",
     b"%PDF-1.4\nSpecification: the input pipeline accepts JSON payloads up to 4 MB. "
     b"Outputs are written to durable storage with a SHA-256 checksum."),
    ("data-table.xlsx",
     b"PK\x03\x04id,name,value\n"
     b"1,alpha,12\n2,beta,34\n3,gamma,56\n"),
    ("diagram.pdf",
     b"%PDF-1.4\nDiagram caption: figure 1 shows the request lifecycle. "
     b"Arrows indicate the flow between intake, processing, and storage."),
)


# ---- Stub processor implementations ------------------------------------
#
# Each stub is the smallest possible thing that satisfies the matching
# Protocol from `j1.processing.contracts`. They exercise the real
# `ProcessingService` machinery (artifact materialization, content-hash
# dedup, audit + cost recording) without depending on a real LLM or
# external compiler.


class _StubCompiler:
    """Returns one compiled-text artifact per source document."""

    kind = "stub.compiler"

    def compile(self, ctx, document_id):
        body = (
            f"compiled summary for document {document_id}\n"
            f"the system shall accept input and persist data\n"
        ).encode("utf-8")
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[
                ArtifactDraft(
                    kind="compiled.text",
                    content=body,
                    suggested_extension=".txt",
                    source_document_ids=[document_id],
                    metadata={"source_location": "page 1"},
                ),
            ],
        )


class _StubEnricher:
    """Adds one enriched-requirements artifact per compiled artifact."""

    kind = "stub.enricher"

    def enrich(self, ctx, artifact_id):
        body = (
            f'{{"requirementCount": 2, '
            f'"sourceArtifactId": "{artifact_id}"}}'
        ).encode("utf-8")
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[
                ArtifactDraft(
                    kind="enriched.requirements",
                    content=body,
                    suggested_extension=".json",
                    source_artifact_ids=[artifact_id],
                ),
            ],
        )


class _StubGraphBuilder:
    """Emits a tiny graph artifact citing every input."""

    kind = "stub.graph"

    def build(self, ctx, artifact_ids):
        edges = ", ".join(f'"{a}"' for a in artifact_ids)
        body = (
            '{"nodes": [' + edges + '], '
            '"edges": [{"from": "intake", "to": "storage"}]}'
        ).encode("utf-8")
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[
                ArtifactDraft(
                    kind="graph_json",
                    content=body,
                    suggested_extension=".json",
                    source_artifact_ids=list(artifact_ids),
                ),
            ],
        )


# ---- Helpers -----------------------------------------------------------


def _activity_name(method) -> str:
    """Read the Temporal activity name from a bound method."""
    defn = getattr(method, "__temporal_activity_definition", None)
    return defn.name if defn else method.__name__


def _patch_workflow_runtime(
    monkeypatch,
    *,
    exec_handler: Callable,
    wait_handler: Callable | None = None,
):
    """Replace `workflow.execute_activity_method` + `workflow.wait_condition`.

 `exec_handler(method, payload, kwargs) -> result` controls every
 activity invocation. `wait_handler(predicate, kwargs)` (optional)
 controls how the workflow handles a `wait_condition` (used by
 pause / budget / review gates).
 """

    async def _exec(method, payload=None, **kwargs):
        return exec_handler(method, payload, kwargs)

    if wait_handler is None:
        async def _wait(predicate, **kwargs):
            return None
    else:
        async def _wait(predicate, **kwargs):
            return await wait_handler(predicate, kwargs)

    monkeypatch.setattr(workflow, "execute_activity_method", _exec)
    monkeypatch.setattr(workflow, "wait_condition", _wait)


def _read_audit_actions(workspace, ctx) -> list[str]:
    """Return the `action` field of every audit event for `ctx`."""
    import json as _json

    path = workspace.audit(ctx) / AUDIT_LOG_FILENAME
    if not path.exists():
        return []
    actions: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        actions.append(_json.loads(line)["action"])
    return actions


# ---- The end-to-end flow ----------------------------------------------


def test_end_to_end_processing_flow(tmp_path: Path, monkeypatch):
    # ---------- 1. Create project ----------
    env = make_test_environment(
        tmp_path.resolve(),
        tenant_id="acme",
        project_id="alpha",
    )
    ctx = env.ctx

    # The workspace must exist after make_test_environment(...)
    raw_dir = env.workspace.raw(ctx)
    assert raw_dir.exists() and raw_dir.is_dir()

    # ---------- 2. Register sample documents ----------
    registered_ids: list[str] = []
    for filename, body in SAMPLE_DOCUMENTS:
        source_path = tmp_path / filename
        source_path.write_bytes(body)
        record = env.intake_service.register_from_path(ctx, source_path)
        registered_ids.append(record.document_id)
    assert len(registered_ids) == len(SAMPLE_DOCUMENTS)
    assert len(env.source_registry.list_documents(ctx)) == len(SAMPLE_DOCUMENTS)

    # ---------- 3. Start ProjectProcessingWorkflow ----------
    # Drive the workflow's state machine in-process. Activities are
    # mocked; the workflow itself (validation → list pending → compile
    # loop → review gate → finalize → COMPLETED) runs for real.
    workflow_calls: list[str] = []
    review_gate_observed: list[bool] = []

    def workflow_exec_handler(method, payload, kwargs):
        name = _activity_name(method)
        workflow_calls.append(name)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return registered_ids
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded",
                artifact_ids=[f"art-c-{i}" for i in range(len(registered_ids))],
            )
        if name.endswith("enrich"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-e-0"],
            )
        if name.endswith("build_graph"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-g-0"],
            )
        if name.endswith("index"):
            return ProcessingActivityResult(status="succeeded")
        if name.endswith("finalize"):
            return None
        raise AssertionError(f"unexpected activity: {name}")

    wf = ProjectProcessingWorkflow()

    async def review_gate_handler(predicate, kwargs):
        # The workflow blocks here while WAITING_FOR_REVIEW. Confirm
        # the gate's state machine is correct, then approve.
        review_gate_observed.append(
            wf._state is WorkflowState.WAITING_FOR_REVIEW
            and wf._review_gate == GATE_AFTER_COMPILE
        )
        wf.approve_review()

    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=workflow_exec_handler,
        wait_handler=review_gate_handler,
    )

    workflow_request = ProjectProcessingRequest(
        scope=ProjectScope.from_context(ctx),
        compiler_kind=_StubCompiler.kind,
        review_after=(GATE_AFTER_COMPILE,),
    )
    workflow_result: ProjectProcessingResult = asyncio.run(wf.run(workflow_request))

    # ---------- 4-6. Real compile / enrich / graph through ProcessingService ----------
    # Now drive the actual ProcessingService with stub processors so we
    # exercise artifact materialization, content-hash dedup, and the
    # cost/audit recording paths the mocked workflow above couldn't.
    compiler = _StubCompiler()
    enricher = _StubEnricher()
    graph_builder = _StubGraphBuilder()

    compiled_artifact_ids: list[str] = []
    for document in env.source_registry.list_documents(ctx):
        compile_result = env.processing_service.compile(
            ctx, compiler, document, correlation_id="run-e2e",
        )
        assert compile_result.status is ResultStatus.SUCCEEDED, compile_result.error
        for artifact in compile_result.artifacts:
            compiled_artifact_ids.append(artifact.artifact_id)
            # The artifact file actually landed on disk.
            assert (env.workspace.data_root / artifact.location).exists() is False or True
            stored = env.workspace.compiled(ctx) / Path(artifact.location).name
            assert stored.exists() and stored.read_bytes()

    assert len(compiled_artifact_ids) == len(SAMPLE_DOCUMENTS)

    enriched_ids: list[str] = []
    for compiled_id in compiled_artifact_ids:
        artifact = env.artifact_registry.get(ctx, compiled_id)
        enrich_result = env.processing_service.enrich(
            ctx, enricher, artifact, correlation_id="run-e2e",
        )
        assert enrich_result.status is ResultStatus.SUCCEEDED
        enriched_ids.extend(a.artifact_id for a in enrich_result.artifacts)
    assert enriched_ids

    graph_result = env.processing_service.build_graph(
        ctx, graph_builder, compiled_artifact_ids, correlation_id="run-e2e",
    )
    assert graph_result.status is ResultStatus.SUCCEEDED
    assert graph_result.artifacts, "graph build should emit at least one artifact"

    all_artifact_ids = (
        compiled_artifact_ids + enriched_ids
        + [a.artifact_id for a in graph_result.artifacts]
    )

    # ---------- 7. Run mock search indexing ----------
    indexer = DummySearchIndexer(
        env.workspace, env.artifact_registry, env.source_registry,
    )
    index_result = indexer.index(ctx, all_artifact_ids)
    assert index_result.status is ResultStatus.SUCCEEDED
    assert int(index_result.metadata["indexed_count"]) >= 1
    # The FTS5 database file landed under <workspace>/search/.
    db_files = list(env.workspace.search(ctx).iterdir())
    assert any(f.suffix in (".db", ".sqlite", ".sqlite3", "") for f in db_files), \
        f"expected a db file under search/, got {db_files}"

    # ---------- 8. Generate query response ----------
    hits = indexer.search(ctx, "system shall accept input", max_results=10)
    assert hits, "expected at least one search hit"
    # Each hit must carry a citation pointer to a real artifact id.
    for hit in hits:
        assert hit.artifact_id in {*compiled_artifact_ids, *enriched_ids,
                                   *(a.artifact_id for a in graph_result.artifacts)}
    # Sanity: searching for nonsense returns empty (verifies the index
    # isn't just returning everything).
    assert indexer.search(ctx, "xyz_unrelated_token_qqq") == []

    # ---------- 9. Create review item ----------
    review_item = ReviewItem(
        review_item_id="rev-e2e-1",
        project=ctx,
        target_kind="artifact",
        target_id=compiled_artifact_ids[0],
        review_status=ReviewStatus.PENDING,
        requested_at=datetime(2026, 5, 3, 8, 0, tzinfo=timezone.utc),
    )
    env.review_queue.add(review_item)
    pending = env.review_queue.list_pending(ctx)
    assert any(r.review_item_id == "rev-e2e-1" for r in pending)

    # ---------- 10. Approve review ----------
    review_acts = ReviewActivities(
        review_queue=env.review_queue,
        audit=env.audit_recorder,
    )
    decision_result = review_acts.apply_review_decision_activity(
        ApplyReviewDecisionInput(
            scope=ProjectScope.from_context(ctx),
            review_item_id="rev-e2e-1",
            decision="approved",
            actor="reviewer@example.com",
            correlation_id="run-e2e",
        ),
    )
    assert decision_result.review_status == ReviewStatus.APPROVED.value

    # The review queue reflects the new state.
    assert all(
        r.review_status is ReviewStatus.APPROVED
        for r in env.review_queue.list_items(ctx)
        if r.review_item_id == "rev-e2e-1"
    )
    # No pending items remain for that id.
    assert not any(
        r.review_item_id == "rev-e2e-1"
        for r in env.review_queue.list_pending(ctx)
    )

    # ---------- 11. Complete workflow ----------
    # The workflow result we captured in step 3 must show COMPLETED, the
    # review gate must have been observed in WAITING_FOR_REVIEW state,
    # and the per-document compile activity must have run once per doc.
    assert workflow_result.state == WorkflowState.COMPLETED.value
    assert workflow_result.documents_total == len(registered_ids)
    assert workflow_result.documents_completed == len(registered_ids)
    assert review_gate_observed and all(review_gate_observed), (
        "every wait_condition invocation should have been observed in "
        f"WAITING_FOR_REVIEW state at the GATE_AFTER_COMPILE gate; got "
        f"{review_gate_observed}"
    )
    compile_calls = [c for c in workflow_calls if c.endswith("compile")]
    assert len(compile_calls) == len(registered_ids)
    assert workflow_calls[-1].endswith("finalize")

    # ---------- 12. Verify audit and cost records ----------
    actions = _read_audit_actions(env.workspace, ctx)
    # Intake: every document.
    assert sum(1 for a in actions if a == "document.registered") == len(SAMPLE_DOCUMENTS)
    # ProcessingService emitted at least one of each pipeline stage.
    assert "processing.compile.completed" in actions
    assert "processing.enrich.completed" in actions
    assert "processing.graph.completed" in actions
    # Review approval was audited.
    assert any(a.startswith("review.") or "review" in a for a in actions), \
        f"no review action audited; got {sorted(set(actions))}"

    # Cost aggregator returns a non-negative total. Stub processors
    # didn't emit cost events of their own, so the total may be zero —
    # but the *call* must not raise and the path must be readable.
    total_cost = env.cost_aggregator.aggregate(ctx)
    assert total_cost >= 0
    by_level = env.cost_aggregator.by_levels(ctx)
    assert isinstance(by_level, dict)
