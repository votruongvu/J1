import hashlib
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from temporalio import activity
from temporalio.exceptions import ApplicationError

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactRegistry
from j1.audit.recorder import AuditRecorder
from j1.cost.breakdown import CostBreakdown
from j1.cost.recorder import CostRecorder
from j1.errors.exceptions import DocumentNotFoundError
from j1.intake.registry import SourceRegistry
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.orchestration.errors import ERROR_TYPE_LOOKUP_FAILED
from j1.orchestration.activities.payloads import (
    ArtifactEnrichmentInput,
    ArtifactEnrichmentResult,
    CostBreakdownPayload,
    DraftPayload,
    GraphBuildInput,
    GraphBuildResult,
    GraphCorpusInput,
    GraphCorpusResult,
    KnowledgeCompilationInput,
    KnowledgeCompilationResult,
    RegisterArtifactsInput,
    RegisterArtifactsResult,
)
from j1.processing.contracts import (
    EnrichmentProcessor,
    GraphBuilder,
    KnowledgeCompiler,
)
from j1.processing.results import ArtifactDraft
from j1.workspace.layout import WorkspaceArea
from j1.workspace.resolver import WorkspaceResolver

ACTIVITY_RUN_COMPILATION = "j1.knowledge.run_compilation"
ACTIVITY_REGISTER_COMPILED = "j1.knowledge.register_compiled_artifacts"
ACTIVITY_RUN_ENRICHMENT = "j1.knowledge.run_enrichment"
ACTIVITY_PREPARE_GRAPH_CORPUS = "j1.knowledge.prepare_graph_corpus"
ACTIVITY_RUN_GRAPH_BUILD = "j1.knowledge.run_graph_build"
ACTIVITY_REGISTER_GRAPH = "j1.knowledge.register_graph_artifacts"

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

ACTION_COMPILATION_COMPLETED = "j1.knowledge.compilation.completed"
ACTION_COMPILATION_FAILED = "j1.knowledge.compilation.failed"
ACTION_ENRICHMENT_COMPLETED = "j1.knowledge.enrichment.completed"
ACTION_ENRICHMENT_FAILED = "j1.knowledge.enrichment.failed"
ACTION_GRAPH_CORPUS_PREPARED = "j1.knowledge.graph_corpus.prepared"
ACTION_GRAPH_BUILD_COMPLETED = "j1.knowledge.graph_build.completed"
ACTION_GRAPH_BUILD_FAILED = "j1.knowledge.graph_build.failed"
ACTION_REGISTER_COMPILED = "j1.knowledge.register_compiled.completed"
ACTION_REGISTER_GRAPH = "j1.knowledge.register_graph.completed"

TARGET_DOCUMENT = "document"
TARGET_ARTIFACT = "artifact"
TARGET_ARTIFACT_SET = "artifact_set"

CHECKSUM_PREFIX = "sha256:"


# Artifact kinds that the validation + retrieval surfaces gate on
# `metadata.run_id`. If any of these is registered without a
# run_id, the orchestration layer raises so the bug surfaces
# loudly at write time rather than silently breaking validation
# lineage checks downstream (the "graph_json with run_id=None"
# class of failures the test reports keep finding).
#
# Generic / blob-style kinds NOT listed here are still permitted
# without a run_id — they're typically operator-uploaded files
# that legitimately have no run context.
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
    """Raised when an artifact write violates the lineage contract.

    Distinct exception class so the Temporal layer can decide to
    treat it as non-retryable (it indicates a programmer error in
    the producer, not a transient failure).
    """


def _enforce_lineage_or_raise(
    kind: str, metadata: dict, artifact_id: str,
) -> None:
    """Fail-fast guard: artifacts of lineage-required kinds MUST
    carry a ``run_id`` in metadata at write time.

    The orchestration registration activity stamps ``run_id`` from
    the workflow's ``correlation_id`` (see ``_register_drafts``);
    this guard catches any future producer that bypasses that path
    — for example a custom adapter that calls the artifact
    registry directly. Without this check, retrieval and validation
    surfaces silently see ``run_id=None`` rows and the failure mode
    is invisible until a tester runs validation hours later.

    The check is intentionally minimal: presence-only. Whether the
    `run_id` value is correct is verified by the existing
    `retrieved_chunks_belong_to_run` validation check.
    """
    if kind not in _LINEAGE_REQUIRED_KINDS:
        return
    run_id = metadata.get("run_id") if isinstance(metadata, dict) else None
    if not run_id:
        raise LineageError(
            f"refusing to register artifact {artifact_id!r} of kind "
            f"{kind!r}: no run_id in metadata. Lineage-required "
            "kinds must be registered with run_id set so retrieval "
            "and validation can scope correctly. This is a "
            "programmer error — the producer must pass run_id (or "
            "correlation_id at the activity boundary) through."
        )


class KnowledgeProcessingActivities:
    def __init__(
        self,
        workspace: WorkspaceResolver,
        sources: SourceRegistry,
        artifacts: ArtifactRegistry,
        audit: AuditRecorder,
        cost: CostRecorder,
        compilers: Mapping[str, KnowledgeCompiler] | None = None,
        enrichers: Mapping[str, EnrichmentProcessor] | None = None,
        graph_builders: Mapping[str, GraphBuilder] | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        snapshot_service=None,
    ) -> None:
        self._workspace = workspace
        self._sources = sources
        self._artifacts = artifacts
        self._audit = audit
        self._cost = cost
        self._compilers = dict(compilers or {})
        self._enrichers = dict(enrichers or {})
        self._graph_builders = dict(graph_builders or {})
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        # Phase 9: when wired, every materialised artifact gets a
        # ``snapshot_id`` stamped on the typed field + metadata
        # mirror. The activity resolves the snapshot via
        # ``require_existing_target_snapshot`` using the
        # ``target_snapshot_id`` threaded by the workflow (REST
        # boundary for single-doc, ``allocate_target_snapshot``
        # activity for bulk-job per-document). ``None`` keeps the
        # pre-snapshot path active for legacy test fixtures.
        self._snapshot_service = snapshot_service

    def all_activities(self) -> list:
        return [
            self.run_knowledge_compilation_activity,
            self.register_compiled_artifacts_activity,
            self.run_artifact_enrichment_activity,
            self.prepare_graph_corpus_activity,
            self.run_graph_build_activity,
            self.register_graph_artifacts_activity,
        ]

    # ---- Compilation ---------------------------------------------------------

    @activity.defn(name=ACTIVITY_RUN_COMPILATION)
    def run_knowledge_compilation_activity(
        self, input: KnowledgeCompilationInput
    ) -> KnowledgeCompilationResult:
        ctx = input.scope.to_context()
        compiler = self._lookup(self._compilers, input.processor_kind, "compiler")
        try:
            self._sources.get(ctx, input.document_id)
        except DocumentNotFoundError as exc:
            raise ApplicationError(
                str(exc),
                type=ERROR_TYPE_LOOKUP_FAILED,
                non_retryable=True,
            ) from exc

        # Thread ``run_id`` (= the workflow's ``correlation_id``)
        # through to the compiler when the concrete implementation
        # accepts it. Mirrors the inspect-based passthrough used for
        # ``assessment_plan`` in the legacy ``ProcessingService`` and
        # for ``document_id`` / ``run_id`` in
        # ``run_graph_build_activity``. Concrete adapters (e.g.
        # ``RAGAnythingCompiler``) use ``run_id`` to namespace
        # LightRAG's ``working_dir`` per-run; without this passthrough
        # the workspace stays shared across runs and a reindex sees
        # LightRAG's stale ``kv_store_doc_status`` from the first run
        # → de-duplicates → returns zero new chunks → "Compile safety
        # retry triggered" → still LOW quality. Legacy / mock
        # compilers without the kwarg keep working unchanged.
        compile_kwargs: dict[str, str | None] = {}
        try:
            import inspect
            sig = inspect.signature(compiler.compile)
            if "run_id" in sig.parameters and input.correlation_id:
                compile_kwargs["run_id"] = input.correlation_id
            # Phase 9: thread the snapshot id through to the
            # compiler so the bridge's snapshot-aware workspace
            # resolver picks up the snapshot-scoped path. The
            # workflow allocates the snapshot up-front (REST
            # boundary for single-doc, ``allocate_target_snapshot``
            # activity for bulk-job per-document) and threads the id
            # through ``input.target_snapshot_id``. Validate via
            # ``require_existing_target_snapshot`` — no lazy create.
            target_snapshot_id = getattr(
                input, "target_snapshot_id", None,
            )
            if (
                "snapshot_id" in sig.parameters
                and self._snapshot_service is not None
                and target_snapshot_id
            ):
                try:
                    snap = self._snapshot_service.require_existing_target_snapshot(
                        ctx,
                        document_id=input.document_id,
                        snapshot_id=target_snapshot_id,
                    )
                    compile_kwargs["snapshot_id"] = snap.snapshot_id
                except Exception:  # noqa: BLE001 — best-effort
                    pass
        except (TypeError, ValueError):
            pass

        try:
            result = compiler.compile(
                ctx, input.document_id, **compile_kwargs,
            )
        except Exception as exc:
            self._audit.record(
                ctx,
                actor=input.actor,
                action=ACTION_COMPILATION_FAILED,
                target_kind=TARGET_DOCUMENT,
                target_id=input.document_id,
                correlation_id=input.correlation_id,
                payload={
                    "processor_kind": input.processor_kind,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return KnowledgeCompilationResult(
                status=STATUS_FAILED,
                error=str(exc),
                message=type(exc).__name__,
            )

        for breakdown in result.cost_events:
            self._cost.record(
                ctx, breakdown, correlation_id=input.correlation_id
            )

        self._audit.record(
            ctx,
            actor=input.actor,
            action=ACTION_COMPILATION_COMPLETED,
            target_kind=TARGET_DOCUMENT,
            target_id=input.document_id,
            correlation_id=input.correlation_id,
            payload={
                "processor_kind": input.processor_kind,
                "draft_count": len(result.drafts),
                "result_status": result.status.value,
            },
        )

        return KnowledgeCompilationResult(
            status=result.status.value,
            drafts=[_draft_to_payload(d) for d in result.drafts],
            cost_events=[_cost_to_payload(c) for c in result.cost_events],
            message=result.message,
            error=result.error,
        )

    @activity.defn(name=ACTIVITY_REGISTER_COMPILED)
    def register_compiled_artifacts_activity(
        self, input: RegisterArtifactsInput
    ) -> RegisterArtifactsResult:
        return self._register_drafts(
            input,
            area=WorkspaceArea.COMPILED,
            audit_action=ACTION_REGISTER_COMPILED,
        )

    # ---- Enrichment ----------------------------------------------------------

    @activity.defn(name=ACTIVITY_RUN_ENRICHMENT)
    def run_artifact_enrichment_activity(
        self, input: ArtifactEnrichmentInput
    ) -> ArtifactEnrichmentResult:
        ctx = input.scope.to_context()
        processor = self._lookup(
            self._enrichers, input.processor_kind, "enricher"
        )
        try:
            self._artifacts.get(ctx, input.artifact_id)
        except Exception as exc:
            raise ApplicationError(
                str(exc),
                type=ERROR_TYPE_LOOKUP_FAILED,
                non_retryable=True,
            ) from exc

        try:
            result = processor.enrich(ctx, input.artifact_id)
        except Exception as exc:
            self._audit.record(
                ctx,
                actor=input.actor,
                action=ACTION_ENRICHMENT_FAILED,
                target_kind=TARGET_ARTIFACT,
                target_id=input.artifact_id,
                correlation_id=input.correlation_id,
                payload={
                    "processor_kind": input.processor_kind,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return ArtifactEnrichmentResult(
                status=STATUS_FAILED,
                error=str(exc),
                message=type(exc).__name__,
            )

        for breakdown in result.cost_events:
            self._cost.record(
                ctx, breakdown, correlation_id=input.correlation_id
            )

        registered_ids: list[str] = []
        for draft in result.drafts:
            record = self._materialize_draft(
                ctx,
                draft,
                area=WorkspaceArea.ENRICHED,
                source_document_ids=[],
                source_artifact_ids=[input.artifact_id],
                target_snapshot_id=getattr(
                    input, "target_snapshot_id", None,
                ),
            )
            registered_ids.append(record.artifact_id)

        self._audit.record(
            ctx,
            actor=input.actor,
            action=ACTION_ENRICHMENT_COMPLETED,
            target_kind=TARGET_ARTIFACT,
            target_id=input.artifact_id,
            correlation_id=input.correlation_id,
            payload={
                "processor_kind": input.processor_kind,
                "artifact_ids": registered_ids,
                "result_status": result.status.value,
            },
        )

        return ArtifactEnrichmentResult(
            status=result.status.value,
            artifact_ids=registered_ids,
            cost_events=[_cost_to_payload(c) for c in result.cost_events],
            error=result.error,
            message=result.message,
        )

    # ---- Graph ---------------------------------------------------------------

    @activity.defn(name=ACTIVITY_PREPARE_GRAPH_CORPUS)
    def prepare_graph_corpus_activity(
        self, input: GraphCorpusInput
    ) -> GraphCorpusResult:
        ctx = input.scope.to_context()
        records = self._artifacts.list_artifacts(ctx)
        include = set(input.include_kinds)
        exclude = set(input.exclude_kinds)
        if include:
            records = [r for r in records if r.kind in include]
        if exclude:
            records = [r for r in records if r.kind not in exclude]
        artifact_ids = [r.artifact_id for r in records]
        self._audit.record(
            ctx,
            actor="system",
            action=ACTION_GRAPH_CORPUS_PREPARED,
            target_kind=TARGET_ARTIFACT_SET,
            target_id=_set_target(artifact_ids),
            payload={
                "include_kinds": list(include),
                "exclude_kinds": list(exclude),
                "artifact_count": len(artifact_ids),
            },
        )
        return GraphCorpusResult(
            status=STATUS_SUCCEEDED, artifact_ids=artifact_ids
        )

    @activity.defn(name=ACTIVITY_RUN_GRAPH_BUILD)
    def run_graph_build_activity(self, input: GraphBuildInput) -> GraphBuildResult:
        ctx = input.scope.to_context()
        builder = self._lookup(
            self._graph_builders, input.processor_kind, "graph_builder"
        )
        target_id = _set_target(input.artifact_ids)
        # Thread document_id + correlation_id (= run_id) through if
        # the concrete builder accepts them. Mirrors the pattern in
        # ``ProcessingService.compile`` for ``assessment_plan`` —
        # legacy / mock builders without these kwargs stay working
        # unchanged. New builders (RAGAnythingGraphBuilder) use
        # them to scope LightRAG's working_dir per-run AND stamp
        # ``metadata.run_id`` on every emitted graph_json draft.
        build_kwargs: dict[str, str | None] = {}
        try:
            import inspect
            sig = inspect.signature(builder.build)
            if "document_id" in sig.parameters and input.document_id:
                build_kwargs["document_id"] = input.document_id
            if "run_id" in sig.parameters and input.correlation_id:
                build_kwargs["run_id"] = input.correlation_id
        except (TypeError, ValueError):
            pass
        try:
            result = builder.build(
                ctx, list(input.artifact_ids), **build_kwargs,
            )
        except Exception as exc:
            self._audit.record(
                ctx,
                actor=input.actor,
                action=ACTION_GRAPH_BUILD_FAILED,
                target_kind=TARGET_ARTIFACT_SET,
                target_id=target_id,
                correlation_id=input.correlation_id,
                payload={
                    "processor_kind": input.processor_kind,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return GraphBuildResult(
                status=STATUS_FAILED,
                error=str(exc),
                message=type(exc).__name__,
            )

        for breakdown in result.cost_events:
            self._cost.record(
                ctx, breakdown, correlation_id=input.correlation_id
            )

        self._audit.record(
            ctx,
            actor=input.actor,
            action=ACTION_GRAPH_BUILD_COMPLETED,
            target_kind=TARGET_ARTIFACT_SET,
            target_id=target_id,
            correlation_id=input.correlation_id,
            payload={
                "processor_kind": input.processor_kind,
                "draft_count": len(result.drafts),
                "result_status": result.status.value,
            },
        )

        return GraphBuildResult(
            status=result.status.value,
            drafts=[_draft_to_payload(d) for d in result.drafts],
            cost_events=[_cost_to_payload(c) for c in result.cost_events],
            error=result.error,
            message=result.message,
        )

    @activity.defn(name=ACTIVITY_REGISTER_GRAPH)
    def register_graph_artifacts_activity(
        self, input: RegisterArtifactsInput
    ) -> RegisterArtifactsResult:
        return self._register_drafts(
            input,
            area=WorkspaceArea.GRAPH,
            audit_action=ACTION_REGISTER_GRAPH,
        )

    # ---- Shared helpers ------------------------------------------------------

    def _register_drafts(
        self,
        input: RegisterArtifactsInput,
        *,
        area: WorkspaceArea,
        audit_action: str,
    ) -> RegisterArtifactsResult:
        ctx = input.scope.to_context()
        new_ids: list[str] = []
        reused_ids: list[str] = []
        for draft_payload in input.drafts:
            content_hash = (
                f"{CHECKSUM_PREFIX}"
                f"{hashlib.sha256(draft_payload.content).hexdigest()}"
            )
            existing = self._artifacts.find_by_content_hash(ctx, content_hash)
            if existing is not None:
                reused_ids.append(existing.artifact_id)
                continue
            record = self._materialize_draft(
                ctx,
                _payload_to_draft(draft_payload),
                area=area,
                source_document_ids=list(input.source_document_ids),
                source_artifact_ids=list(input.source_artifact_ids),
                # Thread the workflow's correlation_id (= the
                # ingestion run id) into the artifact's metadata so
                # downstream consumers (validation, search indexer,
                # graph QA) can answer "what did THIS run produce?"
                # by direct lookup. Mirrors the merge done by
                # `processing/service.py:_register_draft`. Without
                # this the registered artifact carries no run_id,
                # which is the root cause of graph_json artifacts
                # appearing with run_id=None during validation.
                run_id=input.correlation_id,
                target_snapshot_id=getattr(
                    input, "target_snapshot_id", None,
                ),
            )
            new_ids.append(record.artifact_id)

        self._audit.record(
            ctx,
            actor=input.actor,
            action=audit_action,
            target_kind=TARGET_ARTIFACT_SET,
            target_id=_set_target(new_ids + reused_ids),
            correlation_id=input.correlation_id,
            payload={
                "area": area.value,
                "registered_count": len(new_ids),
                "reused_count": len(reused_ids),
            },
        )

        return RegisterArtifactsResult(
            status=STATUS_SUCCEEDED,
            artifact_ids=new_ids,
            reused_artifact_ids=reused_ids,
        )

    def _materialize_draft(
        self,
        ctx,
        draft: ArtifactDraft,
        *,
        area: WorkspaceArea,
        source_document_ids: list[str],
        source_artifact_ids: list[str],
        run_id: str | None = None,
        target_snapshot_id: str | None = None,
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

        content_hash = (
            f"{CHECKSUM_PREFIX}"
            f"{hashlib.sha256(draft.content).hexdigest()}"
        )
        now = self._clock()
        # Producer-supplied `draft.metadata` wins on key conflict —
        # explicit producer intent is authoritative. Otherwise we
        # stamp `run_id` so the validation surface + search indexer
        # can scope artifacts to a specific run.
        merged_metadata = dict(draft.metadata)
        if run_id and "run_id" not in merged_metadata:
            merged_metadata["run_id"] = run_id
        # Phase 9: resolve the snapshot the artifact belongs to.
        # The workflow allocated the candidate up-front (REST
        # boundary for single-doc, ``allocate_target_snapshot``
        # activity for bulk-job per-doc) and threaded the id through
        # ``target_snapshot_id``. We validate via
        # ``require_existing_target_snapshot`` — no lazy create.
        snapshot_id: str | None = None
        primary_doc_id = (
            (draft.source_document_ids or source_document_ids or [None])[0]
        )
        if (
            self._snapshot_service is not None
            and primary_doc_id
            and target_snapshot_id
        ):
            try:
                snap = self._snapshot_service.require_existing_target_snapshot(
                    ctx,
                    document_id=primary_doc_id,
                    snapshot_id=target_snapshot_id,
                )
            except Exception:  # noqa: BLE001 — best-effort
                snap = None
            if snap is not None:
                snapshot_id = snap.snapshot_id
                merged_metadata.setdefault("snapshot_id", snapshot_id)
        # Fail-fast lineage guard. Phase-3 prefers snapshot_id; legacy
        # callers that supplied only run_id still satisfy the guard
        # via the metadata fallback for backward compatibility.
        _enforce_lineage_or_raise(draft.kind, merged_metadata, artifact_id)
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
            source_document_ids=list(draft.source_document_ids or source_document_ids),
            source_artifact_ids=list(draft.source_artifact_ids or source_artifact_ids),
            metadata=merged_metadata,
            snapshot_id=snapshot_id,
            created_by_run_id=run_id,
        )
        try:
            self._artifacts.add(record)
        except Exception:
            final_path.unlink(missing_ok=True)
            raise
        return record

    @staticmethod
    def _lookup(registry: dict, kind: str, role: str):
        try:
            return registry[kind]
        except KeyError as exc:
            raise ApplicationError(
                f"unknown {role} kind: {kind!r}",
                type=ERROR_TYPE_LOOKUP_FAILED,
                non_retryable=True,
            ) from exc


def _draft_to_payload(draft: ArtifactDraft) -> DraftPayload:
    return DraftPayload(
        kind=draft.kind,
        content=draft.content,
        suggested_extension=draft.suggested_extension,
        source_document_ids=list(draft.source_document_ids),
        source_artifact_ids=list(draft.source_artifact_ids),
        metadata={k: str(v) for k, v in draft.metadata.items()},
        review_required=draft.review_required,
    )


def _payload_to_draft(payload: DraftPayload) -> ArtifactDraft:
    return ArtifactDraft(
        kind=payload.kind,
        content=payload.content,
        suggested_extension=payload.suggested_extension,
        source_document_ids=list(payload.source_document_ids),
        source_artifact_ids=list(payload.source_artifact_ids),
        metadata=dict(payload.metadata),
        review_required=payload.review_required,
    )


def _cost_to_payload(breakdown: CostBreakdown) -> CostBreakdownPayload:
    return CostBreakdownPayload(
        vendor=breakdown.vendor,
        model=breakdown.model,
        unit_kind=breakdown.unit_kind,
        units=breakdown.units,
        amount=str(breakdown.amount),
        currency=breakdown.currency,
        metadata={k: str(v) for k, v in breakdown.metadata.items()},
    )


def _set_target(ids: list[str]) -> str:
    if not ids:
        return "empty"
    return f"set:{','.join(ids)}"
