"""Unified memory projection — the logical read model for query.

See [docs/unified-memory-contract.md](../../../docs/unified-memory-contract.md).

The query layer reads through ``UnifiedMemoryResolver`` instead of
piecing together "what is currently queryable for this scope" from
``DocumentRecord``, ``IngestionRun``, the artifact registry, and the
snapshot store. The physical storage stays split; only the read
shape is unified.
"""

from j1.memory.unified import (
    DocumentMemoryView,
    MemoryNotQueryableError,
    MemoryScope,
    ProjectActiveMemoryView,
    QueryableStatus,
    RunMemoryView,
    UnifiedMemoryResolver,
    UnifiedMemoryView,
)

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
