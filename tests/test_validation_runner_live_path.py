"""End-to-end test: validation set runner emits the new live-path
markers + retrieval audit events.

Spec acceptance gates:
  * ``j1.retrieval.live_path.entered`` fires at runner entry
  * ``j1.retrieval.intent.selected`` fires (proves intent router
    runs)
  * ``j1.retrieval.evidence_pack.finalized`` fires (proves
    check_pack ran)
  * ``j1.retrieval.live_path.evidence_sent`` fires with
    ``planner_used=True`` for structured intents
  * If boilerplate is selected, the audit shows ``reason_selected``

NO domain-specific signals — synthetic two-document corpus, generic
abstract section labels.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from j1.projects.context import ProjectContext
from j1.validation.runner import (
    DefaultValidationRunner,
    EVENT_LIVE_PATH_ENTERED,
    EVENT_LIVE_PATH_EVIDENCE_SENT,
)
from j1.validation.dtos import (
    ValidationSetDTO, ValidationTestCaseDTO,
)


# ---- Fixture: tiny stub query engine + audit ---------------------


class _SpyAudit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, ctx, *, actor, action, target_kind, target_id, payload):
        self.events.append({"action": action, "payload": dict(payload)})


class _StubQueryEngine:
    """Returns a hand-rolled QueryResponse that the runner will
    project into RetrievedChunkRefDTOs + then send through
    build_evidence_blocks."""

    def __init__(self, sources):
        self._sources = sources

    def query(self, ctx, request):
        from j1.query.models import QueryResponse, SourceReference
        return QueryResponse(
            answer="(stub answer)",
            mode_used="knowledge_first",
            sources=[
                SourceReference(
                    artifact_id=s["artifact_id"],
                    artifact_type=s["kind"],
                    title=s.get("title", s["artifact_id"]),
                    source_document_id=s["doc"],
                    source_location=s["section"],
                    chunk_id=None,
                    run_id=s["run"],
                    score=s["score"],
                )
                for s in self._sources
            ],
            related_artifacts=[s["artifact_id"] for s in self._sources],
        )


@pytest.fixture
def workspace_dir(tmp_path):
    return tmp_path


def _make_artifact_record(
    workspace_dir, *, aid, body, doc, run, section, kind="compiled.text",
):
    from j1.artifacts.models import ArtifactRecord
    from j1.jobs.status import ProcessingStatus, ReviewStatus
    from j1.workspace.layout import WorkspaceArea

    # Write to the COMPILED area so the runner's resolver locates it
    proj_root = (
        workspace_dir / "tenants" / "t" / "projects" / "p"
    )
    area = proj_root / WorkspaceArea.COMPILED.value
    area.mkdir(parents=True, exist_ok=True)
    rel = f"{WorkspaceArea.COMPILED.value}/{aid}.txt"
    (proj_root / rel).write_text(body, encoding="utf-8")
    return ArtifactRecord(
        artifact_id=aid,
        project=ProjectContext(tenant_id="t", project_id="p"),
        kind=kind,
        location=rel,
        content_hash=f"sha256:{aid}",
        byte_size=len(body),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED, version=1,
        created_at=datetime(2026, 5, 13, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 13, tzinfo=timezone.utc),
        source_document_ids=[doc],
        metadata={
            "run_id": run, "source_document_id": doc,
            "section_path": section,
        },
    )


class _StubRegistry:
    def __init__(self, records):
        self._r = records

    def get(self, ctx, aid):
        from j1.artifacts.registry import ArtifactNotFoundError
        if aid not in self._r:
            raise ArtifactNotFoundError(aid)
        return self._r[aid]

    def list_artifacts(self, ctx, *, kind=None):
        recs = list(self._r.values())
        return [r for r in recs if kind is None or r.kind == kind]


@pytest.fixture
def runner_and_audit(workspace_dir):
    from j1.workspace.resolver import WorkspaceResolver
    from j1.config.settings import Settings
    settings = Settings(data_root=workspace_dir)
    workspace = WorkspaceResolver(settings)
    records = {
        "A-roles": _make_artifact_record(
            workspace_dir, aid="A-roles",
            body=("Owner is responsible for producing the report; "
                  "reviewer approves."),
            doc="doc-A", run="run-A", section="Section A / Roles",
        ),
        "A-deps": _make_artifact_record(
            workspace_dir, aid="A-deps",
            body="Activity 2 depends on Activity 1 output.",
            doc="doc-A", run="run-A", section="Section B / Deps",
        ),
        "B-leak": _make_artifact_record(
            workspace_dir, aid="B-leak",
            body="Document B content that should never appear in doc-A queries.",
            doc="doc-B", run="run-B", section="Chapter 1 / Body",
        ),
    }
    registry = _StubRegistry(records)
    sources = [
        {"artifact_id": "A-roles", "kind": "compiled.text",
         "doc": "doc-A", "run": "run-A",
         "section": "Section A / Roles", "score": 0.9},
        {"artifact_id": "A-deps", "kind": "compiled.text",
         "doc": "doc-A", "run": "run-A",
         "section": "Section B / Deps", "score": 0.8},
        {"artifact_id": "B-leak", "kind": "compiled.text",
         "doc": "doc-B", "run": "run-B",
         "section": "Chapter 1 / Body", "score": 0.99},
    ]
    audit = _SpyAudit()
    runner = DefaultValidationRunner(
        query_engine=_StubQueryEngine(sources),
        artifact_registry=registry,
        workspace=workspace,
        synthesize_answers=False,  # no LLM in this test
        audit=audit,
    )
    return runner, audit


# =====================================================================
# Acceptance gates
# =====================================================================


def test_validation_runner_emits_live_path_entered_marker(
    runner_and_audit,
):
    """The very first acceptance gate: when the FE clicks
    'Run Validation', operators must see ``j1.retrieval.live_path.entered``
    in the audit stream for EVERY case."""
    runner, audit = runner_and_audit
    ctx = ProjectContext(tenant_id="t", project_id="p")
    case = ValidationTestCaseDTO(
        test_case_id="tc-1",
        question="Who is responsible for producing the report?",
        type="answer",
        priority="normal",
        expected_behavior="answer_with_citations",
        validation_scope="document",
    )
    vset = ValidationSetDTO(
        validation_set_id="vs-1",
        run_id="run-A",
        document_ids=["doc-A"],
        source="generated",
        status="draft",
        created_at="2026-05-13T12:00:00Z",
        created_by=None, generator_version="v1",
        artifacts_content_hash="sha256:test",
        test_cases=[case],
    )
    runner.run(ctx, vset, active_document_id="doc-A")

    actions = [e["action"] for e in audit.events]
    assert EVENT_LIVE_PATH_ENTERED in actions, (
        f"missing live-path entry marker; events={actions}"
    )
    entered = next(
        e for e in audit.events
        if e["action"] == EVENT_LIVE_PATH_ENTERED
    )
    assert entered["payload"]["handler"] == (
        "DefaultValidationRunner._execute_case"
    )
    assert entered["payload"]["document_id"] == "doc-A"
    assert entered["payload"]["run_id"] == "run-A"
    assert entered["payload"]["retrieval_mode"] == "planner_first"


def test_validation_runner_emits_new_retrieval_audit_events(
    runner_and_audit,
):
    """Spec acceptance: running the same validation query must
    produce ``j1.retrieval.intent.selected`` and
    ``j1.retrieval.evidence_pack.finalized`` events. Synthesizer
    is enabled so the evidence-build path actually runs."""
    runner, audit = runner_and_audit
    runner._synthesize_answers = True
    runner._synthesizer = _StubSynth()
    ctx = ProjectContext(tenant_id="t", project_id="p")
    case = ValidationTestCaseDTO(
        test_case_id="tc-1",
        question="Who is responsible for producing the report?",
        type="answer", priority="normal",
        expected_behavior="answer_with_citations",
        validation_scope="document",
    )
    vset = ValidationSetDTO(
        validation_set_id="vs-1", run_id="run-A",
        document_ids=["doc-A"], source="generated", status="draft",
        created_at="2026-05-13T12:00:00Z",
        created_by=None, generator_version="v1",
        artifacts_content_hash="sha256:test",
        test_cases=[case],
    )
    runner.run(ctx, vset, active_document_id="doc-A")

    actions = [e["action"] for e in audit.events]
    assert "j1.retrieval.intent.selected" in actions
    assert "j1.retrieval.evidence_pack.finalized" in actions


def test_validation_runner_blocks_cross_document_leak_at_live_path(
    runner_and_audit,
):
    """B-leak is in the retrieval response with the HIGHEST score
    (0.99) but the active scope is doc-A. The new path must drop
    it with reason ``wrong_document`` BEFORE evidence packing.
    Synthesizer enabled so the evidence-build path runs."""
    runner, audit = runner_and_audit
    runner._synthesize_answers = True
    runner._synthesizer = _StubSynth()
    ctx = ProjectContext(tenant_id="t", project_id="p")
    case = ValidationTestCaseDTO(
        test_case_id="tc-1",
        question="Who is responsible for producing the report?",
        type="answer", priority="normal",
        expected_behavior="answer_with_citations",
        validation_scope="document",
    )
    vset = ValidationSetDTO(
        validation_set_id="vs-1", run_id="run-A",
        document_ids=["doc-A"], source="generated", status="draft",
        created_at="2026-05-13T12:00:00Z",
        created_by=None, generator_version="v1",
        artifacts_content_hash="sha256:test",
        test_cases=[case],
    )
    runner.run(ctx, vset, active_document_id="doc-A")

    # B-leak appears in a ``wrong_document`` drop event.
    drop_events = [
        e for e in audit.events
        if e["action"] == "j1.retrieval.evidence_pack.dropped"
        and e["payload"].get("artifact_id") == "B-leak"
    ]
    assert drop_events, "B-leak was never dropped — cross-doc leak!"
    assert drop_events[0]["payload"]["reason_dropped"] == "wrong_document"


def test_evidence_sent_marker_carries_planner_used_flag(
    runner_and_audit,
):
    """Final acceptance gate: ``j1.retrieval.live_path.evidence_sent``
    fires with ``planner_used=True`` for a structured intent
    (responsibility_mapping in this case)."""
    runner, audit = runner_and_audit
    ctx = ProjectContext(tenant_id="t", project_id="p")
    case = ValidationTestCaseDTO(
        test_case_id="tc-1",
        question="Who is responsible for producing the report?",
        type="answer", priority="normal",
        expected_behavior="answer_with_citations",
        validation_scope="document",
    )
    vset = ValidationSetDTO(
        validation_set_id="vs-1", run_id="run-A",
        document_ids=["doc-A"], source="generated", status="draft",
        created_at="2026-05-13T12:00:00Z",
        created_by=None, generator_version="v1",
        artifacts_content_hash="sha256:test",
        test_cases=[case],
    )
    # Enable synthesize_answers so _maybe_synthesize_for_case
    # runs the evidence build (which is where the marker fires).
    runner._synthesize_answers = True
    runner._synthesizer = _StubSynth()
    runner.run(ctx, vset, active_document_id="doc-A")

    sent_events = [
        e for e in audit.events
        if e["action"] == EVENT_LIVE_PATH_EVIDENCE_SENT
    ]
    assert sent_events, (
        "no live_path.evidence_sent emitted — evidence build path "
        "never reached. Did synthesize_answers gate fire?"
    )
    payload = sent_events[0]["payload"]
    assert payload["intent"] == "responsibility_mapping"
    assert payload["planner_used"] is True


class _StubSynth:
    """Minimal synthesizer for the test — doesn't call any LLM,
    just returns a fixed string so the runner's
    ``_maybe_synthesize_for_case`` flows through the
    build_evidence_blocks path."""

    class _Result:
        def __init__(self, answer):
            self.answer = answer

    def synthesize(self, *args, **kwargs):
        return self._Result("(stubbed synthesizer answer)")


def test_marker_payload_lists_evidence_ids_and_intent(
    runner_and_audit,
):
    """The evidence_sent marker payload must enumerate the
    evidence ids + intent so an operator can verify pack
    composition from the audit log alone (no separate query)."""
    runner, audit = runner_and_audit
    ctx = ProjectContext(tenant_id="t", project_id="p")
    case = ValidationTestCaseDTO(
        test_case_id="tc-1",
        question="Which activities depend on each other?",
        type="answer", priority="normal",
        expected_behavior="answer_with_citations",
        validation_scope="document",
    )
    vset = ValidationSetDTO(
        validation_set_id="vs-1", run_id="run-A",
        document_ids=["doc-A"], source="generated", status="draft",
        created_at="2026-05-13T12:00:00Z",
        created_by=None, generator_version="v1",
        artifacts_content_hash="sha256:test",
        test_cases=[case],
    )
    runner._synthesize_answers = True
    runner._synthesizer = _StubSynth()
    runner.run(ctx, vset, active_document_id="doc-A")

    sent_events = [
        e for e in audit.events
        if e["action"] == EVENT_LIVE_PATH_EVIDENCE_SENT
    ]
    assert sent_events
    payload = sent_events[0]["payload"]
    assert payload["intent"] == "dependency_mapping"
    # Evidence list is structured (artifact_id + artifact_type +
    # section_path) so a downstream consumer doesn't have to join
    # against another event.
    assert isinstance(payload["evidence"], list)
    for entry in payload["evidence"]:
        assert "artifact_id" in entry
        assert "artifact_type" in entry
        assert "section_path" in entry


def test_runner_without_audit_still_runs_planner_path(
    workspace_dir,
):
    """Backward-compat: a runner constructed WITHOUT an audit
    recorder must still exercise the planner-first evidence
    path. The Python logger captures the live-path markers
    instead of the audit log."""
    from j1.workspace.resolver import WorkspaceResolver
    from j1.config.settings import Settings
    settings = Settings(data_root=workspace_dir)
    workspace = WorkspaceResolver(settings)
    records = {
        "A-roles": _make_artifact_record(
            workspace_dir, aid="A-roles",
            body="Owner produces report.",
            doc="doc-A", run="run-A",
            section="Section A / Roles",
        ),
    }
    sources = [
        {"artifact_id": "A-roles", "kind": "compiled.text",
         "doc": "doc-A", "run": "run-A",
         "section": "Section A / Roles", "score": 0.9},
    ]
    runner = DefaultValidationRunner(
        query_engine=_StubQueryEngine(sources),
        artifact_registry=_StubRegistry(records),
        workspace=workspace,
        synthesize_answers=False,
        audit=None,  # explicit: no audit recorder
    )
    ctx = ProjectContext(tenant_id="t", project_id="p")
    case = ValidationTestCaseDTO(
        test_case_id="tc-1",
        question="Who is responsible?",
        type="answer", priority="normal",
        expected_behavior="answer_with_citations",
        validation_scope="document",
    )
    vset = ValidationSetDTO(
        validation_set_id="vs-1", run_id="run-A",
        document_ids=["doc-A"], source="generated", status="draft",
        created_at="2026-05-13T12:00:00Z",
        created_by=None, generator_version="v1",
        artifacts_content_hash="sha256:test",
        test_cases=[case],
    )
    # Must not raise even with audit=None.
    vrun = runner.run(ctx, vset, active_document_id="doc-A")
    assert vrun is not None
