"""RAGAnything provider adapters.

Implements the framework's `KnowledgeCompiler`, `GraphBuilder`, and
`QueryProvider` Protocols on top of the external `raganything` library.

Constructor pattern:

 from j1 import LLMProviderRegistry
 from j1.providers.raganything import RAGAnythingCompiler

 compiler = RAGAnythingCompiler.from_default(
 llm_registry=registry,
 settings=raganything_settings,
 )

`from_default` lazily imports `raganything` and raises
`ProviderUnavailable("install raganything")` if the library is not
installed — the framework's own tests don't need the dep.

Tests inject a fake by passing a callable directly:

 compiler = RAGAnythingCompiler(
 llm_registry=registry,
 settings=raganything_settings,
 compile_callable=my_fake,
 )
"""

from j1.providers.raganything.compiler import (
    PROVIDER_NAME,
    RAGAnythingCompiler,
)
from j1.providers.raganything.graph import RAGAnythingGraphBuilder
from j1.providers.raganything.retrieval import RAGAnythingQueryProvider
from j1.providers.raganything.settings import (
    ENV_RAGANYTHING_CACHE_DIR,
    ENV_RAGANYTHING_STORAGE_DIR,
    ENV_RAGANYTHING_WORKDIR,
    RAGAnythingSettings,
    load_raganything_settings,
)

__all__ = [
    "ENV_RAGANYTHING_CACHE_DIR",
    "ENV_RAGANYTHING_STORAGE_DIR",
    "ENV_RAGANYTHING_WORKDIR",
    "PROVIDER_NAME",
    "RAGAnythingCompiler",
    "RAGAnythingGraphBuilder",
    "RAGAnythingQueryProvider",
    "RAGAnythingSettings",
    "load_raganything_settings",
]
