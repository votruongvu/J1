"""Wave 6 tests — typed enrichment overlay + module skeletons +
composite runner.

Pins the contract surface:

  1. `EnrichmentResult` dataclass + sub-records round-trip.
  2. Per-module outcomes carry run/skip reasons + provenance.
  3. `EnrichmentModule` protocol shape — skeletons conform.
  4. Skeleton modules do NOT call any LLM.
  5. Runner aggregates outcomes; SKIPPED + RUN → succeeded,
     PARTIAL → succeeded_with_warnings, FAILED → failed.
  6. Domain-agnostic: no skeleton branches on `domain_id`.
  7. Compile result is never mutated by the runner.
  8. Module execution is configurable (the runner takes a
     caller-supplied module list).

The skeletons use cheap signals + domain hints only — no LLM
imports are allowed.
"""

from __future__ import annotations

import ast
import inspect
import sys

import pytest

from j1.domains.civil_engineering.pack import build_civil_engineering_pack
from j1.domains.general import build_general_pack
from j1.domains.models import (
    DomainEnrichmentPolicy,
    DomainExtractionHints,
    DomainPack,
    DomainValidationRules,
)
from j1.orchestration.activities.payloads import ArtifactActivityResult
from j1.processing.compile_result import (
    MetadataPresence,
    NormalizedCompileResult,
    normalize_compile_result,
)
from j1.processing.enrich_assessment import (
    SourceSignals,
    assess_post_compile_enrich,
    build_signals_from_normalized_compile_result,
)
from j1.processing.enrichment_modules import (
    MODULE_ID_METADATA_ENRICHMENT,
    MODULE_ID_TERMINOLOGY_ENRICHMENT,
    MODULE_ID_VALIDATION,
    CompositeEnrichmentRunner,
    EnrichmentContext,
    MetadataEnrichmentModule,
    TerminologyEnrichmentModule,
    ValidationEnrichmentModule,
)
from j1.processing.enrichment_overlay import (
    ENRICHMENT_RESULT_SCHEMA_VERSION,
    ClassificationResult,
    DocumentMetadataOverlay,
    EnrichmentModule,
    EnrichmentModuleOutcome,
    EnrichmentModuleStatus,
    EnrichmentResult,
    ImageSummary,
    ModelUsageRecord,
    ProvenanceLink,
    TableSummary,
    TerminologyEntry,
    ValidationFinding,
    ValidationResult,
)
from j1.processing.initial_execution_plan import build_initial_execution_plan
from j1.processing.profiling import DocumentProfile


# ---- Helpers --------------------------------------------------------


def _compile_result(
    *,
    artifact_ids: tuple[str, ...] = ("compile-1", "chunk-1"),
    kinds: tuple[str, ...] = ("parsed_content_manifest", "chunk"),
    has_tables: bool = False,
    table_count: int = 0,
    has_images: bool = False,
    image_count: int = 0,
    metadata_presence: MetadataPresence | None = None,
) -> NormalizedCompileResult:
    ar = ArtifactActivityResult(
        status="succeeded",
        artifact_ids=list(artifact_ids),
        kinds=kinds,
        content_stats={
            "has_tables": has_tables,
            "table_count": table_count,
            "has_images": has_images,
            "image_count": image_count,
            "page_count": 10,
            "total_text_chars": 15000,
        },
        compile_metrics={
            "chunks_count": sum(1 for k in kinds if k == "chunk"),
            "extracted_text_chars": 15000,
        },
    )
    return normalize_compile_result(
        ar, document_id="doc-1",
        metadata_presence=metadata_presence or MetadataPresence(),
    )


def _build_ctx(
    *,
    domain_pack=None,
    compile_result: NormalizedCompileResult | None = None,
    initial_plan_candidates: tuple[str, ...] = (),
) -> EnrichmentContext:
    cr = compile_result or _compile_result()
    signals = build_signals_from_normalized_compile_result(cr)
    plan = assess_post_compile_enrich(
        signals,
        domain_pack=domain_pack,
        initial_plan_candidates=initial_plan_candidates,
    )
    profile = DocumentProfile(
        document_id="doc-1",
        extension=".pdf",
        page_count=10,
        total_text_chars=15000,
    )
    initial_plan = build_initial_execution_plan(profile, domain_pack=domain_pack)
    return EnrichmentContext(
        document_id="doc-1",
        compile_result=cr,
        enrich_plan=plan,
        domain_pack=domain_pack,
        initial_plan=initial_plan,
    )


# ---- 1. Round-trip + schema --------------------------------------


def test_schema_version_is_pinned():
    """Wire-schema version bumps are migrations — pin the literal."""
    assert ENRICHMENT_RESULT_SCHEMA_VERSION == "1"


def test_enrichment_result_round_trips_with_every_field():
    original = EnrichmentResult(
        document_id="doc-1",
        status="succeeded_with_warnings",
        module_outcomes=(
            EnrichmentModuleOutcome(
                module_id="metadata_enrichment",
                status=EnrichmentModuleStatus.RUN,
                reason="extracted 4 fields",
                duration_ms=1500,
                output_artifact_refs=("m-1",),
                source_refs=(ProvenanceLink(
                    source_artifact_id="c-1", source_kind="compile",
                    relation="extracted_from",
                ),),
                model_usage=ModelUsageRecord(
                    model="gpt-fast", provider="openai", role="text",
                    input_tokens=400, output_tokens=120, duration_ms=1500,
                ),
                warnings=("low confidence on project_number",),
            ),
            EnrichmentModuleOutcome(
                module_id="table_enrichment",
                status=EnrichmentModuleStatus.PARTIAL,
                reason="3 of 5 tables recovered",
                warnings=("ambiguous headers",),
            ),
        ),
        document_metadata_overlay=DocumentMetadataOverlay(
            fields={"project_number": "ABC-2025-001"},
            missing_required_fields=("document_date",),
        ),
        terminology_map=(
            TerminologyEntry(term="RFI", definition="Request for Information"),
        ),
        classification_result=ClassificationResult(
            category="method_statement", confidence=0.91,
            candidates=(("method_statement", 0.91), ("rfi", 0.06)),
        ),
        table_summaries=(
            TableSummary(
                table_id="t-1", title="BOQ A", row_count=12,
                column_names=("item", "desc", "unit", "qty", "rate"),
            ),
        ),
        image_summaries=(
            ImageSummary(image_id="img-1", caption="site photo, west elevation"),
        ),
        validation_result=ValidationResult(
            passed=False,
            findings=(
                ValidationFinding(
                    rule="required_metadata_field",
                    severity="error",
                    message="project_number missing",
                    field_name="project_number",
                ),
            ),
            checked_rules=("required_metadata_field",),
        ),
        retrieval_hints=("normalize RFI",),
        warnings=("low confidence on project_number",),
        errors=(),
        model_usage=ModelUsageRecord(
            model="gpt-fast", provider="openai",
            input_tokens=400, output_tokens=120, duration_ms=1500,
        ),
        duration_ms=4_500,
        domain_id="civil_engineering",
    )
    restored = EnrichmentResult.from_payload(original.to_payload())
    assert restored == original
    # Wire-shape preservation: ValidationFinding's `field_name`
    # serialises as `field` (existing wire convention).
    payload = original.to_payload()
    finding = payload["validation_result"]["findings"][0]
    assert finding["field"] == "project_number"


# ---- 2. Per-module outcomes carry reasons + provenance ----------


def test_module_outcome_records_run_reason_and_provenance():
    """A RUN outcome MUST carry a non-empty reason + at least one
    typed provenance link for traceability."""
    outcome = EnrichmentModuleOutcome(
        module_id="metadata_enrichment",
        status=EnrichmentModuleStatus.RUN,
        reason="extracted 4 fields",
        source_refs=(ProvenanceLink(
            source_artifact_id="c-1", relation="extracted_from",
        ),),
    )
    assert outcome.reason
    assert outcome.source_refs
    assert outcome.source_refs[0].source_artifact_id == "c-1"


def test_module_outcome_records_skip_reason():
    outcome = EnrichmentModuleOutcome(
        module_id="metadata_enrichment",
        status=EnrichmentModuleStatus.SKIPPED,
        reason="domain pack has no required_metadata_fields",
    )
    assert outcome.status == EnrichmentModuleStatus.SKIPPED
    assert "no required" in outcome.reason


# ---- 3. EnrichmentModule protocol shape -------------------------


@pytest.mark.parametrize(
    "module_class",
    [
        MetadataEnrichmentModule,
        TerminologyEnrichmentModule,
        ValidationEnrichmentModule,
    ],
)
def test_skeleton_modules_conform_to_protocol(module_class):
    """Each skeleton must implement `module_id`, `can_run`, `run`
    — the runner depends on the protocol."""
    instance = module_class()
    assert isinstance(instance, EnrichmentModule)
    assert hasattr(instance, "module_id")
    assert callable(getattr(instance, "can_run", None))
    assert callable(getattr(instance, "run", None))


def test_skeleton_module_ids_are_stable_strings():
    """The module ids match the recommended_tasks vocabulary the
    post-compile analyzer emits — keeps routing on a single name set."""
    assert MetadataEnrichmentModule().module_id == "metadata_enrichment"
    assert TerminologyEnrichmentModule().module_id == "terminology_enrichment"
    assert ValidationEnrichmentModule().module_id == "validation"


# ---- 4. No LLM in skeletons -------------------------------------


def test_enrichment_modules_module_does_not_import_llm_clients():
    """Wave-6 skeletons are decision + bookkeeping only. AST-check
    imports to catch a regression."""
    mod = sys.modules.get("j1.processing.enrichment_modules")
    assert mod is not None
    tree = ast.parse(inspect.getsource(mod))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    forbidden_prefixes = (
        "j1.llm",
        "j1.providers.raganything",
        "j1.providers.mineru",
        "openai",
        "anthropic",
    )
    for name in imported:
        for prefix in forbidden_prefixes:
            assert not name.startswith(prefix), (
                f"enrichment_modules.py unexpectedly imports {name!r}; "
                "the Wave-6 skeletons must stay LLM-free."
            )


def test_enrichment_overlay_module_does_not_import_llm_clients():
    """Same AST check on the overlay dataclasses module."""
    mod = sys.modules.get("j1.processing.enrichment_overlay")
    assert mod is not None
    tree = ast.parse(inspect.getsource(mod))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    forbidden_prefixes = ("j1.llm", "j1.providers", "openai", "anthropic")
    for name in imported:
        for prefix in forbidden_prefixes:
            assert not name.startswith(prefix)


# ---- 5. Runner aggregation --------------------------------------


def test_runner_aggregates_status_succeeded_when_all_run_or_skipped():
    pack = build_civil_engineering_pack()
    ctx = _build_ctx(domain_pack=pack)
    runner = CompositeEnrichmentRunner(modules=[
        MetadataEnrichmentModule(),
        TerminologyEnrichmentModule(),
        ValidationEnrichmentModule(),
    ])
    result = runner.run(ctx)
    # Metadata skeleton SKIPS (LLM not wired); terminology RUNs (hints
    # present); validation RUNs (rules present). No PARTIAL/FAILED.
    assert result.status == "succeeded"


def test_runner_aggregates_status_succeeded_with_warnings_for_partial():
    """A module returning PARTIAL must lift the overall status to
    succeeded_with_warnings."""
    class _PartialModule:
        module_id = "test_partial"
        def can_run(self, ctx):
            return True, "test"
        def run(self, ctx):
            return EnrichmentModuleOutcome(
                module_id=self.module_id,
                status=EnrichmentModuleStatus.PARTIAL,
                reason="partial result",
            )
    ctx = _build_ctx()
    result = CompositeEnrichmentRunner(modules=[_PartialModule()]).run(ctx)
    assert result.status == "succeeded_with_warnings"


def test_runner_marks_failed_when_module_raises():
    """Modules are protocol-bound to not raise, but the runner's
    defensive catch must surface unexpected exceptions as a FAILED
    outcome AND lift overall status to `failed`."""
    class _BrokenModule:
        module_id = "broken"
        def can_run(self, ctx):
            return True, "broken module proceeds"
        def run(self, ctx):
            raise RuntimeError("boom")
    ctx = _build_ctx()
    result = CompositeEnrichmentRunner(modules=[_BrokenModule()]).run(ctx)
    assert result.status == "failed"
    assert result.module_outcomes[0].status == EnrichmentModuleStatus.FAILED
    assert "boom" in result.module_outcomes[0].errors[0]


def test_runner_records_can_run_skip_reason():
    """When `can_run` returns (False, reason), the runner must
    record the reason in the outcome — the FE renders it as the
    skip explanation."""
    pack_without_validation_rules = DomainPack(
        id="bare_pack",
        display_name="Bare",
        version="0.1",
        # No validation_rules, no extraction_hints
    )
    ctx = _build_ctx(domain_pack=pack_without_validation_rules)
    result = CompositeEnrichmentRunner(modules=[
        ValidationEnrichmentModule(),
    ]).run(ctx)
    assert result.module_outcomes[0].status == EnrichmentModuleStatus.SKIPPED
    assert "validation rules" in result.module_outcomes[0].reason


def test_runner_module_execution_is_configurable():
    """Runner takes an explicit module list — operators can swap
    in/out modules without modifying the runner."""
    runner = CompositeEnrichmentRunner(modules=[
        TerminologyEnrichmentModule(),  # Only one module
    ])
    result = runner.run(_build_ctx(domain_pack=build_civil_engineering_pack()))
    assert len(result.module_outcomes) == 1
    assert result.module_outcomes[0].module_id == "terminology_enrichment"


def test_runner_aggregates_warnings_from_outcomes():
    class _WarningModule:
        module_id = "warner"
        def can_run(self, ctx):
            return True, "test"
        def run(self, ctx):
            return EnrichmentModuleOutcome(
                module_id=self.module_id,
                status=EnrichmentModuleStatus.RUN,
                reason="ran with warnings",
                warnings=("watch out for X",),
            )
    result = CompositeEnrichmentRunner(modules=[_WarningModule()]).run(_build_ctx())
    assert "watch out for X" in result.warnings


# ---- 6. Domain-agnostic skeletons (no civil branches) ----------


def test_no_skeleton_branches_on_specific_domain_id():
    """The skeleton module code must not contain any `domain ==
    "civil"` style branches. Behaviour comes from typed hints."""
    mod = sys.modules.get("j1.processing.enrichment_modules")
    src = inspect.getsource(mod)
    # No hardcoded id literal in conditionals / code paths.
    # Allow mentions in docstrings (comments only document the sample).
    tree = ast.parse(src)
    docstring_node_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef)
        ):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(
                body[0].value, ast.Constant,
            ) and isinstance(body[0].value.value, str):
                docstring_node_ids.add(id(body[0].value))
    forbidden_terms = ("civil_engineering", "civil")
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstring_node_ids:
                continue
            for term in forbidden_terms:
                assert term not in node.value.lower(), (
                    f"enrichment_modules.py code-literal leaks "
                    f"{term!r}: {node.value!r}"
                )


def test_skeleton_modules_run_with_generic_pack():
    """A generic pack with no extraction hints / validation rules
    must produce well-formed (SKIPPED) outcomes — never errors."""
    ctx = _build_ctx(domain_pack=build_general_pack())
    result = CompositeEnrichmentRunner(modules=[
        TerminologyEnrichmentModule(),
        ValidationEnrichmentModule(),
    ]).run(ctx)
    assert result.status == "succeeded"
    for outcome in result.module_outcomes:
        assert outcome.status == EnrichmentModuleStatus.SKIPPED
        assert outcome.reason


# ---- 7. Runner doesn't mutate inputs ----------------------------


def test_runner_does_not_mutate_compile_result():
    pack = build_civil_engineering_pack()
    ctx = _build_ctx(domain_pack=pack)
    before = ctx.compile_result.to_payload()
    _ = CompositeEnrichmentRunner(modules=[
        MetadataEnrichmentModule(),
        TerminologyEnrichmentModule(),
        ValidationEnrichmentModule(),
    ]).run(ctx)
    after = ctx.compile_result.to_payload()
    assert before == after


def test_runner_does_not_mutate_domain_pack():
    pack = build_civil_engineering_pack()
    before_hints = pack.extraction_hints.to_dict()
    before_rules = pack.validation_rules.to_dict()
    ctx = _build_ctx(domain_pack=pack)
    _ = CompositeEnrichmentRunner(modules=[
        TerminologyEnrichmentModule(),
        ValidationEnrichmentModule(),
    ]).run(ctx)
    assert pack.extraction_hints.to_dict() == before_hints
    assert pack.validation_rules.to_dict() == before_rules


# ---- 8. Skeleton-specific behaviour -----------------------------


def test_metadata_skeleton_runs_when_plan_recommends_or_metadata_missing():
    pack = build_civil_engineering_pack()
    missing_pres = MetadataPresence(
        required_fields=("project_number",),
        missing_fields=("project_number",),
    )
    ctx = _build_ctx(
        domain_pack=pack,
        compile_result=_compile_result(metadata_presence=missing_pres),
    )
    module = MetadataEnrichmentModule()
    ok, _reason = module.can_run(ctx)
    assert ok
    outcome = module.run(ctx)
    # Skeleton SKIPS but with an LLM-deferred reason — the FE
    # renders the planned-target count.
    assert outcome.status == EnrichmentModuleStatus.SKIPPED
    assert "LLM" in outcome.reason or "extractor" in outcome.reason
    # Provenance links back to the compile artifact.
    assert outcome.source_refs
    assert outcome.source_refs[0].source_artifact_id == "compile-1"


def test_terminology_skeleton_projects_hints_into_typed_entries():
    pack = build_civil_engineering_pack()
    ctx = _build_ctx(domain_pack=pack)
    result = CompositeEnrichmentRunner(modules=[
        TerminologyEnrichmentModule(),
    ]).run(ctx)
    # Civil pack has 6 RFI/NCR/etc. entries
    assert len(result.terminology_map) >= 5
    rfi_entries = [t for t in result.terminology_map if t.term == "RFI"]
    assert rfi_entries
    assert rfi_entries[0].definition == "Request for Information"
    # Provenance points at the domain pack (not a compile artifact).
    assert rfi_entries[0].provenance.source_kind == "domain_pack"


def test_validation_skeleton_emits_findings_for_missing_required_metadata():
    pack = build_civil_engineering_pack()
    presence = MetadataPresence(
        required_fields=("project_number", "document_date"),
        present_fields=("document_date",),
        missing_fields=("project_number",),
    )
    ctx = _build_ctx(
        domain_pack=pack,
        compile_result=_compile_result(metadata_presence=presence),
    )
    result = CompositeEnrichmentRunner(modules=[
        ValidationEnrichmentModule(),
    ]).run(ctx)
    assert result.validation_result is not None
    findings = result.validation_result.findings
    required_findings = [
        f for f in findings if f.rule == "required_metadata_field"
    ]
    assert len(required_findings) == 1
    assert required_findings[0].field_name == "project_number"
    assert required_findings[0].severity == "warning"


def test_validation_skeleton_returns_passed_when_only_info_findings():
    """Findings at `info` level (e.g. expected_document_structure)
    don't block — `passed=True` when no severity-error findings."""
    pack = build_civil_engineering_pack()
    # No missing metadata → only structure findings (info severity)
    ctx = _build_ctx(
        domain_pack=pack,
        compile_result=_compile_result(metadata_presence=MetadataPresence()),
    )
    result = CompositeEnrichmentRunner(modules=[
        ValidationEnrichmentModule(),
    ]).run(ctx)
    assert result.validation_result is not None
    assert result.validation_result.passed is True
    assert all(
        f.severity in ("info", "warning")
        for f in result.validation_result.findings
    )
