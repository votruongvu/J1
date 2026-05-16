"""Provider-neutral LLM role abstraction.

Three roles ship with the framework: text, vision, embedding. Each
role has a small Protocol; concrete implementations live in
`j1.llm.openai_compat` (the bundled OpenAI-compatible client) or
`j1.llm.langchain_adapter` (optional, lazy-imports `langchain-core`).

A single `LLMProviderRegistry` resolves clients by role string. The
composition root constructs the registry once and hands it to every
adapter — no adapter reads env vars directly.
"""

from j1.llm.classloader import (
    CHAT_MODEL_CATALOG,
    EMBEDDING_CATALOG,
    register_trusted_prefix,
    resolve_callable,
    resolve_chat_model,
    resolve_embedding_model,
)
from j1.llm.clients import (
    EmbeddingClient,
    LLMCapabilityError,
    LLMUsage,
    TextLLMClient,
    VisionLLMClient,
)
from j1.llm.errors import (
    LLMConfigError,
    LLMContextOverflowError,
    LLMError,
    LLMProviderUnavailable,
    LLMRoleNotRegistered,
)
from j1.llm.langchain_adapter import (
    LangChainEmbeddingClient,
    LangChainTextLLMClient,
    LangChainVisionLLMClient,
)
from j1.llm.openai_compat import (
    OpenAICompatEmbeddingClient,
    OpenAICompatTextLLMClient,
    OpenAICompatVisionLLMClient,
)
from j1.llm.registry import (
    KNOWN_ROLES,
    LLM_ROLE_EMBEDDING,
    LLM_ROLE_ENRICHMENT,
    LLM_ROLE_FAST,
    LLM_ROLE_INDEXING,
    LLM_ROLE_PREMIUM,
    LLM_ROLE_QUERY,
    LLM_ROLE_TEXT,
    LLM_ROLE_VISION,
    LLMProviderRegistry,
)
from j1.llm.settings import (
    PROVIDER_LANGCHAIN,
    PROVIDER_OPENAI_COMPAT,
    SUPPORTED_PROVIDERS,
    EmbeddingSettings,
    EnrichmentLLMSettings,
    FastLLMSettings,
    IndexingLLMSettings,
    LLMSettings,
    QueryLLMSettings,
    TextLLMSettings,
    VisionLLMSettings,
    load_llm_settings,
)

__all__ = [
    "CHAT_MODEL_CATALOG",
    "EMBEDDING_CATALOG",
    "EmbeddingClient",
    "EmbeddingSettings",
    "EnrichmentLLMSettings",
    "FastLLMSettings",
    "IndexingLLMSettings",
    "KNOWN_ROLES",
    "LLM_ROLE_EMBEDDING",
    "LLM_ROLE_ENRICHMENT",
    "LLM_ROLE_FAST",
    "LLM_ROLE_INDEXING",
    "LLM_ROLE_PREMIUM",
    "LLM_ROLE_QUERY",
    "LLM_ROLE_TEXT",
    "LLM_ROLE_VISION",
    "LLMCapabilityError",
    "LLMConfigError",
    "LLMContextOverflowError",
    "LLMError",
    "LLMProviderRegistry",
    "LLMProviderUnavailable",
    "LLMRoleNotRegistered",
    "LLMSettings",
    "LLMUsage",
    "LangChainEmbeddingClient",
    "LangChainTextLLMClient",
    "LangChainVisionLLMClient",
    "OpenAICompatEmbeddingClient",
    "OpenAICompatTextLLMClient",
    "OpenAICompatVisionLLMClient",
    "PROVIDER_LANGCHAIN",
    "PROVIDER_OPENAI_COMPAT",
    "QueryLLMSettings",
    "SUPPORTED_PROVIDERS",
    "TextLLMClient",
    "TextLLMSettings",
    "VisionLLMClient",
    "VisionLLMSettings",
    "load_llm_settings",
    "register_trusted_prefix",
    "resolve_callable",
    "resolve_chat_model",
    "resolve_embedding_model",
]
