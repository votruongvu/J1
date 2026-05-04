"""J1 extension layer — contracts, primitives, manifest, and registry
that adapters / connectors / providers / domain policies plug into.

Public surface (recommended import path):

    from j1.extension import (
        # Contracts
        SourceConnector, CompilerAdapter, EnrichmentAdapter,
        GraphAdapter, RetrievalAdapter, RerankerAdapter,
        LLMProviderAdapter, EmbeddingProviderAdapter,
        VisionProviderAdapter, OutputFormatter,
        EvaluationAdapter, DomainPolicy,
        # Primitives
        Source, SourceMetadata, Document, Artifact, Chunk,
        Collection, Evidence, Citation, RetrievalResult,
        GraphNode, GraphEdge, WorkflowState, ProviderConfig,
        EvaluationResult,
        # Infrastructure
        AdapterManifest, ManifestError,
        CapabilityRegistry, RegistryEntry, RegistryError,
    )

The extension layer is **additive** — it does not replace the legacy
core protocols under `j1.processing.contracts` or the role-based
LLM clients under `j1.llm.clients`. Implementations satisfying the
extension contracts compose with both surfaces.

Note on naming collisions: `CompilerAdapter` and `GraphAdapter` are
also names in the `j1.connectors` legacy package (with a different
shape — they're the adapter-pattern wrappers used by
`ExternalKnowledgeCompiler` / `ExternalGraphBuilder`). To keep
imports unambiguous, the extension contracts are NOT re-exported
from the top-level `j1.__init__` namespace. Always import them via
`from j1.extension import ...` or `from j1.extension.contracts
import ...`.
"""

from j1.extension.contracts import (
    CompilerAdapter,
    DomainPolicy,
    EmbeddingProviderAdapter,
    EnrichmentAdapter,
    EvaluationAdapter,
    GraphAdapter,
    LLMProviderAdapter,
    OutputFormatter,
    RerankerAdapter,
    RetrievalAdapter,
    SourceConnector,
    VisionProviderAdapter,
)
from j1.extension.manifest import (
    KNOWN_ADAPTER_TYPES,
    AdapterManifest,
    ManifestError,
)
from j1.extension.primitives import (
    Artifact,
    ArtifactDraft,
    ArtifactProcessingResult,
    Chunk,
    Citation,
    Collection,
    Document,
    EvaluationResult,
    Evidence,
    GraphEdge,
    GraphNode,
    GraphPath,
    ModelResponse,
    ProcessingResult,
    ProjectContext,
    ProviderConfig,
    QueryRequest,
    QueryResponse,
    QueryResult,
    ResultStatus,
    RetrievalResult,
    Source,
    SourceMetadata,
    SourceReference,
    WorkflowState,
)
from j1.extension.registry import (
    CapabilityRegistry,
    RegistryEntry,
    RegistryError,
)

__all__ = [
    # Contracts
    "CompilerAdapter",
    "DomainPolicy",
    "EmbeddingProviderAdapter",
    "EnrichmentAdapter",
    "EvaluationAdapter",
    "GraphAdapter",
    "LLMProviderAdapter",
    "OutputFormatter",
    "RerankerAdapter",
    "RetrievalAdapter",
    "SourceConnector",
    "VisionProviderAdapter",
    # Primitives — new
    "Chunk",
    "Citation",
    "Collection",
    "Evidence",
    "EvaluationResult",
    "GraphEdge",
    "GraphNode",
    "ProviderConfig",
    "RetrievalResult",
    "Source",
    "SourceMetadata",
    "WorkflowState",
    # Primitives — re-exports of existing core types
    "Artifact",
    "ArtifactDraft",
    "ArtifactProcessingResult",
    "Document",
    "GraphPath",
    "ModelResponse",
    "ProcessingResult",
    "ProjectContext",
    "QueryRequest",
    "QueryResponse",
    "QueryResult",
    "ResultStatus",
    "SourceReference",
    # Manifest
    "AdapterManifest",
    "KNOWN_ADAPTER_TYPES",
    "ManifestError",
    # Registry
    "CapabilityRegistry",
    "RegistryEntry",
    "RegistryError",
]
