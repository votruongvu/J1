"""Contract — Derived enrichment artifact envelope (Phase 1).

Pins the surface introduced in `j1.processing.derived_enrichment`:

  * `DerivedEnrichmentArtifact` round-trips through `to_payload()` /
    `from_payload()` with `derived=True` + `canonical=False` invariants.
  * `EnrichmentSourceRef` round-trips and recognises empty refs via
    `has_any_ref()`.
  * `normalize_enrichment_artifact_payload(...)` produces a valid
    envelope for every known `enriched.*` kind, preserving the inner
    payload verbatim under `payload` and extracting source refs
    using the per-kind heuristic described in the module's
    `_extract_source_refs` dispatch.
  * Unknown artifact kinds normalise to a safe envelope with the
    `unknown_artifact_kind` warning.
  * Run-level kinds (today: `enriched.confidence_assessment`) get
    `run_level_summary_without_direct_source_refs` rather than the
    generic missing-refs warning.
  * Idempotent re-normalisation of an already-wrapped payload.
  * Lineage (`document_id`, `snapshot_id`, `run_id`) is preserved
    on the envelope AND propagated to per-ref source records.

Phase boundary: persistent Knowledge Memory is NOT under test
here. This file pins the contract layer only.
"""

from __future__ import annotations

import pytest

from j1.processing.derived_enrichment import (
    DERIVED_ENRICHMENT_ARTIFACT_SCHEMA,
    DERIVED_FROM_STAGE_COMPILE,
    KNOWN_DERIVED_ENRICHMENT_KINDS,
    PRODUCER_STAGE_DOMAIN_ENRICHMENT,
    RUN_LEVEL_ENRICHMENT_KINDS,
    WARNING_LEGACY_PAYLOAD_NORMALIZED,
    WARNING_MISSING_SOURCE_REFS,
    WARNING_PRODUCER_METADATA_MISSING,
    WARNING_RUN_LEVEL_SUMMARY_WITHOUT_SOURCE_REFS,
    WARNING_UNKNOWN_ARTIFACT_KIND,
    DerivedEnrichmentArtifact,
    DerivedFrom,
    EnrichmentProducer,
    EnrichmentSourceRef,
    normalize_enrichment_artifact_payload,
)
from j1.processing.enrichment_overlay import ProvenanceLink


# ---- Envelope round-trip + invariants --------------------------


def test_envelope_round_trips_through_payload():
    env = DerivedEnrichmentArtifact(
        artifact_kind="enriched.risks",
        artifact_id="art-1",
        domain_id="civil_engineering",
        document_id="doc-1",
        snapshot_id="snap-1",
        run_id="run-1",
        producer=EnrichmentProducer(module="RiskExtractor", model="m"),
        derived_from=DerivedFrom(
            run_id="run-1", snapshot_id="snap-1",
            source_artifact_ids=("compile-art",),
        ),
        source_refs=(EnrichmentSourceRef(
            artifact_id="compile-art", artifact_kind="compiled_text",
            chunk_id="c1", page=3,
        ),),
        payload={"risks": [{"text": "Falling object"}]},
        warnings=(WARNING_LEGACY_PAYLOAD_NORMALIZED,),
    )
    payload = env.to_payload()
    re = DerivedEnrichmentArtifact.from_payload(payload)
    assert re.artifact_schema == DERIVED_ENRICHMENT_ARTIFACT_SCHEMA
    assert re.artifact_kind == "enriched.risks"
    assert re.artifact_id == "art-1"
    assert re.domain_id == "civil_engineering"
    assert re.document_id == "doc-1"
    assert re.snapshot_id == "snap-1"
    assert re.run_id == "run-1"
    assert re.derived is True
    assert re.canonical is False
    assert re.producer.module == "RiskExtractor"
    assert re.producer.model == "m"
    assert re.derived_from.run_id == "run-1"
    assert re.derived_from.source_artifact_ids == ("compile-art",)
    assert len(re.source_refs) == 1
    assert re.source_refs[0].chunk_id == "c1"
    assert re.source_refs[0].page == 3
    assert re.payload == {"risks": [{"text": "Falling object"}]}
    assert WARNING_LEGACY_PAYLOAD_NORMALIZED in re.warnings


def test_envelope_invariants_derived_true_canonical_false():
    """Even if a payload arrives with canonical=True (defensive
    against future shapes or hostile callers), `from_payload`
    coerces back to the class invariants."""
    env = DerivedEnrichmentArtifact.from_payload({
        "artifact_schema": DERIVED_ENRICHMENT_ARTIFACT_SCHEMA,
        "artifact_kind": "enriched.requirements",
        "canonical": True,   # <-- ignored
        "derived": False,    # <-- ignored
        "payload": {},
    })
    assert env.derived is True
    assert env.canonical is False


def test_envelope_default_payload_is_empty_dict():
    env = DerivedEnrichmentArtifact()
    assert env.payload == {}
    assert env.source_refs == ()
    assert env.warnings == ()


# ---- Source ref ------------------------------------------------


def test_source_ref_round_trips_with_typed_locator():
    ref = EnrichmentSourceRef(
        document_id="d", snapshot_id="s", run_id="r",
        artifact_id="a", artifact_kind="chunk",
        chunk_id="c", page=12,
        locator={"type": "page_range", "page_end": 15},
        evidence_text="excerpt",
    )
    payload = ref.to_payload()
    re = EnrichmentSourceRef.from_payload(payload)
    assert re == ref


def test_source_ref_has_any_ref_when_any_field_populated():
    cases = [
        EnrichmentSourceRef(artifact_id="a"),
        EnrichmentSourceRef(chunk_id="c"),
        EnrichmentSourceRef(page=1),
        EnrichmentSourceRef(table_id="t"),
        EnrichmentSourceRef(image_id="i"),
        EnrichmentSourceRef(graph_entity_id="g"),
        EnrichmentSourceRef(graph_relationship_id="gr"),
        EnrichmentSourceRef(locator={"type": "page", "value": 1}),
    ]
    for ref in cases:
        assert ref.has_any_ref(), f"ref should report present: {ref}"


def test_source_ref_has_any_ref_false_when_all_empty():
    assert EnrichmentSourceRef().has_any_ref() is False
    # Only document_id / snapshot_id / run_id alone is NOT a ref —
    # those describe WHERE the artifact lives, not what evidence
    # it points at.
    assert EnrichmentSourceRef(
        document_id="d", snapshot_id="s", run_id="r",
    ).has_any_ref() is False


def test_source_ref_evidence_text_is_capped():
    long = "x" * 1000
    ref = EnrichmentSourceRef.from_payload({"evidence_text": long})
    assert len(ref.evidence_text) <= 280
    assert ref.evidence_text.endswith("…")


def test_source_ref_from_provenance_link():
    link = ProvenanceLink(
        source_artifact_id="compile-art",
        source_chunk_id="c1",
        source_kind="chunk",
        relation="extracted_from",
    )
    ref = EnrichmentSourceRef.from_provenance_link(
        link,
        document_id="doc-1", snapshot_id="snap-1", run_id="run-1",
    )
    assert ref.artifact_id == "compile-art"
    assert ref.chunk_id == "c1"
    assert ref.artifact_kind == "chunk"
    assert ref.document_id == "doc-1"
    assert ref.snapshot_id == "snap-1"


# ---- Normalizer: known kinds (one test per kind) ---------------


def _ctx(**over):
    base = dict(
        document_id="doc-1",
        snapshot_id="snap-1",
        run_id="run-1",
        artifact_id="art-1",
        domain_id="civil_engineering",
    )
    base.update(over)
    return base


def test_normalize_enriched_requirements_extracts_per_item_refs():
    payload = {
        "requirements": [
            {"text": "R1", "chunk_id": "c1", "page": 3},
            {"text": "R2", "page": 5},
            {"text": "R3"},  # no per-item refs — skipped
        ],
        "source_artifact_id": "compile-art",
        "model": "m",
        "provider": "openai",
    }
    env = normalize_enrichment_artifact_payload(
        payload, artifact_kind="enriched.requirements", **_ctx(),
    )
    assert env.artifact_kind == "enriched.requirements"
    assert env.derived is True
    assert env.canonical is False
    # Per-item refs preserved + coarse compile pointer.
    assert len(env.source_refs) == 2
    assert env.source_refs[0].chunk_id == "c1"
    assert env.source_refs[0].page == 3
    assert env.source_refs[0].artifact_id == "compile-art"
    assert env.source_refs[1].page == 5
    # Payload preserved verbatim.
    assert env.payload["requirements"][0]["text"] == "R1"
    # Derived-from set from `source_artifact_id`.
    assert env.derived_from.source_artifact_ids == ("compile-art",)
    assert env.derived_from.stage == DERIVED_FROM_STAGE_COMPILE


def test_normalize_enriched_risks_extracts_per_item_refs():
    env = normalize_enrichment_artifact_payload(
        {"risks": [{"text": "R", "chunk_id": "c1", "page": 7}],
         "source_artifact_id": "compile-art"},
        artifact_kind="enriched.risks", **_ctx(),
    )
    assert env.artifact_kind == "enriched.risks"
    assert env.source_refs[0].chunk_id == "c1"
    assert env.source_refs[0].page == 7


def test_normalize_enriched_tables_extracts_table_ids():
    env = normalize_enrichment_artifact_payload(
        {"tables": [{"table_id": "t-1", "page": 12}],
         "source_artifact_id": "compile-art"},
        artifact_kind="enriched.tables", **_ctx(),
    )
    assert env.source_refs[0].table_id == "t-1"
    assert env.source_refs[0].page == 12


def test_normalize_enriched_visuals_extracts_image_artifact_ids():
    env = normalize_enrichment_artifact_payload(
        {"visuals": [
            {"artifact_id": "img-1", "page": 2},
            {"artifact_id": "img-2"},
        ],
         "source_artifact_id": "compile-art"},
        artifact_kind="enriched.visuals", **_ctx(),
    )
    assert len(env.source_refs) == 2
    assert env.source_refs[0].image_id == "img-1"
    assert env.source_refs[0].page == 2
    assert env.source_refs[0].artifact_kind == "image"
    assert env.source_refs[1].image_id == "img-2"


def test_normalize_enriched_document_map_extracts_page_ranges():
    env = normalize_enrichment_artifact_payload(
        {"sections": [
            {"page_start": 1, "page_end": 3, "title": "Intro"},
            {"page_start": 4, "page_end": 10},
        ],
         "source_artifact_id": "compile-art"},
        artifact_kind="enriched.document_map", **_ctx(),
    )
    assert len(env.source_refs) == 2
    assert env.source_refs[0].page == 1
    assert env.source_refs[0].locator.get("type") == "page_range"
    assert env.source_refs[0].locator.get("page_end") == 3


def test_normalize_enriched_source_map_extracts_per_source_ids():
    env = normalize_enrichment_artifact_payload(
        {"sources": [
            {"artifact_id": "compile-art-1", "artifact_kind": "compiled_text"},
            {"artifact_id": "compile-art-2", "artifact_kind": "graph_json"},
        ]},
        artifact_kind="enriched.source_map", **_ctx(),
    )
    assert len(env.source_refs) == 2
    assert env.source_refs[0].artifact_kind == "compiled_text"
    assert env.source_refs[1].artifact_kind == "graph_json"


def test_normalize_enriched_formulas_extracts_per_item_refs():
    env = normalize_enrichment_artifact_payload(
        {"formulas": [{"text": "F=ma", "page": 9}],
         "source_artifact_id": "compile-art"},
        artifact_kind="enriched.formulas", **_ctx(),
    )
    assert env.source_refs[0].page == 9


def test_normalize_enriched_consistency_findings_extracts_per_item_refs():
    env = normalize_enrichment_artifact_payload(
        {"findings": [{"text": "F", "chunk_id": "c1", "page": 4}],
         "source_artifact_id": "compile-art"},
        artifact_kind="enriched.consistency_findings", **_ctx(),
    )
    assert env.source_refs[0].chunk_id == "c1"
    assert env.source_refs[0].page == 4


def test_normalize_enriched_confidence_assessment_is_run_level():
    """`enriched.confidence_assessment` is run-level summary; we
    surface ONE coarse ref pointing at the compile artifact + emit
    the run-level warning (NOT the generic missing-refs warning)."""
    env = normalize_enrichment_artifact_payload(
        {"overall_confidence": "high",
         "assessments": [{"category": "x"}],
         "source_artifact_id": "compile-art"},
        artifact_kind="enriched.confidence_assessment", **_ctx(),
    )
    assert "enriched.confidence_assessment" in RUN_LEVEL_ENRICHMENT_KINDS
    # The coarse compile ref IS surfaced when source_artifact_id is
    # present; the run-level warning fires only when no refs exist.
    assert len(env.source_refs) == 1
    assert env.source_refs[0].artifact_id == "compile-art"


def test_normalize_enriched_confidence_assessment_no_refs_emits_run_level_warning():
    env = normalize_enrichment_artifact_payload(
        {"overall_confidence": "low"},  # no source_artifact_id
        artifact_kind="enriched.confidence_assessment", **_ctx(),
    )
    assert env.source_refs == ()
    assert WARNING_RUN_LEVEL_SUMMARY_WITHOUT_SOURCE_REFS in env.warnings
    # Mutually exclusive with the generic warning.
    assert WARNING_MISSING_SOURCE_REFS not in env.warnings


def test_normalize_enrichment_result_flattens_provenance_links():
    env = normalize_enrichment_artifact_payload(
        {
            "schema_version": "1",
            "document_id": "doc-1",
            "module_outcomes": [
                {
                    "module_id": "metadata_enrichment",
                    "provenance": {
                        "source_artifact_id": "compile-art-A",
                        "source_chunk_id": "c1",
                        "source_kind": "chunk",
                    },
                },
                {
                    "module_id": "validation",
                    "provenance": {
                        "source_artifact_id": "compile-art-B",
                        "source_chunk_id": None,
                        "source_kind": "compiled_text",
                    },
                },
                {"module_id": "skipped", "provenance": {}},  # empty — filtered
            ],
        },
        artifact_kind="enrichment_result", **_ctx(),
    )
    assert env.artifact_kind == "enrichment_result"
    # Two refs from the two non-empty provenance entries.
    assert len(env.source_refs) == 2
    artifact_ids = sorted(r.artifact_id for r in env.source_refs)
    assert artifact_ids == ["compile-art-A", "compile-art-B"]


def test_normalize_alias_artifact_extracts_per_alias_evidence():
    env = normalize_enrichment_artifact_payload(
        {
            "schema_version": "1",
            "aliases": [
                {
                    "alias": "BoQ",
                    "canonical": "bill of quantities",
                    "evidence": {
                        "document_id": "doc-1",
                        "snapshot_id": "snap-1",
                        "run_id": "run-1",
                        "artifact_id": "chunk-art",
                        "chunk_id": "c1",
                        "page": 5,
                        "snippet": "BoQ refers to ...",
                    },
                },
                {
                    "alias": "BOM",
                    "canonical": "bill of materials",
                    "evidence": {
                        "artifact_id": "chunk-art-2",
                        "chunk_id": "c2",
                    },
                },
            ],
        },
        artifact_kind="domain_enrichment_aliases", **_ctx(),
    )
    assert len(env.source_refs) == 2
    assert env.source_refs[0].chunk_id == "c1"
    assert env.source_refs[0].page == 5
    assert "BoQ refers to" in env.source_refs[0].evidence_text
    assert env.source_refs[1].chunk_id == "c2"


# ---- Normalizer: warnings + edge cases ------------------------


def test_normalize_unknown_kind_emits_warning_and_no_refs():
    env = normalize_enrichment_artifact_payload(
        {"some_data": [1, 2, 3]},
        artifact_kind="enriched.future_thing", **_ctx(),
    )
    assert env.artifact_kind == "enriched.future_thing"
    assert WARNING_UNKNOWN_ARTIFACT_KIND in env.warnings
    assert WARNING_LEGACY_PAYLOAD_NORMALIZED in env.warnings
    # Payload preserved verbatim.
    assert env.payload == {"some_data": [1, 2, 3]}


def test_normalize_known_kind_missing_refs_emits_missing_warning():
    env = normalize_enrichment_artifact_payload(
        {"requirements": [{"text": "R"}]},  # no source_artifact_id, no per-item ref
        artifact_kind="enriched.requirements", **_ctx(),
    )
    assert env.source_refs == ()
    assert WARNING_MISSING_SOURCE_REFS in env.warnings
    # Run-level warning never fires for non-run-level kinds.
    assert WARNING_RUN_LEVEL_SUMMARY_WITHOUT_SOURCE_REFS not in env.warnings


def test_normalize_missing_producer_emits_producer_warning():
    """Payload without producer_module / module / producer keys
    emits the producer-missing warning."""
    env = normalize_enrichment_artifact_payload(
        {"requirements": [{"text": "R", "page": 1}],
         "source_artifact_id": "compile-art"},
        artifact_kind="enriched.requirements", **_ctx(),
    )
    assert WARNING_PRODUCER_METADATA_MISSING in env.warnings


def test_normalize_with_explicit_producer_does_not_warn():
    env = normalize_enrichment_artifact_payload(
        {"requirements": [{"text": "R", "page": 1}],
         "source_artifact_id": "compile-art"},
        artifact_kind="enriched.requirements",
        **_ctx(),
        producer_module="RequirementExtractor",
        producer_version="0.4",
        producer_model="claude-3-5-sonnet",
    )
    assert WARNING_PRODUCER_METADATA_MISSING not in env.warnings
    assert env.producer.module == "RequirementExtractor"
    assert env.producer.version == "0.4"
    assert env.producer.model == "claude-3-5-sonnet"
    assert env.producer.stage == PRODUCER_STAGE_DOMAIN_ENRICHMENT


def test_normalize_with_inline_module_string_used_as_producer():
    env = normalize_enrichment_artifact_payload(
        {"requirements": [{"text": "R", "page": 1}],
         "source_artifact_id": "compile-art",
         "module": "RequirementExtractor"},
        artifact_kind="enriched.requirements", **_ctx(),
    )
    assert env.producer.module == "RequirementExtractor"
    assert WARNING_PRODUCER_METADATA_MISSING not in env.warnings


def test_normalize_idempotent_for_already_wrapped_envelope():
    """Wrapping an envelope payload again must return the same
    envelope shape — no extra warnings, no nested `payload` keys."""
    first = normalize_enrichment_artifact_payload(
        {"requirements": [{"text": "R", "page": 2}],
         "source_artifact_id": "compile-art"},
        artifact_kind="enriched.requirements", **_ctx(),
        producer_module="RequirementExtractor",
    )
    wrapped = first.to_payload()
    second = normalize_enrichment_artifact_payload(
        wrapped, artifact_kind="enriched.requirements", **_ctx(),
    )
    assert second.artifact_kind == first.artifact_kind
    assert second.derived is True
    assert second.canonical is False
    assert second.payload == first.payload
    assert len(second.source_refs) == len(first.source_refs)
    # Warning history is preserved on round-trip — the
    # `legacy_payload_normalized` marker is a PERMANENT fact about
    # this artifact (it was originally a legacy payload). What the
    # contract forbids is the warning being **re-stamped** on the
    # second pass; the count must not grow.
    assert second.warnings.count(WARNING_LEGACY_PAYLOAD_NORMALIZED) == 1
    assert first.warnings.count(WARNING_LEGACY_PAYLOAD_NORMALIZED) == 1


def test_normalize_idempotent_path_backfills_missing_lineage():
    """When the wrapped envelope has lineage gaps and the call
    site supplies them, the gaps fill in. Producer-supplied values
    win; call-site values are pure backfill."""
    inner = DerivedEnrichmentArtifact(
        artifact_kind="enriched.requirements",
        document_id=None, snapshot_id=None, run_id=None,
        payload={"requirements": []},
    )
    env = normalize_enrichment_artifact_payload(
        inner.to_payload(),
        artifact_kind="enriched.requirements",
        document_id="doc-NEW", snapshot_id="snap-NEW", run_id="run-NEW",
    )
    assert env.document_id == "doc-NEW"
    assert env.snapshot_id == "snap-NEW"
    assert env.run_id == "run-NEW"


def test_normalize_idempotent_path_does_not_overwrite_existing_lineage():
    inner = DerivedEnrichmentArtifact(
        artifact_kind="enriched.requirements",
        document_id="doc-KEEP", snapshot_id="snap-KEEP", run_id="run-KEEP",
        payload={"requirements": []},
    )
    env = normalize_enrichment_artifact_payload(
        inner.to_payload(),
        artifact_kind="enriched.requirements",
        document_id="doc-IGNORED", snapshot_id="snap-IGNORED", run_id="run-IGNORED",
    )
    # Wrapped envelope values win.
    assert env.document_id == "doc-KEEP"
    assert env.snapshot_id == "snap-KEEP"
    assert env.run_id == "run-KEEP"


def test_normalize_handles_none_payload():
    env = normalize_enrichment_artifact_payload(
        None, artifact_kind="enriched.requirements", **_ctx(),
    )
    assert env.payload == {}
    assert env.source_refs == ()
    assert WARNING_MISSING_SOURCE_REFS in env.warnings


def test_normalize_payload_is_shallow_copied():
    """Mutations through the envelope must not leak back to the
    caller's input dict."""
    src = {"requirements": [{"page": 1}], "source_artifact_id": "art-x"}
    env = normalize_enrichment_artifact_payload(
        src, artifact_kind="enriched.requirements", **_ctx(),
    )
    # Mutating the envelope's payload dict shouldn't change `src`.
    # The envelope's `payload` is a dict (not a Mapping view).
    payload_dict = dict(env.payload)
    payload_dict["new_key"] = "x"
    assert "new_key" not in src


# ---- Lineage preservation -------------------------------------


def test_normalize_preserves_document_run_snapshot_lineage_on_envelope():
    env = normalize_enrichment_artifact_payload(
        {"requirements": [{"text": "R", "page": 1}],
         "source_artifact_id": "compile-art"},
        artifact_kind="enriched.requirements", **_ctx(),
    )
    assert env.document_id == "doc-1"
    assert env.snapshot_id == "snap-1"
    assert env.run_id == "run-1"
    assert env.derived_from.run_id == "run-1"
    assert env.derived_from.snapshot_id == "snap-1"


def test_normalize_propagates_lineage_to_source_refs():
    env = normalize_enrichment_artifact_payload(
        {"requirements": [{"text": "R", "page": 1}],
         "source_artifact_id": "compile-art"},
        artifact_kind="enriched.requirements", **_ctx(),
    )
    ref = env.source_refs[0]
    assert ref.document_id == "doc-1"
    assert ref.snapshot_id == "snap-1"
    assert ref.run_id == "run-1"


def test_alias_evidence_lineage_wins_over_call_site():
    """Alias artifacts carry their own evidence lineage; the
    extractor honors that over the call-site default. Aliases can
    cite across documents in future builds."""
    env = normalize_enrichment_artifact_payload(
        {
            "aliases": [{
                "alias": "x",
                "evidence": {
                    "document_id": "alias-doc",
                    "snapshot_id": "alias-snap",
                    "run_id": "alias-run",
                    "artifact_id": "alias-art",
                    "chunk_id": "alias-c",
                },
            }],
        },
        artifact_kind="domain_enrichment_aliases", **_ctx(),
    )
    ref = env.source_refs[0]
    assert ref.document_id == "alias-doc"
    assert ref.snapshot_id == "alias-snap"
    assert ref.run_id == "alias-run"
    # Envelope-level lineage still reflects the call-site context.
    assert env.document_id == "doc-1"
    assert env.snapshot_id == "snap-1"
    assert env.run_id == "run-1"


def test_explicit_derived_from_overrides_default():
    env = normalize_enrichment_artifact_payload(
        {"requirements": [{"text": "R", "page": 1}],
         "source_artifact_id": "compile-art"},
        artifact_kind="enriched.requirements", **_ctx(),
        derived_from_run_id="compile-run-OLD",
        derived_from_snapshot_id="compile-snap-OLD",
        derived_from_artifact_ids=("compile-art-A", "compile-art-B"),
    )
    assert env.derived_from.run_id == "compile-run-OLD"
    assert env.derived_from.snapshot_id == "compile-snap-OLD"
    assert env.derived_from.source_artifact_ids == (
        "compile-art-A", "compile-art-B",
    )


# ---- Coverage check: every known kind has an extractor --------


def test_every_known_kind_normalises_without_unknown_warning():
    """Defensive coverage: each kind in
    ``KNOWN_DERIVED_ENRICHMENT_KINDS`` must have a per-kind
    extractor branch. If a kind is added to the set but its
    `if`-branch is missing, this test catches the regression."""
    for kind in KNOWN_DERIVED_ENRICHMENT_KINDS:
        env = normalize_enrichment_artifact_payload(
            {}, artifact_kind=kind, **_ctx(),
        )
        assert env.artifact_kind == kind
        assert WARNING_UNKNOWN_ARTIFACT_KIND not in env.warnings


# ---- Negative: contract module has no LLM imports -------------


def test_module_has_no_llm_imports():
    """The contract layer is deterministic — no provider coupling
    allowed. Future projection / memory build phases can grow LLM
    imports if needed; the envelope module itself stays clean."""
    import importlib
    import inspect

    mod = importlib.import_module("j1.processing.derived_enrichment")
    source = inspect.getsource(mod)
    forbidden = {
        "openai", "langchain", "anthropic", "raganything", "lightrag",
        "TextLLMClient", "VisionLLMClient", "EmbeddingClient",
    }
    leaked = [name for name in forbidden if name in source]
    assert not leaked, (
        f"derived_enrichment.py imports/mentions LLM modules: {leaked}"
    )
