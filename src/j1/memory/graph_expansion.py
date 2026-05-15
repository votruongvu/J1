"""Graph expansion service contract.

Phase-4 seam for optional graph-aware retrieval. Defines the
protocol the query orchestrator will consult to ask "given these
entry candidates, can you walk the knowledge graph one or two hops
and surface related nodes?"

The contract is intentionally minimal:

* Implementations report explicit ``supported=False`` when the
  backing graph engine cannot do n-hop expansion. The orchestrator
  surfaces that signal in diagnostics rather than faking a
  traversal by joining unrelated chunks.

* Hop count is the only knob today. Future variants (filter by
  entity type, weight by edge confidence, etc.) extend the
  ``ExpansionRequest`` dataclass without breaking the protocol.

* The default ``UnsupportedGraphExpansion`` impl is what the
  orchestrator wires when no real graph adapter has been
  registered. It always reports unsupported and returns no
  candidates. This is the honest default — RAGAnything / LightRAG
  does not currently expose a stable n-hop API J1 can call from
  outside the compile / aquery call.

Wiring strategy: the orchestrator looks up the service via DI at
construction time. A deployment that gains a real graph backend
later (Neo4j adapter, custom LightRAG fork, etc.) registers the
real service WITHOUT touching the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


__all__ = [
    "ExpansionCandidate",
    "ExpansionRequest",
    "ExpansionResult",
    "GraphExpansionService",
    "UnsupportedGraphExpansion",
]


@dataclass(frozen=True)
class ExpansionCandidate:
    """One graph-expansion output node.

    The orchestrator translates ``artifact_id`` / ``chunk_id`` into
    retrieval candidates. ``hop_distance`` is the number of edges
    traversed from the entry set — 0 means the candidate WAS in the
    entry set; the service may include it for completeness."""

    artifact_id: str
    chunk_id: str | None = None
    hop_distance: int = 1
    relation_type: str | None = None
    score: float = 0.0


@dataclass(frozen=True)
class ExpansionRequest:
    """Input to ``GraphExpansionService.expand``.

    Pure data. The orchestrator builds this from the retrieval-stage
    entry candidates + the user-configured hop budget. Implementations
    MUST treat the request as a frozen snapshot — no mutation, no
    follow-up calls."""

    document_id: str
    snapshot_id: str
    entry_artifact_ids: tuple[str, ...] = ()
    max_hops: int = 1
    max_candidates: int = 32

    def __post_init__(self) -> None:
        # Belt-and-suspenders: a misconfigured caller asking for 0
        # candidates or negative hops would let unbounded fan-out
        # slip through if downstream tries to coerce. Reject early.
        if self.max_hops < 0:
            raise ValueError("max_hops must be >= 0")
        if self.max_candidates < 1:
            raise ValueError("max_candidates must be >= 1")


@dataclass(frozen=True)
class ExpansionResult:
    """Output of ``GraphExpansionService.expand``.

    Carries the candidate list + a diagnostic block the orchestrator
    surfaces via ``QueryTrace`` so the FE can render
    ``graph_expansion_supported`` and ``graph_hop_count`` honestly.

    ``supported`` is the load-bearing flag. When ``False``, the
    orchestrator MUST NOT pretend the graph was consulted —
    ``unsupported_reason`` carries the operator-readable hint."""

    supported: bool
    candidates: tuple[ExpansionCandidate, ...] = ()
    hop_count: int = 0
    unsupported_reason: str | None = None
    warnings: tuple[str, ...] = ()

    def to_diagnostic(self) -> dict[str, object]:
        """Shape the orchestrator embeds in its ``QueryTrace``."""
        return {
            "graph_expansion_supported": self.supported,
            "graph_hop_count": self.hop_count,
            "graph_expansion_candidate_count": len(self.candidates),
            "graph_expansion_unsupported_reason": self.unsupported_reason,
            "graph_expansion_warnings": list(self.warnings),
        }


class GraphExpansionService(Protocol):
    """Stable contract every graph expansion impl satisfies.

    The protocol is small on purpose: there is exactly one method,
    ``expand``, and it is synchronous + pure-ish (no side effects
    other than reading the underlying graph). Implementations are
    free to be sync or async-via-wrapper; the orchestrator wraps
    them via run_in_executor when threaded into an async path.
    """

    def expand(self, request: ExpansionRequest) -> ExpansionResult:
        """Walk the graph from ``request.entry_artifact_ids`` up to
        ``request.max_hops`` and return at most
        ``request.max_candidates``. Implementations MUST cap at
        the request's limits; the orchestrator does not enforce
        them itself."""
        ...


class UnsupportedGraphExpansion:
    """Default impl wired when no real graph backend is registered.

    Always reports ``supported=False``. Returns no candidates. The
    orchestrator surfaces the reason verbatim so operators see
    "graph expansion not configured" rather than a silent zero.

    Why this is the default: RAGAnything / LightRAG's per-snapshot
    workspace does run graph reasoning internally during ``aquery``,
    but there is no stable external API for J1 to call a separate
    n-hop walk. Faking it by joining unrelated chunks would
    contaminate the evidence pack and break grounding — so the
    honest path is to declare it unsupported until a stable adapter
    exists.
    """

    def __init__(
        self,
        *,
        reason: str = (
            "graph_expansion_not_configured: the active backend does "
            "not expose a stable n-hop API; results would not be "
            "grounded. Wire a GraphExpansionService implementation "
            "to enable."
        ),
    ) -> None:
        self._reason = reason

    def expand(self, request: ExpansionRequest) -> ExpansionResult:
        # The request validates itself on construction; we don't
        # echo it here — the diagnostic only needs the "no, can't
        # do that" signal.
        return ExpansionResult(
            supported=False,
            candidates=(),
            hop_count=0,
            unsupported_reason=self._reason,
        )
