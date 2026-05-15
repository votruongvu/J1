"""Unified memory projection — the logical read model for query.

See [docs/unified-memory-contract.md](../../../docs/unified-memory-contract.md).

The query layer reads through ``UnifiedMemoryResolver`` instead of
piecing together "what is currently queryable for this scope" from
``DocumentRecord``, ``IngestionRun``, the artifact registry, and the
snapshot store. The physical storage stays split; only the read
shape is unified.
"""

from j1.memory.aliases import (
    AliasResolution,
    AliasResolver,
    ENTITY_ALIAS_SOURCE_DOMAIN_CONFIG,
    ENTITY_ALIAS_SOURCE_DOMAIN_ENRICHMENT,
    EntityAlias,
)
from j1.memory.augmentation import (
    AugmentationHints,
    DomainPackAugmentationProvider,
    DomainQueryAugmentationProvider,
    ENV_DOMAIN_QUERY_AUGMENTATION_ENABLED,
    MAX_QUERY_EXPANSION_TERMS,
    NoOpAugmentationProvider,
    compute_query_expansion,
    is_augmentation_enabled,
)
from j1.memory.graph_expansion import (
    ExpansionCandidate,
    ExpansionRequest,
    ExpansionResult,
    GraphExpansionService,
    UnsupportedGraphExpansion,
)
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
    "AliasResolution",
    "AliasResolver",
    "AugmentationHints",
    "DocumentMemoryView",
    "DomainPackAugmentationProvider",
    "DomainQueryAugmentationProvider",
    "ENTITY_ALIAS_SOURCE_DOMAIN_CONFIG",
    "ENTITY_ALIAS_SOURCE_DOMAIN_ENRICHMENT",
    "ENV_DOMAIN_QUERY_AUGMENTATION_ENABLED",
    "EntityAlias",
    "ExpansionCandidate",
    "ExpansionRequest",
    "ExpansionResult",
    "GraphExpansionService",
    "MAX_QUERY_EXPANSION_TERMS",
    "MemoryNotQueryableError",
    "MemoryScope",
    "NoOpAugmentationProvider",
    "ProjectActiveMemoryView",
    "QueryableStatus",
    "RunMemoryView",
    "UnifiedMemoryResolver",
    "UnifiedMemoryView",
    "UnsupportedGraphExpansion",
    "compute_query_expansion",
    "is_augmentation_enabled",
]
