"""Safe import-by-string for LangChain (and other) classes.

Two accepted forms:

  * Short alias from the built-in catalog: ``"ChatOpenAI"`` →
    imports ``langchain_openai.ChatOpenAI``.
  * Fully-qualified ``"module.path:ClassName"`` (or
    ``"module.path.ClassName"``).

Why a catalog? LangChain's class names are stable across versions but
their containing modules move (``langchain.chat_models`` →
``langchain_community.chat_models`` → ``langchain_openai``). The
catalog records the recommended import path so deployments can write
the short name and not chase package reorganisations.

Why an allowlist? `importlib` happily imports anything on `sys.path`,
including modules that execute code at import time. The allowlist
constrains config-driven instantiation to a vetted set of LangChain
extension packages so a misconfigured env var can't pull in arbitrary
code. Custom paths still work — they just need a fully-qualified
spec, no shorthand.
"""

import importlib
from typing import Any

from j1.llm.errors import LLMConfigError, LLMProviderUnavailable


# Built-in shorthand → fully-qualified import path. Entries cover the
# common LangChain chat + embedding classes; deployments needing
# something else use the fully-qualified form.
CHAT_MODEL_CATALOG: dict[str, str] = {
    "ChatOpenAI": "langchain_openai:ChatOpenAI",
    "AzureChatOpenAI": "langchain_openai:AzureChatOpenAI",
    "ChatAnthropic": "langchain_anthropic:ChatAnthropic",
    "ChatGoogleGenerativeAI": "langchain_google_genai:ChatGoogleGenerativeAI",
    "ChatVertexAI": "langchain_google_vertexai:ChatVertexAI",
    "ChatOllama": "langchain_ollama:ChatOllama",
    "ChatGroq": "langchain_groq:ChatGroq",
    "ChatMistralAI": "langchain_mistralai:ChatMistralAI",
    "ChatBedrock": "langchain_aws:ChatBedrock",
    "ChatCohere": "langchain_cohere:ChatCohere",
    "ChatFireworks": "langchain_fireworks:ChatFireworks",
    "ChatTogether": "langchain_together:ChatTogether",
}

EMBEDDING_CATALOG: dict[str, str] = {
    "OpenAIEmbeddings": "langchain_openai:OpenAIEmbeddings",
    "AzureOpenAIEmbeddings": "langchain_openai:AzureOpenAIEmbeddings",
    "HuggingFaceEmbeddings": "langchain_huggingface:HuggingFaceEmbeddings",
    "OllamaEmbeddings": "langchain_ollama:OllamaEmbeddings",
    "CohereEmbeddings": "langchain_cohere:CohereEmbeddings",
    "BedrockEmbeddings": "langchain_aws:BedrockEmbeddings",
    "GoogleGenerativeAIEmbeddings": "langchain_google_genai:GoogleGenerativeAIEmbeddings",
    "MistralAIEmbeddings": "langchain_mistralai:MistralAIEmbeddings",
}

# Fully-qualified specs are accepted if their top-level package matches
# one of these prefixes. Two forms are supported:
#   * exact match on the top-level package, e.g. ``"j1"``
#   * underscore-terminated prefix that matches by head-startswith,
#     e.g. ``"langchain_"`` matches ``langchain_openai``,
#     ``langchain_anthropic``, …
# Plain ``"langchain"`` covers the legacy umbrella `langchain` package.
_TRUSTED_PREFIXES: tuple[str, ...] = (
    "langchain",
    "langchain_",
    "j1",
)


def resolve_chat_model(spec: str) -> Any:
    """Import a LangChain chat model class by alias or qualified path."""
    return _resolve(spec, CHAT_MODEL_CATALOG, what="LangChain chat model")


def resolve_embedding_model(spec: str) -> Any:
    """Import a LangChain embedding class by alias or qualified path."""
    return _resolve(spec, EMBEDDING_CATALOG, what="LangChain embedding")


def resolve_callable(spec: str) -> Any:
    """Import any function/class by ``module:name`` or ``module.name`` spec.

    Used by the RAGAnything / Graphify adapters to load a deployment-
    supplied processor callable from an env var. Subject to the same
    trusted-prefix allowlist as `resolve_chat_model`, but with one
    additional carve-out: any path under the deployment's own package
    space is allowed if its first segment is registered via
    `register_trusted_prefix(...)`.
    """
    return _resolve_qualified(spec, what="callable")


def register_trusted_prefix(prefix: str) -> None:
    """Add an import-prefix to the trusted-allowlist for class-loading.

    Deployments call this once at startup if they want to load custom
    classes outside the LangChain / j1 namespaces. Example::

        register_trusted_prefix("mycompany_kb")

    Idempotent. Prefix should NOT end with ``.``.
    """
    global _TRUSTED_PREFIXES
    p = prefix.strip().rstrip(".")
    if not p:
        raise LLMConfigError("trusted prefix must be non-empty")
    if p in _TRUSTED_PREFIXES:
        return
    _TRUSTED_PREFIXES = _TRUSTED_PREFIXES + (p,)


# ---- Internals ------------------------------------------------------


def _resolve(spec: str, catalog: dict[str, str], *, what: str) -> Any:
    if not spec or not isinstance(spec, str):
        raise LLMConfigError(f"{what} spec must be a non-empty string")
    spec = spec.strip()
    qualified = catalog.get(spec, spec)
    return _resolve_qualified(qualified, what=what)


def _resolve_qualified(spec: str, *, what: str) -> Any:
    spec = spec.strip()
    if ":" in spec:
        module_path, _, attr = spec.partition(":")
    elif "." in spec:
        module_path, _, attr = spec.rpartition(".")
    else:
        raise LLMConfigError(
            f"{what} spec {spec!r} must be either a short alias or a "
            f"fully-qualified path like 'module.path:ClassName'"
        )

    if not _is_trusted(module_path):
        raise LLMConfigError(
            f"{what} module {module_path!r} is not on the trusted "
            f"allowlist. Use one of the catalog aliases, a "
            f"langchain* / j1.* path, or call "
            f"`j1.llm.classloader.register_trusted_prefix(...)` "
            f"with your own package's prefix."
        )

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise LLMProviderUnavailable(
            f"failed to import {module_path!r} for {what}: {exc}. "
            f"Install the corresponding package (e.g. "
            f"`pip install {module_path.replace('_', '-').split('.')[0]}`)."
        ) from exc

    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise LLMConfigError(
            f"module {module_path!r} has no attribute {attr!r} "
            f"(requested by {what} spec)"
        ) from exc


def _is_trusted(module_path: str) -> bool:
    head = module_path.split(".")[0]
    for prefix in _TRUSTED_PREFIXES:
        if prefix.endswith("_"):
            # Wildcard form: any head-package matching the prefix is OK.
            if head.startswith(prefix):
                return True
        elif head == prefix:
            return True
    return False
