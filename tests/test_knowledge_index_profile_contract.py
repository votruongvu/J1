"""Contract — Knowledge Index profile collapse + capability
recommendations.

The product surfaces ONE profile (``knowledge_index``) plus three
capability checkboxes (Process images / tables / equations). This
module pins the load-bearing pieces of that refactor:

  1. ``ExecutionProfile.KNOWLEDGE_INDEX`` exists, is the canonical
     post-collapse value, and is the new ``DEFAULT_PROFILE``.
  2. Legacy values (minimum_queryable / standard / advanced) are
     preserved as deprecated aliases — accepted on the wire and
     resolved to KNOWLEDGE_INDEX via ``coerce_legacy_profile``.
  3. ``recommend_capabilities_from_assessment`` returns
     per-checkbox recommendations the FE picker consumes.
  4. The compile config plan (``AssessmentPlan``) does NOT own any
     domain-enrichment-related field — those live on the
     separate ``PostCompileEnrichPlan``.

Pinned here as a single regression document. Tests assert
contracts against the production seams (no stubs of the
load-bearing modules).
"""

from __future__ import annotations

import pytest

from j1.processing.execution_profile import (
    DEFAULT_PROFILE,
    LEGACY_PROFILE_VALUES,
    PROFILE_CAPABILITIES,
    PROFILE_LABELS,
    CapabilityRecommendations,
    ExecutionProfile,
    coerce_legacy_profile,
    recommend_capabilities_from_assessment,
)


# ---- Contract 1: KNOWLEDGE_INDEX is canonical -------------------


def test_contract_1_knowledge_index_is_the_canonical_profile():
    """The new canonical profile value MUST exist on the enum.
    Wire string is stable across the API boundary."""
    assert ExecutionProfile.KNOWLEDGE_INDEX.value == "knowledge_index"


def test_contract_1_default_profile_is_knowledge_index():
    """``DEFAULT_PROFILE`` is the value the backend picks when no
    UI selection arrives. Post-collapse this MUST be
    KNOWLEDGE_INDEX so legacy callers + new callers funnel to the
    same canonical."""
    assert DEFAULT_PROFILE == ExecutionProfile.KNOWLEDGE_INDEX


def test_contract_1_knowledge_index_has_a_label():
    """The picker renders ``PROFILE_LABELS[KNOWLEDGE_INDEX]`` —
    must be non-empty and human-readable."""
    assert PROFILE_LABELS[ExecutionProfile.KNOWLEDGE_INDEX] == (
        "Knowledge Index"
    )


def test_contract_1_knowledge_index_has_a_capability_matrix():
    caps = PROFILE_CAPABILITIES[ExecutionProfile.KNOWLEDGE_INDEX]
    # Every user-facing ingest produces the minimum valid J1
    # knowledge output: parsed content + chunks + base graph/index
    # + queryable run.
    assert caps.run_index is True
    assert caps.compile_lightrag_entity_extraction is True
    # Multimodal is permitted at the profile level; per-request
    # capability checkboxes refine which sub-capabilities fire.
    assert caps.compile_multimodal_processing is True


# ---- Contract 2: legacy values are deprecated aliases ----------


def test_contract_2_legacy_values_are_advertised_as_deprecated():
    """``LEGACY_PROFILE_VALUES`` is the canonical set of wire
    strings external callers may still supply for backwards-
    compat. The set MUST NOT include the new ``knowledge_index``
    value — that's the canonical name, not a legacy one."""
    assert LEGACY_PROFILE_VALUES == frozenset({
        "minimum_queryable", "standard", "advanced",
    })
    assert ExecutionProfile.KNOWLEDGE_INDEX.value not in LEGACY_PROFILE_VALUES


@pytest.mark.parametrize("legacy_value", [
    ExecutionProfile.MINIMUM_QUERYABLE,
    ExecutionProfile.STANDARD,
    ExecutionProfile.ADVANCED,
])
def test_contract_2_coerce_legacy_maps_to_knowledge_index(
    legacy_value: ExecutionProfile,
):
    """The wire-boundary helper coerces every legacy value to
    KNOWLEDGE_INDEX. Pinned so a future refactor can't accidentally
    preserve legacy semantics downstream."""
    assert coerce_legacy_profile(legacy_value) == (
        ExecutionProfile.KNOWLEDGE_INDEX
    )


def test_contract_2_coerce_passthrough_for_knowledge_index():
    """Canonical value passes through unchanged."""
    assert coerce_legacy_profile(
        ExecutionProfile.KNOWLEDGE_INDEX,
    ) == ExecutionProfile.KNOWLEDGE_INDEX


# ---- Contract 3: capability recommender ------------------------


def test_contract_3_image_recommendation_when_images_present():
    recs = recommend_capabilities_from_assessment(
        has_images=True, has_tables=False, has_scanned_pages=False,
        text_extractable_ratio=0.9, page_count=20,
    )
    assert recs.image_processing.recommended is True
    # New richer shape carries reasons (plural).
    text = " ".join(recs.image_processing.reasons).lower()
    assert "image" in text


def test_contract_3_image_recommendation_when_scanned_pages_present():
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=False, has_scanned_pages=True,
        text_extractable_ratio=0.1, page_count=20,
    )
    assert recs.image_processing.recommended is True
    text = " ".join(recs.image_processing.reasons).lower()
    assert "scanned" in text


def test_contract_3_table_recommendation_when_tables_present():
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=True, has_scanned_pages=False,
        text_extractable_ratio=0.9, page_count=20,
    )
    assert recs.table_processing.recommended is True
    text = " ".join(recs.table_processing.reasons).lower()
    assert "row" in text or "table" in text


def test_contract_3_equation_recommendation_defaults_to_off():
    """No deterministic equation signal exists today; the assessor
    honestly defaults to OFF. The operator opts in via the
    checkbox."""
    recs = recommend_capabilities_from_assessment(
        has_images=True, has_tables=True, has_scanned_pages=False,
        text_extractable_ratio=0.9, page_count=20,
    )
    assert recs.equation_processing.recommended is False
    text = " ".join(recs.equation_processing.reasons).lower()
    assert "formula" in text or "equation" in text


def test_contract_3_capability_recommendations_serialise_to_payload():
    """The recommender's output MUST serialise to a stable JSON
    shape the FE picker consumes."""
    recs = recommend_capabilities_from_assessment(
        has_images=True, has_tables=True, has_scanned_pages=False,
        text_extractable_ratio=0.9, page_count=20,
    )
    payload = recs.to_payload()
    # Three capability entries + domain_hints array.
    assert set(payload.keys()) == {
        "image_processing", "table_processing",
        "equation_processing", "domain_hints",
    }
    for key in {"image_processing", "table_processing", "equation_processing"}:
        entry = payload[key]
        assert isinstance(entry["recommended"], bool)
        assert isinstance(entry["confidence"], str)
        assert entry["confidence"] in {"low", "medium", "high"}
        assert isinstance(entry["sources"], list)
        assert isinstance(entry["reasons"], list)
        # Every capability has at least one reason — either the
        # positive explanation or the "no signal" default.
        assert entry["reasons"], f"empty reasons for {key}"


def test_contract_3_capability_recommendations_is_a_dataclass():
    """``CapabilityRecommendations`` is the wire shape. Pinned as
    a dataclass so consumers can rely on field access."""
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=False, has_scanned_pages=False,
        text_extractable_ratio=0.9, page_count=20,
    )
    assert isinstance(recs, CapabilityRecommendations)
    # Off by default when no signals fire.
    assert recs.image_processing.recommended is False
    assert recs.table_processing.recommended is False
    assert recs.equation_processing.recommended is False


# ---- Contract 4: AssessmentPlan does NOT own enrichment --------


def test_contract_4_assessment_plan_has_no_enrichment_field():
    """The compile-stage plan owns ONLY compile config — never
    domain-enrichment decisions. PostCompileEnrichPlan is the
    separate post-compile owner. Pinned so a future refactor
    that re-couples them surfaces here first."""
    from dataclasses import fields
    from j1.processing.assessment import AssessmentPlan

    field_names = {f.name for f in fields(AssessmentPlan)}
    forbidden_fields = {
        "should_run_domain_enrichment",
        "domain_enrichment_tasks",
        "enrichment_required",
        "require_enrichment_success",
        "domain_id",  # belongs on the enrich plan
        "enrichment_policy",
    }
    leaked = forbidden_fields & field_names
    assert not leaked, (
        f"AssessmentPlan grew enrichment-ownership fields: "
        f"{leaked!r}. Domain Enrichment lives on "
        "PostCompileEnrichPlan; move the fields back."
    )


def test_contract_4_post_compile_enrich_plan_owns_enrichment_decision():
    """``PostCompileEnrichPlan`` is where enrichment fields belong.
    Sanity-check that the separation still holds — the load-bearing
    fields exist on the enrich plan, not on AssessmentPlan."""
    from dataclasses import fields
    from j1.processing.enrich_assessment import PostCompileEnrichPlan

    field_names = {f.name for f in fields(PostCompileEnrichPlan)}
    # Spot-check: the load-bearing enrichment decisions live here.
    assert "overall_recommendation" in field_names
    assert "require_enrichment_success" in field_names
    assert "domain_enrichment_policy" in field_names
