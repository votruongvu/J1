"""Composition root.

One module that:
  * loads settings (LLM + RAGAnything + Graphify + enrichment + processing
    defaults)
  * constructs LLM clients per role
  * registers them in `LLMProviderRegistry`
  * constructs and registers compiler / graph / retrieval providers
  * validates required roles for the selected providers (clear startup
    errors when something's missing)
  * publishes secrets-safe diagnostics

Used by both the API entrypoint and the worker entrypoint, so they
agree on what's wired. Tests build a `Bootstrap` directly with fake
clients to skip env-driven construction.
"""

from j1.compose.bootstrap import (
    Bootstrap,
    BootstrapResult,
    EnrichmentSettings,
    ProcessingSelection,
    bootstrap_from_env,
    load_enrichment_settings,
    load_processing_selection,
)
from j1.compose.diagnostics import (
    ProviderDiagnostics,
    StartupDiagnostics,
    render_startup_diagnostics,
)

__all__ = [
    "Bootstrap",
    "BootstrapResult",
    "EnrichmentSettings",
    "ProcessingSelection",
    "ProviderDiagnostics",
    "StartupDiagnostics",
    "bootstrap_from_env",
    "load_enrichment_settings",
    "load_processing_selection",
    "render_startup_diagnostics",
]
