"""Graphify provider — optional alternative graph builder.

Selected via `J1_DEFAULT_GRAPH_PROVIDER=graphify` (and only when
`J1_GRAPHIFY_ENABLED=true`). Uses the lazy-import pattern: the
constructor raises `ProviderUnavailable` with a clear install hint
when `graphify` isn't on `sys.path`.
"""

from j1.providers.graphify.graph import (
    PROVIDER_NAME,
    GraphifyGraphBuilder,
)
from j1.providers.graphify.settings import (
    ENV_GRAPHIFY_COMMAND,
    ENV_GRAPHIFY_ENABLED,
    ENV_GRAPHIFY_MODE,
    ENV_GRAPHIFY_WORKDIR,
    GraphifySettings,
    load_graphify_settings,
)

__all__ = [
    "ENV_GRAPHIFY_COMMAND",
    "ENV_GRAPHIFY_ENABLED",
    "ENV_GRAPHIFY_MODE",
    "ENV_GRAPHIFY_WORKDIR",
    "GraphifyGraphBuilder",
    "GraphifySettings",
    "PROVIDER_NAME",
    "load_graphify_settings",
]
