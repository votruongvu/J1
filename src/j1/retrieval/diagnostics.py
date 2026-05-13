"""Retrieval-quality diagnostic event stream.

The observed failure mode: relevant candidates ARE in the
retrieved list (BM25 / native found them) but never reach the
final evidence pack — they get dropped in scope filter / rerank /
dedup / budget cap / intent filter with no audit trail. This
module is the smallest patch that makes those drops explainable.

Event names (kept as constants — never inline strings):

    j1.retrieval.query.received
    j1.retrieval.scope.applied
    j1.retrieval.intent.selected
    j1.retrieval.candidates.retrieved
    j1.retrieval.candidates.reranked
    j1.retrieval.candidates.deduplicated
    j1.retrieval.evidence_pack.selected
    j1.retrieval.evidence_pack.dropped
    j1.retrieval.evidence_pack.finalized

Invariant the golden tests pin against:
For every ``artifact_id`` that appears in ``candidates.retrieved``
but NOT in the final pack, EXACTLY ONE ``evidence_pack.dropped``
event must carry that ``artifact_id`` with a non-null
``reason_dropped`` from the ``DropReason`` enum.

This is the observability surface only. Behavioural changes live
in ``scope``, ``intent_router``, ``boilerplate``,
``evidence_planner``, and ``quality_checks`` — each of which
calls into this module to record decisions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from j1.audit.recorder import AuditRecorder
    from j1.projects.context import ProjectContext

_log = logging.getLogger("j1.retrieval.diagnostics")


# ---- Stable event names ------------------------------------------

EVENT_QUERY_RECEIVED = "j1.retrieval.query.received"
EVENT_SCOPE_APPLIED = "j1.retrieval.scope.applied"
EVENT_INTENT_SELECTED = "j1.retrieval.intent.selected"
EVENT_CANDIDATES_RETRIEVED = "j1.retrieval.candidates.retrieved"
EVENT_CANDIDATES_RERANKED = "j1.retrieval.candidates.reranked"
EVENT_CANDIDATES_DEDUPED = "j1.retrieval.candidates.deduplicated"
EVENT_EVIDENCE_PACK_SELECTED = "j1.retrieval.evidence_pack.selected"
EVENT_EVIDENCE_PACK_DROPPED = "j1.retrieval.evidence_pack.dropped"
EVENT_EVIDENCE_PACK_FINALIZED = "j1.retrieval.evidence_pack.finalized"


class DropReason(StrEnum):
    """Stable codes for ``reason_dropped``. New reasons MUST be
    added here (not inline strings) so consumers can filter the
    audit stream deterministically."""

    WRONG_DOCUMENT = "wrong_document"          # outside active doc
    WRONG_RUN = "wrong_run"                    # outside active run
    NO_SCOPE_METADATA = "no_scope_metadata"    # candidate lacks doc/run id
    KIND_SKIPPED = "kind_skipped"              # type-allowlist filter
    BOILERPLATE = "boilerplate"                # legal/insurance/exhibit
    DEDUPED = "deduped"                        # prefix already seen
    BUDGET_EXHAUSTED = "budget_exhausted"      # blocks/char cap hit
    NO_BODY_TEXT = "no_body_text"              # body load returned empty
    LOW_SCORE = "low_score"                    # rerank cutoff
    NO_COVERAGE_GAIN = "no_coverage_gain"      # greedy coverage rejected
    INTENT_MISMATCH = "intent_mismatch"        # intent-specific reject
    AVOIDED_BY_PLAN = "avoided_by_plan"        # evidence plan demote
    OTHER = "other"


# ---- Candidate record --------------------------------------------


@dataclass
class CandidateDiagnostic:
    """Snapshot of one retrieval candidate at a pipeline checkpoint.

    Used by:
      * ``record_candidates_retrieved`` (after raw retrieval)
      * ``record_candidates_reranked`` (after scoring)
      * ``record_selected`` (chosen for the pack)
      * ``record_dropped`` (excluded with a reason)

    Optional fields stay ``None`` when the producing stage doesn't
    have the value (e.g. ``rerank_score`` is None during the
    retrieved event, populated by the reranked event)."""

    artifact_id: str
    artifact_type: str | None = None
    document_id: str | None = None
    run_id: str | None = None
    source_document_id: str | None = None
    source_run_id: str | None = None
    chunk_id: str | None = None
    page_range: str | None = None
    section_path: str | None = None
    heading: str | None = None
    score: float | None = None
    rerank_score: float | None = None
    final_score: float | None = None
    token_estimate: int | None = None
    reason_selected: str | None = None
    reason_dropped: str | None = None
    scope_status: str | None = None  # "active" | "out_of_scope" | "unscoped"

    def to_payload(self) -> dict[str, Any]:
        # Always emit every field — dashboards expect a stable
        # schema so absent values are explicit nulls, not missing
        # keys (which JSON-projecting tools render inconsistently).
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "document_id": self.document_id,
            "run_id": self.run_id,
            "source_document_id": self.source_document_id,
            "source_run_id": self.source_run_id,
            "chunk_id": self.chunk_id,
            "page_range": self.page_range,
            "section_path": self.section_path,
            "heading": self.heading,
            "score": self.score,
            "rerank_score": self.rerank_score,
            "final_score": self.final_score,
            "token_estimate": self.token_estimate,
            "reason_selected": self.reason_selected,
            "reason_dropped": self.reason_dropped,
            "scope_status": self.scope_status,
        }

    @classmethod
    def from_search_hit(cls, hit: Any) -> "CandidateDiagnostic":
        """Snapshot a ``SearchHit``-shape (object or dict).

        Tolerant: we don't import the actual class because BM25
        results, LightRAG native results, graph results, and
        rerank payloads all have overlapping but distinct shapes.
        Uses duck-typing via ``getter(name)``."""
        getter = _make_getter(hit)
        meta = getter("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        source_doc_ids = getter("source_document_ids")
        source_doc = None
        if isinstance(source_doc_ids, (list, tuple)) and len(source_doc_ids) == 1:
            source_doc = str(source_doc_ids[0])
        section = (
            getter("source_location")
            or meta.get("section_path")
            or meta.get("section")
        )
        heading = meta.get("heading") or getter("title")
        return cls(
            artifact_id=str(getter("artifact_id") or ""),
            artifact_type=getter("artifact_type") or getter("kind"),
            document_id=meta.get("document_id"),
            run_id=getter("run_id") or meta.get("run_id"),
            source_document_id=source_doc or meta.get(
                "source_document_id",
            ),
            source_run_id=meta.get("source_run_id"),
            chunk_id=getter("chunk_id") or meta.get("chunk_id"),
            page_range=meta.get("page_range"),
            section_path=section,
            heading=str(heading) if heading else None,
            score=_safe_float(getter("score")),
            rerank_score=_safe_float(getter("rerank_score")),
            final_score=_safe_float(getter("final_score")),
            token_estimate=_safe_int(meta.get("token_estimate")),
        )


def _make_getter(hit):
    if isinstance(hit, dict):
        return hit.get
    return lambda name: getattr(hit, name, None)


def _safe_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ---- Snapshot ----------------------------------------------------


@dataclass
class _RetrievalSnapshot:
    """In-memory record of one query's pipeline traversal.

    Built up incrementally by the collector; accessible via
    ``RetrievalDiagnostics.snapshot()`` so tests and the manual
    query console can inspect the decisions without scraping the
    audit log."""

    query: str
    run_id: str | None
    document_id: str | None
    scope_summary: dict[str, Any] = field(default_factory=dict)
    intent: str | None = None
    intent_signals: dict[str, Any] = field(default_factory=dict)
    retrieved: list[CandidateDiagnostic] = field(default_factory=list)
    reranked: list[CandidateDiagnostic] = field(default_factory=list)
    deduped: list[CandidateDiagnostic] = field(default_factory=list)
    selected: list[CandidateDiagnostic] = field(default_factory=list)
    dropped: list[CandidateDiagnostic] = field(default_factory=list)
    finalized_summary: dict[str, Any] = field(default_factory=dict)


class RetrievalDiagnostics:
    """Per-query collector + audit emitter.

    Construct ONE per query. Mutate via ``record_*``. Call
    ``snapshot()`` at the end to read the aggregate.

    Audit emit is best-effort: each method catches its own
    failures + logs at WARNING. Instrumentation never breaks the
    retrieval call.

    Typical usage from the retrieval entry point:

        diag = RetrievalDiagnostics(
            audit=audit_recorder, ctx=ctx,
            run_id=run.run_id, document_id=run.document_id,
            query=q,
        )
        diag.record_query_received(...)
        diag.record_scope_applied(active_run_id=...,
                                   active_document_id=...,
                                   admitted=42, rejected=7)
        diag.record_intent_selected(intent.value, signals=...)
        diag.record_candidates_retrieved([...])
        diag.record_candidates_reranked([...])
        diag.record_candidates_deduped(survivors, removed=...)
        for c in selected:
            diag.record_selected(c, reason="...")
        for c in dropped:
            diag.record_dropped(c, reason=DropReason.BOILERPLATE)
        diag.record_evidence_pack_finalized(
            pack_size=5, fallback_triggered=False, checks_passed=True,
        )
    """

    def __init__(
        self,
        *,
        audit: "AuditRecorder | None" = None,
        ctx: "ProjectContext | None" = None,
        run_id: str | None,
        document_id: str | None,
        query: str,
    ) -> None:
        self._audit = audit
        self._ctx = ctx
        self._snapshot = _RetrievalSnapshot(
            query=query, run_id=run_id, document_id=document_id,
        )

    # ---- Recording methods --------------------------------------

    def record_query_received(
        self,
        *,
        max_results: int | None = None,
        artifact_types: list[str] | None = None,
        scope_kind: str | None = None,
    ) -> None:
        self._emit(
            EVENT_QUERY_RECEIVED,
            payload={
                "query_chars": len(self._snapshot.query),
                "max_results": max_results,
                "artifact_types": artifact_types or [],
                "scope_kind": scope_kind,
            },
        )

    def record_scope_applied(
        self,
        *,
        active_run_id: str | None,
        active_document_id: str | None,
        admitted: int,
        rejected: int,
        scope_kind: str,
    ) -> None:
        """Always fires after the scope filter — even when the
        admitted set is the whole retrieved list (rejected=0).
        Operators eyeballing the log expect to see this for
        every query so they can confirm scope WAS checked."""
        summary = {
            "active_run_id": active_run_id,
            "active_document_id": active_document_id,
            "admitted": admitted,
            "rejected": rejected,
            "scope_kind": scope_kind,
        }
        self._snapshot.scope_summary = dict(summary)
        self._emit(EVENT_SCOPE_APPLIED, payload=summary)

    def record_intent_selected(
        self,
        intent: str,
        *,
        signals: dict[str, Any] | None = None,
    ) -> None:
        self._snapshot.intent = intent
        if signals:
            self._snapshot.intent_signals = dict(signals)
        self._emit(
            EVENT_INTENT_SELECTED,
            payload={
                "intent": intent,
                "signals": dict(signals or {}),
            },
        )

    def record_candidates_retrieved(
        self,
        candidates: list[CandidateDiagnostic],
        *,
        source: str | None = None,
    ) -> None:
        self._snapshot.retrieved = list(candidates)
        self._emit(
            EVENT_CANDIDATES_RETRIEVED,
            payload={
                "count": len(candidates),
                "source": source,
                "candidates": [c.to_payload() for c in candidates],
            },
        )

    def record_candidates_reranked(
        self,
        candidates: list[CandidateDiagnostic],
    ) -> None:
        self._snapshot.reranked = list(candidates)
        self._emit(
            EVENT_CANDIDATES_RERANKED,
            payload={
                "count": len(candidates),
                "candidates": [c.to_payload() for c in candidates],
            },
        )

    def record_candidates_deduped(
        self,
        survivors: list[CandidateDiagnostic],
        *,
        removed: list[CandidateDiagnostic] | None = None,
    ) -> None:
        self._snapshot.deduped = list(survivors)
        # Removed candidates emit as ``dropped`` events with
        # reason=DEDUPED so the dropped-stream is the single
        # union of every reject path.
        for c in removed or []:
            c.reason_dropped = c.reason_dropped or DropReason.DEDUPED.value
            self._snapshot.dropped.append(c)
            self._emit(EVENT_EVIDENCE_PACK_DROPPED, payload=c.to_payload())
        self._emit(
            EVENT_CANDIDATES_DEDUPED,
            payload={
                "count": len(survivors),
                "removed_count": len(removed or []),
            },
        )

    def record_selected(
        self,
        candidate: CandidateDiagnostic,
        *,
        reason: str = "rerank_top",
    ) -> None:
        candidate.reason_selected = reason
        self._snapshot.selected.append(candidate)
        self._emit(EVENT_EVIDENCE_PACK_SELECTED, payload=candidate.to_payload())

    def record_dropped(
        self,
        candidate: CandidateDiagnostic,
        *,
        reason: "str | DropReason",
    ) -> None:
        """Critical method: every candidate that left the
        retrieved-list without making it to the evidence pack
        MUST flow through here so the audit log explains why."""
        if hasattr(reason, "value"):
            reason = reason.value  # type: ignore[union-attr]
        candidate.reason_dropped = str(reason)
        self._snapshot.dropped.append(candidate)
        self._emit(EVENT_EVIDENCE_PACK_DROPPED, payload=candidate.to_payload())

    def record_evidence_pack_finalized(
        self,
        *,
        pack_size: int,
        fallback_triggered: bool = False,
        fallback_succeeded: bool | None = None,
        checks_passed: bool = True,
        check_failures: list[str] | None = None,
        check_failures_before_fallback: list[str] | None = None,
    ) -> None:
        """Emit the terminal ``evidence_pack.finalized`` event.

        ``fallback_succeeded`` / ``check_failures_before_fallback``
        are populated when the caller ran a one-pass fallback after
        the initial ``check_pack`` failed for recoverable reasons.
        ``None`` for ``fallback_succeeded`` means no fallback ran.
        """
        summary = {
            "pack_size": pack_size,
            "fallback_triggered": fallback_triggered,
            "fallback_succeeded": fallback_succeeded,
            "checks_passed": checks_passed,
            "check_failures": list(check_failures or []),
            "check_failures_before_fallback": list(
                check_failures_before_fallback or [],
            ),
            "drop_counts": self.dropped_reasons_summary(),
        }
        self._snapshot.finalized_summary = dict(summary)
        self._emit(EVENT_EVIDENCE_PACK_FINALIZED, payload=summary)

    # ---- Read surface -------------------------------------------

    def snapshot(self) -> _RetrievalSnapshot:
        return self._snapshot

    @property
    def selected(self) -> list[CandidateDiagnostic]:
        return list(self._snapshot.selected)

    @property
    def dropped(self) -> list[CandidateDiagnostic]:
        return list(self._snapshot.dropped)

    def dropped_reasons_summary(self) -> dict[str, int]:
        """Count of each drop reason across the snapshot. Used by
        the finalize event and by the test assertions."""
        out: dict[str, int] = {}
        for c in self._snapshot.dropped:
            key = c.reason_dropped or "unknown"
            out[key] = out.get(key, 0) + 1
        return out

    # ---- Internals ----------------------------------------------

    def _emit(self, action: str, *, payload: dict[str, Any]) -> None:
        if self._audit is None or self._ctx is None:
            return
        envelope = {
            "run_id": self._snapshot.run_id,
            "document_id": self._snapshot.document_id,
            "query_preview": _safe_preview(self._snapshot.query, 80),
            **payload,
        }
        try:
            self._audit.record(
                self._ctx,
                actor="system",
                action=action,
                target_kind="retrieval_query",
                target_id=self._snapshot.run_id or "no-run",
                payload=envelope,
            )
        except Exception:  # noqa: BLE001
            _log.warning(
                "retrieval diagnostics: audit emit failed for %s",
                action, exc_info=True,
            )


def _safe_preview(value: str | None, limit: int = 80) -> str | None:
    if value is None:
        return None
    s = str(value)
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


__all__ = [
    "CandidateDiagnostic",
    "DropReason",
    "EVENT_CANDIDATES_DEDUPED",
    "EVENT_CANDIDATES_RERANKED",
    "EVENT_CANDIDATES_RETRIEVED",
    "EVENT_EVIDENCE_PACK_DROPPED",
    "EVENT_EVIDENCE_PACK_FINALIZED",
    "EVENT_EVIDENCE_PACK_SELECTED",
    "EVENT_INTENT_SELECTED",
    "EVENT_QUERY_RECEIVED",
    "EVENT_SCOPE_APPLIED",
    "RetrievalDiagnostics",
]
