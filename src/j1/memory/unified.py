"""Unified Memory projection — the resolver implementation.

Public contract is documented in
[docs/unified-memory-contract.md](../../../docs/unified-memory-contract.md).

Three resolver entry points, one logical shape:

  * ``resolve_project_active_memory(ctx)`` →
    ``ProjectActiveMemoryView`` (collection of document views + an
    aggregate queryability flag).
  * ``resolve_document_active_memory(ctx, document_id)`` →
    ``DocumentMemoryView`` for the document's active snapshot.
  * ``resolve_run_memory(ctx, run_id)`` → ``RunMemoryView`` for
    an explicit run scope (audit / diagnostics).

The resolver composes these existing stores and never mutates them:

  * ``SourceRegistry`` — document records (knowledge_state,
    active_snapshot_id, lifecycle_status).
  * ``IngestionRunStore`` — run records (status, run_type,
    target_snapshot_id, metadata).
  * ``ArtifactRegistry`` — compile + enrichment artifacts, filtered
    by ``(document_id, snapshot_id)``.

Behaviour rules implemented here mirror the queryability contract
verbatim:

  1. Compile is the floor. A document is queryable as soon as the
     active snapshot is set AND compile artifacts resolve for that
     snapshot pair. Enrichment success is NOT part of this check.
  2. Enrichment failure does not regress queryability — the failed
     manual-action run does not promote, so the active snapshot
     stays. The view's ``enrichment_status`` reports the failure as
     optional augmentation.
  3. Missing artifacts override the optimistic status. If the
     ``(document_id, snapshot_id)`` pair has no compile artifacts
     in the registry the view reports
     ``QueryableStatus.MISSING_ARTIFACTS`` even when the snapshot
     store says READY.
  4. Old (non-active) runs do not participate in active query. The
     project / document scopes only resolve the producing run of
     the active snapshot. Old runs are reachable only via
     ``resolve_run_memory``.

The resolver is read-only. It does not enforce auth (the REST
adapter + eligibility resolver already do), it does not load
artifact bytes (it returns refs only), and it does not run queries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Iterable

from j1.projects.context import ProjectContext

if TYPE_CHECKING:
    from j1.artifacts.registry import ArtifactRegistry
    from j1.documents.models import DocumentRecord
    from j1.documents.snapshot_store import DocumentSnapshotStore
    from j1.intake.registry import SourceRegistry
    from j1.runs.models import IngestionRun
    from j1.runs.store import IngestionRunStore


__all__ = [
    "DocumentMemoryView",
    "MemoryNotQueryableError",
    "MemoryScope",
    "ProjectActiveMemoryView",
    "QueryableStatus",
    "RunMemoryView",
    "UnifiedMemoryResolver",
    "UnifiedMemoryView",
]


# ---- Status vocabulary ---------------------------------------------


class QueryableStatus(str, Enum):
    """Explicit, explainable queryability state.

    String values so the REST layer can serialise them directly
    without a translation table. The set is intentionally small —
    new statuses go through a contract review.
    """

    # Compile produced a queryable active snapshot AND its artifacts
    # resolve. The default "this is queryable" verdict.
    QUERYABLE = "queryable"

    # Same as ``QUERYABLE`` plus a successful Domain Enrichment run
    # has produced augmentation artifacts for the active snapshot.
    # Surfaces the optional augmentation to the FE without changing
    # the queryability gate.
    ENRICHMENT_AVAILABLE = "enrichment_available"

    # Queryable from compile-only knowledge; the LAST enrichment
    # attempt failed but the active snapshot was not regressed.
    ENRICHMENT_FAILED = "enrichment_failed"

    # Document exists but no run has produced an active snapshot
    # yet. Pre-ingest / first ingest still mid-flight states.
    NOT_STARTED = "not_started"

    # The producing run's compile failed before any active snapshot
    # could be promoted. Distinct from ``NOT_STARTED`` so the FE can
    # render the failure prompt vs the "queue an ingest" prompt.
    COMPILE_FAILED = "compile_failed"

    # Document is not attached to the knowledge base
    # (``knowledge_state != "attached"``) or its lifecycle is not
    # ``stable``. The document is intentionally invisible.
    NOT_ATTACHED = "not_attached"

    # The document was removed.
    REMOVED = "removed"

    # The active snapshot pointer is set but no compile artifacts
    # resolve for that pair — the DB state is ahead of the physical
    # artifact store. Fails closed: the document is not queryable.
    MISSING_ARTIFACTS = "missing_artifacts"

    # ``resolve_run_memory`` only: the requested run does not exist
    # or its target snapshot is unknown.
    RUN_UNKNOWN = "run_unknown"


_QUERYABLE_STATUSES: frozenset[QueryableStatus] = frozenset({
    QueryableStatus.QUERYABLE,
    QueryableStatus.ENRICHMENT_AVAILABLE,
    QueryableStatus.ENRICHMENT_FAILED,
})


# ---- Scope vocabulary ---------------------------------------------


class MemoryScope(str, Enum):
    """Wire-stable identifier for the three scope variants."""

    PROJECT_ACTIVE = "project_active"
    DOCUMENT_ACTIVE = "document_active"
    RUN_EXPLICIT = "run_explicit"


# ---- View shapes --------------------------------------------------


@dataclass(frozen=True)
class UnifiedMemoryView:
    """Common shape every scope variant returns.

    Concrete subclasses (``DocumentMemoryView``, ``RunMemoryView``,
    ``ProjectActiveMemoryView``) add scope-specific fields but share
    this contract so generic helpers can branch on
    ``queryable_status`` without knowing the scope.
    """

    scope: MemoryScope
    project_id: str
    queryable_status: QueryableStatus
    queryable_reason: str | None = None

    @property
    def queryable(self) -> bool:
        return self.queryable_status in _QUERYABLE_STATUSES


@dataclass(frozen=True)
class DocumentMemoryView(UnifiedMemoryView):
    """Single-document view. Returned for ``resolve_document_active_memory``
    and as the per-document items inside ``ProjectActiveMemoryView``.
    """

    document_id: str = ""
    snapshot_id: str | None = None
    active_run_id: str | None = None
    run_status: str | None = None
    compile_status: str | None = None  # "succeeded" / "failed" / "not_started"
    compile_artifact_refs: tuple[str, ...] = ()
    domain_id: str | None = None
    enrichment_status: str | None = None
    enrichment_artifact_refs: tuple[str, ...] = ()
    plan_warnings: tuple[str, ...] = ()
    unsupported_controls: tuple[str, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class RunMemoryView(UnifiedMemoryView):
    """Explicit-run scope view. Returned by ``resolve_run_memory``.

    Distinct from ``DocumentMemoryView`` because run-explicit scope
    can resolve a run whose snapshot never promoted (audit case).
    The ``queryable`` flag reports whether the run's artifacts are
    intact enough for a diagnostic query, not whether they would
    serve the active query path.
    """

    run_id: str = ""
    document_id: str | None = None
    snapshot_id: str | None = None
    run_status: str | None = None
    run_type: str | None = None
    compile_status: str | None = None
    compile_artifact_refs: tuple[str, ...] = ()
    enrichment_status: str | None = None
    enrichment_artifact_refs: tuple[str, ...] = ()
    is_active_for_document: bool = False


@dataclass(frozen=True)
class ProjectActiveMemoryView(UnifiedMemoryView):
    """Aggregate view for the project-active scope. The aggregate
    ``queryable_status`` is:

      * ``QUERYABLE`` when at least one document view is queryable.
      * ``NOT_STARTED`` when the project has no attached documents
        with an active snapshot.
      * ``MISSING_ARTIFACTS`` only when every otherwise-eligible
        document failed the artifact existence check.
    """

    documents: tuple[DocumentMemoryView, ...] = field(default_factory=tuple)

    @property
    def queryable_documents(self) -> tuple[DocumentMemoryView, ...]:
        return tuple(d for d in self.documents if d.queryable)


# ---- Exceptions ---------------------------------------------------


class MemoryNotQueryableError(RuntimeError):
    """Raised by callers that want to fail-fast on a not-queryable view.

    The resolver itself never raises — it always returns a view with
    ``queryable_status`` filled in. Wrapping the view in this
    exception is the caller's choice (validation service translates
    it to HTTP 409 with structured copy)."""

    def __init__(
        self,
        view: UnifiedMemoryView,
        *,
        message: str | None = None,
    ) -> None:
        self.view = view
        self.queryable_status = view.queryable_status
        self.queryable_reason = view.queryable_reason
        super().__init__(
            message
            or view.queryable_reason
            or f"memory not queryable ({view.queryable_status.value})",
        )


# ---- Resolver ------------------------------------------------------


# Artifact kind prefixes the resolver scans for. Kept small and
# explicit — the registry filters by exact kind, then we re-filter
# by ``(document_id, snapshot_id)`` in code.
_COMPILE_ARTIFACT_KINDS: tuple[str, ...] = (
    "compiled.text",
    "compiled.chunks",
    "compiled.graph",
    "chunk",
    "final_ingestion_report",
)

_ENRICHMENT_ARTIFACT_KIND_PREFIX = "enriched."

_DOCUMENT_ENRICHMENT_RESULT_KIND = "domain_enrichment_result"


# Disallowed lifecycle states match the eligibility resolver. Kept
# in sync deliberately: the memory view is the lifecycle gate the
# eligibility resolver also applies, so callers MUST NOT see a
# "queryable" status for a lifecycle that eligibility would reject.
_DISALLOWED_LIFECYCLE: frozenset[str] = frozenset(
    {"removing", "removed", "failed", "cleanup_failed"},
)


class UnifiedMemoryResolver:
    """Composes the unified memory view over the existing stores.

    Constructed with the three (optionally four) stores the rest of
    the application already wires. Every method is read-only.

    All stores except ``snapshot_store`` are REQUIRED. The snapshot
    store is optional — when absent the resolver falls back to the
    document record's ``active_snapshot_id`` (which is the visibility
    key by contract); the only feature it gates is filling
    ``created_at``/``updated_at`` from the snapshot record.
    """

    def __init__(
        self,
        *,
        registry: "SourceRegistry",
        run_store: "IngestionRunStore",
        artifact_registry: "ArtifactRegistry",
        snapshot_store: "DocumentSnapshotStore | None" = None,
    ) -> None:
        self._registry = registry
        self._run_store = run_store
        self._artifacts = artifact_registry
        self._snapshot_store = snapshot_store

    # ---- Public API ------------------------------------------------

    def resolve_project_active_memory(
        self, ctx: ProjectContext,
    ) -> ProjectActiveMemoryView:
        """Resolve the project-active memory view.

        Iterates every document in the project, projects each into
        a ``DocumentMemoryView``, and rolls up an aggregate. The
        aggregate is queryable iff at least one document is.
        """
        docs = self._registry.list_documents(ctx)
        per_doc: list[DocumentMemoryView] = [
            self._project_document(ctx, doc) for doc in docs
        ]
        # Aggregate verdict — see ProjectActiveMemoryView docstring.
        if any(d.queryable for d in per_doc):
            agg = QueryableStatus.QUERYABLE
            reason: str | None = None
        elif not per_doc:
            agg = QueryableStatus.NOT_STARTED
            reason = (
                "Project has no documents. Upload one to build a "
                "knowledge base."
            )
        elif all(
            d.queryable_status in (
                QueryableStatus.NOT_ATTACHED,
                QueryableStatus.REMOVED,
                QueryableStatus.NOT_STARTED,
            )
            for d in per_doc
        ):
            agg = QueryableStatus.NOT_STARTED
            reason = (
                "No attached document has reached a queryable "
                "snapshot yet."
            )
        else:
            # Mixed failures (compile / artifacts) — pick the most
            # diagnostic failure status so the FE renders something
            # specific.
            if any(
                d.queryable_status == QueryableStatus.MISSING_ARTIFACTS
                for d in per_doc
            ):
                agg = QueryableStatus.MISSING_ARTIFACTS
                reason = (
                    "One or more active snapshots are missing their "
                    "compile artifacts. Re-index the affected document."
                )
            else:
                agg = QueryableStatus.COMPILE_FAILED
                reason = (
                    "No document in this project has a successful "
                    "compile available for query."
                )
        return ProjectActiveMemoryView(
            scope=MemoryScope.PROJECT_ACTIVE,
            project_id=getattr(ctx, "project_id", "") or "",
            queryable_status=agg,
            queryable_reason=reason,
            documents=tuple(per_doc),
        )

    def resolve_document_active_memory(
        self, ctx: ProjectContext, document_id: str,
    ) -> DocumentMemoryView:
        """Resolve the document-active memory view.

        Returns ``QueryableStatus.NOT_STARTED`` (or a more specific
        terminal status) when the document exists but is not
        queryable. Raises nothing — callers translate the status
        themselves.
        """
        try:
            doc = self._registry.get(ctx, document_id)
        except Exception:
            # Treat unknown document the same way the REST layer
            # treats it elsewhere: NOT_STARTED with an actionable
            # reason. Caller (REST adapter) is responsible for
            # 404'ing on the document lookup BEFORE asking the
            # resolver — this branch is the safety belt.
            return DocumentMemoryView(
                scope=MemoryScope.DOCUMENT_ACTIVE,
                project_id=getattr(ctx, "project_id", "") or "",
                document_id=document_id,
                queryable_status=QueryableStatus.NOT_STARTED,
                queryable_reason=(
                    f"document {document_id!r} not found in this project"
                ),
            )
        return self._project_document(ctx, doc)

    def resolve_run_memory(
        self, ctx: ProjectContext, run_id: str,
    ) -> RunMemoryView:
        """Resolve the explicit-run memory view.

        Run-explicit scope is the audit / diagnostic surface. The
        view reports queryable iff the run has a target snapshot
        AND the compile artifacts for that snapshot still resolve.
        """
        run = self._run_store.get(ctx, run_id)
        if run is None:
            return RunMemoryView(
                scope=MemoryScope.RUN_EXPLICIT,
                project_id=getattr(ctx, "project_id", "") or "",
                run_id=run_id,
                queryable_status=QueryableStatus.RUN_UNKNOWN,
                queryable_reason=f"run {run_id!r} not found",
            )
        snapshot_id = getattr(run, "target_snapshot_id", None)
        document_id = getattr(run, "document_id", None)
        if not snapshot_id or not document_id:
            return RunMemoryView(
                scope=MemoryScope.RUN_EXPLICIT,
                project_id=getattr(ctx, "project_id", "") or "",
                run_id=run_id,
                document_id=document_id,
                run_status=str(run.status) if run.status else None,
                run_type=getattr(run, "run_type", None),
                queryable_status=QueryableStatus.NOT_STARTED,
                queryable_reason=(
                    "run has no target snapshot yet — wait for the "
                    "workflow to allocate one"
                ),
            )
        compile_refs = self._compile_artifact_refs(
            ctx, document_id=document_id, snapshot_id=snapshot_id,
        )
        enrichment_refs = self._enrichment_artifact_refs(
            ctx, document_id=document_id, snapshot_id=snapshot_id,
        )
        compile_status = self._compile_status_for_run(run, compile_refs)
        enrichment_status = self._enrichment_status_for_snapshot(
            ctx, document_id=document_id, snapshot_id=snapshot_id,
        )
        # Is this run the producer of the document's active snapshot?
        try:
            doc = self._registry.get(ctx, document_id)
        except Exception:
            doc = None
        is_active = bool(
            doc
            and getattr(doc, "active_snapshot_id", None) == snapshot_id
        )
        status, reason = self._classify_for_run(
            run, compile_refs, compile_status,
        )
        return RunMemoryView(
            scope=MemoryScope.RUN_EXPLICIT,
            project_id=getattr(ctx, "project_id", "") or "",
            run_id=run_id,
            document_id=document_id,
            snapshot_id=snapshot_id,
            run_status=str(run.status) if run.status else None,
            run_type=getattr(run, "run_type", None),
            queryable_status=status,
            queryable_reason=reason,
            compile_status=compile_status,
            compile_artifact_refs=compile_refs,
            enrichment_status=enrichment_status,
            enrichment_artifact_refs=enrichment_refs,
            is_active_for_document=is_active,
        )

    # ---- Internals -------------------------------------------------

    def _project_document(
        self, ctx: ProjectContext, doc: "DocumentRecord",
    ) -> DocumentMemoryView:
        project_id = getattr(ctx, "project_id", "") or ""
        document_id = doc.document_id
        knowledge_state = (
            getattr(doc, "knowledge_state", "attached") or "attached"
        )
        lifecycle = (
            getattr(doc, "lifecycle_status", "stable") or "stable"
        )
        snapshot_id = getattr(doc, "active_snapshot_id", None)

        # 1) Lifecycle / attach gates.
        if knowledge_state == "removed" or lifecycle == "removed":
            return DocumentMemoryView(
                scope=MemoryScope.DOCUMENT_ACTIVE,
                project_id=project_id,
                document_id=document_id,
                queryable_status=QueryableStatus.REMOVED,
                queryable_reason=(
                    "Document has been removed from the knowledge "
                    "base. Re-upload to bring it back."
                ),
            )
        if (
            knowledge_state != "attached"
            or lifecycle in _DISALLOWED_LIFECYCLE
        ):
            return DocumentMemoryView(
                scope=MemoryScope.DOCUMENT_ACTIVE,
                project_id=project_id,
                document_id=document_id,
                queryable_status=QueryableStatus.NOT_ATTACHED,
                queryable_reason=(
                    f"Document is {knowledge_state!r} "
                    f"(lifecycle={lifecycle!r}); attach it to the "
                    "knowledge base before querying."
                ),
            )

        # 2) Active-snapshot pointer.
        if not snapshot_id:
            failed = self._latest_failed_run(ctx, document_id)
            if failed is not None:
                return DocumentMemoryView(
                    scope=MemoryScope.DOCUMENT_ACTIVE,
                    project_id=project_id,
                    document_id=document_id,
                    active_run_id=failed.run_id,
                    run_status=str(failed.status),
                    queryable_status=QueryableStatus.COMPILE_FAILED,
                    queryable_reason=(
                        "The most recent ingestion run failed "
                        "before promoting a snapshot. Re-index the "
                        "document."
                    ),
                    compile_status="failed",
                )
            return DocumentMemoryView(
                scope=MemoryScope.DOCUMENT_ACTIVE,
                project_id=project_id,
                document_id=document_id,
                queryable_status=QueryableStatus.NOT_STARTED,
                queryable_reason=(
                    "No active snapshot yet. The first ingestion run "
                    "has not produced queryable knowledge."
                ),
            )

        # 3) Active snapshot exists — look up the producing run +
        # compile artifacts.
        active_run = self._producing_run_for_snapshot(
            ctx, document_id=document_id, snapshot_id=snapshot_id,
        )
        compile_refs = self._compile_artifact_refs(
            ctx, document_id=document_id, snapshot_id=snapshot_id,
        )
        if not compile_refs:
            return DocumentMemoryView(
                scope=MemoryScope.DOCUMENT_ACTIVE,
                project_id=project_id,
                document_id=document_id,
                snapshot_id=snapshot_id,
                active_run_id=(
                    active_run.run_id if active_run else None
                ),
                run_status=(
                    str(active_run.status) if active_run else None
                ),
                queryable_status=QueryableStatus.MISSING_ARTIFACTS,
                queryable_reason=(
                    "Active snapshot is set but its compile artifacts "
                    "could not be resolved. Re-index the document."
                ),
                compile_status="missing",
            )

        # 4) Enrichment status — optional augmentation.
        enrichment_refs = self._enrichment_artifact_refs(
            ctx, document_id=document_id, snapshot_id=snapshot_id,
        )
        enrichment_status = self._enrichment_status_for_snapshot(
            ctx, document_id=document_id, snapshot_id=snapshot_id,
        )
        if enrichment_status == "succeeded":
            queryable = QueryableStatus.ENRICHMENT_AVAILABLE
        elif enrichment_status == "failed":
            queryable = QueryableStatus.ENRICHMENT_FAILED
        else:
            queryable = QueryableStatus.QUERYABLE

        plan_warnings, unsupported = self._plan_signals_for_run(active_run)
        domain_id = self._domain_id_for_run(active_run)
        created_at, updated_at = self._snapshot_timestamps(
            ctx, snapshot_id=snapshot_id,
        )

        return DocumentMemoryView(
            scope=MemoryScope.DOCUMENT_ACTIVE,
            project_id=project_id,
            document_id=document_id,
            snapshot_id=snapshot_id,
            active_run_id=active_run.run_id if active_run else None,
            run_status=str(active_run.status) if active_run else None,
            compile_status="succeeded",
            compile_artifact_refs=compile_refs,
            domain_id=domain_id,
            enrichment_status=enrichment_status,
            enrichment_artifact_refs=enrichment_refs,
            plan_warnings=plan_warnings,
            unsupported_controls=unsupported,
            created_at=created_at,
            updated_at=updated_at,
            queryable_status=queryable,
        )

    # ---- Store helpers --------------------------------------------

    def _runs_for_document(
        self, ctx: ProjectContext, document_id: str,
    ) -> list["IngestionRun"]:
        try:
            return list(self._run_store.list_runs(
                ctx, document_id=document_id,
            ))
        except Exception:  # noqa: BLE001 — best-effort projection
            return []

    def _producing_run_for_snapshot(
        self, ctx: ProjectContext, *, document_id: str, snapshot_id: str,
    ) -> "IngestionRun | None":
        # Prefer the run that allocated this snapshot. The
        # ``target_snapshot_id`` match is the canonical lineage —
        # the heuristic "latest succeeded" stays as a fallback for
        # legacy runs that pre-date target_snapshot_id.
        runs = self._runs_for_document(ctx, document_id)
        for r in runs:
            if getattr(r, "target_snapshot_id", None) == snapshot_id:
                return r
        # Fallback: snapshot store lookup. Some test wirings record
        # ``created_by_run_id`` even when ``target_snapshot_id`` is
        # missing on the run side.
        if self._snapshot_store is not None:
            try:
                snap = self._snapshot_store.get(ctx, snapshot_id)
            except Exception:  # noqa: BLE001
                snap = None
            if snap is not None:
                producer_run_id = getattr(snap, "created_by_run_id", None)
                if producer_run_id:
                    for r in runs:
                        if r.run_id == producer_run_id:
                            return r
        return None

    def _latest_failed_run(
        self, ctx: ProjectContext, document_id: str,
    ) -> "IngestionRun | None":
        from j1.runs.models import RunStatus
        runs = sorted(
            self._runs_for_document(ctx, document_id),
            key=lambda r: (r.started_at, r.updated_at),
            reverse=True,
        )
        for r in runs:
            if r.status in (RunStatus.FAILED, RunStatus.CANCELLED):
                return r
        return None

    def _compile_artifact_refs(
        self, ctx: ProjectContext, *, document_id: str, snapshot_id: str,
    ) -> tuple[str, ...]:
        refs: list[str] = []
        for kind in _COMPILE_ARTIFACT_KINDS:
            try:
                records = self._artifacts.list_artifacts(ctx, kind=kind)
            except Exception:  # noqa: BLE001
                continue
            for r in records:
                if not self._artifact_matches_snapshot(r, snapshot_id):
                    continue
                sources = getattr(r, "source_document_ids", None) or ()
                if document_id not in sources:
                    continue
                refs.append(r.artifact_id)
        return tuple(refs)

    @staticmethod
    def _artifact_matches_snapshot(record, snapshot_id: str) -> bool:
        """Snapshot match check that tolerates the dual stamping
        production code uses today.

        ``ArtifactRecord.snapshot_id`` is the typed field; the JSON
        registry's reader does not propagate it through the round-
        trip (pre-existing limitation). Production code keeps
        ``metadata["snapshot_id"]`` stamped alongside the typed
        field for exactly this reason. We accept either side."""
        typed = getattr(record, "snapshot_id", None)
        if typed == snapshot_id:
            return True
        meta = getattr(record, "metadata", None) or {}
        return meta.get("snapshot_id") == snapshot_id

    def _enrichment_artifact_refs(
        self, ctx: ProjectContext, *, document_id: str, snapshot_id: str,
    ) -> tuple[str, ...]:
        # ``list_artifacts(kind=...)`` filters by EXACT kind; we
        # don't have a prefix variant, so we list-all once and
        # filter in-process. Cheap for the document-scoped sizes
        # the resolver sees.
        try:
            records = self._artifacts.list_artifacts(ctx)
        except Exception:  # noqa: BLE001
            return ()
        refs: list[str] = []
        for r in records:
            kind = getattr(r, "kind", "")
            if not (
                kind.startswith(_ENRICHMENT_ARTIFACT_KIND_PREFIX)
                or kind == _DOCUMENT_ENRICHMENT_RESULT_KIND
            ):
                continue
            if not self._artifact_matches_snapshot(r, snapshot_id):
                continue
            sources = getattr(r, "source_document_ids", None) or ()
            if document_id not in sources:
                continue
            refs.append(r.artifact_id)
        return tuple(refs)

    def _enrichment_status_for_snapshot(
        self, ctx: ProjectContext, *, document_id: str, snapshot_id: str,
    ) -> str | None:
        """``"succeeded"`` / ``"failed"`` / ``None``.

        Looks for the most recent ``run_domain_enrichment`` run on
        this document that targeted (or sourced from) the given
        snapshot. Returns ``None`` when no manual enrichment has
        ever been attempted for the snapshot.
        """
        from j1.runs.models import RunStatus
        runs = self._runs_for_document(ctx, document_id)
        candidates: list["IngestionRun"] = []
        for r in runs:
            if getattr(r, "run_type", None) != "run_domain_enrichment":
                continue
            # Match by either target snapshot (the candidate this
            # enrichment built) or by the source-snapshot metadata
            # the manual-action endpoint stamps.
            target = getattr(r, "target_snapshot_id", None)
            meta = dict(getattr(r, "metadata", None) or {})
            source = meta.get("manual_action_source_snapshot_id")
            if snapshot_id in (target, source):
                candidates.append(r)
        if not candidates:
            return None
        latest = sorted(
            candidates,
            key=lambda r: (r.started_at, r.updated_at),
            reverse=True,
        )[0]
        if latest.status in (
            RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS,
        ):
            return "succeeded"
        if latest.status in (RunStatus.FAILED, RunStatus.CANCELLED):
            return "failed"
        # In-flight / pending — surface as None until terminal.
        return None

    def _compile_status_for_run(
        self,
        run: "IngestionRun",
        compile_refs: tuple[str, ...],
    ) -> str:
        from j1.runs.models import RunStatus
        if run.status in (RunStatus.FAILED, RunStatus.CANCELLED):
            return "failed"
        if compile_refs:
            return "succeeded"
        if run.status in (
            RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS,
        ):
            # Status says succeeded but artifacts are missing —
            # caller should treat this as ``MISSING_ARTIFACTS``.
            return "missing"
        return "not_started"

    def _classify_for_run(
        self,
        run: "IngestionRun",
        compile_refs: tuple[str, ...],
        compile_status: str,
    ) -> tuple[QueryableStatus, str | None]:
        from j1.runs.models import RunStatus
        if compile_status == "failed":
            return (
                QueryableStatus.COMPILE_FAILED,
                f"run {run.run_id!r} failed before producing usable "
                "compile artifacts",
            )
        if compile_status == "missing":
            return (
                QueryableStatus.MISSING_ARTIFACTS,
                f"run {run.run_id!r} has no compile artifacts in the "
                "registry. Re-index the document.",
            )
        if compile_status == "not_started":
            return (
                QueryableStatus.NOT_STARTED,
                f"run {run.run_id!r} has not produced compile artifacts "
                f"yet (status={run.status!s})",
            )
        return (QueryableStatus.QUERYABLE, None)

    def _plan_signals_for_run(
        self, run: "IngestionRun | None",
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if run is None:
            return ((), ())
        meta = dict(getattr(run, "metadata", None) or {})
        warnings = meta.get("plan_warnings") or meta.get("warnings") or ()
        unsupported = meta.get("unsupported_profile_controls") or ()
        return (
            tuple(str(w) for w in warnings),
            tuple(str(u) for u in unsupported),
        )

    def _domain_id_for_run(
        self, run: "IngestionRun | None",
    ) -> str | None:
        if run is None:
            return None
        meta = dict(getattr(run, "metadata", None) or {})
        domain_id = (
            meta.get("domain_id")
            or meta.get("selected_domain_id")
            or meta.get("manual_action_domain_id")
        )
        return str(domain_id) if domain_id else None

    def _snapshot_timestamps(
        self, ctx: ProjectContext, *, snapshot_id: str,
    ) -> tuple[datetime | None, datetime | None]:
        if self._snapshot_store is None:
            return (None, None)
        try:
            snap = self._snapshot_store.get(ctx, snapshot_id)
        except Exception:  # noqa: BLE001
            return (None, None)
        if snap is None:
            return (None, None)
        return (
            getattr(snap, "created_at", None),
            getattr(snap, "promoted_at", None) or getattr(snap, "updated_at", None),
        )
