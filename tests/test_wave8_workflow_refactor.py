"""Wave 8 tests — workflow refactor.

Pins the four contract surfaces this wave delivers:

1. Final-status projection (`project_final_status`) — mapping
   framework status + structured signals onto the Wave-8
   operator-facing vocabulary.
2. Workflow's `_wave8_enrichment_outcome` helper — projects the
   activity result onto the fine-grained outcome label.
3. `resolve_require_enrichment_success` is consulted by the
   active enrichment-stage activity, not the raw plan field.
4. Idempotency: the activity short-circuits when an
   `enrichment_result` artifact already exists for the same
   (run, doc) pair.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.domains.models import DomainEnrichmentPolicy
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.orchestration.activities.payloads import (
    ProjectScope,
    RunEnrichmentStageInput,
)
from j1.orchestration.activities.processing import (
    _find_existing_enrichment_result,
)
from j1.orchestration.workflows.project_processing import (
    _project_search_attr_final_status,
    _wave8_enrichment_outcome,
)
from j1.processing.enrichment_policy import (
    REQUIRE_SUCCESS_SOURCE_DOMAIN,
    REQUIRE_SUCCESS_SOURCE_ENV,
    REQUIRE_SUCCESS_SOURCE_SYSTEM_DEFAULT,
    resolve_require_enrichment_success,
)
from j1.processing.final_status import (
    ALL_INGESTION_FINAL_STATUSES,
    INGESTION_STATUS_CANCELLED,
    INGESTION_STATUS_COMPLETED_WITHOUT_ENRICHMENT,
    INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT,
    INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS,
    INGESTION_STATUS_FAILED_COMPILE,
    INGESTION_STATUS_FAILED_ENRICHMENT_REQUIRED,
    INGESTION_STATUS_FAILED_FINALIZATION,
    INGESTION_STATUS_FAILED_UNKNOWN,
    IngestionFinalStatusProjection,
    project_final_status,
)
from j1.processing.results import ARTIFACT_KIND_ENRICHMENT_RESULT
from j1.processing.status import StepStatus, StepSource
from j1.processing.step_result import StepResult


# ---- 1. Final-status projection ------------------------------------


def test_projection_returns_completed_with_enrichment_on_succeeded_enrichment():
    p = project_final_status(
        framework_final_status="completed",
        enrichment_status="succeeded",
    )
    assert p.status == INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT


def test_projection_returns_completed_without_enrichment_on_skipped():
    p = project_final_status(
        framework_final_status="completed",
        enrichment_status="skipped",
        enrichment_skipped_reason="domain policy=never",
    )
    assert p.status == INGESTION_STATUS_COMPLETED_WITHOUT_ENRICHMENT
    assert "domain policy" in p.reason


def test_projection_returns_warnings_on_partial_completed():
    p = project_final_status(
        framework_final_status="partial_completed",
        enrichment_status="succeeded_with_warnings",
    )
    assert p.status == INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS


def test_projection_returns_warnings_on_optional_enrichment_failure():
    p = project_final_status(
        framework_final_status="partial_completed",
        enrichment_status="failed",
        enrichment_required=False,
    )
    assert p.status == INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS


def test_projection_returns_failed_enrichment_required():
    p = project_final_status(
        framework_final_status="failed",
        failure_code="ENRICHMENT_REQUIRED",
        enrichment_required=True,
    )
    assert p.status == INGESTION_STATUS_FAILED_ENRICHMENT_REQUIRED


def test_projection_returns_failed_compile_for_compile_failure_codes():
    for code in (
        "COMPILE_FAILED", "CHUNK_FAILED", "INDEX_FAILED",
        "VERIFICATION_FAILED", "EMPTY_DOCUMENT",
    ):
        p = project_final_status(
            framework_final_status="failed",
            failure_code=code,
        )
        assert p.status == INGESTION_STATUS_FAILED_COMPILE, (
            f"failure_code {code} should map to failed_compile"
        )


def test_projection_returns_failed_finalization_for_dedicated_code():
    p = project_final_status(
        framework_final_status="failed",
        failure_code="FINALIZATION_FAILED",
    )
    assert p.status == INGESTION_STATUS_FAILED_FINALIZATION


def test_projection_returns_failed_unknown_when_no_code():
    p = project_final_status(framework_final_status="failed")
    assert p.status == INGESTION_STATUS_FAILED_UNKNOWN


def test_projection_returns_cancelled():
    p = project_final_status(framework_final_status="cancelled")
    assert p.status == INGESTION_STATUS_CANCELLED


def test_projection_legacy_completed_no_enrichment_signals():
    """Completed framework status + no enrichment signals →
    completed_without_enrichment (legacy/pre-Wave-6 run)."""
    p = project_final_status(framework_final_status="completed")
    assert p.status == INGESTION_STATUS_COMPLETED_WITHOUT_ENRICHMENT


def test_all_projected_values_are_in_vocabulary():
    """Every projection path produces a value in the documented
    `ALL_INGESTION_FINAL_STATUSES` tuple."""
    paths = [
        ("completed", None, "succeeded", False, None),
        ("completed", None, "skipped", False, "x"),
        ("partial_completed", None, "succeeded_with_warnings", False, None),
        ("partial_completed", None, "failed", False, None),
        ("failed", "ENRICHMENT_REQUIRED", None, True, None),
        ("failed", "COMPILE_FAILED", None, False, None),
        ("failed", None, None, False, None),
        ("cancelled", None, None, False, None),
        ("completed", None, None, False, None),
    ]
    for status, code, enrich, required, reason in paths:
        p = project_final_status(
            framework_final_status=status,
            failure_code=code,
            enrichment_status=enrich,
            enrichment_required=required,
            enrichment_skipped_reason=reason,
        )
        assert p.status in ALL_INGESTION_FINAL_STATUSES, (
            f"{(status, code, enrich, required)} produced "
            f"out-of-vocabulary {p.status!r}"
        )


# ---- 2. _wave8_enrichment_outcome ----------------------------------


@pytest.mark.parametrize(
    ("activity_status", "require_success", "expected_outcome"),
    [
        ("succeeded", False, "completed"),
        ("succeeded", True, "completed"),
        ("succeeded_with_warnings", False, "completed_with_warnings"),
        ("succeeded_with_warnings", True, "completed_with_warnings"),
        ("failed", False, "failed_optional"),
        ("failed", True, "failed_required"),
        ("skipped", False, "skipped"),
        ("skipped", True, "skipped"),
    ],
)
def test_wave8_outcome_projection_pinned(
    activity_status, require_success, expected_outcome,
):
    """Activity status + require_success → fine-grained Wave-8
    outcome label. Drives final-status search attr + step metadata."""
    assert _wave8_enrichment_outcome(
        enrichment_status=activity_status,
        require_success=require_success,
    ) == expected_outcome


# ---- 3. require_enrichment_success precedence at runtime -----------


def test_resolver_picks_env_default_when_domain_has_no_opinion():
    """The activity reads `EnrichmentConcurrencySettings.require_
    enrichment_success` as env_default. When the domain pack has
    no opinion (default policy=auto + require=False), the env
    value wins."""
    r = resolve_require_enrichment_success(
        domain_policy=DomainEnrichmentPolicy(),
        env_default=True,
    )
    assert r.require_enrichment_success is True
    assert r.source == REQUIRE_SUCCESS_SOURCE_ENV


def test_resolver_domain_opinion_beats_env_default():
    """A pack with require_success=True wins over env=False."""
    r = resolve_require_enrichment_success(
        domain_policy=DomainEnrichmentPolicy(require_enrichment_success=True),
        env_default=False,
    )
    assert r.require_enrichment_success is True
    assert r.source == REQUIRE_SUCCESS_SOURCE_DOMAIN


def test_resolver_falls_through_to_system_default_when_no_input():
    r = resolve_require_enrichment_success()
    assert r.require_enrichment_success is False
    assert r.source == REQUIRE_SUCCESS_SOURCE_SYSTEM_DEFAULT


# ---- 4. Search-attribute projector --------------------------------


def _enrich_step(metadata: dict) -> StepResult:
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    return StepResult(
        step="enrich_stage",
        status=StepStatus.COMPLETED,
        required=False,
        source=StepSource.CALLER,
        started_at=now,
        completed_at=now,
        metadata=metadata,
    )


def test_search_attr_projector_reads_enrichment_outcome_metadata():
    """The `J1FinalStatus` search-attribute value comes from the
    most-recent `enrich_stage` step's `enrichment_outcome` metadata."""
    enrich = _enrich_step({
        "document_id": "d",
        "enrichment_outcome": "completed",
    })
    status = _project_search_attr_final_status(
        framework_final_status="completed",
        step_results=[enrich],
    )
    assert status == INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT


def test_search_attr_projector_handles_skipped_outcome():
    enrich = _enrich_step({
        "document_id": "d",
        "enrichment_outcome": "skipped",
        "enrichment_skipped_reason": "compile failed",
    })
    status = _project_search_attr_final_status(
        framework_final_status="completed",
        step_results=[enrich],
    )
    assert status == INGESTION_STATUS_COMPLETED_WITHOUT_ENRICHMENT


def test_search_attr_projector_routes_failed_required_to_required_status():
    enrich = _enrich_step({
        "document_id": "d",
        "enrichment_outcome": "failed_required",
        "failure_code": "ENRICHMENT_REQUIRED",
    })
    status = _project_search_attr_final_status(
        framework_final_status="failed",
        step_results=[enrich],
        failure_code="ENRICHMENT_REQUIRED",
    )
    assert status == INGESTION_STATUS_FAILED_ENRICHMENT_REQUIRED


def test_search_attr_projector_handles_partial_completed_warnings():
    enrich = _enrich_step({
        "document_id": "d",
        "enrichment_outcome": "failed_optional",
    })
    status = _project_search_attr_final_status(
        framework_final_status="partial_completed",
        step_results=[enrich],
    )
    assert status == INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS


def test_search_attr_projector_no_enrichment_steps_falls_back():
    """Runs without any enrich_stage step still get a sensible
    projection — completed_without_enrichment."""
    status = _project_search_attr_final_status(
        framework_final_status="completed",
        step_results=[],
    )
    assert status == INGESTION_STATUS_COMPLETED_WITHOUT_ENRICHMENT


# ---- 5. Idempotency: enrichment_result lookup --------------------


class _FakeArtifactRegistry:
    """Minimal in-memory artifact registry honouring the
    `list_artifacts(kind=...)` slice of the protocol."""

    def __init__(self, records: list[ArtifactRecord]) -> None:
        self._records = records

    def list_artifacts(self, ctx, *, kind: str | None = None):
        return [
            r for r in self._records
            if kind is None or r.kind == kind
        ]


def _make_enrichment_artifact(
    *,
    run_id: str,
    artifact_id: str = "art-1",
    status: str = "succeeded",
    updated_at: datetime | None = None,
) -> ArtifactRecord:
    ts = updated_at or datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    from j1.projects.context import ProjectContext
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ProjectContext(tenant_id="acme", project_id="alpha"),
        kind=ARTIFACT_KIND_ENRICHMENT_RESULT,
        location=f"enriched/enrichment_result_{run_id}.json",
        content_hash=f"hash-{artifact_id}",
        byte_size=100,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=ts,
        updated_at=ts,
        source_document_ids=["doc-1"],
        source_artifact_ids=[],
        metadata={
            "run_id": run_id,
            "status": status,
            "domain_id": "civil_engineering",
        },
    )


def test_idempotency_returns_none_on_miss():
    """No matching artifact → return None so activity runs the
    stage normally."""
    registry = _FakeArtifactRegistry([])
    ctx = object()
    result = _find_existing_enrichment_result(
        registry, ctx, run_id="run-1", document_id="doc-1",
    )
    assert result is None


def test_idempotency_finds_existing_artifact_for_run():
    """An artifact with matching `metadata.run_id` triggers a
    short-circuit return."""
    artifact = _make_enrichment_artifact(run_id="run-1")
    registry = _FakeArtifactRegistry([artifact])
    ctx = object()
    result = _find_existing_enrichment_result(
        registry, ctx, run_id="run-1", document_id="doc-1",
    )
    assert result is not None
    assert result["_artifact_id"] == "art-1"
    assert result["_cache_hit"] is True
    assert result["status"] == "succeeded"


def test_idempotency_picks_latest_when_multiple_matches():
    """Replay can produce two enrichment_result artifacts for the
    same run. Most recent wins."""
    old = _make_enrichment_artifact(
        run_id="run-1", artifact_id="art-old",
        updated_at=datetime(2026, 5, 10, 11, 0, 0, tzinfo=timezone.utc),
    )
    new = _make_enrichment_artifact(
        run_id="run-1", artifact_id="art-new",
        updated_at=datetime(2026, 5, 11, 14, 0, 0, tzinfo=timezone.utc),
    )
    registry = _FakeArtifactRegistry([old, new])
    result = _find_existing_enrichment_result(
        registry, object(), run_id="run-1", document_id="doc-1",
    )
    assert result["_artifact_id"] == "art-new"


def test_idempotency_ignores_other_runs_artifacts():
    """Artifacts from a different run_id must NOT trigger the
    short-circuit — each run gets its own enrichment."""
    other = _make_enrichment_artifact(run_id="other-run")
    registry = _FakeArtifactRegistry([other])
    result = _find_existing_enrichment_result(
        registry, object(), run_id="run-1", document_id="doc-1",
    )
    assert result is None


def test_idempotency_empty_run_id_skips_lookup():
    """Defensive: an empty run_id (test fixture / legacy path)
    short-circuits the helper without crashing."""
    registry = _FakeArtifactRegistry([
        _make_enrichment_artifact(run_id="run-1"),
    ])
    result = _find_existing_enrichment_result(
        registry, object(), run_id="", document_id="doc-1",
    )
    assert result is None


def test_idempotency_registry_failure_falls_through_safely():
    """If `list_artifacts` raises, the helper returns None so the
    activity re-runs rather than crashing."""

    class _BrokenRegistry:
        def list_artifacts(self, ctx, *, kind=None):
            raise RuntimeError("registry down")

    result = _find_existing_enrichment_result(
        _BrokenRegistry(), object(), run_id="run-1", document_id="d",
    )
    assert result is None


# ---- 6. Legacy regression --------------------------------------


def test_wave8_outcome_module_has_no_split_mode_strings():
    """Final-status module must not reintroduce split-mode
    vocabulary."""
    import inspect
    from j1.processing import final_status
    src = inspect.getsource(final_status)
    for forbidden in ("split_mode", "SplitMode", "insert_content"):
        assert forbidden not in src


def test_search_attr_constants_do_not_include_legacy_gating():
    """The new Wave-8 search attrs must not encode pre-compile
    gating vocabulary."""
    from j1.orchestration.workflows import project_processing as mod
    import inspect
    src = inspect.getsource(mod)
    # Check the search-attr constant block — these are operator-
    # facing strings. Ensure no legacy names appear in the new
    # constants.
    for name in (
        "J1_GraphRequired", "J1_IndexRequired", "J1_SplitMode",
        "J1PreCompileGating",
    ):
        assert name not in src
