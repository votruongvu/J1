"""Strict active-document / active-run scope filter.

This is the gate that prevents the audit-observed contamination
where a query for document A surfaces chunks from document B.

Two responsibilities:

  1. ``annotate_scope`` — extract scope metadata (document_id,
     run_id, source_document_id, source_run_id) from a candidate
     in a uniform way so downstream code doesn't have to
     re-implement getattr-or-dict for every retrieval source
     (BM25 hits, native results, graph nodes, enriched artifacts
     ALL go through this).

  2. ``enforce_active_scope`` — drop every candidate whose
     extracted scope doesn't match the active document/run, and
     emit a structured drop event for each rejection so the
     audit log explains the contamination.

Design rules:

  * **Strict by default.** When an active scope is supplied, a
    candidate without scope metadata is rejected (``NO_SCOPE_METADATA``).
    We prefer false negatives (drop a useful but unscoped chunk)
    over the failure mode in the audit (admit an off-document
    chunk silently).

  * **No exemptions inside the filter.** "Cross-document search"
    is the caller passing ``active_document_id=None``. The
    filter doesn't have its own knob.

  * **Run scope is finer-grained than document scope.** When
    ``active_run_id`` is supplied, source_run_id must match.
    Document scope is checked first because a chunk from doc-A
    run-X is wrong for ANY run of doc-B regardless of run scope.

  * **Generic across artifact types.** Works for ``chunk``,
    ``compiled.text``, ``graph_json``, ``enriched.*`` because
    they share the metadata convention (``run_id``,
    ``source_document_id``, ``document_id``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from j1.retrieval.diagnostics import (
        CandidateDiagnostic, RetrievalDiagnostics,
    )

# Sentinel marker for "no active scope supplied — admit everything".
# Distinct from ``None`` on a candidate's field (which means the
# candidate doesn't know its own document_id).
_UNSCOPED = object()


@dataclass(frozen=True)
class CandidateScope:
    """Extracted scope metadata for one candidate.

    Either ``document_id`` field can be ``None`` when the
    artifact doesn't carry a scope identifier — that's the
    ``no_scope_metadata`` case the strict filter rejects."""

    artifact_id: str
    artifact_type: str | None
    document_id: str | None      # the document the artifact belongs to
    run_id: str | None           # the run that produced the artifact
    source_document_id: str | None  # the source document chunked
    source_run_id: str | None       # the source run chunked

    def belongs_to(
        self,
        *,
        active_document_id: str | None,
        active_run_id: str | None,
    ) -> bool:
        """Does this candidate belong to the supplied active scope?

        Document check uses the FIRST non-None of
        ``source_document_id`` then ``document_id`` — chunked
        artifacts (graph_json, enriched.*) usually carry only
        ``source_document_id``; raw chunks may carry both."""
        if active_document_id is not None:
            doc = self.source_document_id or self.document_id
            if doc is None:
                return False
            if doc != active_document_id:
                return False
        if active_run_id is not None:
            run = self.source_run_id or self.run_id
            if run is None:
                return False
            if run != active_run_id:
                return False
        return True

    def has_any_scope(self) -> bool:
        """True iff at least one of the four scope fields is set.
        ``belongs_to`` rejects scope-less candidates; this is
        the read-side test ``annotate_scope`` returns to callers
        that want to log the ``no_scope_metadata`` case."""
        return any(
            v is not None for v in (
                self.document_id, self.run_id,
                self.source_document_id, self.source_run_id,
            )
        )


class ScopeViolation(Exception):
    """Raised by callers (not by this module) when they need to
    surface a scope failure as an exception rather than a drop.

    The default filter LOGS + DROPS; some test surfaces want a
    hard fail and they can raise this from their post-filter
    callback. Kept here so consumers don't need a separate
    exception type."""


def annotate_scope(
    candidate: Any,
) -> CandidateScope:
    """Extract scope metadata from a candidate in any of the
    shapes the retrieval pipeline produces.

    Accepts a ``SearchHit``, an ``ArtifactRecord``, a rerank
    payload dict, a graph node, or the
    ``CandidateDiagnostic`` dataclass — uses duck-typed
    ``getattr`` / ``dict.get`` so we don't take a hard import.

    Specific rules:

      * ``source_document_id`` and ``source_run_id`` come from
        the artifact's ``metadata`` dict (canonical location for
        chunk / enriched provenance) OR the top-level
        ``source_document_ids`` list when it has exactly ONE
        entry. Ambiguous (multi-source) chunks deliberately
        return ``None`` — strict filter rejects them.
      * ``document_id`` and ``run_id`` come from the top-level
        attribute first, then the metadata dict.
    """
    getter = _make_getter(candidate)
    meta = getter("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}

    artifact_id = str(getter("artifact_id") or "")
    artifact_type = getter("artifact_type") or getter("kind")

    document_id = getter("document_id") or meta.get("document_id")
    run_id = getter("run_id") or meta.get("run_id")

    source_doc = meta.get("source_document_id")
    if source_doc is None:
        ids = getter("source_document_ids")
        if isinstance(ids, (list, tuple)) and len(ids) == 1:
            source_doc = str(ids[0])

    source_run = meta.get("source_run_id")

    return CandidateScope(
        artifact_id=artifact_id,
        artifact_type=str(artifact_type) if artifact_type else None,
        document_id=str(document_id) if document_id else None,
        run_id=str(run_id) if run_id else None,
        source_document_id=str(source_doc) if source_doc else None,
        source_run_id=str(source_run) if source_run else None,
    )


def enforce_active_scope(
    candidates: Iterable[Any],
    *,
    active_document_id: str | None,
    active_run_id: str | None,
    diagnostics: "RetrievalDiagnostics | None" = None,
) -> tuple[list[Any], list[CandidateScope]]:
    """Filter ``candidates`` to those belonging to the active
    scope. Returns ``(admitted_candidates, all_scopes)``.

    ``all_scopes`` includes BOTH admitted and rejected scopes (in
    input order) so callers that need the scope annotations for
    downstream rerank don't have to re-call ``annotate_scope``.

    Drops are recorded on ``diagnostics`` (when supplied) with
    the appropriate ``DropReason``:

      * ``WRONG_DOCUMENT``     — scope present but document_id mismatch
      * ``WRONG_RUN``          — document matched, run_id mismatch
      * ``NO_SCOPE_METADATA``  — candidate carries no scope IDs

    No active scope (both args None) admits everything. Each
    candidate's scope is still annotated and returned so callers
    can stamp ``scope_status`` on diagnostics if they want.
    """
    from j1.retrieval.diagnostics import (
        CandidateDiagnostic, DropReason,
    )

    admitted: list[Any] = []
    scopes: list[CandidateScope] = []
    no_active_scope = (
        active_document_id is None and active_run_id is None
    )
    for cand in candidates:
        scope = annotate_scope(cand)
        scopes.append(scope)

        # No active scope → admit-all path (cross-document search).
        if no_active_scope:
            admitted.append(cand)
            continue

        # Reject candidates without any scope when strict
        # filtering is on. The audit shows ``no_scope_metadata``
        # — operators can then go fix the upstream artifact
        # registration that didn't stamp doc/run ids.
        if active_document_id is not None and not scope.has_any_scope():
            _record_drop(
                diagnostics, scope, cand, DropReason.NO_SCOPE_METADATA,
            )
            continue

        # Document mismatch.
        if active_document_id is not None:
            owning_doc = scope.source_document_id or scope.document_id
            if owning_doc is not None and owning_doc != active_document_id:
                _record_drop(
                    diagnostics, scope, cand, DropReason.WRONG_DOCUMENT,
                )
                continue
            if owning_doc is None:
                _record_drop(
                    diagnostics, scope, cand, DropReason.NO_SCOPE_METADATA,
                )
                continue

        # Run mismatch (document already passed).
        if active_run_id is not None:
            owning_run = scope.source_run_id or scope.run_id
            if owning_run is not None and owning_run != active_run_id:
                _record_drop(
                    diagnostics, scope, cand, DropReason.WRONG_RUN,
                )
                continue
            if owning_run is None:
                _record_drop(
                    diagnostics, scope, cand, DropReason.NO_SCOPE_METADATA,
                )
                continue

        admitted.append(cand)

    return admitted, scopes


def _record_drop(
    diagnostics: "RetrievalDiagnostics | None",
    scope: CandidateScope,
    cand: Any,
    reason: Any,
) -> None:
    if diagnostics is None:
        return
    from j1.retrieval.diagnostics import CandidateDiagnostic
    diag = CandidateDiagnostic.from_search_hit(cand)
    diag.scope_status = "out_of_scope" if scope.has_any_scope() else "unscoped"
    diagnostics.record_dropped(diag, reason=reason)


def _make_getter(hit):
    if isinstance(hit, dict):
        return hit.get
    return lambda name: getattr(hit, name, None)


__all__ = [
    "CandidateScope",
    "ScopeViolation",
    "annotate_scope",
    "enforce_active_scope",
]
