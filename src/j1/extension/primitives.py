"""Canonical extension-layer primitives.

These are the value objects that flow across J1's extension contracts
(`j1.extension.contracts`). They are deliberately:

 * **Domain-neutral** — no industry / customer / vendor vocabulary.
 * **Frozen dataclasses** — easy to serialise (Temporal payloads,
 JSON snapshots, debug logs).
 * **Re-exports of existing core types where they map cleanly** —
 so the extension layer stays consistent with the rest of J1
 instead of duplicating shapes.

Adding a new primitive here is allowed iff it is generic and
appears in at least one extension contract method signature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Re-exports — existing core primitives that are already canonical.
from j1.artifacts.models import ArtifactRecord  # noqa: F401
from j1.documents.models import DocumentRecord, SourceDocument  # noqa: F401
from j1.processing.results import (  # noqa: F401
    ArtifactDraft,
    ArtifactProcessingResult,
    ModelResponse,
    ProcessingResult,
    QueryResult,
)
from j1.processing.status import ResultStatus  # noqa: F401
from j1.projects.context import ProjectContext  # noqa: F401

# Legacy ``j1.query.models`` (QueryRequest / QueryResponse /
# GraphPath / SourceReference) was removed when the
# SmartQueryOrchestrator rolled out. Extension connectors that
# previously imported those types should migrate to the
# orchestrator's ``OrchestratorRequest`` / ``QueryTrace`` shapes.


# ---- Source-side primitives -----------------------------------------


@dataclass(frozen=True)
class SourceMetadata:
    """Transport-neutral description of where a document came from.

 Connectors fill this in when they fetch a `Source`. The framework
 treats `extra` as opaque — connectors stash whatever they need
 (etags, http response headers, S3 versionId, …) and J1 stores it
 on `DocumentRecord.metadata`.
 """

    uri: str
    content_type: str | None = None
    title: str | None = None
    fetched_at: str | None = None  # ISO-8601 timestamp
    checksum: str | None = None    # `sha256:<hex>` if known
    size_bytes: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Source:
    """A document plus its bytes, as returned by a `SourceConnector`.

 `content` is the raw payload. `metadata` carries the transport
 description. The framework registers the source via
 `DocumentIntakeService` and obtains a `DocumentRecord` in return.
 """

    content: bytes
    metadata: SourceMetadata


# ---- Document / chunk primitives ------------------------------------


# `Document` is the public alias for J1's existing `DocumentRecord`.
# Kept as a name to mirror the user-facing terminology ("Document" is
# what callers think in; `DocumentRecord` is the storage record).
Document = DocumentRecord


# `Artifact` is the public alias for `ArtifactRecord` — same reasoning.
Artifact = ArtifactRecord


@dataclass(frozen=True)
class Chunk:
    """An ordered, citation-addressable slice of a `Document`.

 Compilers / chunkers emit `Chunk`s. Indexers and retrieval
 adapters consume them.

 `chunk_id` is content-stable when the inputs are stable
 (recommended: `sha256:<hex>` of `content + position`).
 `position` lets a client reconstruct order without needing
 monotonic id numbers.
 """

    chunk_id: str
    document_id: str
    content: bytes
    position: int
    content_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Collection:
    """A named, project-scoped grouping of documents.

 Connectors and retrieval adapters can scope work to a collection
 (e.g. "fetch into `release-notes`", "retrieve only from `policies`").
 The framework treats collection names as opaque labels.
 """

    name: str
    description: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---- Retrieval primitives -------------------------------------------


@dataclass(frozen=True)
class Citation:
    """A pointer back to the supporting source for a piece of evidence.

 `locator` is whatever the source provider hands back as a
 "you can find this here" reference: a chunk id, a page number,
 a URL fragment, a byte range, etc. The framework does not
 interpret it.
 """

    document_id: str
    locator: str | None = None
    snippet: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Evidence:
    """A retrieved unit that carries one or more citations.

 Retrieval adapters return `Evidence` items inside a
 `RetrievalResult`. Higher layers (output formatters, evaluators)
 consume them.
 """

    content: str
    score: float
    citations: list[Citation] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalResult:
    """The canonical retrieval-adapter output.

 Strictly richer than `QueryResult` (which is the legacy
 answer-shape used by the existing `QueryProvider` Protocol):
 `RetrievalResult` returns evidence; the formatter / synthesizer
 builds the final answer.
 """

    status: ResultStatus
    evidences: list[Evidence] = field(default_factory=list)
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---- Graph primitives -----------------------------------------------


@dataclass(frozen=True)
class GraphNode:
    """A typed graph node. Generic — no fixed schema for properties."""

    node_id: str
    type: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphEdge:
    """A typed directed edge between two `GraphNode`s."""

    source_id: str
    target_id: str
    type: str
    properties: dict[str, Any] = field(default_factory=dict)


# ---- Workflow / config primitives -----------------------------------


@dataclass(frozen=True)
class WorkflowState:
    """Lightweight workflow state snapshot for orchestration callers.

 The Temporal-side `WorkflowStatus` is richer; this is the generic,
 transport-neutral shape adapters and evaluators consume.

 `phase` is the *role label* (e.g. "compile", "retrieve") — NOT a
 " / " milestone. The string set is open; document
 your conventions in your own deployment.
 """

    workflow_id: str
    role: str
    state: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderConfig:
    """Generic provider config carried by a manifest or registry entry.

 `name` and `type` identify the adapter; `options` is the
 plain-dict configuration (no secrets — secrets live in
 `secrets_ref` and resolution is the deployment's job).
 """

    name: str
    type: str
    version: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    secrets_ref: dict[str, str] = field(default_factory=dict)


# ---- Evaluation primitives ------------------------------------------


@dataclass(frozen=True)
class EvaluationResult:
    """Canonical output of an `EvaluationAdapter`.

 `score` is a 0..1 normalised score; `passed` is the boolean
 verdict the adapter assigns; `findings` is a free-form list the
 adapter populates with whatever it wants to surface (anomalies,
 rule failures, …).
 """

    status: ResultStatus
    score: float | None = None
    passed: bool | None = None
    findings: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = [
    # Re-exported existing primitives
    "Artifact",
    "ArtifactDraft",
    "ArtifactProcessingResult",
    "ArtifactRecord",
    "Document",
    "DocumentRecord",
    "GraphPath",
    "ModelResponse",
    "ProcessingResult",
    "ProjectContext",
    "QueryRequest",
    "QueryResponse",
    "QueryResult",
    "ResultStatus",
    "SourceDocument",
    "SourceReference",
    # New extension primitives
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
]
