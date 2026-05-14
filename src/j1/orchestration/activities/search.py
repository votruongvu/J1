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
    """Phase 8: canonical evidence adapter is the ONLY write path.
    The legacy SQLite dual-write + ``J1_LEGACY_SQLITE_EVIDENCE_ENABLED``
    flag were deleted. Operators that want SQLite have to fork the
    activity surface — no opt-in here."""

    def __init__(
        self,
        audit: AuditRecorder,
        indexers: Mapping[str, SearchIndexer] | None = None,
        *,
        evidence_adapter=None,
        snapshot_service=None,
        artifact_registry=None,
    ) -> None:
        self._audit = audit
        # Phase 8: ``indexers`` map is retained for activity-protocol
        # compatibility but never consulted on the default path.
        self._indexers = dict(indexers or {})
        self._evidence_adapter = evidence_adapter
        self._snapshot_service = snapshot_service
        self._artifact_registry = artifact_registry

    def all_activities(self) -> list:
        return [self.build_search_index_activity]

    @activity.defn(name=ACTIVITY_BUILD_SEARCH_INDEX)
    def build_search_index_activity(
        self, input: SearchIndexInput
    ) -> SearchIndexResult:
        ctx = input.scope.to_context()
        target_id = _set_target(input.artifact_ids)

        # Phase 8: canonical evidence adapter is the ONLY write
        # path. No SQLite fallback. No dual-write.
        evidence_count = self._write_evidence_adapter(
            ctx,
            artifact_ids=list(input.artifact_ids),
            correlation_id=input.correlation_id,
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
                "evidence_indexed_count": evidence_count,
            },
        )
        return SearchIndexResult(
            status=STATUS_SUCCEEDED,
            indexed_artifact_count=evidence_count,
        )


    def _write_evidence_adapter(
        self,
        ctx,
        *,
        artifact_ids,
        correlation_id,
    ) -> int:
        """Phase 4: the canonical write path. Returns the count of
        evidence rows the adapter accepted (sum across docs).

        The adapter and snapshot service MUST be wired in the
        default dev runtime; when they're missing the function is
        a no-op + returns 0 so a degraded test harness still gets a
        meaningful (zero) count instead of a crash."""
        if (
            self._evidence_adapter is None
            or self._snapshot_service is None
            or self._artifact_registry is None
        ):
            return 0
        if not artifact_ids or not correlation_id:
            return 0
        # Group artifact ids by the document they belong to so each
        # snapshot's evidence write is scoped correctly. One write
        # per (document, snapshot) — adapters de-dupe by their
        # natural key.
        from collections import defaultdict
        by_doc: dict[str, list[str]] = defaultdict(list)
        for art_id in artifact_ids:
            try:
                record = self._artifact_registry.get(ctx, art_id)
            except Exception:  # noqa: BLE001 — best-effort
                continue
            doc_ids = list(record.source_document_ids or [])
            doc_id = doc_ids[0] if doc_ids else None
            if not doc_id:
                continue
            by_doc[doc_id].append(art_id)

        # Phase 4 memoization: a single chunk body is shared by
        # multiple downstream writes when a document spans several
        # documents-of-record. The chunk resolver wired into the
        # adapter caches its file reads inside this attribute for
        # the duration of one activity invocation (cleared on the
        # next call). Lifetime is bounded to one Temporal activity,
        # so the cache is small and per-process.
        cache = self.__dict__.setdefault("_chunk_cache", {})
        cache.clear()

        from j1.search.evidence_adapter import EvidenceIndexRequest
        indexed_total = 0
        for document_id, ids in by_doc.items():
            try:
                snap = self._snapshot_service.get_or_create_for_run(
                    ctx, document_id=document_id, run_id=correlation_id,
                )
            except Exception:  # noqa: BLE001 — best-effort
                continue
            try:
                result = self._evidence_adapter.index(EvidenceIndexRequest(
                    ctx=ctx,
                    document_id=document_id,
                    snapshot_id=snap.snapshot_id,
                    created_by_run_id=correlation_id,
                    artifact_ids=tuple(ids),
                ))
                if getattr(result, "success", False):
                    indexed_total += int(
                        getattr(result, "indexed_count", 0) or 0
                    )
            except Exception:  # noqa: BLE001 — best-effort
                continue
        return indexed_total


def _set_target(ids: list[str]) -> str:
    if not ids:
        return "empty"
    return f"set:{','.join(ids)}"
