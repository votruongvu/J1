import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger("j1.processing.service")

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactRegistry
from j1.audit.recorder import AuditRecorder
from j1.cost.recorder import CostRecorder
from j1.documents.models import DocumentRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.processing.contracts import (
    EnrichmentProcessor,
    GraphBuilder,
    KnowledgeCompiler,
    QueryProvider,
    SearchIndexer,
)
from j1.processing.results import (
    ArtifactDraft,
    ArtifactProcessingResult,
    ProcessingResult,
    QueryResult,
    ResultStatus,
)
from j1.projects.context import ProjectContext
from j1.workspace.layout import WorkspaceArea
from j1.workspace.resolver import WorkspaceResolver

ACTION_COMPILE_OK = "processing.compile.completed"
ACTION_COMPILE_FAIL = "processing.compile.failed"
ACTION_ENRICH_OK = "processing.enrich.completed"
ACTION_ENRICH_FAIL = "processing.enrich.failed"
ACTION_GRAPH_OK = "processing.graph.completed"
ACTION_GRAPH_FAIL = "processing.graph.failed"
ACTION_INDEX_OK = "processing.index.completed"
ACTION_INDEX_FAIL = "processing.index.failed"
ACTION_QUERY_OK = "processing.query.completed"
ACTION_QUERY_FAIL = "processing.query.failed"
# Generic "a non-compile / non-enrich report artifact was
# persisted" event. Used by ``persist_compile_strategy_report``,
# ``persist_post_compile_enrich_plan``,
# ``persist_compile_result_summary``,
# ``persist_final_ingestion_report``,
# ``persist_initial_execution_plan``,
# ``persist_final_summary``, and ``persist_error_report``.
# Replaces the previous misuse of ``ACTION_COMPILE_OK`` for these
# auxiliary reports — the legacy ``processing.compile.completed``
# is still emitted alongside the new event for one release so UI
# consumers continue to work while they're migrated.
ACTION_REPORT_PERSISTED = "processing.report.persisted"

TARGET_DOCUMENT = "document"
TARGET_ARTIFACT = "artifact"
TARGET_ARTIFACT_SET = "artifact_set"
TARGET_QUERY = "query"

CHECKSUM_PREFIX = "sha256:"


# Artifact kinds that MUST carry ``metadata.run_id`` at registration.
# Mirror of ``orchestration.activities.knowledge._LINEAGE_REQUIRED_KINDS``
# — duplicated here (rather than imported) so the processing layer
# stays free of orchestration coupling, and so a future producer
# that bypasses orchestration still hits the same gate. Keep the
# two lists in sync.
_LINEAGE_REQUIRED_KINDS: frozenset[str] = frozenset({
    "graph_json",
    "chunk",
    "compiled.text",
    "compiled.json",
    "parsed_content_manifest",
    "enriched.tables",
    "enriched.visuals",
    "enriched.document_map",
    "enriched.requirements",
    "enriched.formulas",
    "enriched.risks",
    "enriched.consistency_findings",
    "enriched.source_map",
    "enriched.confidence_assessment",
    "graph_corpus",
    "report",
})


class LineageError(RuntimeError):
    """Raised when a lineage-required artifact is registered without
    ``metadata.run_id``. Mirrors
    ``orchestration.activities.knowledge.LineageError`` so callers
    can catch a single type regardless of which registration path
    raised."""


class ProcessingService:
    def __init__(
        self,
        workspace: WorkspaceResolver,
        artifact_registry: ArtifactRegistry,
        audit: AuditRecorder,
        cost: CostRecorder,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        *,
        smart_query_orchestrator: "object | None" = None,
    ) -> None:
        self._workspace = workspace
        self._artifacts = artifact_registry
        self._audit = audit
        self._cost = cost
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        # SmartQueryOrchestrator — when wired, ``query`` delegates
        # to the new pipeline (intent classification → multi-route
        # retrieval → grouped evidence → sufficiency gate →
        # synthesis → citation binder → answer-quality gate)
        # INSTEAD OF the legacy QueryProvider path. The provider
        # passed to ``query`` becomes informational (the orchestrator
        # picks its own routes). Optional so existing tests +
        # deployments keep working unchanged.
        #
        # Typed as ``object`` to avoid a static import from
        # ``j1.processing`` onto ``j1.query`` — the only API the
        # service consumes is ``.run(OrchestratorRequest)``.
        self._smart_query_orchestrator = smart_query_orchestrator

    def compile(
        self,
        ctx: ProjectContext,
        compiler: KnowledgeCompiler,
        document: DocumentRecord,
        *,
        actor: str = "system",
        correlation_id: str | None = None,
        assessment_plan: object | None = None,
        target_snapshot_id: str | None = None,
    ) -> ArtifactProcessingResult:
        # Detect whether the compiler accepts ``assessment_plan``,
        # ``run_id``, and ``snapshot_id`` (the ``KnowledgeCompiler``
        # Protocol's ``compile`` signature only requires
        # ``(ctx, document_id)``; concrete adapters opt in via
        # additive kwargs). Mock compilers + legacy implementations
        # stay working without changes.
        #
        # ``run_id`` MUST flow through whenever the caller supplied a
        # ``correlation_id``. Concrete adapters (RAGAnythingCompiler)
        # use it to namespace LightRAG's per-run ``working_dir``;
        # without it a reindex shares the workdir with the original
        # run, hits LightRAG's doc-status dedupe, and produces zero
        # new chunks → "Compile safety retry triggered" → LOW
        # quality.
        #
        # ``snapshot_id`` (Phase 9): when provided, lets the
        # compiler land outputs under the snapshot-scoped workspace
        # directly, instead of round-tripping through
        # ``get_or_create_for_run``.
        compile_kwargs: dict = {}
        try:
            import inspect
            sig = inspect.signature(compiler.compile)
            if assessment_plan is not None and "assessment_plan" in sig.parameters:
                compile_kwargs["assessment_plan"] = assessment_plan
            if correlation_id and "run_id" in sig.parameters:
                compile_kwargs["run_id"] = correlation_id
            if target_snapshot_id and "snapshot_id" in sig.parameters:
                compile_kwargs["snapshot_id"] = target_snapshot_id
        except (TypeError, ValueError):
            # Builtins / C extensions don't expose a signature;
            # fall back to no kwargs (legacy behaviour).
            pass
        try:
            output = compiler.compile(ctx, document.document_id, **compile_kwargs)
        except Exception as exc:
            return self._fail_artifact(
                ctx,
                action=ACTION_COMPILE_FAIL,
                target_kind=TARGET_DOCUMENT,
                target_id=document.document_id,
                exc=exc,
                actor=actor,
                correlation_id=correlation_id,
                processor_kind=getattr(compiler, "kind", None),
            )
        return self._handle_artifact_output(
            ctx,
            output,
            area=WorkspaceArea.COMPILED,
            action=ACTION_COMPILE_OK,
            target_kind=TARGET_DOCUMENT,
            target_id=document.document_id,
            actor=actor,
            correlation_id=correlation_id,
            processor_kind=getattr(compiler, "kind", None),
            source_document_ids=[document.document_id],
        )

    def persist_error_report(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        document_id: str | None,
        failure_code: str,
        failure_message: str,
        stage: str | None = None,
        step: str | None = None,
        step_results: list[dict] | None = None,
        actor: str = "system",
    ) -> ArtifactRecord:
        """Write a structured `error_report.json` artifact describing
 why a run failed.

 Persisted via the same `_register_draft` path successful
 artifacts use, so the FE's existing artifact-listing surface
 picks it up automatically. Tagged with `metadata.run_id` so
 `_resolve_run_artifacts` returns it under the failed run.
 Best-effort: any persistence failure is logged + raised back
 to the caller (typically the workflow's failure handler) so
 it can decide whether to swallow the error (we don't want a
 broken error-report path to mask the original failure)."""
        import json as _json
        from j1.processing.results import (
            ARTIFACT_KIND_ERROR_REPORT,
            ArtifactDraft,
            ArtifactProcessingResult,
            ResultStatus,
        )

        payload = {
            "schema_version": "1",
            "run_id": run_id,
            "document_id": document_id,
            "failure_code": failure_code,
            "failure_message": failure_message,
            "last_stage": stage,
            "last_step": step,
            # The per-step status table at the moment of failure —
            # tells the operator WHICH step actually failed and
            # which prior steps already succeeded.
            "step_results": step_results or [],
            "created_at": self._clock().isoformat(),
        }
        draft = ArtifactDraft(
            kind=ARTIFACT_KIND_ERROR_REPORT,
            content=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            suggested_extension=".json",
            source_document_ids=[document_id] if document_id else [],
            metadata={
                "filename": f"error_report_{run_id}.json",
                "failure_code": failure_code,
                "last_stage": stage or "",
                "last_step": step or "",
            },
        )
        result = ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED, drafts=[draft],
        )
        from j1.workspace.layout import WorkspaceArea
        # action + target_kind constants live at module top of this file
        # See ``ACTION_REPORT_PERSISTED`` rationale at module top.
        registered = self._handle_artifact_output(
            ctx, result,
            area=WorkspaceArea.COMPILED,
            action=ACTION_REPORT_PERSISTED,
            target_kind=TARGET_DOCUMENT,
            target_id=document_id or run_id,
            actor=actor,
            correlation_id=run_id,
            processor_kind=None,
            source_document_ids=[document_id] if document_id else [],
            legacy_action=ACTION_COMPILE_OK,
        )
        if registered.artifacts:
            return registered.artifacts[0]
        # `_handle_artifact_output` only returns empty when registration
        # itself failed — bubble up to caller.
        raise RuntimeError(
            f"failed to persist error_report artifact for run {run_id!r}"
        )

    def persist_compile_strategy_report(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        document_id: str | None,
        payload: dict,
        actor: str = "system",
    ) -> ArtifactRecord:
        """Write a `compile_strategy_report` artifact summarising the
 AssessmentPlan + CompileConfig + per-attempt timeline + final
 quality verdict for one document's compile stage. Uses the same
 artifact registration helper as the other per-run reports so the
 FE artifact-listing surface picks it up uniformly.

 `payload` is the JSON-serialisable dict the workflow builds
 — the service doesn't import the assessment / retry
 dataclasses to keep the dependency tree thin."""
        import json as _json
        from j1.processing.results import (
            ARTIFACT_KIND_COMPILE_STRATEGY_REPORT,
            ArtifactDraft,
            ArtifactProcessingResult,
            ResultStatus,
        )
        from j1.workspace.layout import WorkspaceArea
        # action + target_kind constants live at module top of this file

        attempts = payload.get("attempts") or []
        attempts_count = len(attempts)
        final_quality = str(payload.get("final_compile_quality") or "unknown")
        retry_used = bool(payload.get("retry_used"))
        draft = ArtifactDraft(
            kind=ARTIFACT_KIND_COMPILE_STRATEGY_REPORT,
            content=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            suggested_extension=".json",
            source_document_ids=[document_id] if document_id else [],
            metadata={
                "filename": f"compile_strategy_{run_id}.json",
                "attempts_count": attempts_count,
                "retry_used": retry_used,
                "final_compile_quality": final_quality,
                "initial_mode": payload.get("initial_mode"),
                "final_mode": payload.get("final_mode"),
            },
        )
        result = ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED, drafts=[draft],
        )
        registered = self._handle_artifact_output(
            ctx, result,
            area=WorkspaceArea.COMPILED,
            action=ACTION_REPORT_PERSISTED,
            target_kind=TARGET_DOCUMENT,
            target_id=document_id or run_id,
            actor=actor,
            correlation_id=run_id,
            processor_kind=None,
            source_document_ids=[document_id] if document_id else [],
            legacy_action=ACTION_COMPILE_OK,
        )
        if registered.artifacts:
            return registered.artifacts[0]
        raise RuntimeError(
            "failed to persist compile_strategy_report artifact for "
            f"run {run_id!r}"
        )

    def persist_post_compile_enrich_plan(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        document_id: str | None,
        payload: dict,
        actor: str = "system",
    ) -> ArtifactRecord:
        """Write a `post_compile_enrich_plan` artifact carrying the
 rule-based enrich-assessment verdict. Mirrors
 `persist_compile_strategy_report`'s shape so the FE artifact
 listing picks it up uniformly. `payload` is the
 `PostCompileEnrichPlan.to_payload` dict — the service layer
 doesn't import `enrich_assessment` to keep the dependency
 graph thin."""
        import json as _json
        from j1.processing.results import (
            ARTIFACT_KIND_POST_COMPILE_ENRICH_PLAN,
            ArtifactDraft,
            ArtifactProcessingResult,
            ResultStatus,
        )
        from j1.workspace.layout import WorkspaceArea
        # action + target_kind constants live at module top of this file

        overall = str(payload.get("overall_recommendation") or "optional")
        recommended = list(payload.get("recommended_tasks") or [])
        decision_source = str(payload.get("decision_source") or "rule_based")
        draft = ArtifactDraft(
            kind=ARTIFACT_KIND_POST_COMPILE_ENRICH_PLAN,
            content=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            suggested_extension=".json",
            source_document_ids=[document_id] if document_id else [],
            metadata={
                "filename": f"post_compile_enrich_plan_{run_id}.json",
                "overall_recommendation": overall,
                "recommended_task_count": len(recommended),
                "decision_source": decision_source,
            },
        )
        result = ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED, drafts=[draft],
        )
        registered = self._handle_artifact_output(
            ctx, result,
            area=WorkspaceArea.COMPILED,
            action=ACTION_REPORT_PERSISTED,
            target_kind=TARGET_DOCUMENT,
            target_id=document_id or run_id,
            actor=actor,
            correlation_id=run_id,
            processor_kind=None,
            source_document_ids=[document_id] if document_id else [],
            legacy_action=ACTION_COMPILE_OK,
        )
        if registered.artifacts:
            return registered.artifacts[0]
        raise RuntimeError(
            "failed to persist post_compile_enrich_plan artifact for "
            f"run {run_id!r}"
        )

    def persist_enrichment_result(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        document_id: str | None,
        payload: dict,
        actor: str = "system",
    ) -> ArtifactRecord:
        """Write the typed enrichment overlay as an
 `enrichment_result` artifact. Same persistence shape as
 the other JSON-overlay artifacts.

 `payload` is the `EnrichmentResult.to_payload` dict — the
 service layer doesn't import `enrichment_overlay` to keep
 the dependency graph thin."""
        import json as _json
        from j1.processing.results import (
            ARTIFACT_KIND_ENRICHMENT_RESULT,
            ArtifactDraft,
            ArtifactProcessingResult,
            ResultStatus,
        )
        from j1.workspace.layout import WorkspaceArea

        status = str(payload.get("status") or "succeeded")
        module_outcomes = payload.get("module_outcomes") or []
        module_count = len(module_outcomes)
        run_count = sum(
            1 for o in module_outcomes
            if isinstance(o, dict) and o.get("status") == "run"
        )
        domain_id = str(payload.get("domain_id") or "none")
        draft = ArtifactDraft(
            kind=ARTIFACT_KIND_ENRICHMENT_RESULT,
            content=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            suggested_extension=".json",
            source_document_ids=[document_id] if document_id else [],
            metadata={
                "filename": f"enrichment_result_{run_id}.json",
                "status": status,
                "module_count": module_count,
                "module_run_count": run_count,
                "domain_id": domain_id,
            },
        )
        result = ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED, drafts=[draft],
        )
        # Action string: this is an enrichment overlay being
        # persisted, NOT a compile artifact. Previously emitted
        # ``processing.compile.completed`` here, which overloaded the
        # compile event for non-compile artifacts (the
        # ``processing.compile.completed`` audit row count was
        # ~9× the real compile count, breaking attribution).
        registered = self._handle_artifact_output(
            ctx, result,
            area=WorkspaceArea.ENRICHED,
            action=ACTION_ENRICH_OK,
            target_kind=TARGET_DOCUMENT,
            target_id=document_id or run_id,
            actor=actor,
            correlation_id=run_id,
            processor_kind=None,
            source_document_ids=[document_id] if document_id else [],
        )
        if registered.artifacts:
            return registered.artifacts[0]
        raise RuntimeError(
            "failed to persist enrichment_result artifact for "
            f"run {run_id!r}"
        )

    def persist_compile_result_summary(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        document_id: str | None,
        payload: dict,
        actor: str = "system",
    ) -> ArtifactRecord:
        """Write a `compile_result_summary` artifact carrying the
 typed normalized compile result (chunks_count,
 detected_tables / detected_images, quality_signals, retry
 history, etc.). Mirrors `persist_initial_execution_plan`'s
 shape so FE artifact listings surface it uniformly.

 `payload` is the `NormalizedCompileResult.to_payload`
 dict — the service layer doesn't import `compile_result`
 to keep the dependency graph thin."""
        import json as _json
        from j1.processing.results import (
            ARTIFACT_KIND_COMPILE_RESULT_SUMMARY,
            ArtifactDraft,
            ArtifactProcessingResult,
            ResultStatus,
        )
        from j1.workspace.layout import WorkspaceArea

        engine = str(payload.get("compile_engine") or "raganything")
        status = str(payload.get("status") or "succeeded")
        chunks = int(payload.get("chunks_count") or 0)
        verdict = str(payload.get("final_quality_verdict") or "unknown")
        draft = ArtifactDraft(
            kind=ARTIFACT_KIND_COMPILE_RESULT_SUMMARY,
            content=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            suggested_extension=".json",
            source_document_ids=[document_id] if document_id else [],
            metadata={
                "filename": f"compile_result_summary_{run_id}.json",
                "compile_engine": engine,
                "status": status,
                "chunks_count": chunks,
                "final_quality_verdict": verdict,
            },
        )
        result = ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED, drafts=[draft],
        )
        registered = self._handle_artifact_output(
            ctx, result,
            area=WorkspaceArea.COMPILED,
            action=ACTION_REPORT_PERSISTED,
            target_kind=TARGET_DOCUMENT,
            target_id=document_id or run_id,
            actor=actor,
            correlation_id=run_id,
            processor_kind=None,
            source_document_ids=[document_id] if document_id else [],
            legacy_action=ACTION_COMPILE_OK,
        )
        if registered.artifacts:
            return registered.artifacts[0]
        raise RuntimeError(
            "failed to persist compile_result_summary artifact for "
            f"run {run_id!r}"
        )

    def persist_final_ingestion_report(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        document_id: str | None,
        payload: dict,
        actor: str = "system",
    ) -> ArtifactRecord:
        """Write a `final_ingestion_report` artifact — the
 end-to-end run summary aggregating compile + enrichment +
 finalize state. Operator + FE single source of truth at
 terminal.

 `payload` is the `FinalIngestionReport.to_payload` dict —
 the service layer doesn't import the report module to keep
 the dependency graph thin."""
        import json as _json
        from j1.processing.results import (
            ARTIFACT_KIND_FINAL_INGESTION_REPORT,
            ArtifactDraft,
            ArtifactProcessingResult,
            ResultStatus,
        )
        from j1.workspace.layout import WorkspaceArea

        final_status = str(payload.get("final_status") or "unknown")
        domain = str(payload.get("domain_profile_id") or "none")
        # Compact filename — one report per (run, doc) pair.
        draft = ArtifactDraft(
            kind=ARTIFACT_KIND_FINAL_INGESTION_REPORT,
            content=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            suggested_extension=".json",
            source_document_ids=[document_id] if document_id else [],
            metadata={
                "filename": f"final_ingestion_report_{run_id}.json",
                "final_status": final_status,
                "domain_profile_id": domain,
                "schema_version": str(payload.get("schema_version") or "1.0"),
            },
        )
        result = ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED, drafts=[draft],
        )
        registered = self._handle_artifact_output(
            ctx, result,
            area=WorkspaceArea.COMPILED,
            action=ACTION_REPORT_PERSISTED,
            target_kind=TARGET_DOCUMENT,
            target_id=document_id or run_id,
            actor=actor,
            correlation_id=run_id,
            processor_kind=None,
            source_document_ids=[document_id] if document_id else [],
            legacy_action=ACTION_COMPILE_OK,
        )
        if registered.artifacts:
            return registered.artifacts[0]
        raise RuntimeError(
            "failed to persist final_ingestion_report artifact for "
            f"run {run_id!r}"
        )

    def persist_initial_execution_plan(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        document_id: str | None,
        payload: dict,
        actor: str = "system",
    ) -> ArtifactRecord:
        """Write an `initial_execution_plan` artifact carrying the
 pre-compile run plan (selected domain, enrichment_policy,
 candidate modules, cheap signals, wrapped compile plan).

 Mirrors `persist_post_compile_enrich_plan` so the FE artifact
 listing surfaces it uniformly. `payload` is the
 `InitialExecutionPlan.to_payload` dict — the service layer
 doesn't import `initial_execution_plan` to keep the
 dependency graph thin."""
        import json as _json
        from j1.processing.results import (
            ARTIFACT_KIND_INITIAL_EXECUTION_PLAN,
            ArtifactDraft,
            ArtifactProcessingResult,
            ResultStatus,
        )
        from j1.workspace.layout import WorkspaceArea

        domain_id = payload.get("domain_profile_id") or "none"
        enrichment_policy = str(payload.get("enrichment_policy") or "auto")
        candidate_count = len(
            payload.get("candidate_enrichment_modules") or []
        )
        compile_engine = str(payload.get("compile_engine") or "raganything")
        draft = ArtifactDraft(
            kind=ARTIFACT_KIND_INITIAL_EXECUTION_PLAN,
            content=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            suggested_extension=".json",
            source_document_ids=[document_id] if document_id else [],
            metadata={
                "filename": f"initial_execution_plan_{run_id}.json",
                "domain_profile_id": domain_id,
                "enrichment_policy": enrichment_policy,
                "candidate_module_count": candidate_count,
                "compile_engine": compile_engine,
            },
        )
        result = ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED, drafts=[draft],
        )
        registered = self._handle_artifact_output(
            ctx, result,
            area=WorkspaceArea.COMPILED,
            action=ACTION_REPORT_PERSISTED,
            target_kind=TARGET_DOCUMENT,
            target_id=document_id or run_id,
            actor=actor,
            correlation_id=run_id,
            processor_kind=None,
            source_document_ids=[document_id] if document_id else [],
            legacy_action=ACTION_COMPILE_OK,
        )
        if registered.artifacts:
            return registered.artifacts[0]
        raise RuntimeError(
            "failed to persist initial_execution_plan artifact for "
            f"run {run_id!r}"
        )

    def persist_final_summary(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        document_id: str | None,
        final_status: str,
        executed_steps: list[dict] | None = None,
        artifact_kind_counts: dict[str, int] | None = None,
        warning_count: int = 0,
        failure_code: str | None = None,
        failure_message: str | None = None,
        actor: str = "system",
    ) -> ArtifactRecord:
        """Write a `final_summary.json` artifact at terminal state.

 Carries the at-a-glance outcome (status + planner-derived
 executed-stage tally + artifact-kind counts + warning count
 + failure detail when applicable). Persisted at SUCCESS or
 FAILURE transitions so the FE / operators have a single
 canonical artifact summarising the run."""
        import json as _json
        from j1.processing.results import (
            ARTIFACT_KIND_FINAL_SUMMARY,
            ArtifactDraft,
            ArtifactProcessingResult,
            ResultStatus,
        )
        from j1.workspace.layout import WorkspaceArea
        # action + target_kind constants live at module top of this file

        payload = {
            "schema_version": "1",
            "run_id": run_id,
            "document_id": document_id,
            "final_status": final_status,
            "warning_count": int(warning_count or 0),
            "failure_code": failure_code,
            "failure_message": failure_message,
            "executed_steps": list(executed_steps or []),
            "artifact_kind_counts": dict(artifact_kind_counts or {}),
            "created_at": self._clock().isoformat(),
        }
        draft = ArtifactDraft(
            kind=ARTIFACT_KIND_FINAL_SUMMARY,
            content=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            suggested_extension=".json",
            source_document_ids=[document_id] if document_id else [],
            metadata={
                "filename": f"final_summary_{run_id}.json",
                "final_status": final_status,
                "warning_count": int(warning_count or 0),
            },
        )
        result = ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED, drafts=[draft],
        )
        registered = self._handle_artifact_output(
            ctx, result,
            area=WorkspaceArea.COMPILED,
            action=ACTION_REPORT_PERSISTED,
            target_kind=TARGET_DOCUMENT,
            target_id=document_id or run_id,
            actor=actor,
            correlation_id=run_id,
            processor_kind=None,
            source_document_ids=[document_id] if document_id else [],
            legacy_action=ACTION_COMPILE_OK,
        )
        if registered.artifacts:
            return registered.artifacts[0]
        raise RuntimeError(
            f"failed to persist final_summary artifact for run {run_id!r}"
        )

    def enrich(
        self,
        ctx: ProjectContext,
        processor: EnrichmentProcessor,
        artifact: ArtifactRecord,
        *,
        actor: str = "system",
        correlation_id: str | None = None,
    ) -> ArtifactProcessingResult:
        try:
            output = processor.enrich(ctx, artifact.artifact_id)
        except Exception as exc:
            return self._fail_artifact(
                ctx,
                action=ACTION_ENRICH_FAIL,
                target_kind=TARGET_ARTIFACT,
                target_id=artifact.artifact_id,
                exc=exc,
                actor=actor,
                correlation_id=correlation_id,
                processor_kind=getattr(processor, "kind", None),
            )
        return self._handle_artifact_output(
            ctx,
            output,
            area=WorkspaceArea.ENRICHED,
            action=ACTION_ENRICH_OK,
            target_kind=TARGET_ARTIFACT,
            target_id=artifact.artifact_id,
            actor=actor,
            correlation_id=correlation_id,
            processor_kind=getattr(processor, "kind", None),
            source_artifact_ids=[artifact.artifact_id],
        )

    def build_graph(
        self,
        ctx: ProjectContext,
        builder: GraphBuilder,
        artifact_ids: list[str],
        *,
        actor: str = "system",
        correlation_id: str | None = None,
        document_id: str | None = None,
    ) -> ArtifactProcessingResult:
        # Thread document_id + run_id (= correlation_id) into the
        # builder when the concrete implementation accepts them.
        # Mirrors the inspect-based passthrough used by
        # ``compile``. Concrete adapters (e.g.
        # ``RAGAnythingGraphBuilder``) use these to:
        #   1. Scope LightRAG's working_dir per-run, so a reindex
        #      reads the right scoped graph storage rather than
        #      the global workdir.
        #   2. Stamp ``metadata.run_id`` + ``metadata.document_id``
        #      onto every emitted graph_json draft at the producer
        #      layer, so the registry-level lineage guard at
        #      ``JsonArtifactRegistry.add()`` doesn't reject them.
        # Legacy / mock builders without the kwargs keep working
        # unchanged.
        build_kwargs: dict[str, str | None] = {}
        try:
            import inspect
            sig = inspect.signature(builder.build)
            if "document_id" in sig.parameters and document_id:
                build_kwargs["document_id"] = document_id
            if "run_id" in sig.parameters and correlation_id:
                build_kwargs["run_id"] = correlation_id
        except (TypeError, ValueError):
            pass
        try:
            output = builder.build(ctx, list(artifact_ids), **build_kwargs)
        except Exception as exc:
            return self._fail_artifact(
                ctx,
                action=ACTION_GRAPH_FAIL,
                target_kind=TARGET_ARTIFACT_SET,
                target_id=_set_id(artifact_ids),
                exc=exc,
                actor=actor,
                correlation_id=correlation_id,
                processor_kind=getattr(builder, "kind", None),
            )
        return self._handle_artifact_output(
            ctx,
            output,
            area=WorkspaceArea.GRAPH,
            action=ACTION_GRAPH_OK,
            target_kind=TARGET_ARTIFACT_SET,
            target_id=_set_id(artifact_ids),
            actor=actor,
            correlation_id=correlation_id,
            processor_kind=getattr(builder, "kind", None),
            source_artifact_ids=list(artifact_ids),
        )

    def index(
        self,
        ctx: ProjectContext,
        indexer: SearchIndexer,
        artifact_ids: list[str],
        *,
        actor: str = "system",
        correlation_id: str | None = None,
    ) -> ProcessingResult:
        try:
            output = indexer.index(ctx, list(artifact_ids))
        except Exception as exc:
            return self._fail_processing(
                ctx,
                action=ACTION_INDEX_FAIL,
                target_kind=TARGET_ARTIFACT_SET,
                target_id=_set_id(artifact_ids),
                exc=exc,
                actor=actor,
                correlation_id=correlation_id,
                processor_kind=getattr(indexer, "kind", None),
            )
        self._audit.record(
            ctx,
            actor=actor,
            action=ACTION_INDEX_OK,
            target_kind=TARGET_ARTIFACT_SET,
            target_id=_set_id(artifact_ids),
            correlation_id=correlation_id,
            payload={
                "processor_kind": getattr(indexer, "kind", None),
                "artifact_count": len(artifact_ids),
                "result_status": output.status.value,
            },
        )
        return output

    def query(
        self,
        ctx: ProjectContext,
        provider: QueryProvider,
        question: str,
        *,
        max_results: int | None = None,
        actor: str = "system",
        correlation_id: str | None = None,
    ) -> QueryResult:
        """Run a query through SmartQueryOrchestrator.

        The legacy QueryProvider path was removed — every query now
        flows through the new pipeline (intent → routes → evidence
        → gates → synthesis → citation binder → quality gate).

        ``provider`` is kept in the signature for backward
        compatibility with Temporal callers that supply it; its
        ``kind`` is forwarded to the audit payload as
        ``processor_kind`` so existing audit consumers keep
        working. The orchestrator picks its own routes — the
        provider parameter is informational only.
        """
        if self._smart_query_orchestrator is None:
            raise RuntimeError(
                "ProcessingService.query requires a "
                "SmartQueryOrchestrator. The legacy QueryProvider "
                "path was removed; wire an orchestrator via "
                "``smart_query_orchestrator=`` at construction."
            )
        return self._query_via_orchestrator(
            ctx=ctx,
            question=question,
            provider_kind=getattr(provider, "kind", None),
            max_results=max_results,
            actor=actor,
            correlation_id=correlation_id,
        )

    def _query_via_orchestrator(
        self,
        *,
        ctx: ProjectContext,
        question: str,
        provider_kind: str | None,
        max_results: int | None,
        actor: str,
        correlation_id: str | None,
    ) -> QueryResult:
        """Run a query through SmartQueryOrchestrator and project the
        result into the legacy ``QueryResult`` shape.

        Audit semantics preserved: emit ``processing.query.completed``
        on success (any orchestrator final_status that produced an
        answer), ``processing.query.failed`` on the orchestrator
        raising. ``evidence_insufficient`` /
        ``retrieval_insufficient`` map to ``SUCCEEDED`` with an
        empty answer + a ``message`` carrying the gate reason —
        consumers can distinguish via the ``orchestrator_final_status``
        metadata key.
        """
        from j1.query.orchestrator import OrchestratorRequest
        from j1.query.scope import RunScope, default_scope
        # ``correlation_id`` is the run_id by convention (the
        # workflow stamps it at activity dispatch). RunScope-pin
        # is the strict scoping; legacy callers without a
        # correlation_id fall back to workspace-wide.
        scope = (
            RunScope(run_id=str(correlation_id))
            if correlation_id else default_scope()
        )
        try:
            result = self._smart_query_orchestrator.run(
                OrchestratorRequest(
                    ctx=ctx,
                    question=question,
                    scope=scope,
                    run_id=correlation_id,
                ),
            )
        except Exception as exc:  # noqa: BLE001 — orchestrator failure
            self._audit.record(
                ctx, actor=actor, action=ACTION_QUERY_FAIL,
                target_kind=TARGET_QUERY,
                target_id=_question_id(question),
                correlation_id=correlation_id,
                payload={
                    "processor_kind": (
                        provider_kind or "smart_query_orchestrator"
                    ),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return QueryResult(
                status=ResultStatus.FAILED,
                message=type(exc).__name__,
                error=str(exc),
            )
        # Citations are the orchestrator's cited subset — strictly
        # the blocks the LLM drew from. The QueryResult.citations
        # contract is ``list[str]`` (artifact ids); we project the
        # cited blocks' artifact_ids into that shape.
        citations = [
            c.candidate.artifact_id for c in result.citations
        ]
        is_success = result.final_status == "passed"
        self._audit.record(
            ctx, actor=actor, action=ACTION_QUERY_OK,
            target_kind=TARGET_QUERY,
            target_id=_question_id(question),
            correlation_id=correlation_id,
            payload={
                "processor_kind": (
                    provider_kind or "smart_query_orchestrator"
                ),
                "citation_count": len(citations),
                "result_status": result.final_status,
                "orchestrator": True,
            },
        )
        return QueryResult(
            status=(
                ResultStatus.SUCCEEDED if is_success
                else ResultStatus.SUCCEEDED  # gate failures are
                # still "the query ran" — audit consumers
                # distinguish via metadata.
            ),
            answer=result.answer or None,
            citations=citations,
            message=result.message,
            metadata={
                "orchestrator_final_status": result.final_status,
                "orchestrator_message": result.message or "",
                "intent": result.trace.plan.intent.value,
                "groups_covered": list(result.trace.groups_covered),
                "groups_missing": list(result.trace.groups_missing),
            },
        )

    def _handle_artifact_output(
        self,
        ctx: ProjectContext,
        output: ArtifactProcessingResult,
        *,
        area: WorkspaceArea,
        action: str,
        target_kind: str,
        target_id: str,
        actor: str,
        correlation_id: str | None,
        processor_kind: str | None,
        source_document_ids: list[str] | None = None,
        source_artifact_ids: list[str] | None = None,
        legacy_action: str | None = None,
    ) -> ArtifactProcessingResult:
        registered: list[ArtifactRecord] = []
        for draft in output.drafts:
            record = self._register_draft(
                ctx,
                draft,
                area,
                fallback_source_documents=source_document_ids or [],
                fallback_source_artifacts=source_artifact_ids or [],
                run_id=correlation_id,
            )
            registered.append(record)
        for breakdown in output.cost_events:
            self._cost.record(ctx, breakdown, correlation_id=correlation_id)
        # Issue-7 transparency: each enricher produces ``json`` +
        # ``md`` sibling drafts for the same kind by design, so an
        # ``artifact_ids`` list of length 18 across 9 kinds is the
        # expected shape, not a duplicate-write bug. Surface the
        # breakdown (kind → [json_count, md_count]) so consumers
        # don't have to re-derive it from a registry query.
        kinds_by_format: dict[str, dict[str, int]] = {}
        for r in registered:
            fmt = "unknown"
            if r.metadata:
                fmt = str(r.metadata.get("format") or "unknown")
            bucket = kinds_by_format.setdefault(r.kind, {})
            bucket[fmt] = bucket.get(fmt, 0) + 1
        payload = {
            "processor_kind": processor_kind,
            "artifact_ids": [r.artifact_id for r in registered],
            "result_status": output.status.value,
            "kinds_by_format": kinds_by_format,
        }
        self._audit.record(
            ctx,
            actor=actor,
            action=action,
            target_kind=target_kind,
            target_id=target_id,
            correlation_id=correlation_id,
            payload=payload,
        )
        # Phase E: dual-emit the legacy action when supplied so
        # existing UI consumers (which still match against
        # ``processing.compile.completed`` for non-compile reports)
        # keep functioning for one release. Same payload — only the
        # ``action`` string differs. Drop after consumers migrate.
        if legacy_action and legacy_action != action:
            self._audit.record(
                ctx,
                actor=actor,
                action=legacy_action,
                target_kind=target_kind,
                target_id=target_id,
                correlation_id=correlation_id,
                payload={**payload, "deprecated_alias_of": action},
            )
        return replace(output, artifacts=registered)

    def _register_draft(
        self,
        ctx: ProjectContext,
        draft: ArtifactDraft,
        area: WorkspaceArea,
        *,
        fallback_source_documents: list[str],
        fallback_source_artifacts: list[str],
        run_id: str | None = None,
    ) -> ArtifactRecord:
        artifact_id = self._id_factory()
        ext = draft.suggested_extension
        stored_filename = f"{artifact_id}{ext}"
        area_dir = self._workspace.area(ctx, area)
        area_dir.mkdir(parents=True, exist_ok=True)
        final_path = area_dir / stored_filename
        tmp_path = final_path.with_suffix(final_path.suffix + ".tmp")
        tmp_path.write_bytes(draft.content)
        tmp_path.replace(final_path)

        content_hash = f"{CHECKSUM_PREFIX}{hashlib.sha256(draft.content).hexdigest()}"
        now = self._clock()
        sources_doc = list(draft.source_document_ids or fallback_source_documents)
        sources_art = list(draft.source_artifact_ids or fallback_source_artifacts)

        # Tag the artifact with `run_id` so the review surface can
        # answer "what did this run produce?" by direct lookup
        # (`metadata.run_id == run_id`) rather than falling back to
        # lineage-join on `source_document_ids`. Producer-supplied
        # `draft.metadata` wins on key conflict — explicit producer
        # intent is authoritative.
        merged_metadata = dict(draft.metadata)
        if run_id and "run_id" not in merged_metadata:
            merged_metadata["run_id"] = run_id

        # Lineage guard for legacy callers that bypass orchestration.
        #
        # Two policies, by kind:
        #
        # 1. ``graph_json`` — **fail-fast**. Graph artifacts are the
        #    production failure mode operators hit (the latest
        #    validation report flagged 7 graph_json rows with
        #    run_id=None). The draft-layer stamping in
        #    ``_graph_drafts_from_storage`` now propagates run_id
        #    end-to-end; if it didn't, that's a producer bug we
        #    want surfaced loudly, not silently accumulated. Raises
        #    ``LineageError`` (subclass of ``RuntimeError``) — the
        #    Temporal layer treats this as non-retryable.
        #
        # 2. All other lineage-required kinds — **soft guard**. They
        #    still emit a WARNING and tag
        #    ``lineage_origin=legacy_processing_service`` so the
        #    project-wide ``invalidate_lineage_missing_artifacts``
        #    sweep can clean up retroactively. Test fixtures and
        #    legacy direct callers that legitimately register
        #    without a run scope keep working unchanged.
        #
        # The orchestration path
        # (``KnowledgeProcessingActivities._materialize_draft``)
        # raises ``LineageError`` for EVERY lineage-required kind —
        # that's the strict production write path; this method is
        # the looser legacy alternative.
        if draft.kind in _LINEAGE_REQUIRED_KINDS and not merged_metadata.get("run_id"):
            if draft.kind == "graph_json":
                raise LineageError(
                    f"refusing to register artifact of kind {draft.kind!r}: "
                    "no run_id in metadata. graph_json artifacts MUST "
                    "carry run_id so retrieval and validation can scope "
                    "correctly. The producer "
                    "(_graph_drafts_from_storage) stamps it at the "
                    "draft layer — if you hit this, the producer was "
                    "called without ctx/document_id/run_id. Pass them "
                    "explicitly, or use the orchestration path "
                    "(KnowledgeProcessingActivities) which threads "
                    "correlation_id through."
                )
            _log.warning(
                "legacy registration of lineage-required artifact "
                "kind=%r without run_id; the orphan will be visible "
                "to retrieval until /documents/{id}/repair (or "
                "another sweep) runs. Prefer the orchestration path "
                "(KnowledgeProcessingActivities) which enforces "
                "lineage strictly.",
                draft.kind,
            )
            merged_metadata.setdefault("lineage_origin", "legacy_processing_service")

        record = ArtifactRecord(
            artifact_id=artifact_id,
            project=ctx,
            kind=draft.kind,
            location=f"{area.value}/{stored_filename}",
            content_hash=content_hash,
            byte_size=len(draft.content),
            status=ProcessingStatus.SUCCEEDED,
            review_status=ReviewStatus.PENDING if draft.review_required else ReviewStatus.NOT_REQUIRED,
            version=1,
            created_at=now,
            updated_at=now,
            source_document_ids=sources_doc,
            source_artifact_ids=sources_art,
            metadata=merged_metadata,
        )
        try:
            self._artifacts.add(record)
        except Exception:
            Path(final_path).unlink(missing_ok=True)
            raise
        return record

    def _fail_artifact(
        self,
        ctx: ProjectContext,
        *,
        action: str,
        target_kind: str,
        target_id: str,
        exc: Exception,
        actor: str,
        correlation_id: str | None,
        processor_kind: str | None,
    ) -> ArtifactProcessingResult:
        self._audit.record(
            ctx,
            actor=actor,
            action=action,
            target_kind=target_kind,
            target_id=target_id,
            correlation_id=correlation_id,
            payload={
                "processor_kind": processor_kind,
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )
        return ArtifactProcessingResult(
            status=ResultStatus.FAILED,
            message=type(exc).__name__,
            error=str(exc),
        )

    def _fail_processing(
        self,
        ctx: ProjectContext,
        *,
        action: str,
        target_kind: str,
        target_id: str,
        exc: Exception,
        actor: str,
        correlation_id: str | None,
        processor_kind: str | None,
    ) -> ProcessingResult:
        self._audit.record(
            ctx,
            actor=actor,
            action=action,
            target_kind=target_kind,
            target_id=target_id,
            correlation_id=correlation_id,
            payload={
                "processor_kind": processor_kind,
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )
        return ProcessingResult(
            status=ResultStatus.FAILED,
            message=type(exc).__name__,
            error=str(exc),
        )


def _set_id(ids: list[str]) -> str:
    if not ids:
        return "empty"
    return f"set:{','.join(ids)}"


def _question_id(question: str) -> str:
    digest = hashlib.sha256(question.encode("utf-8")).hexdigest()[:16]
    return f"q:{digest}"
