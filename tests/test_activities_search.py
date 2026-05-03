import json

import pytest
from temporalio.exceptions import ApplicationError

from j1.audit.sink import AUDIT_LOG_FILENAME
from j1.orchestration.activities.payloads import ProjectScope, SearchIndexInput
from j1.orchestration.activities.search import (
    ACTIVITY_BUILD_SEARCH_INDEX,
    SearchActivities,
)
from j1.processing.results import ProcessingResult, ResultStatus


class _Indexer:
    kind = "mock.index"

    def __init__(self, *, raise_exc=None, status=ResultStatus.SUCCEEDED):
        self._exc = raise_exc
        self._status = status

    def index(self, ctx, artifact_ids):
        if self._exc:
            raise self._exc
        return ProcessingResult(status=self._status)


@pytest.fixture
def search_activities(audit_recorder):
    return SearchActivities(
        audit=audit_recorder, indexers={"mock.index": _Indexer()}
    )


def _read_audit(workspace, ctx):
    path = workspace.audit(ctx) / AUDIT_LOG_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_activity_name(search_activities):
    name = search_activities.build_search_index_activity.__temporal_activity_definition.name
    assert name == ACTIVITY_BUILD_SEARCH_INDEX


def test_build_search_index_succeeds(search_activities, ctx, workspace):
    result = search_activities.build_search_index_activity(
        SearchIndexInput(
            scope=ProjectScope.from_context(ctx),
            artifact_ids=["a", "b"],
            processor_kind="mock.index",
        )
    )
    assert result.status == "succeeded"
    assert result.indexed_artifact_count == 2
    events = _read_audit(workspace, ctx)
    assert events[0]["action"] == "j1.search.index.completed"


def test_build_search_index_unknown_kind_raises_non_retryable(audit_recorder, ctx):
    activities = SearchActivities(audit=audit_recorder, indexers={})
    with pytest.raises(ApplicationError) as exc:
        activities.build_search_index_activity(
            SearchIndexInput(
                scope=ProjectScope.from_context(ctx),
                artifact_ids=["a"],
                processor_kind="missing",
            )
        )
    assert exc.value.non_retryable is True


def test_build_search_index_failure_captured(audit_recorder, ctx, workspace):
    activities = SearchActivities(
        audit=audit_recorder,
        indexers={"mock.index": _Indexer(raise_exc=RuntimeError("boom"))},
    )
    result = activities.build_search_index_activity(
        SearchIndexInput(
            scope=ProjectScope.from_context(ctx),
            artifact_ids=["a"],
            processor_kind="mock.index",
        )
    )
    assert result.status == "failed"
    assert result.error == "boom"
    events = _read_audit(workspace, ctx)
    assert events[0]["action"] == "j1.search.index.failed"
