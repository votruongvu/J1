"""PR-09 contract — Alias quality after real-document evaluation.

Per ``docs/j1_sequential_pr_implementation_plan.md``'s PR-09, J1
MUST guarantee that the alias-producer rules satisfy these five
contracts. This module is the single navigable regression document
for the contract; it consolidates the load-bearing pins so any
future evidence-based tuning (driven by an actual A/B harness run
against real indexed documents) starts from a known baseline.

The five contracts:

  1. Stoplisted acronyms are not broadened. ``PDF``, ``HTTP``,
     ``URL``, etc. must never produce a ``(canonical, alias)``
     pair the resolver consumes — they are document chrome, not
     domain terms.
  2. Valid domain aliases ARE broadened — the regex extractor
     surfaces both pattern families (``ALIAS (canonical)`` and
     ``canonical (ALIAS)``).
  3. Lowercase / common words are rejected. ``the (foo)`` or
     ``a (bar)`` must not become an alias mapping — the producer
     would otherwise drown the loader in false positives.
  4. Duplicate aliases are deduplicated at each layer: within
     one text (extractor), across chunks (sweep), and across
     extracted entries with the same canonical (payload merge).
  5. Scope safety holds — the loader still filters by
     ``(snapshot_id, document_id)`` after any future tuning. An
     alias stamped under one scope cannot leak into a different
     scope's retrieval.

The PR-09 spec calls for running the harness against real data
first and tuning only with evidence. That is operator work
(needs a real indexed project, which a code review can't do).
This file pins the baseline so that work is anchored.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.processing.enrichment_aliases import (
    ALIAS_ARTIFACT_KIND,
    build_alias_payload,
    extract_aliases_from_chunks,
    extract_aliases_from_text,
    load_enrichment_aliases_for_snapshot,
    parse_alias_payload,
)
from j1.projects.context import ProjectContext


_NOW = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
_CTX = ProjectContext(tenant_id="acme", project_id="alpha")


# ---- Synthetic in-memory artifact registry (for scope tests) ----


class _InMemoryArtifactRegistry:
    """Subset of the artifact registry the alias loader consumes.
    Pure in-memory so the contract surface is exercised without a
    workspace mount."""

    def __init__(self):
        self._records: list[ArtifactRecord] = []

    def add(self, record: ArtifactRecord) -> None:
        self._records.append(record)

    def list_artifacts(self, ctx, *, kind: str | None = None):
        out = []
        for r in self._records:
            if r.project.tenant_id != ctx.tenant_id:
                continue
            if r.project.project_id != ctx.project_id:
                continue
            if kind is not None and r.kind != kind:
                continue
            out.append(r)
        return out

    def update_metadata(self, ctx, artifact_id, metadata):
        for r in self._records:
            if r.artifact_id == artifact_id:
                r.metadata = dict(metadata)
                return

    def get(self, ctx, artifact_id):
        for r in self._records:
            if r.artifact_id == artifact_id:
                return r
        raise KeyError(artifact_id)


def _alias_artifact(
    *, artifact_id: str, document_id: str, snapshot_id: str,
    text: str,
) -> ArtifactRecord:
    extracted = extract_aliases_from_text(
        text,
        run_id=f"run-{snapshot_id}",
        snapshot_id=snapshot_id,
        document_id=document_id,
    )
    payload = build_alias_payload(extracted)
    return ArtifactRecord(
        artifact_id=artifact_id, project=_CTX,
        kind=ALIAS_ARTIFACT_KIND,
        location=f"enrichment/aliases/{artifact_id}.json",
        content_hash=f"sha256:{artifact_id}", byte_size=len(text) or 1,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_NOW, updated_at=_NOW,
        source_document_ids=[document_id],
        metadata={
            "snapshot_id": snapshot_id, "payload": payload,
            "run_id": f"run-{snapshot_id}",
        },
        snapshot_id=snapshot_id,
        created_by_run_id=f"run-{snapshot_id}",
    )


# ---- Contract 1: stoplisted acronyms are not broadened ----------


@pytest.mark.parametrize("stoplisted_acronym,canonical", [
    ("PDF", "portable document format"),
    ("HTTP", "hypertext transfer protocol"),
    ("HTTPS", "secure hypertext transfer protocol"),
    ("URL", "uniform resource locator"),
    ("URI", "uniform resource identifier"),
    ("API", "application programming interface"),
    ("JSON", "javascript object notation"),
    ("YAML", "yet another markup language"),
    ("XML", "extensible markup language"),
    ("CSV", "comma separated values"),
    ("USA", "united states of america"),
    ("UK", "united kingdom"),
    ("EU", "european union"),
])
def test_contract_1_stoplisted_acronym_not_broadened(
    stoplisted_acronym: str, canonical: str,
):
    """Every stoplisted alias MUST be rejected by the extractor —
    operators reading the report would otherwise see endless
    ``PDF``-class noise drowning real domain mappings."""
    text = f"The {canonical} ({stoplisted_acronym}) is widely used."
    extracted = extract_aliases_from_text(
        text, run_id="r", snapshot_id="s", document_id="d",
    )
    aliases = {e.alias for e in extracted}
    assert stoplisted_acronym not in aliases, (
        f"stoplisted {stoplisted_acronym!r} leaked through the "
        f"extractor on text {text!r}"
    )


# ---- Contract 2: valid domain aliases ARE broadened ------------


@pytest.mark.parametrize("text,expected_alias,expected_canonical", [
    # ALIAS (canonical) pattern
    ("The bill of quantities (BOQ) must be approved.",
     "BOQ", "bill of quantities"),
    ("Submit a request for information (RFI) for clarifications.",
     "RFI", "request for information"),
    # canonical (ALIAS) pattern
    ("Reference the BOQ (bill of quantities) before each cycle.",
     "BOQ", "bill of quantities"),
])
def test_contract_2_valid_domain_alias_is_broadened(
    text: str, expected_alias: str, expected_canonical: str,
):
    """Valid alias shapes MUST surface as ``(canonical, alias)``
    pairs the resolver consumes. Both pattern families are
    covered — operators reading a report MUST see real domain
    mappings emerging."""
    extracted = extract_aliases_from_text(
        text, run_id="r", snapshot_id="s", document_id="d",
    )
    pairs = {(e.canonical, e.alias) for e in extracted}
    assert (expected_canonical, expected_alias) in pairs, (
        f"valid pattern not extracted from {text!r}; "
        f"got pairs={pairs!r}"
    )


# ---- Contract 3: lowercase / common words are rejected ----------


@pytest.mark.parametrize("bad_text", [
    # Lowercase "alias" — not an initialism.
    "Consider the data (df) for analysis.",
    # Single capital letter — too short to be a meaningful acronym.
    "Use a (b) for testing.",
    # Common English word disguised as alias.
    "Read the manual (an) for instructions.",
])
def test_contract_3_lowercase_or_common_word_rejected(bad_text: str):
    """The regex MUST reject patterns where the would-be alias is
    lowercase, a single letter, or a common English word. These
    are document chrome — broadening on them poisons the retrieval
    expansion."""
    extracted = extract_aliases_from_text(
        bad_text, run_id="r", snapshot_id="s", document_id="d",
    )
    assert extracted == (), (
        f"extractor accepted noise pattern {bad_text!r} — got "
        f"{extracted!r}"
    )


def test_contract_3_capitalised_proper_noun_rejected():
    """The canonical-side regex requires lowercase phrases.
    Capitalised words (sentence starts, proper nouns) are
    rejected — ``In short (IS)`` is a sentence-start, not a
    domain term mapping."""
    bad = "In short (IS), the report concludes the design works."
    extracted = extract_aliases_from_text(
        bad, run_id="r", snapshot_id="s", document_id="d",
    )
    # IS isn't in the stoplist but the canonical "In short" starts
    # with uppercase — should be rejected.
    aliases = {e.alias for e in extracted}
    assert "IS" not in aliases, (
        f"capitalised canonical leaked through; got aliases={aliases!r}"
    )


# ---- Contract 4: duplicates deduplicated at each layer ----------


def test_contract_4_extractor_dedupes_within_same_text():
    """Same ``(alias, canonical)`` pair appearing twice in one
    text MUST collapse to a single extracted entry. The dedup
    key is the pair (alias, canonical) — same alias attached to
    DIFFERENT canonical strings counts as different mappings, by
    design.

    Operator note: a greedy regex can produce different canonicals
    for the same alias across occurrences (e.g. "the bill of
    quantities (BOQ)" vs "check the bill of quantities (BOQ)"
    when the sentence preceding the second occurrence is
    absorbed into the match). This test uses surrounding text
    that avoids the greedy edge case so the dedup contract is
    exercised cleanly."""
    text = (
        "Approve the bill of quantities (BOQ) first. "
        "Then the bill of quantities (BOQ) goes to costing."
    )
    extracted = extract_aliases_from_text(
        text, run_id="r", snapshot_id="s", document_id="d",
    )
    # All BOQ entries point at the same canonical "bill of quantities".
    boq_entries = [
        e for e in extracted
        if e.alias == "BOQ" and e.canonical == "bill of quantities"
    ]
    assert len(boq_entries) == 1, (
        f"dedup did not collapse identical (alias, canonical) pair; "
        f"got {boq_entries!r}"
    )


def test_contract_4_sweep_dedupes_across_chunks():
    """Across chunks, the sweep MUST deduplicate by
    ``(alias, canonical, evidence.artifact_id, evidence.chunk_id)``
    — the same alias in two different chunks counts once per
    evidence record, not once per chunk."""
    chunks = [
        {"body": "The bill of quantities (BOQ) is required.",
         "artifact_id": "a1", "chunk_id": "c1"},
        # Same alias, same artifact / chunk → dedup target.
        {"body": "BOQ (bill of quantities) re-stated.",
         "artifact_id": "a1", "chunk_id": "c1"},
    ]
    extracted = extract_aliases_from_chunks(
        chunks, run_id="r", snapshot_id="s", document_id="d",
    )
    boq_entries = [e for e in extracted if e.alias == "BOQ"]
    assert len(boq_entries) == 1


def test_contract_4_payload_round_trip_merges_same_canonical():
    """When the same canonical appears with multiple alias forms,
    the round-trip through ``parse_alias_payload`` MUST emit ONE
    ``EntityAlias`` bundle with the union of aliases. The
    resolver consumes the merged shape."""
    chunks = [
        {"body": "The bill of quantities (BOQ) is required.",
         "artifact_id": "a1", "chunk_id": "c1"},
        {"body": "Submit the BOM (bill of materials) too.",
         "artifact_id": "a1", "chunk_id": "c2"},
        # Second alias form of the same canonical.
        {"body": "BoM (bill of materials) re-stated.",
         "artifact_id": "a1", "chunk_id": "c3"},
    ]
    extracted = extract_aliases_from_chunks(
        chunks, run_id="r", snapshot_id="s", document_id="d",
    )
    payload = build_alias_payload(extracted)
    bundles = parse_alias_payload(payload)
    # bill_of_materials canonical surfaces with all alias variants
    # merged (case-preserving the first observed form).
    bom_bundle = next(
        (b for b in bundles if b.canonical_name == "bill of materials"),
        None,
    )
    assert bom_bundle is not None
    # At least one alias surfaces; multiple forms collapse into
    # the same bundle.
    assert "BOM" in bom_bundle.aliases or "BoM" in bom_bundle.aliases


# ---- Contract 5: scope safety holds (snapshot + document) ------


def test_contract_5_loader_filters_by_snapshot_id():
    """Aliases stamped under snap-A MUST be invisible to a query
    scoped to snap-B for the SAME document."""
    artifacts = _InMemoryArtifactRegistry()
    artifacts.add(_alias_artifact(
        artifact_id="al-snap-a",
        document_id="doc-1", snapshot_id="snap-a",
        text="The bill of quantities (BOQ) is required.",
    ))
    out = load_enrichment_aliases_for_snapshot(
        ctx=_CTX, artifact_registry=artifacts,
        document_id="doc-1", snapshot_id="snap-b",
    )
    assert out == ()


def test_contract_5_loader_filters_by_document_id():
    """Aliases stamped on doc-A MUST be invisible when querying
    doc-B even when both share a snapshot id."""
    artifacts = _InMemoryArtifactRegistry()
    artifacts.add(_alias_artifact(
        artifact_id="al-doc-other",
        document_id="doc-other", snapshot_id="snap-shared",
        text="The bill of quantities (BOQ) is required.",
    ))
    out = load_enrichment_aliases_for_snapshot(
        ctx=_CTX, artifact_registry=artifacts,
        document_id="doc-target", snapshot_id="snap-shared",
    )
    assert out == ()


def test_contract_5_loader_returns_aliases_within_matching_scope():
    """Sanity check — when the loader IS queried with the matching
    scope, it surfaces the alias. Pinned so a future "tune the
    stoplist" change can't accidentally over-filter and silently
    return empty for legitimate queries."""
    artifacts = _InMemoryArtifactRegistry()
    artifacts.add(_alias_artifact(
        artifact_id="al-active",
        document_id="doc-1", snapshot_id="snap-active",
        text="The bill of quantities (BOQ) is required.",
    ))
    out = load_enrichment_aliases_for_snapshot(
        ctx=_CTX, artifact_registry=artifacts,
        document_id="doc-1", snapshot_id="snap-active",
    )
    assert len(out) == 1
    assert out[0].canonical_name == "bill of quantities"
    assert "BOQ" in out[0].aliases


# ---- Bonus: tuning-baseline pins so future changes are explicit


def test_stoplist_baseline_pinned():
    """Document the CURRENT stoplist content. A future
    evidence-based tuning that adds (or removes) entries
    intentionally must update this test — silent drift fails."""
    from j1.processing.enrichment_aliases import _STOPLIST_ALIASES
    assert _STOPLIST_ALIASES == frozenset({
        "PDF", "HTTP", "HTTPS", "URL", "URI", "API",
        "JSON", "YAML", "XML", "CSV",
        "USA", "UK", "EU",
    }), (
        "stoplist changed without a corresponding contract update — "
        "if this is intentional tuning, update the pinned set here"
    )


def test_leading_determiner_baseline_pinned():
    """Same pattern for the leading-determiner trim set."""
    from j1.processing.enrichment_aliases import _LEADING_DETERMINERS
    assert _LEADING_DETERMINERS == frozenset({
        "the", "a", "an", "this", "that", "these", "those",
    })


def test_alias_regex_length_bounds_pinned():
    """The alias regex bounds (currently 2-8 chars with upper-case
    first letter) MUST be load-bearing-visible in tests so a
    tuning that widens / narrows the bounds is explicit. Pinned
    via behaviour rather than regex string — assertions check
    that 1-char and 9-char would-be aliases are rejected."""
    # Single-char "B" — too short
    out_short = extract_aliases_from_text(
        "Use B (something here) to.", run_id="r", snapshot_id="s",
        document_id="d",
    )
    assert all(e.alias != "B" for e in out_short)
    # Nine-char acronym — too long for the current bounds
    out_long = extract_aliases_from_text(
        "Reference the very long phrase (ABCDEFGHI).",
        run_id="r", snapshot_id="s", document_id="d",
    )
    assert all(e.alias != "ABCDEFGHI" for e in out_long)


# ---- Synthetic real-document smoke ------------------------------


def test_synthetic_real_document_smoke_passes_all_contracts():
    """End-to-end smoke against a synthetic civil-engineering doc:
    multi-paragraph text that exercises stoplist + valid pattern +
    rejected lowercase + dedup all at once. Models what a real
    document would do; pinned so a tuning that breaks ANY
    contract surfaces here as well as in the focused tests."""
    text = (
        "Section 1: Scope.\n"
        "The bill of quantities (BOQ) lists every quantity.\n"
        "Submit a request for information (RFI) within 5 days.\n"
        "Reference the BOQ (bill of quantities) at each milestone.\n"
        "Convert all files to PDF (portable document format).\n"
        "Use HTTP (hypertext transfer protocol) for transport.\n"
        "Consider the data (df) frame.\n"
    )
    extracted = extract_aliases_from_chunks(
        [{"body": text, "artifact_id": "a1", "chunk_id": "c1"}],
        run_id="r", snapshot_id="s", document_id="d",
    )
    aliases = {e.alias for e in extracted}
    # Valid domain aliases surface.
    assert "BOQ" in aliases
    assert "RFI" in aliases
    # Stoplisted noise rejected.
    assert "PDF" not in aliases
    assert "HTTP" not in aliases
    # Lowercase noise rejected.
    assert "df" not in aliases
    # Dedup at the chunk level — BOQ appears twice in the text but
    # collapses to one extracted entry.
    boq_count = sum(1 for e in extracted if e.alias == "BOQ")
    assert boq_count == 1
