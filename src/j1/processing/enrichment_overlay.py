"""Enrichment overlay.

Typed container for the post-compile enrichment stage's output.
The overlay is **derived** — every field carries provenance back
to the raw compile artifacts/chunks it was built from, and the
raw compile output stays intact in the workspace.

Design contracts:

 1. **Non-destructive.** Enrichment writes new typed records +
 new artifact kinds (`enriched.tables`, `enriched.visuals`, …);
 it never mutates the existing compile artifacts.

 2. **Provenance is explicit.** Every per-module outcome and every
 overlay field that points at content carries
 `source_artifact_refs` referencing the compile artifacts the
 enrichment was derived from. The FE renders the chain
 "this enrichment came from chunk X, which came from compile
 output Y".

 3. **Partial execution is supported.** Each module can succeed,
 skip with a reason, fail, or run partially. The aggregated
 `EnrichmentResult.status` reflects the worst non-success
 outcome; per-module outcomes are preserved on the
 `module_outcomes` list.

 4. **Domain-driven.** Modules consume `DomainExtractionHints`,
 `DomainPromptPack`, and `DomainValidationRules` through the
 typed contracts on `DomainPack`. No domain-specific logic
 lives in module code; the modules read the typed fields and
 pass them to whatever extraction/LLM machinery they wrap.

 5. **The skeleton modules
 defined here are decision + bookkeeping only. The existing
 LLM-backed enrichers in `j1.enrichers` keep operating as
 today; future code will wrap them via the `EnrichmentModule`
 protocol so their outputs land on the typed overlay.

The module is PURE — no I/O. Persistence happens at the
`ProcessingService` layer via `persist_enrichment_result`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


__all__ = [
    "ENRICHMENT_RESULT_SCHEMA_VERSION",
    "ClassificationResult",
    "DocumentMetadataOverlay",
    "EnrichmentModule",
    "EnrichmentModuleOutcome",
    "EnrichmentModuleStatus",
    "EnrichmentResult",
    "ModelUsageRecord",
    "ProvenanceLink",
    "TableSummary",
    "ImageSummary",
    "TerminologyEntry",
    "ValidationFinding",
    "ValidationResult",
]


ENRICHMENT_RESULT_SCHEMA_VERSION = "1"


# Stable wire vocabulary — used by the FE module-status pill and
# by audit-log consumers. Keep stable; renames are migrations.
class EnrichmentModuleStatus(StrEnum):
    """Per-module outcome state.

 * `RUN` — module executed and produced its expected outputs.
 * `PARTIAL` — module ran but produced only some outputs
 (e.g. table enricher recovered 2 of 5 tables). Surfaced
 distinctly from `RUN` so reviewers see "we got some, not
 all" instead of green-success copy.
 * `SKIPPED` — module decided not to run; reason recorded.
 * `FAILED` — module raised / vendor returned an error. The
 raw compile output is untouched.
 """

    RUN = "run"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    FAILED = "failed"


# ---- Provenance + small typed sub-records ---------------------------


@dataclass(frozen=True)
class ProvenanceLink:
    """A typed back-reference from an enriched output to the raw
 compile artifact/chunk it was derived from.

 Pure data; the FE/audit layer resolves `source_artifact_id` /
 `source_chunk_id` against the artifact registry when rendering.
 `relation` is a short label ("extracted_from", "summarises",
 "validates") so the chain is human-readable on the timeline.
 """

    source_artifact_id: str | None = None
    source_chunk_id: str | None = None
    source_kind: str | None = None
    relation: str = "derived_from"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_artifact_id": self.source_artifact_id,
            "source_chunk_id": self.source_chunk_id,
            "source_kind": self.source_kind,
            "relation": self.relation,
        }


@dataclass(frozen=True)
class ModelUsageRecord:
    """One LLM/model call's footprint. Aggregated per-module +
 on the top-level result for cost/runtime visibility.

 Pure data; no LLM coupling. Fields stay optional so a module
 that doesn't call a model leaves the record empty."""

    model: str | None = None
    provider: str | None = None
    role: str | None = None  # text / vision / fast
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int | None = None
    cost_estimate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "role": self.role,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "duration_ms": self.duration_ms,
            "cost_estimate": self.cost_estimate,
        }


@dataclass(frozen=True)
class TableSummary:
    """One enriched table — typed summary the FE renders without
 re-fetching the raw artifact."""

    table_id: str
    title: str | None = None
    summary: str | None = None
    column_names: tuple[str, ...] = ()
    row_count: int | None = None
    provenance: ProvenanceLink = field(default_factory=ProvenanceLink)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "table_id": self.table_id,
            "title": self.title,
            "summary": self.summary,
            "column_names": list(self.column_names),
            "row_count": self.row_count,
            "provenance": self.provenance.to_dict(),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ImageSummary:
    """One enriched image — caption + role + provenance."""

    image_id: str
    caption: str | None = None
    role: str | None = None
    confidence: float | None = None
    provenance: ProvenanceLink = field(default_factory=ProvenanceLink)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_id": self.image_id,
            "caption": self.caption,
            "role": self.role,
            "confidence": self.confidence,
            "provenance": self.provenance.to_dict(),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class TerminologyEntry:
    """One terminology mapping the terminology enricher emitted.

 Acts as both glossary entry + retrieval-normalisation hint."""

    term: str
    normalized: str | None = None
    synonyms: tuple[str, ...] = ()
    definition: str | None = None
    provenance: ProvenanceLink = field(default_factory=ProvenanceLink)

    def to_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "normalized": self.normalized,
            "synonyms": list(self.synonyms),
            "definition": self.definition,
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True)
class ClassificationResult:
    """Document-level classification output.

 `category` is the top-level type (e.g. "method_statement"),
 `subcategory` is an optional finer label, `confidence` is the
 classifier's 0..1 score."""

    category: str | None = None
    subcategory: str | None = None
    confidence: float | None = None
    candidates: tuple[tuple[str, float], ...] = ()
    reasoning: str | None = None
    provenance: ProvenanceLink = field(default_factory=ProvenanceLink)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "subcategory": self.subcategory,
            "confidence": self.confidence,
            "candidates": [
                {"category": c, "confidence": s} for c, s in self.candidates
            ],
            "reasoning": self.reasoning,
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True)
class DocumentMetadataOverlay:
    """Document-level metadata the enrichment stage observed.

 Distinct from the raw parsed metadata: this overlay carries
 the enricher's EXTRACTION of metadata fields the domain asked
 for (e.g. `project_number`, `drawing_revision`). Empty when
 the metadata enricher didn't run."""

    fields: dict[str, str] = field(default_factory=dict)
    missing_required_fields: tuple[str, ...] = ()
    extras: dict[str, str] = field(default_factory=dict)
    provenance: tuple[ProvenanceLink, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "fields": dict(self.fields),
            "missing_required_fields": list(self.missing_required_fields),
            "extras": dict(self.extras),
            "provenance": [p.to_dict() for p in self.provenance],
        }


@dataclass(frozen=True)
class ValidationFinding:
    """One observation from the validation enricher.

 `field_name` is the metadata key the finding refers to (when
 relevant) — not named `field` to avoid shadowing the
 `dataclasses.field` import at class-definition time."""

    rule: str
    severity: str = "warning"  # info / warning / error
    message: str = ""
    field_name: str | None = None
    provenance: ProvenanceLink = field(default_factory=ProvenanceLink)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "field": self.field_name,  # wire shape stays "field"
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True)
class ValidationResult:
    """Aggregate output of the validation enricher.

 `passed=True` means no severity-error findings; warnings can
 still be present. The FE renders findings + a banner driven
 by `passed`."""

    passed: bool = True
    findings: tuple[ValidationFinding, ...] = ()
    checked_rules: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "findings": [f.to_dict() for f in self.findings],
            "checked_rules": list(self.checked_rules),
        }


# ---- Per-module outcome ---------------------------------------------


@dataclass(frozen=True)
class EnrichmentModuleOutcome:
    """One module's structured outcome record.

 The FE renders the module-status pill + the run/skip reason
 from this record. `output_artifact_refs` captures the new
 artifacts the module wrote (provenance flows back via the
 artifact registry); `source_refs` carries explicit per-output
 provenance for cases where multiple compile artifacts feed
 one module."""

    module_id: str
    status: EnrichmentModuleStatus
    reason: str = ""
    duration_ms: int | None = None
    output_artifact_refs: tuple[str, ...] = ()
    source_refs: tuple[ProvenanceLink, ...] = ()
    model_usage: ModelUsageRecord = field(default_factory=ModelUsageRecord)
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "module_id": self.module_id,
            "status": self.status.value,
            "reason": self.reason,
            "duration_ms": self.duration_ms,
            "output_artifact_refs": list(self.output_artifact_refs),
            "source_refs": [s.to_dict() for s in self.source_refs],
            "model_usage": self.model_usage.to_dict(),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


# ---- Module protocol -------------------------------------------------


@runtime_checkable
class EnrichmentModule(Protocol):
    """The interface every enrichment module conforms to.

 Modules are PURE in their decision surface — `should_run`
 inspects inputs + domain hints and returns a yes/no with a
 reason. Side-effect heavy work (LLM calls, artifact writes)
 is gated behind `run`. A module that doesn't have what it
 needs returns a SKIPPED outcome; failure is captured as a
 FAILED outcome with errors, never as a raised exception that
 blows up the run."""

    module_id: str
    """Stable identifier — matches `recommended_tasks` ids in
 `PostCompileEnrichPlan` (e.g. `metadata_enrichment`,
 `terminology_enrichment`, `validation`)."""

    def can_run(self, ctx: "EnrichmentContext") -> tuple[bool, str]:
        """Return (yes, reason). False with a reason means the
 module will be skipped with that reason recorded in the
 outcome. The reason is operator-readable."""
        ...

    def run(self, ctx: "EnrichmentContext") -> EnrichmentModuleOutcome:
        """Execute the module. MUST return a structured outcome —
 never raise. Failures are encoded as
 `EnrichmentModuleStatus.FAILED` with `errors` populated.

 Caller (the runner) aggregates outcomes onto the
 `EnrichmentResult`."""
        ...


# ---- Top-level overlay container ------------------------------------


@dataclass(frozen=True)
class EnrichmentResult:
    """The typed overlay produced by the enrichment stage.

 Persisted as the `enrichment_result` artifact and surfaced
 via `GET /ingestion-runs/{id}/enrichment-result`. Distinct from
 the existing per-enricher artifacts (`enriched.tables`,
 `enriched.visuals`, …) — those stay as-is; this is the
 aggregated typed view downstream consumers (post-compile
 reports, final summaries, FE panels) branch on.

 Fields:
 * `module_outcomes` — per-module structured outcome records
 (status, reason, duration, model usage, provenance).
 * `document_metadata_overlay` — extracted domain metadata.
 * `terminology_map` — terminology enricher output.
 * `classification_result` — document classifier output.
 * `table_summaries` / `image_summaries` — per-element summaries.
 * `validation_result` — validation enricher findings.
 * `retrieval_hints` — additional retrieval cues
 (synonyms, key terms) the indexer can consume.
 * `confidence_notes` — operator-readable notes about
 confidence ("classification confidence is low because…").
 * `warnings` / `errors` — aggregate non-blocking caveats +
 terminal errors across modules.
 * `model_usage` — aggregated cost/runtime across modules.
 * `duration_ms` — total enrichment-stage duration.
 * `skipped_reason` — operator-readable reason when the entire
 enrichment stage was skipped (e.g. SKIP verdict from the
 post-compile assessor). Populated only when
 `status == "skipped"`; empty otherwise.

 `status` aggregates the worst non-success module outcome:
 * `succeeded` — every module RUN or domain-skipped.
 * `succeeded_with_warnings` — at least one PARTIAL.
 * `failed` — at least one FAILED module. Caller layers the
 `require_enrichment_success` policy on top of this value to
 decide whether the run-level outcome is FAILED.
 * `skipped` — the entire enrichment stage was skipped before
 any module ran (distinct from per-module
 SKIPPED so the FE can render "enrichment skipped"
 differently from "every module skipped itself").
 """

    document_id: str
    schema_version: str = ENRICHMENT_RESULT_SCHEMA_VERSION
    status: str = "succeeded"  # succeeded / succeeded_with_warnings / failed / skipped
    skipped_reason: str = ""
    module_outcomes: tuple[EnrichmentModuleOutcome, ...] = ()
    document_metadata_overlay: DocumentMetadataOverlay = field(
        default_factory=DocumentMetadataOverlay,
    )
    terminology_map: tuple[TerminologyEntry, ...] = ()
    classification_result: ClassificationResult | None = None
    table_summaries: tuple[TableSummary, ...] = ()
    image_summaries: tuple[ImageSummary, ...] = ()
    validation_result: ValidationResult | None = None
    retrieval_hints: tuple[str, ...] = ()
    confidence_notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    model_usage: ModelUsageRecord = field(default_factory=ModelUsageRecord)
    duration_ms: int | None = None
    domain_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "document_id": self.document_id,
            "status": self.status,
            "skipped_reason": self.skipped_reason,
            "module_outcomes": [
                o.to_dict() for o in self.module_outcomes
            ],
            "document_metadata_overlay": self.document_metadata_overlay.to_dict(),
            "terminology_map": [t.to_dict() for t in self.terminology_map],
            "classification_result": (
                self.classification_result.to_dict()
                if self.classification_result else None
            ),
            "table_summaries": [t.to_dict() for t in self.table_summaries],
            "image_summaries": [i.to_dict() for i in self.image_summaries],
            "validation_result": (
                self.validation_result.to_dict()
                if self.validation_result else None
            ),
            "retrieval_hints": list(self.retrieval_hints),
            "confidence_notes": list(self.confidence_notes),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "model_usage": self.model_usage.to_dict(),
            "duration_ms": self.duration_ms,
            "domain_id": self.domain_id,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "EnrichmentResult":
        return cls(
            document_id=str(payload.get("document_id") or ""),
            schema_version=str(
                payload.get("schema_version") or ENRICHMENT_RESULT_SCHEMA_VERSION
            ),
            status=str(payload.get("status") or "succeeded"),
            skipped_reason=str(payload.get("skipped_reason") or ""),
            module_outcomes=tuple(
                _outcome_from_dict(o)
                for o in (payload.get("module_outcomes") or ())
                if isinstance(o, dict)
            ),
            document_metadata_overlay=_metadata_overlay_from_dict(
                payload.get("document_metadata_overlay") or {},
            ),
            terminology_map=tuple(
                _terminology_from_dict(t)
                for t in (payload.get("terminology_map") or ())
                if isinstance(t, dict)
            ),
            classification_result=(
                _classification_from_dict(payload["classification_result"])
                if payload.get("classification_result") else None
            ),
            table_summaries=tuple(
                _table_summary_from_dict(t)
                for t in (payload.get("table_summaries") or ())
                if isinstance(t, dict)
            ),
            image_summaries=tuple(
                _image_summary_from_dict(i)
                for i in (payload.get("image_summaries") or ())
                if isinstance(i, dict)
            ),
            validation_result=(
                _validation_from_dict(payload["validation_result"])
                if payload.get("validation_result") else None
            ),
            retrieval_hints=tuple(payload.get("retrieval_hints") or ()),
            confidence_notes=tuple(payload.get("confidence_notes") or ()),
            warnings=tuple(payload.get("warnings") or ()),
            errors=tuple(payload.get("errors") or ()),
            model_usage=ModelUsageRecord(
                **(payload.get("model_usage") or {})
            ),
            duration_ms=(
                int(payload["duration_ms"])
                if isinstance(payload.get("duration_ms"), int) else None
            ),
            domain_id=(
                str(payload["domain_id"])
                if payload.get("domain_id") else None
            ),
        )


# ---- from_payload helpers (kept module-private) ---------------------


def _outcome_from_dict(data: dict) -> EnrichmentModuleOutcome:
    try:
        status = EnrichmentModuleStatus(data.get("status") or "skipped")
    except ValueError:
        status = EnrichmentModuleStatus.SKIPPED
    return EnrichmentModuleOutcome(
        module_id=str(data.get("module_id") or "unknown"),
        status=status,
        reason=str(data.get("reason") or ""),
        duration_ms=(
            int(data["duration_ms"])
            if isinstance(data.get("duration_ms"), int) else None
        ),
        output_artifact_refs=tuple(data.get("output_artifact_refs") or ()),
        source_refs=tuple(
            _provenance_from_dict(s)
            for s in (data.get("source_refs") or ())
            if isinstance(s, dict)
        ),
        model_usage=ModelUsageRecord(**(data.get("model_usage") or {})),
        warnings=tuple(data.get("warnings") or ()),
        errors=tuple(data.get("errors") or ()),
    )


def _provenance_from_dict(data: dict) -> ProvenanceLink:
    return ProvenanceLink(
        source_artifact_id=(
            str(data["source_artifact_id"])
            if data.get("source_artifact_id") else None
        ),
        source_chunk_id=(
            str(data["source_chunk_id"])
            if data.get("source_chunk_id") else None
        ),
        source_kind=(
            str(data["source_kind"]) if data.get("source_kind") else None
        ),
        relation=str(data.get("relation") or "derived_from"),
    )


def _terminology_from_dict(data: dict) -> TerminologyEntry:
    return TerminologyEntry(
        term=str(data.get("term") or ""),
        normalized=(
            str(data["normalized"]) if data.get("normalized") else None
        ),
        synonyms=tuple(data.get("synonyms") or ()),
        definition=(
            str(data["definition"]) if data.get("definition") else None
        ),
        provenance=_provenance_from_dict(data.get("provenance") or {}),
    )


def _classification_from_dict(data: dict) -> ClassificationResult:
    raw_candidates = data.get("candidates") or ()
    candidates: list[tuple[str, float]] = []
    for c in raw_candidates:
        if not isinstance(c, dict):
            continue
        try:
            candidates.append(
                (str(c.get("category") or ""), float(c.get("confidence") or 0.0))
            )
        except (TypeError, ValueError):
            continue
    return ClassificationResult(
        category=(
            str(data["category"]) if data.get("category") else None
        ),
        subcategory=(
            str(data["subcategory"]) if data.get("subcategory") else None
        ),
        confidence=(
            float(data["confidence"])
            if isinstance(data.get("confidence"), (int, float)) else None
        ),
        candidates=tuple(candidates),
        reasoning=(
            str(data["reasoning"]) if data.get("reasoning") else None
        ),
        provenance=_provenance_from_dict(data.get("provenance") or {}),
    )


def _table_summary_from_dict(data: dict) -> TableSummary:
    return TableSummary(
        table_id=str(data.get("table_id") or "unknown"),
        title=str(data["title"]) if data.get("title") else None,
        summary=str(data["summary"]) if data.get("summary") else None,
        column_names=tuple(data.get("column_names") or ()),
        row_count=(
            int(data["row_count"])
            if isinstance(data.get("row_count"), int) else None
        ),
        provenance=_provenance_from_dict(data.get("provenance") or {}),
        warnings=tuple(data.get("warnings") or ()),
    )


def _image_summary_from_dict(data: dict) -> ImageSummary:
    return ImageSummary(
        image_id=str(data.get("image_id") or "unknown"),
        caption=str(data["caption"]) if data.get("caption") else None,
        role=str(data["role"]) if data.get("role") else None,
        confidence=(
            float(data["confidence"])
            if isinstance(data.get("confidence"), (int, float)) else None
        ),
        provenance=_provenance_from_dict(data.get("provenance") or {}),
        warnings=tuple(data.get("warnings") or ()),
    )


def _validation_from_dict(data: dict) -> ValidationResult:
    return ValidationResult(
        passed=bool(data.get("passed", True)),
        findings=tuple(
            ValidationFinding(
                rule=str(f.get("rule") or "unknown"),
                severity=str(f.get("severity") or "warning"),
                message=str(f.get("message") or ""),
                field_name=str(f["field"]) if f.get("field") else None,
                provenance=_provenance_from_dict(f.get("provenance") or {}),
            )
            for f in (data.get("findings") or ())
            if isinstance(f, dict)
        ),
        checked_rules=tuple(data.get("checked_rules") or ()),
    )


def _metadata_overlay_from_dict(data: dict) -> DocumentMetadataOverlay:
    return DocumentMetadataOverlay(
        fields={
            str(k): str(v) for k, v in (data.get("fields") or {}).items()
        },
        missing_required_fields=tuple(
            data.get("missing_required_fields") or ()
        ),
        extras={
            str(k): str(v) for k, v in (data.get("extras") or {}).items()
        },
        provenance=tuple(
            _provenance_from_dict(p)
            for p in (data.get("provenance") or ())
            if isinstance(p, dict)
        ),
    )
