"""Wave 12 — documentation guards + small cleanup.

Pins:
  1. Wave 10–11 docs exist + describe the new architecture (not the
     legacy one).
  2. Docs do not describe split-mode / pre-compile gating as active
     architecture.
  3. `image_summaries[].warnings` carries per-image errors lifted
     from `metadata.error` (Wave-12 small cleanup).
"""

from __future__ import annotations

from pathlib import Path

import pytest


_DOCS_ROOT = Path(__file__).resolve().parent.parent / "docs"


# ---- 1. Required docs exist ---------------------------------------


_REQUIRED_DOCS: tuple[Path, ...] = (
    _DOCS_ROOT / "architecture" / "ingestion-pipeline.md",
    _DOCS_ROOT / "architecture" / "domain-profiles.md",
    _DOCS_ROOT / "architecture" / "enrichment-overlay.md",
    _DOCS_ROOT / "architecture" / "final-ingestion-report.md",
    _DOCS_ROOT / "guides" / "adding-a-domain-profile.md",
    _DOCS_ROOT / "guides" / "adding-an-enrichment-module.md",
    _DOCS_ROOT / "operations" / "production-worker-wiring.md",
    _DOCS_ROOT / "reference" / "artifacts.md",
    _DOCS_ROOT / "reference" / "ui-copy.md",
    _DOCS_ROOT / "tech-debt.md",
)


@pytest.mark.parametrize("doc", _REQUIRED_DOCS)
def test_required_doc_exists(doc):
    assert doc.is_file(), f"missing doc: {doc.relative_to(_DOCS_ROOT.parent)}"


@pytest.mark.parametrize("doc", _REQUIRED_DOCS)
def test_required_doc_is_non_trivial(doc):
    """Every doc must carry at least 500 characters of content so
    a future maintainer doesn't ship a placeholder."""
    text = doc.read_text(encoding="utf-8")
    assert len(text) >= 500, (
        f"{doc.name} is only {len(text)} chars; expected substantive content"
    )


# ---- 2. No legacy vocabulary as ACTIVE architecture ---------------


# Operator-visible architectural terms we explicitly retired. The
# new docs only mention them in the "Retired wording" section.
_RETIRED_AS_ACTIVE_TERMS: tuple[str, ...] = (
    "split_mode is the recommended",
    "split mode is the recommended",
    "use split mode",
    "use split_mode",
    "pre-compile graph gating",
    "pre-compile index gating",
    "graph gating is recommended",
    "index gating is recommended",
    "pre-compile final decision is",
)


@pytest.mark.parametrize("doc", _REQUIRED_DOCS)
def test_doc_does_not_recommend_legacy_architecture(doc):
    """Docs may MENTION the legacy vocabulary in retired-wording
    tables, but must not describe them as active architecture."""
    text = doc.read_text(encoding="utf-8").lower()
    for term in _RETIRED_AS_ACTIVE_TERMS:
        assert term.lower() not in text, (
            f"{doc.name} appears to describe {term!r} as active architecture"
        )


# ---- 3. Docs reference the new architecture explicitly -----------


def test_pipeline_doc_mentions_post_compile_overlay():
    text = (
        _DOCS_ROOT / "architecture" / "ingestion-pipeline.md"
    ).read_text(encoding="utf-8")
    # Must describe the new pipeline shape — post-compile overlay
    # NOT pre-compile gating.
    assert "post-compile" in text.lower()
    assert "overlay" in text.lower()
    assert "final_ingestion_report" in text


def test_final_report_doc_describes_all_six_final_statuses():
    text = (
        _DOCS_ROOT / "architecture" / "final-ingestion-report.md"
    ).read_text(encoding="utf-8")
    for status in (
        "completed_with_enrichment",
        "completed_without_enrichment",
        "completed_with_enrichment_warnings",
        "failed_compile",
        "failed_enrichment_required",
        "failed_finalization",
    ):
        assert status in text, f"final-report doc missing {status}"


def test_runbook_mentions_production_wiring_steps():
    text = (
        _DOCS_ROOT / "operations" / "production-worker-wiring.md"
    ).read_text(encoding="utf-8")
    # Must mention the three deps the activity needs.
    assert "enrichment_text_client" in text
    assert "enrichment_vision_client" in text
    assert "enrichment_llm_call_limiter" in text
    # Must describe the per-run image adapter construction.
    assert "PerImageVisionAdapter" in text
    assert "WorkspaceImageBytesProvider" in text


def test_domain_profile_guide_warns_against_hardcoding():
    text = (
        _DOCS_ROOT / "guides" / "adding-a-domain-profile.md"
    ).read_text(encoding="utf-8")
    # Must warn against `if domain == ...` branches in code.
    assert "if pack.id ==" in text or "if domain ==" in text or (
        "do not" in text.lower() and "hardcode" in text.lower()
    )
    assert "DomainPromptPack" in text or "DomainPack" in text


def test_module_guide_pins_prompt_resolution_precedence():
    text = (
        _DOCS_ROOT / "guides" / "adding-an-enrichment-module.md"
    ).read_text(encoding="utf-8")
    assert "resolve_module_prompt" in text
    assert "prompt_addon" in text
    assert "provenance" in text.lower()


def test_artifact_reference_lists_every_pipeline_artifact():
    text = (
        _DOCS_ROOT / "reference" / "artifacts.md"
    ).read_text(encoding="utf-8")
    for kind in (
        "initial_execution_plan",
        "compile_result_summary",
        "post_compile_enrich_plan",
        "enrichment_result",
        "final_ingestion_report",
        "final_summary",
    ):
        assert kind in text


def test_tech_debt_doc_records_known_asymmetries():
    text = (
        _DOCS_ROOT / "tech-debt.md"
    ).read_text(encoding="utf-8").lower()
    assert "skipped_reason" in text
    assert "image_summaries" in text
    assert "detectedimage" in text
    assert "production worker" in text or "staging" in text or "prod" in text


# ---- 4. Small cleanup — image_summaries[].warnings projection ----


def test_image_summary_warnings_carry_per_image_error():
    """The Wave-12 cleanup: when `PerImageVisionAdapter` records
    a per-image error on `entry.metadata.error`, the image module
    lifts that into `ImageSummary.warnings[]` so the typed overlay
    is the operator's trace path (not the harder-to-surface
    metadata field)."""
    from j1.processing.compile_result import (
        DetectedImage, NormalizedCompileResult,
    )
    from j1.processing.enrich_assessment import (
        EnrichRecommendation, PostCompileEnrichPlan,
    )
    from j1.processing.enrichment_modules import EnrichmentContext
    from j1.processing.legacy_enricher_modules import ImageEnrichmentModule

    class _FakeVisionAnalysisClient:
        """Returns the adapter-shape payload directly — `images: [
        {image_id, metadata: {error: ...}}]` — so the wrapper sees
        the per-image error and can lift it."""

        def analyze(self, prompt, schema, *, metadata=None):
            return ({
                "images": [
                    {
                        "image_id": "art-1",
                        "caption": None,
                        "metadata": {"error": "TimeoutError: vendor 429"},
                    },
                ],
            }, None)

    ctx = EnrichmentContext(
        document_id="doc-1",
        compile_result=NormalizedCompileResult(
            document_id="doc-1",
            status="succeeded",
            raw_artifact_refs=("raw-1",),
            detected_images=(DetectedImage(image_id="i-0", page=1),),
        ),
        enrich_plan=PostCompileEnrichPlan(
            overall_recommendation=EnrichRecommendation.OPTIONAL,
        ),
        domain_pack=None,
    )
    mod = ImageEnrichmentModule(vision_client=_FakeVisionAnalysisClient())
    mod.run(ctx)
    typed = mod.get_typed_outputs()
    summaries = typed.get("image_summaries") or ()
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.image_id == "art-1"
    # Per-image error lifted onto the typed overlay's warnings.
    assert len(summary.warnings) == 1
    assert "TimeoutError" in summary.warnings[0]
    assert "per-image vision call failed" in summary.warnings[0]


def test_image_summary_warnings_empty_when_no_error():
    """Happy-path images carry no extra warnings."""
    from j1.processing.compile_result import (
        DetectedImage, NormalizedCompileResult,
    )
    from j1.processing.enrich_assessment import (
        EnrichRecommendation, PostCompileEnrichPlan,
    )
    from j1.processing.enrichment_modules import EnrichmentContext
    from j1.processing.legacy_enricher_modules import ImageEnrichmentModule

    class _FakeVisionAnalysisClient:
        def analyze(self, prompt, schema, *, metadata=None):
            return ({
                "images": [{"image_id": "art-1", "caption": "site plan"}],
            }, None)

    ctx = EnrichmentContext(
        document_id="doc-1",
        compile_result=NormalizedCompileResult(
            document_id="doc-1", status="succeeded",
            raw_artifact_refs=("raw-1",),
            detected_images=(DetectedImage(image_id="i-0", page=1),),
        ),
        enrich_plan=PostCompileEnrichPlan(
            overall_recommendation=EnrichRecommendation.OPTIONAL,
        ),
        domain_pack=None,
    )
    mod = ImageEnrichmentModule(vision_client=_FakeVisionAnalysisClient())
    mod.run(ctx)
    summary = mod.get_typed_outputs()["image_summaries"][0]
    assert summary.warnings == ()
    assert summary.caption == "site plan"
