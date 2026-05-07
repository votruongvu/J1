"""IngestionValidationService — read/write surface for validation.

Phase 1: synchronous manual test query (`run_manual_test_query`).
Phase 2: generate / list / get validation sets, run validation,
list / get validation runs.

All methods enforce run ownership via `_load_run` (raises
`ReviewNotFound` → REST 404 on cross-tenant / cross-project access).

The service is constructed from already-built dependencies (no
container / no facade) so tests wire it from `tmp_path` fixtures
the same way `IngestionResultReviewService` is wired.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from j1.artifacts.registry import ArtifactRegistry
from j1.audit.recorder import AuditRecorder
from j1.ingestion_review.exceptions import ReviewNotFound
from j1.ingestion_review.projectors.chunks import ChunkProjector, _ChunkRecord
from j1.projects.context import ProjectContext
from j1.query.engine import HybridQueryEngine
from j1.query.models import QueryMode, QueryRequest
from j1.query.scope import RunScope
from j1.runs.models import IngestionRun
from j1.runs.store import IngestionRunStore
from j1.validation.checks import aggregate_status, run_checks
from j1.validation.dtos import (
    ManualTestQueryRequest,
    ManualTestQueryResponseDTO,
    RetrievedChunkRefDTO,
    ValidationCheckDTO,
    ValidationCitationDTO,
    ValidationRunDTO,
    ValidationSetDTO,
)
from j1.validation.generator import (
    DefaultTestCaseGenerator,
    GenerationOptions,
)
from j1.validation.runner import (
    DefaultValidationRunner,
    MAX_CASES_PER_RUN,
)
from j1.validation.store import ValidationRunStore, ValidationSetStore
from j1.workspace.layout import WorkspaceArea
from j1.workspace.resolver import WorkspaceResolver

_log = logging.getLogger("j1.validation")

_ACTION_MANUAL_QUERY = "j1.validation.manual_query.completed"
_ACTION_SET_GENERATED = "j1.validation.set_generated"
_ACTION_RUN_COMPLETED = "j1.validation.run_completed"
_TARGET_KIND_RUN = "ingestion_run"
_TARGET_KIND_VALIDATION_SET = "validation_set"
_TARGET_KIND_VALIDATION_RUN = "validation_run"

# Hard cap on `top_k` — Phase 1's manual query is synchronous and we
# don't want a tester accidentally requesting 10k results and blocking
# the worker. The REST layer also clamps via Pydantic but the service
# enforces too so stand-alone callers (tests, future async paths) get
# the same guarantee.
_TOP_K_HARD_CAP = 50

# Preview length for retrieved-chunk excerpts on the response. Mirrors
# the chunk projector's value so the UI layer renders consistent
# preview lengths across the Validation tab and the Chunks tab.
_PREVIEW_MAX_CHARS = 240


class IngestionValidationService:
    """Validation surface — manual queries (Phase 1) + generated
    sets and runs (Phase 2).

    Verdicts / human overrides / async execution arrive in later
    phases; the constructor accepts the relevant dependencies as
    Optional so a Phase 1-only deployment can still wire just the
    manual-query path.
    """

    def __init__(
        self,
        *,
        run_store: IngestionRunStore,
        artifact_registry: ArtifactRegistry,
        query_engine: HybridQueryEngine,
        audit: AuditRecorder | None = None,
        workspace: WorkspaceResolver | None = None,
        validation_set_store: ValidationSetStore | None = None,
        validation_run_store: ValidationRunStore | None = None,
        test_case_generator: DefaultTestCaseGenerator | None = None,
    ) -> None:
        self._run_store = run_store
        self._artifacts = artifact_registry
        self._query_engine = query_engine
        self._audit = audit
        self._workspace = workspace
        self._set_store = validation_set_store
        self._run_store_v = validation_run_store
        self._generator = test_case_generator

    def run_manual_test_query(
        self,
        ctx: ProjectContext,
        run_id: str,
        request: ManualTestQueryRequest,
        *,
        actor: str = "system",
    ) -> ManualTestQueryResponseDTO:
        """Execute a single tester question against this run.

        Phase 1: synchronous. Calls `HybridQueryEngine.query` with
        `RunScope(run_id)` so retrieval is restricted to artifacts
        produced by this run. Builds deterministic check results
        from the engine output.

        Raises `ReviewNotFound` (→ 404 at REST) when the run doesn't
        exist in `(ctx.tenant_id, ctx.project_id)`. Cross-tenant /
        cross-project access produces an identical 404 — existence
        is never leakable.
        """
        run = self._load_run(ctx, run_id)
        # Reserve the request id up-front so the value the FE sees in
        # the response also lands in the audit log on the same row.
        request_id = f"tq-{uuid.uuid4().hex[:12]}"

        top_k = max(1, min(request.top_k, _TOP_K_HARD_CAP))
        mode = _coerce_mode(request.mode)

        query_request = QueryRequest(
            question=request.question,
            mode=mode,
            max_results=top_k,
            scope=RunScope(run_id=run.run_id),
        )

        try:
            response = self._query_engine.query(ctx, query_request)
        except Exception as exc:  # noqa: BLE001
            # Engine failures must not 500 — surface them as a structured
            # `inconclusive` response so the FE can render an actionable
            # message instead of a transport error.
            _log.warning(
                "validation manual query engine failure run_id=%s: %s",
                run.run_id, exc,
            )
            return _inconclusive_response(
                request_id=request_id,
                run_id=run.run_id,
                question=request.question,
                error=str(exc),
            )

        retrieved = _retrieved_chunks_from_response(response)
        citations = _citations_from_response(response)

        checks = run_checks(
            ctx=ctx,
            run_id=run.run_id,
            answer=response.answer,
            retrieved_chunks=retrieved,
            citations=citations,
            citation_required=request.citation_required,
            artifact_registry=self._artifacts,
        )
        validation_status = aggregate_status(checks)

        evidence_flags = {
            "graphUsed": bool(response.graph_paths),
            "tablesUsed": _has_artifact_kind(retrieved, "enriched.tables"),
            "imagesUsed": _has_artifact_kind(retrieved, "enriched.visuals"),
        }

        raw_response = (
            _engine_response_to_raw(response)
            if request.include_raw
            else None
        )

        self._audit_manual_query(
            ctx=ctx,
            run=run,
            request_id=request_id,
            request=request,
            validation_status=validation_status,
            retrieved_count=len(retrieved),
            citation_count=len(citations),
            actor=actor,
        )

        return ManualTestQueryResponseDTO(
            request_id=request_id,
            run_id=run.run_id,
            question=request.question,
            answer=response.answer,
            mode_used=response.mode_used,
            retrieved_chunks=retrieved,
            citations=[_citation_to_dict(c) for c in citations],
            checks=checks,
            validation_status=validation_status,
            evidence_flags=evidence_flags,
            raw_response=raw_response,
        )

    # ---- Phase 2: validation sets ----------------------------------------

    def generate_validation_set(
        self,
        ctx: ProjectContext,
        run_id: str,
        *,
        max_cases: int = 25,
        citation_required: bool = False,
        force: bool = False,
        actor: str = "system",
    ) -> ValidationSetDTO:
        """Generate a fresh validation set from this run's chunks.

        Idempotent on `(run_id, generator_version, artifacts_hash)`:
        when an existing set in the store has a matching hash and
        `force=False`, the existing record is returned unchanged.
        Set `force=True` to bypass the cache (e.g. after editing the
        prompt or chunk content).

        Raises `ReviewNotFound` if the run isn't visible in the
        caller's `(tenant, project)`.
        Raises `RuntimeError` when Phase 2 dependencies aren't wired
        (set store / generator) — same shape as Phase 1's missing-
        deps degradation.
        """
        if self._set_store is None or self._generator is None or self._workspace is None:
            raise RuntimeError(
                "validation set generation not configured "
                "(pass validation_set_store, test_case_generator, "
                "workspace to IngestionValidationService)"
            )
        max_cases = max(1, min(max_cases, MAX_CASES_PER_RUN))

        run = self._load_run(ctx, run_id)
        chunks = self._project_run_chunks(ctx, run)

        # Generate first so we can compute the artifacts hash off
        # the sampled chunks. Cheap — no LLM call yet on the empty
        # path; the real LLM cost is per chunk inside generate().
        vset = self._generator.generate(
            run_id=run.run_id,
            document_ids=_document_ids(run),
            chunks=chunks,
            options=GenerationOptions(
                max_cases=max_cases,
                citation_required=citation_required,
            ),
            actor=actor,
        )

        # Idempotency: scan existing sets for a hash match. Force
        # bypasses the cache.
        if not force:
            existing = self._find_existing_set(ctx, run.run_id, vset.artifacts_content_hash)
            if existing is not None:
                _log.debug(
                    "reusing existing validation set %s (hash match)",
                    existing.validation_set_id,
                )
                return existing

        self._set_store.upsert(ctx, vset)
        self._audit_set_generated(ctx, run, vset, actor)
        return vset

    def list_validation_sets(
        self, ctx: ProjectContext, run_id: str,
    ) -> list[ValidationSetDTO]:
        """List sets for a run, most-recent-first. Empty list when
        the run exists but no sets have been generated."""
        if self._set_store is None:
            return []
        # Run-ownership check first — cross-tenant access raises 404
        # rather than returning an empty list (which would leak
        # existence: missing run vs. no-sets-yet should not be
        # distinguishable).
        self._load_run(ctx, run_id)
        return self._set_store.list_for_run(ctx, run_id)

    def get_validation_set(
        self,
        ctx: ProjectContext,
        run_id: str,
        validation_set_id: str,
    ) -> ValidationSetDTO:
        """Fetch one set by id. Raises `ReviewNotFound` for missing /
        cross-tenant / set-belongs-to-different-run."""
        if self._set_store is None:
            raise ReviewNotFound(
                f"validation set {validation_set_id!r} not found"
            )
        self._load_run(ctx, run_id)
        vset = self._set_store.get(ctx, validation_set_id)
        if vset is None or vset.run_id != run_id:
            # Identical message regardless of cause — existence is
            # not probeable across runs.
            raise ReviewNotFound(
                f"validation set {validation_set_id!r} not found"
            )
        return vset

    # ---- Phase 2: validation runs ----------------------------------------

    def run_validation(
        self,
        ctx: ProjectContext,
        run_id: str,
        validation_set_id: str,
        *,
        actor: str = "system",
    ) -> ValidationRunDTO:
        """Execute a validation set. Synchronous in Phase 2 — blocks
        until every case has run. Persists three lifecycle snapshots
        (pending → running → terminal) via the run store.

        Raises `ReviewNotFound` for unknown / cross-tenant set or run.
        Raises `RuntimeError` when the Phase 2 dependencies aren't
        wired."""
        if self._run_store_v is None:
            raise RuntimeError(
                "validation run execution not configured "
                "(pass validation_run_store to IngestionValidationService)"
            )
        # Both ownership gates first — `_load_run` then a set-scope
        # check. Cross-tenant probing for a known set under a wrong
        # project must still 404.
        vset = self.get_validation_set(ctx, run_id, validation_set_id)

        runner = DefaultValidationRunner(
            query_engine=self._query_engine,
            artifact_registry=self._artifacts,
            lifecycle_callback=lambda v: self._run_store_v.upsert(ctx, v),  # type: ignore[union-attr]
        )
        vrun = runner.run(ctx, vset, actor=actor)
        self._audit_run_completed(ctx, run_id, vrun, actor)
        return vrun

    def list_validation_runs(
        self, ctx: ProjectContext, run_id: str,
    ) -> list[ValidationRunDTO]:
        if self._run_store_v is None:
            return []
        self._load_run(ctx, run_id)
        return self._run_store_v.list_for_run(ctx, run_id)

    def get_validation_run(
        self,
        ctx: ProjectContext,
        run_id: str,
        validation_run_id: str,
    ) -> ValidationRunDTO:
        if self._run_store_v is None:
            raise ReviewNotFound(
                f"validation run {validation_run_id!r} not found"
            )
        self._load_run(ctx, run_id)
        vrun = self._run_store_v.get(ctx, validation_run_id)
        if vrun is None or vrun.run_id != run_id:
            raise ReviewNotFound(
                f"validation run {validation_run_id!r} not found"
            )
        return vrun

    # ---- Phase 2 helpers (private) -------------------------------------

    def _project_run_chunks(
        self, ctx: ProjectContext, run: IngestionRun,
    ) -> list[_ChunkRecord]:
        """Use the existing `ChunkProjector` to flatten the run's
        chunk artifacts into a list of `_ChunkRecord`. Reuses the
        same `path_resolver` pattern the review service uses so
        the two surfaces see identical chunk text."""
        if self._workspace is None:
            return []
        # Resolve only chunk-kind artifacts that belong to this run.
        # Phase 4+ artifact tagging means we read directly from the
        # registry by run_id; Phase 1's lineage fallback is preserved
        # in `_resolve_run_artifacts` (we don't need that here yet).
        artifacts = [
            a for a in self._artifacts.list_artifacts(ctx)
            if a.kind == "chunk" and a.metadata.get("run_id") == run.run_id
        ]
        # Closure binds `ctx` so the projector can resolve paths
        # without knowing about the workspace.
        def _resolver(record):
            from pathlib import PurePosixPath
            location = record.location
            parts = PurePosixPath(location).parts
            if len(parts) < 2:
                from pathlib import Path
                return Path(location)
            area_name, *rest = parts
            area = WorkspaceArea(area_name)
            return self._workspace.area(ctx, area).joinpath(*rest)  # type: ignore[union-attr]

        projector = ChunkProjector(path_resolver=_resolver)
        return projector.project_records(artifacts)

    def _find_existing_set(
        self,
        ctx: ProjectContext,
        run_id: str,
        artifacts_content_hash: str | None,
    ) -> ValidationSetDTO | None:
        """Idempotency lookup — returns the most-recent set whose
        `artifacts_content_hash` matches. None when no match (caller
        proceeds to upsert the freshly generated set)."""
        if self._set_store is None or not artifacts_content_hash:
            return None
        for existing in self._set_store.list_for_run(ctx, run_id):
            if existing.artifacts_content_hash == artifacts_content_hash:
                return existing
        return None

    def _audit_set_generated(
        self,
        ctx: ProjectContext,
        run: IngestionRun,
        vset: ValidationSetDTO,
        actor: str,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(
                ctx,
                actor=actor,
                action=_ACTION_SET_GENERATED,
                target_kind=_TARGET_KIND_VALIDATION_SET,
                target_id=vset.validation_set_id,
                correlation_id=run.run_id,
                payload={
                    "validationSetId": vset.validation_set_id,
                    "runId": run.run_id,
                    "caseCount": len(vset.test_cases),
                    "source": vset.source,
                    "generatorVersion": vset.generator_version,
                },
            )
        except Exception:  # noqa: BLE001
            _log.debug("audit write failed for set generation", exc_info=True)

    def _audit_run_completed(
        self,
        ctx: ProjectContext,
        run_id: str,
        vrun: ValidationRunDTO,
        actor: str,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(
                ctx,
                actor=actor,
                action=_ACTION_RUN_COMPLETED,
                target_kind=_TARGET_KIND_VALIDATION_RUN,
                target_id=vrun.validation_run_id,
                correlation_id=run_id,
                payload={
                    "validationRunId": vrun.validation_run_id,
                    "validationSetId": vrun.validation_set_id,
                    "runId": run_id,
                    "executionStatus": vrun.execution_status,
                    "validationStatus": vrun.validation_status,
                    "total": vrun.summary.total,
                    "passed": vrun.summary.passed,
                    "failed": vrun.summary.failed,
                },
            )
        except Exception:  # noqa: BLE001
            _log.debug("audit write failed for run completion", exc_info=True)

    # ---- Internals -----------------------------------------------------

    def _load_run(self, ctx: ProjectContext, run_id: str) -> IngestionRun:
        """Run-ownership gate.

        Same shape and behaviour as `IngestionResultReviewService._load_run`:
        identical message on missing-vs-cross-tenant so existence is
        not probeable. Returning the typed `ReviewNotFound` lets the
        REST layer share the existing exception handler.
        """
        run = self._run_store.get(ctx, run_id)
        if run is None:
            raise ReviewNotFound(f"ingestion run {run_id!r} not found")
        return run

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
        except Exception:  # noqa: BLE001
            # Telemetry never fails the user-facing call.
            _log.debug("audit write failed for manual test query", exc_info=True)


# ---- Module-level helpers (easy to unit-test) --------------------------


def _document_ids(run: IngestionRun) -> list[str]:
    """Best-effort recovery of the run's target documents.

    Mirrors the helper in `j1.ingestion_review.service` so the
    validation set carries the same document_ids list the rest of
    the review surface surfaces. Inlined rather than imported to
    avoid coupling validation to review's private internals.
    """
    raw = run.metadata.get("target_document_ids")
    if isinstance(raw, list) and raw:
        seen: list[str] = []
        for entry in raw:
            text = str(entry)
            if text and text not in seen:
                seen.append(text)
        if run.document_id and run.document_id not in seen:
            seen.append(run.document_id)
        return seen
    return [run.document_id] if run.document_id else []


def _coerce_mode(raw: str) -> QueryMode:
    """Tolerantly map a request-supplied mode string to a `QueryMode`.

    Unknown values fall back to AUTO so a tester typo can't turn into
    a 500. The REST layer additionally validates upstream, but the
    service is the source of truth for the final dispatch.
    """
    try:
        return QueryMode(raw)
    except ValueError:
        return QueryMode.AUTO


def _retrieved_chunks_from_response(response: Any) -> list[RetrievedChunkRefDTO]:
    """Translate `QueryResponse.sources` into the public chunk-ref DTO.

    `score` is not surfaced on the engine's `SourceReference` today,
    so we default to 0.0 — the FE renders this as a neutral indicator.
    Hooking up real BM25 scores requires plumbing them through
    `KnowledgeQueryProvider`, which is a Phase 2+ concern.
    """
    out: list[RetrievedChunkRefDTO] = []
    for source in getattr(response, "sources", []):
        title = str(getattr(source, "title", "") or "")
        out.append(
            RetrievedChunkRefDTO(
                artifact_id=source.artifact_id,
                chunk_id=getattr(source, "chunk_id", None),
                run_id=getattr(source, "run_id", None),
                document_id=getattr(source, "source_document_id", None),
                source_location=getattr(source, "source_location", None),
                score=0.0,
                preview=title[:_PREVIEW_MAX_CHARS],
            )
        )
    return out


def _citations_from_response(response: Any) -> list[ValidationCitationDTO]:
    """Project the engine's `SourceReference` list into the local
    validation citation DTO.

    Phase 1's REST endpoint emits the same list as both
    `retrievedChunks[]` and `citations[]` because the underlying
    `HybridQueryEngine` doesn't yet distinguish "the chunks that
    matched" from "the chunks the answer cites." Splitting the two
    is a Phase 2+ concern (LLM-judge attribution).
    """
    out: list[ValidationCitationDTO] = []
    for source in getattr(response, "sources", []):
        out.append(
            ValidationCitationDTO(
                artifact_id=source.artifact_id,
                artifact_type=source.artifact_type,
                source_document_id=getattr(source, "source_document_id", None),
                source_location=getattr(source, "source_location", None),
                chunk_id=getattr(source, "chunk_id", None),
                run_id=getattr(source, "run_id", None),
            )
        )
    return out


def _citation_to_dict(citation: ValidationCitationDTO) -> dict[str, Any]:
    """REST schema-friendly camelCase dict.

    The validation service produces dataclasses; the REST adapter
    converts them to the response Pydantic models. Going through a
    plain dict here keeps the REST layer the only place that needs
    to know about CamelModel.
    """
    return {
        "artifactId": citation.artifact_id,
        "artifactType": citation.artifact_type,
        "sourceDocumentId": citation.source_document_id,
        "sourceLocation": citation.source_location,
        "chunkId": citation.chunk_id,
        "runId": citation.run_id,
    }


def _has_artifact_kind(
    retrieved: list[RetrievedChunkRefDTO], kind_prefix: str,
) -> bool:
    """Return True when any retrieved item's artifact_id resolves to
    an artifact whose kind starts with the given prefix.

    We can't tell the kind directly from the chunk-ref DTO (it only
    surfaces ids/locations), so this is a forward-stub for the
    Phase 4 modality-aware checks. Phase 1 sets the flag to False
    unconditionally for the table/image variants — the engine's
    response carries `graph_paths` for `graphUsed`, which is the
    only flag we can populate honestly today.
    """
    # Stub for Phase 1 — modality-aware checks land in Phase 4 with
    # the artifact-registry lookup wired in. Returning False here is
    # the truthful "we don't know yet" answer; the FE can render a
    # neutral indicator instead of a misleading green check.
    _ = retrieved, kind_prefix
    return False


def _engine_response_to_raw(response: Any) -> dict[str, Any]:
    """Project the engine response into a JSON-friendly dict.

    Callers asking for `?includeRaw=true` get the full server-side
    view of the engine result for debugging — citations, related
    artifacts, graph paths, warnings, mode used. The dict is shallow
    on purpose; deep introspection of vendor objects is out of
    Phase 1 scope.
    """
    return {
        "answer": response.answer,
        "modeUsed": response.mode_used,
        "confidence": response.confidence,
        "reviewRequired": response.review_required,
        "warnings": list(response.warnings),
        "warningCategories": [c.value for c in response.warning_categories],
        "relatedArtifacts": list(response.related_artifacts),
        "graphPaths": [
            {
                "nodes": list(p.nodes),
                "edges": list(p.edges),
                "description": p.description,
            }
            for p in response.graph_paths
        ],
        "sources": [
            {
                "artifactId": s.artifact_id,
                "artifactType": s.artifact_type,
                "title": s.title,
                "sourceDocumentId": s.source_document_id,
                "sourceLocation": s.source_location,
                "chunkId": getattr(s, "chunk_id", None),
                "runId": getattr(s, "run_id", None),
            }
            for s in response.sources
        ],
    }


def _inconclusive_response(
    *,
    request_id: str,
    run_id: str,
    question: str,
    error: str,
) -> ManualTestQueryResponseDTO:
    """Build a response for the engine-failure path.

    `validation_status` is `inconclusive` (not `failed`) so the FE
    renders this as "couldn't determine" rather than "the document
    doesn't answer the question." Operators shouldn't act on a
    failed deterministic check that didn't actually run.
    """
    failure_check = ValidationCheckDTO(
        name="engine_invocation",
        severity="required",
        passed=False,
        detail=f"engine raised: {error}",
        expected="successful query",
        actual="exception",
    )
    return ManualTestQueryResponseDTO(
        request_id=request_id,
        run_id=run_id,
        question=question,
        answer="",
        mode_used="",
        retrieved_chunks=[],
        citations=[],
        checks=[failure_check],
        validation_status="inconclusive",
        evidence_flags={
            "graphUsed": False,
            "tablesUsed": False,
            "imagesUsed": False,
        },
        raw_response=None,
    )
