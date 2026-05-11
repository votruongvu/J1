import json
from datetime import datetime, timezone

import pytest
from temporalio.exceptions import ApplicationError

from j1.artifacts.models import ArtifactRecord
from j1.audit.sink import AUDIT_LOG_FILENAME
from j1.documents.models import DocumentRecord
from j1.enrichers import ARTIFACT_TYPE_CONSISTENCY_FINDINGS
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.orchestration.activities.payloads import (
    ApplyReviewDecisionInput,
    ProjectScope,
)
from j1.orchestration.activities.review import (
    ACTION_REVIEW_DECISION,
    ACTIVITY_APPLY_REVIEW_DECISION,
)
from j1.profiles import DEFAULT_PROFILE_ID, Profile, ProfileLoader
from j1.query.models import QueryRequest, QueryResponse
from j1.query.providers import (
    ConsistencyProvider,
    EvidenceProvider,
    KnowledgeQueryProvider,
    ReportGenerator,
)
from j1.review.governance import (
    CONFIDENCE_HIGH_THRESHOLD,
    CONFIDENCE_LOW_THRESHOLD,
    CONFIDENCE_MEDIUM_THRESHOLD,
    ConfidenceLevel,
    WarningCategory,
    confidence_level_from_score,
)
from j1.review.models import ReviewDecision, ReviewItem
from j1.search.indexer import SqliteSearchIndexer
from j1.workspace.layout import WorkspaceArea


# ---- ConfidenceLevel --------------------------------------------------


def test_confidence_levels_have_four_values():
    assert {c.value for c in ConfidenceLevel} == {
        "high",
        "medium",
        "low",
        "ambiguous",
    }


@pytest.mark.parametrize(
    "score,expected",
    [
        (1.0, ConfidenceLevel.HIGH),
        (0.85, ConfidenceLevel.HIGH),
        (CONFIDENCE_HIGH_THRESHOLD, ConfidenceLevel.HIGH),
        (0.79, ConfidenceLevel.MEDIUM),
        (CONFIDENCE_MEDIUM_THRESHOLD, ConfidenceLevel.MEDIUM),
        (0.49, ConfidenceLevel.LOW),
        (CONFIDENCE_LOW_THRESHOLD, ConfidenceLevel.LOW),
        (0.19, ConfidenceLevel.AMBIGUOUS),
        (0.0, ConfidenceLevel.AMBIGUOUS),
    ],
)
def test_confidence_level_from_score(score, expected):
    assert confidence_level_from_score(score) is expected


# ---- WarningCategory --------------------------------------------------


def test_warning_categories_have_five_values():
    assert {c.value for c in WarningCategory} == {
        "informational",
        "review_required",
        "high_risk",
        "source_verification_required",
        "not_for_final_decision",
    }


# ---- ReviewDecision ---------------------------------------------------


def test_review_decision_constructible():
    decision = ReviewDecision(
        review_item_id="r-1",
        decision=ReviewStatus.APPROVED,
        actor="reviewer@example.com",
        decided_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        notes="looks good",
    )
    assert decision.decision is ReviewStatus.APPROVED
    assert decision.actor == "reviewer@example.com"


def test_review_decision_is_frozen():
    decision = ReviewDecision(
        review_item_id="r-1",
        decision=ReviewStatus.APPROVED,
        actor="r",
        decided_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(Exception):
        decision.decision = ReviewStatus.REJECTED  # type: ignore[misc]


# ---- QueryResponse confidence_level + warning_categories --------------


def test_query_response_confidence_level_property():
    high = QueryResponse(answer="", mode_used="x", confidence=0.9)
    low = QueryResponse(answer="", mode_used="x", confidence=0.3)
    ambiguous = QueryResponse(answer="", mode_used="x", confidence=0.0)
    assert high.confidence_level is ConfidenceLevel.HIGH
    assert low.confidence_level is ConfidenceLevel.LOW
    assert ambiguous.confidence_level is ConfidenceLevel.AMBIGUOUS


def test_query_response_warning_categories_default_empty():
    r = QueryResponse(answer="", mode_used="x")
    assert r.warning_categories == []


# ---- Provider warning categories --------------------------------------


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _stage_artifact(
    workspace,
    ctx,
    artifact_registry,
    *,
    artifact_id: str,
    kind: str = "compiled.text",
    content: bytes = b"",
    area: WorkspaceArea = WorkspaceArea.COMPILED,
    suffix: str = ".txt",
    review_status: ReviewStatus = ReviewStatus.NOT_REQUIRED,
    source_document_ids: list[str] | None = None,
    metadata_extras: dict | None = None,
) -> ArtifactRecord:
    area_dir = workspace.area(ctx, area)
    area_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{artifact_id}{suffix}"
    (area_dir / stored).write_bytes(content)
    record = ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=f"{area.value}/{stored}",
        content_hash=f"sha256:{artifact_id}",
        byte_size=len(content),
        status=ProcessingStatus.SUCCEEDED,
        review_status=review_status,
        version=1,
        created_at=_now(),
        updated_at=_now(),
        source_document_ids=source_document_ids or [],
        metadata=metadata_extras or {},
    )
    artifact_registry.add(record)
    return record


def _stage_document(ctx, registry, document_id="doc-1") -> DocumentRecord:
    record = DocumentRecord(
        document_id=document_id,
        project=ctx,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.PENDING,
        created_at=_now(),
    )
    registry.add(record)
    return record


@pytest.fixture
def search_indexer(workspace, artifact_registry, registry):
    return SqliteSearchIndexer(workspace, artifact_registry, registry)


@pytest.fixture
def default_profile() -> Profile:
    return ProfileLoader().load(DEFAULT_PROFILE_ID)


def test_knowledge_provider_emits_review_required_category(
    workspace, ctx, artifact_registry, search_indexer
):
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"keyword content here",
        review_status=ReviewStatus.PENDING,
    )
    search_indexer.index(ctx, ["a-1"])
    provider = KnowledgeQueryProvider(search_indexer)
    response = provider.query(ctx, QueryRequest(question="keyword"))
    assert WarningCategory.REVIEW_REQUIRED in response.warning_categories


def test_knowledge_provider_no_categories_when_artifacts_clean(
    workspace, ctx, artifact_registry, search_indexer
):
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"clean text",
    )
    search_indexer.index(ctx, ["a-1"])
    provider = KnowledgeQueryProvider(search_indexer)
    response = provider.query(ctx, QueryRequest(question="clean"))
    assert response.warning_categories == []


def test_evidence_provider_emits_source_verification_required(
    workspace, ctx, artifact_registry, registry, search_indexer
):
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"orphan section",
    )
    search_indexer.index(ctx, ["a-1"])
    provider = EvidenceProvider(search_indexer, registry)
    response = provider.query(ctx, QueryRequest(question="orphan"))
    assert WarningCategory.SOURCE_VERIFICATION_REQUIRED in response.warning_categories


def test_consistency_provider_always_emits_review_required(
    workspace, ctx, artifact_registry
):
    """Consistency findings always carry review_required category per spec."""
    provider = ConsistencyProvider(artifact_registry, workspace)
    response = provider.query(ctx, QueryRequest(question="conflicts"))
    assert response.review_required is True
    assert WarningCategory.REVIEW_REQUIRED in response.warning_categories


def test_consistency_provider_emits_review_required_for_each_finding(
    workspace, ctx, artifact_registry
):
    payload = json.dumps({
        "findings": [
            {"description": "section X mismatches Y"},
            {"description": "field Z is contradictory"},
        ]
    }).encode("utf-8")
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="c-1",
        kind=ARTIFACT_TYPE_CONSISTENCY_FINDINGS,
        content=payload,
        area=WorkspaceArea.ENRICHED,
        suffix=".json",
        review_status=ReviewStatus.PENDING,
        metadata_extras={"format": "json"},
    )
    provider = ConsistencyProvider(artifact_registry, workspace)
    response = provider.query(ctx, QueryRequest(question="any conflicts?"))
    assert all(
        c is WarningCategory.REVIEW_REQUIRED for c in response.warning_categories
    )
    assert len(response.warning_categories) == len(response.warnings)


def test_report_generator_emits_review_required_when_artifact_pending(
    workspace, ctx, artifact_registry, search_indexer, default_profile
):
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"x", review_status=ReviewStatus.PENDING,
    )
    search_indexer.index(ctx, ["a-1"])
    gen = ReportGenerator(search_indexer, default_profile)
    response = gen.query(ctx, QueryRequest(question="status report"))
    assert WarningCategory.REVIEW_REQUIRED in response.warning_categories
    assert WarningCategory.NOT_FOR_FINAL_DECISION in response.warning_categories


def test_report_generator_emits_informational_when_no_template(
    workspace, ctx, artifact_registry, search_indexer
):
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"x",
    )
    search_indexer.index(ctx, ["a-1"])
    profile = Profile(profile_id="empty", metadata={})
    gen = ReportGenerator(search_indexer, profile)
    response = gen.query(ctx, QueryRequest(question="anything"))
    assert WarningCategory.INFORMATIONAL in response.warning_categories


# ---- apply_review_decision_activity -----------------------------------


def test_apply_decision_activity_has_temporal_marker(review_activities):
    name = (
        review_activities.apply_review_decision_activity
        .__temporal_activity_definition.name
    )
    assert name == ACTIVITY_APPLY_REVIEW_DECISION


def test_apply_decision_records_to_queue(
    review_activities, review_queue, ctx
):
    item = ReviewItem(
        review_item_id="r-1",
        project=ctx,
        target_kind="artifact",
        target_id="art-1",
        review_status=ReviewStatus.PENDING,
        requested_at=_now(),
    )
    review_queue.add(item)

    review_activities.apply_review_decision_activity(
        ApplyReviewDecisionInput(
            scope=ProjectScope.from_context(ctx),
            review_item_id="r-1",
            decision=ReviewStatus.APPROVED.value,
            actor="reviewer",
            notes="ok",
        )
    )
    updated = review_queue.get(ctx, "r-1")
    assert updated.review_status is ReviewStatus.APPROVED
    assert updated.actor == "reviewer"
    assert updated.notes == "ok"


def test_apply_decision_writes_audit(
    review_activities, review_queue, workspace, ctx
):
    review_queue.add(
        ReviewItem(
            review_item_id="r-1",
            project=ctx,
            target_kind="artifact",
            target_id="art-1",
            review_status=ReviewStatus.PENDING,
            requested_at=_now(),
        )
    )
    result = review_activities.apply_review_decision_activity(
        ApplyReviewDecisionInput(
            scope=ProjectScope.from_context(ctx),
            review_item_id="r-1",
            decision=ReviewStatus.REJECTED.value,
            actor="reviewer",
            notes="needs more sources",
            correlation_id="run-7",
        )
    )
    assert result.review_status == "rejected"
    assert result.audit_event_id

    events = [
        json.loads(line)
        for line in (workspace.audit(ctx) / AUDIT_LOG_FILENAME).read_text().splitlines()
        if line.strip()
    ]
    decision_events = [e for e in events if e["action"] == ACTION_REVIEW_DECISION]
    assert len(decision_events) == 1
    payload = decision_events[0]["payload"]
    assert payload["decision"] == "rejected"
    assert payload["notes"] == "needs more sources"
    assert decision_events[0]["target_id"] == "r-1"
    assert decision_events[0]["correlation_id"] == "run-7"


def test_apply_decision_invalid_status_raises_non_retryable(
    review_activities, review_queue, ctx
):
    review_queue.add(
        ReviewItem(
            review_item_id="r-1",
            project=ctx,
            target_kind="artifact",
            target_id="art-1",
            review_status=ReviewStatus.PENDING,
            requested_at=_now(),
        )
    )
    with pytest.raises(ApplicationError) as exc:
        review_activities.apply_review_decision_activity(
            ApplyReviewDecisionInput(
                scope=ProjectScope.from_context(ctx),
                review_item_id="r-1",
                decision="not-a-real-decision",
                actor="reviewer",
            )
        )
    assert exc.value.non_retryable is True


def test_apply_decision_supports_changes_requested(
    review_activities, review_queue, ctx
):
    review_queue.add(
        ReviewItem(
            review_item_id="r-1",
            project=ctx,
            target_kind="artifact",
            target_id="art-1",
            review_status=ReviewStatus.PENDING,
            requested_at=_now(),
        )
    )
    review_activities.apply_review_decision_activity(
        ApplyReviewDecisionInput(
            scope=ProjectScope.from_context(ctx),
            review_item_id="r-1",
            decision=ReviewStatus.CHANGES_REQUESTED.value,
            actor="reviewer",
        )
    )
    assert (
        review_queue.get(ctx, "r-1").review_status
        is ReviewStatus.CHANGES_REQUESTED
    )


# ---- Workflow review state transitions (existing surface, sanity check)


def test_workflow_review_state_transitions_remain_supported():
    """Workflow signals + state already exercised elsewhere; this test
 sanity-checks the public surface that governance flows depend on."""
    from j1.orchestration.workflows.project_processing import (
        GATE_AFTER_COMPILE,
        ProjectProcessingWorkflow,
        WorkflowState,
    )

    wf = ProjectProcessingWorkflow()
    # The workflow exposes the messages the spec lists.
    assert hasattr(wf, "approve_review")
    assert hasattr(wf, "reject_review")
    # The state machine has the WAITING_FOR_REVIEW state.
    assert WorkflowState.WAITING_FOR_REVIEW.value == "waiting_for_review"
    # Gate constants exist.
    assert GATE_AFTER_COMPILE
