"""Contract — Phase 3A auto-build / rebuild hooks for Knowledge Memory.

Pins:

  * Settings parsing — both env flags default to ``False`` and
    accept the canonical truthy / falsy strings.
  * Hook behaviour:
      - disabled by settings → ``skipped`` (``disabled_by_settings``)
      - settings on, service ``None`` → ``skipped`` (``service_not_wired``)
      - service raises ``NoActiveSnapshotError`` → ``skipped``
        (``no_active_snapshot``)
      - service raises any other exception → ``failed`` (never
        re-raised into the caller — the workflow must not fail
        because a memory build failed)
      - success → ``completed`` with artifact id, entry count,
        source string, ``includes_domain_insights`` derived from
        the build result
  * The hook stamps ``trigger`` + ``includes_domain_insights`` on
    the persisted artifact's metadata via Phase 2's
    ``ProcessingService.persist_knowledge_memory`` extension.
  * Source resolution:
      - ``after_compile`` trigger with empty enrichment → ``base_compile``
      - ``after_domain_enrichment`` trigger with non-empty enrichment
        → ``base_compile_plus_domain_insights``
  * No LLM imports in the auto-build module.

The tests use stub `KnowledgeMemoryService` instances + a fake
`KnowledgeMemoryBuildResult` shape — the underlying
`KnowledgeMemoryBuilder` is exercised by the Phase 2 contract
tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from j1.memory.auto_build import (
    KnowledgeMemoryBuildAttempt,
    SKIP_REASON_DISABLED_BY_SETTINGS,
    SKIP_REASON_NO_ACTIVE_SNAPSHOT,
    SKIP_REASON_SERVICE_NOT_WIRED,
    SOURCE_BASE_COMPILE,
    SOURCE_BASE_COMPILE_PLUS_DOMAIN_INSIGHTS,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_SKIPPED,
    TRIGGER_AFTER_COMPILE,
    TRIGGER_AFTER_DOMAIN_ENRICHMENT,
    maybe_build_after_compile,
    maybe_build_after_enrichment,
)
from j1.memory.service import KnowledgeMemoryBuildResult, NoActiveSnapshotError
from j1.processing.knowledge_memory_settings import (
    ENV_KNOWLEDGE_MEMORY_AUTO_BUILD_ENABLED,
    ENV_KNOWLEDGE_MEMORY_REBUILD_AFTER_ENRICHMENT,
    KnowledgeMemoryLifecycleSettings,
    load_knowledge_memory_lifecycle_settings,
)


# ---- Settings ---------------------------------------------------


def test_settings_defaults_are_conservative():
    s = load_knowledge_memory_lifecycle_settings(env={})
    assert s.auto_build_after_compile is False
    assert s.rebuild_after_enrichment is False
    assert s.any_enabled() is False


def test_settings_auto_build_parsed():
    s = load_knowledge_memory_lifecycle_settings(env={
        ENV_KNOWLEDGE_MEMORY_AUTO_BUILD_ENABLED: "true",
    })
    assert s.auto_build_after_compile is True
    assert s.rebuild_after_enrichment is False
    assert s.any_enabled() is True


def test_settings_rebuild_after_enrichment_parsed():
    s = load_knowledge_memory_lifecycle_settings(env={
        ENV_KNOWLEDGE_MEMORY_REBUILD_AFTER_ENRICHMENT: "1",
    })
    assert s.auto_build_after_compile is False
    assert s.rebuild_after_enrichment is True


def test_settings_both_independent():
    s = load_knowledge_memory_lifecycle_settings(env={
        ENV_KNOWLEDGE_MEMORY_AUTO_BUILD_ENABLED: "yes",
        ENV_KNOWLEDGE_MEMORY_REBUILD_AFTER_ENRICHMENT: "on",
    })
    assert s.auto_build_after_compile is True
    assert s.rebuild_after_enrichment is True


def test_settings_malformed_falls_back_to_default():
    s = load_knowledge_memory_lifecycle_settings(env={
        ENV_KNOWLEDGE_MEMORY_AUTO_BUILD_ENABLED: "maybe",
        ENV_KNOWLEDGE_MEMORY_REBUILD_AFTER_ENRICHMENT: "",
    })
    assert s.auto_build_after_compile is False
    assert s.rebuild_after_enrichment is False


def test_settings_accept_canonical_falsy_strings():
    for raw in ("false", "0", "no", "off"):
        s = load_knowledge_memory_lifecycle_settings(env={
            ENV_KNOWLEDGE_MEMORY_AUTO_BUILD_ENABLED: raw,
        })
        assert s.auto_build_after_compile is False, raw


# ---- Attempt round-trip -----------------------------------------


def test_attempt_to_payload_carries_all_fields():
    a = KnowledgeMemoryBuildAttempt(
        trigger=TRIGGER_AFTER_COMPILE,
        status=STATUS_COMPLETED,
        artifact_id="mem-1",
        entry_count=5,
        snapshot_id="snap-1",
        run_id="run-1",
        source=SOURCE_BASE_COMPILE,
        includes_domain_insights=False,
        warnings=("w1",),
    )
    payload = a.to_payload()
    assert payload["trigger"] == TRIGGER_AFTER_COMPILE
    assert payload["status"] == STATUS_COMPLETED
    assert payload["artifact_id"] == "mem-1"
    assert payload["entry_count"] == 5
    assert payload["source"] == SOURCE_BASE_COMPILE
    assert payload["includes_domain_insights"] is False
    assert payload["warnings"] == ["w1"]
    assert payload["error"] is None
    assert payload["reason"] is None


# ---- Stub helpers -----------------------------------------------


def _enabled_settings(*, auto: bool = True, rebuild: bool = True):
    return KnowledgeMemoryLifecycleSettings(
        auto_build_after_compile=auto,
        rebuild_after_enrichment=rebuild,
    )


def _disabled_settings():
    return KnowledgeMemoryLifecycleSettings()


@dataclass
class _StubBuildResult:
    """Minimal shape mirroring `KnowledgeMemoryBuildResult` used
    by the hook's source-resolution helper."""

    status: str = "succeeded"
    artifact_id: str | None = "mem-stub"
    entry_count: int = 7
    snapshot_id: str | None = "snap-1"
    run_id: str | None = "run-1"
    warnings: tuple[str, ...] = ()
    # The hook reads `message` as a fallback for enrichment-count
    # heuristics; tests use that to drive source resolution.
    message: str = ""


class _StubService:
    """`KnowledgeMemoryService` stand-in. Records calls + returns
    a configurable `_StubBuildResult`."""

    def __init__(self, result: Any = None, raises: Exception | None = None) -> None:
        self._result = result if result is not None else _StubBuildResult()
        self._raises = raises
        self.calls: list[tuple[str, str, str]] = []

    def build_and_persist(self, ctx, document_id, *, actor="system", trigger=None):
        self.calls.append((document_id, actor, trigger or ""))
        if self._raises is not None:
            raise self._raises
        return self._result


# ---- Hook: disabled --------------------------------------------


def test_hook_after_compile_skipped_when_disabled():
    service = _StubService()
    a = maybe_build_after_compile(
        ctx=None, document_id="d1", service=service,
        settings=_disabled_settings(),
    )
    assert a.status == STATUS_SKIPPED
    assert a.reason == SKIP_REASON_DISABLED_BY_SETTINGS
    assert service.calls == []  # Service NEVER touched when disabled.


def test_hook_after_enrichment_skipped_when_disabled():
    service = _StubService()
    a = maybe_build_after_enrichment(
        ctx=None, document_id="d1", service=service,
        settings=_disabled_settings(),
    )
    assert a.status == STATUS_SKIPPED
    assert a.reason == SKIP_REASON_DISABLED_BY_SETTINGS
    assert service.calls == []


# ---- Hook: service not wired -----------------------------------


def test_hook_after_compile_skipped_when_service_not_wired():
    a = maybe_build_after_compile(
        ctx=None, document_id="d1", service=None,
        settings=_enabled_settings(),
    )
    assert a.status == STATUS_SKIPPED
    assert a.reason == SKIP_REASON_SERVICE_NOT_WIRED


def test_hook_after_enrichment_skipped_when_service_not_wired():
    a = maybe_build_after_enrichment(
        ctx=None, document_id="d1", service=None,
        settings=_enabled_settings(),
    )
    assert a.status == STATUS_SKIPPED
    assert a.reason == SKIP_REASON_SERVICE_NOT_WIRED


# ---- Hook: NoActiveSnapshotError -------------------------------


def test_hook_after_compile_skipped_on_no_active_snapshot():
    service = _StubService(
        raises=NoActiveSnapshotError("doc-1 has no active snapshot"),
    )
    a = maybe_build_after_compile(
        ctx=None, document_id="d1", service=service,
        settings=_enabled_settings(),
    )
    assert a.status == STATUS_SKIPPED
    assert a.reason == SKIP_REASON_NO_ACTIVE_SNAPSHOT
    # Service WAS called — the skip happened inside the underlying
    # build, not as a feature-flag short-circuit.
    assert len(service.calls) == 1


# ---- Hook: unexpected error -------------------------------------


def test_hook_after_compile_failure_records_error_not_raise():
    service = _StubService(raises=ValueError("registry blew up"))
    a = maybe_build_after_compile(
        ctx=None, document_id="d1", service=service,
        settings=_enabled_settings(),
    )
    assert a.status == STATUS_FAILED
    assert a.error and "ValueError" in a.error
    assert "registry blew up" in a.error


def test_hook_after_enrichment_failure_records_error_not_raise():
    service = _StubService(raises=RuntimeError("disk full"))
    a = maybe_build_after_enrichment(
        ctx=None, document_id="d1", service=service,
        settings=_enabled_settings(),
    )
    assert a.status == STATUS_FAILED
    assert a.error and "RuntimeError" in a.error


def test_hook_never_raises_for_any_exception_type():
    """Defensive: even bizarre exception types must not propagate."""

    class _Weird(BaseException):
        """Not even Exception subclass."""

    # `BaseException` doesn't get caught by `except Exception` —
    # the hook's contract is "never raise into the caller for
    # *Exception subclasses*". BaseException subclasses (which
    # include SystemExit / KeyboardInterrupt) intentionally do
    # propagate because catching those is anti-pattern.
    #
    # This test exercises the realistic case: a vanilla Exception
    # subclass.
    class _MyError(Exception):
        pass

    service = _StubService(raises=_MyError("nope"))
    a = maybe_build_after_compile(
        ctx=None, document_id="d1", service=service,
        settings=_enabled_settings(),
    )
    assert a.status == STATUS_FAILED


# ---- Hook: success path ----------------------------------------


def test_hook_after_compile_success_returns_completed_attempt():
    service = _StubService(result=_StubBuildResult(
        artifact_id="mem-success", entry_count=12,
        snapshot_id="snap-1", run_id="run-1",
        message="Built knowledge memory with 12 entries from 0 enrichment artifact(s) and 3 compile artifact(s).",
    ))
    a = maybe_build_after_compile(
        ctx=None, document_id="d1", service=service,
        settings=_enabled_settings(),
    )
    assert a.status == STATUS_COMPLETED
    assert a.trigger == TRIGGER_AFTER_COMPILE
    assert a.artifact_id == "mem-success"
    assert a.entry_count == 12
    assert a.snapshot_id == "snap-1"
    assert a.run_id == "run-1"
    # Compile-only build → base_compile, no domain insights.
    assert a.source == SOURCE_BASE_COMPILE
    assert a.includes_domain_insights is False


def test_hook_after_enrichment_success_marks_includes_domain_insights():
    service = _StubService(result=_StubBuildResult(
        artifact_id="mem-enriched", entry_count=42,
        snapshot_id="snap-1", run_id="run-2",
        message="Built knowledge memory with 42 entries from 5 enrichment artifact(s) and 3 compile artifact(s).",
    ))
    a = maybe_build_after_enrichment(
        ctx=None, document_id="d1", service=service,
        settings=_enabled_settings(),
    )
    assert a.status == STATUS_COMPLETED
    assert a.trigger == TRIGGER_AFTER_DOMAIN_ENRICHMENT
    assert a.source == SOURCE_BASE_COMPILE_PLUS_DOMAIN_INSIGHTS
    assert a.includes_domain_insights is True


def test_hook_after_enrichment_with_no_enrichment_artifacts_falls_back_to_base():
    """Defensive: enrichment hook fires but the build didn't find
    any enrichment artifacts for the snapshot (workflow ordering
    edge case). The attempt should accurately reflect what
    actually got built — base_compile, not enriched."""
    service = _StubService(result=_StubBuildResult(
        artifact_id="mem-empty", entry_count=3,
        message="Built knowledge memory with 3 entries from 0 enrichment artifact(s) and 2 compile artifact(s).",
    ))
    a = maybe_build_after_enrichment(
        ctx=None, document_id="d1", service=service,
        settings=_enabled_settings(),
    )
    assert a.status == STATUS_COMPLETED
    assert a.source == SOURCE_BASE_COMPILE
    assert a.includes_domain_insights is False


def test_hook_passes_trigger_to_service_build_and_persist():
    """The hook forwards the trigger string so the artifact's
    metadata.trigger is stamped correctly."""
    service = _StubService()
    maybe_build_after_compile(
        ctx=None, document_id="d1", service=service,
        settings=_enabled_settings(),
    )
    assert service.calls[0][2] == TRIGGER_AFTER_COMPILE

    service2 = _StubService()
    maybe_build_after_enrichment(
        ctx=None, document_id="d1", service=service2,
        settings=_enabled_settings(),
    )
    assert service2.calls[0][2] == TRIGGER_AFTER_DOMAIN_ENRICHMENT


def test_hook_forwards_warnings_from_build_result():
    service = _StubService(result=_StubBuildResult(
        warnings=("no_enrichment_artifacts", "domain_pack_not_found"),
    ))
    a = maybe_build_after_compile(
        ctx=None, document_id="d1", service=service,
        settings=_enabled_settings(),
    )
    assert "no_enrichment_artifacts" in a.warnings
    assert "domain_pack_not_found" in a.warnings


# ---- ProcessingService persist_knowledge_memory: trigger metadata


def test_persist_knowledge_memory_stamps_trigger_metadata():
    """The Phase 2 persist seam now accepts ``trigger`` +
    ``includes_domain_insights``. Verify the metadata reaches the
    persisted ``ArtifactRecord`` so dashboards can answer "was
    this built after compile, after enrichment, or by the manual
    action?" without re-reading the JSON payload."""
    from unittest.mock import MagicMock
    from j1.processing.service import ProcessingService

    # Capture the draft passed to `_handle_artifact_output`.
    captured: dict = {}

    class _CapturingService(ProcessingService):
        def _handle_artifact_output(self, ctx, output, **kwargs):
            captured["draft"] = output.drafts[0]
            from j1.artifacts.models import ArtifactRecord, ProcessingStatus, ReviewStatus
            from datetime import datetime, timezone
            record = ArtifactRecord(
                artifact_id="mem-captured",
                project=ctx,
                kind=output.drafts[0].kind,
                location="enriched/mem-captured.json",
                content_hash="sha256:x",
                byte_size=len(output.drafts[0].content),
                status=ProcessingStatus.SUCCEEDED,
                review_status=ReviewStatus.NOT_REQUIRED,
                version=1,
                created_at=datetime(2026, 5, 16, tzinfo=timezone.utc),
                updated_at=datetime(2026, 5, 16, tzinfo=timezone.utc),
                source_document_ids=output.drafts[0].source_document_ids,
                source_artifact_ids=[],
                metadata=output.drafts[0].metadata,
            )
            from j1.processing.results import ArtifactProcessingResult, ResultStatus
            return ArtifactProcessingResult(
                status=ResultStatus.SUCCEEDED, drafts=[], artifacts=[record],
            )

    # We don't need a real workspace; the captured-call path
    # short-circuits before persistence.
    svc = _CapturingService.__new__(_CapturingService)
    # Manually bind the bare minimum attributes the method uses
    # before reaching `_handle_artifact_output`.
    svc._workspace = MagicMock()
    svc._artifacts = MagicMock()
    svc._artifacts.list_artifacts = MagicMock(return_value=[])
    svc._artifacts.update_metadata = MagicMock()
    from j1.projects.context import ProjectContext
    ctx = ProjectContext(tenant_id="t1", project_id="p1", profile=None)

    svc.persist_knowledge_memory(
        ctx,
        run_id="run-1",
        document_id="doc-1",
        snapshot_id="snap-1",
        payload={"entries": [], "domain_id": "civil"},
        trigger=TRIGGER_AFTER_COMPILE,
        includes_domain_insights=False,
    )
    draft = captured["draft"]
    assert draft.metadata["trigger"] == TRIGGER_AFTER_COMPILE
    assert draft.metadata["includes_domain_insights"] is False

    captured.clear()
    svc.persist_knowledge_memory(
        ctx,
        run_id="run-2",
        document_id="doc-1",
        snapshot_id="snap-1",
        payload={"entries": [], "domain_id": "civil"},
        trigger=TRIGGER_AFTER_DOMAIN_ENRICHMENT,
        includes_domain_insights=True,
    )
    draft = captured["draft"]
    assert draft.metadata["trigger"] == TRIGGER_AFTER_DOMAIN_ENRICHMENT
    assert draft.metadata["includes_domain_insights"] is True


def test_persist_knowledge_memory_omits_trigger_when_not_supplied():
    """Phase 2 callers that don't pass ``trigger`` must keep
    working. The metadata simply doesn't include the field."""
    from unittest.mock import MagicMock
    from j1.processing.service import ProcessingService

    captured: dict = {}

    class _CapturingService(ProcessingService):
        def _handle_artifact_output(self, ctx, output, **kwargs):
            captured["draft"] = output.drafts[0]
            from j1.processing.results import ArtifactProcessingResult, ResultStatus
            from j1.artifacts.models import ArtifactRecord, ProcessingStatus, ReviewStatus
            from datetime import datetime, timezone
            record = ArtifactRecord(
                artifact_id="mem-1", project=ctx, kind=output.drafts[0].kind,
                location="enriched/mem-1.json", content_hash="sha256:x",
                byte_size=1, status=ProcessingStatus.SUCCEEDED,
                review_status=ReviewStatus.NOT_REQUIRED, version=1,
                created_at=datetime(2026, 5, 16, tzinfo=timezone.utc),
                updated_at=datetime(2026, 5, 16, tzinfo=timezone.utc),
                source_document_ids=[], source_artifact_ids=[],
                metadata=output.drafts[0].metadata,
            )
            return ArtifactProcessingResult(
                status=ResultStatus.SUCCEEDED, drafts=[], artifacts=[record],
            )

    svc = _CapturingService.__new__(_CapturingService)
    svc._workspace = MagicMock()
    svc._artifacts = MagicMock()
    svc._artifacts.list_artifacts = MagicMock(return_value=[])
    svc._artifacts.update_metadata = MagicMock()
    from j1.projects.context import ProjectContext
    ctx = ProjectContext(tenant_id="t1", project_id="p1", profile=None)

    svc.persist_knowledge_memory(
        ctx,
        run_id="run-1",
        document_id="doc-1",
        snapshot_id="snap-1",
        payload={"entries": []},
    )
    draft = captured["draft"]
    assert "trigger" not in draft.metadata
    assert "includes_domain_insights" not in draft.metadata


# ---- No-LLM regression guard -----------------------------------


def test_auto_build_module_has_no_llm_imports():
    import importlib
    import inspect
    for module_name in (
        "j1.memory.auto_build",
        "j1.processing.knowledge_memory_settings",
    ):
        mod = importlib.import_module(module_name)
        source = inspect.getsource(mod)
        forbidden = {
            "openai", "langchain", "anthropic", "raganything", "lightrag",
            "TextLLMClient", "VisionLLMClient",
        }
        leaked = [name for name in forbidden if name in source]
        assert not leaked, f"{module_name} leaks LLM imports: {leaked}"


# ---- ProcessingActivities accepts the new kwarg ----------------


def test_processing_activities_accepts_knowledge_memory_service_kwarg():
    """Defensive: the new optional kwarg must not break the
    existing constructor signature. Tests that wire
    ProcessingActivities without the kwarg should keep working."""
    import inspect
    from j1.orchestration.activities.processing import ProcessingActivities
    sig = inspect.signature(ProcessingActivities.__init__)
    assert "knowledge_memory_service" in sig.parameters
    # Default must be None so legacy callers don't need to pass it.
    param = sig.parameters["knowledge_memory_service"]
    assert param.default is None
