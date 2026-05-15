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
    filter (default workspace-wide, validation passes a RunScope).

    Phase 6: ``eligible_snapshot_ids`` is now the canonical
    visibility gate. ``eligible_run_ids`` is retained as a
    trace/debug companion (legacy BM25 SQLite code may still read
    it) but is NOT load-bearing on the active path.
    """

    ctx: ProjectContext
    scope: QueryScope = default_scope()
    eligible_run_ids: frozenset[str] | None = None
    eligible_snapshot_ids: frozenset[str] | None = None
    # Pre-resolved ``(document_id, snapshot_id)`` allowlist. When set,
    # adapters that fan out per pair (RAGAnything) MUST prefer this
    # over the scope-driven resolver — it carries the caller's
    # explicit choice (e.g. ``snapshot_explicit`` validating a
    # CANDIDATE snapshot that hasn't been promoted to active yet, so
    # the active-snapshot-only eligibility resolver would return an
    # empty intersection and the adapter would falsely emit
    # ``no_eligible_snapshot``).
    eligible_snapshot_pairs: frozenset[tuple[str, str]] | None = None
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

    **Snapshot scoping (the load-bearing invariant)**: the LightRAG
    working-dir is keyed by ``(document_id, snapshot_id)`` (see
    ``j1.providers.raganything._bridge._snapshot_workspace_path``).
    The adapter resolves the eligible ``(document_id, snapshot_id)``
    pairs *before* calling the provider and fans out one query per
    pair, merging results. The provider raises
    ``WorkspaceScopeMissing`` if the adapter forgets — global
    fallback is never used on the active query path.
    """

    kind: RetrievalRouteKind = RetrievalRouteKind.RAGANYTHING

    def __init__(
        self,
        provider: Any,
        *,
        chunk_kind_default: str = "chunk",
        eligible_snapshot_pairs_resolver: (
            Callable[
                [ProjectContext, QueryScope],
                frozenset[tuple[str, str]] | None,
            ] | None
        ) = None,
    ) -> None:
        self._provider = provider
        self._chunk_kind_default = chunk_kind_default
        self._eligible_snapshot_pairs_resolver = (
            eligible_snapshot_pairs_resolver
        )

    def execute(
        self,
        job: RetrievalJob,
        context: RouteContext,
    ) -> list[EvidenceCandidate]:
        from j1.providers.errors import WorkspaceScopeMissing
        run_id = _resolve_run_id(context.scope, context.run_id)
        pairs = self._resolve_snapshot_pairs(context)
        if not pairs:
            # No eligible snapshot for this scope — fail closed.
            # Surface a single trace candidate so operators can see
            # the adapter ran and refused, with the reason. Score=0
            # so this never drives an answer.
            return [_no_eligible_snapshot_candidate(job, context, run_id)]

        # Per-snapshot fan-out: one provider call per (document, snapshot)
        # pair. The bridge's working_dir is keyed on both; a project-wide
        # query against N documents issues N RAGAnything queries.
        candidates: list[EvidenceCandidate] = []
        queried_snapshot_ids: list[str] = []
        scope_errors: list[str] = []
        for document_id, snapshot_id in sorted(pairs):
            try:
                result = self._provider.query(
                    context.ctx,
                    job.query,
                    max_results=job.max_results,
                    document_id=document_id,
                    run_id=run_id,
                    snapshot_id=snapshot_id,
                )
            except WorkspaceScopeMissing as exc:
                # Should be unreachable now — fan-out passes snapshot_id
                # explicitly — but if a future bridge path raises this
                # for a different reason, the trace records it.
                scope_errors.append(
                    f"{document_id}/{snapshot_id}: {exc}"
                )
                continue
            queried_snapshot_ids.append(snapshot_id)
            candidates.extend(
                self._candidates_from_result(
                    result, job, context, run_id,
                    document_id=document_id,
                    snapshot_id=snapshot_id,
                )
            )
        # If every fan-out call hit WorkspaceScopeMissing, return a
        # marker. Normal path returns the merged candidate list.
        if not candidates and scope_errors:
            return [_workspace_scope_missing_candidate(
                job, context, run_id, scope_errors,
            )]
        return candidates

    def _resolve_snapshot_pairs(
        self, context: RouteContext,
    ) -> frozenset[tuple[str, str]]:
        """Resolve ``(document_id, snapshot_id)`` pairs for this query.

        Preference order:
          0. ``context.eligible_snapshot_pairs`` — caller pre-resolved
             the pairs (e.g. ``snapshot_explicit`` for a candidate
             snapshot that isn't promoted to active yet). Skip the
             scope-driven resolver entirely; the caller already
             stated which snapshots to query.
          1. Resolver-supplied pairs filtered by
             ``context.eligible_snapshot_ids`` (the orchestrator
             may have narrowed the set).
          2. Resolver-supplied pairs as-is (full workspace fan-out).
          3. ``context.document_id`` narrows whichever set above to a
             single document when the caller pinned one.
        """
        pairs: frozenset[tuple[str, str]] | None
        if context.eligible_snapshot_pairs is not None:
            pairs = context.eligible_snapshot_pairs
        else:
            pairs = None
            if self._eligible_snapshot_pairs_resolver is not None:
                try:
                    pairs = self._eligible_snapshot_pairs_resolver(
                        context.ctx, context.scope,
                    )
                except Exception:  # noqa: BLE001 — resolver failure → no scope
                    pairs = None
            if not pairs:
                return frozenset()
            # Narrow by the orchestrator-supplied snapshot id set when
            # present. This lets the orchestrator pre-resolve eligibility
            # once and keeps BM25 + RAGAnything on the same allowlist.
            eligible = context.eligible_snapshot_ids
            if eligible:
                pairs = frozenset(
                    p for p in pairs if p[1] in eligible
                )
        if not pairs:
            return frozenset()
        # Narrow to a single document when the caller pinned one.
        if context.document_id:
            pairs = frozenset(
                p for p in pairs if p[0] == context.document_id
            )
        return pairs

    def _candidates_from_result(
        self,
        result: Any,
        job: RetrievalJob,
        context: RouteContext,
        run_id: str | None,
        *,
        document_id: str,
        snapshot_id: str,
    ) -> list[EvidenceCandidate]:
        candidates: list[EvidenceCandidate] = []
        # Preferred shape: bridge exposes structured chunks under
        # ``metadata['evidence_chunks']``. When LightRAG / the
        # bridge surfaces real per-chunk evidence one day, this
        # branch lights up.
        evidence_chunks = []
        if getattr(result, "metadata", None):
            evidence_chunks = result.metadata.get(
                "evidence_chunks", [],
            ) or []
        if evidence_chunks:
            for ec in evidence_chunks:
                candidates.append(_candidate_from_evidence_chunk(
                    ec, job, context, run_id, self._chunk_kind_default,
                ))
            return candidates
        # Second fallback: citation strings the bridge attached.
        for c in getattr(result, "citations", []) or []:
            candidates.append(
                _candidate_from_citation(
                    c, job, context, run_id, self._chunk_kind_default,
                )
            )
        if candidates:
            return candidates
        # Final fallback: the bridge returned ONLY a prose answer
        # (today's default). Surface that as a SINGLE advisory
        # candidate so the trace shows what LightRAG-native said.
        # Marked with ``raganything_native_answer=True`` in extra
        # so the EvidencePackBuilder can downrank / drop it when
        # real chunked evidence exists from other routes. Empty
        # answer → no candidates (sufficiency gate fails cleanly
        # as "retrieval returned nothing").
        native_answer = getattr(result, "answer", None) or ""
        native_answer = native_answer.strip()
        # Surface the LightRAG working_dir + status so the trace
        # shows which storage path the query actually read. When
        # the path is wrong (e.g. ``document_id`` missing → global
        # workdir fallback → no data), LightRAG produces a generic
        # "Sorry, I'm not able to provide an answer to that
        # question" for every query; operators couldn't see why
        # without this surface.
        result_metadata = getattr(result, "metadata", None) or {}
        working_dir = result_metadata.get("working_dir")
        result_status = getattr(result, "status", None)
        if not native_answer:
            # Emit a marker candidate so the trace shows the route
            # ran but the bridge returned NOTHING. ``score=0.0`` so
            # it never drives an answer.
            candidates.append(EvidenceCandidate(
                route=job.route,
                artifact_id="raganything.empty_response",
                artifact_kind="raganything.empty_response",
                chunk_id=None,
                text_preview=(
                    f"RAGAnything returned no answer. "
                    f"working_dir={working_dir!r}, "
                    f"status={getattr(result_status, 'value', result_status)!r}"
                ),
                score=0.0,
                matched_anchors=(),
                run_id=run_id,
                document_id=document_id,
                project_id=context.ctx.project_id,
                extra={
                    "body": "",
                    "raganything_native_answer": True,
                    "advisory_only": True,
                    "raganything_working_dir": working_dir,
                    "raganything_result_status": (
                        getattr(result_status, "value", str(result_status))
                    ),
                    "raganything_result_error": getattr(
                        result, "error", None,
                    ),
                    "snapshot_id": snapshot_id,
                },
            ))
            return candidates
        candidates.append(EvidenceCandidate(
            route=job.route,
            artifact_id="raganything.native_answer",
            artifact_kind="raganything.native_answer",
            chunk_id=None,
            text_preview=_truncate(native_answer),
            score=0.5,  # advisory — lower than real BM25 / chunk
            matched_anchors=(),
            run_id=run_id,
            document_id=document_id,
            project_id=context.ctx.project_id,
            extra={
                "body": native_answer,
                "raganything_native_answer": True,
                "advisory_only": True,
                "raganything_working_dir": working_dir,
                "snapshot_id": snapshot_id,
            },
        ))
        return candidates


def _no_eligible_snapshot_candidate(
    job: RetrievalJob,
    context: RouteContext,
    run_id: str | None,
) -> EvidenceCandidate:
    """Marker for the trace when the adapter could not resolve any
    eligible ``(document_id, snapshot_id)`` pair. Score 0 so the
    sufficiency gate sees a clean "retrieval returned nothing" and
    the synthesizer is never called.

    The text is SCOPE-AWARE — operators on Run Detail should never
    see "project may have no attached documents with an active
    snapshot" when they explicitly named a run. The
    project-active copy fires only when the scope is actually
    project-active (``WorkspaceScope``).
    """
    from j1.query.scope import ActiveScope as _ActiveScope
    from j1.query.scope import RunScope as _RunScope
    scope = context.scope
    if isinstance(scope, _RunScope):
        msg = (
            "Selected run has no queryable snapshot. The run may not "
            "exist for this project, may not have produced a "
            "snapshot yet, or its snapshot artifacts were deleted."
        )
        reason = "no_queryable_run_snapshot"
    elif isinstance(scope, _ActiveScope):
        msg = (
            "Document has no active snapshot to query. Re-index the "
            "document or wait for the current run to promote."
        )
        reason = "no_active_document_snapshot"
    else:
        msg = (
            "No attached documents with an active snapshot in this "
            "project. Attach a document or run an initial ingest."
        )
        reason = "no_active_project_snapshot"
    return EvidenceCandidate(
        route=job.route,
        artifact_id="raganything.no_eligible_snapshot",
        artifact_kind="raganything.no_eligible_snapshot",
        chunk_id=None,
        text_preview=f"RAGAnything adapter refused: {msg}",
        score=0.0,
        matched_anchors=(),
        run_id=run_id,
        document_id=context.document_id,
        project_id=context.ctx.project_id,
        extra={
            "body": "",
            "raganything_refused": "no_eligible_snapshot",
            "raganything_refused_reason": reason,
            "advisory_only": True,
        },
    )


def _workspace_scope_missing_candidate(
    job: RetrievalJob,
    context: RouteContext,
    run_id: str | None,
    errors: list[str],
) -> EvidenceCandidate:
    """Marker for the trace when every fan-out call hit
    ``WorkspaceScopeMissing``. Operator-visible reason so the cause
    isn't reverse-engineered from a generic FAILED route."""
    return EvidenceCandidate(
        route=job.route,
        artifact_id="raganything.workspace_scope_missing",
        artifact_kind="raganything.workspace_scope_missing",
        chunk_id=None,
        text_preview=(
            "RAGAnything bridge raised WorkspaceScopeMissing for every "
            f"eligible snapshot ({len(errors)} attempted)."
        ),
        score=0.0,
        matched_anchors=(),
        run_id=run_id,
        document_id=context.document_id,
        project_id=context.ctx.project_id,
        extra={
            "body": "",
            "raganything_refused": "workspace_scope_missing",
            "advisory_only": True,
            "errors": errors,
        },
    )


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


class LexicalEvidenceAdapter:
    """Phase 6: auxiliary lexical recall via the canonical
    ``EvidenceIndexAdapter`` (Postgres FTS by default).

    Ranking is whatever the underlying adapter implements —
    Postgres FTS uses ``ts_rank_cd`` over the GIN-indexed tsvector,
    which is a normalised cover-density rank, NOT true BM25.
    Operators should not confuse the two: ``ts_rank_cd`` weights
    by term frequency × inverse coverage gaps, while BM25 weights
    by term frequency × inverse document frequency with length
    normalisation. The route metadata travels with the block so
    the answer-quality gate can flag answers built entirely on
    lexical recall — the same contract the previous BM25 route had.

    The class is named ``LexicalEvidenceAdapter`` to keep the
    "auxiliary lexical recall" intent without the BM25 misnomer.
    The route kind is still ``RetrievalRouteKind.BM25`` for
    backward-compat with the orchestrator's job dispatch and the
    trace JSON the FE renders; renaming the enum would touch every
    workflow trace ever recorded and isn't worth the churn this
    phase.

    Input scope: ``RouteContext.eligible_snapshot_ids`` is the
    canonical gate. ``RouteContext.eligible_run_ids`` is read only
    when the Phase-6 snapshot set is missing AND the legacy SQLite
    indexer is wired (a fully-deprecated test-only path).
    """

    kind: RetrievalRouteKind = RetrievalRouteKind.BM25

    def __init__(
        self,
        evidence_adapter: Any,
        *,
        eligible_snapshot_ids_resolver: (
            Callable[
                [ProjectContext, QueryScope],
                frozenset[str] | None,
            ] | None
        ) = None,
    ) -> None:
        self._adapter = evidence_adapter
        self._eligible_snapshot_ids_resolver = eligible_snapshot_ids_resolver

    def execute(
        self,
        job: RetrievalJob,
        context: RouteContext,
    ) -> list[EvidenceCandidate]:
        # Phase 6: snapshot-id allowlist is the canonical visibility
        # key. Resolver fallback exists for the validation diagnostic
        # path where the orchestrator didn't pre-compute it.
        eligible = context.eligible_snapshot_ids
        if eligible is None and self._eligible_snapshot_ids_resolver:
            eligible = self._eligible_snapshot_ids_resolver(
                context.ctx, context.scope,
            )
        if not eligible:
            # Empty allowlist → no visible snapshots → no results.
            # Refusal short-circuits the adapter call entirely
            # (matches the SearchService contract).
            return []
        try:
            hits = self._adapter.search(
                context.ctx,
                query=job.query,
                allowed_snapshot_ids=list(eligible),
                max_results=job.max_results,
            )
        except Exception:  # noqa: BLE001 — auxiliary recall is best-effort
            return []
        candidates: list[EvidenceCandidate] = []
        for hit in hits or []:
            body = getattr(hit, "content", "") or ""
            candidates.append(EvidenceCandidate(
                route=job.route,
                artifact_id=hit.artifact_id,
                artifact_kind=getattr(hit, "artifact_type", None) or "evidence_chunk",
                chunk_id=getattr(hit, "chunk_id", None),
                text_preview=_truncate(body),
                score=float(getattr(hit, "score", 0.0) or 0.0),
                matched_anchors=_anchors_in(
                    body, (job.query,),
                ),
                run_id=getattr(hit, "created_by_run_id", None),
                document_id=getattr(hit, "document_id", None),
                project_id=context.ctx.project_id,
                extra={
                    "section_path": (
                        getattr(hit, "metadata", None) or {}
                    ).get("section_path"),
                    "body": body,
                    # Phase 6: keep the legacy ``bm25_score`` key for
                    # back-compat with existing trace consumers; the
                    # value is now ``ts_rank_cd``, not true BM25.
                    "bm25_score": float(
                        getattr(hit, "score", 0.0) or 0.0,
                    ),
                    "snapshot_id": getattr(hit, "snapshot_id", None),
                    "lexical_ranker": "ts_rank_cd",
                },
            ))
        return candidates


# Phase 6: backward-compat alias for any external code that still
# imports ``BM25Adapter``. The class name is misleading; new code
# should use ``LexicalEvidenceAdapter``. Phase 7 deletes the alias.
BM25Adapter = LexicalEvidenceAdapter


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
