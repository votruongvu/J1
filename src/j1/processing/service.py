import hashlib
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

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

TARGET_DOCUMENT = "document"
TARGET_ARTIFACT = "artifact"
TARGET_ARTIFACT_SET = "artifact_set"
TARGET_QUERY = "query"

CHECKSUM_PREFIX = "sha256:"


class ProcessingService:
    def __init__(
        self,
        workspace: WorkspaceResolver,
        artifact_registry: ArtifactRegistry,
        audit: AuditRecorder,
        cost: CostRecorder,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._workspace = workspace
        self._artifacts = artifact_registry
        self._audit = audit
        self._cost = cost
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)

    def compile(
        self,
        ctx: ProjectContext,
        compiler: KnowledgeCompiler,
        document: DocumentRecord,
        *,
        actor: str = "system",
        correlation_id: str | None = None,
        assessment_plan: object | None = None,
    ) -> ArtifactProcessingResult:
        # Detect whether the compiler accepts `assessment_plan` (the
        # `KnowledgeCompiler` Protocol's `compile` signature only
        # requires `(ctx, document_id)`; concrete adapters opt in
        # via the additive kwarg). Mock compilers + legacy
        # implementations stay working without changes.
        compile_kwargs: dict = {}
        if assessment_plan is not None:
            try:
                import inspect
                sig = inspect.signature(compiler.compile)
                if "assessment_plan" in sig.parameters:
                    compile_kwargs["assessment_plan"] = assessment_plan
            except (TypeError, ValueError):
                # Builtins / C extensions don't expose a signature;
                # fall back to no kwarg (legacy behaviour).
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
        from j1.audit.records import ACTION_COMPILE_OK, TARGET_DOCUMENT
        registered = self._handle_artifact_output(
            ctx, result,
            area=WorkspaceArea.COMPILED,
            action=ACTION_COMPILE_OK,
            target_kind=TARGET_DOCUMENT,
            target_id=document_id or run_id,
            actor=actor,
            correlation_id=run_id,
            processor_kind=None,
            source_document_ids=[document_id] if document_id else [],
        )
        if registered.artifacts:
            return registered.artifacts[0]
        # `_handle_artifact_output` only returns empty when registration
        # itself failed — bubble up to caller.
        raise RuntimeError(
            f"failed to persist error_report artifact for run {run_id!r}"
        )

    def persist_validation_report(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        document_id: str | None,
        passed: bool,
        errors: list[str],
        rules_evaluated: list[str] | None = None,
        actor: str = "system",
    ) -> ArtifactRecord:
        """Write a `validation_report.json` artifact summarising the
        outcome of `_validate_completion`.

        Persisted on every terminal transition (success OR failure),
        so operators can see WHICH rules ran and WHICH ones tripped
        without re-running validation. Backs the `validation_report`
        kind in the artifact contract."""
        import json as _json
        from j1.processing.results import (
            ARTIFACT_KIND_VALIDATION_REPORT,
            ArtifactDraft,
            ArtifactProcessingResult,
            ResultStatus,
        )
        from j1.workspace.layout import WorkspaceArea
        from j1.audit.records import ACTION_COMPILE_OK, TARGET_DOCUMENT

        payload = {
            "schema_version": "1",
            "run_id": run_id,
            "document_id": document_id,
            "passed": passed,
            "errors": list(errors or []),
            "rules_evaluated": list(rules_evaluated or []),
            "created_at": self._clock().isoformat(),
        }
        draft = ArtifactDraft(
            kind=ARTIFACT_KIND_VALIDATION_REPORT,
            content=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            suggested_extension=".json",
            source_document_ids=[document_id] if document_id else [],
            metadata={
                "filename": f"validation_report_{run_id}.json",
                "passed": passed,
                "error_count": len(errors or []),
            },
        )
        result = ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED, drafts=[draft],
        )
        registered = self._handle_artifact_output(
            ctx, result,
            area=WorkspaceArea.COMPILED,
            action=ACTION_COMPILE_OK,
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
            f"failed to persist validation_report artifact for run {run_id!r}"
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
        quality verdict for one document's compile stage. Mirrors
        `persist_validation_report`'s shape so the FE artifact-listing
        surface picks it up uniformly.

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
        from j1.audit.records import ACTION_COMPILE_OK, TARGET_DOCUMENT

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
            action=ACTION_COMPILE_OK,
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
        `PostCompileEnrichPlan.to_payload()` dict — the service layer
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
        from j1.audit.records import ACTION_COMPILE_OK, TARGET_DOCUMENT

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
            action=ACTION_COMPILE_OK,
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
            "failed to persist post_compile_enrich_plan artifact for "
            f"run {run_id!r}"
        )

    def persist_stage_validation_report(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        document_id: str | None,
        stage_name: str,
        attempt: int,
        payload: dict,
        actor: str = "system",
    ) -> ArtifactRecord:
        """Write a `stage_validation_report` artifact for a single
        ingestion stage. Mirrors `persist_validation_report` but
        scoped to one stage (vs. the whole-run summary).

        `payload` is the JSON-serialisable shape returned by
        `StageValidationResult.to_payload()` — the service layer
        doesn't import the dataclass to keep the module dependency
        tree minimal; the activity caller serialises before
        invoking. Filename includes the stage name and attempt so
        re-validation (e.g. on resume) doesn't overwrite the prior
        attempt's report. The artifact is registered under the same
        run_id correlation as everything else the run produces, so
        `_resolve_run_artifacts` finds it without lineage walks."""
        import json as _json
        from j1.processing.results import (
            ARTIFACT_KIND_STAGE_VALIDATION_REPORT,
            ArtifactDraft,
            ArtifactProcessingResult,
            ResultStatus,
        )
        from j1.workspace.layout import WorkspaceArea
        from j1.audit.records import ACTION_COMPILE_OK, TARGET_DOCUMENT

        validation_status = str(payload.get("validation_status") or "unknown")
        check_count = len(payload.get("checks") or [])
        error_count = len(payload.get("errors") or [])
        warning_count = len(payload.get("warnings") or [])
        draft = ArtifactDraft(
            kind=ARTIFACT_KIND_STAGE_VALIDATION_REPORT,
            content=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            suggested_extension=".json",
            source_document_ids=[document_id] if document_id else [],
            metadata={
                "filename":
                    f"stage_validation_{stage_name}_{run_id}_{attempt}.json",
                "stage_name": stage_name,
                "attempt": attempt,
                "validation_status": validation_status,
                "check_count": check_count,
                "error_count": error_count,
                "warning_count": warning_count,
            },
        )
        result = ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED, drafts=[draft],
        )
        registered = self._handle_artifact_output(
            ctx, result,
            area=WorkspaceArea.COMPILED,
            action=ACTION_COMPILE_OK,
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
            "failed to persist stage_validation_report artifact for "
            f"run {run_id!r} stage {stage_name!r}"
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
        from j1.audit.records import ACTION_COMPILE_OK, TARGET_DOCUMENT

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
            action=ACTION_COMPILE_OK,
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
    ) -> ArtifactProcessingResult:
        try:
            output = builder.build(ctx, list(artifact_ids))
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
        try:
            output = provider.query(ctx, question, max_results=max_results)
        except Exception as exc:
            self._audit.record(
                ctx,
                actor=actor,
                action=ACTION_QUERY_FAIL,
                target_kind=TARGET_QUERY,
                target_id=_question_id(question),
                correlation_id=correlation_id,
                payload={
                    "processor_kind": getattr(provider, "kind", None),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return QueryResult(
                status=ResultStatus.FAILED,
                message=type(exc).__name__,
                error=str(exc),
            )
        for breakdown in output.cost_events:
            self._cost.record(ctx, breakdown, correlation_id=correlation_id)
        self._audit.record(
            ctx,
            actor=actor,
            action=ACTION_QUERY_OK,
            target_kind=TARGET_QUERY,
            target_id=_question_id(question),
            correlation_id=correlation_id,
            payload={
                "processor_kind": getattr(provider, "kind", None),
                "citation_count": len(output.citations),
                "result_status": output.status.value,
            },
        )
        return output

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
        self._audit.record(
            ctx,
            actor=actor,
            action=action,
            target_kind=target_kind,
            target_id=target_id,
            correlation_id=correlation_id,
            payload={
                "processor_kind": processor_kind,
                "artifact_ids": [r.artifact_id for r in registered],
                "result_status": output.status.value,
            },
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
