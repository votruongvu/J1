"""Tests for `CompositeEnricher` — the bundled enricher that produces
the union of every generic enricher's drafts in a single
`ArtifactProcessingResult`.

The Results > Assets tab needs `enriched.tables` / `enriched.visuals`
/ `enriched.formulas` artifacts to populate. Each individual enricher
produces ONE kind, but the workflow runs only ONE `enricher_kind` per
run — wiring them individually means an upload picks one and the
other Assets categories silently disappear. The composite collapses
that constraint into a single registered kind that emits the full set.
"""

from __future__ import annotations

import pytest

from j1.enrichers import (
    ARTIFACT_TYPE_CONFIDENCE_ASSESSMENT,
    ARTIFACT_TYPE_CONSISTENCY_FINDINGS,
    ARTIFACT_TYPE_DOCUMENT_MAP,
    ARTIFACT_TYPE_FORMULAS,
    ARTIFACT_TYPE_REQUIREMENTS,
    ARTIFACT_TYPE_RISKS,
    ARTIFACT_TYPE_SOURCE_MAP,
    ARTIFACT_TYPE_TABLES,
    ARTIFACT_TYPE_VISUALS,
    COMPOSITE_ENRICHER_KIND,
    CompositeEnricher,
    GENERIC_ENRICHERS,
)
from j1.processing.results import ArtifactDraft, ArtifactProcessingResult
from j1.processing.status import ResultStatus
from j1.profiles import DEFAULT_PROFILE_ID, Profile, ProfileLoader
from j1.projects.context import ProjectContext


@pytest.fixture
def default_profile() -> Profile:
    return ProfileLoader().load(DEFAULT_PROFILE_ID)


@pytest.fixture
def empty_profile() -> Profile:
    """Fallback used when ProfileLoader fails — exercises the
    'no profile' path the worker wiring takes when the profiles
    directory isn't present."""
    return Profile(profile_id="default", metadata={})


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="acme", project_id="alpha")


def test_composite_kind_is_stable():
    """Worker registration + REST capabilities both reference
    `COMPOSITE_ENRICHER_KIND`. Pin the value so a rename in one place
    can't silently de-sync the two."""
    assert COMPOSITE_ENRICHER_KIND == "j1.enricher.composite"
    assert CompositeEnricher.kind == COMPOSITE_ENRICHER_KIND


def test_from_default_constructs_one_child_per_generic_enricher(
    default_profile,
):
    composite = CompositeEnricher.from_default(default_profile)
    assert len(composite._enrichers) == len(GENERIC_ENRICHERS)


def test_enrich_returns_union_of_child_drafts(default_profile, ctx):
    """End-to-end: run the composite over an artifact id and confirm
    every Assets-tab kind shows up at least once. Stub-mode children
    produce empty arrays in their JSON, but they STILL emit drafts
    of the right kind — that's what the FE needs to flip the tab."""
    composite = CompositeEnricher.from_default(default_profile)
    result = composite.enrich(ctx, "art-1")

    assert result.status is ResultStatus.SUCCEEDED
    kinds = {d.kind for d in result.drafts}
    # Each generic enricher's `artifact_type` should appear at least
    # once. The exact draft count varies per enricher (some produce
    # both .json and .md, some json-only).
    expected_kinds = {
        ARTIFACT_TYPE_DOCUMENT_MAP,
        ARTIFACT_TYPE_REQUIREMENTS,
        ARTIFACT_TYPE_TABLES,
        ARTIFACT_TYPE_VISUALS,
        ARTIFACT_TYPE_FORMULAS,
        ARTIFACT_TYPE_RISKS,
        ARTIFACT_TYPE_CONSISTENCY_FINDINGS,
        ARTIFACT_TYPE_SOURCE_MAP,
        ARTIFACT_TYPE_CONFIDENCE_ASSESSMENT,
    }
    assert expected_kinds.issubset(kinds)


def test_enrich_includes_processor_name_metadata(default_profile, ctx):
    composite = CompositeEnricher.from_default(default_profile)
    result = composite.enrich(ctx, "art-1")
    assert result.metadata["processor_name"] == COMPOSITE_ENRICHER_KIND
    assert result.metadata["child_count"] == len(GENERIC_ENRICHERS)


def test_enrich_isolates_individual_child_failures(default_profile, ctx):
    """A child that raises must not blow up the composite — its
    failure surfaces under `metadata.failed_kinds` and the rest of
    the children still run."""

    class _Boom:
        kind = "test.boom"
        def enrich(self, *_a, **_kw):
            raise RuntimeError("synthetic failure")

    class _Quiet:
        kind = "test.quiet"
        def enrich(self, *_a, **_kw):
            return ArtifactProcessingResult(
                status=ResultStatus.SUCCEEDED,
                drafts=[ArtifactDraft(
                    kind="enriched.tables",
                    content=b"{}",
                    suggested_extension=".json",
                )],
            )

    composite = CompositeEnricher(
        default_profile,
        enrichers=(_Boom(), _Quiet()),
    )
    result = composite.enrich(ctx, "art-1")

    assert result.status is ResultStatus.SUCCEEDED
    assert len(result.drafts) == 1
    assert result.drafts[0].kind == "enriched.tables"
    assert result.metadata["failed_kinds"] == [
        {"kind": "test.boom", "error": "RuntimeError: synthetic failure"},
    ]


def test_enrich_returns_failed_when_every_child_fails(default_profile, ctx):
    """If every child failed AND none skipped, the composite's
    overall result is FAILED — so the workflow records the enrich
    step as a failed-optional and the FE's Quality tab can surface
    it."""

    class _Boom:
        kind = "test.boom"
        def enrich(self, *_a, **_kw):
            raise RuntimeError("synthetic failure")

    composite = CompositeEnricher(
        default_profile,
        enrichers=(_Boom(), _Boom()),
    )
    result = composite.enrich(ctx, "art-1")

    assert result.status is ResultStatus.FAILED
    assert "every enricher failed" in (result.error or "")
    assert len(result.metadata["failed_kinds"]) == 2


def test_enrich_classifies_skipped_children_as_skipped(default_profile, ctx):
    """A SKIPPED child (e.g. enabled=False) is recorded in
    `skipped_kinds` so operators can see why nothing landed."""

    class _Skipper:
        kind = "test.skipper"
        def enrich(self, *_a, **_kw):
            return ArtifactProcessingResult(
                status=ResultStatus.SKIPPED,
                drafts=[],
                metadata={},
            )

    composite = CompositeEnricher(
        default_profile,
        enrichers=(_Skipper(),),
    )
    result = composite.enrich(ctx, "art-1")

    assert result.status is ResultStatus.SUCCEEDED
    assert result.drafts == []
    assert result.metadata["skipped_kinds"] == ["test.skipper"]


def test_constructs_with_empty_profile_no_crash(empty_profile, ctx):
    """The dev `_wiring.py` falls back to `Profile(profile_id="default",
    metadata={})` when ProfileLoader fails. The composite must still
    construct and run — its children handle missing prompts via
    `_profile_prompt` returning empty string."""
    composite = CompositeEnricher.from_default(empty_profile)
    result = composite.enrich(ctx, "art-1")
    # Children produce stub outputs in this state; the composite
    # still SUCCEEDED with drafts.
    assert result.status is ResultStatus.SUCCEEDED
    assert len(result.drafts) > 0
