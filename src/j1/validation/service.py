"""IngestionValidationService — Phase 1 manual test query.

The service is the read/write entry point for the validation surface.
Phase 1 has one method: `run_manual_test_query`. It:

  1. Loads the run (raises `ReviewNotFound` on cross-tenant access —
     same uniform 404 shape as the rest of the review surface).
  2. Calls the existing `HybridQueryEngine.query` with a `RunScope`
     so retrieval is filtered to artifacts produced by this run.
  3. Composes deterministic check results from the engine output.
  4. Returns a `ManualTestQueryResponseDTO` carrying the answer,
     retrieved chunks, citations, checks, and the aggregated
     `validationStatus`.

There is NO persistence in Phase 1. Audit logging is best-effort
through the existing `AuditRecorder` so /events shows the manual
query, but no `ValidationRun` record is created.

The service is constructed from already-built dependencies — no
container / no facade — so tests can wire it from `tmp_path`
fixtures the same way `IngestionResultReviewService` is wired.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from j1.artifacts.registry import ArtifactRegistry
from j1.audit.recorder import AuditRecorder
from j1.ingestion_review.exceptions import ReviewNotFound
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
)

_log = logging.getLogger("j1.validation")

_ACTION_MANUAL_QUERY = "j1.validation.manual_query.completed"
_TARGET_KIND_RUN = "ingestion_run"

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
    """Phase 1 surface — manual test query only.

    Validation sets / runs / verdicts arrive in later phases; the
    constructor stays minimal so we don't paint ourselves into a
    corner with premature dependencies.
    """

    def __init__(
        self,
        *,
        run_store: IngestionRunStore,
        artifact_registry: ArtifactRegistry,
        query_engine: HybridQueryEngine,
        audit: AuditRecorder | None = None,
    ) -> None:
        self._run_store = run_store
        self._artifacts = artifact_registry
        self._query_engine = query_engine
        self._audit = audit

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
