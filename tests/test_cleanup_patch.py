"""Cleanup-patch tests (post-).

Covers the five cleanup objectives:

1. Dead-code removal — confirms the deleted modules can't be
 imported and aren't referenced by active workflow code.
2. Typed domain contracts — `DomainExtractionHints`,
 `DomainValidationRules`, `DomainPromptPack` load from the civil
 YAML and stay empty for the generic pack.
3. Policy-override resolver — request > project > domain > system
 precedence.
4. CompileResult minimal signals — `metadata_presence` +
 `ContentRepresentationFlags`.
5. Analyzer consumes `InitialExecutionPlan` candidates + the
 resolved policy.
"""

from __future__ import annotations

import importlib

import pytest

from j1.domains.civil_engineering.pack import build_civil_engineering_pack
from j1.domains.general import build_general_pack
from j1.domains.models import (
    DomainEnrichmentPolicy,
    DomainExtractionHints,
    DomainPack,
    DomainPromptPack,
    DomainValidationRules,
)
from j1.orchestration.activities.payloads import ArtifactActivityResult
from j1.processing.compile_result import (
    ContentRepresentationFlags,
    MetadataPresence,
    NormalizedCompileResult,
    normalize_compile_result,
)
from j1.processing.enrich_assessment import (
    ENV_DOMAIN_ENRICHMENT_AUTO_ENABLED,
    TASK_REQUIREMENT_EXTRACTION,
    EnrichRecommendation,
    SourceSignals,
    assess_post_compile_enrich,
)


@pytest.fixture(autouse=True)
def _enable_auto_enrichment(monkeypatch):
    """Opt these planner-overlay tests into auto-enrichment so the
    rule-based + policy logic is exercised. The deployment-wide
    gate is OFF by default in production."""
    monkeypatch.setenv(ENV_DOMAIN_ENRICHMENT_AUTO_ENABLED, "true")
from j1.processing.enrichment_policy import (
    POLICY_SOURCE_DOMAIN,
    POLICY_SOURCE_PROJECT,
    POLICY_SOURCE_REQUEST,
    POLICY_SOURCE_SYSTEM_DEFAULT,
    SYSTEM_DEFAULT_POLICY,
    ResolvedEnrichmentPolicy,
    resolve_enrichment_policy,
)


# ---- 1. Dead-code removal -------------------------------------------


@pytest.mark.parametrize(
    "module_path",
    [
        "j1.processing.planning",
        "j1.processing.planning_llm",
        "j1.processing.post_compile_planning",
        "j1.processing.ingestion_profiles",
        "j1.orchestration.activities.planning",
    ],
)
def test_dead_module_no_longer_importable(module_path):
    """Hard-deletion: importing any of the dead modules must raise.
 A regression here means someone re-introduced the legacy code."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_path)


def test_active_workflow_does_not_reference_dead_modules():
    """Defensive: the workflow source must not import any of the
 deleted modules. Catches a half-applied revert."""
    import inspect
    from j1.orchestration.workflows import project_processing
    src = inspect.getsource(project_processing)
    forbidden = (
        "from j1.processing.planning ",
        "from j1.processing.planning_llm",
        "from j1.processing.post_compile_planning",
        "from j1.processing.ingestion_profiles",
        "from j1.orchestration.activities.planning ",
        "from j1.orchestration.activities.planning import",
    )
    for needle in forbidden:
        assert needle not in src, (
            f"workflow unexpectedly references deleted module: {needle!r}"
        )


# ---- 2. Typed domain contracts --------------------------------------


def test_civil_pack_carries_typed_extraction_hints():
    pack = build_civil_engineering_pack()
    hints = pack.extraction_hints
    assert isinstance(hints, DomainExtractionHints)
    # Sample population pins
    assert "project_number" in hints.metadata_fields
    assert "Contractor" in hints.entity_hints
    assert any("BOQ" in t for t in hints.table_hints)
    assert any("drawing" in i.lower() for i in hints.image_hints)
    assert any("RFI" in t for t in hints.terminology_hints)


def test_civil_pack_carries_typed_validation_rules():
    pack = build_civil_engineering_pack()
    rules = pack.validation_rules
    assert isinstance(rules, DomainValidationRules)
    assert "project_number" in rules.required_metadata_fields
    assert "document_date" in rules.required_metadata_fields
    assert rules.expected_document_structure
    assert rules.low_quality_warning_conditions
    assert rules.enrichment_triggers


def test_civil_pack_carries_typed_prompt_pack():
    pack = build_civil_engineering_pack()
    pp = pack.prompt_pack
    assert isinstance(pp, DomainPromptPack)
    assert pp.table_enrichment_prompt is not None
    assert "BOQ" in pp.table_enrichment_prompt
    assert pp.image_enrichment_prompt is not None
    # Text/metadata prompts intentionally not overridden — defaults apply
    assert pp.text_enrichment_prompt is None
    assert pp.metadata_enrichment_prompt is None


def test_general_pack_carries_empty_contracts():
    """The generic fallback pack must remain a no-op overlay: all
 new contracts default to empty so generic runs see no domain
 influence beyond the fallback context."""
    pack = build_general_pack()
    assert pack.extraction_hints == DomainExtractionHints()
    assert pack.validation_rules == DomainValidationRules()
    assert pack.prompt_pack == DomainPromptPack()


def test_domain_contracts_are_generic_no_civil_branches_in_models():
    """The typed contracts on `j1.domains.models` must not contain
 civil-specific CODE LOGIC. Docstrings may MENTION civil as an
 example (it's the sample pack), but no field name, conditional,
 or default value should encode civil vocabulary.

 Implementation: AST-walk the module, inspect identifiers + bool
 op operands + string literals OUTSIDE docstrings. Skip module/
 class/function docstrings entirely."""
    import ast
    import inspect
    from j1.domains import models

    tree = ast.parse(inspect.getsource(models))
    # Walk every node, skipping ast.Expr → ast.Constant(string) at
    # the head of a module/class/function body (docstrings).
    docstring_node_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(
                body[0].value, ast.Constant,
            ) and isinstance(body[0].value.value, str):
                docstring_node_ids.add(id(body[0].value))

    forbidden_terms = ("civil", "BOQ", "drawing", "RFI", "NCR")
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstring_node_ids:
                continue
            for term in forbidden_terms:
                assert term.lower() not in node.value.lower(), (
                    f"j1.domains.models leaks domain-specific string "
                    f"literal containing {term!r}: {node.value!r}"
                )
        if isinstance(node, ast.Name):
            for term in forbidden_terms:
                assert term.lower() not in node.id.lower(), (
                    f"j1.domains.models leaks identifier containing "
                    f"{term!r}: {node.id!r}"
                )


# ---- 3. Policy-override resolver ------------------------------------


def test_resolve_request_wins_over_all_lower_layers():
    resolved = resolve_enrichment_policy(
        request_override="never",
        project_default="always",
        domain_policy=DomainEnrichmentPolicy(policy="always"),
        system_default="auto",
    )
    assert resolved.policy == "never"
    assert resolved.source == POLICY_SOURCE_REQUEST


def test_resolve_project_wins_when_no_request_override():
    resolved = resolve_enrichment_policy(
        project_default="always",
        domain_policy=DomainEnrichmentPolicy(policy="never"),
    )
    assert resolved.policy == "always"
    assert resolved.source == POLICY_SOURCE_PROJECT


def test_resolve_domain_wins_when_no_request_or_project():
    resolved = resolve_enrichment_policy(
        domain_policy=DomainEnrichmentPolicy(policy="never"),
    )
    assert resolved.policy == "never"
    assert resolved.source == POLICY_SOURCE_DOMAIN


def test_resolve_system_default_when_no_other_layer():
    resolved = resolve_enrichment_policy()
    assert resolved.policy == SYSTEM_DEFAULT_POLICY == "auto"
    assert resolved.source == POLICY_SOURCE_SYSTEM_DEFAULT


def test_resolve_invalid_layers_fall_through():
    """A typo at the request layer should not crash — fall through
 to the next layer. Keeps deployment-level typos from breaking
 runs at the policy boundary."""
    resolved = resolve_enrichment_policy(
        request_override="aggressive",  # typo
        domain_policy=DomainEnrichmentPolicy(policy="always"),
    )
    assert resolved.policy == "always"
    assert resolved.source == POLICY_SOURCE_DOMAIN


def test_resolve_rejects_invalid_system_default():
    """The system default is the backstop — an invalid value there
 is a deployment-time bug, not a runtime fallback."""
    with pytest.raises(ValueError, match="not one of"):
        resolve_enrichment_policy(system_default="silent")


# ---- 4. CompileResult minimal signals -------------------------------


def test_compile_result_carries_metadata_presence_when_supplied():
    ar = ArtifactActivityResult(
        status="succeeded", artifact_ids=["a"], kinds=("chunk",),
        content_stats={"page_count": 5, "total_text_chars": 5000},
        compile_metrics={"chunks_count": 1, "extracted_text_chars": 5000},
    )
    presence = MetadataPresence(
        required_fields=("project_number", "document_date"),
        present_fields=("document_date",),
        missing_fields=("project_number",),
    )
    result = normalize_compile_result(
        ar, document_id="doc-1", metadata_presence=presence,
    )
    assert result.metadata_presence == presence


def test_compile_result_flags_tables_present_but_unstructured():
    """Bridge surfaced a table_count > 0 but no per-table
 descriptors → flag. 's table enricher will recommend
 enrichment for these."""
    ar = ArtifactActivityResult(
        status="succeeded", artifact_ids=["a"], kinds=("chunk",),
        content_stats={
            "table_count": 4, "image_count": 0,
            "page_count": 5, "total_text_chars": 5000,
        },
        compile_metrics={"chunks_count": 1},
    )
    result = normalize_compile_result(ar, document_id="doc-1")
    assert result.representation_flags.tables_present_but_unstructured is True
    assert result.representation_flags.images_present_but_undescribed is None


def test_compile_result_flags_images_present_but_undescribed():
    ar = ArtifactActivityResult(
        status="succeeded", artifact_ids=["a"], kinds=("chunk",),
        content_stats={
            "table_count": 0, "image_count": 3,
            "page_count": 5, "total_text_chars": 5000,
            "images": [
                {"image_id": "img-1"},
                {"image_id": "img-2"},
                {"image_id": "img-3"},
            ],
        },
        compile_metrics={"chunks_count": 1},
    )
    result = normalize_compile_result(ar, document_id="doc-1")
    assert result.representation_flags.images_present_but_undescribed is True


def test_compile_result_flags_text_only_low_density():
    """No tables/images + low chars/page average → flag."""
    ar = ArtifactActivityResult(
        status="succeeded", artifact_ids=["a"], kinds=("chunk",),
        content_stats={
            "table_count": 0, "image_count": 0,
            "page_count": 50, "total_text_chars": 5000,  # 100 chars/page
        },
        compile_metrics={"chunks_count": 1, "extracted_text_chars": 5000},
    )
    result = normalize_compile_result(ar, document_id="doc-1")
    assert result.representation_flags.text_only_but_low_density is True


def test_compile_result_flags_remain_none_without_signal():
    """Empty content_stats → all flags stay None ('no signal')."""
    ar = ArtifactActivityResult(
        status="succeeded", artifact_ids=["a"], kinds=("chunk",),
    )
    result = normalize_compile_result(ar, document_id="doc-1")
    assert result.representation_flags.tables_present_but_unstructured is None
    assert result.representation_flags.images_present_but_undescribed is None
    assert result.representation_flags.text_only_but_low_density is None


def test_compile_result_round_trip_preserves_new_signals():
    ar = ArtifactActivityResult(
        status="succeeded", artifact_ids=["a"], kinds=("chunk",),
        content_stats={
            "table_count": 2, "image_count": 1,
            "page_count": 5, "total_text_chars": 8000,
            "images": [{"image_id": "img-1"}],
        },
        compile_metrics={"chunks_count": 1, "extracted_text_chars": 8000},
    )
    original = normalize_compile_result(
        ar, document_id="doc-1",
        metadata_presence=MetadataPresence(
            required_fields=("project_number",),
            missing_fields=("project_number",),
        ),
    )
    restored = NormalizedCompileResult.from_payload(original.to_payload())
    assert restored == original


def test_compile_result_still_preserves_raw_artifact_refs():
    """The cleanup must NOT regress the raw-output preservation
 contract from."""
    ar = ArtifactActivityResult(
        status="succeeded",
        artifact_ids=["a-1", "a-2", "a-3"],
        kinds=("chunk", "chunk", "parsed_content_manifest"),
    )
    result = normalize_compile_result(ar, document_id="doc-1")
    assert result.raw_artifact_refs == ("a-1", "a-2", "a-3")


# ---- 5. Analyzer consumes InitialExecutionPlan + resolved policy ----


def _good_signals(**overrides) -> SourceSignals:
    base = dict(
        compile_status="succeeded", final_compile_quality="good",
        total_text_chars=5000, text_block_count=20,
    )
    base.update(overrides)
    return SourceSignals(**base)


def test_analyzer_adds_initial_plan_candidates_to_recommended():
    plan = assess_post_compile_enrich(
        _good_signals(),
        initial_plan_candidates=("requirement_extraction", "risk_extraction"),
    )
    assert "requirement_extraction" in plan.recommended_tasks
    assert "risk_extraction" in plan.recommended_tasks


def test_analyzer_records_candidate_provenance_in_reasons():
    """The FE must be able to render 'candidate from initial
 execution plan' — the reason string carries that source."""
    plan = assess_post_compile_enrich(
        _good_signals(),
        initial_plan_candidates=("requirement_extraction",),
    )
    assert any(
        "candidate from initial execution plan" in r
        for r in plan.reasons
    )


def test_analyzer_does_not_duplicate_candidates_already_recommended():
    """Civil pack force-recommends requirement_extraction. When the
 initial plan also carries it, it should appear once."""
    pack = build_civil_engineering_pack()
    plan = assess_post_compile_enrich(
        _good_signals(),
        domain_pack=pack,
        initial_plan_candidates=("requirement_extraction",),
    )
    count = plan.recommended_tasks.count("requirement_extraction")
    assert count == 1, plan.recommended_tasks


def test_analyzer_promotes_optional_to_recommended_with_candidates():
    """No tables/images → OPTIONAL. With initial-plan candidates
 landing on recommended, lift the verdict to RECOMMENDED."""
    plan = assess_post_compile_enrich(
        _good_signals(),
        initial_plan_candidates=("requirement_extraction",),
    )
    assert plan.overall_recommendation == EnrichRecommendation.RECOMMENDED


def test_analyzer_denied_task_overrides_initial_plan_candidate():
    """Pack denying a task wins over an initial-plan candidate."""
    pack = DomainPack(
        id="opted_out_for_requirements",
        display_name="Opted Out",
        version="0.1",
        enrichment_policy=DomainEnrichmentPolicy(
            policy="auto",
            denied_tasks=(TASK_REQUIREMENT_EXTRACTION,),
        ),
    )
    plan = assess_post_compile_enrich(
        _good_signals(),
        domain_pack=pack,
        initial_plan_candidates=(TASK_REQUIREMENT_EXTRACTION,),
    )
    assert TASK_REQUIREMENT_EXTRACTION not in plan.recommended_tasks
    assert TASK_REQUIREMENT_EXTRACTION in plan.skipped_tasks


def test_analyzer_request_override_never_collapses_civil_always():
    """Per-run `never` override beats domain `always`. Final
 verdict is SKIP; the resolved-policy block on the plan records
 the source."""
    pack = build_civil_engineering_pack()
    resolved = resolve_enrichment_policy(
        request_override="never",
        domain_policy=pack.enrichment_policy,
    )
    plan = assess_post_compile_enrich(
        _good_signals(),
        domain_pack=pack,
        resolved_policy=resolved,
    )
    assert plan.overall_recommendation == EnrichRecommendation.SKIP
    resolved_block = plan.domain_enrichment_policy.get("resolved")
    assert resolved_block == {"policy": "never", "source": "request"}


def test_analyzer_resolved_policy_block_surfaces_source():
    """Even without a real override (resolved=domain), the plan
 carries the resolved block so the FE shows the source."""
    pack = build_civil_engineering_pack()
    resolved = resolve_enrichment_policy(domain_policy=pack.enrichment_policy)
    plan = assess_post_compile_enrich(
        _good_signals(),
        domain_pack=pack,
        resolved_policy=resolved,
    )
    assert plan.domain_enrichment_policy["resolved"] == {
        "policy": "always", "source": "domain",
    }


# ---- Architecture invariants preserved ------------------------------


def test_analyzer_does_not_mutate_normalized_compile_result():
    """W5 invariant: the analyzer's input shouldn't be mutated."""
    from j1.processing.enrich_assessment import build_signals_from_normalized_compile_result

    ar = ArtifactActivityResult(
        status="succeeded", artifact_ids=["a"], kinds=("chunk",),
        content_stats={"image_count": 1, "page_count": 5, "total_text_chars": 5000, "images": [{"image_id": "img-1"}]},
        compile_metrics={"chunks_count": 1, "extracted_text_chars": 5000},
    )
    result = normalize_compile_result(ar, document_id="doc-1")
    before_payload = result.to_payload()
    signals = build_signals_from_normalized_compile_result(result)
    _ = assess_post_compile_enrich(signals)
    after_payload = result.to_payload()
    assert before_payload == after_payload


def test_analyzer_does_not_invoke_enrichment_modules():
    """W5 invariant: the analyzer is decision-only. It must not
 import or call any module from `j1.enrichers`."""
    import ast
    import inspect
    from j1.processing import enrich_assessment

    tree = ast.parse(inspect.getsource(enrich_assessment))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for name in imported:
        assert not name.startswith("j1.enrichers"), (
            f"enrich_assessment.py imports {name!r}; the analyzer "
            "must not depend on enrichment modules — it only decides."
        )
