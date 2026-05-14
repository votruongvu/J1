"""Snapshot-centered metadata model — Phase 1 foundation types.

Why this exists
---------------
The current run_id-centered model causes correctness bugs: artifacts
and indexes are scoped by ``run_id``, which means a second run that
hasn't fully succeeded can leak into queries against the "good"
data, and a query never knows which run's state it is reading.

The target rule (Phase 1 introduces the types; Phase 2 wires them
through the ingestion lifecycle):

  * ``Document.active_snapshot_id`` controls query visibility.
  * ``run_id`` must NOT control query visibility.
  * ``run_id`` must NOT be the main storage namespace.
  * ``run_id`` only identifies the execution that created a snapshot.
  * Where a run lineage matters, store it as ``created_by_run_id``.

This module defines the canonical shapes. It does NOT modify
``DocumentRecord`` or ``IngestionRun`` yet — those changes happen in
Phase 2 after the ingestion lifecycle is rewritten. Phase 1 ships
the types so the docker-compose stack + provider config + storage
adapters can be designed against them.

Data flow
---------
A ``DocumentSnapshot`` is created by an ``IngestionRun`` once
compile / enrich / graph all succeed. The snapshot owns its own
artifact-id set and its own ``IndexRef`` entries (vector / graph /
evidence). Queries read EXCLUSIVELY through the snapshot; the run
is execution metadata.

States
~~~~~~
* ``building``   — run is producing the snapshot, not query-visible yet
* ``ready``      — snapshot can be promoted to ``active_snapshot_id``
* ``failed``     — run aborted; snapshot will never become active
* ``superseded`` — a later snapshot took over as ``active``;
                   superseded snapshots are kept for rollback / audit
                   until cleanup retention expires

Promotion
~~~~~~~~~
``Document.active_snapshot_id`` is set to a ``ready`` snapshot ID.
The previous active snapshot transitions to ``superseded``. Promotion
is the ONLY operation that changes query visibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


# ---- Enums ----------------------------------------------------


class SnapshotState(StrEnum):
    """Lifecycle of a single ``DocumentSnapshot``."""

    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class IndexKind(StrEnum):
    """Coarse index categories — each backed by a different provider."""

    VECTOR = "vector"           # e.g. Qdrant collection / namespace
    GRAPH = "graph"             # e.g. Neo4j database / subgraph label
    EVIDENCE = "evidence"       # e.g. Postgres FTS partition
    RAG = "rag"                 # composite RAGAnything workspace path


# ---- Index reference ------------------------------------------


@dataclass(frozen=True)
class IndexRef:
    """A pointer to one physical index that belongs to a snapshot.

    ``location`` is provider-specific: a Qdrant collection name, a
    Postgres partition, a directory path for the RAGAnything bridge,
    etc. The framework treats it as an opaque handle — only the
    provider adapter knows how to read or delete it.

    Carrying the ``provider`` string alongside the ``kind`` lets the
    cleanup path dispatch to the right adapter without inspecting
    ``location``."""

    snapshot_id: str
    kind: IndexKind
    provider: str            # e.g. "qdrant" | "neo4j" | "postgres_fts" | "raganything"
    location: str            # provider-specific handle (collection / path / partition)
    stats: dict[str, Any] = field(default_factory=dict)


# ---- Snapshot ------------------------------------------------


@dataclass(frozen=True)
class DocumentSnapshot:
    """A versioned, queryable knowledge state for one document.

    Identity: ``snapshot_id`` is unique across the tenant + project
    + document. The framework treats it as the strict namespacing
    key — providers carve their storage by ``snapshot_id``, not by
    ``run_id``.

    Lineage: ``created_by_run_id`` records the run that built the
    snapshot. Operators answer "which run made this snapshot" by
    reading the field; the runtime never asks the inverse question
    ("which snapshot is active for this run") — visibility is
    snapshot-driven, not run-driven.
    """

    snapshot_id: str
    document_id: str
    tenant_id: str
    project_id: str
    created_by_run_id: str
    state: SnapshotState
    created_at: datetime
    promoted_at: datetime | None = None
    superseded_at: datetime | None = None
    # Provider-resolved locations. Empty tuple while in BUILDING.
    index_refs: tuple[IndexRef, ...] = ()
    # Free-form stats: artifact counts, chunk counts, byte sizes,
    # whatever the projector wants to surface. NOT load-bearing.
    summary: dict[str, Any] = field(default_factory=dict)


# ---- Model extension proposals --------------------------------
#
# Phase 2 will add these fields to existing types. The proposals
# live here so the migration path is documented in one place.
#
#  * ``DocumentRecord.active_snapshot_id: str | None``
#      — None means the document has no usable snapshot yet
#      — query layer reads documents with active_snapshot_id != None
#      — promotion sets this field and demotes the previous value
#        to a snapshot with state=SUPERSEDED
#
#  * ``IngestionRun.target_snapshot_id: str | None``
#      — set when the run starts (the snapshot_id it is building)
#      — None for legacy runs that ran before the refactor
#      — the run can switch this to a different snapshot_id only
#        before promotion (re-target on resume / replay)
#
#  * ``Artifact.snapshot_id: str | None``
#      — the snapshot this artifact belongs to
#      — None during the building window before assignment;
#        snapshot creation moves an artifact's snapshot_id from
#        None to the run's target_snapshot_id atomically
#      — replaces today's metadata["run_id"] as the lineage key
#      — metadata still keeps created_by_run_id for audit
#
#  * ``IndexRef`` rows live in the metadata store keyed on
#    (snapshot_id, kind, provider). Phase 2 will pick a table
#    name (proposal: ``j1.snapshot_index_refs``).
#
# This module is intentionally pure types — no I/O — so Phase 2 can
# import without dragging in storage adapters.


__all__ = [
    "DocumentSnapshot",
    "IndexKind",
    "IndexRef",
    "SnapshotState",
]
