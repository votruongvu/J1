"""Tests for the vendor-neutral AssessmentPlan + RAGAnything mapper.

Two layers:

  1. Profile → AssessmentPlan via `DefaultAssessmentPlanner`. Pure
     rule-based; no I/O, no vendor imports.
  2. AssessmentPlan → RAGAnything `CompileConfig` via
     `map_assessment_to_raganything_config`. The only place
     RAGAnything-specific knowledge of the plan lives.

The user's spec calls for 10 specific test cases; this file covers
each, plus a vendor-neutrality check that fails CI if anyone
adds a `parse_method` / `parser` / `mineru_*` field to the core
`AssessmentPlan` dataclass.
"""

from __future__ import annotations

import inspect

import pytest

from j1.processing.assessment import (
    AssessmentPlan,
    Capability,
    CompileMode,
    Complexity,
    DefaultAssessmentPlanner,
    FallbackPolicy,
)
from j1.processing.profiling import DocumentProfile
from j1.providers.raganything.plan_mapper import (
    CompileCapabilityUnsupported,
    CompileConfig,
    map_assessment_to_raganything_config,
)
from j1.providers.raganything.settings import RAGAnythingSettings


def _profile(**overrides) -> DocumentProfile:
    """DocumentProfile builder with defaults (`.pdf`, unknown signals)."""
    base = dict(
        document_id="doc-1",
        extension=".pdf",
        mime_type="application/pdf",
        file_size_bytes=10_000,
        page_count=10,
        text_extractable_ratio=None,
        has_images=None,
        has_tables=None,
        has_scanned_pages=None,
    )
    base.update(overrides)
    return DocumentProfile(**base)


def _settings(**overrides) -> RAGAnythingSettings:
    """RAGAnythingSettings builder. Tests construct settings DIRECTLY
    via the dataclass (bypassing the env loader's URL guard) since
    the plan mapper only reads `parse_method` + the supports_*
    fields, not the VLM URL."""
    base = dict(
        parse_method="auto",
        backend="vlm-http-client",
        vlm_http_server_url="http://stub:1/v1",
    )
    base.update(overrides)
    return RAGAnythingSettings(**base)


# ---- 1) plain text → STANDARD compile mode (two-mode model) -----


def test_plain_text_extension_produces_standard_plan():
    """Two-mode model: plain text → STANDARD compile mode with
    LOW complexity, only TEXT_EXTRACTION required, and confidence
    1.0. The bridge takes a plaintext bypass for these extensions
    independently — the compile mode itself is standard."""
    profile = _profile(
        extension=".txt", page_count=1,
        text_extractable_ratio=1.0,
        has_images=False, has_tables=False, has_scanned_pages=False,
    )
    plan = DefaultAssessmentPlanner().assess(profile)
    assert plan.mode == CompileMode.STANDARD
    assert plan.complexity == Complexity.LOW
    assert plan.required_capabilities == frozenset({Capability.TEXT_EXTRACTION})
    assert plan.confidence == 1.0


# ---- 2) standard profile produces standard plan -------------------


def test_standard_profile_for_readable_pdf_with_tables_produces_standard_plan():
    """A readable PDF with tables flagged should land in standard
    mode with TABLE_EXTRACTION required."""
    profile = _profile(
        extension=".pdf",
        text_extractable_ratio=0.95,
        has_images=False,
        has_tables=True,
        has_scanned_pages=False,
    )
    plan = DefaultAssessmentPlanner().assess(profile)
    assert plan.mode == CompileMode.STANDARD
    assert Capability.TEXT_EXTRACTION in plan.required_capabilities
    assert Capability.LAYOUT_DETECTION in plan.required_capabilities
    assert Capability.TABLE_EXTRACTION in plan.required_capabilities


# ---- 3) deep profile when OCR required ----------------------------


def test_deep_profile_when_ocr_required_produces_deep_plan_with_ocr():
    """Scanned PDF (text_extractable_ratio < 0.1) → deep mode +
    OCR + LAYOUT_DETECTION required. Risk flag surfaces the
    quality concern for a future optimisation pass."""
    profile = _profile(
        extension=".pdf",
        text_extractable_ratio=0.0,
        has_scanned_pages=True,
        has_images=True,
    )
    plan = DefaultAssessmentPlanner().assess(profile)
    assert plan.mode == CompileMode.DEEP
    assert plan.complexity == Complexity.HIGH
    assert Capability.OCR in plan.required_capabilities
    assert Capability.LAYOUT_DETECTION in plan.required_capabilities
    assert Capability.IMAGE_EXTRACTION in plan.required_capabilities
    assert any("scanned" in flag for flag in plan.risk_flags)


def test_deep_profile_image_only_extension_triggers_ocr():
    """`.tiff` and similar scan-only extensions are treated as
    deep + OCR even when other signals are unknown."""
    profile = _profile(extension=".tiff", page_count=1)
    plan = DefaultAssessmentPlanner().assess(profile)
    assert plan.mode == CompileMode.DEEP
    assert Capability.OCR in plan.required_capabilities


# ---- 3.5) FAST mode is reserved for 100%-text extensions only ----
#
# Spec: a binary container (PDF / DOCX / PPTX / image) might carry
# images, tables, or scanned regions that need VLM / OCR. The
# planner MUST never choose FAST for those — even if every other
# signal looks clean. Only files where the bytes ARE the content
# (txt / md / json / yaml / toml / log / config) qualify.


@pytest.mark.parametrize("extension", [
    ".txt", ".md", ".markdown", ".rst", ".log",
    ".json", ".jsonl", ".ndjson",
    ".yaml", ".yml",
    ".toml",
    ".tsv",
    ".ini", ".cfg", ".conf", ".env",
])
def test_text_only_extensions_assigned_standard_mode(extension):
    """Two-mode model: every 100%-text extension lands in STANDARD
    mode. The bridge's plaintext-bypass optimisation kicks in
    separately based on extension — independent of the compile
    mode, which has no FAST option."""
    profile = _profile(extension=extension, page_count=1)
    plan = DefaultAssessmentPlanner().assess(profile)
    assert plan.mode == CompileMode.STANDARD, (
        f"{extension} should map to STANDARD mode; got {plan.mode}"
    )
    # The reason still surfaces the plaintext-bypass hint so audit
    # logs explain why this STANDARD compile is cheap.
    assert "plain-text" in plan.reason.lower() or (
        "plaintext bypass" in plan.reason.lower()
    )


@pytest.mark.parametrize("extension", [
    ".pdf",
    ".docx", ".doc",
    ".pptx", ".ppt",
    ".xlsx", ".xls",
    ".odt", ".ods", ".odp",
    ".rtf",
    ".pages", ".numbers", ".key",
])
def test_binary_container_extensions_never_assigned_fast_mode(extension):
    """Spec contract: PDF / DOCX / PPTX / XLSX / etc. are binary
    containers that may carry images, tables, or scanned regions
    invisible to the file extension. The planner must NEVER choose
    FAST for these — even when every signal looks clean."""
    profile = _profile(
        extension=extension,
        page_count=10,
        text_extractable_ratio=1.0,
        has_images=False,
        has_tables=False,
        has_scanned_pages=False,
    )
    plan = DefaultAssessmentPlanner().assess(profile)
    assert plan.mode != CompileMode.FAST, (
        f"{extension} must NEVER map to FAST mode; got {plan.mode}. "
        "Binary containers may carry vision content the file extension "
        "doesn't reveal."
    )


def test_legacy_fast_mode_coerces_to_standard_regardless_of_extension():
    """Two-mode model: FAST is removed from the official vocabulary.
    The safety belt coerces ANY FAST plan to STANDARD — even for
    100%-text extensions where pre-refactor FAST was the answer.
    Operators see the migration in `reason`."""
    from j1.processing.assessment import _enforce_fast_mode_safety

    legacy_plan = AssessmentPlan(
        document_id="d", mode=CompileMode.FAST,
        document_type="pdf", complexity=Complexity.LOW, confidence=1.0,
        required_capabilities=frozenset({Capability.TEXT_EXTRACTION}),
        reason="legacy plan persisted before the two-mode refactor",
    )
    pdf_profile = _profile(extension=".pdf", page_count=1)
    coerced = _enforce_fast_mode_safety(legacy_plan, pdf_profile)
    assert coerced.mode == CompileMode.STANDARD
    assert "migrated legacy FAST→STANDARD" in coerced.reason
    # Standard baseline capabilities are added; pre-existing
    # required caps survive.
    assert Capability.TEXT_EXTRACTION in coerced.required_capabilities
    assert Capability.LAYOUT_DETECTION in coerced.required_capabilities


def test_legacy_fast_mode_coercion_also_fires_for_text_extension():
    """Belt fires uniformly. A FAST plan for a .txt file is still
    coerced to STANDARD — no extension exemption in the two-mode
    model."""
    from j1.processing.assessment import _enforce_fast_mode_safety

    legacy_plan = AssessmentPlan(
        document_id="d", mode=CompileMode.FAST,
        document_type="plain_text",
        complexity=Complexity.LOW, confidence=1.0,
        required_capabilities=frozenset({Capability.TEXT_EXTRACTION}),
        reason="legacy plain-text FAST plan",
    )
    txt_profile = _profile(extension=".txt", page_count=1)
    coerced = _enforce_fast_mode_safety(legacy_plan, txt_profile)
    assert coerced.mode == CompileMode.STANDARD
    assert "migrated legacy FAST→STANDARD" in coerced.reason


def test_safety_belt_noop_for_already_standard():
    """The belt only acts on FAST plans. STANDARD/DEEP pass through
    untouched."""
    from j1.processing.assessment import _enforce_fast_mode_safety

    standard_plan = AssessmentPlan(
        document_id="d", mode=CompileMode.STANDARD,
        document_type="pdf",
        complexity=Complexity.MEDIUM, confidence=0.9,
        required_capabilities=frozenset({Capability.TEXT_EXTRACTION}),
        reason="standard reason",
    )
    pdf_profile = _profile(extension=".pdf", page_count=1)
    same = _enforce_fast_mode_safety(standard_plan, pdf_profile)
    assert same is standard_plan or (
        same.mode == CompileMode.STANDARD
        and same.reason == "standard reason"
    )


# ---- RecommendedProcessingPath: derived from mode + capabilities --


def test_recommended_path_standard_for_plain_text():
    """Two-mode model: plain text → STANDARD compile mode → the
    operator-intent path is STANDARD_COMPILE."""
    from j1.processing.assessment import RecommendedProcessingPath

    profile = _profile(extension=".txt", page_count=1)
    plan = DefaultAssessmentPlanner().assess(profile)
    assert plan.mode == CompileMode.STANDARD
    assert plan.recommended_path == RecommendedProcessingPath.STANDARD_COMPILE


def test_recommended_path_deep_for_scanned_pdf():
    """Scanned PDF / weak text layer / scan-only extension → OCR
    capability required → path is DEEP_COMPILE."""
    from j1.processing.assessment import RecommendedProcessingPath

    profile = _profile(
        extension=".pdf",
        text_extractable_ratio=0.0,
        has_scanned_pages=True,
    )
    plan = DefaultAssessmentPlanner().assess(profile)
    assert Capability.OCR in plan.required_capabilities
    assert plan.recommended_path == RecommendedProcessingPath.DEEP_COMPILE


def test_recommended_path_standard_for_pdf_with_tables():
    """A readable PDF with table signals → STANDARD compile mode
    with TABLE_EXTRACTION capability → path is STANDARD_COMPILE.
    Standard already handles multimodal capability flags; rich
    content does not by itself escalate to deep."""
    from j1.processing.assessment import RecommendedProcessingPath

    profile = _profile(
        extension=".pdf",
        text_extractable_ratio=0.95,
        has_tables=True,
    )
    plan = DefaultAssessmentPlanner().assess(profile)
    assert plan.mode == CompileMode.STANDARD
    assert plan.recommended_path == RecommendedProcessingPath.STANDARD_COMPILE


def test_recommended_path_standard_default():
    """Clean PDF with no flagged signals → STANDARD_COMPILE."""
    from j1.processing.assessment import RecommendedProcessingPath

    profile = _profile(
        extension=".pdf",
        text_extractable_ratio=0.95,
        page_count=5,
    )
    plan = DefaultAssessmentPlanner().assess(profile)
    assert plan.recommended_path == RecommendedProcessingPath.STANDARD_COMPILE


def test_recommended_path_round_trips_through_payload():
    """`to_payload` carries the path, `from_payload` reads it back.
    Critical for the workflow→activity boundary."""
    from j1.processing.assessment import RecommendedProcessingPath

    profile = _profile(extension=".txt", page_count=1)
    plan = DefaultAssessmentPlanner().assess(profile)
    payload = plan.to_payload()
    assert payload["recommended_path"] == "standard_compile"
    rehydrated = AssessmentPlan.from_payload(payload)
    assert rehydrated.recommended_path == RecommendedProcessingPath.STANDARD_COMPILE


def test_recommended_path_from_payload_tolerates_missing_field():
    """Older payloads (pre-API-shape-refactor) omit the field.
    `from_payload` must fall back to STANDARD_COMPILE rather
    than crash."""
    from j1.processing.assessment import RecommendedProcessingPath

    legacy_payload = {
        "schema_version": "1",
        "document_id": "d",
        "mode": "standard",
        "document_type": "pdf",
        "complexity": "medium",
        "confidence": 0.8,
        "required_capabilities": ["text_extraction"],
        "optional_capabilities": [],
        "risk_flags": [],
        "fallback_policy": "degrade_with_warning",
        "reason": "legacy",
    }
    plan = AssessmentPlan.from_payload(legacy_payload)
    assert plan.recommended_path == RecommendedProcessingPath.STANDARD_COMPILE


def test_recommended_path_from_payload_coerces_legacy_values():
    """Legacy values from before the two-mode refactor must coerce
    on the read path:
      * `fast_text_compile`  → STANDARD_COMPILE
      * `multimodal_compile` → STANDARD_COMPILE
      * `ocr_parse`          → DEEP_COMPILE
    The actual recommended_path stored in legacy artifacts is one
    of these strings; replaying them MUST NOT crash."""
    from j1.processing.assessment import RecommendedProcessingPath

    def _payload(path_value: str) -> dict:
        return {
            "schema_version": "1",
            "document_id": "d",
            "mode": "standard",
            "document_type": "pdf",
            "complexity": "medium",
            "confidence": 0.8,
            "required_capabilities": [],
            "optional_capabilities": [],
            "risk_flags": [],
            "fallback_policy": "degrade_with_warning",
            "reason": "legacy",
            "recommended_path": path_value,
        }

    assert AssessmentPlan.from_payload(
        _payload("fast_text_compile"),
    ).recommended_path == RecommendedProcessingPath.STANDARD_COMPILE
    assert AssessmentPlan.from_payload(
        _payload("multimodal_compile"),
    ).recommended_path == RecommendedProcessingPath.STANDARD_COMPILE
    assert AssessmentPlan.from_payload(
        _payload("ocr_parse"),
    ).recommended_path == RecommendedProcessingPath.DEEP_COMPILE


def test_recommended_path_from_payload_tolerates_unknown_value():
    """Future-proofing: a payload that includes a path the current
    worker doesn't recognise must not crash replay. Fallback to
    STANDARD_COMPILE."""
    from j1.processing.assessment import RecommendedProcessingPath

    future_payload = {
        "schema_version": "2",
        "document_id": "d",
        "mode": "standard",
        "document_type": "pdf",
        "complexity": "medium",
        "confidence": 0.8,
        "required_capabilities": [],
        "optional_capabilities": [],
        "risk_flags": [],
        "fallback_policy": "degrade_with_warning",
        "reason": "from a future worker",
        "recommended_path": "quantum_compile",
    }
    plan = AssessmentPlan.from_payload(future_payload)
    assert plan.recommended_path == RecommendedProcessingPath.STANDARD_COMPILE


def test_planner_never_emits_legacy_compile_mode():
    """Two-mode invariant: across the planner's rule surface the
    only modes emitted are STANDARD and DEEP. FAST is removed from
    the official vocabulary; the safety belt coerces any legacy
    FAST before it leaves `assess()`."""
    planner = DefaultAssessmentPlanner()
    profiles = [
        _profile(extension=".txt", page_count=1),
        _profile(extension=".json", page_count=1),
        _profile(extension=".pdf",
                 text_extractable_ratio=0.95, has_tables=True),
        _profile(extension=".pdf", text_extractable_ratio=0.0,
                 has_scanned_pages=True),
        _profile(extension=".xlsx"),
        _profile(extension=".pdf",
                 text_extractable_ratio=0.95, page_count=4),
        _profile(extension=".tiff"),
    ]
    for p in profiles:
        plan = planner.assess(p)
        assert plan.mode in {CompileMode.STANDARD, CompileMode.DEEP}, (
            f"planner emitted {plan.mode} for {p.extension}; "
            "FAST is no longer in the two-mode vocabulary"
        )


def test_extraction_evidence_block_built_from_compile_result():
    """The workflow's `_build_extraction_evidence` helper derives
    the block from `compile_result.content_stats` +
    `compile_metrics`. Verify the field mapping + that chunks are
    NEVER claimed here (chunking_status='pending_verification')."""
    from types import SimpleNamespace

    from j1.orchestration.workflows.project_processing import (
        _build_extraction_evidence,
    )

    compile_result = SimpleNamespace(
        status="succeeded",
        content_stats={
            "provider": "raganything",
            "parser_engine": "raganything.parse_document",
            "total_text_chars": 8421,
            "text_block_count": 12,
            "page_count": 5,
            "has_images": True,
            "has_tables": False,
            "image_count": 3,
        },
        compile_metrics={
            "chunks_count": 99,  # ← MUST NOT bleed into extraction evidence
            "extracted_text_chars": 8421,
        },
    )
    block = _build_extraction_evidence(compile_result)
    assert block["parser"] == "raganything"
    assert block["parser_method"] == "raganything.parse_document"
    assert block["text_char_count"] == 8421
    assert block["content_block_count"] == 12
    assert block["page_count"] == 5
    assert "text" in block["detected_content_types"]
    assert "images" in block["detected_content_types"]
    assert "tables" not in block["detected_content_types"]
    # CHUNK COUNT MUST NOT APPEAR HERE — chunks are verified
    # separately. The block always reports pending_verification.
    assert "chunks_count" not in block
    assert "chunk_count" not in block
    assert block["chunking_status"] == "pending_verification"


def test_extraction_evidence_block_none_compile_result_safe():
    """Defensive: missing compile_result → safe empty block.
    The bridge calls this with None when persisting a strategy
    report before any compile attempt completes."""
    from j1.orchestration.workflows.project_processing import (
        _build_extraction_evidence,
    )

    block = _build_extraction_evidence(None)
    assert block["parser"] == "raganything"
    assert block["parser_method"] is None
    assert block["text_char_count"] is None
    assert block["content_block_count"] is None
    assert block["detected_content_types"] == []
    assert block["page_count"] is None
    assert block["chunking_status"] == "pending_verification"


# ---- 4) fast plan maps to txt / light behaviour ------------------


def test_fast_plan_maps_to_parse_method_txt_in_raganything_adapter():
    plan = AssessmentPlan(
        document_id="d", mode=CompileMode.FAST,
        document_type="plain_text", complexity=Complexity.LOW,
        confidence=1.0,
        required_capabilities=frozenset({Capability.TEXT_EXTRACTION}),
    )
    config = map_assessment_to_raganything_config(plan, _settings())
    assert config.parse_method == "txt"
    # Fast mode disables image / equation processing by default.
    assert config.enable_image_processing is False
    assert config.enable_equation_processing is False
    # Tables off too unless explicitly required.
    assert config.enable_table_processing is False


def test_fast_plan_with_required_table_capability_enables_tables():
    plan = AssessmentPlan(
        document_id="d", mode=CompileMode.FAST,
        document_type="plain_text", complexity=Complexity.LOW,
        confidence=1.0,
        required_capabilities=frozenset({
            Capability.TEXT_EXTRACTION, Capability.TABLE_EXTRACTION,
        }),
    )
    config = map_assessment_to_raganything_config(plan, _settings())
    assert config.parse_method == "txt"
    assert config.enable_table_processing is True


# ---- 5) standard plan maps to parse_method=auto -------------------


def test_standard_plan_maps_to_parse_method_auto():
    plan = AssessmentPlan(
        document_id="d", mode=CompileMode.STANDARD,
        document_type="pdf", complexity=Complexity.MEDIUM,
        confidence=0.85,
        required_capabilities=frozenset({
            Capability.TEXT_EXTRACTION,
            Capability.LAYOUT_DETECTION,
            Capability.TABLE_EXTRACTION,
        }),
    )
    config = map_assessment_to_raganything_config(plan, _settings())
    assert config.parse_method == "auto"
    assert config.enable_table_processing is True
    assert config.enable_image_processing is True  # standard default
    assert config.resolved_mode == "standard"


# ---- 6) deep plan maps to parse_method=ocr when OCR required ------


def test_deep_plan_with_ocr_required_maps_to_parse_method_ocr():
    plan = AssessmentPlan(
        document_id="d", mode=CompileMode.DEEP,
        document_type="pdf", complexity=Complexity.HIGH,
        confidence=0.85,
        required_capabilities=frozenset({
            Capability.TEXT_EXTRACTION, Capability.LAYOUT_DETECTION,
            Capability.OCR,
        }),
    )
    config = map_assessment_to_raganything_config(plan, _settings())
    assert config.parse_method == "ocr"
    # Deep mode enables image / table / equation processing by default.
    assert config.enable_image_processing is True
    assert config.enable_table_processing is True
    assert config.enable_equation_processing is True


def test_deep_plan_without_ocr_falls_back_to_auto():
    """Deep mode but OCR not required (e.g. complex but text-
    extractable layout) → `auto`, not `ocr`."""
    plan = AssessmentPlan(
        document_id="d", mode=CompileMode.DEEP,
        document_type="pdf", complexity=Complexity.HIGH,
        confidence=0.85,
        required_capabilities=frozenset({
            Capability.TEXT_EXTRACTION, Capability.LAYOUT_DETECTION,
        }),
    )
    config = map_assessment_to_raganything_config(plan, _settings())
    assert config.parse_method == "auto"


# ---- 7) env defaults are fallback only ----------------------------


def test_env_default_parse_method_does_not_override_plan_choice():
    """Even if `J1_RAGANYTHING_PARSE_METHOD=auto` (the env default),
    a fast plan must still resolve to `txt`. Plan > env."""
    plan = AssessmentPlan(
        document_id="d", mode=CompileMode.FAST,
        document_type="plain_text", complexity=Complexity.LOW,
        confidence=1.0,
        required_capabilities=frozenset({Capability.TEXT_EXTRACTION}),
    )
    settings = _settings(parse_method="auto")  # env default
    config = map_assessment_to_raganything_config(plan, settings)
    assert config.parse_method == "txt"  # plan wins, not "auto"


def test_env_allowed_parse_methods_constrains_plan_choice():
    """When the operator restricts the deployment via an allow-list,
    a plan that requests an out-of-list method is degraded to the
    deployment default with a warning. Env is the operator's
    safety hatch — settings carry the allow-list as a real field
    populated by `load_raganything_settings`."""
    plan = AssessmentPlan(
        document_id="d", mode=CompileMode.FAST,
        document_type="plain_text", complexity=Complexity.LOW,
        confidence=1.0,
        required_capabilities=frozenset({Capability.TEXT_EXTRACTION}),
    )
    settings = _settings(parse_method="auto", allowed_parse_methods=("auto",))
    config = map_assessment_to_raganything_config(plan, settings)
    # `txt` not in allow-list → fall back to the env default (`auto`).
    assert config.parse_method == "auto"
    assert any(
        "allow-list" in w or "fallback" in w.lower() or "falling back" in w
        for w in config.warnings
    )


# ---- 8) AssessmentPlan does not contain vendor-specific fields ----


def test_assessment_plan_dataclass_is_vendor_neutral():
    """Lock the AssessmentPlan field set so a future change can't
    sneak `parse_method` / `parser` / `mineru_*` / `raganything_*`
    into the vendor-neutral contract. The mapper translates; the
    plan describes intent only."""
    forbidden_substrings = (
        "parse_method", "parser", "mineru", "raganything",
        "vlm", "backend",
    )
    field_names = [f.name for f in inspect.signature(AssessmentPlan).parameters.values()]
    leaks = [
        f for f in field_names
        if any(sub in f.lower() for sub in forbidden_substrings)
    ]
    assert not leaks, (
        f"AssessmentPlan must stay vendor-neutral; found leaked fields: {leaks}"
    )


# ---- 9) process_document_complete NOT called during assessment ----


def test_planner_does_not_call_process_document_complete(monkeypatch):
    """The AssessmentPlanner runs against a pre-parsed
    `DocumentProfile`; it MUST never trigger heavy compile work.
    Guard against a future change that pulls in RAGAnything for
    "deeper signals" — the cost would defeat the whole point of
    pre-compile assessment."""
    # Set a tripwire on the bridge's compile entrypoint. Importing
    # it lazily so the test doesn't pay the import cost when it
    # asserts non-call.
    called: list[str] = []

    def _tripwire(*args, **kwargs):
        called.append("default_compile")

    from j1.providers.raganything import _bridge
    monkeypatch.setattr(_bridge, "default_compile", _tripwire)

    profile = _profile(extension=".pdf", text_extractable_ratio=0.9)
    plan = DefaultAssessmentPlanner().assess(profile)
    assert isinstance(plan, AssessmentPlan)
    assert called == [], (
        "DefaultAssessmentPlanner must not invoke RAGAnything's "
        "default_compile during assessment"
    )


# ---- 10) Unsupported capability degrades with warning ------------


def test_unsupported_capability_records_warning_under_default_policy():
    """Default `fallback_policy=DEGRADE_WITH_WARNING`: a required
    capability the deployment can't honour (per the real
    `supports_image` settings field) produces a warning on the
    CompileConfig, NOT a hard failure. The compile still runs with
    whatever the parser CAN do, and operators see the gap."""
    plan = AssessmentPlan(
        document_id="d", mode=CompileMode.STANDARD,
        document_type="pdf", complexity=Complexity.MEDIUM,
        confidence=0.85,
        required_capabilities=frozenset({
            Capability.TEXT_EXTRACTION, Capability.IMAGE_EXTRACTION,
        }),
        fallback_policy=FallbackPolicy.DEGRADE_WITH_WARNING,
    )
    settings = _settings(supports_image=False)
    config = map_assessment_to_raganything_config(plan, settings)
    assert isinstance(config, CompileConfig)
    assert any(
        "image" in w.lower() and "unsupported" in w.lower()
        for w in config.warnings
    ), config.warnings


def test_unsupported_capability_raises_under_fail_policy():
    """`fallback_policy=FAIL` is the strict mode for callers that
    can't proceed without the capability. Mapper raises
    `CompileCapabilityUnsupported` so the compile activity records
    a stage failure with the missing capability in the message."""
    plan = AssessmentPlan(
        document_id="d", mode=CompileMode.STANDARD,
        document_type="pdf", complexity=Complexity.MEDIUM,
        confidence=0.85,
        required_capabilities=frozenset({
            Capability.TEXT_EXTRACTION, Capability.FORMULA_EXTRACTION,
        }),
        fallback_policy=FallbackPolicy.FAIL,
    )
    settings = _settings(supports_equation=False)
    with pytest.raises(CompileCapabilityUnsupported) as excinfo:
        map_assessment_to_raganything_config(plan, settings)
    assert excinfo.value.capability == Capability.FORMULA_EXTRACTION


# ---- Bonus: bridge wiring (backward-compat) ----------------------


# ---- Config-overrides split (RAGAnythingConfig flags) -----------


def test_standard_plan_with_table_cap_sets_enable_table_processing_true():
    """Standard plan + TABLE_EXTRACTION required → mapper emits
    `enable_table_processing=True` on the config-overrides slice
    AND on the CompileConfig field. The bridge applies the slice
    onto `RAGAnythingConfig` before parser invocation."""
    plan = AssessmentPlan(
        document_id="d", mode=CompileMode.STANDARD,
        document_type="pdf", complexity=Complexity.MEDIUM,
        confidence=0.85,
        required_capabilities=frozenset({
            Capability.TEXT_EXTRACTION,
            Capability.TABLE_EXTRACTION,
        }),
    )
    config = map_assessment_to_raganything_config(plan, _settings())
    assert config.enable_table_processing is True
    overrides = config.to_config_overrides()
    assert overrides["enable_table_processing"] is True


def test_fast_plan_disables_image_and_equation_config_flags():
    """Fast plan with NO image/formula capabilities required → mapper
    emits `enable_image_processing=False` and
    `enable_equation_processing=False` so RAGAnythingConfig skips the
    expensive paths. Table also off (fast mode default)."""
    plan = AssessmentPlan(
        document_id="d", mode=CompileMode.FAST,
        document_type="plain_text", complexity=Complexity.LOW,
        confidence=1.0,
        required_capabilities=frozenset({Capability.TEXT_EXTRACTION}),
    )
    config = map_assessment_to_raganything_config(plan, _settings())
    assert config.enable_image_processing is False
    assert config.enable_equation_processing is False
    assert config.enable_table_processing is False


def test_deep_plan_enables_image_table_equation_config_flags():
    """Deep plan → all three RAGAnythingConfig flags True regardless
    of whether each capability is in `required_capabilities`. The
    deep mode's whole point is to maximise extraction quality, so
    the per-capability defaults flip ON together."""
    plan = AssessmentPlan(
        document_id="d", mode=CompileMode.DEEP,
        document_type="pdf", complexity=Complexity.HIGH,
        confidence=0.85,
        required_capabilities=frozenset({
            Capability.TEXT_EXTRACTION,
            Capability.LAYOUT_DETECTION,
            Capability.OCR,
        }),
    )
    config = map_assessment_to_raganything_config(plan, _settings())
    assert config.enable_image_processing is True
    assert config.enable_table_processing is True
    assert config.enable_equation_processing is True


def test_parse_method_still_maps_correctly_alongside_config_flags():
    """The CompileConfig split (parser_kwargs vs config_overrides)
    must NOT regress the parse_method mapping. Each mode still
    resolves the same way — txt / auto / ocr — independent of
    the per-capability flag values."""
    fast = map_assessment_to_raganything_config(
        AssessmentPlan(
            document_id="d", mode=CompileMode.FAST,
            document_type="plain_text", complexity=Complexity.LOW,
            confidence=1.0,
            required_capabilities=frozenset({Capability.TEXT_EXTRACTION}),
        ),
        _settings(),
    )
    standard = map_assessment_to_raganything_config(
        AssessmentPlan(
            document_id="d", mode=CompileMode.STANDARD,
            document_type="pdf", complexity=Complexity.MEDIUM,
            confidence=0.85,
            required_capabilities=frozenset({Capability.TEXT_EXTRACTION}),
        ),
        _settings(),
    )
    deep_with_ocr = map_assessment_to_raganything_config(
        AssessmentPlan(
            document_id="d", mode=CompileMode.DEEP,
            document_type="pdf", complexity=Complexity.HIGH,
            confidence=0.85,
            required_capabilities=frozenset({
                Capability.TEXT_EXTRACTION, Capability.OCR,
            }),
        ),
        _settings(),
    )
    assert fast.to_parser_kwargs() == {"parse_method": "txt"}
    assert standard.to_parser_kwargs() == {"parse_method": "auto"}
    assert deep_with_ocr.to_parser_kwargs() == {"parse_method": "ocr"}


def test_to_parser_kwargs_does_not_leak_config_overrides():
    """`to_parser_kwargs` must only return values that are valid
    `process_document_complete` kwargs. Per-capability switches
    (config-layer concern) MUST NOT appear here."""
    plan = AssessmentPlan(
        document_id="d", mode=CompileMode.STANDARD,
        document_type="pdf", complexity=Complexity.MEDIUM,
        confidence=0.85,
        required_capabilities=frozenset({
            Capability.TEXT_EXTRACTION,
            Capability.IMAGE_EXTRACTION,
            Capability.TABLE_EXTRACTION,
        }),
    )
    parser_kwargs = map_assessment_to_raganything_config(
        plan, _settings(),
    ).to_parser_kwargs()
    assert "enable_image_processing" not in parser_kwargs
    assert "enable_table_processing" not in parser_kwargs
    assert "enable_equation_processing" not in parser_kwargs


def test_compile_request_assessment_plan_field_defaults_to_none():
    """`RAGAnythingCompileRequest.assessment_plan` defaults to None
    so legacy callers + every existing test keep working without
    constructing a plan. The bridge falls back to settings.parse_method
    in this case."""
    from j1.providers.raganything.compiler import RAGAnythingCompileRequest
    from j1.projects.context import ProjectContext
    req = RAGAnythingCompileRequest(
        ctx=ProjectContext(tenant_id="t", project_id="p"),
        document_id="d",
        settings=_settings(),
        text_client=None,
        vision_client=None,
        embedding_client=None,
    )
    assert req.assessment_plan is None
