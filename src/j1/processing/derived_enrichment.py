"""Derived enrichment artifact contract — Phase 1.

A common envelope wrapping every post-compile domain enrichment
artifact so future code (Knowledge Memory projection, query
diagnostics, provenance UI) can read them uniformly without
knowing each producer's inner payload shape.

Hard contract — this module is **additive only**:

  * No producer is required to wrap its payload in this envelope.
    The existing legacy payloads on `enriched.*` artifact kinds
    remain the persistence shape. The envelope is materialised at
    READ time by `normalize_enrichment_artifact_payload(...)`.
  * No query route is changed. Consumers that already know the
    per-kind payload shape (intent classifier, evidence builder)
    keep working unchanged.
  * No artifact-registry / lineage-guard / persistence layer is
    touched. The envelope's `document_id` / `snapshot_id` /
    `run_id` fields come FROM the registry metadata at the call
    site; they are not authoritative storage.

The envelope makes four facts explicit:

  1. The artifact is **derived**, not canonical source evidence
     (`derived=True`, `canonical=False`).
  2. It is tied to a specific (document, snapshot, run) tuple.
  3. It carries `source_refs` pointing back into compile evidence
     where producers expose them — never invented.
  4. Source-ref absence is recorded as a warning, never silently
     dropped, so future projection can decide how to handle weak
     context items.

Phase boundary: persistent Knowledge Memory is NOT implemented
here. This module is the contract layer that lets Phase 2 read
enrichment artifacts uniformly when it lands. See `MEMORY.md`
for the multi-phase plan.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from j1.processing.enrichment_overlay import ProvenanceLink


_log = logging.getLogger(__name__)


# Stable schema marker. The version bump is a coordinated FE/BE
# release event — bump on contract-breaking changes only. Additive
# fields don't require a bump because consumers iterate keys
# rather than positional fields.
DERIVED_ENRICHMENT_ARTIFACT_SCHEMA = "derived_enrichment_artifact.v1"


# ---- Stage vocabulary -----------------------------------------------


# Pinned stage strings used in `producer.stage` / `derived_from.stage`.
# Dashboards / future memory build code filter on these — add values,
# don't rename.
PRODUCER_STAGE_DOMAIN_ENRICHMENT = "post_compile_domain_enrichment"
DERIVED_FROM_STAGE_COMPILE = "compile"


# ---- Warning codes --------------------------------------------------


# Stable warning vocabulary surfaced on `DerivedEnrichmentArtifact.warnings`.
# Operators / dashboards key off these strings; add codes, don't rename.
WARNING_MISSING_SOURCE_REFS = "missing_source_refs"
WARNING_RUN_LEVEL_SUMMARY_WITHOUT_SOURCE_REFS = (
    "run_level_summary_without_direct_source_refs"
)
WARNING_UNSUPPORTED_SOURCE_REF_SHAPE = "unsupported_source_ref_shape"
WARNING_LEGACY_PAYLOAD_NORMALIZED = "legacy_payload_normalized"
WARNING_PRODUCER_METADATA_MISSING = "producer_metadata_missing"
WARNING_UNKNOWN_ARTIFACT_KIND = "unknown_artifact_kind"


# ---- Source reference shape -----------------------------------------


@dataclass(frozen=True)
class EnrichmentSourceRef:
    """Minimal source reference for a derived enrichment item.

    Points back into compile evidence — chunk, page, table, image,
    graph entity, or the source artifact itself. ``locator`` is an
    open-ended escape hatch for future locator types that don't
    fit the typed fields; today every known producer can be mapped
    onto the typed fields.

    Field semantics:
      * `source_ref_id` — stable id when the producer minted one;
        otherwise the call site composes one or leaves it None.
      * `artifact_kind` — the kind string of the COMPILE artifact
        the ref points at (e.g. ``"chunk"``, ``"compiled_text"``,
        ``"graph_json"``). Defaults to ``"unknown"`` rather than
        omitting the field so consumers always see the column.
      * `chunk_id` / `page` / `table_id` / `image_id` — typed
        fields the most-common producers populate.
      * `graph_entity_id` / `graph_relationship_id` — for graph-
        derived enrichments (entity / relationship-linked items).
      * `locator` — open-ended `(type, value)` for paths we don't
        know yet. Empty dict means "no extra locator beyond the
        typed fields."
      * `evidence_text` — short excerpt the producer already has
        in hand. **NEVER** populated with a fresh read from disk —
        the envelope is metadata, not a content store. Capped at
        ~280 chars during normalisation.

    All fields are optional so producers can fill what they know
    and leave the rest. ``has_any_ref()`` is the contract test for
    "does this ref point at anything at all" — used by the
    normaliser to decide whether to emit
    ``WARNING_MISSING_SOURCE_REFS``.
    """

    source_ref_id: str | None = None
    document_id: str | None = None
    snapshot_id: str | None = None
    run_id: str | None = None
    artifact_id: str | None = None
    artifact_kind: str = "unknown"
    chunk_id: str | None = None
    page: int | None = None
    table_id: str | None = None
    image_id: str | None = None
    graph_entity_id: str | None = None
    graph_relationship_id: str | None = None
    locator: Mapping[str, Any] = field(default_factory=dict)
    evidence_text: str = ""

    # Cap on `evidence_text` length. Producers shouldn't ship long
    # excerpts through the envelope — those belong in the source
    # artifact. The cap is enforced by `_clean_evidence_text` below
    # at envelope-construction time.
    _EVIDENCE_TEXT_CAP: int = 280

    def has_any_ref(self) -> bool:
        """True iff this ref points at *something* — artifact id,
        chunk id, page, table id, image id, graph id, or a non-
        empty locator. A ref where every field is None / empty
        contributes nothing and the normaliser treats it as
        absent."""
        return bool(
            self.artifact_id
            or self.chunk_id
            or self.page is not None
            or self.table_id
            or self.image_id
            or self.graph_entity_id
            or self.graph_relationship_id
            or self.locator
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "source_ref_id": self.source_ref_id,
            "document_id": self.document_id,
            "snapshot_id": self.snapshot_id,
            "run_id": self.run_id,
            "artifact_id": self.artifact_id,
            "artifact_kind": self.artifact_kind,
            "chunk_id": self.chunk_id,
            "page": self.page,
            "table_id": self.table_id,
            "image_id": self.image_id,
            "graph_entity_id": self.graph_entity_id,
            "graph_relationship_id": self.graph_relationship_id,
            "locator": dict(self.locator) if self.locator else {},
            "evidence_text": self.evidence_text,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "EnrichmentSourceRef":
        locator = payload.get("locator") or {}
        if not isinstance(locator, Mapping):
            locator = {}
        return cls(
            source_ref_id=_str_or_none(payload.get("source_ref_id")),
            document_id=_str_or_none(payload.get("document_id")),
            snapshot_id=_str_or_none(payload.get("snapshot_id")),
            run_id=_str_or_none(payload.get("run_id")),
            artifact_id=_str_or_none(payload.get("artifact_id")),
            artifact_kind=str(payload.get("artifact_kind") or "unknown"),
            chunk_id=_str_or_none(payload.get("chunk_id")),
            page=_int_or_none(payload.get("page")),
            table_id=_str_or_none(payload.get("table_id")),
            image_id=_str_or_none(payload.get("image_id")),
            graph_entity_id=_str_or_none(payload.get("graph_entity_id")),
            graph_relationship_id=_str_or_none(payload.get("graph_relationship_id")),
            locator=dict(locator),
            evidence_text=_clean_evidence_text(payload.get("evidence_text")),
        )

    @classmethod
    def from_provenance_link(
        cls,
        link: ProvenanceLink,
        *,
        document_id: str | None = None,
        snapshot_id: str | None = None,
        run_id: str | None = None,
    ) -> "EnrichmentSourceRef":
        """Convert the existing `ProvenanceLink` (used by typed
        enrichment overlays) into a `EnrichmentSourceRef`. The two
        shapes overlap on ``source_artifact_id`` + ``source_chunk_id``
        + ``source_kind``; the rest is uniform metadata the caller
        already knows from the artifact-registry record."""
        return cls(
            document_id=document_id,
            snapshot_id=snapshot_id,
            run_id=run_id,
            artifact_id=link.source_artifact_id,
            artifact_kind=link.source_kind or "unknown",
            chunk_id=link.source_chunk_id,
        )


# ---- Producer + derived-from metadata --------------------------------


@dataclass(frozen=True)
class EnrichmentProducer:
    """Producer-side metadata recorded on the envelope.

    Lets a downstream consumer ask "which module produced this and
    what model did it use" without grepping the source. All fields
    optional — producers fill what they know."""

    stage: str = PRODUCER_STAGE_DOMAIN_ENRICHMENT
    module: str | None = None
    version: str | None = None
    model: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "module": self.module,
            "version": self.version,
            "model": self.model,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "EnrichmentProducer":
        return cls(
            stage=str(payload.get("stage") or PRODUCER_STAGE_DOMAIN_ENRICHMENT),
            module=_str_or_none(payload.get("module")),
            version=_str_or_none(payload.get("version")),
            model=_str_or_none(payload.get("model")),
        )

    def is_complete(self) -> bool:
        """True iff producer metadata is rich enough to attribute
        the artifact. Used by the normaliser to decide whether to
        emit ``WARNING_PRODUCER_METADATA_MISSING``."""
        return bool(self.module)


@dataclass(frozen=True)
class DerivedFrom:
    """Back-pointer to the upstream stage(s) the artifact derives
    from. Today this is always compile; future enrichers that
    chain off another enrichment artifact would record the
    upstream enrichment run here.

    `source_artifact_ids` is the **canonical** evidence-set
    pointer — compile artifacts the enricher read at production
    time. Individual ``EnrichmentSourceRef`` entries on the
    envelope are finer-grained pointers INTO those artifacts."""

    stage: str = DERIVED_FROM_STAGE_COMPILE
    run_id: str | None = None
    snapshot_id: str | None = None
    source_artifact_ids: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "run_id": self.run_id,
            "snapshot_id": self.snapshot_id,
            "source_artifact_ids": list(self.source_artifact_ids),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DerivedFrom":
        ids_raw = payload.get("source_artifact_ids") or ()
        ids: tuple[str, ...]
        if isinstance(ids_raw, (list, tuple)):
            ids = tuple(str(x) for x in ids_raw if x is not None)
        else:
            ids = ()
        return cls(
            stage=str(payload.get("stage") or DERIVED_FROM_STAGE_COMPILE),
            run_id=_str_or_none(payload.get("run_id")),
            snapshot_id=_str_or_none(payload.get("snapshot_id")),
            source_artifact_ids=ids,
        )


# ---- The envelope ----------------------------------------------------


@dataclass(frozen=True)
class DerivedEnrichmentArtifact:
    """Common envelope wrapping every post-compile domain
    enrichment artifact.

    Phase 1 boundary: this dataclass is the contract layer. It is
    **never** the persistence shape — producers continue to write
    their inner payload as-is to the artifact registry. The
    envelope is materialised on read by
    ``normalize_enrichment_artifact_payload()``.

    The two flags ``derived`` and ``canonical`` are constants for
    the entire class. They're stamped on the payload so a
    downstream consumer that drops the class wrapper and works on
    the dict can still tell which side of the canonical /
    derived line the artifact is on.

    ``payload`` carries the producer's original payload verbatim.
    Wrappers MUST NOT mutate it; the normaliser stores a shallow
    copy of the input dict so callers that pass the same dict to
    multiple normalisations are safe."""

    artifact_schema: str = DERIVED_ENRICHMENT_ARTIFACT_SCHEMA
    artifact_kind: str = ""
    artifact_id: str | None = None
    domain_id: str | None = None
    document_id: str | None = None
    snapshot_id: str | None = None
    run_id: str | None = None
    # Constants — stamped onto the envelope so dict consumers can
    # still tell derived from canonical without re-importing this
    # module's enum-ish constants.
    canonical: bool = False
    derived: bool = True
    derived_from: DerivedFrom = field(default_factory=DerivedFrom)
    producer: EnrichmentProducer = field(default_factory=EnrichmentProducer)
    confidence: str | None = None
    source_refs: tuple[EnrichmentSourceRef, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        """JSON-friendly dict for transit / persistence preview /
        FE consumption. Round-trips via ``from_payload``."""
        return {
            "artifact_schema": self.artifact_schema,
            "artifact_kind": self.artifact_kind,
            "artifact_id": self.artifact_id,
            "domain_id": self.domain_id,
            "document_id": self.document_id,
            "snapshot_id": self.snapshot_id,
            "run_id": self.run_id,
            "canonical": self.canonical,
            "derived": self.derived,
            "derived_from": self.derived_from.to_payload(),
            "producer": self.producer.to_payload(),
            "confidence": self.confidence,
            "source_refs": [r.to_payload() for r in self.source_refs],
            "payload": _copy_payload(self.payload),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DerivedEnrichmentArtifact":
        refs_raw = payload.get("source_refs") or ()
        refs: tuple[EnrichmentSourceRef, ...]
        if isinstance(refs_raw, (list, tuple)):
            refs = tuple(
                EnrichmentSourceRef.from_payload(r)
                for r in refs_raw
                if isinstance(r, Mapping)
            )
        else:
            refs = ()
        return cls(
            artifact_schema=str(
                payload.get("artifact_schema")
                or DERIVED_ENRICHMENT_ARTIFACT_SCHEMA
            ),
            artifact_kind=str(payload.get("artifact_kind") or ""),
            artifact_id=_str_or_none(payload.get("artifact_id")),
            domain_id=_str_or_none(payload.get("domain_id")),
            document_id=_str_or_none(payload.get("document_id")),
            snapshot_id=_str_or_none(payload.get("snapshot_id")),
            run_id=_str_or_none(payload.get("run_id")),
            # Forced — these are class invariants. We accept the
            # field on the wire so legacy / future tools that store
            # the dict round-trip cleanly, but new code never sets
            # `canonical=True` on a DerivedEnrichmentArtifact.
            canonical=False,
            derived=True,
            derived_from=DerivedFrom.from_payload(
                payload.get("derived_from") or {},
            ),
            producer=EnrichmentProducer.from_payload(
                payload.get("producer") or {},
            ),
            confidence=_str_or_none(payload.get("confidence")),
            source_refs=refs,
            payload=_copy_payload(payload.get("payload") or {}),
            warnings=tuple(
                str(w) for w in (payload.get("warnings") or ())
            ),
        )

    def has_any_source_refs(self) -> bool:
        return any(r.has_any_ref() for r in self.source_refs)


# ---- Known kinds vocabulary -----------------------------------------


# Stable set of artifact kinds the normaliser understands per-shape.
# Kinds outside this set still normalise — the envelope wraps the
# raw payload — but the normaliser emits
# ``WARNING_UNKNOWN_ARTIFACT_KIND`` and skips per-kind source-ref
# extraction.
KNOWN_DERIVED_ENRICHMENT_KINDS: frozenset[str] = frozenset({
    # Per-element enricher kinds (legacy `_LLMBackedEnricher`
    # subclasses in `j1/enrichers.py`).
    "enriched.document_map",
    "enriched.requirements",
    "enriched.tables",
    "enriched.visuals",
    "enriched.formulas",
    "enriched.risks",
    "enriched.consistency_findings",
    "enriched.source_map",
    "enriched.confidence_assessment",
    # Typed overlay (composite runner).
    "enrichment_result",
    # Evidence-backed aliases.
    "domain_enrichment_aliases",
})


# Kinds that are inherently RUN-LEVEL — they summarise the whole
# run rather than pointing at specific chunks. The normaliser
# emits ``WARNING_RUN_LEVEL_SUMMARY_WITHOUT_SOURCE_REFS`` when one
# of these has no per-record source refs, and skips the generic
# missing-source-refs warning. The list is intentional, not
# auto-derived — operators can scan it to spot run-level kinds.
RUN_LEVEL_ENRICHMENT_KINDS: frozenset[str] = frozenset({
    "enriched.confidence_assessment",
})


# ---- Normaliser -----------------------------------------------------


def normalize_enrichment_artifact_payload(
    raw_payload: Mapping[str, Any] | None,
    *,
    artifact_kind: str,
    document_id: str | None = None,
    snapshot_id: str | None = None,
    run_id: str | None = None,
    artifact_id: str | None = None,
    domain_id: str | None = None,
    producer_module: str | None = None,
    producer_version: str | None = None,
    producer_model: str | None = None,
    derived_from_run_id: str | None = None,
    derived_from_snapshot_id: str | None = None,
    derived_from_artifact_ids: Sequence[str] | None = None,
    confidence: str | None = None,
) -> DerivedEnrichmentArtifact:
    """Normalise a raw enrichment artifact payload into the common
    envelope.

    Inputs:
      * ``raw_payload`` — the producer's payload exactly as it was
        persisted (e.g. ``{"requirements": [...], "source_artifact_id":
        "art-xyz"}`` for ``enriched.requirements``). ``None`` is
        tolerated and treated as an empty mapping.
      * ``artifact_kind`` — wire string (``"enriched.requirements"``,
        ``"enrichment_result"``, etc.). Unknown kinds normalise but
        emit ``WARNING_UNKNOWN_ARTIFACT_KIND``.
      * Lineage args — ``document_id`` / ``snapshot_id`` / ``run_id``
        come from the artifact-registry record at the call site, NOT
        from the payload. The normaliser stamps them on the envelope
        + on each source ref so future consumers can resolve refs
        without going back to the registry.
      * Producer args — when the call site knows the producer module
        / version / model, pass them in; otherwise the normaliser
        emits ``WARNING_PRODUCER_METADATA_MISSING``.
      * ``derived_from_*`` — typically the compile run that produced
        the source artifacts the enricher read. When omitted, falls
        back to ``run_id`` / ``snapshot_id`` so the envelope is
        always self-consistent.

    Idempotency: when ``raw_payload`` is already an envelope
    payload (``artifact_schema == DERIVED_ENRICHMENT_ARTIFACT_SCHEMA``),
    the function deserialises it via ``DerivedEnrichmentArtifact.from_payload``
    and returns it. No warning is emitted for the already-wrapped
    case; the envelope is the contract regardless of how it
    arrived. Lineage fields supplied by the call site override
    only when the envelope's own field is ``None`` — the in-payload
    value wins for forward-compat with future producers that
    materialise the envelope server-side.

    Source-ref extraction is per-kind (see ``_extract_source_refs``).
    The normaliser never invents source refs: a producer that
    didn't ship them gets an empty ``source_refs`` tuple + the
    appropriate warning."""
    payload = dict(raw_payload or {})
    warnings: list[str] = []

    # Already-wrapped payload? Round-trip via from_payload.
    if (
        isinstance(payload.get("artifact_schema"), str)
        and payload["artifact_schema"] == DERIVED_ENRICHMENT_ARTIFACT_SCHEMA
    ):
        envelope = DerivedEnrichmentArtifact.from_payload(payload)
        # Fill in lineage / domain / artifact_id from the call site
        # ONLY when missing on the wrapped envelope. Producer-supplied
        # values win — that's the forward-compat contract.
        envelope = _backfill_envelope_lineage(
            envelope,
            document_id=document_id,
            snapshot_id=snapshot_id,
            run_id=run_id,
            artifact_id=artifact_id,
            domain_id=domain_id,
        )
        return envelope

    # New normalisation path.
    known_kind = artifact_kind in KNOWN_DERIVED_ENRICHMENT_KINDS
    if not known_kind:
        warnings.append(WARNING_UNKNOWN_ARTIFACT_KIND)
        source_refs: tuple[EnrichmentSourceRef, ...] = ()
    else:
        source_refs = _extract_source_refs(
            payload=payload,
            artifact_kind=artifact_kind,
            document_id=document_id,
            snapshot_id=snapshot_id,
            run_id=run_id,
        )

    # Missing-source-refs vs run-level-summary warnings — emit
    # exactly one, never both. Run-level kinds are EXPECTED to be
    # ref-less; everything else gets the generic warning.
    if known_kind and not source_refs:
        if artifact_kind in RUN_LEVEL_ENRICHMENT_KINDS:
            warnings.append(WARNING_RUN_LEVEL_SUMMARY_WITHOUT_SOURCE_REFS)
        else:
            warnings.append(WARNING_MISSING_SOURCE_REFS)

    # Resolve producer metadata. The fallback `module` is the
    # `model` field on the payload when present — some legacy
    # enrichers stamp `model` / `provider` directly on the payload
    # without naming the producing module.
    inferred_module = producer_module or _infer_producer_module(payload)
    inferred_model = producer_model or _str_or_none(payload.get("model"))
    producer = EnrichmentProducer(
        stage=PRODUCER_STAGE_DOMAIN_ENRICHMENT,
        module=inferred_module,
        version=producer_version,
        model=inferred_model,
    )
    if not producer.is_complete():
        warnings.append(WARNING_PRODUCER_METADATA_MISSING)

    # Resolve `derived_from`. The enricher reads compile artifacts;
    # by convention the upstream stage is `compile`. The upstream
    # `run_id` / `snapshot_id` default to THIS artifact's lineage
    # because in the single-snapshot case the enrichment runs in
    # the same snapshot as the compile that produced the source
    # artifacts. `derived_from_artifact_ids` defaults to the
    # `source_artifact_id` field on the payload (the dominant
    # convention in legacy enrichers).
    df_source_ids: tuple[str, ...]
    if derived_from_artifact_ids is not None:
        df_source_ids = tuple(
            str(x) for x in derived_from_artifact_ids if x is not None
        )
    else:
        raw_sid = _str_or_none(payload.get("source_artifact_id"))
        df_source_ids = (raw_sid,) if raw_sid else ()
    derived_from = DerivedFrom(
        stage=DERIVED_FROM_STAGE_COMPILE,
        run_id=derived_from_run_id or run_id,
        snapshot_id=derived_from_snapshot_id or snapshot_id,
        source_artifact_ids=df_source_ids,
    )

    # Every payload arriving on this branch was NOT an envelope —
    # by definition that's a "legacy" payload being normalised on
    # read. The warning surfaces that fact so dashboards can
    # distinguish envelope-native producers (future) from current
    # legacy producers.
    warnings.append(WARNING_LEGACY_PAYLOAD_NORMALIZED)

    return DerivedEnrichmentArtifact(
        artifact_schema=DERIVED_ENRICHMENT_ARTIFACT_SCHEMA,
        artifact_kind=artifact_kind,
        artifact_id=artifact_id,
        domain_id=domain_id,
        document_id=document_id,
        snapshot_id=snapshot_id,
        run_id=run_id,
        canonical=False,
        derived=True,
        derived_from=derived_from,
        producer=producer,
        confidence=confidence,
        source_refs=source_refs,
        payload=_copy_payload(payload),
        warnings=tuple(warnings),
    )


# ---- Per-kind source-ref extraction ---------------------------------


def _extract_source_refs(
    *,
    payload: Mapping[str, Any],
    artifact_kind: str,
    document_id: str | None,
    snapshot_id: str | None,
    run_id: str | None,
) -> tuple[EnrichmentSourceRef, ...]:
    """Dispatch to per-kind extractor. Adding a new kind: add the
    extractor + its `if` branch here, register the kind string in
    ``KNOWN_DERIVED_ENRICHMENT_KINDS`` above, and add a contract
    test under ``tests/test_derived_enrichment_artifact_contract.py``."""

    if artifact_kind == "enriched.requirements":
        return _refs_for_per_item_list(
            payload, list_key="requirements",
            document_id=document_id, snapshot_id=snapshot_id, run_id=run_id,
        )
    if artifact_kind == "enriched.risks":
        return _refs_for_per_item_list(
            payload, list_key="risks",
            document_id=document_id, snapshot_id=snapshot_id, run_id=run_id,
        )
    if artifact_kind == "enriched.tables":
        return _refs_for_per_item_list(
            payload, list_key="tables",
            document_id=document_id, snapshot_id=snapshot_id, run_id=run_id,
            table_id_key="table_id",
        )
    if artifact_kind == "enriched.formulas":
        return _refs_for_per_item_list(
            payload, list_key="formulas",
            document_id=document_id, snapshot_id=snapshot_id, run_id=run_id,
        )
    if artifact_kind == "enriched.consistency_findings":
        return _refs_for_per_item_list(
            payload, list_key="findings",
            document_id=document_id, snapshot_id=snapshot_id, run_id=run_id,
        )
    if artifact_kind == "enriched.visuals":
        return _refs_for_visuals(
            payload,
            document_id=document_id, snapshot_id=snapshot_id, run_id=run_id,
        )
    if artifact_kind == "enriched.document_map":
        return _refs_for_document_map(
            payload,
            document_id=document_id, snapshot_id=snapshot_id, run_id=run_id,
        )
    if artifact_kind == "enriched.source_map":
        return _refs_for_source_map(
            payload,
            document_id=document_id, snapshot_id=snapshot_id, run_id=run_id,
        )
    if artifact_kind == "enriched.confidence_assessment":
        # Run-level summary — no per-item source refs by contract.
        # The producer stamps `source_artifact_id` only; we still
        # surface that as a single coarse ref so consumers see the
        # compile artifact this assessment scored.
        return _refs_for_run_level_with_artifact(
            payload,
            document_id=document_id, snapshot_id=snapshot_id, run_id=run_id,
        )
    if artifact_kind == "enrichment_result":
        return _refs_for_enrichment_result(
            payload,
            document_id=document_id, snapshot_id=snapshot_id, run_id=run_id,
        )
    if artifact_kind == "domain_enrichment_aliases":
        return _refs_for_alias_artifact(payload)

    # Defensive: should never reach this branch — caller checked
    # `KNOWN_DERIVED_ENRICHMENT_KINDS` first. Return empty so the
    # caller still produces a valid envelope.
    return ()


def _refs_for_per_item_list(
    payload: Mapping[str, Any],
    *,
    list_key: str,
    document_id: str | None,
    snapshot_id: str | None,
    run_id: str | None,
    table_id_key: str | None = None,
) -> tuple[EnrichmentSourceRef, ...]:
    """Per-item extractor for kinds where the payload is
    ``{list_key: [item, item, ...], "source_artifact_id": "..."}``.

    Each item may carry ``page``, ``chunk_id``, ``section`` —
    extracted into the per-item source ref. The top-level
    ``source_artifact_id`` is used for ``artifact_id`` so every
    ref points back at the compile artifact the producer read."""
    items = payload.get(list_key) or ()
    if not isinstance(items, (list, tuple)):
        return ()
    source_artifact_id = _str_or_none(payload.get("source_artifact_id"))
    refs: list[EnrichmentSourceRef] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        chunk_id = _str_or_none(
            item.get("chunk_id") or item.get("source_chunk_id"),
        )
        page = _int_or_none(item.get("page"))
        table_id = (
            _str_or_none(item.get(table_id_key))
            if table_id_key else None
        )
        evidence_text = _clean_evidence_text(
            item.get("evidence") or item.get("snippet"),
        )
        if not (chunk_id or page is not None or table_id):
            # No item-level ref shape — skip. The coarse
            # artifact-level pointer is emitted once below when
            # no item produced a ref. Replicating the coarse
            # pointer N times for N item-refless items would
            # inflate `source_refs` without adding information.
            continue
        refs.append(EnrichmentSourceRef(
            document_id=document_id,
            snapshot_id=snapshot_id,
            run_id=run_id,
            artifact_id=source_artifact_id,
            artifact_kind="compiled_text",
            chunk_id=chunk_id,
            page=page,
            table_id=table_id,
            evidence_text=evidence_text,
        ))
    # If no per-item refs but the payload carried a source artifact
    # id, emit a single coarse ref so the artifact still resolves
    # to compile evidence at envelope level.
    if not refs and source_artifact_id:
        refs.append(EnrichmentSourceRef(
            document_id=document_id,
            snapshot_id=snapshot_id,
            run_id=run_id,
            artifact_id=source_artifact_id,
            artifact_kind="compiled_text",
        ))
    return tuple(refs)


def _refs_for_visuals(
    payload: Mapping[str, Any],
    *,
    document_id: str | None,
    snapshot_id: str | None,
    run_id: str | None,
) -> tuple[EnrichmentSourceRef, ...]:
    visuals = payload.get("visuals") or ()
    if not isinstance(visuals, (list, tuple)):
        return ()
    source_artifact_id = _str_or_none(payload.get("source_artifact_id"))
    refs: list[EnrichmentSourceRef] = []
    for visual in visuals:
        if not isinstance(visual, Mapping):
            continue
        image_artifact_id = _str_or_none(visual.get("artifact_id"))
        page = _int_or_none(visual.get("page"))
        refs.append(EnrichmentSourceRef(
            document_id=document_id,
            snapshot_id=snapshot_id,
            run_id=run_id,
            artifact_id=image_artifact_id or source_artifact_id,
            artifact_kind="image" if image_artifact_id else "compiled_text",
            image_id=image_artifact_id,
            page=page,
        ))
    return tuple(refs)


def _refs_for_document_map(
    payload: Mapping[str, Any],
    *,
    document_id: str | None,
    snapshot_id: str | None,
    run_id: str | None,
) -> tuple[EnrichmentSourceRef, ...]:
    sections = payload.get("sections") or ()
    source_artifact_id = _str_or_none(payload.get("source_artifact_id"))
    if not isinstance(sections, (list, tuple)):
        return ()
    refs: list[EnrichmentSourceRef] = []
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        page_start = _int_or_none(section.get("page_start"))
        if page_start is None:
            continue
        refs.append(EnrichmentSourceRef(
            document_id=document_id,
            snapshot_id=snapshot_id,
            run_id=run_id,
            artifact_id=source_artifact_id,
            artifact_kind="compiled_text",
            page=page_start,
            locator=(
                {"type": "page_range", "page_end": section.get("page_end")}
                if section.get("page_end") is not None else {}
            ),
        ))
    if not refs and source_artifact_id:
        refs.append(EnrichmentSourceRef(
            document_id=document_id,
            snapshot_id=snapshot_id,
            run_id=run_id,
            artifact_id=source_artifact_id,
            artifact_kind="compiled_text",
        ))
    return tuple(refs)


def _refs_for_source_map(
    payload: Mapping[str, Any],
    *,
    document_id: str | None,
    snapshot_id: str | None,
    run_id: str | None,
) -> tuple[EnrichmentSourceRef, ...]:
    sources = payload.get("sources") or ()
    if not isinstance(sources, (list, tuple)):
        return ()
    refs: list[EnrichmentSourceRef] = []
    for entry in sources:
        if not isinstance(entry, Mapping):
            continue
        aid = _str_or_none(entry.get("artifact_id"))
        if not aid:
            continue
        refs.append(EnrichmentSourceRef(
            document_id=document_id,
            snapshot_id=snapshot_id,
            run_id=run_id,
            artifact_id=aid,
            artifact_kind=str(entry.get("artifact_kind") or "compiled_text"),
        ))
    return tuple(refs)


def _refs_for_run_level_with_artifact(
    payload: Mapping[str, Any],
    *,
    document_id: str | None,
    snapshot_id: str | None,
    run_id: str | None,
) -> tuple[EnrichmentSourceRef, ...]:
    aid = _str_or_none(payload.get("source_artifact_id"))
    if not aid:
        return ()
    return (EnrichmentSourceRef(
        document_id=document_id,
        snapshot_id=snapshot_id,
        run_id=run_id,
        artifact_id=aid,
        artifact_kind="compiled_text",
    ),)


def _refs_for_enrichment_result(
    payload: Mapping[str, Any],
    *,
    document_id: str | None,
    snapshot_id: str | None,
    run_id: str | None,
) -> tuple[EnrichmentSourceRef, ...]:
    """`enrichment_result` is the typed overlay. Source refs live
    on each module outcome's ``provenance`` field (a
    ``ProvenanceLink`` shape). We surface them flattened at the
    envelope level so consumers see the union of refs across all
    modules. Per-module breakdowns stay accessible via the inner
    payload."""
    refs: list[EnrichmentSourceRef] = []
    outcomes = payload.get("module_outcomes") or ()
    if isinstance(outcomes, (list, tuple)):
        for outcome in outcomes:
            if not isinstance(outcome, Mapping):
                continue
            prov = outcome.get("provenance")
            if isinstance(prov, Mapping):
                refs.append(_provenance_dict_to_ref(
                    prov, document_id=document_id,
                    snapshot_id=snapshot_id, run_id=run_id,
                ))
            elif isinstance(prov, (list, tuple)):
                for entry in prov:
                    if isinstance(entry, Mapping):
                        refs.append(_provenance_dict_to_ref(
                            entry, document_id=document_id,
                            snapshot_id=snapshot_id, run_id=run_id,
                        ))
    # Filter out empty refs — `ProvenanceLink` with all-None fields
    # is the default factory value and shouldn't pollute the
    # envelope's source-ref set.
    return tuple(r for r in refs if r.has_any_ref())


def _refs_for_alias_artifact(
    payload: Mapping[str, Any],
) -> tuple[EnrichmentSourceRef, ...]:
    """`domain_enrichment_aliases` carries a list of aliases with
    typed evidence per occurrence. Each `evidence` dict already
    has document_id / snapshot_id / run_id / artifact_id /
    chunk_id / page — those win over the artifact-level lineage
    args because aliases can cite across documents in future."""
    aliases = payload.get("aliases") or ()
    if not isinstance(aliases, (list, tuple)):
        return ()
    refs: list[EnrichmentSourceRef] = []
    for alias in aliases:
        if not isinstance(alias, Mapping):
            continue
        evidence = alias.get("evidence")
        if not isinstance(evidence, Mapping):
            continue
        if not _evidence_has_any_ref(evidence):
            continue
        refs.append(EnrichmentSourceRef(
            document_id=_str_or_none(evidence.get("document_id")),
            snapshot_id=_str_or_none(evidence.get("snapshot_id")),
            run_id=_str_or_none(evidence.get("run_id")),
            artifact_id=_str_or_none(evidence.get("artifact_id")),
            artifact_kind="chunk",
            chunk_id=_str_or_none(evidence.get("chunk_id")),
            page=_int_or_none(evidence.get("page")),
            evidence_text=_clean_evidence_text(evidence.get("snippet")),
        ))
    return tuple(refs)


def _evidence_has_any_ref(evidence: Mapping[str, Any]) -> bool:
    return bool(
        evidence.get("artifact_id")
        or evidence.get("chunk_id")
        or evidence.get("page") is not None
        or evidence.get("document_id")
    )


def _provenance_dict_to_ref(
    prov: Mapping[str, Any],
    *,
    document_id: str | None,
    snapshot_id: str | None,
    run_id: str | None,
) -> EnrichmentSourceRef:
    return EnrichmentSourceRef(
        document_id=document_id,
        snapshot_id=snapshot_id,
        run_id=run_id,
        artifact_id=_str_or_none(prov.get("source_artifact_id")),
        artifact_kind=str(prov.get("source_kind") or "unknown"),
        chunk_id=_str_or_none(prov.get("source_chunk_id")),
    )


# ---- Internal helpers -----------------------------------------------


def _backfill_envelope_lineage(
    envelope: DerivedEnrichmentArtifact,
    *,
    document_id: str | None,
    snapshot_id: str | None,
    run_id: str | None,
    artifact_id: str | None,
    domain_id: str | None,
) -> DerivedEnrichmentArtifact:
    """Fill missing lineage fields on an already-wrapped envelope.
    Producer-supplied values win — backfill ONLY when the envelope
    field is None. Used by the idempotent re-normalisation path."""
    from dataclasses import replace
    return replace(
        envelope,
        document_id=envelope.document_id or document_id,
        snapshot_id=envelope.snapshot_id or snapshot_id,
        run_id=envelope.run_id or run_id,
        artifact_id=envelope.artifact_id or artifact_id,
        domain_id=envelope.domain_id or domain_id,
    )


def _infer_producer_module(payload: Mapping[str, Any]) -> str | None:
    """Best-effort module name from the payload itself. Returns
    None when the payload doesn't expose one — the caller logs
    ``WARNING_PRODUCER_METADATA_MISSING``."""
    for key in ("producer_module", "module", "producer"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, Mapping):
            inner = val.get("module")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_evidence_text(value: Any) -> str:
    if not value:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    cap = EnrichmentSourceRef._EVIDENCE_TEXT_CAP
    if len(s) > cap:
        return s[: cap - 1] + "…"  # Unicode ellipsis
    return s


def _copy_payload(value: Any) -> dict[str, Any]:
    """Shallow copy of the payload dict. Producers / call sites
    may pass the same dict to multiple normalisations; we don't
    want mutations through the envelope to leak back."""
    if isinstance(value, Mapping):
        return dict(value)
    return {}
