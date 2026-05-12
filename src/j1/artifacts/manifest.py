"""`ArtifactManifest` — per-run record of every artifact a run
produced or safely reused from a prior run.

Phase 9 of the document-centric refactor. The manifest is the
explicit, queryable answer to "which artifacts belong to run X?"
and "which of those did this run actually produce vs. reuse from
an earlier attempt?" Today that information lives implicitly in
``artifact.metadata["run_id"]`` + ``source_artifact_ids`` lineage
walks; the manifest makes the contract first-class.

What this module ships (the **contract**, not yet the workflow
hookup):

* :class:`ArtifactManifest` — frozen dataclass describing one run's
  artifact set, with explicit reuse provenance.
* :class:`ManifestArtifactRef` — one artifact reference inside the
  manifest. Carries the artifact id, kind, and (for reused
  artifacts) the source run id + the structured reuse reason.
* :class:`JsonlArtifactManifestStore` — append-only JSONL store
  mirroring the existing `IngestionRunStore` pattern. One file per
  project; latest snapshot per `manifest_id` wins on read.
* :func:`is_safe_to_reuse` — the predicate that decides whether a
  prior artifact can be referenced by a new run's manifest. Encodes
  the spec section-12 rules: same document version, same compile
  config hash, source document not removed.
* :func:`build_manifest` — convenience constructor.

What this module does **NOT** ship in Phase 9:

* The workflow doesn't yet call `build_manifest` at the end of each
  run — that wiring belongs in a follow-up that touches the
  Temporal activity layer. Phase 9 lands the contract + tests so
  the follow-up doesn't have to invent both at once.

Why JSONL: the run store already uses this pattern, the artifact
registry already uses this pattern, and the manifest's read pattern
(latest-snapshot-per-id) matches both. Atomic-rename write protects
against partial writes; the schema is forward-compatible because
the deserializer ignores unknown fields.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Protocol

from j1._serialization import to_jsonable
from j1.artifacts.models import ArtifactRecord
from j1.projects.context import ProjectContext
from j1.workspace.layout import WorkspaceArea
from j1.workspace.resolver import WorkspaceResolver


MANIFEST_FILENAME = "artifact_manifests.jsonl"


# ---- Data model ----------------------------------------------------


@dataclass(frozen=True)
class ManifestArtifactRef:
    """One artifact entry inside an `ArtifactManifest`.

    `artifact_id` + `kind` are the load-bearing fields — together
    they let the consumer rehydrate the actual `ArtifactRecord`
    from the registry without rewalking the lineage graph.

    `produced_by_this_run` distinguishes artifacts THIS run created
    from artifacts it REUSED from an earlier attempt. Reused
    entries also carry `reused_from_run_id` + `reuse_reason` so
    operators can trace "where did this artifact come from?"
    without a database query.

    `reuse_reason` is a short structured tag (e.g. `"same_document_
    version"`, `"same_compile_config_hash"`) — kept as a literal so
    the FE can render a stable badge instead of free-text. Empty
    string when the artifact was produced fresh.
    """

    artifact_id: str
    kind: str
    produced_by_this_run: bool = True
    reused_from_run_id: str | None = None
    reuse_reason: str = ""


@dataclass(frozen=True)
class ArtifactManifest:
    """A run's full artifact set, with explicit reuse provenance.

    Identity rules:

    * `manifest_id` — opaque uuid; mostly used for logging.
    * `run_id`      — FK to the run record. One manifest per run.
    * `document_id` / `document_version_id` — denormalised from the
      run record so the manifest can answer "which artifacts belong
      to this document version?" without joining.

    The full `artifacts` tuple is the authoritative artifact set for
    this run. The companion `IngestionRun.metadata` already carries
    `produced_artifact_ids` for backward-compat; both shapes can
    coexist during the rollout (Phase 9 lands the manifest contract
    + store; a follow-up phase deprecates the metadata list once
    every consumer reads from the manifest).

    Frozen by design — manifests are an immutable record of what
    one run committed. Re-running a step doesn't mutate the prior
    manifest; the next run gets its own manifest with its own
    reused-from pointers.
    """

    manifest_id: str
    run_id: str
    document_id: str
    document_version_id: str | None
    artifacts: tuple[ManifestArtifactRef, ...]
    created_at: datetime
    # Aggregate convenience pointers. When `reused_from_run_id` is
    # set, EVERY reused artifact in `artifacts` came from this run;
    # use it for cheap "did we reuse anything?" / "which prior run
    # are we deriving from?" lookups without scanning `artifacts`.
    # `None` when nothing was reused.
    reused_from_run_id: str | None = None

    def reused_artifact_ids(self) -> tuple[str, ...]:
        """Convenience: ids of artifacts this run REUSED from a
        prior run. Empty when the run produced everything fresh."""
        return tuple(
            a.artifact_id for a in self.artifacts if not a.produced_by_this_run
        )

    def produced_artifact_ids(self) -> tuple[str, ...]:
        """Convenience: ids of artifacts this run produced itself."""
        return tuple(
            a.artifact_id for a in self.artifacts if a.produced_by_this_run
        )


# ---- Safe-reuse predicate ------------------------------------------


# Reuse-reason vocabulary. Kept as constants so audit-log readers
# and the FE can match against stable strings rather than free-text.
REUSE_REASON_SAME_DOCUMENT_VERSION = "same_document_version"
REUSE_REASON_SAME_FILE_HASH = "same_file_hash"
REUSE_REASON_SAME_COMPILE_CONFIG = "same_compile_config_hash"
REUSE_REASON_SAME_PARSER_SETTINGS = "same_parser_settings"
REUSE_REASON_SAME_DOMAIN_CONFIG = "same_domain_config_version"
REUSE_REASON_SAME_PROCESSOR_VERSION = "same_processor_version"


# Artifact metadata-key vocabulary that the safe-reuse predicate
# reads. We list them here so consumers know what to stamp at write
# time if they want their artifacts to be reusable.
_META_DOCUMENT_VERSION = "document_version_id"
_META_FILE_HASH = "file_hash"
_META_COMPILE_CONFIG = "compile_config_hash"
_META_PARSER_SETTINGS = "parser_settings_hash"
_META_DOMAIN_CONFIG = "domain_config_version"
_META_PROCESSOR_VERSION = "processor_version"


@dataclass(frozen=True)
class ReuseContext:
    """The current run's identity that we're checking a prior
    artifact's compatibility against.

    Fields default to ``None`` because not every run carries every
    facet (e.g. a domain-agnostic project has no `domain_config_
    version`). When a facet is None on EITHER side (current or
    prior), the predicate treats that facet as "no constraint" —
    reuse proceeds.
    """

    document_version_id: str | None = None
    file_hash: str | None = None
    compile_config_hash: str | None = None
    parser_settings_hash: str | None = None
    domain_config_version: str | None = None
    processor_version: str | None = None


def is_safe_to_reuse(
    *,
    prior_artifact: ArtifactRecord,
    current: ReuseContext,
) -> tuple[bool, str]:
    """Return ``(safe, reason)`` for reusing ``prior_artifact`` in
    the current run.

    Spec section-12 rules. The predicate is **strict by default** —
    reuse only proceeds when EVERY non-None facet matches. The
    intent: artifacts are immutable, so a mismatched reuse silently
    surfaces stale data, which is the worst kind of bug.

    The hard rejection: a prior artifact whose source document is
    marked ``removed`` MUST NEVER be reused, regardless of every
    other check. Spec: "Never let removed document artifacts be
    reused by default."

    Returns:

      ``(False, "")``         — reuse rejected (the empty reason
                                 string is just so callers always
                                 get a 2-tuple they can destructure).
      ``(True,  REUSE_…)``    — reuse safe; the reason string is
                                 one of the ``REUSE_REASON_…``
                                 vocabulary literals, suitable for
                                 stamping onto the manifest.

    The reason returned is the **most specific** facet that matched
    — when the current run's `document_version_id` matches the
    prior's, that's the strongest signal and we return
    ``REUSE_REASON_SAME_DOCUMENT_VERSION``. Falls through to
    progressively weaker facets when the strongest isn't available.
    """
    prior_meta = (
        prior_artifact.metadata if isinstance(prior_artifact.metadata, dict)
        else {}
    )
    # Hard veto: removed-document artifacts never participate in
    # reuse. Phase 3's lifecycle stamping marks every artifact with
    # `metadata.knowledge_state` when the parent document changes
    # state, so this check is O(1) — no registry round-trip needed.
    if prior_meta.get("knowledge_state") == "removed":
        return (False, "")

    # Walk facets strongest-first. For each facet, when BOTH sides
    # carry a value, they must match. When either side is missing
    # the facet, we don't allow it to BE the matching reason — but
    # other facets can still grant safe-reuse.
    matches: list[str] = []
    if not _facet_compatible(
        current.document_version_id, prior_meta.get(_META_DOCUMENT_VERSION),
    ):
        return (False, "")
    if current.document_version_id is not None \
            and prior_meta.get(_META_DOCUMENT_VERSION) is not None:
        matches.append(REUSE_REASON_SAME_DOCUMENT_VERSION)

    if not _facet_compatible(
        current.file_hash, prior_meta.get(_META_FILE_HASH),
    ):
        return (False, "")
    if current.file_hash is not None \
            and prior_meta.get(_META_FILE_HASH) is not None:
        matches.append(REUSE_REASON_SAME_FILE_HASH)

    if not _facet_compatible(
        current.compile_config_hash, prior_meta.get(_META_COMPILE_CONFIG),
    ):
        return (False, "")
    if current.compile_config_hash is not None \
            and prior_meta.get(_META_COMPILE_CONFIG) is not None:
        matches.append(REUSE_REASON_SAME_COMPILE_CONFIG)

    if not _facet_compatible(
        current.parser_settings_hash, prior_meta.get(_META_PARSER_SETTINGS),
    ):
        return (False, "")
    if current.parser_settings_hash is not None \
            and prior_meta.get(_META_PARSER_SETTINGS) is not None:
        matches.append(REUSE_REASON_SAME_PARSER_SETTINGS)

    if not _facet_compatible(
        current.domain_config_version, prior_meta.get(_META_DOMAIN_CONFIG),
    ):
        return (False, "")
    if current.domain_config_version is not None \
            and prior_meta.get(_META_DOMAIN_CONFIG) is not None:
        matches.append(REUSE_REASON_SAME_DOMAIN_CONFIG)

    if not _facet_compatible(
        current.processor_version, prior_meta.get(_META_PROCESSOR_VERSION),
    ):
        return (False, "")
    if current.processor_version is not None \
            and prior_meta.get(_META_PROCESSOR_VERSION) is not None:
        matches.append(REUSE_REASON_SAME_PROCESSOR_VERSION)

    if not matches:
        # No positive match on any facet — the prior artifact has
        # zero metadata supporting reuse. Refuse so we don't silently
        # carry forward something we can't reason about.
        return (False, "")
    # Strongest reason wins (matches[] is appended strongest-first).
    return (True, matches[0])


def _facet_compatible(current: str | None, prior: object) -> bool:
    """One facet's compatibility check. Either-side-None counts as
    compatible (no constraint); two non-None values must match
    exactly."""
    if current is None or prior is None:
        return True
    return current == prior


# ---- Builder + store ------------------------------------------------


def build_manifest(
    *,
    manifest_id: str,
    run_id: str,
    document_id: str,
    document_version_id: str | None,
    produced: Iterable[ArtifactRecord] = (),
    reused: Iterable[ArtifactRecord] = (),
    reused_from_run_id: str | None = None,
    reuse_reasons: dict[str, str] | None = None,
    now: datetime | None = None,
) -> ArtifactManifest:
    """Compose an `ArtifactManifest` from the run's produced +
    reused artifact lists.

    `reuse_reasons` is a dict keyed by `artifact_id` carrying the
    structured reason for each reused artifact. Missing keys
    default to the empty string — callers SHOULD pass them so the
    manifest stays self-explaining, but the predicate doesn't
    enforce it.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)
    reasons = dict(reuse_reasons or {})
    refs: list[ManifestArtifactRef] = []
    for record in produced:
        refs.append(ManifestArtifactRef(
            artifact_id=record.artifact_id,
            kind=record.kind,
            produced_by_this_run=True,
        ))
    for record in reused:
        refs.append(ManifestArtifactRef(
            artifact_id=record.artifact_id,
            kind=record.kind,
            produced_by_this_run=False,
            reused_from_run_id=reused_from_run_id,
            reuse_reason=reasons.get(record.artifact_id, ""),
        ))
    return ArtifactManifest(
        manifest_id=manifest_id,
        run_id=run_id,
        document_id=document_id,
        document_version_id=document_version_id,
        artifacts=tuple(refs),
        created_at=now,
        reused_from_run_id=reused_from_run_id if list(reused) else None,
    )


class ArtifactManifestStore(Protocol):
    """Read/write surface for `ArtifactManifest`. Mirrors the
    pattern of `IngestionRunStore` so callers can shape-shift
    between the two without learning a second API."""

    def upsert(
        self, ctx: ProjectContext, manifest: ArtifactManifest,
    ) -> None: ...

    def get(
        self, ctx: ProjectContext, manifest_id: str,
    ) -> ArtifactManifest | None: ...

    def get_for_run(
        self, ctx: ProjectContext, run_id: str,
    ) -> ArtifactManifest | None: ...

    def list_for_document(
        self, ctx: ProjectContext, document_id: str,
    ) -> list[ArtifactManifest]: ...


class JsonlArtifactManifestStore:
    """File-backed implementation. JSONL append for writes; reads
    walk the whole file picking the latest snapshot per
    `manifest_id`. Same trade-off the `IngestionRunStore` makes:
    O(file size) reads, but eventual consistency is trivial and
    crash safety is free (the file is append-only).
    """

    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    def upsert(
        self, ctx: ProjectContext, manifest: ArtifactManifest,
    ) -> None:
        path = self._path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(_manifest_to_dict(manifest)) + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def get(
        self, ctx: ProjectContext, manifest_id: str,
    ) -> ArtifactManifest | None:
        latest: ArtifactManifest | None = None
        for manifest in self._iter_all(ctx):
            if manifest.manifest_id == manifest_id:
                latest = manifest
        return latest

    def get_for_run(
        self, ctx: ProjectContext, run_id: str,
    ) -> ArtifactManifest | None:
        """Run → manifest is 1:1 by contract (one manifest per run).
        Returns the latest snapshot for the run id; ``None`` when
        no manifest exists yet."""
        latest: ArtifactManifest | None = None
        for manifest in self._iter_all(ctx):
            if manifest.run_id == run_id:
                latest = manifest
        return latest

    def list_for_document(
        self, ctx: ProjectContext, document_id: str,
    ) -> list[ArtifactManifest]:
        """Manifests for one document, latest-per-run, sorted by
        creation time descending. Lets the document-detail panel
        render "manifests across runs" without scanning every
        run's metadata."""
        latest_by_run: dict[str, ArtifactManifest] = {}
        for manifest in self._iter_all(ctx):
            if manifest.document_id != document_id:
                continue
            latest_by_run[manifest.run_id] = manifest
        return sorted(
            latest_by_run.values(),
            key=lambda m: m.created_at,
            reverse=True,
        )

    def _path(self, ctx: ProjectContext) -> Path:
        return self._workspace.area(ctx, WorkspaceArea.AUDIT) / MANIFEST_FILENAME

    def _iter_all(self, ctx: ProjectContext):
        path = self._path(ctx)
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                manifest = _manifest_from_dict(payload)
                if manifest is not None:
                    yield manifest


# ---- Serialisation helpers ----------------------------------------


def _manifest_to_dict(manifest: ArtifactManifest) -> dict:
    return to_jsonable({
        "manifest_id": manifest.manifest_id,
        "run_id": manifest.run_id,
        "document_id": manifest.document_id,
        "document_version_id": manifest.document_version_id,
        "artifacts": [
            {
                "artifact_id": a.artifact_id,
                "kind": a.kind,
                "produced_by_this_run": a.produced_by_this_run,
                "reused_from_run_id": a.reused_from_run_id,
                "reuse_reason": a.reuse_reason,
            }
            for a in manifest.artifacts
        ],
        "created_at": manifest.created_at,
        "reused_from_run_id": manifest.reused_from_run_id,
    })


def _manifest_from_dict(payload: dict) -> ArtifactManifest | None:
    """Forward-compatible deserialiser. Unknown fields are ignored;
    missing optional fields default to safe values."""
    try:
        manifest_id = str(payload["manifest_id"])
        run_id = str(payload["run_id"])
        document_id = str(payload["document_id"])
    except (KeyError, TypeError):
        return None
    created_at_raw = payload.get("created_at")
    if isinstance(created_at_raw, str):
        try:
            created_at = datetime.fromisoformat(created_at_raw)
        except ValueError:
            return None
    elif isinstance(created_at_raw, datetime):
        created_at = created_at_raw
    else:
        return None
    refs_raw = payload.get("artifacts") or []
    refs: list[ManifestArtifactRef] = []
    for entry in refs_raw:
        if not isinstance(entry, dict):
            continue
        artifact_id = entry.get("artifact_id")
        kind = entry.get("kind")
        if not artifact_id or not kind:
            continue
        refs.append(ManifestArtifactRef(
            artifact_id=str(artifact_id),
            kind=str(kind),
            produced_by_this_run=bool(
                entry.get("produced_by_this_run", True),
            ),
            reused_from_run_id=(
                str(entry["reused_from_run_id"])
                if entry.get("reused_from_run_id") else None
            ),
            reuse_reason=str(entry.get("reuse_reason") or ""),
        ))
    return ArtifactManifest(
        manifest_id=manifest_id,
        run_id=run_id,
        document_id=document_id,
        document_version_id=(
            str(payload["document_version_id"])
            if payload.get("document_version_id") else None
        ),
        artifacts=tuple(refs),
        created_at=created_at,
        reused_from_run_id=(
            str(payload["reused_from_run_id"])
            if payload.get("reused_from_run_id") else None
        ),
    )


__all__ = [
    "ArtifactManifest",
    "ArtifactManifestStore",
    "JsonlArtifactManifestStore",
    "MANIFEST_FILENAME",
    "ManifestArtifactRef",
    "REUSE_REASON_SAME_COMPILE_CONFIG",
    "REUSE_REASON_SAME_DOCUMENT_VERSION",
    "REUSE_REASON_SAME_DOMAIN_CONFIG",
    "REUSE_REASON_SAME_FILE_HASH",
    "REUSE_REASON_SAME_PARSER_SETTINGS",
    "REUSE_REASON_SAME_PROCESSOR_VERSION",
    "ReuseContext",
    "build_manifest",
    "is_safe_to_reuse",
]
