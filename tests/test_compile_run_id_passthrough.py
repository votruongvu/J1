"""Regression tests for ``run_id`` passthrough into compile.

Bug fixed:
   On reindex, the orchestration activity called
   ``compiler.compile(ctx, document_id)`` without forwarding the
   workflow's ``correlation_id`` as ``run_id``. The
   ``RAGAnythingCompiler.compile`` method had a ``run_id`` parameter
   (used to namespace LightRAG's ``working_dir`` per-run) but it
   stayed at its ``None`` default. Reindex therefore reused the
   first run's shared workdir; LightRAG's ``kv_store_doc_status``
   already marked the document as PROCESSED, the ``ainsert`` short-
   circuited to dedupe, and the new run got zero chunks →
   "Compile safety retry triggered (initial=standard → final=deep)"
   → "Final compile quality is LOW".

These tests pin both entry points (orchestration activity AND
legacy ``ProcessingService.compile``) to thread ``run_id`` through
when the concrete compiler accepts it.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from j1.processing.results import ArtifactProcessingResult, ResultStatus
from j1.projects.context import ProjectContext


# ---- Captured compile call ---------------------------------------


@dataclass
class _Capture:
    ctx: ProjectContext | None = None
    document_id: str | None = None
    run_id: str | None = None
    assessment_plan: object | None = None
    called: int = 0


class _SpyCompiler:
    """Stub compiler that records the kwargs it was called with —
    so the tests can assert run_id flowed through."""

    kind = "spy"

    def __init__(self) -> None:
        self.capture = _Capture()

    def compile(
        self,
        ctx: ProjectContext,
        document_id: str,
        *,
        run_id: str | None = None,
        assessment_plan: object | None = None,
    ) -> ArtifactProcessingResult:
        self.capture.ctx = ctx
        self.capture.document_id = document_id
        self.capture.run_id = run_id
        self.capture.assessment_plan = assessment_plan
        self.capture.called += 1
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[],
            metadata={"provider": "spy"},
        )


class _LegacySpyCompiler:
    """Stub compiler whose ``compile`` only takes the Protocol-minimum
    ``(ctx, document_id)`` — no ``run_id`` keyword. Validates that the
    inspect-based passthrough degrades gracefully for adapters that
    haven't opted in."""

    kind = "legacy-spy"

    def __init__(self) -> None:
        self.capture = _Capture()

    def compile(
        self, ctx: ProjectContext, document_id: str,
    ) -> ArtifactProcessingResult:
        self.capture.ctx = ctx
        self.capture.document_id = document_id
        self.capture.called += 1
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED, drafts=[],
        )


# ---- Orchestration activity passthrough --------------------------


def test_orchestration_passes_correlation_id_as_run_id():
    """The orchestration ``run_knowledge_compilation_activity``
    threads ``correlation_id`` into the compiler as ``run_id`` when
    the concrete compiler accepts the kwarg."""
    from j1.orchestration.activities.knowledge import (
        KnowledgeProcessingActivities,
    )
    from j1.orchestration.activities.payloads import (
        KnowledgeCompilationInput,
        ProjectScope,
    )

    # Pre-populate the document so the source lookup succeeds. The
    # _sources field uses a Mock that returns truthy from .get().
    sources = MagicMock()
    artifacts = MagicMock()
    audit = MagicMock()
    cost = MagicMock()
    spy = _SpyCompiler()

    acts = KnowledgeProcessingActivities(
        workspace=MagicMock(),
        sources=sources,
        artifacts=artifacts,
        audit=audit,
        cost=cost,
        compilers={"spy": spy},
    )

    acts.run_knowledge_compilation_activity(
        KnowledgeCompilationInput(
            scope=ProjectScope(tenant_id="t1", project_id="p1"),
            document_id="doc-a",
            processor_kind="spy",
            correlation_id="run-reindex-2",
        )
    )

    # Run id reached the compiler — this is the fix for the
    # "Compile safety retry triggered" reindex regression.
    assert spy.capture.called == 1
    assert spy.capture.run_id == "run-reindex-2"
    assert spy.capture.document_id == "doc-a"


def test_orchestration_legacy_compiler_without_run_id_kwarg():
    """Legacy compilers / mocks whose ``compile`` doesn't accept
    ``run_id`` keep working — the inspect detection silently drops
    the kwarg. No exceptions, compile still runs."""
    from j1.orchestration.activities.knowledge import (
        KnowledgeProcessingActivities,
    )
    from j1.orchestration.activities.payloads import (
        KnowledgeCompilationInput,
        ProjectScope,
    )

    spy = _LegacySpyCompiler()
    acts = KnowledgeProcessingActivities(
        workspace=MagicMock(),
        sources=MagicMock(),
        artifacts=MagicMock(),
        audit=MagicMock(),
        cost=MagicMock(),
        compilers={"legacy-spy": spy},
    )

    acts.run_knowledge_compilation_activity(
        KnowledgeCompilationInput(
            scope=ProjectScope(tenant_id="t1", project_id="p1"),
            document_id="doc-a",
            processor_kind="legacy-spy",
            correlation_id="run-1",
        )
    )

    # Legacy signature was respected — compile ran with only
    # (ctx, document_id), no TypeError.
    assert spy.capture.called == 1
    assert spy.capture.document_id == "doc-a"


def test_orchestration_no_correlation_id_means_no_run_id():
    """When the caller didn't supply a ``correlation_id`` (legacy
    direct dispatch), nothing to pass — ``run_id`` stays None at the
    compiler. Same as before this fix."""
    from j1.orchestration.activities.knowledge import (
        KnowledgeProcessingActivities,
    )
    from j1.orchestration.activities.payloads import (
        KnowledgeCompilationInput,
        ProjectScope,
    )

    spy = _SpyCompiler()
    acts = KnowledgeProcessingActivities(
        workspace=MagicMock(),
        sources=MagicMock(),
        artifacts=MagicMock(),
        audit=MagicMock(),
        cost=MagicMock(),
        compilers={"spy": spy},
    )
    acts.run_knowledge_compilation_activity(
        KnowledgeCompilationInput(
            scope=ProjectScope(tenant_id="t1", project_id="p1"),
            document_id="doc-a",
            processor_kind="spy",
            correlation_id=None,
        )
    )
    assert spy.capture.called == 1
    assert spy.capture.run_id is None


# ---- Legacy ProcessingService passthrough -----------------------


def test_processing_service_passes_correlation_id_as_run_id():
    """The legacy ``ProcessingService.compile`` ALSO threads
    ``correlation_id`` through as ``run_id``. Same fix at the
    second entry point — every test/adapter path that compiles
    through ProcessingService now namespaces LightRAG correctly."""
    from datetime import datetime, timezone
    from pathlib import Path

    from j1.documents.models import DocumentRecord
    from j1.processing.service import ProcessingService

    # Stub a DocumentRecord with the minimum fields ProcessingService
    # touches — `document_id` for compile invocation and
    # `_handle_artifact_output` (no drafts → no registration).
    doc = MagicMock(spec=DocumentRecord)
    doc.document_id = "doc-a"

    workspace = MagicMock()
    workspace.area.return_value = Path("/tmp")
    artifacts = MagicMock()
    audit = MagicMock()
    cost = MagicMock()

    svc = ProcessingService(
        workspace=workspace,
        artifact_registry=artifacts,
        audit=audit,
        cost=cost,
        clock=lambda: datetime(2026, 5, 12, tzinfo=timezone.utc),
        id_factory=lambda: "art-id",
    )

    spy = _SpyCompiler()
    svc.compile(
        MagicMock(),
        spy,
        doc,
        correlation_id="run-reindex-2",
    )

    assert spy.capture.called == 1
    assert spy.capture.run_id == "run-reindex-2"


def test_processing_service_passes_both_assessment_plan_and_run_id():
    """Both kwargs flow through together — adapters can use the plan
    AND the per-run scope simultaneously."""
    from datetime import datetime, timezone
    from pathlib import Path
    from unittest.mock import MagicMock

    from j1.documents.models import DocumentRecord
    from j1.processing.service import ProcessingService

    doc = MagicMock(spec=DocumentRecord)
    doc.document_id = "doc-a"
    workspace = MagicMock()
    workspace.area.return_value = Path("/tmp")

    svc = ProcessingService(
        workspace=workspace,
        artifact_registry=MagicMock(),
        audit=MagicMock(),
        cost=MagicMock(),
    )

    spy = _SpyCompiler()
    plan_sentinel = object()
    svc.compile(
        MagicMock(),
        spy,
        doc,
        correlation_id="run-1",
        assessment_plan=plan_sentinel,
    )

    assert spy.capture.run_id == "run-1"
    assert spy.capture.assessment_plan is plan_sentinel


# ---- disable_entity_extraction passthrough ----------------------


@dataclass
class _ProfileCapture:
    """Spy capture for the no-op-LLM keystone wiring."""
    document_id: str | None = None
    disable_entity_extraction: bool | None = None
    called: int = 0


class _ProfileSpyCompiler:
    """Stub compiler that records whether `disable_entity_extraction`
    flowed through. The kwarg is what hooks LightRAG's no-op
    `llm_model_func` into the `minimum_queryable` execution profile
    — without it the profile is a lie."""

    kind = "profile-spy"

    def __init__(self) -> None:
        self.capture = _ProfileCapture()

    def compile(
        self,
        ctx: ProjectContext,
        document_id: str,
        *,
        run_id: str | None = None,
        assessment_plan: object | None = None,
        disable_entity_extraction: bool = False,
    ) -> ArtifactProcessingResult:
        self.capture.document_id = document_id
        self.capture.disable_entity_extraction = disable_entity_extraction
        self.capture.called += 1
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED, drafts=[],
        )


def test_processing_service_forwards_disable_entity_extraction_when_supported():
    """The keystone hand-off: ProcessingService.compile must forward
    `disable_entity_extraction=True` to a compiler that accepts it.
    Without this, `minimum_queryable` runs would silently fall back
    to the real LLM and the profile would be a fiction."""
    from datetime import datetime, timezone
    from pathlib import Path

    from j1.documents.models import DocumentRecord
    from j1.processing.service import ProcessingService

    doc = MagicMock(spec=DocumentRecord)
    doc.document_id = "doc-a"
    workspace = MagicMock()
    workspace.area.return_value = Path("/tmp")

    svc = ProcessingService(
        workspace=workspace,
        artifact_registry=MagicMock(),
        audit=MagicMock(),
        cost=MagicMock(),
        clock=lambda: datetime(2026, 5, 15, tzinfo=timezone.utc),
        id_factory=lambda: "art-id",
    )

    spy = _ProfileSpyCompiler()
    svc.compile(
        MagicMock(),
        spy,
        doc,
        correlation_id="run-min",
        disable_entity_extraction=True,
    )
    assert spy.capture.called == 1
    assert spy.capture.disable_entity_extraction is True


def test_processing_service_default_keeps_extraction_enabled():
    """Default behaviour: when the kwarg is omitted, the real LLM
    runs. Pinned so a future refactor doesn't accidentally flip
    the default for everyone."""
    from datetime import datetime, timezone
    from pathlib import Path

    from j1.documents.models import DocumentRecord
    from j1.processing.service import ProcessingService

    doc = MagicMock(spec=DocumentRecord)
    doc.document_id = "doc-a"
    workspace = MagicMock()
    workspace.area.return_value = Path("/tmp")

    svc = ProcessingService(
        workspace=workspace,
        artifact_registry=MagicMock(),
        audit=MagicMock(),
        cost=MagicMock(),
        clock=lambda: datetime(2026, 5, 15, tzinfo=timezone.utc),
        id_factory=lambda: "art-id",
    )

    spy = _ProfileSpyCompiler()
    svc.compile(MagicMock(), spy, doc, correlation_id="run-x")
    assert spy.capture.called == 1
    assert spy.capture.disable_entity_extraction is False


def test_processing_service_silently_drops_kwarg_for_legacy_compilers():
    """Legacy compilers whose `compile` signature predates the
    `disable_entity_extraction` kwarg must keep working — the
    inspect-based forward drops it without raising."""
    from datetime import datetime, timezone
    from pathlib import Path

    from j1.documents.models import DocumentRecord
    from j1.processing.service import ProcessingService

    doc = MagicMock(spec=DocumentRecord)
    doc.document_id = "doc-a"
    workspace = MagicMock()
    workspace.area.return_value = Path("/tmp")

    svc = ProcessingService(
        workspace=workspace,
        artifact_registry=MagicMock(),
        audit=MagicMock(),
        cost=MagicMock(),
    )

    spy = _LegacySpyCompiler()
    svc.compile(
        MagicMock(),
        spy,
        doc,
        correlation_id="run-x",
        disable_entity_extraction=True,  # silently dropped
    )
    assert spy.capture.called == 1
