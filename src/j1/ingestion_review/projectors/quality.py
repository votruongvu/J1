"""QualityReportProjector — compose a neutral quality report.

Combines four input streams into one UI-friendly DTO:

  * `enriched.confidence_assessment` artifacts → overall confidence,
    per-modality breakdown, low-confidence findings.
  * `enriched.consistency_findings` artifacts → low-confidence
    findings (consistency-checker side).
  * Audit-log warnings already collected by the service →
    `warnings[]`.
  * `IngestionRun.metadata["step_results"]` → `skippedSteps[]` and
    `failedOptionalSteps[]`.

Vendor-neutral: producers may emit JSON in many shapes; the projector
tolerates snake_case AND camelCase field names and degrades gracefully
when fields are missing. The unprojected source JSON is exposed only
under `raw_debug` and only when the caller opts in.
"""

from __future__ import annotations

import json
import logging
import statistics
from pathlib import Path
from typing import Any, Iterable

from j1.artifacts.models import ArtifactRecord
from j1.ingestion_review.dtos import (
    FailedOptionalStepDTO,
    LowConfidenceFindingDTO,
    ModalityConfidenceDTO,
    QualityReportDTO,
    SkippedStepDTO,
    WarningDTO,
)

_log = logging.getLogger("j1.ingestion_review.quality")

# Artifact kinds the projector reads. Only JSON-format artifacts are
# inspected (markdown siblings carry no machine-readable signal).
KIND_CONFIDENCE_ASSESSMENT = "enriched.confidence_assessment"
KIND_CONSISTENCY_FINDINGS = "enriched.consistency_findings"


class QualityReportProjector:
    """Build a `QualityReportDTO` for one run.

    Constructor takes the path-resolver callable (same pattern as
    `ChunkProjector`) so the projector stays workspace-agnostic and
    inherits the path-traversal guard from the caller's context."""

    def __init__(self, *, path_resolver) -> None:
        self._path_resolver = path_resolver

    def project(
        self,
        artifacts: list[ArtifactRecord],
        *,
        warnings: list[WarningDTO],
        step_results: list[dict[str, Any]],
        include_raw: bool = False,
    ) -> QualityReportDTO:
        confidence_payloads = self._read_payloads(
            artifacts, kind=KIND_CONFIDENCE_ASSESSMENT,
        )
        consistency_payloads = self._read_payloads(
            artifacts, kind=KIND_CONSISTENCY_FINDINGS,
        )

        modality = _project_modality_confidences(
            confidence_payloads, artifacts,
        )
        overall = _project_overall_confidence(
            confidence_payloads, artifacts, modality,
        )
        findings = _project_low_confidence_findings(
            confidence_payloads, consistency_payloads,
        )
        skipped, failed_optional = _project_step_outcomes(step_results)

        # Surface confidence-assessor LLM failures as quality
        # warnings. Without this, an LLM call that errored produced
        # an artifact with no `overall_confidence` AND (since the
        # enricher recently stopped fabricating `default_confidence`
        # on error) no fallback — the FE would show "—" with no
        # explanation. Synthesising an explicit warning per failed
        # assessment keeps the UX honest and operator-actionable.
        merged_warnings = list(warnings)
        merged_warnings.extend(
            _project_confidence_assessment_failures(confidence_payloads),
        )

        raw_debug: dict[str, Any] | None = None
        if include_raw:
            raw_debug = {
                "confidence_assessment": [p.payload for p in confidence_payloads],
                "consistency_findings": [p.payload for p in consistency_payloads],
            }

        return QualityReportDTO(
            overall_confidence=overall,
            modality_confidences=modality,
            warnings=merged_warnings,
            skipped_steps=skipped,
            failed_optional_steps=failed_optional,
            low_confidence_findings=findings,
            raw_debug=raw_debug,
        )

    # ---- Internals ---------------------------------------------------

    def _read_payloads(
        self, artifacts: list[ArtifactRecord], *, kind: str,
    ) -> list["_ArtifactPayload"]:
        out: list[_ArtifactPayload] = []
        for artifact in artifacts:
            if artifact.kind != kind:
                continue
            # Skip the markdown sibling enrichers also produce — only
            # JSON carries machine-readable signal.
            location = artifact.location.lower()
            if not location.endswith(".json"):
                continue
            try:
                path = self._path_resolver(artifact)
            except Exception:  # noqa: BLE001 — projector must not crash the report
                _log.warning(
                    "quality artifact %s not readable; skipping",
                    artifact.artifact_id,
                )
                continue
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                _log.warning(
                    "quality artifact %s has invalid JSON: %s",
                    artifact.artifact_id, exc,
                )
                continue
            if not isinstance(payload, dict):
                # Unsupported top-level shape — log and move on.
                continue
            out.append(_ArtifactPayload(
                artifact_id=artifact.artifact_id,
                source_artifact_ids=list(artifact.source_artifact_ids),
                source_document_ids=list(artifact.source_document_ids),
                metadata=dict(artifact.metadata),
                payload=payload,
            ))
        return out


# ---- Internal types -------------------------------------------------


class _ArtifactPayload:
    """One quality artifact's parsed JSON plus the lineage we need
    for traceability fields. Plain class (not a frozen dataclass) so
    `payload` can stay a mutable dict for downstream readers."""

    def __init__(
        self,
        *,
        artifact_id: str,
        source_artifact_ids: list[str],
        source_document_ids: list[str],
        metadata: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        self.artifact_id = artifact_id
        self.source_artifact_ids = source_artifact_ids
        self.source_document_ids = source_document_ids
        self.metadata = metadata
        self.payload = payload


# ---- Projection helpers ---------------------------------------------


def _project_modality_confidences(
    confidence_payloads: list[_ArtifactPayload],
    artifacts: list[ArtifactRecord],  # noqa: ARG001 — reserved for future weighting
) -> list[ModalityConfidenceDTO]:
    """Group `assessments[]` entries by modality, average the confidence,
    and surface the count.

    Producer schema (tolerated):
        assessments: [{ modality, confidence, sample_count? }, ...]
    Both snake_case and camelCase field names are accepted."""
    grouped: dict[str, list[float]] = {}
    counts: dict[str, int] = {}
    for entry in _iter_assessments(confidence_payloads):
        modality = _str_field(entry, "modality")
        confidence = _float_field(entry, "confidence", "score")
        if not modality or confidence is None:
            continue
        grouped.setdefault(modality, []).append(confidence)
        sample = _int_field(entry, "sample_count", "sampleCount", "samples")
        if sample is not None:
            counts[modality] = counts.get(modality, 0) + sample
        else:
            counts[modality] = counts.get(modality, 0) + 1
    out: list[ModalityConfidenceDTO] = []
    for modality, scores in sorted(grouped.items()):
        out.append(ModalityConfidenceDTO(
            modality=modality,
            confidence=round(statistics.fmean(scores), 4),
            sample_count=counts.get(modality),
        ))
    return out


def _project_overall_confidence(
    confidence_payloads: list[_ArtifactPayload],
    artifacts: list[ArtifactRecord],
    modality: list[ModalityConfidenceDTO],
) -> float | None:
    """Pick the overall confidence value, in priority order:

      1. `overall_confidence` / `overallConfidence` field on any
         payload (most explicit).
      2. Mean of modality confidences if the projector produced any.
      3. `default_confidence` field on any payload (the stub
         enricher's contract today).
      4. `metadata["confidence"]` on the artifact record (set by the
         _StructuredEnricher base class).

    Returns None when none of the above are available."""
    for payload in confidence_payloads:
        explicit = _float_field(
            payload.payload, "overall_confidence", "overallConfidence",
        )
        if explicit is not None:
            return round(explicit, 4)

    if modality:
        return round(
            statistics.fmean(m.confidence for m in modality), 4,
        )

    for payload in confidence_payloads:
        default = _float_field(payload.payload, "default_confidence", "defaultConfidence")
        if default is not None:
            return round(default, 4)

    for artifact in artifacts:
        if artifact.kind != KIND_CONFIDENCE_ASSESSMENT:
            continue
        meta_value = artifact.metadata.get("confidence")
        try:
            return round(float(meta_value), 4)
        except (TypeError, ValueError):
            continue

    return None


def _project_confidence_assessment_failures(
    confidence_payloads: list[_ArtifactPayload],
) -> list[WarningDTO]:
    """Synthesise a warning per confidence-assessment artifact whose
    payload carries an `error` field (LLM call failed).

    Producers (the `ConfidenceAssessor` enricher) record `error` when
    the structured-output extraction raised. The projector previously
    consumed only the happy path — readers couldn't tell whether a
    missing `overall_confidence` meant "LLM said so" or "LLM didn't
    run". Surfacing as a warning keeps the Quality tab honest without
    pretending the assessment was healthy.
    """
    out: list[WarningDTO] = []
    for payload in confidence_payloads:
        error = payload.payload.get("error")
        if not error:
            continue
        out.append(WarningDTO(
            code="CONFIDENCE_ASSESSMENT_UNAVAILABLE",
            message=(
                "Confidence assessment unavailable: the LLM extraction "
                f"failed ({error})."
            ),
            severity="warning",
            step="enrich",
            artifact_id=payload.artifact_id,
        ))
    return out


def _project_low_confidence_findings(
    confidence_payloads: list[_ArtifactPayload],
    consistency_payloads: list[_ArtifactPayload],
) -> list[LowConfidenceFindingDTO]:
    """Compose the list of low-confidence regions.

    Sources:
      * `assessments[]` entries with a `confidence < 0.7` floor get
        surfaced as `LowConfidenceFindingDTO` so the UI can highlight
        them on the page/chunk they came from.
      * `findings[]` entries on consistency-checker output (the kind
        is dedicated to this).

    Source-traceability (page / chunk / artifact) is preserved
    when the producer emitted those fields. The artifact_id falls
    back to the producing artifact's id when missing."""
    out: list[LowConfidenceFindingDTO] = []

    for payload in confidence_payloads:
        for entry in _iter_assessments([payload]):
            score = _float_field(entry, "confidence", "score")
            # Only surface the low-confidence ones from confidence
            # assessments — the high-confidence ones are noise on the
            # FE. 0.7 is the conventional cutoff; tunable per
            # deployment via a future config.
            if score is None or score >= 0.7:
                continue
            out.append(_finding_from_entry(
                entry, default_score=score,
                default_category="confidence",
                fallback_artifact_id=payload.artifact_id,
            ))

    for payload in consistency_payloads:
        for entry in _iter_findings([payload]):
            score = _float_field(entry, "score", "confidence")
            if score is None:
                # Consistency findings without a numeric score still
                # surface — the FE can render a category badge. Use
                # 0.0 so the FE knows it's an "issue" weight.
                score = 0.0
            out.append(_finding_from_entry(
                entry, default_score=score,
                default_category="consistency",
                fallback_artifact_id=payload.artifact_id,
            ))
    return out


def _project_step_outcomes(
    step_results: list[dict[str, Any]],
) -> tuple[list[SkippedStepDTO], list[FailedOptionalStepDTO]]:
    """Split persisted step_results into the two FE-facing buckets.

    Skipped steps: always surfaced regardless of `required`.
    Failed-optional steps: only entries with `status=failed` AND
    `required=false`. Required failures already drove the run to
    FAILED at the workflow level — we don't double-surface them."""
    skipped: list[SkippedStepDTO] = []
    failed_optional: list[FailedOptionalStepDTO] = []
    for entry in step_results:
        if not isinstance(entry, dict):
            continue
        step = _str_field(entry, "step")
        if not step:
            continue
        status = str(entry.get("status") or "").lower()
        reason = _str_field(entry, "reason")
        if status == "skipped":
            source = _str_field(entry, "source")
            skipped.append(SkippedStepDTO(
                step=step, reason=reason, policy=source,
            ))
            continue
        if status == "failed" and not bool(entry.get("required")):
            error_type: str | None = None
            error = entry.get("error")
            if isinstance(error, dict):
                error_type = _str_field(error, "type")
            failed_optional.append(FailedOptionalStepDTO(
                step=step, reason=reason, error_type=error_type,
            ))
    return skipped, failed_optional


def _finding_from_entry(
    entry: dict[str, Any],
    *,
    default_score: float,
    default_category: str,
    fallback_artifact_id: str,
) -> LowConfidenceFindingDTO:
    """Build a `LowConfidenceFindingDTO` from a producer-supplied
    entry, preserving any source-traceability fields it carries."""
    return LowConfidenceFindingDTO(
        score=default_score,
        category=_str_field(entry, "category", "type") or default_category,
        message=_str_field(entry, "message", "description", "note"),
        page=_int_field(entry, "page", "page_idx", "pageIndex"),
        chunk_id=_str_field(entry, "chunk_id", "chunkId"),
        artifact_id=_str_field(
            entry, "artifact_id", "artifactId",
        ) or fallback_artifact_id,
    )


def _iter_assessments(
    payloads: Iterable[_ArtifactPayload],
) -> Iterable[dict[str, Any]]:
    """Yield each assessment entry inside the confidence payloads.

    Tolerates the producer placing entries under either `assessments`
    (current stub) or `findings` (a richer producer schema)."""
    for payload in payloads:
        for key in ("assessments", "findings"):
            entries = payload.payload.get(key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict):
                    yield entry


def _iter_findings(
    payloads: Iterable[_ArtifactPayload],
) -> Iterable[dict[str, Any]]:
    """Yield each finding entry inside the consistency payloads."""
    for payload in payloads:
        entries = payload.payload.get("findings")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                yield entry


# ---- Field helpers (mirror chunks.py — narrow & local) -------------


def _str_field(d: dict, *keys: str) -> str | None:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        text = str(v).strip()
        if text:
            return text
    return None


def _int_field(d: dict, *keys: str) -> int | None:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


def _float_field(d: dict, *keys: str) -> float | None:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None
