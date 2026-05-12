import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Protocol

from j1._serialization import to_jsonable
from j1.artifacts.models import ArtifactRecord
from j1.errors.exceptions import J1Error
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext
from j1.workspace.resolver import WorkspaceResolver

_log = logging.getLogger("j1.artifacts.registry")

ARTIFACT_REGISTRY_FILENAME = "artifacts.json"
ARTIFACT_REGISTRY_VERSION = 1


# Artifact kinds for which ``metadata.run_id`` is REQUIRED at the
# registry layer. This is the last line of defense: even if a
# producer or a registration helper bypasses the higher-level
# lineage guards (``ProcessingService._register_draft``,
# ``KnowledgeProcessingActivities._materialize_draft``), the registry
# itself refuses to write the record. Defense in depth — operators
# kept hitting "graph_json with run_id=None" because the validation
# report flagged 7+ artifacts at a time, and each repair cycle
# uncovered new orphans created via paths the upstream guards
# missed.
#
# The list is intentionally tight: kinds in this set MUST be tied
# to a specific ingestion run for retrieval/validation to scope
# them correctly. Generic kinds (raw uploads, user attachments)
# legitimately have no run scope and are allowed to omit ``run_id``.
_REGISTRY_LINEAGE_REQUIRED_KINDS: frozenset[str] = frozenset({
    "graph_json",
})


class RegistryLineageError(J1Error):
    """Raised when a write would land a lineage-required artifact
    without ``metadata.run_id``. Subclass of ``J1Error`` so the
    legacy error-handling paths catch it without needing to know
    about lineage specifically."""


def _enforce_registry_lineage_or_raise(record: ArtifactRecord) -> None:
    """Last-line-of-defense lineage check. Refuses to add a
    ``graph_json`` (or other lineage-required-kind) artifact without
    a non-empty ``metadata.run_id``.

    Why at the registry layer (and not just at the producer / draft
    path): every other guard sits behind a specific entry point
    (orchestration ``_materialize_draft``, legacy
    ``ProcessingService._register_draft``). A future adapter that
    calls ``artifacts.add()`` directly — for replay, repair, batch
    import, in-process tests, anything — bypasses those guards. The
    validation reports kept surfacing new orphan IDs run after run;
    each upstream patch closed one path while another stayed open.
    The registry is the ONE place every write goes through, so
    enforcing here is hermetic.
    """
    if record.kind not in _REGISTRY_LINEAGE_REQUIRED_KINDS:
        return
    meta = record.metadata if isinstance(record.metadata, dict) else {}
    run_id = meta.get("run_id")
    if not run_id:
        raise RegistryLineageError(
            f"refusing to register artifact_id={record.artifact_id!r} of "
            f"kind={record.kind!r}: metadata.run_id is missing or empty. "
            "Lineage-required kinds must carry a non-empty run_id so "
            "retrieval and validation can scope correctly. The producer "
            "(_graph_drafts_from_storage) stamps it at the draft layer; "
            "upstream registration helpers (_register_draft, "
            "_materialize_draft) re-stamp from the workflow's "
            "correlation_id. If you reached this error, the caller "
            "bypassed both — pass run_id through the registration "
            "chain, or use the project-wide cleanup sweep to invalidate "
            "the existing orphan (POST /documents/{id}/repair)."
        )


class ArtifactNotFoundError(J1Error):
    pass


class ArtifactRegistry(Protocol):
    def add(self, record: ArtifactRecord) -> None: ...

    def get(self, ctx: ProjectContext, artifact_id: str) -> ArtifactRecord: ...

    def find_by_content_hash(
        self, ctx: ProjectContext, content_hash: str
    ) -> ArtifactRecord | None: ...

    def list_artifacts(
        self, ctx: ProjectContext, *, kind: str | None = None
    ) -> list[ArtifactRecord]: ...

    def update_metadata(
        self, ctx: ProjectContext, artifact_id: str,
        metadata: dict,
    ) -> None:
        """Replace the artifact's `metadata` dict in-place. Used by
 the soft-delete path to set `metadata.deleted_at` without
 rewriting the artifact's content. Raises
 `ArtifactNotFoundError` if the id isn't registered."""
        ...

    def delete_by_artifact_id(
        self, ctx: ProjectContext, artifact_id: str,
    ) -> bool:
        """Physically remove the registry record for `artifact_id`.
 Used by the hard-delete (purge) path AFTER the artifact's
 on-disk file has been removed. Returns True iff a record
 was removed; False if the id wasn't present (idempotent —
 purge is allowed to run twice). Raising on missing would
 force the caller to coordinate with file-deletion ordering,
 which is unnecessary friction."""
        ...


class JsonArtifactRegistry:
    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    def add(self, record: ArtifactRecord) -> None:
        # Last-line-of-defense lineage check. Raises if a
        # lineage-required-kind artifact (graph_json) lands without
        # ``metadata.run_id`` — hermetic guard that catches bypasses
        # of the upstream registration helpers. See
        # ``_enforce_registry_lineage_or_raise`` docstring.
        _enforce_registry_lineage_or_raise(record)
        self._raw_add(record)

    def _raw_add(self, record: ArtifactRecord) -> None:
        """Write a record WITHOUT running the lineage guard.

        Internal/test/migration use only. Intended for two narrow
        scenarios:

          * **Test fixtures** that need to seed a known-orphan
            ``graph_json`` artifact in order to exercise the
            project-wide cleanup sweep / repair endpoint. The
            public ``add`` correctly refuses such writes; tests
            that need to PROVE the sweep cleans them up must
            seed-then-sweep.
          * **One-off migration tools** that backfill or rewrite
            historical artifacts predating the guard. Real
            production code paths must use ``add()``.

        Not exposed on the ``ArtifactRegistry`` Protocol — callers
        rely on ``isinstance(JsonArtifactRegistry)`` to access it.
        """
        records = self._read(record.project)
        if any(r.artifact_id == record.artifact_id for r in records):
            raise J1Error(
                f"artifact_id {record.artifact_id} already present in registry"
            )
        records.append(record)
        self._write(record.project, records)

    def get(self, ctx: ProjectContext, artifact_id: str) -> ArtifactRecord:
        for record in self._read(ctx):
            if record.artifact_id == artifact_id:
                return record
        raise ArtifactNotFoundError(
            f"artifact {artifact_id} not found in {ctx.tenant_id}/{ctx.project_id}"
        )

    def find_by_content_hash(
        self, ctx: ProjectContext, content_hash: str
    ) -> ArtifactRecord | None:
        for record in self._read(ctx):
            if record.content_hash == content_hash:
                return record
        return None

    def list_artifacts(
        self, ctx: ProjectContext, *, kind: str | None = None
    ) -> list[ArtifactRecord]:
        records = self._read(ctx)
        if kind is None:
            return records
        return [r for r in records if r.kind == kind]

    def update_metadata(
        self, ctx: ProjectContext, artifact_id: str, metadata: dict,
    ) -> None:
        """Rewrite `metadata` for one artifact. Used by soft-delete
 to set `metadata.deleted_at` without touching the artifact
 bytes. Atomic via tmp-file + rename in `_write`."""
        from dataclasses import replace as _replace
        records = self._read(ctx)
        for i, r in enumerate(records):
            if r.artifact_id == artifact_id:
                records[i] = _replace(r, metadata=dict(metadata))
                self._write(ctx, records)
                return
        raise ArtifactNotFoundError(
            f"artifact {artifact_id} not found in {ctx.tenant_id}/{ctx.project_id}"
        )

    def delete_by_artifact_id(
        self, ctx: ProjectContext, artifact_id: str,
    ) -> bool:
        records = self._read(ctx)
        kept = [r for r in records if r.artifact_id != artifact_id]
        if len(kept) == len(records):
            return False
        self._write(ctx, kept)
        return True

    def _path(self, ctx: ProjectContext) -> Path:
        return self._workspace.runtime(ctx) / ARTIFACT_REGISTRY_FILENAME

    def _read(self, ctx: ProjectContext) -> list[ArtifactRecord]:
        path = self._path(ctx)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [_record_from_dict(d) for d in data.get("artifacts", [])]

    def _write(
        self, ctx: ProjectContext, records: list[ArtifactRecord]
    ) -> None:
        path = self._path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": ARTIFACT_REGISTRY_VERSION,
            "artifacts": [to_jsonable(r) for r in records],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(path)


def _record_from_dict(d: dict) -> ArtifactRecord:
    project_data = d["project"]
    project = ProjectContext(
        tenant_id=project_data["tenant_id"],
        project_id=project_data["project_id"],
        profile=project_data.get("profile"),
    )
    return ArtifactRecord(
        artifact_id=d["artifact_id"],
        project=project,
        kind=d["kind"],
        location=d["location"],
        content_hash=d["content_hash"],
        byte_size=d["byte_size"],
        status=ProcessingStatus(d["status"]),
        review_status=ReviewStatus(d["review_status"]),
        version=d["version"],
        created_at=datetime.fromisoformat(d["created_at"]),
        updated_at=datetime.fromisoformat(d["updated_at"]),
        source_document_ids=list(d.get("source_document_ids", [])),
        source_artifact_ids=list(d.get("source_artifact_ids", [])),
        metadata=dict(d.get("metadata", {})),
    )
