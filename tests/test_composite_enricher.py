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


# ---- Vision-client forwarding (the "No vision LLM configured" fix) -


class _StubVisionClient:
    """Minimal vision client double — returns a fixed description so
    we can assert the composite actually called through to the
    client instead of falling back to the stub markdown."""

    provider = "stub"
    model = "stub-vision"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def analyze_image(self, image_data: bytes, *, prompt: str, metadata: dict):
        self.calls.append({
            "image_bytes": len(image_data),
            "prompt": prompt,
            "metadata": metadata,
        })
        # The vision client contract: returns (description, usage).
        return ("Stub description for testing.", None)


def test_composite_forwards_vision_client_only_to_visual_describer(
    default_profile,
):
    """The fix for 'No vision LLM configured' bug: the composite must
    pass the vision client to `VisualContentDescriber` AND must NOT
    pass it to other children (which would crash with TypeError —
    no other generic enricher accepts a `vision_client` kwarg)."""
    from j1.enrichers import VisualContentDescriber
    composite = CompositeEnricher.from_default(
        default_profile, vision_client=_StubVisionClient(),
    )
    # The composite should construct cleanly (no TypeError from
    # passing vision_client to a child that doesn't accept it).
    vcd = next(
        (e for e in composite._enrichers if isinstance(e, VisualContentDescriber)),
        None,
    )
    assert vcd is not None, "VisualContentDescriber missing from composite"
    # And the vision client should be wired into VCD specifically.
    assert vcd._vision_client is not None
    # No other child stores a vision client (defensive — would
    # mean we're forwarding too aggressively).
    other_clients = [
        getattr(e, "_vision_client", "<sentinel>")
        for e in composite._enrichers
        if not isinstance(e, VisualContentDescriber)
    ]
    # The sentinel matches the missing-attribute case (correct for
    # non-VCD children), so any actual client would stand out.
    assert all(c == "<sentinel>" for c in other_clients)


def test_composite_with_vision_client_does_not_emit_no_vision_stub(
    default_profile, ctx, tmp_path,
):
    """End-to-end: a composite WITH a vision client and image bytes
    must NOT emit 'No vision LLM configured — visual enrichment
    skipped' markdown. It should call the vision client and embed
    the description."""
    # Stub `content_source` returns image bytes so VCD takes the
    # 'analyze' branch instead of 'no bytes available'.
    def _content_source(_ctx, _artifact_id: str) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # minimal PNG header

    vision = _StubVisionClient()
    composite = CompositeEnricher.from_default(
        default_profile,
        vision_client=vision,
        content_source=_content_source,
    )
    result = composite.enrich(ctx, "art-1")
    assert result.status is ResultStatus.SUCCEEDED

    # Find the visual draft. It exists per the union-of-kinds rule.
    visual_drafts = [d for d in result.drafts if d.kind == "enriched.visuals"]
    md_drafts = [d for d in visual_drafts if d.suggested_extension == ".md"]
    assert md_drafts, "expected a markdown visual draft"
    md_text = md_drafts[0].content.decode("utf-8")
    # The bug we're fixing: this exact sentence appearing in the FE
    # means the vision client wasn't wired through.
    assert "No vision LLM configured" not in md_text
    # And the stub description from our vision client made it through.
    assert "Stub description for testing" in md_text
    # The vision client was called with the image bytes.
    assert vision.calls
    assert vision.calls[0]["image_bytes"] > 0


def test_composite_without_vision_client_emits_no_vision_stub(
    default_profile, ctx,
):
    """Counter-test: when the deployment has no vision client
    configured (no `J1_VISION_LLM_*` env vars), the composite must
    still construct but VCD emits the 'No vision LLM configured'
    stub. Pinning this so the fallback contract stays explicit."""
    composite = CompositeEnricher.from_default(
        default_profile, vision_client=None,
    )
    result = composite.enrich(ctx, "art-1")
    visual_md = next(
        (d for d in result.drafts
         if d.kind == "enriched.visuals" and d.suggested_extension == ".md"),
        None,
    )
    assert visual_md is not None
    assert "No vision LLM configured" in visual_md.content.decode("utf-8")


# ---- Text + embedding client forwarding (infrastructure plumbing) ---


class _StubTextClient:
    provider = "stub"
    model = "stub-text"


class _StubEmbeddingClient:
    provider = "stub"
    model = "stub-embedding"


def test_composite_forwards_text_client_to_every_child(default_profile):
    """The base `_StructuredEnricher.__init__` accepts a `text_client`
    kwarg; the composite must forward it so future LLM-backed
    enricher implementations (TableExtractor / RiskExtractor / …)
    can read `self._text_client` without re-plumbing the composite."""
    text_client = _StubTextClient()
    composite = CompositeEnricher.from_default(
        default_profile, text_client=text_client,
    )
    for child in composite._enrichers:
        # Every base-class subclass exposes `_text_client`.
        assert getattr(child, "_text_client", None) is text_client


def test_composite_forwards_embedding_client_to_every_child(default_profile):
    embedding_client = _StubEmbeddingClient()
    composite = CompositeEnricher.from_default(
        default_profile, embedding_client=embedding_client,
    )
    for child in composite._enrichers:
        assert getattr(child, "_embedding_client", None) is embedding_client


def test_composite_skips_text_client_when_unset(default_profile):
    """Default = None means 'no client wired' — every child sees
    `_text_client = None` and falls through to its stub `_produce`."""
    composite = CompositeEnricher.from_default(default_profile)
    for child in composite._enrichers:
        assert getattr(child, "_text_client", None) is None
        assert getattr(child, "_embedding_client", None) is None


def test_composite_forwards_all_three_clients_independently(default_profile):
    """Vision + text + embedding can all be wired together. The
    dispatch in `_construct_child` must NOT cross-wire (e.g. send
    vision_client to non-VCD children) and must apply text +
    embedding to every child."""
    from j1.enrichers import VisualContentDescriber
    vision = _StubVisionClient()
    text_client = _StubTextClient()
    embedding_client = _StubEmbeddingClient()
    composite = CompositeEnricher.from_default(
        default_profile,
        vision_client=vision,
        text_client=text_client,
        embedding_client=embedding_client,
    )
    vcd = next(
        (e for e in composite._enrichers if isinstance(e, VisualContentDescriber)),
        None,
    )
    # VCD has all three.
    assert vcd is not None
    assert vcd._vision_client is vision
    assert vcd._text_client is text_client
    assert vcd._embedding_client is embedding_client
    # Other children have text + embedding but NO vision attribute
    # (they don't accept a `vision_client` kwarg).
    others = [e for e in composite._enrichers if not isinstance(e, VisualContentDescriber)]
    for child in others:
        assert child._text_client is text_client
        assert child._embedding_client is embedding_client
        # Crucially: the dispatch did not silently set a vision
        # attribute on these — that would mean we forwarded too
        # aggressively and a non-VCD child started using it.
        assert not hasattr(child, "_vision_client"), (
            f"{type(child).__name__} unexpectedly carries _vision_client"
        )
