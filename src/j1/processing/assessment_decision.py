"""Persistent ``AssessmentDecision`` — the source of truth for a run's
profile recommendation.

Created at ``POST /documents/{id}/assessment-plan`` time, threaded
through the ingest endpoint as ``assessmentDecisionId``, consumed by
the workflow which copies the decision into ``IngestionRun.metadata``
and short-circuits the build-initial-execution-plan activity when
the decision is valid.

Validation contract at consume time:

  * the decision exists in the per-(tenant, project) store;
  * ``decision.document_id`` matches the run's document;
  * ``decision.file_hash`` matches the document's current checksum
    when both sides have one (we accept either side missing to
    keep legacy paths working);
  * ``decision.schema_version`` is in the supported set.

When any check fails the workflow MAY rebuild the assessment and
stamps ``assessment_decision_source="rebuilt_fallback"`` with a
warning. Otherwise it stamps ``"persisted"``. The final report
exposes this so operators can audit whether what the FE picker
showed actually shaped the run.

Layout: stored as JSONL under the workspace's audit area
alongside ``ingestion_runs.jsonl`` and ``document_snapshots.jsonl``
so a single backup covers all three.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from j1.projects.context import ProjectContext
from j1.workspace.layout import WorkspaceArea
from j1.workspace.resolver import WorkspaceResolver


__all__ = [
    "ASSESSMENT_DECISION_SOURCE_PERSISTED",
    "ASSESSMENT_DECISION_SOURCE_REBUILT_FALLBACK",
    "ASSESSMENT_DECISION_SCHEMA_VERSION",
    "AssessmentDecision",
    "AssessmentDecisionStore",
    "AssessmentDecisionValidationError",
    "JsonlAssessmentDecisionStore",
    "validate_decision_for_document",
]


_log = logging.getLogger("j1.assessment_decision")
_DECISIONS_FILENAME = "assessment_decisions.jsonl"

ASSESSMENT_DECISION_SCHEMA_VERSION = "1"
SUPPORTED_SCHEMA_VERSIONS = frozenset({ASSESSMENT_DECISION_SCHEMA_VERSION})

# Wire vocabulary stamped onto IngestionRun.metadata + the run record
# so the FE / final report can render the right copy without inferring
# from a free-form string.
ASSESSMENT_DECISION_SOURCE_PERSISTED = "persisted"
ASSESSMENT_DECISION_SOURCE_REBUILT_FALLBACK = "rebuilt_fallback"


class AssessmentDecisionValidationError(ValueError):
    """Raised by ``validate_decision_for_document`` so callers can map
    the reason onto an operator-readable warning. The message string is
    stable enough to use verbatim in REST / workflow warnings."""


@dataclass(frozen=True)
class AssessmentDecision:
    """Persistent recommendation record.

    Authored by the REST assessment endpoint, consumed by the workflow.
    Frozen + JSON-serialisable so it survives a round-trip through
    Temporal's data converter.
    """

    assessment_decision_id: str
    document_id: str
    selected_domain_id: str
    recommended_profile: str
    effective_profile: str
    recommendation_source: str
    fallback_used: bool
    document_version_id: str | None = None
    file_hash: str | None = None
    selected_profile: str | None = None
    lightweight_assessment: dict[str, Any] | None = None
    matched_domain_rules: tuple[dict[str, Any], ...] = ()
    matched_general_rules: tuple[dict[str, Any], ...] = ()
    compile_option_preview: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    # Optional LLM Advanced Assessment payload. Populated by
    # ``POST /documents/{id}/advanced-assessment`` when the operator
    # explicitly runs the LLM helper. Shape follows the strict
    # contract in
    # :mod:`j1.processing.llm_advanced_assessment` — see
    # ``LLMAdvancedAssessmentResult.to_payload()``. ``None`` is the
    # default: Advanced Assessment NEVER runs automatically.
    llm_assessment_result: dict[str, Any] | None = None
    # Manual-action vocabulary the LLM (or domain rule) suggests
    # AFTER the document is indexed. Wire strings from
    # :mod:`j1.processing.manual_actions`. The FE renders these as
    # explicit buttons on the run-detail page; the workflow does
    # NOT auto-trigger any of them.
    recommended_next_steps: tuple[str, ...] = ()
    schema_version: str = ASSESSMENT_DECISION_SCHEMA_VERSION
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    # ---- (de)serialisation ----

    def to_payload(self) -> dict[str, Any]:
        return {
            "assessmentDecisionId": self.assessment_decision_id,
            "documentId": self.document_id,
            "documentVersionId": self.document_version_id,
            "fileHash": self.file_hash,
            "selectedDomainId": self.selected_domain_id,
            "lightweightAssessment": self.lightweight_assessment,
            "matchedDomainRules": list(self.matched_domain_rules),
            "matchedGeneralRules": list(self.matched_general_rules),
            "recommendedProfile": self.recommended_profile,
            "selectedProfile": self.selected_profile,
            "effectiveProfile": self.effective_profile,
            "recommendationSource": self.recommendation_source,
            "fallbackUsed": self.fallback_used,
            "compileOptionPreview": dict(self.compile_option_preview),
            "warnings": list(self.warnings),
            "llmAssessmentResult": (
                dict(self.llm_assessment_result)
                if self.llm_assessment_result is not None else None
            ),
            "recommendedNextSteps": list(self.recommended_next_steps),
            "schemaVersion": self.schema_version,
            "createdAt": self.created_at.isoformat(),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "AssessmentDecision":
        """Hydrate from the wire / store payload. Tolerates extra keys
        so the wire schema can evolve without breaking older readers."""
        created_raw = payload.get("createdAt") or payload.get("created_at")
        if isinstance(created_raw, str):
            try:
                created_at = datetime.fromisoformat(created_raw)
            except ValueError:
                created_at = datetime.now(timezone.utc)
        elif isinstance(created_raw, datetime):
            created_at = created_raw
        else:
            created_at = datetime.now(timezone.utc)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        def _tuple_of_dicts(key_camel: str, key_snake: str) -> tuple[dict, ...]:
            raw = payload.get(key_camel)
            if raw is None:
                raw = payload.get(key_snake)
            if not isinstance(raw, list):
                return ()
            return tuple(r for r in raw if isinstance(r, dict))

        def _warning_tuple(key_camel: str, key_snake: str) -> tuple[str, ...]:
            raw = payload.get(key_camel) or payload.get(key_snake) or ()
            if not isinstance(raw, (list, tuple)):
                return ()
            return tuple(str(w) for w in raw if w is not None)

        return cls(
            assessment_decision_id=str(
                payload.get("assessmentDecisionId")
                or payload.get("assessment_decision_id")
                or ""
            ),
            document_id=str(
                payload.get("documentId")
                or payload.get("document_id")
                or ""
            ),
            document_version_id=(
                payload.get("documentVersionId")
                or payload.get("document_version_id")
            ),
            file_hash=(
                payload.get("fileHash")
                or payload.get("file_hash")
            ),
            selected_domain_id=str(
                payload.get("selectedDomainId")
                or payload.get("selected_domain_id")
                or ""
            ),
            lightweight_assessment=(
                payload.get("lightweightAssessment")
                or payload.get("lightweight_assessment")
            ),
            matched_domain_rules=_tuple_of_dicts(
                "matchedDomainRules", "matched_domain_rules",
            ),
            matched_general_rules=_tuple_of_dicts(
                "matchedGeneralRules", "matched_general_rules",
            ),
            recommended_profile=str(
                payload.get("recommendedProfile")
                or payload.get("recommended_profile")
                or ""
            ),
            selected_profile=(
                payload.get("selectedProfile")
                or payload.get("selected_profile")
            ),
            effective_profile=str(
                payload.get("effectiveProfile")
                or payload.get("effective_profile")
                or ""
            ),
            recommendation_source=str(
                payload.get("recommendationSource")
                or payload.get("recommendation_source")
                or ""
            ),
            fallback_used=bool(
                payload.get("fallbackUsed")
                if payload.get("fallbackUsed") is not None
                else payload.get("fallback_used", False)
            ),
            compile_option_preview=dict(
                payload.get("compileOptionPreview")
                or payload.get("compile_option_preview")
                or {}
            ),
            warnings=_warning_tuple("warnings", "warnings"),
            llm_assessment_result=(
                dict(payload["llmAssessmentResult"])
                if isinstance(
                    payload.get("llmAssessmentResult"), dict,
                ) else (
                    dict(payload["llm_assessment_result"])
                    if isinstance(
                        payload.get("llm_assessment_result"), dict,
                    ) else None
                )
            ),
            recommended_next_steps=_warning_tuple(
                "recommendedNextSteps", "recommended_next_steps",
            ),
            schema_version=str(
                payload.get("schemaVersion")
                or payload.get("schema_version")
                or ASSESSMENT_DECISION_SCHEMA_VERSION
            ),
            created_at=created_at,
        )


# ---- Store ----------------------------------------------------------


class AssessmentDecisionStore:
    """Protocol-shaped base. Tests use an in-memory subclass; the dev
    deployment uses ``JsonlAssessmentDecisionStore``."""

    def upsert(
        self, ctx: ProjectContext, decision: AssessmentDecision,
    ) -> None:
        raise NotImplementedError

    def get(
        self, ctx: ProjectContext, decision_id: str,
    ) -> AssessmentDecision | None:
        raise NotImplementedError

    def latest_for_document(
        self, ctx: ProjectContext, document_id: str,
    ) -> AssessmentDecision | None:
        """Most recently persisted decision for ``document_id``, or
        ``None`` when the document has no decisions yet. The default
        implementation returns ``None`` so legacy in-memory test
        stubs keep working — JSONL store overrides."""
        return None


class JsonlAssessmentDecisionStore(AssessmentDecisionStore):
    """Append-only JSONL store mirroring ``JsonlIngestionRunStore`` and
    ``JsonlDocumentSnapshotStore`` (same workspace area, last-write
    wins on read, malformed lines are skipped).

    Located under the workspace's ``audit`` area so a single backup
    covers run records, snapshots, AND assessment decisions.
    """

    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    # ---- Path ------------------------------------------------------

    def _path(self, ctx: ProjectContext) -> Path:
        return (
            self._workspace.area(ctx, WorkspaceArea.AUDIT)
            / _DECISIONS_FILENAME
        )

    # ---- Writes ----------------------------------------------------

    def upsert(
        self, ctx: ProjectContext, decision: AssessmentDecision,
    ) -> None:
        path = self._path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            decision.to_payload(), separators=(",", ":"),
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")

    # ---- Reads -----------------------------------------------------

    def get(
        self, ctx: ProjectContext, decision_id: str,
    ) -> AssessmentDecision | None:
        latest: AssessmentDecision | None = None
        for decision in self._iter_all(ctx):
            if decision.assessment_decision_id == decision_id:
                latest = decision
        return latest

    def latest_for_document(
        self, ctx: ProjectContext, document_id: str,
    ) -> AssessmentDecision | None:
        """Most-recently-persisted decision matching ``document_id``.
        Used by the ``/assessment-plan`` handler to surface the
        previous LLM assessment (if any) to the picker without
        re-running the LLM."""
        latest: AssessmentDecision | None = None
        for decision in self._iter_all(ctx):
            if decision.document_id != document_id:
                continue
            if latest is None or decision.created_at >= latest.created_at:
                latest = decision
        return latest

    def _iter_all(
        self, ctx: ProjectContext,
    ) -> Iterable[AssessmentDecision]:
        path = self._path(ctx)
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                yield AssessmentDecision.from_payload(payload)


# ---- Validation -----------------------------------------------------


def validate_decision_for_document(
    decision: AssessmentDecision,
    *,
    document_id: str,
    file_hash: str | None = None,
    document_version_id: str | None = None,
) -> None:
    """Raise ``AssessmentDecisionValidationError`` if the decision
    can't legitimately drive the run.

    The error messages are stable enough to surface directly in REST /
    workflow warnings — the FE / final report can map them to copy.
    """
    if not decision.assessment_decision_id:
        raise AssessmentDecisionValidationError(
            "assessment decision is missing an id"
        )
    if decision.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise AssessmentDecisionValidationError(
            f"assessment decision schema version "
            f"{decision.schema_version!r} is not supported"
        )
    if decision.document_id and decision.document_id != document_id:
        raise AssessmentDecisionValidationError(
            f"assessment decision {decision.assessment_decision_id!r} "
            f"belongs to document {decision.document_id!r}, not "
            f"{document_id!r}"
        )
    if (
        decision.file_hash
        and file_hash
        and decision.file_hash != file_hash
    ):
        raise AssessmentDecisionValidationError(
            f"assessment decision {decision.assessment_decision_id!r} "
            "was built against a different file (hash mismatch); "
            "re-run the assessment for the current upload"
        )
    if (
        decision.document_version_id
        and document_version_id
        and decision.document_version_id != document_version_id
    ):
        raise AssessmentDecisionValidationError(
            f"assessment decision {decision.assessment_decision_id!r} "
            "was built against a different document version"
        )


# ---- Factory --------------------------------------------------------


def new_decision_id() -> str:
    """Stable wire id. Mirrors the run-id / snapshot-id 12-char
    hex idiom used elsewhere in the codebase."""
    return f"ad-{secrets.token_hex(8)}"
