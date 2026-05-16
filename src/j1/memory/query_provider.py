"""Phase 4 — `KnowledgeMemoryContextProvider`.

Opt-in memory-aware query path. Given the active snapshot's
persistent `knowledge_memory` artifact + the user's query string,
returns a structured context the orchestrator folds into:

  * **expansion terms** — vocab pulled from matching entries (alias
    canonical names, requirement / risk / finding short headlines,
    domain pack retrieval hints). Fed into the existing query
    expansion path used by the augmentation provider.
  * **derived candidates** — matching entries with `source_refs`
    that point at base compile evidence. The orchestrator can use
    these to ground answers; Phase 4 surfaces them as diagnostics
    + as additional expansion hints but does NOT short-circuit the
    base evidence pipeline.
  * **summary context** — high-level entries (graph_summary,
    document_summary, quality_summary) used as ranking signal /
    contextual hint, never cited unless source refs exist.

Hard contract — every Phase 4 behaviour the orchestrator depends
on lives here, NOT in the orchestrator:

  * **Never raises** into the orchestrator. Bad payloads / missing
    artifacts / registry errors return an empty result with a
    diagnostic warning. Query proceeds with existing fallback
    behaviour.
  * **Deterministic, no LLM**. Entry selection uses substring +
    keyword matching only. The provider never adds a pre-retrieval
    LLM call (Phase 4 hard constraint).
  * **Snapshot-isolated**. Reads only the active snapshot's
    memory artifact for the given document — superseded rows are
    filtered out via `j1.memory.status._select_active_memory`.
  * **Capped**. The maximum number of selected entries is
    controlled by `KnowledgeMemoryQuerySettings.max_entries`;
    truncation surfaces a warning so the diagnostic block is
    accurate.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from j1.memory.knowledge_memory import (
    KnowledgeMemoryEntry,
    KnowledgeMemoryPayload,
    MEMORY_ENTRY_TYPE_ALIAS,
    MEMORY_ENTRY_TYPE_DOCUMENT_SUMMARY,
    MEMORY_ENTRY_TYPE_DOMAIN_INSIGHT,
    MEMORY_ENTRY_TYPE_GRAPH_SUMMARY,
    MEMORY_ENTRY_TYPE_QUALITY_SUMMARY,
    MEMORY_ENTRY_TYPE_RETRIEVAL_HINT,
    MEMORY_ENTRY_TYPE_RISK,
    MEMORY_ENTRY_TYPE_REQUIREMENT,
    MEMORY_ENTRY_TYPE_SECTION,
    MEMORY_ENTRY_TYPE_TABLE_ROW,
    MEMORY_ENTRY_TYPE_TABLE_SUMMARY,
    MEMORY_ENTRY_TYPE_TERMINOLOGY,
    MEMORY_ENTRY_TYPE_VALIDATION_CHECK,
    MEMORY_ENTRY_TYPE_VISUAL_SUMMARY,
)
from j1.memory.query_settings import KnowledgeMemoryQuerySettings
from j1.memory.status import _select_active_memory


_log = logging.getLogger(__name__)


# ---- Use-mode vocabulary ---------------------------------------


# Pinned strings — surface on the query diagnostic + dashboards.
# Add values, don't rename.
USE_MODE_EXPANSION_ONLY = "expansion_only"
USE_MODE_DERIVED_CANDIDATE = "derived_candidate"
USE_MODE_SUMMARY_CONTEXT = "summary_context"


# Entry types that ALWAYS map to one use mode regardless of source-
# ref shape. Per-type rules let the matcher skip per-entry
# inspection for the common cases.
_EXPANSION_ONLY_TYPES: frozenset[str] = frozenset({
    MEMORY_ENTRY_TYPE_ALIAS,
    MEMORY_ENTRY_TYPE_TERMINOLOGY,
    MEMORY_ENTRY_TYPE_RETRIEVAL_HINT,
})
_SUMMARY_CONTEXT_TYPES: frozenset[str] = frozenset({
    MEMORY_ENTRY_TYPE_DOCUMENT_SUMMARY,
    MEMORY_ENTRY_TYPE_GRAPH_SUMMARY,
    MEMORY_ENTRY_TYPE_QUALITY_SUMMARY,
    MEMORY_ENTRY_TYPE_SECTION,
})
# Entry types that USUALLY have source refs — derived-candidate
# is the default mode; falls back to expansion_only when no
# refs are present so we don't pretend to ground unrefed entries.
_DERIVED_CANDIDATE_TYPES: frozenset[str] = frozenset({
    MEMORY_ENTRY_TYPE_REQUIREMENT,
    MEMORY_ENTRY_TYPE_RISK,
    MEMORY_ENTRY_TYPE_VALIDATION_CHECK,
    MEMORY_ENTRY_TYPE_TABLE_SUMMARY,
    MEMORY_ENTRY_TYPE_TABLE_ROW,
    MEMORY_ENTRY_TYPE_VISUAL_SUMMARY,
    MEMORY_ENTRY_TYPE_DOMAIN_INSIGHT,
})


# ---- Status vocabulary for the trace block --------------------


STATUS_USED = "used"
STATUS_NOT_AVAILABLE = "not_available"
STATUS_FALLBACK = "fallback"
STATUS_LOADED_NO_MATCH = "loaded_no_match"
STATUS_FAILED = "failed"
STATUS_DISABLED = "disabled"


# ---- Scope vocabulary -----------------------------------------


# Phase 5A patch (2026-05-16): the provider now supports both
# document-active and project-active scopes. Surface on the
# diagnostic block so the FE / dashboards can distinguish per-
# document memory consults from project-wide aggregations.
SCOPE_DOCUMENT_ACTIVE = "document_active"
SCOPE_PROJECT_ACTIVE = "project_active"


# ---- Warning codes --------------------------------------------


WARNING_MEMORY_PAYLOAD_MALFORMED = "memory_payload_malformed"
WARNING_MEMORY_NOT_READABLE = "memory_artifact_not_readable"
WARNING_TRUNCATED = "selection_truncated"
WARNING_MEMORY_NO_ENTRIES = "memory_artifact_has_no_entries"
WARNING_MEMORY_LOADED_NO_MATCH = "loaded_no_matching_entries"
# Phase 5A patch — project-active scope warnings.
WARNING_PROJECT_MEMORY_PARTIAL = "project_memory_partial"
WARNING_PROJECT_MEMORY_DOCUMENT_CAP_APPLIED = (
    "project_memory_document_cap_applied"
)
WARNING_PROJECT_MEMORY_ARTIFACT_CAP_APPLIED = (
    "project_memory_artifact_cap_applied"
)
WARNING_NO_PROJECT_MEMORY_ARTIFACTS = "no_project_memory_artifacts"


# Intent keyword → memory-type table. Substring match on
# normalised query lowercases the query and looks for these
# heuristics. Add tokens, don't rename — the table is small on
# purpose so the matcher stays predictable.
_INTENT_KEYWORDS: dict[str, frozenset[str]] = {
    MEMORY_ENTRY_TYPE_RISK: frozenset({
        "risk", "issue", "concern", "hazard", "danger",
    }),
    MEMORY_ENTRY_TYPE_REQUIREMENT: frozenset({
        "requirement", "shall", "must", "compliance",
        "specification", "compliant",
    }),
    MEMORY_ENTRY_TYPE_VALIDATION_CHECK: frozenset({
        "check", "validation", "missing", "inconsistency",
        "discrepancy",
    }),
    MEMORY_ENTRY_TYPE_TABLE_SUMMARY: frozenset({
        "boq", "quantity", "rate", "amount", "table",
        "cost", "price", "schedule",
    }),
    MEMORY_ENTRY_TYPE_VISUAL_SUMMARY: frozenset({
        "drawing", "sheet", "revision", "figure", "image",
        "blueprint", "diagram",
    }),
}


# ---- Selected entry wrapper -----------------------------------


@dataclass(frozen=True)
class SelectedMemoryEntry:
    """One memory entry plus its classified use mode + the match
    signal that surfaced it. Diagnostic-friendly; the orchestrator
    can inspect both the entry and the reason it was picked.

    Phase 5A patch: optional lineage fields point back at the
    memory artifact + its `(document_id, snapshot_id)` pair. None
    for document-active selections (the lineage is implicit in
    the request's `document_id`); populated for project-active
    selections so the diagnostic block can group entries by
    document. Backward-compatible — defaults preserve Phase 5A
    constructor calls that don't pass them.
    """

    entry: KnowledgeMemoryEntry
    use_mode: str
    match_reason: str  # short operator-readable reason
    document_id: str | None = None
    snapshot_id: str | None = None
    artifact_id: str | None = None

    def has_source_refs(self) -> bool:
        return bool(self.entry.source_refs)


# ---- Result --------------------------------------------------


@dataclass(frozen=True)
class KnowledgeMemoryQueryContext:
    """Structured result the provider returns to the orchestrator.

    Surface for downstream code:

      * `status` — one of `STATUS_*` strings.
      * `selected_entries` — capped list of `SelectedMemoryEntry`.
      * `expansion_terms` — alias / retrieval-hint / requirement-
        title strings the orchestrator can fold into the existing
        query expansion pipeline. Deduplicated, capped.
      * `selected_entry_types` — set of memory-entry-type strings
        across `selected_entries`, sorted.
      * `resolved_source_ref_count` — number of selected entries
        that carry at least one source ref (i.e. eligible for
        source-grounded answer evidence).
      * `available` — whether the memory artifact was located at
        all.
      * `artifact_id` / `entry_count` — provenance for the trace.
      * `warnings` — stable warning codes.

    The result is intentionally read-only / hashable so the
    orchestrator can attach it to the trace without copying."""

    status: str = STATUS_NOT_AVAILABLE
    available: bool = False
    artifact_id: str | None = None
    entry_count: int = 0
    selected_entries: tuple[SelectedMemoryEntry, ...] = ()
    expansion_terms: tuple[str, ...] = ()
    selected_entry_types: tuple[str, ...] = ()
    resolved_source_ref_count: int = 0
    warnings: tuple[str, ...] = ()
    # Phase 5A patch — project-active scope diagnostics. Document
    # scope leaves these at defaults; project scope populates them
    # so dashboards / FE rendering can distinguish per-document
    # consults from project-wide aggregations.
    scope: str = SCOPE_DOCUMENT_ACTIVE
    project_id: str | None = None
    document_count: int = 0
    memory_artifact_count: int = 0

    def to_payload(self) -> dict[str, Any]:
        """Wire shape for the `QueryTrace.knowledge_memory` field.
        Snake_case at the dataclass level; the trace projects to
        the FE's preferred casing at its boundary."""
        return {
            "status": self.status,
            "scope": self.scope,
            "project_id": self.project_id,
            "available": self.available,
            "artifact_id": self.artifact_id,
            "entry_count": self.entry_count,
            "document_count": self.document_count,
            "memory_artifact_count": self.memory_artifact_count,
            "selected_entry_count": len(self.selected_entries),
            "selected_entry_types": list(self.selected_entry_types),
            "expansion_terms": list(self.expansion_terms),
            "resolved_source_ref_count": self.resolved_source_ref_count,
            "warnings": list(self.warnings),
        }


# ---- Provider -------------------------------------------------


class KnowledgeMemoryContextProvider:
    """Loads the active `knowledge_memory` artifact for a query
    and selects relevant entries.

    Dependencies — all required EXCEPT ``workspace``:

      * ``source_lookup`` — `SourceLookupService` / facade
        equivalent. Used to read the document record + its
        `active_snapshot_id` for `document_active` scope queries.
      * ``artifact_registry`` — `ArtifactRegistry`. Used to list
        + read the `knowledge_memory` artifact metadata.
      * ``workspace`` — `WorkspaceResolver`, optional. Used to
        read artifact JSON bytes off disk when the metadata
        doesn't carry the payload inline. Without it the provider
        falls back to the inline-payload path (test fakes); when
        neither is available the provider returns
        ``status=not_available``.
      * ``settings`` — `KnowledgeMemoryQuerySettings`. The
        provider re-reads on each call (cheap; tests can mutate
        env without rebuilding the provider).

    Phase 4 keeps the matcher deterministic — see module
    docstring for the rationale.
    """

    def __init__(
        self,
        *,
        source_lookup,
        artifact_registry,
        workspace=None,
    ) -> None:
        self._source_lookup = source_lookup
        self._artifacts = artifact_registry
        self._workspace = workspace

    # ---- Public API ----------------------------------------------

    def context_for_query(
        self,
        *,
        ctx,
        question: str,
        document_id: str | None,
        settings: KnowledgeMemoryQuerySettings,
        eligible_snapshot_pairs: "frozenset[tuple[str, str]] | None" = None,
    ) -> KnowledgeMemoryQueryContext:
        """Return a `KnowledgeMemoryQueryContext` for the query.

        Never raises into the caller. Internal errors get caught,
        logged, and surfaced as `status=failed` + a warning code so
        the orchestrator can proceed with fallback behaviour.

        Scope dispatch (Phase 5A patch — 2026-05-16):

          * ``document_id`` populated → ``document_active`` path
            (Phase 4 behaviour). The provider resolves the active
            snapshot via ``source_lookup`` + reads the artifact.
          * ``document_id`` None → ``project_active`` path (new).
            The provider walks the artifact registry for active
            ``knowledge_memory`` rows in the project, optionally
            filtered to ``eligible_snapshot_pairs`` when the caller
            already pre-resolved them. Caps applied per settings.

        Other scopes (snapshot_explicit / run / document_run) reach
        the project path through ``eligible_snapshot_pairs`` —
        the caller resolves the pairs upstream and we treat the
        union the same way. Phase 5A patch keeps the focus on
        ``project_active``; the broader scopes inherit naturally.
        """
        if not settings.enabled:
            return KnowledgeMemoryQueryContext(status=STATUS_DISABLED)

        try:
            if document_id:
                return self._context_for_document(
                    ctx=ctx, question=question,
                    document_id=document_id, settings=settings,
                )
            return self._context_for_project(
                ctx=ctx, question=question,
                eligible_snapshot_pairs=eligible_snapshot_pairs,
                settings=settings,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort hook
            _log.warning(
                "knowledge_memory provider raised; falling back",
                exc_info=True,
            )
            return KnowledgeMemoryQueryContext(
                status=STATUS_FAILED,
                warnings=(f"provider_error:{type(exc).__name__}",),
            )

    # ---- Internals -----------------------------------------------

    def _context_for_document(
        self,
        *,
        ctx,
        question: str,
        document_id: str,
        settings: KnowledgeMemoryQuerySettings,
    ) -> KnowledgeMemoryQueryContext:
        active_snapshot_id = self._resolve_active_snapshot(
            ctx=ctx, document_id=document_id,
        )
        if not active_snapshot_id:
            return KnowledgeMemoryQueryContext(
                status=STATUS_NOT_AVAILABLE,
                scope=SCOPE_DOCUMENT_ACTIVE,
                project_id=getattr(ctx, "project_id", None),
            )

        record = self._find_active_memory_record(
            ctx=ctx,
            document_id=document_id,
            snapshot_id=active_snapshot_id,
        )
        if record is None:
            return KnowledgeMemoryQueryContext(
                status=STATUS_NOT_AVAILABLE,
                scope=SCOPE_DOCUMENT_ACTIVE,
                project_id=getattr(ctx, "project_id", None),
            )

        warnings: list[str] = []
        payload = self._read_memory_payload(ctx, record, warnings)
        if payload is None:
            return KnowledgeMemoryQueryContext(
                status=STATUS_FAILED,
                scope=SCOPE_DOCUMENT_ACTIVE,
                project_id=getattr(ctx, "project_id", None),
                available=True,
                artifact_id=getattr(record, "artifact_id", None),
                document_count=1,
                memory_artifact_count=1,
                warnings=tuple(warnings),
            )

        # Empty memory artifact — nothing to do.
        if not payload.entries:
            warnings.append(WARNING_MEMORY_NO_ENTRIES)
            return KnowledgeMemoryQueryContext(
                status=STATUS_LOADED_NO_MATCH,
                scope=SCOPE_DOCUMENT_ACTIVE,
                project_id=getattr(ctx, "project_id", None),
                available=True,
                artifact_id=getattr(record, "artifact_id", None),
                entry_count=0,
                document_count=1,
                memory_artifact_count=1,
                warnings=tuple(warnings),
            )

        selected, truncated = self._select_entries(
            entries=payload.entries,
            question=question,
            max_entries=settings.max_entries,
            artifact_id=getattr(record, "artifact_id", None),
            document_id=document_id,
            snapshot_id=active_snapshot_id,
        )
        if truncated:
            warnings.append(WARNING_TRUNCATED)

        if not selected:
            warnings.append(WARNING_MEMORY_LOADED_NO_MATCH)
            return KnowledgeMemoryQueryContext(
                status=STATUS_LOADED_NO_MATCH,
                scope=SCOPE_DOCUMENT_ACTIVE,
                project_id=getattr(ctx, "project_id", None),
                available=True,
                artifact_id=getattr(record, "artifact_id", None),
                entry_count=len(payload.entries),
                document_count=1,
                memory_artifact_count=1,
                warnings=tuple(warnings),
            )

        expansion_terms = self._expansion_terms_for(selected)
        types = tuple(sorted({s.entry.memory_type for s in selected}))
        resolved_refs = sum(1 for s in selected if s.has_source_refs())

        return KnowledgeMemoryQueryContext(
            status=STATUS_USED,
            scope=SCOPE_DOCUMENT_ACTIVE,
            project_id=getattr(ctx, "project_id", None),
            available=True,
            artifact_id=getattr(record, "artifact_id", None),
            entry_count=len(payload.entries),
            document_count=1,
            memory_artifact_count=1,
            selected_entries=tuple(selected),
            expansion_terms=expansion_terms,
            selected_entry_types=types,
            resolved_source_ref_count=resolved_refs,
            warnings=tuple(warnings),
        )

    def _context_for_project(
        self,
        *,
        ctx,
        question: str,
        eligible_snapshot_pairs: "frozenset[tuple[str, str]] | None",
        settings: KnowledgeMemoryQuerySettings,
    ) -> KnowledgeMemoryQueryContext:
        """Phase 5A patch — project-active scope.

        Walks the project's `knowledge_memory` artifacts via the
        artifact registry, filters to active rows, optionally
        narrows to a caller-supplied `eligible_snapshot_pairs`
        allowlist, applies document + artifact caps, loads each
        payload, runs the matcher on the combined entry pool, and
        returns the same `KnowledgeMemoryQueryContext` shape the
        document path returns.

        Caps:
          * `max_project_documents` — limits the unique
            `(document_id, snapshot_id)` pair set inspected.
          * `max_project_artifacts` — defence-in-depth cap on the
            number of memory artifacts actually loaded.
          * `max_entries` — same per-query cap as document scope.

        Diagnostics:
          * `document_count` — unique documents whose memory was
            loaded.
          * `memory_artifact_count` — total artifacts loaded.
          * `project_memory_partial` warning when documents in the
            eligibility set lacked memory.
          * `project_memory_document_cap_applied` /
            `project_memory_artifact_cap_applied` when caps fired.
        """
        warnings: list[str] = []
        project_id = getattr(ctx, "project_id", None)

        list_artifacts = getattr(self._artifacts, "list_artifacts", None)
        if not callable(list_artifacts):
            return KnowledgeMemoryQueryContext(
                status=STATUS_NOT_AVAILABLE,
                scope=SCOPE_PROJECT_ACTIVE,
                project_id=project_id,
            )
        try:
            records = list_artifacts(ctx, kind="knowledge_memory")
        except TypeError:
            try:
                records = [
                    r for r in list_artifacts(ctx)
                    if getattr(r, "kind", None) == "knowledge_memory"
                ]
            except Exception:  # noqa: BLE001
                return KnowledgeMemoryQueryContext(
                    status=STATUS_NOT_AVAILABLE,
                    scope=SCOPE_PROJECT_ACTIVE,
                    project_id=project_id,
                )
        except Exception:  # noqa: BLE001
            return KnowledgeMemoryQueryContext(
                status=STATUS_NOT_AVAILABLE,
                scope=SCOPE_PROJECT_ACTIVE,
                project_id=project_id,
            )

        # Filter to active rows; record per-pair lineage for the
        # cap-and-load loop. Pair = (document_id, snapshot_id).
        active_records_by_pair: dict[
            tuple[str, str], Any,
        ] = {}
        for record in records:
            metadata = dict(getattr(record, "metadata", None) or {})
            state = metadata.get("search_state") or "active"
            if state != "active":
                continue
            doc_id = _str_or_none_meta(metadata, "document_id")
            snap_id = _str_or_none_meta(metadata, "snapshot_id")
            if not doc_id or not snap_id:
                # Defensive: a memory artifact without lineage
                # metadata can't be matched against a pair set;
                # skip rather than risk cross-document leakage.
                continue
            pair = (doc_id, snap_id)
            if eligible_snapshot_pairs is not None and pair not in eligible_snapshot_pairs:
                continue
            # Multiple active rows for the same pair → defensive
            # skip; Phase 2 supersede sweep should prevent this.
            if pair in active_records_by_pair:
                continue
            active_records_by_pair[pair] = record

        # Track how many eligibility pairs are missing memory to
        # surface the partial-coverage warning.
        eligible_count = (
            len(eligible_snapshot_pairs)
            if eligible_snapshot_pairs is not None
            else len(active_records_by_pair)
        )
        missing_count = eligible_count - len(active_records_by_pair)

        if not active_records_by_pair:
            # Either no memory artifacts at all OR none matched
            # the eligibility filter. Either way the operator
            # sees the same outcome: query falls back to existing
            # routes with a clear diagnostic.
            warnings.append(WARNING_NO_PROJECT_MEMORY_ARTIFACTS)
            return KnowledgeMemoryQueryContext(
                status=STATUS_NOT_AVAILABLE,
                scope=SCOPE_PROJECT_ACTIVE,
                project_id=project_id,
                document_count=0,
                memory_artifact_count=0,
                warnings=tuple(warnings),
            )

        # Cap application — documents first (the eligibility key),
        # then a defence-in-depth artifact cap. The dict's
        # insertion order is deterministic across CPython
        # versions, so the cap selects a stable subset.
        ordered_pairs = list(active_records_by_pair.items())
        if len(ordered_pairs) > settings.max_project_documents:
            ordered_pairs = ordered_pairs[:settings.max_project_documents]
            warnings.append(WARNING_PROJECT_MEMORY_DOCUMENT_CAP_APPLIED)
        if len(ordered_pairs) > settings.max_project_artifacts:
            ordered_pairs = ordered_pairs[:settings.max_project_artifacts]
            warnings.append(WARNING_PROJECT_MEMORY_ARTIFACT_CAP_APPLIED)

        # Partial-coverage warning fires when SOME eligibility
        # pairs lacked memory artifacts (not when caps fired).
        if missing_count > 0:
            warnings.append(WARNING_PROJECT_MEMORY_PARTIAL)

        # Load payloads + aggregate entries from each artifact.
        # Lineage stamped on each entry's `SelectedMemoryEntry` so
        # the diagnostic block + later phases can group by doc.
        all_entries: list[tuple[
            KnowledgeMemoryEntry, str, str, str,
        ]] = []  # (entry, document_id, snapshot_id, artifact_id)
        loaded_artifact_count = 0
        total_artifact_entries = 0
        for (doc_id, snap_id), record in ordered_pairs:
            payload = self._read_memory_payload(ctx, record, warnings)
            if payload is None:
                continue
            loaded_artifact_count += 1
            total_artifact_entries += len(payload.entries)
            artifact_id = getattr(record, "artifact_id", "")
            for entry in payload.entries:
                all_entries.append((entry, doc_id, snap_id, artifact_id))

        if not all_entries:
            if WARNING_MEMORY_NO_ENTRIES not in warnings:
                warnings.append(WARNING_MEMORY_NO_ENTRIES)
            return KnowledgeMemoryQueryContext(
                status=STATUS_LOADED_NO_MATCH,
                scope=SCOPE_PROJECT_ACTIVE,
                project_id=project_id,
                available=True,
                document_count=len(ordered_pairs),
                memory_artifact_count=loaded_artifact_count,
                warnings=tuple(warnings),
            )

        # Run the selection matcher over the combined entry pool.
        # We re-implement the per-entry walk here (rather than
        # calling `_select_entries`) so each surviving entry
        # retains its `(document_id, snapshot_id, artifact_id)`
        # lineage on the returned `SelectedMemoryEntry`.
        selected, truncated = self._select_project_entries(
            all_entries=all_entries,
            question=question,
            max_entries=settings.max_entries,
        )
        if truncated:
            warnings.append(WARNING_TRUNCATED)

        if not selected:
            warnings.append(WARNING_MEMORY_LOADED_NO_MATCH)
            return KnowledgeMemoryQueryContext(
                status=STATUS_LOADED_NO_MATCH,
                scope=SCOPE_PROJECT_ACTIVE,
                project_id=project_id,
                available=True,
                document_count=len(ordered_pairs),
                memory_artifact_count=loaded_artifact_count,
                entry_count=total_artifact_entries,
                warnings=tuple(warnings),
            )

        expansion_terms = self._expansion_terms_for(selected)
        types = tuple(sorted({s.entry.memory_type for s in selected}))
        resolved_refs = sum(1 for s in selected if s.has_source_refs())

        return KnowledgeMemoryQueryContext(
            status=STATUS_USED,
            scope=SCOPE_PROJECT_ACTIVE,
            project_id=project_id,
            available=True,
            artifact_id=None,  # multiple artifacts; per-entry on SelectedMemoryEntry
            entry_count=total_artifact_entries,
            document_count=len(ordered_pairs),
            memory_artifact_count=loaded_artifact_count,
            selected_entries=tuple(selected),
            expansion_terms=expansion_terms,
            selected_entry_types=types,
            resolved_source_ref_count=resolved_refs,
            warnings=tuple(warnings),
        )

    def _select_project_entries(
        self,
        *,
        all_entries: list[tuple[KnowledgeMemoryEntry, str, str, str]],
        question: str,
        max_entries: int,
    ) -> tuple[list[SelectedMemoryEntry], bool]:
        """Variant of `_select_entries` that retains per-entry
        lineage. Same matcher logic; just stamps `document_id` /
        `snapshot_id` / `artifact_id` on each surviving
        `SelectedMemoryEntry` so the project-scope diagnostic +
        future source-ref injection (Phase 5B) can group by
        document."""
        norm_query = (question or "").lower()
        query_tokens = _tokenise(norm_query)
        if not norm_query and not query_tokens:
            return [], False

        per_entry: list[SelectedMemoryEntry] = []
        for entry, doc_id, snap_id, artifact_id in all_entries:
            reason = self._match_reason_for(
                entry, norm_query=norm_query, query_tokens=query_tokens,
            )
            if reason is None:
                continue
            per_entry.append(SelectedMemoryEntry(
                entry=entry,
                use_mode=self._use_mode_for(entry),
                match_reason=reason,
                document_id=doc_id,
                snapshot_id=snap_id,
                artifact_id=artifact_id,
            ))

        truncated = len(per_entry) > max_entries
        if truncated:
            per_entry = per_entry[:max_entries]
        return per_entry, truncated

    # ---- Snapshot resolution -------------------------------------

    def _resolve_active_snapshot(
        self, *, ctx, document_id: str,
    ) -> str | None:
        try:
            doc = self._source_lookup.get_source(ctx, document_id)
        except Exception:  # noqa: BLE001 — defensive
            return None
        return getattr(doc, "active_snapshot_id", None)

    # ---- Artifact lookup ----------------------------------------

    def _find_active_memory_record(
        self, *, ctx, document_id: str, snapshot_id: str,
    ) -> Any | None:
        list_artifacts = getattr(self._artifacts, "list_artifacts", None)
        if not callable(list_artifacts):
            return None
        try:
            records = list_artifacts(ctx, kind="knowledge_memory")
        except TypeError:
            try:
                records = [
                    r for r in list_artifacts(ctx)
                    if getattr(r, "kind", None) == "knowledge_memory"
                ]
            except Exception:  # noqa: BLE001
                return None
        except Exception:  # noqa: BLE001
            return None
        active = _select_active_memory(
            records, document_id=document_id, snapshot_id=snapshot_id,
        )
        if len(active) != 1:
            # 0 active rows → not built; multiple → corrupted state
            # (Phase 3B status returns `unknown`). For query, we
            # treat both as "no usable memory" and fall back.
            return None
        return active[0]

    # ---- Payload read -------------------------------------------

    def _read_memory_payload(
        self, ctx, record, warnings: list[str],
    ) -> KnowledgeMemoryPayload | None:
        """Inline-metadata first (test fakes / future producers),
        then workspace file. Returns None on unreadable / malformed
        payloads and stamps a warning."""
        metadata = getattr(record, "metadata", None) or {}
        inline = metadata.get("payload")
        if isinstance(inline, Mapping):
            try:
                return KnowledgeMemoryPayload.from_payload(inline)
            except Exception:  # noqa: BLE001
                warnings.append(WARNING_MEMORY_PAYLOAD_MALFORMED)
                return None

        if self._workspace is None:
            warnings.append(WARNING_MEMORY_NOT_READABLE)
            return None

        location = getattr(record, "location", None)
        if not location:
            warnings.append(WARNING_MEMORY_NOT_READABLE)
            return None

        try:
            from j1.workspace.layout import WorkspaceArea
        except ImportError:
            warnings.append(WARNING_MEMORY_NOT_READABLE)
            return None
        parts = PurePosixPath(str(location)).parts
        if not parts:
            warnings.append(WARNING_MEMORY_NOT_READABLE)
            return None
        area_name, *rest = parts
        try:
            area = WorkspaceArea(area_name)
        except ValueError:
            warnings.append(WARNING_MEMORY_NOT_READABLE)
            return None
        try:
            area_root = self._workspace.area(ctx, area).resolve()
        except Exception:  # noqa: BLE001
            warnings.append(WARNING_MEMORY_NOT_READABLE)
            return None
        candidate = area_root.joinpath(*rest).resolve()
        try:
            candidate.relative_to(area_root)
        except ValueError:
            warnings.append(WARNING_MEMORY_NOT_READABLE)
            return None
        if not candidate.exists():
            warnings.append(WARNING_MEMORY_NOT_READABLE)
            return None
        try:
            data = candidate.read_bytes()
            decoded = json.loads(data.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            warnings.append(WARNING_MEMORY_PAYLOAD_MALFORMED)
            return None
        if not isinstance(decoded, Mapping):
            warnings.append(WARNING_MEMORY_PAYLOAD_MALFORMED)
            return None
        try:
            return KnowledgeMemoryPayload.from_payload(decoded)
        except Exception:  # noqa: BLE001
            warnings.append(WARNING_MEMORY_PAYLOAD_MALFORMED)
            return None

    # ---- Selection ----------------------------------------------

    def _select_entries(
        self,
        *,
        entries: Iterable[KnowledgeMemoryEntry],
        question: str,
        max_entries: int,
        artifact_id: str | None = None,
        document_id: str | None = None,
        snapshot_id: str | None = None,
    ) -> tuple[list[SelectedMemoryEntry], bool]:
        """Return `(selected, truncated)`. Selection is a small
        deterministic substring/keyword matcher — see module
        docstring.

        Phase 5A patch: optional lineage kwargs (default None for
        Phase 4 callers) get stamped on each ``SelectedMemoryEntry``
        so the document-active path can also expose lineage on the
        diagnostic. Backward-compatible — pre-patch callers don't
        need to pass them."""
        norm_query = (question or "").lower()
        query_tokens = _tokenise(norm_query)
        if not norm_query and not query_tokens:
            return [], False

        # Build a per-entry match record. Each entry can be
        # surfaced by multiple signals; we keep the first match
        # reason so the diagnostic stays compact.
        per_entry: list[SelectedMemoryEntry] = []
        for entry in entries:
            reason = self._match_reason_for(
                entry, norm_query=norm_query, query_tokens=query_tokens,
            )
            if reason is None:
                continue
            per_entry.append(SelectedMemoryEntry(
                entry=entry,
                use_mode=self._use_mode_for(entry),
                match_reason=reason,
                document_id=document_id,
                snapshot_id=snapshot_id,
                artifact_id=artifact_id,
            ))

        truncated = len(per_entry) > max_entries
        if truncated:
            per_entry = per_entry[:max_entries]
        return per_entry, truncated

    def _match_reason_for(
        self,
        entry: KnowledgeMemoryEntry,
        *,
        norm_query: str,
        query_tokens: set[str],
    ) -> str | None:
        title_lower = (entry.title or "").lower()
        content_lower = (entry.content or "").lower()
        # 1. Title substring match — strongest signal.
        if title_lower and title_lower in norm_query:
            return "title_in_query"
        if title_lower:
            for tok in query_tokens:
                if tok and tok in title_lower:
                    return "title_token_match"
        # 2. Content substring match — second-strongest.
        if content_lower:
            for tok in query_tokens:
                if tok and tok in content_lower:
                    return "content_token_match"
        # 3. Tag match.
        for tag in entry.tags:
            tag_lower = tag.lower()
            if tag_lower and tag_lower in norm_query:
                return "tag_match"
        # 4. Intent-keyword match by memory type. A query that
        # mentions "risks" surfaces every risk entry as a candidate.
        intent_tokens = _INTENT_KEYWORDS.get(entry.memory_type, frozenset())
        if intent_tokens and any(t in norm_query for t in intent_tokens):
            return "intent_keyword"
        # 5. Alias entries — match on canonical_name in
        # structured_payload (the builder copies the YAML field
        # in there for alias entries).
        if entry.memory_type == MEMORY_ENTRY_TYPE_ALIAS:
            canonical = str(
                entry.structured_payload.get("canonical_name", "")
                or entry.structured_payload.get("canonical", "")
            ).lower()
            if canonical and canonical in norm_query:
                return "alias_canonical_match"
            for alias in entry.structured_payload.get("aliases", []) or []:
                if str(alias).lower() in norm_query:
                    return "alias_synonym_match"
        return None

    def _use_mode_for(self, entry: KnowledgeMemoryEntry) -> str:
        if entry.memory_type in _EXPANSION_ONLY_TYPES:
            return USE_MODE_EXPANSION_ONLY
        if entry.memory_type in _SUMMARY_CONTEXT_TYPES:
            return USE_MODE_SUMMARY_CONTEXT
        if entry.memory_type in _DERIVED_CANDIDATE_TYPES:
            # Derived only when the entry actually carries source
            # refs — otherwise it's a hint without grounding.
            if entry.source_refs:
                return USE_MODE_DERIVED_CANDIDATE
            return USE_MODE_EXPANSION_ONLY
        # Unknown / future types — treat conservatively as
        # expansion-only so we never accidentally cite an unrefed
        # memory entry as evidence.
        return USE_MODE_EXPANSION_ONLY

    def _expansion_terms_for(
        self, selected: list[SelectedMemoryEntry],
    ) -> tuple[str, ...]:
        """Build a deduplicated, capped expansion-term list from
        selected entries. Order preserved from selection order so
        downstream consumers can sort/cap further if needed."""
        seen: set[str] = set()
        terms: list[str] = []
        for s in selected:
            entry = s.entry
            # Alias entries contribute their canonical + alias synonyms.
            if entry.memory_type == MEMORY_ENTRY_TYPE_ALIAS:
                canonical = str(
                    entry.structured_payload.get("canonical_name", "")
                    or entry.structured_payload.get("canonical", "")
                    or entry.title
                ).strip()
                if canonical and canonical.lower() not in seen:
                    seen.add(canonical.lower())
                    terms.append(canonical)
                for alias in entry.structured_payload.get("aliases", []) or []:
                    text = str(alias).strip()
                    if text and text.lower() not in seen:
                        seen.add(text.lower())
                        terms.append(text)
                continue
            # Terminology / retrieval-hint entries contribute their
            # title text.
            if entry.memory_type in (
                MEMORY_ENTRY_TYPE_TERMINOLOGY,
                MEMORY_ENTRY_TYPE_RETRIEVAL_HINT,
            ):
                text = entry.title.strip() if entry.title else ""
                if text and text.lower() not in seen:
                    seen.add(text.lower())
                    terms.append(text)
                continue
            # Derived-candidate entries: title is operator-readable
            # headline; useful as expansion when it's short.
            if (
                entry.memory_type in _DERIVED_CANDIDATE_TYPES
                and entry.title
                and len(entry.title) <= 80
            ):
                text = entry.title.strip()
                if text and text.lower() not in seen:
                    seen.add(text.lower())
                    terms.append(text)
        return tuple(terms)


# ---- Helpers --------------------------------------------------


def _str_or_none_meta(metadata: dict, key: str) -> str | None:
    """Read a string-valued artifact metadata key. Returns ``None``
    when the key is absent or non-stringly-coercible. Used by the
    project-active path's per-record lineage check so a memory
    artifact with a missing / malformed lineage stamp is skipped
    rather than producing cross-document leakage."""
    value = metadata.get(key)
    if value is None:
        return None
    s = str(value).strip()
    return s or None


# Common English stopwords that produce noisy false-positive
# matches against entry content/titles. Kept small on purpose —
# this is a substring matcher, not an IR pipeline. Lowercase
# only. Update by adding tokens (renames break the matcher's
# determinism guarantees).
_QUERY_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "are", "was", "were", "you", "this",
    "that", "with", "from", "into", "have", "has", "had", "will",
    "would", "should", "could", "can", "but", "not", "any", "all",
    "some", "what", "where", "when", "why", "how", "which", "who",
    "whom", "whose", "they", "them", "their", "its", "our", "your",
    "his", "her", "him", "she", "they", "about", "such",
})


def _tokenise(text: str) -> set[str]:
    """Cheap tokeniser — splits on whitespace + punctuation, drops
    short noise tokens AND common English stopwords. Deterministic;
    no external deps.

    Stopword filtering matters: without it, a 3-char token like
    "the" co-occurs in nearly every entry's content and produces
    false-positive matches. The matcher is a relevance-style
    surface; absent stopword filtering it would surface every
    entry on most queries."""
    if not text:
        return set()
    out: set[str] = set()
    current: list[str] = []

    def _maybe_add(tok: str) -> None:
        if len(tok) < 3:
            return
        if tok in _QUERY_STOPWORDS:
            return
        out.add(tok)

    for ch in text:
        if ch.isalnum():
            current.append(ch)
        else:
            if current:
                _maybe_add("".join(current))
                current = []
    if current:
        _maybe_add("".join(current))
    return out
