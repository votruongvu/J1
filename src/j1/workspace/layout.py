from enum import StrEnum

TENANTS_DIR = "tenants"
PROJECTS_DIR = "projects"


class WorkspaceArea(StrEnum):
    RAW = "raw"
    COMPILED = "compiled"
    ENRICHED = "enriched"
    GRAPH = "graph"
    SEARCH = "search"
    AUDIT = "audit"
    RUNTIME = "runtime"
    # Post-ingestion validation: stores generated validation sets +
    # validation run records. Durable — losing it loses
    # tester history, generated test cases, and verdict notes.
    VALIDATION = "validation"


# Backup classification: which areas hold authoritative state and which can
# be regenerated from authoritative state.
#
# DURABLE areas: must be included in any backup. Loss is permanent.
# REBUILDABLE areas: derived from durable state and can be reconstructed
#  (e.g., the search index can be rebuilt from `compiled/`/`enriched/`/
#  `graph/` artifacts).
DURABLE_AREAS: frozenset[WorkspaceArea] = frozenset({
    WorkspaceArea.RAW,
    WorkspaceArea.COMPILED,
    WorkspaceArea.ENRICHED,
    WorkspaceArea.GRAPH,
    WorkspaceArea.AUDIT,
    WorkspaceArea.RUNTIME,
    WorkspaceArea.VALIDATION,
})

REBUILDABLE_AREAS: frozenset[WorkspaceArea] = frozenset({
    WorkspaceArea.SEARCH,
})


def is_durable(area: WorkspaceArea) -> bool:
    return area in DURABLE_AREAS


def is_rebuildable(area: WorkspaceArea) -> bool:
    return area in REBUILDABLE_AREAS
