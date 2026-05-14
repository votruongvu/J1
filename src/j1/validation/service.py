"""IngestionValidationService — the validation surface.

After the 2026-05-14 product decision, this service is intentionally
small. It owns two flows:

* **Manual Test Query** — synchronous, one-off questions against a
  specific run. Delegates to ``SmartQueryOrchestrator`` for the heavy
  lifting (intent classification, retrieval, sufficiency gate,
  synthesis, citation binding, answer-quality gate). The detailed
  inspection tool inside the Validation Tab.

* **Imported Test Cases** — auxiliary helper: a user uploads a CSV
  per document and runs the imported questions against the
  document's latest succeeded run. The Validation Tab shows only
  compact status badges and aggregate summary; per-question detail
  routes back through Manual Test Query.

Generated test cases, LLM-question generation, draft/approve/reject
lifecycles, judge calls, and validation-set storage were deleted in
the same product change. There is no compatibility shim.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from j1.documents.snapshot_store import DocumentSnapshotStore
    from j1.intake.registry import SourceRegistry

from j1.artifacts.registry import ArtifactRegistry
from j1.audit.recorder import AuditRecorder
from j1.ingestion_review.exceptions import ReviewNotFound
from j1.projects.context import ProjectContext
from j1.query.scope import ActiveScope, QueryScope, RunScope, WorkspaceScope
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import IngestionRunStore
from j1.validation.dtos import (
    EvidenceBlockDTO,
    LLMTraceDTO,
    ManualTestQueryRequest,
    ManualTestQueryResponseDTO,
    NativeDebugQueryResponseDTO,
    RetrievedChunkRefDTO,
    ValidationCheckDTO,
)
from j1.validation.imported_test_cases import (
    CSVImportError,
    ImportedTestCaseExecution,
    ImportedTestCaseExecutor,
    ImportedTestCaseSet,
    ImportedTestCaseStore,
    parse_csv_bytes,
)
from j1.workspace.resolver import WorkspaceResolver

_log = logging.getLogger("j1.validation")

_ACTION_MANUAL_QUERY = "j1.validation.manual_query.completed"
_ACTION_NATIVE_DEBUG = "j1.validation.native_debug_query.completed"
_ACTION_IMPORTED_IMPORT = "j1.validation.imported_test_cases.imported"
_ACTION_IMPORTED_EXECUTE = "j1.validation.imported_test_cases.executed"
_TARGET_KIND_RUN = "ingestion_run"
_TARGET_KIND_DOCUMENT = "document"

# Hard cap on ``top_k`` for manual queries. Manual query is synchronous
# and we don't want a tester accidentally requesting 10k results and
# blocking the worker. The REST layer also clamps via Pydantic but the
# service enforces too so stand-alone callers (tests, future async
# paths) get the same guarantee.
_TOP_K_HARD_CAP = 50

_DEFAULT_VALIDATION_CANDIDATE_TOP_K = 20
_DEFAULT_NATIVE_QUERY_TIMEOUT_SECONDS = 30.0

# Mirror of the runner's preview cap. Used by the orchestrator → DTO
# projection helpers below to keep retrieved-chunk preview widths
# visually consistent.
_PREVIEW_MAX_CHARS = 240


class IngestionValidationService:
    """Validation surface — manual test queries + imported test cases.

    Constructed with explicit dependencies (no facade, no container)
    so it's trivially constructable in tests. Every dependency that
    isn't strictly required for the manual-query happy path is
    Optional — partial deployments degrade gracefully (the REST layer
    surfaces 503 when an endpoint's required collaborators aren't
    wired).
    """

    def __init__(
        self,
        *,
        run_store: IngestionRunStore,
        artifact_registry: ArtifactRegistry,
        audit: AuditRecorder | None = None,
        workspace: WorkspaceResolver | None = None,
        source_registry: "SourceRegistry | None" = None,
        # Snapshot store — resolves ``ActiveScope`` to a concrete run_id
        # via ``Document.active_snapshot_id`` →
        # ``DocumentSnapshot.created_by_run_id``. Optional so
        # legacy/test wirings stay constructible; when absent the
        # ``validation_scope="active"`` path falls back to the
        # caller's run_id.
        snapshot_store: "DocumentSnapshotStore | None" = None,
        # Manual-query path (SmartQueryOrchestrator).
        smart_query_orchestrator: object | None = None,
        # Native-debug path (LightRAG aquery, no BM25). Optional.
        native_query_provider: Any | None = None,
        native_query_timeout_seconds: float = (
            _DEFAULT_NATIVE_QUERY_TIMEOUT_SECONDS
        ),
        # Retrieval breadth knob (manual-query candidate pool).
        validation_candidate_top_k: int = _DEFAULT_VALIDATION_CANDIDATE_TOP_K,
        # Imported-test-cases storage + executor. Optional so a
        # deployment without the workspace wired still gets the
        # manual-query endpoint.
        imported_test_case_store: ImportedTestCaseStore | None = None,
        imported_test_case_executor: ImportedTestCaseExecutor | None = None,
    ) -> None:
        self._run_store = run_store
        self._artifacts = artifact_registry
        self._audit = audit
        self._workspace = workspace
        self._source_registry = source_registry
        self._snapshot_store = snapshot_store
        self._smart_query_orchestrator = smart_query_orchestrator
        self._native_query_provider = native_query_provider
        self._native_query_timeout_seconds = native_query_timeout_seconds
        self._validation_candidate_top_k = validation_candidate_top_k
        self._imported_store = imported_test_case_store
        self._imported_executor = imported_test_case_executor

    # ---- Manual Test Query -----------------------------------------

    def run_manual_test_query(
        self,
        ctx: ProjectContext,
        run_id: str,
        request: ManualTestQueryRequest,
        *,
        actor: str = "system",
    ) -> ManualTestQueryResponseDTO:
        """Execute one tester question against this run.

        Synchronous. Drives the ``SmartQueryOrchestrator`` with
        ``RunScope(run_id)`` so retrieval is restricted to artifacts
        produced by this run (or, when ``validation_scope="active"``,
        the document's currently promoted run).

        Raises ``ReviewNotFound`` (→ 404 at REST) when the run doesn't
        exist in ``(ctx.tenant_id, ctx.project_id)``. Cross-tenant /
        cross-project access produces an identical 404 — existence is
        never leakable.
        """
        run = self._load_run(ctx, run_id)
        if self._smart_query_orchestrator is None:
            raise RuntimeError(
                "IngestionValidationService.run_manual_test_query "
                "requires a SmartQueryOrchestrator."
            )
        request_id = f"tq-{uuid.uuid4().hex[:12]}"
        return self._run_manual_query_via_orchestrator(
            ctx=ctx, run=run, request=request,
            request_id=request_id, actor=actor,
        )

    def run_document_test_query(
        self,
        ctx: ProjectContext,
        document_id: str,
        request: ManualTestQueryRequest,
        *,
        actor: str = "system",
    ) -> ManualTestQueryResponseDTO:
        """Execute one tester question against a document's active
        snapshot — no run id required.

        The request's typed ``scope`` field decides what's queried:

          * ``document_active``  → the document's ``active_snapshot_id``
            (the visibility key the eligibility resolver narrows to).
          * ``snapshot_explicit`` → a fixed allowlist (e.g. validate a
            specific candidate snapshot for this document).

        Both leave run lineage out of the routing path. The
        underlying ``SmartQueryOrchestrator`` is already
        snapshot-aware; this method is just the no-run-id surface.

        Defaults: when ``request.scope`` is ``None`` (legacy callers),
        treat it as ``document_active`` using the URL's ``document_id``
        — this is the explicit contract of the endpoint, not a fall
        through to ``RunScope``.
        """
        if self._smart_query_orchestrator is None:
            raise RuntimeError(
                "IngestionValidationService.run_document_test_query "
                "requires a SmartQueryOrchestrator."
            )
        from j1.validation.dtos import (
            ManualTestQueryRequest as _MTQR,
            QueryScopeDTO as _ScopeDTO,
        )
        # Default to document_active when no typed scope was supplied.
        # Refuses legacy ``validation_scope="run"`` for this endpoint
        # — Run is not a primary routing key here.
        scope_dto = request.scope or _ScopeDTO(
            type="document_active", document_id=document_id,
        )
        # Normalise: a caller that sent type=document_active without
        # a documentId still resolves via the URL.
        if (
            scope_dto.type == "document_active"
            and not scope_dto.document_id
        ):
            scope_dto = _ScopeDTO(
                type="document_active", document_id=document_id,
            )
        normalised = _MTQR(
            question=request.question,
            top_k=request.top_k,
            mode=request.mode,
            citation_required=request.citation_required,
            include_raw=request.include_raw,
            synthesize=request.synthesize,
            scope=scope_dto,
            # Legacy fields ignored when ``scope`` is set.
            validation_scope=request.validation_scope,
            allow_run_scope=False,
        )
        request_id = f"tq-{uuid.uuid4().hex[:12]}"
        return self._run_manual_query_via_orchestrator(
            ctx=ctx, run=None, request=normalised,
            request_id=request_id, actor=actor,
            document_id_override=document_id,
        )

    def run_project_query(
        self,
        ctx: ProjectContext,
        request: ManualTestQueryRequest,
        *,
        actor: str = "system",
    ) -> ManualTestQueryResponseDTO:
        """Execute one query against the project's active knowledge
        scope — no document / run id required.

        Defaults the scope to ``project_active`` (every attached
        document's active snapshot). Operators with a specific
        ``snapshot_explicit`` allowlist can override via the typed
        ``scope`` field; ``document_active`` is also accepted when
        the caller wants to pin a single document.
        """
        if self._smart_query_orchestrator is None:
            raise RuntimeError(
                "IngestionValidationService.run_project_query "
                "requires a SmartQueryOrchestrator."
            )
        from j1.validation.dtos import (
            ManualTestQueryRequest as _MTQR,
            QueryScopeDTO as _ScopeDTO,
        )
        scope_dto = request.scope or _ScopeDTO(type="project_active")
        normalised = _MTQR(
            question=request.question,
            top_k=request.top_k,
            mode=request.mode,
            citation_required=request.citation_required,
            include_raw=request.include_raw,
            synthesize=request.synthesize,
            scope=scope_dto,
            validation_scope=request.validation_scope,
            allow_run_scope=False,
        )
        request_id = f"pq-{uuid.uuid4().hex[:12]}"
        return self._run_manual_query_via_orchestrator(
            ctx=ctx, run=None, request=normalised,
            request_id=request_id, actor=actor,
        )

    def run_native_debug_query(
        self,
        ctx: ProjectContext,
        run_id: str,
        question: str,
        *,
        actor: str = "system",
    ) -> NativeDebugQueryResponseDTO:
        """Direct LightRAG-native diagnostic. No BM25, no reranking,
        no coverage selection — pure ``rag.aquery`` against this run's
        workspace.

        Used by operators to isolate whether retrieval problems
        originate in native indexing or elsewhere. The response
        surfaces the resolved workspace path so the operator can
        visually confirm "yes, the call hit the per-run directory I
        expected" without inferring it from debug logs.
        """
        run = self._load_run(ctx, run_id)
        request_id = f"nd-{uuid.uuid4().hex[:12]}"

        tenant = getattr(ctx, "tenant_id", None) or ""
        project = getattr(ctx, "project_id", None) or ""
        snapshot_id = getattr(run, "target_snapshot_id", None) or ""
        workspace_id = (
            f"{tenant}/{project}/{run.document_id}/{snapshot_id}"
            if tenant and project and run.document_id and snapshot_id
            else ""
        )
        workspace_path: str | None = None
        if self._native_query_provider is not None:
            try:
                workspace_path = (
                    self._native_query_provider.workspace_path_for(
                        ctx, run.document_id, snapshot_id or None,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                _log.debug(
                    "workspace_path_for failed for run=%s: %s",
                    run.run_id, exc,
                )

        provider_wired = self._native_query_provider is not None
        if not provider_wired:
            return NativeDebugQueryResponseDTO(
                request_id=request_id,
                run_id=run.run_id,
                document_id=run.document_id,
                question=question,
                answer="",
                workspace_path=workspace_path,
                workspace_id=workspace_id,
                native_query_used=False,
                native_query_failed_reason="native_provider_not_wired",
                native_latency_ms=0,
                provider_wired=False,
            )

        native_answer, native_latency, native_error = self._run_native_query(
            ctx=ctx, run=run, question=question,
        )

        if self._audit is not None:
            try:
                self._audit.record(
                    ctx,
                    actor=actor,
                    action=_ACTION_NATIVE_DEBUG,
                    target_kind=_TARGET_KIND_RUN,
                    target_id=run.run_id,
                    correlation_id=run.run_id,
                    payload={
                        "request_id": request_id,
                        "document_id": run.document_id,
                        "workspace_id": workspace_id,
                        "native_query_used": native_answer is not None,
                        "native_query_failed_reason": native_error,
                        "native_latency_ms": native_latency,
                    },
                )
            except Exception:  # noqa: BLE001 — audit never fails the call
                _log.warning(
                    "native-debug audit record failed run=%s",
                    run.run_id, exc_info=True,
                )

        return NativeDebugQueryResponseDTO(
            request_id=request_id,
            run_id=run.run_id,
            document_id=run.document_id,
            question=question,
            answer=native_answer or "",
            workspace_path=workspace_path,
            workspace_id=workspace_id,
            native_query_used=native_answer is not None,
            native_query_failed_reason=native_error,
            native_latency_ms=native_latency,
            provider_wired=True,
        )

    # ---- Imported Test Cases ---------------------------------------

    def import_test_cases(
        self,
        ctx: ProjectContext,
        document_id: str,
        csv_bytes: bytes,
        *,
        source_filename: str | None = None,
        actor: str = "system",
    ) -> ImportedTestCaseSet:
        """Parse a CSV blob and REPLACE the document's imported set.

        Every import wipes the prior set (and the prior execution
        snapshot) — matches the product spec exactly. Raises
        ``CSVImportError`` for unrecoverable parse failures; the REST
        layer translates to HTTP 400.
        """
        if self._imported_store is None:
            raise RuntimeError(
                "IngestionValidationService.import_test_cases "
                "requires an ImportedTestCaseStore."
            )
        from datetime import datetime, timezone
        cases = parse_csv_bytes(csv_bytes, source_filename=source_filename)
        imported_set = ImportedTestCaseSet(
            document_id=document_id,
            cases=cases,
            imported_at=datetime.now(timezone.utc),
            source_filename=source_filename,
        )
        self._imported_store.save_set(ctx, imported_set)
        if self._audit is not None:
            try:
                self._audit.record(
                    ctx,
                    actor=actor,
                    action=_ACTION_IMPORTED_IMPORT,
                    target_kind=_TARGET_KIND_DOCUMENT,
                    target_id=document_id,
                    payload={
                        "case_count": len(cases),
                        "source_filename": source_filename,
                    },
                )
            except Exception:  # noqa: BLE001 — telemetry never blocks
                _log.warning(
                    "imported-test-cases audit (import) failed doc=%s",
                    document_id, exc_info=True,
                )
        return imported_set

    def get_imported_test_cases(
        self, ctx: ProjectContext, document_id: str,
    ) -> ImportedTestCaseSet | None:
        if self._imported_store is None:
            return None
        return self._imported_store.get_set(ctx, document_id)

    def delete_imported_test_cases(
        self, ctx: ProjectContext, document_id: str,
    ) -> bool:
        if self._imported_store is None:
            return False
        return self._imported_store.delete_set(ctx, document_id)

    def get_latest_imported_execution(
        self, ctx: ProjectContext, document_id: str,
    ) -> ImportedTestCaseExecution | None:
        if self._imported_store is None:
            return None
        return self._imported_store.get_latest_execution(ctx, document_id)

    def execute_imported_test_cases(
        self,
        ctx: ProjectContext,
        document_id: str,
        *,
        actor: str = "system",
    ) -> ImportedTestCaseExecution:
        """Run every imported question against the document's latest
        succeeded run.

        Raises ``ReviewNotFound`` when no imported set exists or no
        succeeded run is available — both are user-visible errors the
        FE renders as actionable hints.
        """
        if self._imported_store is None or self._imported_executor is None:
            raise RuntimeError(
                "IngestionValidationService.execute_imported_test_cases "
                "requires both an ImportedTestCaseStore and an "
                "ImportedTestCaseExecutor."
            )
        imported_set = self._imported_store.get_set(ctx, document_id)
        if imported_set is None or not imported_set.cases:
            raise ReviewNotFound(
                f"no imported test cases for document {document_id!r}"
            )
        run_id = self._latest_succeeded_run_id(ctx, document_id)
        if run_id is None:
            raise ReviewNotFound(
                f"document {document_id!r} has no succeeded run to "
                "execute imported test cases against"
            )
        execution = self._imported_executor.execute(
            ctx, imported_set, run_id=run_id,
        )
        self._imported_store.save_execution(ctx, execution)
        if self._audit is not None:
            try:
                self._audit.record(
                    ctx,
                    actor=actor,
                    action=_ACTION_IMPORTED_EXECUTE,
                    target_kind=_TARGET_KIND_DOCUMENT,
                    target_id=document_id,
                    payload={
                        "run_id": run_id,
                        "case_count": len(imported_set.cases),
                        "overall": execution.summary.overall,
                        "answered": execution.summary.answered,
                        "with_sources": execution.summary.with_sources,
                        "scope_issues": execution.summary.scope_issues,
                        "errors": execution.summary.errors,
                    },
                )
            except Exception:  # noqa: BLE001
                _log.warning(
                    "imported-test-cases audit (execute) failed doc=%s",
                    document_id, exc_info=True,
                )
        return execution

    # ---- Internals -------------------------------------------------

    def _load_run(self, ctx: ProjectContext, run_id: str) -> IngestionRun:
        """Run-ownership gate. Cross-tenant / cross-project access
        produces an identical 404 to missing-run so existence is not
        probeable."""
        run = self._run_store.get(ctx, run_id)
        if run is None:
            raise ReviewNotFound(f"ingestion run {run_id!r} not found")
        return run

    def _latest_succeeded_run_id(
        self, ctx: ProjectContext, document_id: str,
    ) -> str | None:
        """Pick the most recent succeeded run for the document.

        Same heuristic the REST reindex / refresh-enrich paths use
        — keeps "active run" consistent across surfaces. Returns
        ``None`` when no usable run exists."""
        runs = self._run_store.list_runs(ctx, document_id=document_id)
        sorted_runs = sorted(
            runs,
            key=lambda r: (r.started_at, r.updated_at),
            reverse=True,
        )
        for r in sorted_runs:
            if r.status in (
                RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS,
            ):
                return r.run_id
        return None

    def _run_manual_query_via_orchestrator(
        self,
        *,
        ctx: ProjectContext,
        run: IngestionRun | None,
        request: ManualTestQueryRequest,
        request_id: str,
        actor: str,
        document_id_override: str | None = None,
    ) -> ManualTestQueryResponseDTO:
        """Drive one manual test query through the SmartQueryOrchestrator
        and project the result into ``ManualTestQueryResponseDTO``.

        The pipeline owns intent classification, multi-route retrieval,
        sufficiency gate, synthesis, citation binding, and the
        answer-quality gate. The FE renders the same DTO fields it
        always has — ``validation_status`` comes from the
        orchestrator's ``final_status``, ``checks[]`` is a flattened
        view of the gate results.

        ``run`` is optional: the run-keyed endpoint passes a real run;
        the snapshot-centric doc/project endpoints pass ``None`` and
        rely on the typed ``request.scope`` field. ``document_id_override``
        lets the doc-level endpoint thread the URL's document id into
        the route context (the orchestrator uses it to narrow the
        RAGAnything fan-out when scope is ``document_active``).
        """
        from j1.query.orchestrator import OrchestratorRequest

        engine_scope, eligible_snapshot_ids = self._resolve_query_scope(
            ctx=ctx, run=run, request=request,
        )
        result = self._smart_query_orchestrator.run(OrchestratorRequest(
            ctx=ctx,
            question=request.question,
            scope=engine_scope,
            run_id=run.run_id if run is not None else None,
            document_id=(
                document_id_override
                if document_id_override is not None
                else (run.document_id if run is not None else None)
            ),
            eligible_snapshot_ids=eligible_snapshot_ids,
        ))

        retrieved_chunks = _retrieved_chunks_from_trace(result.trace)
        citations_list = _citations_from_orchestrator(result)
        checks = _checks_from_gate_results(result.gate_results)
        validation_status = _validation_status_from_final(
            result.final_status,
        )
        evidence_sent_to_llm = _evidence_blocks_from_trace(result.trace)
        evidence_flags = _evidence_flags_from_trace(result.trace)
        llm_trace = LLMTraceDTO(
            called=bool(result.trace.llm_evidence),
            provider="smart_query_orchestrator",
            model="composite",
            error=(
                None if result.final_status == "passed"
                else result.message
            ),
        )
        debug: dict[str, Any] = {
            "query_engine": "smart_query_orchestrator",
            "orchestrator_final_status": result.final_status,
            "orchestrator_message": result.message,
            "orchestrator_trace": result.trace.to_dict(),
        }
        if run is not None:
            # Audit trail is per-run today. Project / document
            # endpoints skip the per-run audit and rely on the
            # orchestrator's internal trace for diagnostics.
            self._audit_manual_query(
                ctx=ctx, run=run, request_id=request_id, request=request,
                validation_status=validation_status,
                retrieved_count=len(retrieved_chunks),
                citation_count=len(citations_list),
                actor=actor,
            )
        return ManualTestQueryResponseDTO(
            request_id=request_id,
            run_id=run.run_id if run is not None else "",
            question=request.question,
            answer=result.answer or "",
            mode_used="smart_query_orchestrator",
            retrieved_chunks=retrieved_chunks,
            citations=citations_list,
            checks=checks,
            validation_status=validation_status,
            evidence_flags=evidence_flags,
            raw_response=None,
            synthesized_answer=result.answer or None,
            llm=llm_trace,
            evidence_sent_to_llm=evidence_sent_to_llm,
            debug=debug,
        )

    def _resolve_query_scope(
        self,
        *,
        ctx: ProjectContext,
        run: IngestionRun | None,
        request: "ManualTestQueryRequest",
    ) -> tuple[QueryScope, "frozenset[str] | None"]:
        """Map the request's scope to a concrete ``(QueryScope, eligible_snapshot_ids)``.

        Preference order:

          1. ``request.scope`` (the typed snapshot-centric contract).
             * ``project_active``     → ``WorkspaceScope`` (eligibility
               resolver narrows to attached docs).
             * ``document_active``    → ``ActiveScope(document_id)``.
             * ``snapshot_explicit``  → ``WorkspaceScope`` +
               pre-resolved ``eligible_snapshot_ids`` allowlist. The
               orchestrator + adapters honour the allowlist over any
               eligibility resolution.

          2. Legacy ``request.validation_scope`` token. ``"active"``
             still maps to ``ActiveScope``. ``"run"`` is REJECTED for
             UI paths — the handler raises before we get here unless
             ``request.allow_run_scope`` is explicitly true (the
             diagnostic escape hatch).

        Run is never a primary query scope. Even with
        ``allow_run_scope=True``, the diagnostic ``RunScope`` is only
        for operators who want raw run-keyed artifact inspection.
        """
        from j1.validation.dtos import (
            ManualTestQueryRequest as _MTQR,  # noqa: F401 — for narrow imports
        )

        scope_dto = getattr(request, "scope", None)
        if scope_dto is not None:
            if scope_dto.type == "project_active":
                return WorkspaceScope(), None
            if scope_dto.type == "document_active":
                doc_id = scope_dto.document_id
                if not doc_id and run is not None:
                    doc_id = run.document_id
                if not doc_id:
                    raise ValueError(
                        "scope.type='document_active' requires a "
                        "documentId (either in the scope payload or "
                        "as the URL routing key)"
                    )
                return ActiveScope(document_id=doc_id), None
            if scope_dto.type == "snapshot_explicit":
                ids = tuple(scope_dto.snapshot_ids or ())
                if not ids:
                    raise ValueError(
                        "scope.type='snapshot_explicit' requires at "
                        "least one snapshotId"
                    )
                return WorkspaceScope(), frozenset(ids)
            raise ValueError(f"unknown scope type {scope_dto.type!r}")

        # Legacy path: ``validation_scope`` string token. Only the
        # run-keyed endpoint reaches this branch — the doc/project
        # endpoints always send a typed ``scope``.
        if run is None:
            raise ValueError(
                "legacy validation_scope token requires a run; "
                "doc/project endpoints must send the typed `scope`"
            )
        if request.validation_scope == "active":
            if self._source_registry is not None:
                from j1.query.active_scope import resolve_to_concrete_scope
                active = ActiveScope(document_id=run.document_id)
                return (
                    resolve_to_concrete_scope(
                        active,
                        registry=self._source_registry,
                        ctx=ctx,
                        snapshot_store=self._snapshot_store,
                    ),
                    None,
                )
            return ActiveScope(document_id=run.document_id), None

        # ``validation_scope="run"`` — only the diagnostic escape
        # hatch reaches this branch (handler refuses UI traffic).
        return RunScope(run_id=run.run_id), None

    def _run_native_query(
        self,
        *,
        ctx: ProjectContext,
        run: IngestionRun,
        question: str,
    ) -> tuple[str | None, int, str | None]:
        """Best-effort native ``aquery`` call.

        Returns ``(native_answer, latency_ms, error)``:
          * ``native_answer`` is the raw prose LightRAG produced, or
            ``None`` when the call failed / timed out.
          * ``latency_ms`` is the wall-clock duration even on failure.
          * ``error`` is a short reason string on failure; ``None``
            on success.
        """
        if self._native_query_provider is None:
            return (None, 0, "native_provider_not_wired")

        from j1.processing.results import ResultStatus

        started = time.monotonic()
        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
            ) as pool:
                future = pool.submit(
                    self._native_query_provider.query,
                    ctx,
                    question,
                    max_results=self._validation_candidate_top_k,
                    document_id=run.document_id,
                    run_id=run.run_id,
                )
                try:
                    result = future.result(
                        timeout=self._native_query_timeout_seconds,
                    )
                except concurrent.futures.TimeoutError:
                    future.cancel()
                    latency_ms = int(
                        (time.monotonic() - started) * 1000,
                    )
                    reason = (
                        f"native_query_timeout_after_"
                        f"{self._native_query_timeout_seconds}s"
                    )
                    _log.warning(
                        "native_query failed run_id=%s document_id=%s "
                        "latency_ms=%d reason=%s",
                        run.run_id, run.document_id, latency_ms, reason,
                    )
                    return (None, latency_ms, reason)
        except Exception as exc:  # noqa: BLE001
            latency_ms = int((time.monotonic() - started) * 1000)
            reason = f"{type(exc).__name__}: {exc}"
            _log.warning(
                "native_query failed run_id=%s document_id=%s "
                "latency_ms=%d reason=%s",
                run.run_id, run.document_id, latency_ms, reason,
                exc_info=True,
            )
            return (None, latency_ms, reason)

        latency_ms = int((time.monotonic() - started) * 1000)
        if getattr(result, "status", None) != ResultStatus.SUCCEEDED:
            reason = str(
                getattr(result, "error", None) or "native_query_failed",
            )
            _log.warning(
                "native_query failed run_id=%s document_id=%s "
                "latency_ms=%d reason=%s",
                run.run_id, run.document_id, latency_ms, reason,
            )
            return (None, latency_ms, reason)
        answer = (getattr(result, "answer", "") or "").strip()
        return (answer, latency_ms, None)

    def _audit_manual_query(
        self,
        *,
        ctx: ProjectContext,
        run: IngestionRun,
        request_id: str,
        request: ManualTestQueryRequest,
        validation_status: str,
        retrieved_count: int,
        citation_count: int,
        actor: str,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(
                ctx,
                actor=actor,
                action=_ACTION_MANUAL_QUERY,
                target_kind=_TARGET_KIND_RUN,
                target_id=run.run_id,
                correlation_id=run.run_id,
                payload={
                    "requestId": request_id,
                    "question": request.question,
                    "mode": request.mode,
                    "topK": request.top_k,
                    "citationRequired": request.citation_required,
                    "validationStatus": validation_status,
                    "retrievedCount": retrieved_count,
                    "citationCount": citation_count,
                },
            )
        except Exception:  # noqa: BLE001 — telemetry never fails the call
            _log.warning(
                "audit write failed for manual test query",
                exc_info=True,
            )


# ---- Module-level projection helpers (easy to unit-test) -----------


def _retrieved_chunks_from_trace(trace: Any) -> list[RetrievedChunkRefDTO]:
    """Project ``QueryTrace.all_candidates`` into the public chunk-ref
    DTO. The FE's "retrieved" list shows what retrieval surfaced,
    independent of what synthesis used."""
    out: list[RetrievedChunkRefDTO] = []
    seen: set[tuple[str, str | None]] = set()
    for cand in getattr(trace, "all_candidates", ()):
        key = (cand.artifact_id, cand.chunk_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(RetrievedChunkRefDTO(
            artifact_id=cand.artifact_id,
            chunk_id=cand.chunk_id,
            run_id=cand.run_id,
            document_id=cand.document_id,
            source_location=(
                (cand.extra or {}).get("section_path")
            ),
            score=float(cand.score or 0.0),
            preview=(cand.text_preview or "")[:_PREVIEW_MAX_CHARS],
            artifact_kind=cand.artifact_kind,
        ))
    return out


def _citations_from_orchestrator(result: Any) -> list[dict[str, Any]]:
    """Project the orchestrator's ``citations`` (cited subset of
    selected) into the wire-shape citation dicts. Strictly the blocks
    the LLM cited — never the broader retrieved set."""
    out: list[dict[str, Any]] = []
    for block in getattr(result, "citations", ()):
        cand = block.candidate
        out.append({
            "artifactId": cand.artifact_id,
            "artifactType": cand.artifact_kind,
            "sourceDocumentId": cand.document_id,
            "sourceLocation": (cand.extra or {}).get("section_path"),
            "chunkId": cand.chunk_id,
            "runId": cand.run_id,
        })
    return out


def _checks_from_gate_results(
    gate_results: tuple,
) -> list[ValidationCheckDTO]:
    """Translate orchestrator ``GateResult``s into the legacy
    ``ValidationCheckDTO`` list. Same wire shape, orchestrator source
    of truth."""
    out: list[ValidationCheckDTO] = []
    for g in gate_results:
        severity = "required" if g.severity == "required" else "optional"
        is_skipped = (
            g.severity == "advisory"
            and bool(g.detail.get("skipped"))
        )
        out.append(ValidationCheckDTO(
            name=g.name,
            severity=severity,
            passed=bool(g.passed) and not is_skipped,
            detail=g.reason,
            expected=None,
            actual=g.detail if g.detail else None,
            skipped=is_skipped,
            skipped_reason=(
                "gate skipped for this intent / plan policy"
                if is_skipped else None
            ),
        ))
    return out


def _validation_status_from_final(final_status: str) -> str:
    """Map ``QueryFinalStatus`` strings into the legacy
    ``ValidationStatus`` literal.

      * ``passed``                  → ``passed``
      * ``failed``                  → ``failed``
      * ``evidence_insufficient``   → ``failed``
      * ``retrieval_insufficient``  → ``inconclusive``
      * anything else               → ``inconclusive``
    """
    if final_status == "passed":
        return "passed"
    if final_status == "failed":
        return "failed"
    if final_status == "evidence_insufficient":
        return "failed"
    if final_status == "retrieval_insufficient":
        return "inconclusive"
    return "inconclusive"


def _evidence_blocks_from_trace(trace: Any) -> list[EvidenceBlockDTO]:
    """Project the orchestrator's ``llm_evidence`` into the DTO.
    Empty when the sufficiency gate failed before synthesis."""
    out: list[EvidenceBlockDTO] = []
    for block in getattr(trace, "llm_evidence", ()):
        cand = block.candidate
        out.append(EvidenceBlockDTO(
            artifact_id=cand.artifact_id,
            artifact_type=cand.artifact_kind,
            text=(block.body or cand.text_preview or "")[:4000],
            chunk_id=cand.chunk_id,
            score=float(cand.score or 0.0),
            section=(cand.extra or {}).get("section_path"),
            source_location=(cand.extra or {}).get("section_path"),
        ))
    return out


def _evidence_flags_from_trace(trace: Any) -> dict[str, bool]:
    """Modality flags the FE renders as Graph/Tables/Images chips."""
    kinds = {
        c.artifact_kind for c in getattr(trace, "all_candidates", ())
    }
    return {
        "graphUsed": "graph_json" in kinds or any(
            "graph" in (k or "") for k in kinds
        ),
        "tablesUsed": "enriched.tables" in kinds,
        "imagesUsed": "enriched.visuals" in kinds,
    }


__all__ = [
    "IngestionValidationService",
]
