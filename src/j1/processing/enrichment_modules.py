"""Enrichment module skeletons + composite runner.

The protocol + sub-records live in `enrichment_overlay.py`. This
module wires:

 * `EnrichmentContext` — typed input bundle the runner passes to
 every module (CompileResult, domain pack, resolved policy, etc.)
 * Skeleton implementations of the modules the existing
 `j1.enrichers.CompositeEnricher` doesn't cover:
 - `MetadataEnrichmentModule`
 - `TerminologyEnrichmentModule`
 - `ValidationEnrichmentModule`
 Each skeleton makes a decision (run / skip) based on cheap
 signals and produces a typed outcome. **No LLM calls yet** —
 deferred to per-module wiring that comes after the
 overall runner ships.

 * `CompositeEnrichmentRunner` — orchestrates a list of modules,
 aggregates outcomes, builds the typed `EnrichmentResult`.

Modules consume the domain layer through typed interfaces only:
 * `DomainExtractionHints` for metadata fields / terminology /
 table-image hints (sets WHICH things to look for).
 * `DomainValidationRules` for required-metadata / quality
 conditions (drives the validation module).
 * `DomainPromptPack` for per-module prompt overrides (the
 skeleton modules don't call LLMs yet, but the protocol is
 plumbed so future LLM-client wiring plugs in without
 touching the orchestrator).

Domain-agnostic by construction: no module branches on
`domain_id`; behaviour is driven entirely by the typed hints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from j1.domains.models import DomainPack
from j1.processing.compile_result import NormalizedCompileResult
from j1.processing.enrich_assessment import PostCompileEnrichPlan
from j1.processing.enrichment_overlay import (
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
from j1.processing.enrichment_policy import ResolvedEnrichmentPolicy
from j1.processing.initial_execution_plan import InitialExecutionPlan


__all__ = [
    "MODULE_ID_METADATA_ENRICHMENT",
    "MODULE_ID_TERMINOLOGY_ENRICHMENT",
    "MODULE_ID_VALIDATION",
    "CompositeEnrichmentRunner",
    "EnrichmentContext",
    "MetadataEnrichmentModule",
    "TerminologyEnrichmentModule",
    "ValidationEnrichmentModule",
    "build_skipped_enrichment_result",
    "resolve_module_prompt",
]


# Stable module identifiers. Match `recommended_tasks` ids in
# `PostCompileEnrichPlan` where applicable so a recommended task
# routes cleanly onto the matching module.
MODULE_ID_METADATA_ENRICHMENT = "metadata_enrichment"
MODULE_ID_TERMINOLOGY_ENRICHMENT = "terminology_enrichment"
MODULE_ID_VALIDATION = "validation"


# ---- Module input bundle --------------------------------------------


@dataclass(frozen=True)
class EnrichmentContext:
    """Everything a module needs at decision/run time, bundled.

 Pure data — passed by value through the runner. Modules read
 typed fields; no module mutates the context. Optional fields
 (e.g. `domain_pack`) carry None for runs without a pack.
 """

    document_id: str
    compile_result: NormalizedCompileResult
    enrich_plan: PostCompileEnrichPlan
    domain_pack: DomainPack | None = None
    resolved_policy: ResolvedEnrichmentPolicy | None = None
    initial_plan: InitialExecutionPlan | None = None
    # Caller-supplied resource hints. The runner doesn't enforce
    # these — modules read them as guidance. will turn the
    # bottom-rung concurrency knobs into a real semaphore.
    resource_hints: dict[str, Any] = field(default_factory=dict)


# ---- Skeleton modules -----------------------------------------------


@dataclass(frozen=True)
class MetadataEnrichmentModule:
    """Decision-only skeleton for the metadata enricher.

 Runs when:
 * the post-compile plan has `metadata_enrichment` in
 `recommended_tasks`, OR
 * the domain pack declares `required_metadata_fields` that
 the compile result reports missing.

 Produces a typed `DocumentMetadataOverlay` carrying the list
 of fields the LLM-backed metadata extractor SHOULD target.
 Until LLM wiring lands, the module records its
 decision + a SKIPPED outcome with `reason="awaiting LLM
 extractor wiring"`.

 The overlay+outcome are surfaced via the runner so:
 * the FE / report can show "metadata enrichment recommended:
 N fields", and
 * the future LLM-wired version slots in by changing the
 outcome status from SKIPPED → RUN with the actual extracted
 fields.
 """

    module_id: str = MODULE_ID_METADATA_ENRICHMENT
    target_metadata_fields: tuple[str, ...] = ()

    def can_run(self, ctx: EnrichmentContext) -> tuple[bool, str]:
        plan_recommends = (
            self.module_id in ctx.enrich_plan.recommended_tasks
        )
        missing = ctx.compile_result.metadata_presence.missing_fields
        if plan_recommends or missing:
            return True, "plan recommends or compile reports missing fields"
        return False, "plan did not recommend and no missing required fields"

    def run(self, ctx: EnrichmentContext) -> EnrichmentModuleOutcome:
        # The skeleton doesn't call an LLM. It records the intent +
        # populates the overlay with the targeted fields list so the
        # FE can render "would extract: project_number, …".
        targets = _resolve_metadata_targets(ctx, self.target_metadata_fields)
        missing = ctx.compile_result.metadata_presence.missing_fields
        provenance = ProvenanceLink(
            source_artifact_id=(
                ctx.compile_result.raw_artifact_refs[0]
                if ctx.compile_result.raw_artifact_refs else None
            ),
            source_kind="compile",
            relation="extracted_from",
        )
        return EnrichmentModuleOutcome(
            module_id=self.module_id,
            status=EnrichmentModuleStatus.SKIPPED,
            reason=(
                f"metadata enrichment planned: target {len(targets)} "
                f"field(s) (missing={len(missing)}); LLM extractor not "
                "yet wired"
            ),
            duration_ms=0,
            source_refs=(provenance,),
            model_usage=ModelUsageRecord(),
        )


@dataclass(frozen=True)
class TerminologyEnrichmentModule:
    """Decision-only skeleton for the terminology enricher.

 Runs when the domain pack supplies non-empty
 `extraction_hints.terminology_hints`. Output: typed list of
 `TerminologyEntry` records the indexer/retrieval layer can
 consume as synonyms/normalisation rules.

 Skeleton behaviour: emits one `TerminologyEntry` per hint
 where the hint has an `=` separator (e.g. `"RFI = Request for
 Information"` → term=RFI, definition="Request for Information").
 Hints that don't follow the convention surface as warnings.
 No LLM call — pure projection over the domain hints.
 """

    module_id: str = MODULE_ID_TERMINOLOGY_ENRICHMENT

    def can_run(self, ctx: EnrichmentContext) -> tuple[bool, str]:
        if ctx.domain_pack is None:
            return False, "no domain pack selected"
        hints = ctx.domain_pack.extraction_hints.terminology_hints
        if not hints:
            return False, "domain pack supplies no terminology hints"
        return True, f"{len(hints)} domain terminology hint(s) available"


    def run(self, ctx: EnrichmentContext) -> EnrichmentModuleOutcome:
        started = perf_counter()
        entries: list[TerminologyEntry] = []
        warnings: list[str] = []
        provenance = ProvenanceLink(
            source_kind="domain_pack",
            relation="derived_from",
        )
        assert ctx.domain_pack is not None  # can_run gates this
        for hint in ctx.domain_pack.extraction_hints.terminology_hints:
            term, _, definition = hint.partition("=")
            term = term.strip()
            definition = definition.strip()
            if not term:
                warnings.append(f"unparseable terminology hint: {hint!r}")
                continue
            if not definition:
                warnings.append(
                    f"terminology hint {term!r} has no definition; "
                    "stored without one"
                )
                entries.append(TerminologyEntry(
                    term=term, provenance=provenance,
                ))
                continue
            entries.append(TerminologyEntry(
                term=term, definition=definition, provenance=provenance,
            ))
        duration_ms = int((perf_counter() - started) * 1000)
        if not entries:
            return EnrichmentModuleOutcome(
                module_id=self.module_id,
                status=EnrichmentModuleStatus.SKIPPED,
                reason="no parseable terminology hints",
                duration_ms=duration_ms,
                warnings=tuple(warnings),
            )
        # The runner harvests these entries onto the overlay via
        # the `produced_terminology` field on the outcome (set by
        # the runner inspecting the module's typed output dict).
        return EnrichmentModuleOutcome(
            module_id=self.module_id,
            status=EnrichmentModuleStatus.RUN,
            reason=(
                f"projected {len(entries)} terminology entr(ies) "
                "from domain hints"
            ),
            duration_ms=duration_ms,
            warnings=tuple(warnings),
        )


@dataclass(frozen=True)
class ValidationEnrichmentModule:
    """Decision-only skeleton for the validation enricher.

 Runs whenever the domain pack supplies `validation_rules` with
 any non-empty list. Produces a typed `ValidationResult`
 carrying findings for:

 * each `required_metadata_field` declared by the pack that
 the compile result reports missing — surfaces a "warning"
 finding.
 * each entry in `expected_document_structure` — surfaces an
 "info" finding noting what the operator should check
 (the skeleton can't auto-assert structure; the LLM-wired
 version will).

 No LLM call — pure projection over compile metadata + domain
 validation rules.
 """

    module_id: str = MODULE_ID_VALIDATION

    def can_run(self, ctx: EnrichmentContext) -> tuple[bool, str]:
        if ctx.domain_pack is None:
            return False, "no domain pack selected"
        rules = ctx.domain_pack.validation_rules
        if (
            not rules.required_metadata_fields
            and not rules.expected_document_structure
            and not rules.low_quality_warning_conditions
            and not rules.enrichment_triggers
        ):
            return False, "domain pack supplies no validation rules"
        return True, "domain validation rules present"

    def run(self, ctx: EnrichmentContext) -> EnrichmentModuleOutcome:
        started = perf_counter()
        assert ctx.domain_pack is not None
        rules = ctx.domain_pack.validation_rules

        findings: list[ValidationFinding] = []
        provenance = ProvenanceLink(
            source_kind="domain_pack",
            relation="validates_against",
        )

        # Required-metadata findings (sourced from the metadata-
        # presence check the compile result already produced).
        for field_name in ctx.compile_result.metadata_presence.missing_fields:
            findings.append(ValidationFinding(
                rule="required_metadata_field",
                severity="warning",
                message=(
                    f"required metadata field {field_name!r} is missing "
                    "from the compile output"
                ),
                field_name=field_name,
                provenance=provenance,
            ))

        # Structure findings (info-level — skeleton can't auto-check).
        for expectation in rules.expected_document_structure:
            findings.append(ValidationFinding(
                rule="expected_document_structure",
                severity="info",
                message=expectation,
                provenance=provenance,
            ))

        duration_ms = int((perf_counter() - started) * 1000)
        passed = not any(
            f.severity == "error" for f in findings
        )
        # Reason summarises the finding mix; the runner attaches the
        # ValidationResult to the overlay separately.
        warning_count = sum(1 for f in findings if f.severity == "warning")
        info_count = sum(1 for f in findings if f.severity == "info")
        reason = (
            f"validation produced {warning_count} warning(s) + "
            f"{info_count} info finding(s); passed={passed}"
        )
        # If there's nothing to surface, skip cleanly.
        if not findings:
            return EnrichmentModuleOutcome(
                module_id=self.module_id,
                status=EnrichmentModuleStatus.SKIPPED,
                reason="no validation findings",
                duration_ms=duration_ms,
            )
        return EnrichmentModuleOutcome(
            module_id=self.module_id,
            status=EnrichmentModuleStatus.RUN,
            reason=reason,
            duration_ms=duration_ms,
        )


# ---- Composite runner -----------------------------------------------


@dataclass
class CompositeEnrichmentRunner:
    """Orchestrates a sequence of `EnrichmentModule` instances.

 Pure orchestration — calls each module's `can_run` then `run`,
 catching any unexpected exception as a FAILED outcome (modules
 are protocol-bound to NOT raise; the catch is defensive). The
 runner aggregates outcomes onto the typed `EnrichmentResult`,
 computes the overall status, and stitches in the typed payloads
 individual modules emit through helpers exposed on this class.

 Today the skeletons populate the outcome record only — the
 typed payload-side fields (terminology_map, validation_result,
 etc.) are wired in by the runner via dedicated helpers so the
 future LLM-wired modules can return both an outcome AND the
 typed output in one shot."""

    modules: list[EnrichmentModule] = field(default_factory=list)

    def run(self, ctx: EnrichmentContext) -> EnrichmentResult:
        started = perf_counter()
        outcomes: list[EnrichmentModuleOutcome] = []
        terminology: list[TerminologyEntry] = []
        validation: ValidationResult | None = None
        metadata_overlay = DocumentMetadataOverlay()
        # typed payload accumulators for wrapper modules
        # that emit typed records through `get_typed_outputs`.
        classification: ClassificationResult | None = None
        table_summaries: list[TableSummary] = []
        image_summaries: list[ImageSummary] = []
        retrieval_hints: list[str] = []
        confidence_notes: list[str] = []
        warnings: list[str] = []
        errors: list[str] = []
        for module in self.modules:
            ok, reason = module.can_run(ctx)
            if not ok:
                outcomes.append(EnrichmentModuleOutcome(
                    module_id=module.module_id,
                    status=EnrichmentModuleStatus.SKIPPED,
                    reason=reason,
                    duration_ms=0,
                ))
                continue
            try:
                outcome = module.run(ctx)
            except Exception as exc:  # noqa: BLE001 — defensive catch
                outcomes.append(EnrichmentModuleOutcome(
                    module_id=module.module_id,
                    status=EnrichmentModuleStatus.FAILED,
                    reason=f"module raised: {type(exc).__name__}",
                    errors=(str(exc),),
                ))
                errors.append(f"{module.module_id}: {exc}")
                continue
            outcomes.append(outcome)
            warnings.extend(outcome.warnings)
            errors.extend(outcome.errors)
            # Project module-specific typed payloads onto the
            # aggregated overlay. skeletons emit just the
            # outcome; the runner reconstructs the typed payloads
            # from the module's input context (deterministic
            # projection — same inputs → same payload).
            if isinstance(module, TerminologyEnrichmentModule) and outcome.status == EnrichmentModuleStatus.RUN:
                terminology.extend(_projected_terminology_entries(ctx))
            if isinstance(module, ValidationEnrichmentModule) and outcome.status == EnrichmentModuleStatus.RUN:
                validation = _projected_validation_result(ctx)
            if isinstance(module, MetadataEnrichmentModule):
                metadata_overlay = _projected_metadata_overlay(ctx)
            # wrapper modules expose typed payloads via
            # `get_typed_outputs` (cached on the module instance
            # during `run`). Merge whatever keys we recognise.
            # Unknown keys are tolerated so future wrappers can
            # introduce new typed outputs without changes here.
            if outcome.status in (
                EnrichmentModuleStatus.RUN,
                EnrichmentModuleStatus.PARTIAL,
            ) and hasattr(module, "get_typed_outputs"):
                typed = module.get_typed_outputs() or {}
                cls_record = typed.get("classification_result")
                if isinstance(cls_record, ClassificationResult):
                    classification = cls_record
                tables = typed.get("table_summaries") or ()
                for t in tables:
                    if isinstance(t, TableSummary):
                        table_summaries.append(t)
                images = typed.get("image_summaries") or ()
                for i in images:
                    if isinstance(i, ImageSummary):
                        image_summaries.append(i)
                hints = typed.get("retrieval_hints") or ()
                for h in hints:
                    if isinstance(h, str) and h:
                        retrieval_hints.append(h)
                notes = typed.get("confidence_notes") or ()
                for n in notes:
                    if isinstance(n, str) and n:
                        confidence_notes.append(n)
        duration_ms = int((perf_counter() - started) * 1000)
        status = _aggregate_status(outcomes)
        return EnrichmentResult(
            document_id=ctx.document_id,
            status=status,
            module_outcomes=tuple(outcomes),
            document_metadata_overlay=metadata_overlay,
            terminology_map=tuple(terminology),
            classification_result=classification,
            table_summaries=tuple(table_summaries),
            image_summaries=tuple(image_summaries),
            validation_result=validation,
            retrieval_hints=tuple(retrieval_hints),
            confidence_notes=tuple(confidence_notes),
            warnings=tuple(warnings),
            errors=tuple(errors),
            duration_ms=duration_ms,
            domain_id=(
                ctx.domain_pack.id if ctx.domain_pack is not None else None
            ),
        )


# ---- Projection helpers (pure, deterministic) ----------------------


def _resolve_metadata_targets(
    ctx: EnrichmentContext,
    explicit_targets: tuple[str, ...],
) -> tuple[str, ...]:
    """Pick the metadata-field list the module targets.

 Precedence:
 1. Caller-supplied `explicit_targets` (constructor kwarg).
 2. Domain pack's `extraction_hints.metadata_fields`.
 3. Compile result's `metadata_presence.required_fields`.
 """
    if explicit_targets:
        return explicit_targets
    if ctx.domain_pack is not None:
        hints = ctx.domain_pack.extraction_hints.metadata_fields
        if hints:
            return hints
    return ctx.compile_result.metadata_presence.required_fields


def _projected_terminology_entries(
    ctx: EnrichmentContext,
) -> list[TerminologyEntry]:
    """Project the domain terminology hints into typed records.

 Mirrors the body of `TerminologyEnrichmentModule.run` — keeps
 the projection deterministic across the module + runner so a
 test fixture asserting the overlay terminology doesn't have to
 sequence through the module's exception path."""
    if ctx.domain_pack is None:
        return []
    provenance = ProvenanceLink(
        source_kind="domain_pack",
        relation="derived_from",
    )
    out: list[TerminologyEntry] = []
    for hint in ctx.domain_pack.extraction_hints.terminology_hints:
        term, _, definition = hint.partition("=")
        term = term.strip()
        definition = definition.strip()
        if not term:
            continue
        if not definition:
            out.append(TerminologyEntry(
                term=term, provenance=provenance,
            ))
            continue
        out.append(TerminologyEntry(
            term=term, definition=definition, provenance=provenance,
        ))
    return out


def _projected_validation_result(
    ctx: EnrichmentContext,
) -> ValidationResult:
    """Project compile metadata-presence + domain rules into a
 typed ValidationResult. Pure / deterministic."""
    assert ctx.domain_pack is not None
    rules = ctx.domain_pack.validation_rules
    findings: list[ValidationFinding] = []
    provenance = ProvenanceLink(
        source_kind="domain_pack",
        relation="validates_against",
    )
    for field_name in ctx.compile_result.metadata_presence.missing_fields:
        findings.append(ValidationFinding(
            rule="required_metadata_field",
            severity="warning",
            message=(
                f"required metadata field {field_name!r} is missing "
                "from the compile output"
            ),
            field_name=field_name,
            provenance=provenance,
        ))
    for expectation in rules.expected_document_structure:
        findings.append(ValidationFinding(
            rule="expected_document_structure",
            severity="info",
            message=expectation,
            provenance=provenance,
        ))
    passed = not any(f.severity == "error" for f in findings)
    checked = (
        "required_metadata_field",
        "expected_document_structure",
    )
    return ValidationResult(
        passed=passed,
        findings=tuple(findings),
        checked_rules=checked,
    )


def _projected_metadata_overlay(
    ctx: EnrichmentContext,
) -> DocumentMetadataOverlay:
    """Project the compile metadata-presence + domain hints into
 a typed metadata overlay. The skeleton populates only the
 `missing_required_fields` list + provenance; field VALUES are
 deferred to the LLM-wired version."""
    provenance: tuple[ProvenanceLink, ...] = ()
    if ctx.compile_result.raw_artifact_refs:
        provenance = (
            ProvenanceLink(
                source_artifact_id=ctx.compile_result.raw_artifact_refs[0],
                source_kind="compile",
                relation="extracted_from",
            ),
        )
    return DocumentMetadataOverlay(
        missing_required_fields=ctx.compile_result.metadata_presence.missing_fields,
        provenance=provenance,
    )


def _aggregate_status(
    outcomes: list[EnrichmentModuleOutcome],
) -> str:
    """Compute the top-level `EnrichmentResult.status` from the
 per-module outcomes.

 Returns:
 * `succeeded` — every outcome is RUN or SKIPPED.
 * `succeeded_with_warnings` — at least one PARTIAL.
 * `failed` — at least one FAILED.
 """
    if any(o.status == EnrichmentModuleStatus.FAILED for o in outcomes):
        return "failed"
    if any(o.status == EnrichmentModuleStatus.PARTIAL for o in outcomes):
        return "succeeded_with_warnings"
    return "succeeded"


# ---- integration helpers -----------------------------------


def build_skipped_enrichment_result(
    *,
    document_id: str,
    reason: str,
    domain_id: str | None = None,
) -> EnrichmentResult:
    """Build a sentinel `EnrichmentResult` for the no-enrichment path.

 Used by the workflow when `PostCompileEnrichPlan.should_enrich`
 is False: the workflow still persists a typed overlay so
 downstream consumers (final report, FE panel) see an explicit
 "enrichment skipped: <reason>" record rather than the absence
 of an artifact (which is ambiguous — could be policy, could be
 a write failure).

 Pure / no I/O. `reason` should come from
 `PostCompileEnrichPlan.reasons` / `blocking_issues`."""
    return EnrichmentResult(
        document_id=document_id,
        status="skipped",
        skipped_reason=reason,
        domain_id=domain_id,
        duration_ms=0,
    )


def resolve_module_prompt(
    *,
    domain_pack: DomainPack | None,
    prompt_field: str,
    builtin_default: str,
) -> str:
    """Resolve the prompt text a module should use.

 Precedence:
 1. `domain_pack.prompt_pack.<prompt_field>` if set
 (non-None, non-empty).
 2. `builtin_default` otherwise.

 The active `domain_pack.prompt_addon` is prepended to whichever
 base prompt wins so the model has domain context BEFORE the
 task-specific instructions. When no pack is selected, only the
 `builtin_default` is returned (no addon).

 Pure / no I/O. `prompt_field` must be one of the attribute
 names on `DomainPromptPack` — passing an unknown attribute
 falls through to the default to keep the helper safe at module
 construction time."""
    if domain_pack is None:
        return builtin_default
    pack_prompt: str | None = getattr(
        domain_pack.prompt_pack, prompt_field, None,
    )
    base = pack_prompt if pack_prompt else builtin_default
    addon = (domain_pack.prompt_addon or "").strip()
    if addon:
        return f"{addon}\n\n{base}"
    return base
