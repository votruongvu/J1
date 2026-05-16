"""Knowledge Memory build service — Phase 2 orchestrator.

Sits between the REST manual-action endpoint and the
deterministic `KnowledgeMemoryBuilder` + `persist_knowledge_memory`
seam. The service:

  1. Resolves the active snapshot for a document.
  2. Reads compile + enrichment artifact payloads scoped to that
     snapshot.
  3. Resolves the active domain pack's hints (aliases, terminology,
     retrieval hints).
  4. Hands the inputs to `KnowledgeMemoryBuilder`.
  5. Persists the resulting `KnowledgeMemoryPayload` via
     `ProcessingService.persist_knowledge_memory` — which also
     supersedes any prior active memory artifact for the same
     snapshot.

The service is sync + deterministic — no workflow dispatch, no
LLM calls. Phase 2 keeps it that way; Phase 3 will hook automatic
rebuilds into the workflow.

Hard contract:

  * Reads only the ACTIVE snapshot's artifacts. Superseded
    artifacts are filtered out by `metadata.search_state` to
    prevent stale enrichment leaking into memory.
  * Idempotent — rebuilding for the same snapshot supersedes the
    prior memory row.
  * Best-effort dependencies — missing domain registry / missing
    workspace / etc. each produce a build warning, not a hard
    failure. Memory always builds something, even if minimal.
  * No LLM calls. The service never imports an LLM client.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from j1.memory.knowledge_memory import (
    KnowledgeMemoryBuilder,
    KnowledgeMemoryBuildInputs,
    KnowledgeMemoryPayload,
    WARNING_COMPILE_NOT_SUCCEEDED,
    WARNING_DOMAIN_PACK_NOT_FOUND,
    WARNING_NO_ACTIVE_SNAPSHOT,
    WARNING_SUPERSEDED_ARTIFACT_SKIPPED,
)
from j1.memory.status import (
    KnowledgeMemoryStatus,
    resolve_knowledge_memory_status,
)
from j1.projects.context import ProjectContext


_log = logging.getLogger(__name__)


# Artifact kinds the service reads + projects. Enumerated explicitly
# (not derived from `KNOWN_DERIVED_ENRICHMENT_KINDS`) because
# `post_compile_enrich_plan` is a known kind but NOT a result
# artifact — it's a plan. The service skips it.
_ENRICHMENT_KINDS_PROJECTED: frozenset[str] = frozenset({
    "enriched.requirements",
    "enriched.risks",
    "enriched.tables",
    "enriched.visuals",
    "enriched.formulas",
    "enriched.consistency_findings",
    "enriched.document_map",
    "enriched.source_map",
    "enriched.confidence_assessment",
    "enrichment_result",
    "domain_enrichment_aliases",
})


@dataclass(frozen=True)
class KnowledgeMemoryBuildResult:
    """Operator-readable summary returned to the REST caller."""

    status: str  # "succeeded", "skipped_no_snapshot", "skipped_compile_failed"
    document_id: str
    snapshot_id: str | None
    run_id: str | None
    artifact_id: str | None  # None on skip / failure paths
    entry_count: int
    warnings: tuple[str, ...]
    message: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "document_id": self.document_id,
            "snapshot_id": self.snapshot_id,
            "run_id": self.run_id,
            "artifact_id": self.artifact_id,
            "entry_count": self.entry_count,
            "warnings": list(self.warnings),
            "message": self.message,
        }


class NoActiveSnapshotError(RuntimeError):
    """Raised when the document has no `active_snapshot_id`. The
    REST endpoint maps this to HTTP 409 so the FE can render an
    actionable "run an initial ingest first" message."""


class KnowledgeMemoryService:
    """Phase 2 orchestrator. Composes the build pipeline from
    injectable dependencies so tests can pass fakes.

    Dependencies — all required EXCEPT ``domain_registry`` and
    ``workspace`` (each optional; the service degrades to a
    warning when missing rather than refusing the build):

      * ``source_lookup`` — `facade.source_lookup`, used to read
        the document record (active_snapshot_id + project context).
      * ``artifact_registry`` — `ArtifactRegistry` for listing
        artifacts + reading bytes.
      * ``workspace`` — `WorkspaceResolver` for resolving artifact
        file paths. When absent, the service can still build from
        in-memory artifact metadata but enrichment payloads stay
        empty; warned.
      * ``processing_service`` — `ProcessingService`, used to call
        `persist_knowledge_memory`.
      * ``domain_registry`` — optional, for resolving the active
        pack's static aliases / terminology / retrieval hints.
      * ``builder`` — optional override for tests; defaults to a
        fresh `KnowledgeMemoryBuilder`.
    """

    def __init__(
        self,
        *,
        source_lookup,
        artifact_registry,
        workspace=None,
        processing_service,
        domain_registry=None,
        builder: KnowledgeMemoryBuilder | None = None,
    ) -> None:
        self._source_lookup = source_lookup
        self._artifacts = artifact_registry
        self._workspace = workspace
        self._processing = processing_service
        self._domain_registry = domain_registry
        self._builder = builder or KnowledgeMemoryBuilder()

    def build_and_persist(
        self,
        ctx: ProjectContext,
        document_id: str,
        *,
        actor: str = "system",
        trigger: str | None = None,
    ) -> KnowledgeMemoryBuildResult:
        # 1. Document + active-snapshot guard.
        doc = self._source_lookup.get_source(ctx, document_id)
        active_snapshot_id = getattr(doc, "active_snapshot_id", None)
        if not active_snapshot_id:
            raise NoActiveSnapshotError(
                f"document {document_id!r} has no active snapshot — "
                "run an initial ingest before building knowledge "
                "memory."
            )

        # 2. List artifacts for this document. Filter to the active
        # snapshot AND drop superseded rows so stale enrichment from
        # an old re-index never leaks into the new memory.
        all_records = list(self._artifacts.list_artifacts(ctx))
        active_records, superseded_skipped = (
            self._select_active_artifacts(
                all_records,
                document_id=document_id,
                snapshot_id=active_snapshot_id,
            )
        )

        # 3. Pick compile + enrichment subsets.
        compile_records = [
            r for r in active_records
            if r.kind in ("compiled.text", "compiled.json", "chunk", "graph_json")
        ]
        enrichment_records = [
            r for r in active_records
            if r.kind in _ENRICHMENT_KINDS_PROJECTED
        ]

        # 4. Resolve run_id — prefer the most-recent run on the
        # compile artifacts (they all carry metadata.run_id).
        run_id = self._resolve_active_run_id(active_records)

        # 5. Read enrichment payloads from disk. Skips bytes we
        # can't decode rather than failing the whole build.
        enrichment_artifacts = self._read_enrichment_artifacts(
            ctx, enrichment_records,
        )

        # 6. Domain pack hints.
        domain_id, pack_version, aliases, terminology, retrieval_hints = (
            self._resolve_domain_pack(doc)
        )
        domain_warnings: list[str] = []
        if self._domain_registry is None and domain_id is None:
            domain_warnings.append(WARNING_DOMAIN_PACK_NOT_FOUND)

        # 7. Graph counts — read cheaply from compile metadata
        # rather than parsing graph_json bytes.
        entity_count, rel_count = self._graph_counts(compile_records)

        # 8. Document type hint — pull from the document record /
        # most-recent assessment plan when present.
        document_type_hint = getattr(doc, "document_type_hint", None)

        # 9. Build.
        inputs = KnowledgeMemoryBuildInputs(
            document_id=document_id,
            snapshot_id=active_snapshot_id,
            run_id=run_id,
            project_id=ctx.project_id,
            domain_id=domain_id,
            domain_pack_version=pack_version,
            document_type_hint=document_type_hint,
            compile_artifact_ids=tuple(r.artifact_id for r in compile_records),
            aliases=aliases,
            terminology_hints=terminology,
            retrieval_hints=retrieval_hints,
            enrichment_artifacts=enrichment_artifacts,
            graph_entity_count=entity_count,
            graph_relationship_count=rel_count,
        )
        payload = self._builder.build(inputs)

        # 10. Stamp build-time warnings on the artifact payload.
        accumulated_warnings = list(payload.warnings)
        if superseded_skipped > 0:
            accumulated_warnings.append(WARNING_SUPERSEDED_ARTIFACT_SKIPPED)
        accumulated_warnings.extend(domain_warnings)
        payload_dict = payload.to_payload()
        payload_dict["warnings"] = accumulated_warnings

        # 11. Persist via ProcessingService — this also supersedes
        # any prior knowledge_memory artifact for the same snapshot.
        # Phase 3A: the ``trigger`` arg flows through to the
        # artifact's metadata so dashboards can answer "was this
        # built after compile, after enrichment, or by the manual
        # action?" without re-reading the JSON payload.
        includes_domain_insights = bool(enrichment_artifacts)
        record = self._processing.persist_knowledge_memory(
            ctx,
            run_id=run_id or "manual-build",
            document_id=document_id,
            snapshot_id=active_snapshot_id,
            payload=payload_dict,
            actor=actor,
            trigger=trigger,
            includes_domain_insights=includes_domain_insights,
        )

        return KnowledgeMemoryBuildResult(
            status="succeeded",
            document_id=document_id,
            snapshot_id=active_snapshot_id,
            run_id=run_id,
            artifact_id=record.artifact_id,
            entry_count=len(payload.entries),
            warnings=tuple(accumulated_warnings),
            message=(
                f"Built knowledge memory with {len(payload.entries)} "
                f"entries from {len(enrichment_records)} enrichment "
                f"artifact(s) and {len(compile_records)} compile "
                f"artifact(s)."
            ),
        )

    def resolve_status(
        self, ctx: ProjectContext, document_id: str,
    ) -> KnowledgeMemoryStatus:
        """Phase 3B: project the active snapshot's knowledge_memory
        artifact metadata into a compact status DTO. Read-only; no
        build, no LLM. Resolves `active_snapshot_id` from the
        document record + lists artifacts via the registry the
        service already holds.

        Defensive: a missing document record returns
        ``status=not_built`` with `document_id` stamped so the FE
        can render a "memory not built" state without the REST
        adapter having to short-circuit.
        """
        try:
            doc = self._source_lookup.get_source(ctx, document_id)
        except Exception:  # noqa: BLE001 — defensive read path
            return KnowledgeMemoryStatus(document_id=document_id)
        active_snapshot_id = getattr(doc, "active_snapshot_id", None)
        return resolve_knowledge_memory_status(
            ctx=ctx,
            document_id=document_id,
            active_snapshot_id=active_snapshot_id,
            artifact_registry=self._artifacts,
        )

    # ---- Helpers --------------------------------------------------

    def _select_active_artifacts(
        self, records: Sequence[Any], *,
        document_id: str, snapshot_id: str,
    ) -> tuple[list[Any], int]:
        """Return (active_records, superseded_count). Active means:
          * Belongs to this document (via metadata.document_id OR
            source_document_ids).
          * Belongs to this snapshot (via metadata.snapshot_id).
          * `search_state` is `active` or unset.

        Records with `search_state="superseded"` or `"invalid"` are
        excluded — that's the snapshot-isolation guarantee."""
        active: list[Any] = []
        superseded = 0
        for record in records:
            meta = dict(getattr(record, "metadata", None) or {})
            # Document scope — accept either metadata or source list.
            record_doc = meta.get("document_id")
            if not record_doc:
                sources = getattr(record, "source_document_ids", None) or ()
                if document_id in sources:
                    record_doc = document_id
            if record_doc != document_id:
                continue
            # Snapshot scope — strict; we don't fall back to "no
            # snapshot stamp == include." Memory is snapshot-scoped
            # by contract.
            if meta.get("snapshot_id") != snapshot_id:
                continue
            state = meta.get("search_state") or "active"
            if state != "active":
                superseded += 1
                continue
            active.append(record)
        return active, superseded

    def _resolve_active_run_id(
        self, active_records: Sequence[Any],
    ) -> str | None:
        # Most-recent record (by `created_at` if comparable, else
        # last-in-list) wins. We don't need precise ordering — any
        # active run_id ties memory to a real compile/enrichment
        # run for audit.
        latest_run_id: str | None = None
        latest_created = None
        for record in active_records:
            meta = dict(getattr(record, "metadata", None) or {})
            run_id = meta.get("run_id")
            if not run_id:
                continue
            created = getattr(record, "created_at", None)
            if latest_created is None or (
                created is not None and created >= latest_created
            ):
                latest_created = created
                latest_run_id = str(run_id)
        return latest_run_id

    def _read_enrichment_artifacts(
        self,
        ctx: ProjectContext,
        records: Sequence[Any],
    ) -> list[tuple[str, str, Mapping[str, Any]]]:
        """Read each enrichment artifact's JSON payload from disk.
        Failures are logged and skipped — the build still proceeds
        with whatever was readable."""
        out: list[tuple[str, str, Mapping[str, Any]]] = []
        for record in records:
            try:
                payload = self._read_artifact_payload(ctx, record)
            except Exception:  # noqa: BLE001 — best-effort
                _log.warning(
                    "failed to read enrichment artifact %s for memory build",
                    getattr(record, "artifact_id", "?"),
                    exc_info=True,
                )
                continue
            if not isinstance(payload, Mapping):
                continue
            out.append((record.artifact_id, record.kind, payload))
        return out

    def _read_artifact_payload(
        self, ctx: ProjectContext, record: Any,
    ) -> Mapping[str, Any] | None:
        # Some producers stamp the JSON payload inline on metadata
        # (e.g. `domain_enrichment_aliases` per the audit). Prefer
        # inline when present so we don't need a workspace.
        meta = getattr(record, "metadata", None) or {}
        inline = meta.get("payload")
        if isinstance(inline, Mapping):
            return inline

        if self._workspace is None:
            return None

        location = getattr(record, "location", None)
        if not location:
            return None
        from pathlib import PurePosixPath
        try:
            from j1.workspace.layout import WorkspaceArea
        except ImportError:
            return None
        parts = PurePosixPath(str(location)).parts
        if not parts:
            return None
        area_name, *rest = parts
        try:
            area = WorkspaceArea(area_name)
        except ValueError:
            return None
        try:
            area_root = self._workspace.area(ctx, area).resolve()
        except Exception:  # noqa: BLE001 — best-effort
            return None
        candidate = area_root.joinpath(*rest).resolve()
        try:
            candidate.relative_to(area_root)
        except ValueError:
            # Path-traversal guard.
            return None
        if not candidate.exists():
            return None
        try:
            data = candidate.read_bytes()
        except OSError:
            return None
        try:
            decoded = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if isinstance(decoded, Mapping):
            return decoded
        return None

    def _resolve_domain_pack(self, doc: Any) -> tuple[
        str | None, str | None,
        list[Mapping[str, Any]], list[str], list[str],
    ]:
        """Resolve the active domain pack's hints. Returns:
          ``(domain_id, pack_version, aliases, terminology, retrieval_hints)``

        Missing registry / missing pack → empty lists + None ids,
        with a warning surfaced at the caller. Best-effort across
        registry / pack APIs to stay decoupled from the domain
        package internals."""
        # Look for a domain id on the document record.
        domain_id = (
            getattr(doc, "preferred_domain_id", None)
            or getattr(doc, "domain_id", None)
        )
        if self._domain_registry is None:
            return domain_id, None, [], [], []

        get_pack = (
            getattr(self._domain_registry, "get", None)
            or getattr(self._domain_registry, "get_pack", None)
        )
        if not callable(get_pack):
            return domain_id, None, [], [], []

        try:
            pack = get_pack(domain_id) if domain_id else None
        except Exception:  # noqa: BLE001 — best-effort
            return domain_id, None, [], [], []
        if pack is None:
            return domain_id, None, [], [], []

        version = getattr(pack, "version", None)
        # Aliases — `extraction_hints.entity_aliases` is the canonical
        # location after the 2026-05-* domain refactor.
        aliases: list[Mapping[str, Any]] = []
        extraction_hints = getattr(pack, "extraction_hints", None)
        if extraction_hints is not None:
            entity_aliases = getattr(extraction_hints, "entity_aliases", ()) or ()
            for alias in entity_aliases:
                to_dict = getattr(alias, "to_dict", None)
                if callable(to_dict):
                    aliases.append(to_dict())
                else:
                    # Fallback for plain-dict aliases in legacy packs.
                    if isinstance(alias, Mapping):
                        aliases.append(alias)
            terminology = list(
                getattr(extraction_hints, "terminology_hints", ()) or ()
            )
            retrieval_hints = list(
                getattr(extraction_hints, "retrieval_hints", ()) or ()
            )
        else:
            terminology = []
            retrieval_hints = []

        return (
            domain_id, str(version) if version else None,
            aliases, terminology, retrieval_hints,
        )

    def _graph_counts(
        self, compile_records: Sequence[Any],
    ) -> tuple[int, int]:
        """Read graph counts from compile metadata cheaply. Each
        graph_json record sometimes carries `entity_count` /
        `relationship_count` on its metadata; falls back to 0
        when absent (the builder skips the graph_summary entry)."""
        for record in compile_records:
            if record.kind != "graph_json":
                continue
            meta = dict(getattr(record, "metadata", None) or {})
            entity_count = meta.get("entity_count") or meta.get("entities") or 0
            rel_count = meta.get("relationship_count") or meta.get("relationships") or 0
            try:
                return int(entity_count), int(rel_count)
            except (TypeError, ValueError):
                return 0, 0
        return 0, 0
