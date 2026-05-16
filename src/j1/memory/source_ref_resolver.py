"""Phase 5B — `KnowledgeMemoryEvidenceResolver`.

Resolves the `EnrichmentSourceRef`s carried by selected
`KnowledgeMemoryEntry` rows into source-grounded
`EvidenceCandidate`s the existing query pipeline can consume.

Hard contract:

  * **Memory points; source evidence proves.** Memory entries
    themselves never become evidence. Only their `source_refs`,
    when resolvable to active source artifacts, contribute
    `EvidenceCandidate`s.
  * **Never raises into the caller.** Resolver failure → empty
    result + warning. The orchestrator falls back to canonical
    retrieval.
  * **No LLM, no rerank, no synthesis.** The resolver reads
    artifacts via the registry, optionally loads body bytes via a
    caller-supplied body loader, and stamps memory-guided
    provenance fields on the resulting candidates.
  * **Snapshot-isolated.** A ref pointing at a superseded /
    ineligible snapshot is dropped with a warning; never silently
    surfaced.
  * **Capped + deduplicated.** Returns at most
    ``settings.max_source_evidence`` candidates; deduplicates
    against `(route, artifact_id, chunk_id)` triples already
    present in the canonical pool so the resolver can never
    duplicate a chunk the normal route already produced.
  * **Defers what it can't resolve.** Table/image/page-only refs
    that require infrastructure beyond the registry are recorded
    as `source_ref_table_deferred` / `source_ref_image_deferred`
    diagnostics rather than injected as evidence.

The resolver is wired into ``SmartQueryOrchestrator`` after the
Phase 4 memory provider returns ``status=used`` and after the
Phase 5A expansion merge has populated retrieval variants. The
resolver runs AFTER the canonical retrieval routes so it can
dedupe against the already-retrieved evidence.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Callable

from j1.memory.query_provider import (
    SelectedMemoryEntry,
    USE_MODE_DERIVED_CANDIDATE,
)
from j1.memory.query_settings import KnowledgeMemoryQuerySettings
from j1.processing.derived_enrichment import EnrichmentSourceRef
from j1.query.query_plan import EvidenceCandidate, RetrievalRouteKind


_log = logging.getLogger(__name__)


# ---- Provenance + diagnostic vocabulary ------------------------


# Stamped on each injected candidate's ``extra`` dict so dashboards
# can distinguish memory-guided from canonical-retrieval evidence.
# Stable wire string — add values, don't rename.
EVIDENCE_ORIGIN_MEMORY_GUIDED = "memory_guided_source_ref"


# Warning vocabulary surfaced on the diagnostic block. Stable
# strings — operators / dashboards key off these. Add codes; don't
# rename.
WARNING_SOURCE_REF_ARTIFACT_NOT_FOUND = "source_ref_artifact_not_found"
WARNING_SOURCE_REF_OUT_OF_SCOPE = "source_ref_out_of_scope"
WARNING_SOURCE_REF_SUPERSEDED = "source_ref_superseded"
WARNING_SOURCE_REF_TABLE_DEFERRED = "source_ref_table_deferred"
WARNING_SOURCE_REF_IMAGE_DEFERRED = "source_ref_image_deferred"
WARNING_SOURCE_REF_NO_LOCATOR = "source_ref_no_locator"
WARNING_SOURCE_REF_DEDUPED = "source_ref_deduped_with_canonical"
WARNING_SOURCE_REF_CAP_APPLIED = "source_ref_cap_applied"
WARNING_RESOLVER_DISABLED_BY_SETTINGS = "resolver_disabled_by_settings"


# ---- Result --------------------------------------------------


@dataclass(frozen=True)
class ResolvedSourceEvidence:
    """One resolved candidate + the memory entry that pointed at it.

    Carries the lineage explicitly so the orchestrator can stamp
    diagnostics without re-resolving."""

    candidate: EvidenceCandidate
    memory_id: str
    memory_type: str
    memory_artifact_id: str | None
    source_ref: EnrichmentSourceRef


@dataclass(frozen=True)
class KnowledgeMemoryEvidenceResolution:
    """Structured output the orchestrator folds into the evidence
    pipeline + the trace's ``knowledge_memory`` diagnostic block.

    Fields:
      * ``injected`` — candidates ready for the evidence pipeline.
        Order is selection-stable (memory entries scanned in the
        provider's selection order, refs in their source-ref order).
      * ``resolved_source_ref_count`` — total source refs the
        resolver considered (including ones that didn't produce a
        candidate after dedup / cap).
      * ``injected_evidence_count`` — ``len(injected)`` cached for
        readability.
      * ``deduped_evidence_count`` — refs dropped because the same
        ``(route, artifact_id, chunk_id)`` triple already appeared
        in the canonical pool.
      * ``unresolved_source_ref_count`` — refs the resolver could
        not turn into a candidate (missing artifact, ineligible
        snapshot, deferred kind, no locator).
      * ``warnings`` — stable warning codes.
      * ``applied`` — whether at least one candidate was injected.
        Distinct from ``injected_evidence_count > 0`` only when
        future code wants to flip ``applied=False`` while still
        recording diagnostics.
    """

    injected: tuple[ResolvedSourceEvidence, ...] = ()
    resolved_source_ref_count: int = 0
    injected_evidence_count: int = 0
    deduped_evidence_count: int = 0
    unresolved_source_ref_count: int = 0
    warnings: tuple[str, ...] = ()
    applied: bool = False

    def to_diagnostic(self) -> dict[str, Any]:
        """Project the resolver result into the trace diagnostic
        dict shape. The orchestrator merges this into the existing
        ``knowledge_memory`` block returned by the Phase 4 provider
        so the FE / JSON consumer sees one combined view."""
        return {
            "resolved_source_ref_count": self.resolved_source_ref_count,
            "injected_evidence_count": self.injected_evidence_count,
            "deduped_evidence_count": self.deduped_evidence_count,
            "unresolved_source_ref_count": (
                self.unresolved_source_ref_count
            ),
            "source_ref_resolution_warnings": list(self.warnings),
            "evidence_injection_applied": self.applied,
        }

    @classmethod
    def empty(cls) -> "KnowledgeMemoryEvidenceResolution":
        return cls()


# ---- Resolver ------------------------------------------------


class KnowledgeMemoryEvidenceResolver:
    """Resolve selected memory entries' source refs into source-
    grounded ``EvidenceCandidate`` rows.

    Dependencies (all optional — the resolver gracefully degrades):

      * ``artifact_registry`` — `ArtifactRegistry`. Used to locate
        the source artifact a ref points at. When not provided the
        resolver returns empty + a warning.
      * ``body_loader`` — `Callable[[ArtifactRecord], str]` that
        reads artifact body bytes off disk. Optional; when missing
        we still surface the candidate but ``body`` is empty (the
        sufficiency gate still counts it; the synthesizer sees it
        as a marker citation). Production wires the workspace
        resolver's reader.
    """

    def __init__(
        self,
        *,
        artifact_registry: Any = None,
        body_loader: Callable[[Any], str] | None = None,
    ) -> None:
        self._artifacts = artifact_registry
        self._body_loader = body_loader

    # ---- Public API -------------------------------------------

    def resolve(
        self,
        *,
        ctx,
        selected_entries: tuple[SelectedMemoryEntry, ...],
        settings: KnowledgeMemoryQuerySettings,
        eligible_snapshot_pairs: (
            "frozenset[tuple[str, str]] | None"
        ) = None,
        existing_keys: "frozenset[tuple[str, str, str]] | None" = None,
        document_id: str | None = None,
        project_id: str | None = None,
    ) -> KnowledgeMemoryEvidenceResolution:
        """Resolve refs into evidence candidates.

        Inputs:
          * ``ctx`` — `ProjectContext`. Passed to the registry's
            ``list_artifacts`` / ``get`` for tenant + project
            scoping.
          * ``selected_entries`` — the provider's selected entries
            (Phase 4). The resolver inspects each entry's
            ``source_refs``; entries without refs contribute nothing.
          * ``settings`` — the same settings dataclass the provider
            uses. ``max_source_evidence`` caps the injection size.
          * ``eligible_snapshot_pairs`` — when supplied, every
            resolved ref MUST live in one of these
            ``(document_id, snapshot_id)`` pairs. Refs outside the
            allowlist are dropped with a warning.
          * ``existing_keys`` — set of ``(route, artifact_id,
            chunk_id)`` triples already present in the canonical
            evidence pool. Refs that would collide are dropped with
            the ``source_ref_deduped_with_canonical`` warning.
          * ``document_id`` — orchestrator's request document_id.
            Used as a fallback eligibility filter when
            ``eligible_snapshot_pairs`` is None.
          * ``project_id`` — orchestrator's project_id. Stamped on
            the resulting ``EvidenceCandidate.project_id``.

        Never raises. Any internal failure is caught and returns an
        empty resolution + a ``resolver_error:<Type>`` warning."""
        if not selected_entries:
            return KnowledgeMemoryEvidenceResolution.empty()
        try:
            return self._resolve_inner(
                ctx=ctx,
                selected_entries=selected_entries,
                settings=settings,
                eligible_snapshot_pairs=eligible_snapshot_pairs,
                existing_keys=existing_keys or frozenset(),
                document_id=document_id,
                project_id=project_id or "",
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "knowledge_memory evidence resolver raised; falling back",
                exc_info=True,
            )
            return KnowledgeMemoryEvidenceResolution(
                warnings=(f"resolver_error:{type(exc).__name__}",),
            )

    # ---- Internals --------------------------------------------

    def _resolve_inner(
        self,
        *,
        ctx,
        selected_entries: tuple[SelectedMemoryEntry, ...],
        settings: KnowledgeMemoryQuerySettings,
        eligible_snapshot_pairs: "frozenset[tuple[str, str]] | None",
        existing_keys: "frozenset[tuple[str, str, str]]",
        document_id: str | None,
        project_id: str,
    ) -> KnowledgeMemoryEvidenceResolution:
        injected: list[ResolvedSourceEvidence] = []
        warnings: list[str] = []
        resolved_count = 0
        unresolved_count = 0
        deduped_count = 0
        cap_applied = False

        # Build a lookup helper. We list once per kind on demand so
        # the resolver doesn't pre-pay for a full registry scan when
        # no refs need resolution. ``_artifact_lookup`` caches the
        # result per resolve() call.
        artifact_lookup = _ArtifactLookup(
            artifacts=self._artifacts, ctx=ctx,
        )
        seen_keys: set[tuple[str, str, str]] = set()

        for entry in selected_entries:
            # Phase 5B: only entries the provider classified as
            # derived-candidate (i.e. with source refs) feed the
            # resolver. expansion_only / summary_context entries
            # were already classified by the provider as not
            # eligible for evidence injection; we mirror that here
            # so the cap doesn't fill up with refs from contextual
            # entries that shouldn't ground answers.
            if entry.use_mode != USE_MODE_DERIVED_CANDIDATE:
                continue
            for ref in entry.entry.source_refs:
                resolved_count += 1
                # Eligibility filter — drop refs pointing at
                # snapshots outside the caller's allowlist.
                if not _ref_in_eligible_pairs(
                    ref,
                    eligible_snapshot_pairs=eligible_snapshot_pairs,
                    fallback_document_id=document_id,
                ):
                    unresolved_count += 1
                    if WARNING_SOURCE_REF_OUT_OF_SCOPE not in warnings:
                        warnings.append(WARNING_SOURCE_REF_OUT_OF_SCOPE)
                    continue

                # Per-ref-type dispatch. Chunk + artifact refs
                # resolve directly; table / image / page refs
                # require richer infra and defer with a diagnostic.
                resolution = self._resolve_one_ref(
                    ref=ref,
                    artifact_lookup=artifact_lookup,
                    project_id=project_id,
                    entry=entry,
                )
                if resolution.warning is not None:
                    if resolution.warning not in warnings:
                        warnings.append(resolution.warning)
                if resolution.candidate is None:
                    unresolved_count += 1
                    continue

                key = (
                    resolution.candidate.route.value,
                    resolution.candidate.artifact_id,
                    resolution.candidate.chunk_id or "",
                )
                if key in existing_keys or key in seen_keys:
                    deduped_count += 1
                    if WARNING_SOURCE_REF_DEDUPED not in warnings:
                        warnings.append(WARNING_SOURCE_REF_DEDUPED)
                    continue
                seen_keys.add(key)

                if len(injected) >= settings.max_source_evidence:
                    cap_applied = True
                    continue

                injected.append(ResolvedSourceEvidence(
                    candidate=resolution.candidate,
                    memory_id=entry.entry.memory_id,
                    memory_type=entry.entry.memory_type,
                    memory_artifact_id=entry.artifact_id,
                    source_ref=ref,
                ))

        if cap_applied and WARNING_SOURCE_REF_CAP_APPLIED not in warnings:
            warnings.append(WARNING_SOURCE_REF_CAP_APPLIED)

        return KnowledgeMemoryEvidenceResolution(
            injected=tuple(injected),
            resolved_source_ref_count=resolved_count,
            injected_evidence_count=len(injected),
            deduped_evidence_count=deduped_count,
            unresolved_source_ref_count=unresolved_count,
            warnings=tuple(warnings),
            applied=bool(injected),
        )

    def _resolve_one_ref(
        self,
        *,
        ref: EnrichmentSourceRef,
        artifact_lookup: "_ArtifactLookup",
        project_id: str,
        entry: SelectedMemoryEntry,
    ) -> "_RefResolution":
        # Defer table / image refs that don't carry a chunk/artifact
        # we can directly resolve. We still surface the candidate
        # when the producer attached an artifact_id (the chunk-level
        # injection path covers both).
        if ref.table_id and not (ref.chunk_id or ref.artifact_id):
            return _RefResolution(
                candidate=None,
                warning=WARNING_SOURCE_REF_TABLE_DEFERRED,
            )
        if ref.image_id and not (ref.chunk_id or ref.artifact_id):
            # Image-only refs typically resolve to an `image`
            # artifact directly; surface as a marker candidate so
            # the trace shows it BUT mark it deferred since the
            # current evidence builder doesn't render image bodies.
            artifact_id = ref.image_id
            record = artifact_lookup.get(artifact_id)
            if record is None:
                return _RefResolution(
                    candidate=None,
                    warning=WARNING_SOURCE_REF_IMAGE_DEFERRED,
                )
            cand = self._build_candidate(
                ref=ref, artifact_id=artifact_id,
                chunk_id=None,
                artifact_kind=str(record.kind or "image"),
                body="",
                project_id=project_id,
                entry=entry,
                record=record,
            )
            return _RefResolution(
                candidate=cand,
                warning=WARNING_SOURCE_REF_IMAGE_DEFERRED,
            )

        # Prefer the most specific locator: chunk_id over artifact_id
        # over page. Chunk refs are the gold standard — they point at
        # a specific compile chunk the canonical pool can also reach.
        artifact_id = ref.artifact_id or None
        if not artifact_id and ref.chunk_id:
            # Chunk without artifact — look up by chunk metadata
            # below. For now we still need an artifact_id to form a
            # candidate key; mark unresolved with no-locator if
            # neither chunk_id nor artifact_id can be resolved.
            return _RefResolution(
                candidate=None,
                warning=WARNING_SOURCE_REF_NO_LOCATOR,
            )
        if not artifact_id:
            # Page-only or locator-only refs require richer infra.
            return _RefResolution(
                candidate=None,
                warning=WARNING_SOURCE_REF_NO_LOCATOR,
            )

        record = artifact_lookup.get(artifact_id)
        if record is None:
            return _RefResolution(
                candidate=None,
                warning=WARNING_SOURCE_REF_ARTIFACT_NOT_FOUND,
            )
        # Snapshot supersession check — the registry hides
        # superseded artifacts via search_state. A ref pointing at
        # a record whose ``search_state`` is anything other than
        # ``active`` is dropped with a warning so the operator sees
        # the stale ref instead of silently grounding on it.
        metadata = dict(getattr(record, "metadata", None) or {})
        state = metadata.get("search_state") or "active"
        if state != "active":
            return _RefResolution(
                candidate=None,
                warning=WARNING_SOURCE_REF_SUPERSEDED,
            )

        body = ""
        if self._body_loader is not None:
            try:
                body = self._body_loader(record) or ""
            except Exception:  # noqa: BLE001
                body = ""

        cand = self._build_candidate(
            ref=ref,
            artifact_id=artifact_id,
            chunk_id=ref.chunk_id,
            artifact_kind=str(
                ref.artifact_kind
                or record.kind
                or "compiled_text"
            ),
            body=body,
            project_id=project_id,
            entry=entry,
            record=record,
        )
        return _RefResolution(candidate=cand, warning=None)

    def _build_candidate(
        self,
        *,
        ref: EnrichmentSourceRef,
        artifact_id: str,
        chunk_id: str | None,
        artifact_kind: str,
        body: str,
        project_id: str,
        entry: SelectedMemoryEntry,
        record: Any,
    ) -> EvidenceCandidate:
        """Build the ``EvidenceCandidate`` that the evidence
        pipeline will see. The route is fixed to ``ARTIFACT_LOOKUP``
        so the candidate doesn't collide with chunk-grain RAGAnything
        / BM25 hits — same triple from the chunk route is treated as
        distinct, but the resolver's ``existing_keys`` dedup uses
        the canonical (artifact_id, chunk_id) pair across routes so
        a chunk already retrieved doesn't get re-injected.

        Provenance fields land on ``extra`` for the trace + the
        synthesizer's user prompt; the binder reads them so the
        answer can cite ``[#N]`` and the binder resolves to the
        source artifact, not the memory entry.
        """
        record_run_id = getattr(record, "created_by_run_id", None)
        if not record_run_id:
            md = dict(getattr(record, "metadata", None) or {})
            record_run_id = md.get("run_id") or ref.run_id
        document_id = ref.document_id
        if not document_id:
            doc_ids = list(getattr(record, "source_document_ids", []) or [])
            if doc_ids:
                document_id = doc_ids[0]
        snapshot_id = ref.snapshot_id or getattr(record, "snapshot_id", None)

        text_preview = _truncate(body, 600) if body else ""

        return EvidenceCandidate(
            route=RetrievalRouteKind.ARTIFACT_LOOKUP,
            artifact_id=artifact_id,
            artifact_kind=artifact_kind,
            chunk_id=chunk_id,
            text_preview=text_preview,
            # Lower than canonical RAGAnything semantic hits so the
            # builder's ranker prefers normal-route evidence when
            # both surface the same source. The score is high
            # enough to clear the sufficiency floor (``min_score=0``)
            # so a memory-only answer can still ground itself.
            score=0.8,
            matched_anchors=(),
            run_id=record_run_id,
            document_id=document_id,
            project_id=project_id,
            extra={
                "body": body,
                "evidence_origin": EVIDENCE_ORIGIN_MEMORY_GUIDED,
                "memory_id": entry.entry.memory_id,
                "memory_type": entry.entry.memory_type,
                "memory_artifact_id": entry.artifact_id,
                "source_artifact_id": artifact_id,
                "source_artifact_kind": artifact_kind,
                "snapshot_id": snapshot_id,
            },
        )


# ---- Helpers ----------------------------------------------------


@dataclass(frozen=True)
class _RefResolution:
    """Internal per-ref outcome carrying the candidate + an optional
    warning code. Centralises the resolver's per-branch decisions
    so ``_resolve_inner`` stays linear."""

    candidate: EvidenceCandidate | None
    warning: str | None


class _ArtifactLookup:
    """Lazy, per-call cache around the artifact registry. The
    resolver may inspect refs from multiple memory entries; an
    artifact id repeated across refs should hit the registry only
    once.

    Falls back to ``list_artifacts`` when ``get`` isn't available
    so test stubs can use the simpler list-based registry without
    implementing the full Protocol."""

    def __init__(self, *, artifacts: Any, ctx) -> None:
        self._artifacts = artifacts
        self._ctx = ctx
        self._cache: dict[str, Any] = {}
        self._all_records_listed = False
        self._failed = False

    def get(self, artifact_id: str) -> Any | None:
        if self._artifacts is None or self._failed:
            return None
        if artifact_id in self._cache:
            return self._cache[artifact_id]
        getter = getattr(self._artifacts, "get", None)
        if callable(getter):
            try:
                record = getter(self._ctx, artifact_id)
                self._cache[artifact_id] = record
                return record
            except Exception:  # noqa: BLE001
                # Fall through to the list path; some stubs raise
                # for missing ids rather than returning None.
                pass
        # Fall back to listing once + caching everything.
        if not self._all_records_listed:
            list_artifacts = getattr(self._artifacts, "list_artifacts", None)
            if not callable(list_artifacts):
                self._failed = True
                return None
            try:
                records = list_artifacts(self._ctx)
            except TypeError:
                try:
                    records = list_artifacts(self._ctx, kind=None)
                except Exception:  # noqa: BLE001
                    self._failed = True
                    return None
            except Exception:  # noqa: BLE001
                self._failed = True
                return None
            for record in records or ():
                rid = getattr(record, "artifact_id", None)
                if rid and rid not in self._cache:
                    self._cache[rid] = record
            self._all_records_listed = True
        return self._cache.get(artifact_id)


def _ref_in_eligible_pairs(
    ref: EnrichmentSourceRef,
    *,
    eligible_snapshot_pairs: "frozenset[tuple[str, str]] | None",
    fallback_document_id: str | None,
) -> bool:
    """Eligibility check used by both document-scope + project-
    scope queries. When ``eligible_snapshot_pairs`` is supplied the
    ref's ``(document_id, snapshot_id)`` must be in the allowlist;
    otherwise we fall back to a less-strict check that compares
    against the request's document_id (document-scope path) or
    accepts the ref unconditionally (when the caller hasn't
    pre-resolved eligibility at all).
    """
    if eligible_snapshot_pairs is not None:
        if not ref.document_id or not ref.snapshot_id:
            # Memory entries built without lineage stamps slipped
            # through the supersede sweep; treat as out-of-scope so
            # we don't pretend to ground on an un-located ref.
            return False
        return (ref.document_id, ref.snapshot_id) in eligible_snapshot_pairs
    if fallback_document_id is not None and ref.document_id:
        return ref.document_id == fallback_document_id
    # No eligibility data at all → accept (legacy / test path).
    return True


def _truncate(body: str, limit: int) -> str:
    if not body:
        return ""
    s = str(body)
    return s[:limit]


def collect_existing_keys(
    candidates: Iterable[EvidenceCandidate],
) -> frozenset[tuple[str, str, str]]:
    """Build the dedup-key set from a candidate iterable.

    Helper kept here (rather than in the orchestrator) so the
    resolver + its tests share the same key shape. Mirrors the
    orchestrator's existing dedup tuple.
    """
    keys: set[tuple[str, str, str]] = set()
    for cand in candidates:
        keys.add((
            cand.route.value,
            cand.artifact_id,
            cand.chunk_id or "",
        ))
    return frozenset(keys)


__all__ = [
    "EVIDENCE_ORIGIN_MEMORY_GUIDED",
    "KnowledgeMemoryEvidenceResolution",
    "KnowledgeMemoryEvidenceResolver",
    "ResolvedSourceEvidence",
    "WARNING_RESOLVER_DISABLED_BY_SETTINGS",
    "WARNING_SOURCE_REF_ARTIFACT_NOT_FOUND",
    "WARNING_SOURCE_REF_CAP_APPLIED",
    "WARNING_SOURCE_REF_DEDUPED",
    "WARNING_SOURCE_REF_IMAGE_DEFERRED",
    "WARNING_SOURCE_REF_NO_LOCATOR",
    "WARNING_SOURCE_REF_OUT_OF_SCOPE",
    "WARNING_SOURCE_REF_SUPERSEDED",
    "WARNING_SOURCE_REF_TABLE_DEFERRED",
    "collect_existing_keys",
]
