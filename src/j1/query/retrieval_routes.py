"""Retrieval routes — pluggable adapters the orchestrator dispatches
in parallel.

Each route consumes a ``RetrievalJob`` (the plan's instruction) plus
a ``RouteContext`` (scope + project context) and returns a list of
``EvidenceCandidate``. Routes never decide what evidence to KEEP —
that's the EvidencePackBuilder's job. Routes only surface candidates.

Routes are deliberately small surfaces:

  * They MAY fail (timeout, backend down, no permission). The
    orchestrator records the failure on the ``RouteExecutionRecord``
    and still proceeds with other routes; one failed route doesn't
    kill the query.
  * They MUST honor scope. A route that surfaces a chunk from a
    different active run is a security bug — both the RAGAnything
    adapter (via workspace path) and the BM25 adapter (via
    ``eligible_run_ids``) enforce this.
  * They MUST NOT synthesize an answer. RAGAnything's native answer
    output is treated as advisory only and discarded by the
    orchestrator.

The route registry is a plain dict — easy to override per
deployment (a test can register an in-memory adapter; production
ships RAGAnything + BM25 + ArtifactLookup).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from j1.projects.context import ProjectContext
from j1.query.query_plan import (
    EvidenceCandidate,
    RetrievalJob,
    RetrievalRouteKind,
)
from j1.query.query_trace import RouteExecutionRecord
from j1.query.scope import QueryScope, RunScope, default_scope


# ---- Route context ----------------------------------------------


@dataclass(frozen=True)
class RouteContext:
    """The non-job inputs every route needs.

    ``ctx`` carries tenant/project; ``scope`` is the search-time
    filter (default workspace-wide, validation passes a RunScope);
    ``eligible_run_ids`` is the post-refactor eligibility gate.
    """

    ctx: ProjectContext
    scope: QueryScope = default_scope()
    eligible_run_ids: frozenset[str] | None = None
    document_id: str | None = None
    run_id: str | None = None


# ---- Route protocol ---------------------------------------------


class RetrievalRoute(Protocol):
    """A retrieval adapter. Implementations are plain classes — the
    orchestrator only needs ``execute``."""

    kind: RetrievalRouteKind

    def execute(
        self, job: RetrievalJob, context: RouteContext,
    ) -> list[EvidenceCandidate]: ...


# ---- Helpers ----------------------------------------------------


def _resolve_run_id(scope: QueryScope, fallback: str | None) -> str | None:
    """Return the single run_id for the active scope, when one
    exists. ``RunScope.run_id`` is the canonical source; other scope
    flavours don't pin one run, so the fallback (from
    ``RouteContext.run_id``) wins."""
    if isinstance(scope, RunScope):
        return scope.run_id
    return fallback


def _truncate(body: str, limit: int = 600) -> str:
    """Trim a chunk body to a preview-sized string. The route stores
    full bodies for the synthesizer separately; the preview is what
    lands in the trace + manual view."""
    if not body:
        return ""
    s = str(body)
    return s[:limit]


def _anchors_in(text: str, anchors: tuple[str, ...]) -> tuple[str, ...]:
    """Case-insensitive substring scan — same shape as
    ``j1.retrieval.anchors.count_anchors_present`` but inlined here
    to avoid a module-level dependency on that helper."""
    if not text or not anchors:
        return ()
    text_l = text.lower()
    matched: list[str] = []
    for a in anchors:
        if a and a.lower() in text_l:
            matched.append(a)
    return tuple(matched)


# ---- RAGAnything adapter ---------------------------------------


class RAGAnythingAdapter:
    """Primary retrieval route — wraps the existing
    ``RAGAnythingQueryProvider`` and projects its
    ``QueryResult.metadata`` into ``EvidenceCandidate`` rows.

    Why this layer exists: the orchestrator wants CHUNKS, not the
    LightRAG native prose answer. RAGAnything's hybrid mode returns
    both, and the provider currently surfaces the prose as
    ``QueryResult.answer``. The adapter discards that answer (it's
    advisory only — see docstring on RetrievalRoute) and projects
    the per-chunk evidence from ``metadata['evidence_chunks']`` /
    similar shapes the bridge exposes.

    Routes never call the LLM directly — RAGAnything's own LLM
    calls are limited by the retrieval graph, not the synthesizer.
    """

    kind: RetrievalRouteKind = RetrievalRouteKind.RAGANYTHING

    def __init__(
        self,
        provider: Any,
        *,
        chunk_kind_default: str = "chunk",
    ) -> None:
        self._provider = provider
        self._chunk_kind_default = chunk_kind_default

    def execute(
        self,
        job: RetrievalJob,
        context: RouteContext,
    ) -> list[EvidenceCandidate]:
        run_id = _resolve_run_id(context.scope, context.run_id)
        result = self._provider.query(
            context.ctx,
            job.query,
            max_results=job.max_results,
            document_id=context.document_id,
            run_id=run_id,
        )
        # The provider returns a ``QueryResult``. We only consume
        # its metadata + sources — the native ``answer`` field is
        # intentionally ignored.
        candidates: list[EvidenceCandidate] = []
        evidence_chunks = []
        if getattr(result, "metadata", None):
            evidence_chunks = result.metadata.get(
                "evidence_chunks", [],
            ) or []
        if not evidence_chunks:
            # Fall back to citations + sources when the bridge
            # didn't expose evidence_chunks. Each citation maps to
            # one candidate; body comes from metadata when available.
            for c in getattr(result, "citations", []) or []:
                candidates.append(
                    _candidate_from_citation(
                        c, job, context, run_id, self._chunk_kind_default,
                    )
                )
            return candidates
        for ec in evidence_chunks:
            candidates.append(_candidate_from_evidence_chunk(
                ec, job, context, run_id, self._chunk_kind_default,
            ))
        return candidates


def _candidate_from_evidence_chunk(
    chunk: Mapping[str, Any],
    job: RetrievalJob,
    context: RouteContext,
    run_id: str | None,
    default_kind: str,
) -> EvidenceCandidate:
    body = str(chunk.get("body") or chunk.get("text") or "")
    return EvidenceCandidate(
        route=job.route,
        artifact_id=str(chunk.get("artifact_id") or ""),
        artifact_kind=str(chunk.get("artifact_kind") or default_kind),
        chunk_id=chunk.get("chunk_id"),
        text_preview=_truncate(body),
        score=float(chunk.get("score") or 0.0),
        matched_anchors=_anchors_in(body, tuple(job.filters.get(
            "anchors", ()
        ))),
        run_id=chunk.get("run_id") or run_id,
        document_id=chunk.get("document_id") or context.document_id,
        project_id=context.ctx.project_id,
        extra={
            "section_path": chunk.get("section_path"),
            "lightrag_node_id": chunk.get("lightrag_node_id"),
            "body": body,  # full body kept here for the builder
        },
    )


def _candidate_from_citation(
    citation: str,
    job: RetrievalJob,
    context: RouteContext,
    run_id: str | None,
    default_kind: str,
) -> EvidenceCandidate:
    return EvidenceCandidate(
        route=job.route,
        artifact_id=str(citation),
        artifact_kind=default_kind,
        chunk_id=None,
        text_preview="",
        score=0.0,
        matched_anchors=(),
        run_id=run_id,
        document_id=context.document_id,
        project_id=context.ctx.project_id,
        extra={"body": ""},
    )


# ---- BM25 adapter -----------------------------------------------


class BM25Adapter:
    """Auxiliary lexical recall via the existing
    ``SqliteSearchIndexer``. Used for exact-phrase recall on anchor
    terms ("60% design", "estimate class") that the semantic
    retriever often ranks low.

    BM25 NEVER drives the answer — the orchestrator marks BM25-only
    candidates with their route so the answer-quality gate can flag
    answers built entirely on lexical recall. The
    ``EvidencePackBuilder`` may include BM25 candidates in the
    synthesis-evidence set, but the route metadata travels with the
    block."""

    kind: RetrievalRouteKind = RetrievalRouteKind.BM25

    def __init__(
        self,
        indexer: Any,
        *,
        eligible_run_ids_resolver: (
            Callable[[ProjectContext, QueryScope], frozenset[str] | None]
            | None
        ) = None,
    ) -> None:
        self._indexer = indexer
        self._eligible_run_ids_resolver = eligible_run_ids_resolver

    def execute(
        self,
        job: RetrievalJob,
        context: RouteContext,
    ) -> list[EvidenceCandidate]:
        # Prefer the explicit eligibility set on the context; fall
        # back to the resolver when the orchestrator didn't pre-
        # compute it. ``None`` means "no gate" — that's the legacy
        # diagnostic path (validation_scope=run), not the default.
        eligible = context.eligible_run_ids
        if eligible is None and self._eligible_run_ids_resolver:
            eligible = self._eligible_run_ids_resolver(
                context.ctx, context.scope,
            )
        artifact_types = None
        kind_filter = job.filters.get("artifact_kind")
        if kind_filter:
            artifact_types = [kind_filter]
        hits = self._indexer.search(
            context.ctx,
            job.query,
            artifact_types=artifact_types,
            max_results=job.max_results,
            scope=context.scope,
            eligible_run_ids=eligible,
        )
        candidates: list[EvidenceCandidate] = []
        for hit in hits or []:
            body = getattr(hit, "extracted_text", "") or ""
            candidates.append(EvidenceCandidate(
                route=job.route,
                artifact_id=hit.artifact_id,
                artifact_kind=hit.artifact_type,
                chunk_id=getattr(hit, "chunk_id", None),
                text_preview=_truncate(body),
                score=float(getattr(hit, "score", 0.0) or 0.0),
                matched_anchors=_anchors_in(
                    body, (job.query,),
                ),
                run_id=getattr(hit, "run_id", None),
                document_id=getattr(hit, "source_document_id", None),
                project_id=context.ctx.project_id,
                extra={
                    "section_path": (hit.metadata or {}).get(
                        "section_path"
                    ) if getattr(hit, "metadata", None) else None,
                    "body": body,
                    "bm25_score": float(
                        getattr(hit, "score", 0.0) or 0.0,
                    ),
                },
            ))
        return candidates


# ---- Artifact-lookup adapter ------------------------------------


class ArtifactLookupAdapter:
    """Direct artifact-registry route. For intents whose answer is
    materialised by an enriched overlay (``enriched.requirements``,
    ``enriched.risks``, ``enriched.consistency_findings``), this
    route reads the artifact JSON directly — no LLM, no
    similarity, no rerank. The block body is the artifact text.

    The route honors scope through the artifact registry's listing
    API: callers pass a ``RunScope`` and the registry filters by
    ``metadata.run_id``."""

    kind: RetrievalRouteKind = RetrievalRouteKind.ARTIFACT_LOOKUP

    def __init__(
        self,
        artifact_registry: Any,
        *,
        body_loader: Callable[[Any], str] | None = None,
    ) -> None:
        self._artifacts = artifact_registry
        # ``body_loader`` reads the artifact bytes off disk. Tests
        # can substitute a lambda; production wires the workspace
        # resolver's file reader.
        self._body_loader = body_loader

    def execute(
        self,
        job: RetrievalJob,
        context: RouteContext,
    ) -> list[EvidenceCandidate]:
        kind = job.filters.get("artifact_kind")
        if not kind:
            return []
        run_id = _resolve_run_id(context.scope, context.run_id)
        try:
            records = self._artifacts.list_artifacts(context.ctx)
        except Exception:  # noqa: BLE001 — route failure surfaces in trace
            return []
        candidates: list[EvidenceCandidate] = []
        for record in records:
            if record.kind != kind:
                continue
            # Scope filter — only artifacts produced by the active
            # run participate. ``metadata.run_id`` is set by the
            # processing service on registration.
            if run_id:
                rec_run = (record.metadata or {}).get("run_id")
                if rec_run and rec_run != run_id:
                    continue
            body = ""
            if self._body_loader is not None:
                try:
                    body = self._body_loader(record)
                except Exception:  # noqa: BLE001
                    body = ""
            candidates.append(EvidenceCandidate(
                route=job.route,
                artifact_id=record.artifact_id,
                artifact_kind=record.kind,
                chunk_id=None,
                text_preview=_truncate(body),
                score=1.0,  # artifact-lookup is binary: matches or
                # doesn't; rank by artifact age is the builder's job.
                matched_anchors=(),
                run_id=run_id,
                document_id=(
                    record.source_document_ids[0]
                    if record.source_document_ids else None
                ),
                project_id=context.ctx.project_id,
                extra={
                    "body": body,
                    "artifact_metadata": dict(record.metadata or {}),
                },
            ))
            if len(candidates) >= job.max_results:
                break
        return candidates


# ---- Route runner -----------------------------------------------


class RouteRunner:
    """Dispatches a plan's retrieval jobs across the configured
    routes. The runner produces ``RouteExecutionRecord``s — one per
    job — that land verbatim on the QueryTrace.

    The runner is intentionally serial today. Parallel dispatch is
    a future optimisation; the routes themselves are cheap (BM25 +
    artifact-lookup are sub-millisecond) and RAGAnything's own
    parallelism is per-question, not per-call. Locking in serial
    keeps the trace ordering deterministic — operators reading the
    manual view see routes in the same order as the plan.
    """

    def __init__(
        self,
        routes: Mapping[RetrievalRouteKind, RetrievalRoute],
    ) -> None:
        self._routes = dict(routes)

    def run_all(
        self,
        jobs: tuple[RetrievalJob, ...],
        context: RouteContext,
    ) -> tuple[RouteExecutionRecord, ...]:
        records: list[RouteExecutionRecord] = []
        for job in jobs:
            route = self._routes.get(job.route)
            if route is None:
                records.append(RouteExecutionRecord(
                    route=job.route,
                    query=job.query,
                    label=job.label,
                    duration_ms=0,
                    candidates=(),
                    error=f"no adapter registered for route {job.route.value}",
                ))
                continue
            started = time.perf_counter()
            err: str | None = None
            cands: list[EvidenceCandidate] = []
            try:
                cands = route.execute(job, context)
            except Exception as exc:  # noqa: BLE001 — route failure soft
                err = f"{type(exc).__name__}: {exc}"
            duration_ms = int((time.perf_counter() - started) * 1000)
            records.append(RouteExecutionRecord(
                route=job.route,
                query=job.query,
                label=job.label,
                duration_ms=duration_ms,
                candidates=tuple(cands),
                error=err,
            ))
        return tuple(records)


__all__ = [
    "ArtifactLookupAdapter",
    "BM25Adapter",
    "RAGAnythingAdapter",
    "RetrievalRoute",
    "RouteContext",
    "RouteRunner",
]
