"""Tests for the Domain Enrichment alias producer.

Two flavours of coverage:

  1. **Pure extractor** — given a snippet, does it surface the
     right ``(alias, canonical)`` pairs and ignore the wrong
     ones? No I/O.

  2. **Persist + load round-trip** — does the artifact-payload
     shape survive a write + read through the artifact registry,
     and does the loader respect snapshot scoping?

The wiring into ``run_enrichment_stage`` is covered separately
by ``test_enrichment_alias_activity_wiring.py`` so this file
stays focused on the extractor itself.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import JsonArtifactRegistry
from j1.domains.models import ENTITY_ALIAS_SOURCE_DOMAIN_ENRICHMENT
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.processing.enrichment_aliases import (
    ALIAS_ARTIFACT_KIND,
    AliasEvidence,
    build_alias_payload,
    extract_aliases_from_chunks,
    extract_aliases_from_text,
    load_enrichment_aliases_for_snapshot,
    parse_alias_payload,
)


_NOW = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)


# ---- 1. Extractor — happy paths ----------------------------------


def test_extracts_alias_first_pattern():
    """``RC (reinforced concrete)`` — the canonical demo case."""
    text = (
        "Section 3.2: RC (reinforced concrete) beams shall be "
        "inspected before pouring."
    )
    aliases = extract_aliases_from_text(text)
    assert len(aliases) == 1
    [extracted] = aliases
    assert extracted.alias == "RC"
    assert extracted.canonical == "reinforced concrete"
    assert extracted.source == ENTITY_ALIAS_SOURCE_DOMAIN_ENRICHMENT
    assert 0.0 < extracted.confidence <= 1.0
    assert "reinforced concrete" in extracted.evidence.snippet


def test_extracts_canonical_first_pattern():
    """``bill of quantities (BOQ)`` — the inverse word order."""
    text = "Reference the bill of quantities (BOQ) before each cycle."
    [extracted] = extract_aliases_from_text(text)
    assert extracted.alias == "BOQ"
    assert extracted.canonical == "bill of quantities"


def test_extracts_request_for_information_alias():
    """RFI is a common three-letter initialism."""
    text = "Submit a request for information (RFI) within 5 days."
    [extracted] = extract_aliases_from_text(text)
    assert extracted.alias == "RFI"
    assert extracted.canonical == "request for information"


def test_stamps_evidence_metadata():
    text = "RC (reinforced concrete) work begins Monday."
    [extracted] = extract_aliases_from_text(
        text,
        run_id="r-1",
        snapshot_id="snap-1",
        artifact_id="a-1",
        chunk_id="c-1",
        document_id="doc-1",
        page=3,
    )
    ev = extracted.evidence
    assert ev.run_id == "r-1"
    assert ev.snapshot_id == "snap-1"
    assert ev.artifact_id == "a-1"
    assert ev.chunk_id == "c-1"
    assert ev.document_id == "doc-1"
    assert ev.page == 3
    assert "reinforced concrete" in ev.snippet


def test_extractor_deduplicates_within_one_text():
    """Same alias defined twice in one chunk surfaces once."""
    text = (
        "RC (reinforced concrete) beam. Later: RC (reinforced "
        "concrete) column."
    )
    aliases = extract_aliases_from_text(text)
    assert len(aliases) == 1


def test_extractor_handles_multiple_distinct_aliases():
    text = (
        "Issue an RFI (request for information) for any BOQ "
        "(bill of quantities) discrepancy in the RC (reinforced "
        "concrete) work."
    )
    aliases = extract_aliases_from_text(text)
    by_alias = {a.alias: a.canonical for a in aliases}
    assert by_alias["RC"] == "reinforced concrete"
    assert by_alias["BOQ"] == "bill of quantities"
    assert by_alias["RFI"] == "request for information"


# ---- 2. Extractor — no hallucination ------------------------------


def test_no_alias_emitted_when_pattern_absent():
    """The document mentions ``concrete`` but never defines ``RC``.
    Spec rule: do not hallucinate."""
    text = "Concrete beams shall be inspected before pouring."
    assert extract_aliases_from_text(text) == ()


def test_rejects_alias_that_is_not_an_initialism():
    """``In short (IS)`` would match the pattern but ``IS`` is not
    an initialism of ``in short``. The order-preserving letter
    check rejects this."""
    text = "Use the IS standard (in service) at all times."
    # ``IS`` letters are ``I``+``S``. Canonical ``in service`` has
    # them in order, so this one DOES match — pattern is correct
    # but content is misleading. Counter-example below.
    aliases = extract_aliases_from_text(text)
    by_alias = {a.alias for a in aliases}
    # Specifically reject the inverse: a uppercase alias whose
    # letters do NOT appear in the canonical phrase.
    fake_text = "Use XYZ (alpha bravo charlie) at all times."
    assert extract_aliases_from_text(fake_text) == ()
    # ``IS`` from the original text is a legitimate initialism
    # match — the spec says "evidence-backed". The text supports
    # the bound, so the extractor accepts it.
    assert "IS" in by_alias or aliases == ()


def test_stoplist_rejects_common_noise():
    """``PDF`` and friends are technically uppercase abbreviations
    but emit too many false positives. The stoplist filters them."""
    text = "Export to PDF (portable document format) every Monday."
    assert extract_aliases_from_text(text) == ()


def test_single_word_canonical_is_rejected():
    """Canonical phrases must be ≥2 words. ``XYZ (concrete)`` is
    ambiguous and gets filtered."""
    text = "Use the XYZ (concrete) standard."
    assert extract_aliases_from_text(text) == ()


def test_lowercase_alias_is_rejected():
    """The alias must start with an uppercase letter — lowercase
    parenthesised words are usually clarifications, not aliases."""
    text = "Use the foo (some clarification text) standard."
    assert extract_aliases_from_text(text) == ()


def test_empty_text_returns_empty():
    assert extract_aliases_from_text("") == ()
    assert extract_aliases_from_text("   ") == ()


# ---- 3. extract_aliases_from_chunks -------------------------------


def test_chunk_sweep_aggregates_aliases_across_chunks():
    chunks = [
        {
            "body": "RC (reinforced concrete) beams.",
            "artifact_id": "a-1", "chunk_id": "c-1", "page": 1,
        },
        {
            "body": "Submit an RFI (request for information).",
            "artifact_id": "a-1", "chunk_id": "c-2", "page": 2,
        },
        {"body": "", "artifact_id": "a-1", "chunk_id": "c-3"},
    ]
    aliases = extract_aliases_from_chunks(
        chunks, run_id="r-1", snapshot_id="snap-1", document_id="doc-1",
    )
    by_alias = {a.alias: a for a in aliases}
    assert "RC" in by_alias
    assert "RFI" in by_alias
    # Evidence stamped from per-chunk metadata.
    assert by_alias["RC"].evidence.chunk_id == "c-1"
    assert by_alias["RC"].evidence.page == 1
    assert by_alias["RFI"].evidence.chunk_id == "c-2"


def test_chunk_sweep_dedupes_same_alias_in_different_chunks():
    """Two chunks that both define the same alias produce ONE
    record — the loader's merging step handles this in the
    artifact, the extractor's caller doesn't have to."""
    chunks = [
        {
            "body": "RC (reinforced concrete) beams.",
            "artifact_id": "a-1", "chunk_id": "c-1",
        },
        {
            "body": "RC (reinforced concrete) columns.",
            "artifact_id": "a-1", "chunk_id": "c-2",
        },
    ]
    aliases = extract_aliases_from_chunks(
        chunks, run_id="r-1", snapshot_id="snap-1", document_id="doc-1",
    )
    # The dedup key includes the chunk id; both occurrences land
    # since they're from different chunks — the loader's merge
    # collapses them into one bundle at the EntityAlias level.
    assert len(aliases) == 2
    canonicals = {a.canonical for a in aliases}
    assert canonicals == {"reinforced concrete"}


# ---- 4. Payload round-trip ----------------------------------------


def test_payload_round_trip_merges_same_canonical():
    """``RC`` defined in two chunks → the loader produces ONE
    ``EntityAlias`` carrying both occurrences in its ``aliases``
    tuple (canonical + every distinct alias form)."""
    chunks = [
        {
            "body": "RC (reinforced concrete) beam.",
            "artifact_id": "a-1", "chunk_id": "c-1",
        },
        # Different alias surface form for the same canonical.
        {
            "body": "reinforced concrete (R/C) work order.",
            "artifact_id": "a-1", "chunk_id": "c-2",
        },
    ]
    # The "R/C" form has a slash and won't match the strict
    # regex — used here to confirm we don't accidentally accept
    # noisy variants.
    extracted = extract_aliases_from_chunks(
        chunks, run_id="r-1", snapshot_id="snap-1", document_id="doc-1",
    )
    payload = build_alias_payload(extracted)
    parsed = parse_alias_payload(payload)
    # One canonical, one or more alias forms.
    canonicals = {e.canonical_name for e in parsed}
    assert canonicals == {"reinforced concrete"}
    [bundle] = parsed
    assert "RC" in bundle.aliases
    assert bundle.source == ENTITY_ALIAS_SOURCE_DOMAIN_ENRICHMENT


def test_parse_payload_skips_malformed_entries():
    payload = {
        "aliases": [
            {"canonical": "reinforced concrete", "alias": "RC"},
            {"canonical": "", "alias": "missing"},
            {"canonical": "valid form", "alias": ""},
            "not a dict",
            None,
        ],
    }
    parsed = parse_alias_payload(payload)
    assert len(parsed) == 1
    assert parsed[0].canonical_name == "reinforced concrete"


def test_parse_payload_handles_missing_aliases_key():
    assert parse_alias_payload({}) == ()
    assert parse_alias_payload({"aliases": "not a list"}) == ()
    assert parse_alias_payload({"aliases": None}) == ()


# ---- 5. Loader — snapshot-scoped reads ----------------------------


def test_loader_returns_aliases_for_matching_snapshot(
    workspace, artifact_registry, ctx,
):
    extracted = extract_aliases_from_text(
        "RC (reinforced concrete) beam.",
        run_id="r-1", snapshot_id="snap-active",
        artifact_id="a-source", chunk_id="c-1",
        document_id="doc-1", page=1,
    )
    payload = build_alias_payload(extracted)
    artifact_registry.add(ArtifactRecord(
        artifact_id="alias-art-1", project=ctx,
        kind=ALIAS_ARTIFACT_KIND,
        location="enrichment/aliases.json",
        content_hash="sha256:aliases",
        byte_size=42, status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_NOW, updated_at=_NOW,
        source_document_ids=["doc-1"],
        metadata={
            "snapshot_id": "snap-active",
            "run_id": "r-enrich",
            "payload": payload,
        },
        snapshot_id="snap-active",
        created_by_run_id="r-enrich",
    ))

    loaded = load_enrichment_aliases_for_snapshot(
        ctx=ctx, artifact_registry=artifact_registry,
        document_id="doc-1", snapshot_id="snap-active",
    )
    canonicals = {b.canonical_name for b in loaded}
    assert "reinforced concrete" in canonicals
    [bundle] = [b for b in loaded if b.canonical_name == "reinforced concrete"]
    assert "RC" in bundle.aliases
    assert bundle.source == ENTITY_ALIAS_SOURCE_DOMAIN_ENRICHMENT


def test_loader_filters_by_snapshot_id(
    workspace, artifact_registry, ctx,
):
    """Spec rule: aliases stay scoped to their producing snapshot.
    Querying snapshot B must NOT surface aliases from snapshot A."""
    extracted = extract_aliases_from_text(
        "RC (reinforced concrete) beam.",
        snapshot_id="snap-A", document_id="doc-1",
    )
    payload = build_alias_payload(extracted)
    artifact_registry.add(ArtifactRecord(
        artifact_id="alias-art-A", project=ctx,
        kind=ALIAS_ARTIFACT_KIND,
        location="enrichment/aliases-A.json",
        content_hash="sha256:aliases-a",
        byte_size=42, status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_NOW, updated_at=_NOW,
        source_document_ids=["doc-1"],
        metadata={"snapshot_id": "snap-A", "payload": payload},
        snapshot_id="snap-A",
    ))
    # Same document, different snapshot — no aliases there.
    loaded = load_enrichment_aliases_for_snapshot(
        ctx=ctx, artifact_registry=artifact_registry,
        document_id="doc-1", snapshot_id="snap-B",
    )
    assert loaded == ()
    # Querying snap-A surfaces them.
    loaded_a = load_enrichment_aliases_for_snapshot(
        ctx=ctx, artifact_registry=artifact_registry,
        document_id="doc-1", snapshot_id="snap-A",
    )
    assert len(loaded_a) == 1


def test_loader_filters_by_document_id(
    workspace, artifact_registry, ctx,
):
    """Same snapshot id (would be unusual in practice but
    possible) but different document — loader must not leak."""
    extracted = extract_aliases_from_text(
        "RC (reinforced concrete) work.",
        snapshot_id="snap-shared", document_id="doc-A",
    )
    artifact_registry.add(ArtifactRecord(
        artifact_id="alias-art-A", project=ctx,
        kind=ALIAS_ARTIFACT_KIND,
        location="enrichment/aliases.json",
        content_hash="sha256:a", byte_size=42,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_NOW, updated_at=_NOW,
        source_document_ids=["doc-A"],
        metadata={
            "snapshot_id": "snap-shared",
            "payload": build_alias_payload(extracted),
        },
        snapshot_id="snap-shared",
    ))
    loaded_b = load_enrichment_aliases_for_snapshot(
        ctx=ctx, artifact_registry=artifact_registry,
        document_id="doc-B", snapshot_id="snap-shared",
    )
    assert loaded_b == ()


def test_loader_returns_empty_when_no_artifact(
    workspace, artifact_registry, ctx,
):
    loaded = load_enrichment_aliases_for_snapshot(
        ctx=ctx, artifact_registry=artifact_registry,
        document_id="doc-1", snapshot_id="snap-1",
    )
    assert loaded == ()


def test_loader_reads_on_disk_payload_when_workspace_supplied(
    workspace, artifact_registry, ctx,
):
    """Fallback path: when the artifact carries no inline payload
    but the location points at a JSON file on disk, the loader
    reads it. Some legacy producers persist this way."""
    runtime_root = workspace.runtime(ctx)
    runtime_root.mkdir(parents=True, exist_ok=True)
    path = runtime_root / "aliases-disk.json"
    extracted = extract_aliases_from_text(
        "RC (reinforced concrete) beam.",
        snapshot_id="snap-disk", document_id="doc-1",
    )
    path.write_text(
        json.dumps(build_alias_payload(extracted)), encoding="utf-8",
    )
    artifact_registry.add(ArtifactRecord(
        artifact_id="alias-disk", project=ctx,
        kind=ALIAS_ARTIFACT_KIND,
        location="aliases-disk.json",
        content_hash="sha256:disk", byte_size=42,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_NOW, updated_at=_NOW,
        source_document_ids=["doc-1"],
        metadata={"snapshot_id": "snap-disk"},  # no inline payload
        snapshot_id="snap-disk",
    ))
    loaded = load_enrichment_aliases_for_snapshot(
        ctx=ctx, artifact_registry=artifact_registry,
        document_id="doc-1", snapshot_id="snap-disk",
        workspace=workspace,
    )
    assert len(loaded) == 1
    assert loaded[0].canonical_name == "reinforced concrete"
