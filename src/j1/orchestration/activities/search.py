from collections.abc import Mapping

from temporalio import activity
from temporalio.exceptions import ApplicationError

from j1.audit.recorder import AuditRecorder
from j1.orchestration.activities.payloads import (
    SearchIndexInput,
    SearchIndexResult,
)
from j1.processing.contracts import SearchIndexer

ACTIVITY_BUILD_SEARCH_INDEX = "j1.search.build_index"

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

ACTION_INDEX_COMPLETED = "j1.search.index.completed"
ACTION_INDEX_FAILED = "j1.search.index.failed"
TARGET_ARTIFACT_SET = "artifact_set"


class SearchActivities:
    def __init__(
        self,
        audit: AuditRecorder,
        indexers: Mapping[str, SearchIndexer] | None = None,
    ) -> None:
        self._audit = audit
        self._indexers = dict(indexers or {})

    def all_activities(self) -> list:
        return [self.build_search_index_activity]

    @activity.defn(name=ACTIVITY_BUILD_SEARCH_INDEX)
    def build_search_index_activity(
        self, input: SearchIndexInput
    ) -> SearchIndexResult:
        ctx = input.scope.to_context()
        indexer = self._indexers.get(input.processor_kind)
        if indexer is None:
            raise ApplicationError(
                f"unknown indexer kind: {input.processor_kind}",
                non_retryable=True,
            )
        target_id = _set_target(input.artifact_ids)
        try:
            result = indexer.index(ctx, list(input.artifact_ids))
        except Exception as exc:
            self._audit.record(
                ctx,
                actor=input.actor,
                action=ACTION_INDEX_FAILED,
                target_kind=TARGET_ARTIFACT_SET,
                target_id=target_id,
                correlation_id=input.correlation_id,
                payload={
                    "processor_kind": input.processor_kind,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return SearchIndexResult(
                status=STATUS_FAILED,
                error=str(exc),
                message=type(exc).__name__,
            )

        self._audit.record(
            ctx,
            actor=input.actor,
            action=ACTION_INDEX_COMPLETED,
            target_kind=TARGET_ARTIFACT_SET,
            target_id=target_id,
            correlation_id=input.correlation_id,
            payload={
                "processor_kind": input.processor_kind,
                "artifact_count": len(input.artifact_ids),
                "result_status": result.status.value,
            },
        )
        return SearchIndexResult(
            status=result.status.value,
            indexed_artifact_count=len(input.artifact_ids),
            error=result.error,
            message=result.message,
        )


def _set_target(ids: list[str]) -> str:
    if not ids:
        return "empty"
    return f"set:{','.join(ids)}"
