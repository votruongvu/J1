"""Vendor-specific provider adapters.

Each subpackage implements one or more existing framework Protocols
(`KnowledgeCompiler`, `GraphBuilder`, `QueryProvider`) on top of an
external library. The vendor library is **lazily imported** at
adapter construction; if it isn't installed the constructor raises
`LLMProviderUnavailable` (or a wrapping `J1Error`) with a clear pip
hint, so the framework's own test suite stays hermetic.

What ships today:
  * `j1.providers.raganything` — RAGAnything-backed compiler + graph + retrieval
  * `j1.providers.graphify`    — Graphify-backed graph builder (optional)
"""

from j1.providers.errors import ProviderUnavailable

__all__ = ["ProviderUnavailable"]
