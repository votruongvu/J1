"""Processing-result cache: skip re-running expensive processors.

A small append-only JSONL store that lets the activity layer answer
"have we already produced this output for this input?" before
invoking a processor. The most acute use case today is the compile
stage — MinerU + raganything routinely take minutes per real
document, and Temporal activity retries (worker crash, redeploy,
heartbeat-timeout edge cases) shouldn't re-run that work if the
artifact already exists.

Keys are derived purely from the inputs that decide the output:
`document_hash`, `processor_kind`, `processor_version`, `mode`.
Any change in those produces a fresh cache row. The store itself
is processor-agnostic — there is nothing MinerU- or
raganything-specific in this module. Other processors (different
parsers, future enrichment caches, etc.) reuse the same key shape.

Storage is intentionally trivial: workspace-scoped JSONL, latest
snapshot wins on read, no locking. Latest-snapshot semantics let
two activity attempts that happened to race produce two rows
without corrupting the cache — the most-recent `completed` entry
is what subsequent lookups see. For deployments that outgrow JSONL,
swap the `ProcessingResultCache` Protocol implementation; nothing
in the framework depends on the file backing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from j1._serialization import to_jsonable
from j1.projects.context import ProjectContext
from j1.workspace.layout import WorkspaceArea
from j1.workspace.resolver import WorkspaceResolver

CACHE_FILENAME = "processing_results.jsonl"

CACHE_STATUS_PROCESSING = "processing"
CACHE_STATUS_COMPLETED = "completed"
CACHE_STATUS_FAILED = "failed"

__all__ = [
    "CACHE_FILENAME",
    "CACHE_STATUS_COMPLETED",
    "CACHE_STATUS_FAILED",
    "CACHE_STATUS_PROCESSING",
    "JsonlProcessingResultCache",
    "ProcessingCacheEntry",
    "ProcessingResultCache",
    "make_cache_key",
]


@dataclass
class ProcessingCacheEntry:
    """One entry in the processing-result cache.

    The fields after `status` are the operationally interesting
    metadata Temporal UI / operators want to see when investigating
    repeated processing. None of them are required to drive cache
    behaviour — the lookup uses `cache_key` alone — but they make
    the audit trail self-explaining without a separate join."""

    cache_key: str
    document_id: str
    document_hash: str
    processor_kind: str
    processor_version: str
    mode: str
    status: str
    artifact_ids: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    attempt: int = 1
    error_type: str | None = None
    error_message: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


def make_cache_key(
    *,
    document_hash: str,
    processor_kind: str,
    processor_version: str = "",
    mode: str = "",
) -> str:
    """Deterministic cache key from the inputs that decide the output.

    Stable hex digest so it survives serialisation / cross-language
    consumers without ambiguity. `processor_version` and `mode` are
    optional — empty values produce the same key, which is the right
    default when a processor has no notion of versioning yet."""
    digest = hashlib.sha256()
    for part in (document_hash, processor_kind, processor_version, mode):
        digest.update((part or "").encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


class ProcessingResultCache(Protocol):
    """Read/write surface for the processing-result cache."""

    def lookup(
        self,
        ctx: ProjectContext,
        *,
        document_hash: str,
        processor_kind: str,
        processor_version: str = "",
        mode: str = "",
    ) -> ProcessingCacheEntry | None: ...

    def upsert(self, ctx: ProjectContext, entry: ProcessingCacheEntry) -> None: ...


class JsonlProcessingResultCache:
    """JSONL-backed cache; latest snapshot per `cache_key` wins.

    Lives under the workspace's audit area so a single backup covers
    both the cache and the run records / progress events that
    reference its entries."""

    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    def _path(self, ctx: ProjectContext):
        return self._workspace.area(ctx, WorkspaceArea.AUDIT) / CACHE_FILENAME

    def lookup(
        self,
        ctx: ProjectContext,
        *,
        document_hash: str,
        processor_kind: str,
        processor_version: str = "",
        mode: str = "",
    ) -> ProcessingCacheEntry | None:
        key = make_cache_key(
            document_hash=document_hash,
            processor_kind=processor_kind,
            processor_version=processor_version,
            mode=mode,
        )
        path = self._path(ctx)
        if not path.exists():
            return None
        latest: ProcessingCacheEntry | None = None
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    # Tolerate malformed tail rows — best-effort cache.
                    continue
                if payload.get("cache_key") != key:
                    continue
                latest = _entry_from_payload(payload)
        return latest

    def upsert(self, ctx: ProjectContext, entry: ProcessingCacheEntry) -> None:
        path = self._path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(to_jsonable(entry), separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")


def _entry_from_payload(payload: dict) -> ProcessingCacheEntry:
    return ProcessingCacheEntry(
        cache_key=str(payload["cache_key"]),
        document_id=str(payload.get("document_id", "")),
        document_hash=str(payload.get("document_hash", "")),
        processor_kind=str(payload.get("processor_kind", "")),
        processor_version=str(payload.get("processor_version", "")),
        mode=str(payload.get("mode", "")),
        status=str(payload.get("status", "")),
        artifact_ids=tuple(payload.get("artifact_ids") or ()),
        created_at=_parse_dt(payload.get("created_at")),
        updated_at=_parse_dt(payload.get("updated_at")),
        attempt=int(payload.get("attempt") or 1),
        error_type=payload.get("error_type"),
        error_message=payload.get("error_message"),
        metadata=dict(payload.get("metadata") or {}),
    )


def _parse_dt(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return datetime.fromtimestamp(0, tz=timezone.utc)
    return datetime.fromtimestamp(0, tz=timezone.utc)
