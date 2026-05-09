"""Post-compile planning Temporal activity.

The workflow runs replay-deterministic; LLM calls + artifact writes
must happen here in activity context. This module wraps the pure
post-compile planning core (`j1.processing.post_compile_planning`)
in a Temporal activity that:

  1. Reads the compile-stage's `parsed_content_manifest` artifact
     for the document.
  2. Builds the rule-based plan (Document Understanding + Content
     Digest + Post-Compile Assessment).
  3. Optionally calls the registered FAST/PREMIUM/etc. LLM role
     when `J1_LLM_PLANNING_ENABLED=true`.
  4. Validates the result, falls back when configured, persists
     `planning_result.json` as an `ARTIFACT_KIND_PLANNING_RESULT`
     artifact tagged with `run_id`.

Failure modes intentionally fall through to a rule-based plan when
`fail_open=True` so the workflow never blocks on planner trouble."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from temporalio import activity

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactRegistry
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.llm.registry import LLMProviderRegistry
from j1.orchestration.activities.payloads import ProjectScope
from j1.processing.document_understanding import DocumentMetadata
from j1.processing.manifest import ParsedContentManifest
from j1.processing.planning_llm import (
    PlanningLLMError,
    build_llm_planner,
)
from j1.processing.planning_result import (
    PlanningResult,
    PlanningValidationError,
)
from j1.processing.planning_settings import PlanningSettings
from j1.processing.post_compile_planning import build_planning_result
from j1.processing.profiling import DocumentProfile
from j1.processing.results import (
    ARTIFACT_KIND_PARSED_CONTENT_MANIFEST,
    ARTIFACT_KIND_PLANNING_RESULT,
)
from j1.runs.reporter import ProgressReporter
from j1.workspace.layout import WorkspaceArea
from j1.workspace.resolver import WorkspaceResolver


ACTIVITY_BUILD_PLANNING_RESULT = "j1.processing.build_planning_result"


_log = logging.getLogger("j1.planning.activity")


__all__ = [
    "ACTIVITY_BUILD_PLANNING_RESULT",
    "BuildPlanningResultInput",
    "BuildPlanningResultOutput",
    "PlanningActivities",
]


# ---- Payloads ---------------------------------------------------------


@dataclass(frozen=True)
class BuildPlanningResultInput:
    """Workflow → activity payload.

    Carries only the metadata the activity needs to find the manifest
    and tag the new artifact. The activity reads the manifest itself
    from the workspace; the workflow never serialises parser output
    over the wire."""

    scope: ProjectScope
    run_id: str
    document_id: str
    document_filename: str | None = None
    document_mime_type: str | None = None
    document_extension: str | None = None
    document_metadata_title: str | None = None
    document_language: str | None = None
    file_size_bytes: int | None = None
    # The planner's overlay-merged DocumentProfile (after
    # `_merge_compile_signals`). Serialised as a plain dict because
    # `DocumentProfile` is a frozen dataclass with tuple fields the
    # Temporal data converter handles correctly, but we keep it loose
    # here for forward compatibility.
    profile_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class BuildPlanningResultOutput:
    """Activity → workflow payload.

    The full planning result is also persisted as an artifact, but
    we surface the high-level decisions inline so the workflow can
    apply downstream gates without re-reading the artifact. `source`
    tells the workflow whether to log a `plan.revised` event for the
    LLM-driven path or stick with `plan.generated`."""

    artifact_id: str
    source: str  # rule_based | llm | rule_based_fallback
    recommended_profile: str
    confidence: float
    document_type: str
    execution_plan: dict[str, Any]
    warnings: tuple[str, ...]


# ---- Activity ---------------------------------------------------------


class PlanningActivities:
    """Bundle of post-compile planning activities.

    Constructor takes the data sources directly — no facade — so
    bootstrap stays explicit and the activity is trivially
    constructable in tests with stub registries / fake clocks."""

    def __init__(
        self,
        *,
        workspace: WorkspaceResolver,
        artifacts: ArtifactRegistry,
        llm_registry: LLMProviderRegistry | None,
        planning_settings: PlanningSettings,
        progress_reporter: ProgressReporter | None = None,
        clock=datetime.now,
    ) -> None:
        self._workspace = workspace
        self._artifacts = artifacts
        self._llm_registry = llm_registry
        self._settings = planning_settings
        self._reporter = progress_reporter
        self._clock = clock

    def all_activities(self) -> list:
        return [self.build_planning_result]

    @activity.defn(name=ACTIVITY_BUILD_PLANNING_RESULT)
    def build_planning_result(
        self,
        input: BuildPlanningResultInput,
    ) -> BuildPlanningResultOutput | None:
        """Build + persist the post-compile planning result.

        Returns None when post-compile planning is disabled or no
        manifest is available (legacy/short runs). The workflow
        treats None as "no planning result" and proceeds with the
        existing initial plan."""
        if not self._settings.post_compile_enabled:
            _log.info(
                "post-compile planning disabled by config; skipping run=%s doc=%s",
                input.run_id, input.document_id,
            )
            return None

        ctx = input.scope.to_context()

        manifest = self._read_manifest(ctx, input.run_id, input.document_id)
        if manifest is None:
            _log.info(
                "no parsed_content_manifest for run=%s doc=%s; skipping post-compile planning",
                input.run_id, input.document_id,
            )
            return None

        document = DocumentMetadata(
            document_id=input.document_id,
            filename=input.document_filename,
            mime_type=input.document_mime_type,
            extension=input.document_extension,
            metadata_title=input.document_metadata_title,
            language=input.document_language,
        )
        profile = _profile_from_payload(input.profile_payload)
        llm_planner = self._build_llm_planner_callable()

        timing_started = time.perf_counter()
        try:
            result = build_planning_result(
                run_id=input.run_id,
                document=document,
                file_size_bytes=input.file_size_bytes,
                profile=profile,
                manifest=manifest,
                settings=self._settings,
                llm_planner=llm_planner,
                now=self._clock(timezone.utc) if _accepts_tz(self._clock)
                    else datetime.now(timezone.utc),
            )
        except PlanningValidationError as exc:
            # `fail_open=False` reaches here. Re-raise so the workflow
            # surfaces a planning failure; the existing initial plan
            # still drives downstream gates.
            _log.error(
                "post-compile planning failed (fail_open=False) run=%s: %s",
                input.run_id, exc,
            )
            raise
        except PlanningLLMError as exc:
            # When fail_open=True the core already swallowed this;
            # this branch only hits when fail_open=False AND the LLM
            # path was taken. Treat identically.
            _log.error(
                "post-compile planning LLM call failed run=%s: %s",
                input.run_id, exc,
            )
            raise

        elapsed_ms = int((time.perf_counter() - timing_started) * 1000)
        if self._settings.trace_enabled:
            _log.info(
                "post-compile planning complete run=%s source=%s profile=%s "
                "type=%s confidence=%.2f elapsed_ms=%d",
                input.run_id, result.source, result.recommended_profile,
                (result.document_understanding or {}).get("document_type"),
                result.confidence, elapsed_ms,
            )

        artifact = self._persist_planning_result(
            ctx,
            run_id=input.run_id,
            document_id=input.document_id,
            result=result,
        )

        # Best-effort: emit a planning.completed-style audit event
        # via the existing plan.revised reporter. The FE Planning
        # Report tab unlocks via `availableViews.planning` which is
        # driven off the artifact's presence, so this is purely a
        # timeline nicety.
        self._maybe_emit_progress(ctx, input.run_id, result)

        plan_dict = dict(result.execution_plan or {})
        return BuildPlanningResultOutput(
            artifact_id=artifact.artifact_id,
            source=result.source,
            recommended_profile=result.recommended_profile,
            confidence=result.confidence,
            document_type=str(
                (result.document_understanding or {}).get("document_type")
                or "unknown",
            ),
            execution_plan=plan_dict,
            warnings=tuple(result.warnings),
        )

    # ---- Manifest read --------------------------------------------------

    def _read_manifest(
        self,
        ctx,
        run_id: str,
        document_id: str,
    ) -> ParsedContentManifest | None:
        """Find this run's parsed_content_manifest artifact and load
        it from disk. Returns None on any failure — the caller
        downgrades to "no post-compile plan"."""
        try:
            records = self._artifacts.list_artifacts(ctx)
        except Exception:  # noqa: BLE001 — registry read is best-effort
            return None
        candidates = [
            r for r in records
            if r.kind == ARTIFACT_KIND_PARSED_CONTENT_MANIFEST
            and (
                r.metadata.get("run_id") == run_id
                or document_id in (r.source_document_ids or [])
            )
        ]
        if not candidates:
            return None
        # Prefer the run-tagged manifest, fall back to most recent.
        candidates.sort(
            key=lambda r: (
                r.metadata.get("run_id") != run_id,
                -int(r.updated_at.timestamp()) if r.updated_at else 0,
            ),
        )
        record = candidates[0]
        try:
            path = self._resolve_artifact_path(ctx, record)
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return ParsedContentManifest.from_dict(data)

    def _resolve_artifact_path(self, ctx, record: ArtifactRecord) -> Path:
        location = (record.location or "").strip()
        if not location or "/" not in location:
            raise ValueError(f"artifact {record.artifact_id} location malformed")
        area_name, _, rest = location.partition("/")
        try:
            area = WorkspaceArea(area_name)
        except ValueError as exc:
            raise ValueError(
                f"artifact {record.artifact_id} unknown area {area_name!r}"
            ) from exc
        return self._workspace.area(ctx, area) / rest

    # ---- LLM wiring -----------------------------------------------------

    def _build_llm_planner_callable(self):
        """Resolve the configured planner model; return None when the
        feature flag is off OR no LLM is configured."""
        if not self._settings.llm_planning_enabled:
            return None
        if self._llm_registry is None:
            _log.warning(
                "J1_LLM_PLANNING_ENABLED=true but no LLM registry wired; "
                "falling back to rule-based plan",
            )
            return None
        client = _resolve_llm_client(
            self._llm_registry,
            profile=self._settings.model_profile,
        )
        if client is None:
            _log.warning(
                "planner model_profile=%s not available; falling back to rule-based plan",
                self._settings.model_profile,
            )
            return None
        return build_llm_planner(client=client)

    # ---- Persistence ----------------------------------------------------

    def _persist_planning_result(
        self,
        ctx,
        *,
        run_id: str,
        document_id: str,
        result: PlanningResult,
    ) -> ArtifactRecord:
        """Write `planning_result.json` into the workspace's COMPILED
        area and register an `ARTIFACT_KIND_PLANNING_RESULT` record.

        Co-located with the parsed-content manifest under COMPILED so
        operators can find both artifacts in one place; the artifact
        kind disambiguates."""
        content = result.to_json_bytes()
        artifact_id = f"planning_{run_id}_{document_id}"
        filename = f"{artifact_id}.json"
        area_dir = self._workspace.area(ctx, WorkspaceArea.COMPILED)
        area_dir.mkdir(parents=True, exist_ok=True)
        final_path = area_dir / filename
        tmp_path = final_path.with_suffix(final_path.suffix + ".tmp")
        tmp_path.write_bytes(content)
        tmp_path.replace(final_path)

        content_hash = "sha256:" + hashlib.sha256(content).hexdigest()
        now = self._clock(timezone.utc) if _accepts_tz(self._clock) else datetime.now(timezone.utc)
        record = ArtifactRecord(
            artifact_id=artifact_id,
            project=ctx,
            kind=ARTIFACT_KIND_PLANNING_RESULT,
            location=f"{WorkspaceArea.COMPILED.value}/{filename}",
            content_hash=content_hash,
            byte_size=len(content),
            status=ProcessingStatus.SUCCEEDED,
            review_status=ReviewStatus.NOT_REQUIRED,
            version=1,
            created_at=now,
            updated_at=now,
            source_document_ids=[document_id],
            source_artifact_ids=[],
            metadata={
                "run_id": run_id,
                "filename": filename,
                "planning_source": result.source,
                "planning_profile": result.recommended_profile,
            },
        )
        try:
            self._artifacts.add(record)
        except Exception:
            # Idempotency: a replay that already added the record
            # should not blow up the workflow. Best-effort log.
            _log.warning(
                "planning_result registry write failed (likely duplicate) "
                "run=%s doc=%s",
                run_id, document_id,
            )
        return record

    # ---- Progress event -----------------------------------------------

    def _maybe_emit_progress(
        self,
        ctx,
        run_id: str,
        result: PlanningResult,
    ) -> None:
        """Best-effort `plan.revised` audit-log entry.

        The FE plan card already swaps to a revised plan when a
        `plan.revised` event lands; we reuse that hook so older
        bundles keep working. The full planning detail comes from
        the new artifact via `/ingestion-runs/{id}/planning`."""
        if self._reporter is None:
            return
        try:
            self._reporter.report_plan_revised(
                ctx,
                run_id=run_id,
                plan_payload={
                    "source": result.source,
                    "recommended_profile": result.recommended_profile,
                    "confidence": result.confidence,
                    "document_type": (
                        (result.document_understanding or {})
                        .get("document_type")
                    ),
                },
                reason=f"post-compile planning ({result.source})",
            )
        except Exception:  # noqa: BLE001 — telemetry never blocks workflow
            pass


# ---- Helpers ----------------------------------------------------------


def _profile_from_payload(
    payload: dict[str, Any] | None,
) -> DocumentProfile | None:
    if not payload:
        return None
    try:
        return DocumentProfile(
            document_id=str(payload.get("document_id", "")),
            extension=str(payload.get("extension", "") or ""),
            mime_type=str(payload.get("mime_type", "") or ""),
            file_size_bytes=int(payload.get("file_size_bytes", 0) or 0),
            page_count=_int_or_none(payload.get("page_count")),
            text_extractable_ratio=_float_or_none(
                payload.get("text_extractable_ratio"),
            ),
            has_images=_bool_or_none(payload.get("has_images")),
            has_tables=_bool_or_none(payload.get("has_tables")),
            has_scanned_pages=_bool_or_none(payload.get("has_scanned_pages")),
            estimated_tokens=_int_or_none(payload.get("estimated_tokens")),
            language=payload.get("language"),
            parser_confidence=_float_or_none(payload.get("parser_confidence")),
            warnings=tuple(payload.get("warnings") or ()),
            parse_quality_score=_float_or_none(
                payload.get("parse_quality_score"),
            ),
            text_sufficiency_score=_float_or_none(
                payload.get("text_sufficiency_score"),
            ),
            layout_complexity_score=_float_or_none(
                payload.get("layout_complexity_score"),
            ),
        )
    except (TypeError, ValueError):
        return None


def _resolve_llm_client(registry: LLMProviderRegistry, *, profile: str):
    """Map the user-facing model_profile string to an LLM client.

    `fast_planner` (default) → fast role with text fallback.
    `premium_planner` → premium role with text fallback.
    `text` → text role.
    Anything else → fast → text fallback."""
    p = (profile or "").strip().lower()
    if p in {"premium", "premium_planner"}:
        return registry.try_premium_or_text()
    if p == "text":
        return registry.try_text()
    # Default: fast → text fallback.
    fast = registry.try_fast()
    if fast is not None:
        return fast
    return registry.try_text()


def _int_or_none(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool_or_none(value):
    if value is None:
        return None
    return bool(value)


def _accepts_tz(clock) -> bool:
    """`datetime.now` accepts a tz argument; some test fixtures wrap
    it as `lambda: datetime(...)` with no args. We tolerate both."""
    try:
        clock(timezone.utc)
        return True
    except TypeError:
        return False
