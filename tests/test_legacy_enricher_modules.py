"""Wave 10.5 — tests for the legacy-enricher wrapper modules.

Pins:
  1. Each wrapper conforms to the `EnrichmentModule` protocol.
  2. Wrappers consume `DomainPromptPack` prompt overrides + apply
     `prompt_addon` consistently.
  3. Wrappers route LLM calls through the shared `LLMCallLimiter`.
  4. Skip semantics: no LLM client → skip; no detected tables /
     images → skip; no extracted text → skip.
  5. Failure semantics: LLM raise → FAILED outcome with error.
  6. Typed outputs include `ProvenanceLink` records.
  7. `NormalizedCompileResult` is not mutated.
  8. `EnrichmentResult` serialisation includes ClassificationResult /
     TableSummary / ImageSummary / retrieval_hints / confidence_notes.
  9. No split_mode / pre-compile gating language anywhere.
 10. No hardcoded civil engineering vocabulary in the wrappers.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from j1.domains.models import (
    DomainEnrichmentPolicy,
    DomainExtractionHints,
    DomainPack,
    DomainPromptPack,
    DomainValidationRules,
)
from j1.processing.compile_result import (
    DetectedImage,
    DetectedTable,
    NormalizedCompileResult,
)
from j1.processing.enrich_assessment import (
    EnrichRecommendation,
    PostCompileEnrichPlan,
)
from j1.processing.enrichment_modules import (
    CompositeEnrichmentRunner,
    EnrichmentContext,
)
from j1.processing.enrichment_overlay import (
    ClassificationResult,
    EnrichmentModule,
    EnrichmentModuleStatus,
    EnrichmentResult,
    ImageSummary,
    ProvenanceLink,
    TableSummary,
)
from j1.processing.legacy_enricher_modules import (
    ClassificationEnrichmentModule,
    DEFAULT_CLASSIFICATION_PROMPT,
    DEFAULT_IMAGE_ENRICHMENT_PROMPT,
    DEFAULT_TABLE_ENRICHMENT_PROMPT,
    DEFAULT_TEXT_ENRICHMENT_PROMPT,
    ImageEnrichmentModule,
    MODULE_ID_CLASSIFICATION_ENRICHMENT,
    MODULE_ID_IMAGE_ENRICHMENT,
    MODULE_ID_TABLE_ENRICHMENT,
    MODULE_ID_TEXT_ENRICHMENT,
    TableEnrichmentModule,
    TextEnrichmentModule,
    build_legacy_enricher_modules,
)


# ---- Fakes ---------------------------------------------------------


class _FakeUsage:
    def __init__(
        self, model: str = "fake-model", input_tokens: int = 100,
        output_tokens: int = 50,
    ) -> None:
        self.model = model
        self.provider = "fake"
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.duration_ms = 1000


class _FakeTextClient:
    """Fake text LLM client matching the legacy `_text_client.extract`
    signature (prompt, schema, metadata) → (parsed, usage)."""

    def __init__(self, response: dict[str, Any] | None = None,
                 raise_exc: Exception | None = None) -> None:
        self._response = response or {}
        self._raise = raise_exc
        self.calls: list[tuple[str, dict, dict | None]] = []

    def extract(self, prompt: str, schema: dict,
                metadata: dict | None = None) -> tuple[dict, _FakeUsage]:
        self.calls.append((prompt, dict(schema), dict(metadata or {})))
        if self._raise:
            raise self._raise
        return self._response, _FakeUsage()


class _FakeVisionClient:
    """Fake vision LLM client matching `analyze_image(prompt, schema,
    metadata)` → (parsed, usage). Wave-10.5 wrappers call the same
    arity as the text client."""

    def __init__(self, response: dict[str, Any] | None = None,
                 raise_exc: Exception | None = None) -> None:
        self._response = response or {}
        self._raise = raise_exc
        self.calls: list[tuple[str, dict, dict | None]] = []

    def analyze(self, prompt: str, schema: dict,
                metadata: dict | None = None) -> tuple[dict, _FakeUsage]:
        self.calls.append((prompt, dict(schema), dict(metadata or {})))
        if self._raise:
            raise self._raise
        return self._response, _FakeUsage()


class _FakeLimiter:
    """Fake LLMCallLimiter — records every call so tests can assert
    the wrapper routes through the limiter, not directly to the
    client."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, callable_, *args, metadata=None):
        self.calls.append({"metadata": metadata or {}, "argc": len(args)})
        return callable_(*args)


# ---- Fixtures ------------------------------------------------------


def _compile_result(
    *,
    chunks: int = 5,
    text_chars: int = 5_000,
    tables: int = 0,
    images: int = 0,
    raw_refs: tuple[str, ...] = ("raw-1",),
) -> NormalizedCompileResult:
    return NormalizedCompileResult(
        document_id="doc-1",
        status="succeeded",
        raw_artifact_refs=raw_refs,
        chunks_count=chunks,
        extracted_text_chars=text_chars,
        detected_tables=tuple(
            DetectedTable(table_id=f"t-{i}", page=1)
            for i in range(tables)
        ),
        detected_images=tuple(
            DetectedImage(image_id=f"i-{i}", page=1)
            for i in range(images)
        ),
    )


def _enrich_plan(**overrides) -> PostCompileEnrichPlan:
    base = dict(
        overall_recommendation=EnrichRecommendation.OPTIONAL,
        reasons=(),
        recommended_tasks=(),
        skipped_tasks=(),
    )
    base.update(overrides)
    return PostCompileEnrichPlan(**base)


def _domain_pack(
    *,
    pack_id: str = "test_domain",
    text_prompt: str | None = None,
    classification_prompt: str | None = None,
    table_prompt: str | None = None,
    image_prompt: str | None = None,
    addon: str = "",
) -> DomainPack:
    return DomainPack(
        id=pack_id,
        display_name="Test Domain",
        version="1.0",
        prompt_addon=addon,
        prompt_pack=DomainPromptPack(
            text_enrichment_prompt=text_prompt,
            classification_prompt=classification_prompt,
            table_enrichment_prompt=table_prompt,
            image_enrichment_prompt=image_prompt,
        ),
        extraction_hints=DomainExtractionHints(),
        validation_rules=DomainValidationRules(),
        enrichment_policy=DomainEnrichmentPolicy(),
    )


def _ctx(
    compile_result: NormalizedCompileResult | None = None,
    domain_pack: DomainPack | None = None,
) -> EnrichmentContext:
    return EnrichmentContext(
        document_id="doc-1",
        compile_result=compile_result or _compile_result(),
        enrich_plan=_enrich_plan(),
        domain_pack=domain_pack,
    )


# ---- 1. Protocol conformance --------------------------------------


@pytest.mark.parametrize("wrapper", [
    TextEnrichmentModule, ClassificationEnrichmentModule,
    TableEnrichmentModule, ImageEnrichmentModule,
])
def test_wrapper_conforms_to_enrichment_module_protocol(wrapper):
    instance = wrapper()
    assert isinstance(instance, EnrichmentModule)
    assert isinstance(instance.module_id, str) and instance.module_id


def test_wrapper_module_ids_are_stable():
    assert TextEnrichmentModule().module_id == "text_enrichment"
    assert ClassificationEnrichmentModule().module_id == "classification_enrichment"
    assert TableEnrichmentModule().module_id == "table_enrichment"
    assert ImageEnrichmentModule().module_id == "image_enrichment"


# ---- 2. Skip semantics --------------------------------------------


def test_text_wrapper_skips_when_no_llm_client():
    mod = TextEnrichmentModule(text_client=None)
    ok, reason = mod.can_run(_ctx())
    assert ok is False
    assert "no text LLM client" in reason


def test_text_wrapper_skips_when_no_extracted_text():
    mod = TextEnrichmentModule(text_client=_FakeTextClient())
    ok, reason = mod.can_run(_ctx(_compile_result(text_chars=0, chunks=0)))
    assert ok is False
    assert "no" in reason.lower()


def test_classification_wrapper_skips_when_no_extracted_text():
    mod = ClassificationEnrichmentModule(text_client=_FakeTextClient())
    ok, reason = mod.can_run(_ctx(_compile_result(text_chars=0)))
    assert ok is False


def test_table_wrapper_skips_when_no_detected_tables():
    mod = TableEnrichmentModule(text_client=_FakeTextClient())
    ok, reason = mod.can_run(_ctx(_compile_result(tables=0)))
    assert ok is False
    assert "no tables" in reason.lower()


def test_image_wrapper_skips_when_no_detected_images():
    mod = ImageEnrichmentModule(vision_client=_FakeVisionClient())
    ok, reason = mod.can_run(_ctx(_compile_result(images=0)))
    assert ok is False
    assert "no images" in reason.lower()


def test_image_wrapper_skips_when_no_vision_client():
    mod = ImageEnrichmentModule(vision_client=None)
    ok, reason = mod.can_run(_ctx(_compile_result(images=2)))
    assert ok is False
    assert "no vision LLM client" in reason


# ---- 3. DomainPromptPack consumption -------------------------------


def test_text_wrapper_uses_domain_pack_text_prompt_override():
    fake = _FakeTextClient(response={"requirements": []})
    mod = TextEnrichmentModule(text_client=fake)
    pack = _domain_pack(text_prompt="DOMAIN-SPECIFIC TEXT PROMPT")
    mod.run(_ctx(domain_pack=pack))
    assert len(fake.calls) == 1
    prompt = fake.calls[0][0]
    assert "DOMAIN-SPECIFIC TEXT PROMPT" in prompt
    # Builtin default should NOT appear when override is set
    # (resolve_module_prompt returns override OR builtin, never both).
    assert "Extract key requirements" not in prompt


def test_text_wrapper_uses_builtin_when_pack_has_no_override():
    fake = _FakeTextClient(response={"requirements": []})
    mod = TextEnrichmentModule(text_client=fake)
    # No domain pack — wrapper falls back to its builtin default.
    mod.run(_ctx(domain_pack=None))
    prompt = fake.calls[0][0]
    assert "Extract key requirements" in prompt


def test_text_wrapper_prepends_prompt_addon():
    fake = _FakeTextClient(response={"requirements": []})
    mod = TextEnrichmentModule(text_client=fake)
    pack = _domain_pack(addon="DOMAIN ADDON: civil engineering context")
    mod.run(_ctx(domain_pack=pack))
    prompt = fake.calls[0][0]
    # Addon is prepended, then base prompt.
    assert prompt.startswith("DOMAIN ADDON: civil engineering context")
    assert "Extract key requirements" in prompt


def test_classification_wrapper_uses_pack_classification_prompt():
    fake = _FakeTextClient(response={"category": "method_statement"})
    mod = ClassificationEnrichmentModule(text_client=fake)
    pack = _domain_pack(classification_prompt="CLASSIFY-FOR-THIS-DOMAIN")
    mod.run(_ctx(domain_pack=pack))
    assert "CLASSIFY-FOR-THIS-DOMAIN" in fake.calls[0][0]


def test_table_wrapper_uses_pack_table_prompt():
    fake = _FakeTextClient(response={"tables": []})
    mod = TableEnrichmentModule(text_client=fake)
    pack = _domain_pack(table_prompt="DOMAIN-TABLE-PROMPT")
    mod.run(_ctx(
        compile_result=_compile_result(tables=1), domain_pack=pack,
    ))
    assert "DOMAIN-TABLE-PROMPT" in fake.calls[0][0]


def test_image_wrapper_uses_pack_image_prompt():
    fake = _FakeVisionClient(response={"images": []})
    mod = ImageEnrichmentModule(vision_client=fake)
    pack = _domain_pack(image_prompt="DOMAIN-IMAGE-PROMPT")
    mod.run(_ctx(
        compile_result=_compile_result(images=1), domain_pack=pack,
    ))
    assert "DOMAIN-IMAGE-PROMPT" in fake.calls[0][0]


# ---- 4. LLMCallLimiter usage --------------------------------------


def test_text_wrapper_routes_through_limiter():
    fake = _FakeTextClient(response={"requirements": [{"text": "X"}]})
    limiter = _FakeLimiter()
    mod = TextEnrichmentModule(text_client=fake, llm_call_limiter=limiter)
    mod.run(_ctx())
    assert len(limiter.calls) == 1
    # Limiter was invoked exactly once for this run; the underlying
    # client was also called (limiter delegates the call).
    assert len(fake.calls) == 1


def test_image_wrapper_delegates_limiter_to_adapter():
    """Wave 11B — the image module no longer wraps the adapter's
    outer `analyze` call with the module-side limiter. Instead the
    adapter owns per-image limiter acquisition. The image module's
    own `llm_call_limiter` kwarg is therefore inert for the image
    path; tests that need per-image acquisition checks should
    drive the adapter directly with its own limiter (see the Wave-
    11B adapter test in test_per_image_limiter_acquires_once_per_image).
    """
    fake = _FakeVisionClient(response={"images": [{"image_id": "i-0"}]})
    limiter = _FakeLimiter()
    mod = ImageEnrichmentModule(
        vision_client=fake, llm_call_limiter=limiter,
    )
    mod.run(_ctx(compile_result=_compile_result(images=2)))
    # Module-side limiter is not invoked because the image path
    # uses the adapter directly. The adapter (constructed by the
    # activity) is the entity that holds + acquires the limiter
    # per image — see the Wave-11B per-image limiter tests.
    assert limiter.calls == []


def test_factory_threads_shared_limiter_into_every_wrapper():
    """`build_legacy_enricher_modules` must pass the same limiter
    instance to all 4 wrappers — critical for global concurrency
    bounds."""
    limiter = _FakeLimiter()
    text = _FakeTextClient(response={})
    vision = _FakeVisionClient(response={})
    modules = build_legacy_enricher_modules(
        text_client=text, vision_client=vision, llm_call_limiter=limiter,
    )
    assert len(modules) == 4
    for m in modules:
        assert m._llm_call_limiter is limiter


def test_factory_handles_missing_clients_without_crashing():
    """Worker without LLM credentials (dev / test) — factory builds
    wrappers cleanly; `can_run` returns False with the documented
    reason."""
    modules = build_legacy_enricher_modules()
    assert len(modules) == 4
    for m in modules:
        ok, reason = m.can_run(_ctx(_compile_result(tables=2, images=2)))
        assert ok is False
        assert "no" in reason.lower() and "client" in reason.lower()


# ---- 5. Typed output projection -----------------------------------


def test_text_wrapper_produces_retrieval_hints_and_confidence_notes():
    fake = _FakeTextClient(response={
        "requirements": [
            {"id": "R1", "text": "The system MUST be fail-safe."},
            {"id": "R2", "text": "Reviews SHOULD occur quarterly."},
        ],
        "confidence_notes": ["ambiguous priority on R2"],
    })
    mod = TextEnrichmentModule(text_client=fake)
    outcome = mod.run(_ctx())
    assert outcome.status == EnrichmentModuleStatus.RUN
    typed = mod.get_typed_outputs()
    assert "fail-safe" in " ".join(typed["retrieval_hints"])
    assert "ambiguous priority" in typed["confidence_notes"][0]


def test_classification_wrapper_produces_classification_result():
    fake = _FakeTextClient(response={
        "category": "method_statement",
        "subcategory": "structural",
        "confidence": 0.87,
        "candidates": [
            {"category": "method_statement", "confidence": 0.87},
            {"category": "specification", "confidence": 0.13},
        ],
        "reasoning": "title + section 1 indicate method statement",
    })
    mod = ClassificationEnrichmentModule(text_client=fake)
    outcome = mod.run(_ctx())
    assert outcome.status == EnrichmentModuleStatus.RUN
    typed = mod.get_typed_outputs()
    result = typed["classification_result"]
    assert isinstance(result, ClassificationResult)
    assert result.category == "method_statement"
    assert result.subcategory == "structural"
    assert result.confidence == 0.87
    assert len(result.candidates) == 2


def test_table_wrapper_produces_table_summaries():
    fake = _FakeTextClient(response={
        "tables": [
            {
                "table_id": "t-0", "title": "BOQ", "summary": "Quantities",
                "column_names": ["Item", "Qty", "Unit"], "row_count": 14,
            },
        ],
    })
    mod = TableEnrichmentModule(text_client=fake)
    outcome = mod.run(_ctx(compile_result=_compile_result(tables=1)))
    assert outcome.status == EnrichmentModuleStatus.RUN
    typed = mod.get_typed_outputs()
    tables = typed["table_summaries"]
    assert len(tables) == 1
    assert isinstance(tables[0], TableSummary)
    assert tables[0].title == "BOQ"
    assert tables[0].column_names == ("Item", "Qty", "Unit")


def test_image_wrapper_produces_image_summaries():
    fake = _FakeVisionClient(response={
        "images": [
            {
                "image_id": "i-0", "caption": "site plan",
                "role": "diagram", "confidence": 0.78,
            },
        ],
    })
    mod = ImageEnrichmentModule(vision_client=fake)
    outcome = mod.run(_ctx(compile_result=_compile_result(images=1)))
    assert outcome.status == EnrichmentModuleStatus.RUN
    typed = mod.get_typed_outputs()
    images = typed["image_summaries"]
    assert len(images) == 1
    assert isinstance(images[0], ImageSummary)
    assert images[0].caption == "site plan"
    assert images[0].role == "diagram"


# ---- 6. Provenance carriage ---------------------------------------


def test_text_wrapper_carries_provenance_in_outcome():
    fake = _FakeTextClient(response={"requirements": [{"text": "X"}]})
    mod = TextEnrichmentModule(text_client=fake)
    outcome = mod.run(_ctx(_compile_result(raw_refs=("raw-abc",))))
    assert len(outcome.source_refs) == 1
    assert outcome.source_refs[0].source_artifact_id == "raw-abc"
    assert outcome.source_refs[0].source_kind == "compile"


def test_classification_wrapper_carries_provenance_on_result():
    fake = _FakeTextClient(response={"category": "spec"})
    mod = ClassificationEnrichmentModule(text_client=fake)
    mod.run(_ctx(_compile_result(raw_refs=("raw-xyz",))))
    result = mod.get_typed_outputs()["classification_result"]
    assert result.provenance.source_artifact_id == "raw-xyz"


def test_table_wrapper_carries_provenance_on_each_summary():
    fake = _FakeTextClient(response={
        "tables": [{"table_id": "t-0", "summary": "Q"}],
    })
    mod = TableEnrichmentModule(text_client=fake)
    mod.run(_ctx(
        compile_result=_compile_result(tables=1, raw_refs=("raw-z",)),
    ))
    tables = mod.get_typed_outputs()["table_summaries"]
    assert tables[0].provenance.source_artifact_id == "raw-z"


# ---- 7. Failure semantics -----------------------------------------


def test_text_wrapper_records_failed_outcome_on_llm_exception():
    fake = _FakeTextClient(raise_exc=RuntimeError("LLM down"))
    mod = TextEnrichmentModule(text_client=fake)
    outcome = mod.run(_ctx())
    assert outcome.status == EnrichmentModuleStatus.FAILED
    assert "LLM down" in outcome.errors[0]
    # Typed outputs are not populated on failure.
    assert mod.get_typed_outputs() == {}


def test_classification_wrapper_records_partial_on_missing_category():
    fake = _FakeTextClient(response={"reasoning": "but no category!"})
    mod = ClassificationEnrichmentModule(text_client=fake)
    outcome = mod.run(_ctx())
    assert outcome.status == EnrichmentModuleStatus.PARTIAL
    assert "no top-level category" in outcome.reason


def test_table_wrapper_records_partial_on_empty_response():
    fake = _FakeTextClient(response={"tables": []})
    mod = TableEnrichmentModule(text_client=fake)
    outcome = mod.run(_ctx(_compile_result(tables=2)))
    # LLM returned tables=[] for detected tables — partial outcome.
    assert outcome.status == EnrichmentModuleStatus.PARTIAL


# ---- 8. Runner integration ----------------------------------------


def test_runner_merges_typed_outputs_from_wrappers():
    fake_text = _FakeTextClient(response={
        "requirements": [{"text": "MUST review quarterly"}],
        "confidence_notes": [],
    })
    fake_vision = _FakeVisionClient(response={
        "images": [{"image_id": "i-0", "caption": "plan"}],
    })
    fake_table = _FakeTextClient(response={
        "tables": [{"table_id": "t-0", "summary": "Q", "row_count": 5}],
    })
    runner = CompositeEnrichmentRunner(modules=[
        TextEnrichmentModule(text_client=fake_text),
        ClassificationEnrichmentModule(
            text_client=_FakeTextClient(response={"category": "spec"}),
        ),
        TableEnrichmentModule(text_client=fake_table),
        ImageEnrichmentModule(vision_client=fake_vision),
    ])
    ctx = _ctx(_compile_result(tables=1, images=1))
    result = runner.run(ctx)
    assert isinstance(result, EnrichmentResult)
    # Top-level aggregated typed outputs are populated.
    assert result.classification_result is not None
    assert result.classification_result.category == "spec"
    assert len(result.table_summaries) == 1
    assert len(result.image_summaries) == 1
    assert any("quarterly" in h for h in result.retrieval_hints)


def test_runner_skips_wrapper_modules_with_clear_reasons():
    """Worker without LLM clients still produces a clean
    EnrichmentResult; each wrapper is SKIPPED with documented
    reason."""
    runner = CompositeEnrichmentRunner(modules=[
        TextEnrichmentModule(),  # no client → skip
        TableEnrichmentModule(),
        ImageEnrichmentModule(),
    ])
    ctx = _ctx(_compile_result(tables=2, images=2))
    result = runner.run(ctx)
    skipped = [
        o for o in result.module_outcomes
        if o.status == EnrichmentModuleStatus.SKIPPED
    ]
    assert len(skipped) == 3
    for o in skipped:
        assert "no" in o.reason.lower() and "client" in o.reason.lower()


# ---- 9. Compile result not mutated --------------------------------


def test_wrappers_do_not_mutate_compile_result():
    fake = _FakeTextClient(response={"requirements": []})
    cr = _compile_result(tables=2, images=2)
    raw_refs_before = cr.raw_artifact_refs
    chunks_before = cr.chunks_count
    detected_tables_before = cr.detected_tables
    TextEnrichmentModule(text_client=fake).run(_ctx(cr))
    TableEnrichmentModule(text_client=fake).run(_ctx(cr))
    ImageEnrichmentModule(vision_client=_FakeVisionClient()).run(_ctx(cr))
    # All fields unchanged (NormalizedCompileResult is frozen
    # already, but assert explicitly so a future thaw doesn't
    # silently introduce mutation).
    assert cr.raw_artifact_refs == raw_refs_before
    assert cr.chunks_count == chunks_before
    assert cr.detected_tables == detected_tables_before


# ---- 10. EnrichmentResult serialisation ---------------------------


def test_enrichment_result_serialization_includes_new_typed_outputs():
    fake_text = _FakeTextClient(response={"category": "spec"})
    fake_table = _FakeTextClient(response={
        "tables": [{"table_id": "t-0", "summary": "Q"}],
    })
    fake_vision = _FakeVisionClient(response={
        "images": [{"image_id": "i-0", "caption": "diagram"}],
    })
    runner = CompositeEnrichmentRunner(modules=[
        ClassificationEnrichmentModule(text_client=fake_text),
        TableEnrichmentModule(text_client=fake_table),
        ImageEnrichmentModule(vision_client=fake_vision),
    ])
    ctx = _ctx(_compile_result(tables=1, images=1))
    result = runner.run(ctx)
    payload = result.to_payload()
    assert payload["classification_result"] is not None
    assert payload["classification_result"]["category"] == "spec"
    assert len(payload["table_summaries"]) == 1
    assert len(payload["image_summaries"]) == 1


def test_enrichment_result_round_trip_preserves_typed_outputs():
    fake = _FakeTextClient(response={
        "tables": [{"table_id": "t-0", "title": "BOQ", "summary": "Q"}],
    })
    runner = CompositeEnrichmentRunner(modules=[
        TableEnrichmentModule(text_client=fake),
    ])
    ctx = _ctx(_compile_result(tables=1))
    result = runner.run(ctx)
    payload = result.to_payload()
    rebuilt = EnrichmentResult.from_payload(payload)
    assert len(rebuilt.table_summaries) == 1
    assert rebuilt.table_summaries[0].title == "BOQ"


# ---- 11. Legacy-vocabulary guards ---------------------------------


def test_wrapper_module_has_no_legacy_vocabulary():
    """The wrapper module is operator-visible (its prompts ship to
    LLMs + its source is read by maintainers). Must stay free of
    split-mode / pre-compile-gating / IngestPlanner terminology."""
    from j1.processing import legacy_enricher_modules
    src = inspect.getsource(legacy_enricher_modules)
    for forbidden in (
        "split_mode", "SplitMode", "split mode",
        "insert_content",
        "pre_compile_gating", "PreCompileGating",
        "graph gating", "index gating",
        "IngestPlanner",
    ):
        assert forbidden not in src, (
            f"legacy vocabulary {forbidden!r} leaked into the wrappers"
        )


def test_wrappers_have_no_hardcoded_civil_engineering_terms():
    """Wave 10.5 explicitly forbids hardcoding civil-engineering
    prompts in the wrappers — domain specialisation goes through
    `DomainPromptPack`. The defaults must stay generic."""
    civil_specific = (
        "RFI", "BOQ", "method statement",
        "civil_engineering", "structural drawing",
    )
    defaults = (
        DEFAULT_TEXT_ENRICHMENT_PROMPT,
        DEFAULT_CLASSIFICATION_PROMPT,
        DEFAULT_TABLE_ENRICHMENT_PROMPT,
        DEFAULT_IMAGE_ENRICHMENT_PROMPT,
    )
    for prompt in defaults:
        for term in civil_specific:
            assert term not in prompt, (
                f"civil-engineering term {term!r} leaked into a "
                f"default wrapper prompt"
            )


def test_default_prompts_lead_with_documented_verbs():
    """Operator readability check — every default prompt opens
    with the documented action verb so legacy operators recognise
    the parity with the old enricher prompts."""
    assert DEFAULT_TEXT_ENRICHMENT_PROMPT.startswith("Extract")
    assert DEFAULT_CLASSIFICATION_PROMPT.startswith("Classify")
    assert DEFAULT_TABLE_ENRICHMENT_PROMPT.startswith("Summarise")
    assert DEFAULT_IMAGE_ENRICHMENT_PROMPT.startswith("Describe")
