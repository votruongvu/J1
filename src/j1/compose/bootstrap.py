"""Composition root: build LLM clients + provider registries from env.

`bootstrap_from_env()` returns a fully wired `BootstrapResult` (LLM
registry + compiler / graph / retrieval registries + diagnostics)
that both the API entrypoint and the worker entrypoint use. Tests
construct `Bootstrap` directly with a custom env mapping to control
every knob.

Validation rules (from the architecture spec):

  * Selected compiler MUST be registered.
  * Selected graph provider MUST be registered.
  * Selected retrieval provider MUST be registered.
  * If RAGAnything is the selected compiler, text + embedding LLM
    roles MUST be configured.
  * If visual enrichment is enabled, the vision LLM role MUST be
    configured.
  * If Graphify is selected as graph provider, `J1_GRAPHIFY_ENABLED`
    MUST be true.

Errors are actionable — they name the missing env var(s).
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from j1.errors.exceptions import ConfigError
from j1.llm import (
    LLM_ROLE_EMBEDDING,
    LLM_ROLE_FAST,
    LLM_ROLE_TEXT,
    LLM_ROLE_VISION,
    LLMConfigError,
    LLMProviderRegistry,
    LLMProviderUnavailable,
    LLMSettings,
    LangChainEmbeddingClient,
    LangChainTextLLMClient,
    LangChainVisionLLMClient,
    OpenAICompatEmbeddingClient,
    OpenAICompatTextLLMClient,
    OpenAICompatVisionLLMClient,
    PROVIDER_LANGCHAIN,
    PROVIDER_OPENAI_COMPAT,
    load_llm_settings,
)
from j1.compose.diagnostics import (
    ProviderDiagnostics,
    StartupDiagnostics,
)
from j1.providers.graphify import (
    GraphifyGraphBuilder,
    GraphifySettings,
    PROVIDER_NAME as GRAPHIFY_NAME,
    load_graphify_settings,
)
from j1.providers.raganything import (
    PROVIDER_NAME as RAGANYTHING_NAME,
    RAGAnythingCompiler,
    RAGAnythingGraphBuilder,
    RAGAnythingQueryProvider,
    RAGAnythingSettings,
    load_raganything_settings,
)

# The composition root is the one place in core allowed to import the
# extension layer's reference (mock) adapters — so deployments can
# select `J1_DEFAULT_*=mock` to bring up a deterministic smoke pipeline
# without external dependencies. The static guard
# `tests/extension/test_guards.py::test_core_does_not_import_extension`
# explicitly allowlists this single import path.
from j1.extension.mocks import (
    MockCompilerAdapter,
    MockGraphAdapter,
    MockRetrievalAdapter,
)


# ---- Selection + enrichment env vars ---------------------------------

ENV_DEFAULT_COMPILER = "J1_DEFAULT_COMPILER"
ENV_DEFAULT_GRAPH = "J1_DEFAULT_GRAPH_PROVIDER"
ENV_DEFAULT_RETRIEVAL = "J1_DEFAULT_RETRIEVAL_PROVIDER"

ENV_ENRICH_ENABLED = "J1_ENRICH_ENABLED"
ENV_ENRICH_THRESHOLD = "J1_ENRICH_CONFIDENCE_THRESHOLD"
ENV_ENRICH_IMAGES = "J1_ENRICH_IMAGES"
ENV_ENRICH_TABLES = "J1_ENRICH_TABLES"
ENV_ENRICH_DIAGRAMS = "J1_ENRICH_DIAGRAMS"
ENV_ENRICH_SCANNED_PAGES = "J1_ENRICH_SCANNED_PAGES"

DEFAULT_COMPILER = "raganything"
DEFAULT_GRAPH = "raganything"
DEFAULT_RETRIEVAL = "raganything"

# Reference / smoke selection — wires the bundled deterministic mock
# adapters under `kind="mock"`. No vendor dependencies; no LLM
# credentials required; produces deterministic output suitable for
# end-to-end smoke tests + the bundled dev Docker stack.
MOCK_NAME = "mock"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class ProcessingSelection:
    compiler: str = DEFAULT_COMPILER
    graph: str = DEFAULT_GRAPH
    retrieval: str = DEFAULT_RETRIEVAL


@dataclass(frozen=True)
class EnrichmentSettings:
    enabled: bool = True
    confidence_threshold: float = 0.75
    images: bool = True
    tables: bool = True
    diagrams: bool = True
    scanned_pages: bool = True

    @property
    def visual_modalities_enabled(self) -> bool:
        """Any modality that requires the vision LLM."""
        return self.enabled and (
            self.images or self.diagrams or self.scanned_pages
        )

    def enabled_modalities(self) -> tuple[str, ...]:
        if not self.enabled:
            return ()
        out = []
        for name, on in (
            ("images", self.images), ("tables", self.tables),
            ("diagrams", self.diagrams), ("scanned_pages", self.scanned_pages),
        ):
            if on:
                out.append(name)
        return tuple(out)


def load_processing_selection(
    env: Mapping[str, str] | None = None,
) -> ProcessingSelection:
    source = env if env is not None else os.environ
    return ProcessingSelection(
        compiler=source.get(ENV_DEFAULT_COMPILER, DEFAULT_COMPILER).strip().lower(),
        graph=source.get(ENV_DEFAULT_GRAPH, DEFAULT_GRAPH).strip().lower(),
        retrieval=source.get(ENV_DEFAULT_RETRIEVAL, DEFAULT_RETRIEVAL).strip().lower(),
    )


def load_enrichment_settings(
    env: Mapping[str, str] | None = None,
) -> EnrichmentSettings:
    source = env if env is not None else os.environ

    def _bool(key: str, default: bool) -> bool:
        raw = source.get(key)
        if raw is None or raw == "":
            return default
        return raw.lower() in _TRUTHY

    threshold_raw = source.get(ENV_ENRICH_THRESHOLD)
    try:
        threshold = float(threshold_raw) if threshold_raw else 0.75
    except ValueError as exc:
        raise ConfigError(
            f"{ENV_ENRICH_THRESHOLD} must be a number, got {threshold_raw!r}"
        ) from exc

    return EnrichmentSettings(
        enabled=_bool(ENV_ENRICH_ENABLED, True),
        confidence_threshold=threshold,
        images=_bool(ENV_ENRICH_IMAGES, True),
        tables=_bool(ENV_ENRICH_TABLES, True),
        diagrams=_bool(ENV_ENRICH_DIAGRAMS, True),
        scanned_pages=_bool(ENV_ENRICH_SCANNED_PAGES, True),
    )


# ---- Bootstrap result -----------------------------------------------


@dataclass(frozen=True)
class BootstrapResult:
    """Everything a deployment entrypoint needs."""

    selection: ProcessingSelection
    enrichment: EnrichmentSettings
    llm_registry: LLMProviderRegistry
    compilers: Mapping[str, object] = field(default_factory=dict)
    graph_builders: Mapping[str, object] = field(default_factory=dict)
    retrieval_providers: Mapping[str, object] = field(default_factory=dict)
    diagnostics: StartupDiagnostics = field(default_factory=StartupDiagnostics)



# ---- Bootstrap class ------------------------------------------------


class Bootstrap:
    """Wraps env loading + client construction + validation.

    Pass `env=` to control the entire env mapping (used by tests).
    Pass `llm_registry=` to skip LLM-client construction entirely
    (used by tests that wire fakes directly).
    """

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        llm_registry: LLMProviderRegistry | None = None,
    ) -> None:
        self._env: Mapping[str, str] = env if env is not None else os.environ
        self._llm_registry_override = llm_registry

    def build(self) -> BootstrapResult:
        selection = load_processing_selection(self._env)
        enrichment = load_enrichment_settings(self._env)
        llm_settings = load_llm_settings(self._env)
        raganything_settings = load_raganything_settings(self._env)
        graphify_settings = load_graphify_settings(self._env)

        llm_registry = (
            self._llm_registry_override
            if self._llm_registry_override is not None
            else _build_llm_registry(llm_settings)
        )

        compilers: dict[str, object] = {}
        graph_builders: dict[str, object] = {}
        retrieval_providers: dict[str, object] = {}

        # ---- Compiler selection --------------------------------------
        if selection.compiler == RAGANYTHING_NAME:
            _validate_raganything_llm(llm_registry, "compiler")
            compilers[RAGANYTHING_NAME] = RAGAnythingCompiler.from_default(
                llm_registry=llm_registry,
                settings=raganything_settings,
            )
        elif selection.compiler == MOCK_NAME:
            # No LLM validation: the mock adapter is self-contained.
            compilers[MOCK_NAME] = MockCompilerAdapter()

        # ---- Graph-provider selection --------------------------------
        if selection.graph == RAGANYTHING_NAME:
            _validate_raganything_llm(llm_registry, "graph builder")
            graph_builders[RAGANYTHING_NAME] = RAGAnythingGraphBuilder.from_default(
                llm_registry=llm_registry,
                settings=raganything_settings,
            )
        elif selection.graph == GRAPHIFY_NAME:
            if not graphify_settings.enabled:
                raise ConfigError(
                    f"{ENV_DEFAULT_GRAPH}={GRAPHIFY_NAME} is selected but Graphify "
                    f"is not enabled. Set J1_GRAPHIFY_ENABLED=true to enable it, or "
                    f"choose a different graph provider via {ENV_DEFAULT_GRAPH}."
                )
            graph_builders[GRAPHIFY_NAME] = GraphifyGraphBuilder.from_default(
                settings=graphify_settings,
            )
        elif selection.graph == MOCK_NAME:
            graph_builders[MOCK_NAME] = MockGraphAdapter()
        else:
            raise ConfigError(
                f"{ENV_DEFAULT_GRAPH}={selection.graph!r} is not a registered "
                f"graph provider. Built-in providers: raganything (default), "
                f"graphify (optional via J1_GRAPHIFY_ENABLED=true), "
                f"mock (smoke / dev)."
            )

        # ---- Retrieval-provider selection ----------------------------
        if selection.retrieval == RAGANYTHING_NAME:
            _validate_raganything_llm(llm_registry, "retrieval")
            retrieval_providers[RAGANYTHING_NAME] = RAGAnythingQueryProvider.from_default(
                llm_registry=llm_registry,
                settings=raganything_settings,
            )
        elif selection.retrieval == MOCK_NAME:
            # Empty corpus — the mock returns no evidence rather than
            # fabricating answers. Deployments wanting a populated
            # smoke corpus can register their own MockRetrievalAdapter
            # outside the bootstrap.
            retrieval_providers[MOCK_NAME] = MockRetrievalAdapter(corpus=[])

        # ---- Selection must end up in its registry -------------------
        if selection.compiler not in compilers:
            raise ConfigError(
                f"{ENV_DEFAULT_COMPILER}={selection.compiler!r} is not a registered "
                f"compiler provider. Built-in providers: raganything (default), "
                f"mock (smoke / dev)."
            )
        if selection.retrieval not in retrieval_providers:
            raise ConfigError(
                f"{ENV_DEFAULT_RETRIEVAL}={selection.retrieval!r} is not a registered "
                f"retrieval provider. Built-in providers: raganything (default), "
                f"mock (smoke / dev)."
            )

        # Visual-enrichment validation is independent of which provider
        # is selected — it's purely about whether the deployment intends
        # to actually run vision-backed enrichment.
        if enrichment.visual_modalities_enabled and not llm_registry.has(LLM_ROLE_VISION):
            raise ConfigError(
                "Visual enrichment is enabled but no vision LLM is configured. "
                "Configure J1_VISION_LLM_PROVIDER, J1_VISION_LLM_BASE_URL, "
                "J1_VISION_LLM_API_KEY, and J1_VISION_LLM_MODEL — or disable "
                "visual modalities via J1_ENRICH_IMAGES=false / "
                "J1_ENRICH_DIAGRAMS=false / J1_ENRICH_SCANNED_PAGES=false."
            )

        diagnostics = _build_diagnostics(
            selection=selection,
            enrichment=enrichment,
            llm_registry=llm_registry,
            compilers=compilers,
            graph_builders=graph_builders,
            retrieval_providers=retrieval_providers,
            graphify_enabled=graphify_settings.enabled,
        )

        return BootstrapResult(
            selection=selection,
            enrichment=enrichment,
            llm_registry=llm_registry,
            compilers=compilers,
            graph_builders=graph_builders,
            retrieval_providers=retrieval_providers,
            diagnostics=diagnostics,
        )


def bootstrap_from_env(
    env: Mapping[str, str] | None = None,
) -> BootstrapResult:
    """Shortcut for the typical entrypoint use case."""
    return Bootstrap(env=env).build()


# ---- Internal helpers ----------------------------------------------


def _build_llm_registry(settings: LLMSettings) -> LLMProviderRegistry:
    """Construct an `LLMProviderRegistry` from typed settings.

    Supports both `openai_compat` (HTTP) and `langchain` (lazy-import,
    auto-instantiated via the safe class-loader) per role. Each role
    is independent — text via OpenAI-compat + embeddings via LangChain
    is a perfectly valid configuration.
    """
    registry = LLMProviderRegistry()

    text_client = _build_role_client(
        settings=settings.text, role="text",
        openai_factory=OpenAICompatTextLLMClient,
        langchain_factory=LangChainTextLLMClient.from_settings,
    )
    if text_client is not None:
        registry.register(LLM_ROLE_TEXT, text_client)

    vision_client = _build_role_client(
        settings=settings.vision, role="vision",
        openai_factory=OpenAICompatVisionLLMClient,
        langchain_factory=LangChainVisionLLMClient.from_settings,
    )
    if vision_client is not None:
        registry.register(LLM_ROLE_VISION, vision_client)

    embedding_client = _build_role_client(
        settings=settings.embedding, role="embedding",
        openai_factory=OpenAICompatEmbeddingClient,
        langchain_factory=LangChainEmbeddingClient.from_settings,
    )
    if embedding_client is not None:
        registry.register(LLM_ROLE_EMBEDDING, embedding_client)

    # Phase B: optional FAST role. Same OpenAI-compat client as text
    # — only the `model` differs in typical deployments. When
    # `settings.fast` is None or unconfigured, we silently skip
    # registration. Consumers (`registry.try_fast()`) handle the
    # absence; the planner falls back to deterministic-only.
    if settings.fast is not None:
        fast_client = _build_role_client(
            settings=settings.fast, role="fast",
            openai_factory=OpenAICompatTextLLMClient,
            langchain_factory=LangChainTextLLMClient.from_settings,
        )
        if fast_client is not None:
            registry.register(LLM_ROLE_FAST, fast_client)

    return registry


def _build_role_client(
    *,
    settings,
    role: str,
    openai_factory,
    langchain_factory,
):
    """Dispatch on `settings.provider` to the right concrete client factory.

    Returns `None` when the role isn't configured (composition root
    decides whether that's a fatal validation error). Wraps any
    `LLMConfigError` / `LLMProviderUnavailable` into `ConfigError`
    with the role name prepended so operators see "embedding LLM:
    failed to import langchain_openai" instead of an opaque message.
    """
    if not settings.is_configured:
        return None
    try:
        if settings.provider == PROVIDER_OPENAI_COMPAT:
            return openai_factory(settings)
        if settings.provider == PROVIDER_LANGCHAIN:
            return langchain_factory(settings)
        raise LLMConfigError(
            f"unsupported provider {settings.provider!r}"
        )
    except (LLMConfigError, LLMProviderUnavailable) as exc:
        raise ConfigError(f"{role} LLM: {exc}") from exc


def _validate_raganything_llm(
    llm_registry: LLMProviderRegistry, use_case: str,
) -> None:
    """Both text + embedding required when RAGAnything backs `use_case`."""
    missing = []
    if not llm_registry.has(LLM_ROLE_TEXT):
        missing.append("text")
    if not llm_registry.has(LLM_ROLE_EMBEDDING):
        missing.append("embedding")
    if missing:
        raise ConfigError(
            f"RAGAnything {use_case} requires {', '.join(missing)} LLM "
            f"role(s) to be configured. Configure J1_TEXT_LLM_* and "
            f"J1_EMBEDDING_* (or pre-register clients via Bootstrap("
            f"llm_registry=...) for fakes / LangChain-backed clients)."
        )


def _build_diagnostics(
    *,
    selection: ProcessingSelection,
    enrichment: EnrichmentSettings,
    llm_registry: LLMProviderRegistry,
    compilers: Mapping[str, object],
    graph_builders: Mapping[str, object],
    retrieval_providers: Mapping[str, object],
    graphify_enabled: bool,
) -> StartupDiagnostics:
    def _entries(reg: Mapping[str, object]) -> tuple[ProviderDiagnostics, ...]:
        return tuple(
            ProviderDiagnostics(name=name, available=True)
            for name in sorted(reg)
        )

    llm_roles: dict[str, dict[str, str | None]] = {}
    for role, info in llm_registry.diagnostics().items():
        # Embedding clients also expose dimension via their settings;
        # surface it (best effort) without leaking secrets.
        client = llm_registry.try_resolve(role)
        dimension: str | None = None
        if hasattr(client, "dimension"):
            try:
                dimension = str(client.dimension())
            except Exception:
                dimension = None
        llm_roles[role] = {
            "provider": info.get("provider"),
            "model": info.get("model"),
            "dimension": dimension,
        }

    return StartupDiagnostics(
        compiler_providers=_entries(compilers),
        graph_providers=_entries(graph_builders),
        retrieval_providers=_entries(retrieval_providers),
        enrichment_providers=(),
        selected_compiler=selection.compiler,
        selected_graph=selection.graph,
        selected_retrieval=selection.retrieval,
        llm_roles=llm_roles,
        enrichment_enabled=enrichment.enabled,
        enrichment_modalities=enrichment.enabled_modalities(),
        graphify_enabled=graphify_enabled,
    )
