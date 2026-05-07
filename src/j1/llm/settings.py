"""LLM-role configuration loaded from `J1_*` env vars.

The framework's `J1_` prefix convention is preserved (the
implementation prompt's `APP_*` examples were illustrative). All
roles share a small common shape (`provider`, `base_url`,
`api_key`, `model`, `timeout_seconds`, `max_retries`) plus
role-specific extras.
"""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from j1.llm.errors import LLMConfigError

# ---- Provider type strings ------------------------------------------

PROVIDER_OPENAI_COMPAT = "openai_compat"
PROVIDER_LANGCHAIN = "langchain"

SUPPORTED_PROVIDERS: frozenset[str] = frozenset(
    {PROVIDER_OPENAI_COMPAT, PROVIDER_LANGCHAIN}
)


# ---- Env-var names --------------------------------------------------

# Text role
ENV_TEXT_PROVIDER = "J1_TEXT_LLM_PROVIDER"
ENV_TEXT_BASE_URL = "J1_TEXT_LLM_BASE_URL"
ENV_TEXT_API_KEY = "J1_TEXT_LLM_API_KEY"
ENV_TEXT_MODEL = "J1_TEXT_LLM_MODEL"
ENV_TEXT_TIMEOUT = "J1_TEXT_LLM_TIMEOUT_SECONDS"
ENV_TEXT_MAX_RETRIES = "J1_TEXT_LLM_MAX_RETRIES"
ENV_TEXT_TEMPERATURE = "J1_TEXT_LLM_TEMPERATURE"
ENV_TEXT_MAX_OUTPUT_TOKENS = "J1_TEXT_LLM_MAX_OUTPUT_TOKENS"
ENV_TEXT_CONTEXT_WINDOW_TOKENS = "J1_TEXT_LLM_CONTEXT_WINDOW_TOKENS"
ENV_TEXT_SAFETY_MARGIN_TOKENS = "J1_TEXT_LLM_SAFETY_MARGIN_TOKENS"
ENV_TEXT_LANGCHAIN_CONFIG = "J1_TEXT_LLM_LANGCHAIN_CONFIG"

# Vision role
ENV_VISION_PROVIDER = "J1_VISION_LLM_PROVIDER"
ENV_VISION_BASE_URL = "J1_VISION_LLM_BASE_URL"
ENV_VISION_API_KEY = "J1_VISION_LLM_API_KEY"
ENV_VISION_MODEL = "J1_VISION_LLM_MODEL"
ENV_VISION_TIMEOUT = "J1_VISION_LLM_TIMEOUT_SECONDS"
ENV_VISION_MAX_RETRIES = "J1_VISION_LLM_MAX_RETRIES"
ENV_VISION_TEMPERATURE = "J1_VISION_LLM_TEMPERATURE"
ENV_VISION_MAX_OUTPUT_TOKENS = "J1_VISION_LLM_MAX_OUTPUT_TOKENS"
ENV_VISION_CONTEXT_WINDOW_TOKENS = "J1_VISION_LLM_CONTEXT_WINDOW_TOKENS"
ENV_VISION_SAFETY_MARGIN_TOKENS = "J1_VISION_LLM_SAFETY_MARGIN_TOKENS"
ENV_VISION_LANGCHAIN_CONFIG = "J1_VISION_LLM_LANGCHAIN_CONFIG"

# Embedding role
ENV_EMBEDDING_PROVIDER = "J1_EMBEDDING_PROVIDER"
ENV_EMBEDDING_BASE_URL = "J1_EMBEDDING_BASE_URL"
ENV_EMBEDDING_API_KEY = "J1_EMBEDDING_API_KEY"
ENV_EMBEDDING_MODEL = "J1_EMBEDDING_MODEL"
ENV_EMBEDDING_DIM = "J1_EMBEDDING_DIM"
ENV_EMBEDDING_MAX_TOKENS = "J1_EMBEDDING_MAX_TOKENS"
ENV_EMBEDDING_BATCH_SIZE = "J1_EMBEDDING_BATCH_SIZE"
ENV_EMBEDDING_TIMEOUT = "J1_EMBEDDING_TIMEOUT_SECONDS"
ENV_EMBEDDING_MAX_RETRIES = "J1_EMBEDDING_MAX_RETRIES"
ENV_EMBEDDING_LANGCHAIN_CONFIG = "J1_EMBEDDING_LANGCHAIN_CONFIG"

# Fast role. Optional — used by the adaptive ingestion planner for
# short structured tasks. Same env-var shape as text so deployments
# can reuse base_url + api_key with just a different model.
ENV_FAST_PROVIDER = "J1_FAST_LLM_PROVIDER"
ENV_FAST_BASE_URL = "J1_FAST_LLM_BASE_URL"
ENV_FAST_API_KEY = "J1_FAST_LLM_API_KEY"
ENV_FAST_MODEL = "J1_FAST_LLM_MODEL"
ENV_FAST_TIMEOUT = "J1_FAST_LLM_TIMEOUT_SECONDS"
ENV_FAST_MAX_RETRIES = "J1_FAST_LLM_MAX_RETRIES"
ENV_FAST_TEMPERATURE = "J1_FAST_LLM_TEMPERATURE"
ENV_FAST_MAX_OUTPUT_TOKENS = "J1_FAST_LLM_MAX_OUTPUT_TOKENS"
ENV_FAST_CONTEXT_WINDOW_TOKENS = "J1_FAST_LLM_CONTEXT_WINDOW_TOKENS"
ENV_FAST_SAFETY_MARGIN_TOKENS = "J1_FAST_LLM_SAFETY_MARGIN_TOKENS"
ENV_FAST_LANGCHAIN_CONFIG = "J1_FAST_LLM_LANGCHAIN_CONFIG"


# ---- Settings -------------------------------------------------------


@dataclass(frozen=True)
class _CommonLLMSettings:
    """Fields every role shares."""
    provider: str
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    timeout_seconds: float = 60.0
    max_retries: int = 3
    # Free-form provider config — for `langchain` the deployment puts
    # init kwargs here. JSON-decoded from env, so it stays a Mapping.
    provider_config: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_configured(self) -> bool:
        """Return True when enough fields are present to construct a client.

        OpenAI-compat needs at least a `base_url` and `model`.
        LangChain needs at least a `provider_config` with the class
        name (deployment-supplied; the LangChain adapter validates the
        full shape).
        """
        if self.provider == PROVIDER_OPENAI_COMPAT:
            return bool(self.base_url and self.model)
        if self.provider == PROVIDER_LANGCHAIN:
            return bool(self.provider_config)
        return False


@dataclass(frozen=True)
class _BudgetedLLMSettings(_CommonLLMSettings):
    """Mixin shared by every chat-completion role (text / vision /
    fast). Carries the prompt-budget knobs so the OpenAI-compat
    client can defend against context-window overflow uniformly.

    `context_window_tokens=None` disables the check (legacy /
    don't-know-the-window deployments). When set, the boundary
    enforces:

        available_input_tokens =
            context_window_tokens
            - max_output_tokens     # reserved for the response
            - safety_margin_tokens  # accounting for tokenizer drift

    Estimated prompt tokens above `available_input_tokens` raise
    `LLMContextOverflowError` BEFORE the HTTP request leaves J1 —
    operators see an actionable J1 error instead of LM Studio's
    terse 'Context size has been exceeded' HTTP 400.
    """

    # Total tokens the model can hold in one turn. Operator-supplied
    # because we can't introspect it from the endpoint reliably
    # across LM Studio / vLLM / OpenAI / etc. Set this to whatever
    # the model card / `context_length` of the loaded model says.
    context_window_tokens: int | None = None
    # Conservative buffer to absorb tokenizer-estimate drift (we
    # don't ship `tiktoken`; the fallback estimator is approximate).
    # 256 is a safe default for ≤32K-window models; bump for
    # tighter windows or when you know the prompts pack non-ASCII.
    safety_margin_tokens: int = 256


@dataclass(frozen=True)
class TextLLMSettings(_BudgetedLLMSettings):
    temperature: float = 0.2
    max_output_tokens: int = 4096


@dataclass(frozen=True)
class VisionLLMSettings(_BudgetedLLMSettings):
    temperature: float = 0.1
    max_output_tokens: int = 4096


@dataclass(frozen=True)
class EmbeddingSettings(_CommonLLMSettings):
    dimension: int | None = None
    max_input_tokens: int = 8192
    batch_size: int = 32


@dataclass(frozen=True)
class FastLLMSettings(_BudgetedLLMSettings):
    """FAST role — same shape as text but tighter defaults.

    Consumed by the adaptive ingestion planner for short structured
    tasks (document classification, mode selection, light metadata).
    Lower default temperature (deterministic classification) and
    tighter timeout reflect the fast-and-cheap intent. Optional:
    when `is_configured=False`, the planner falls back to
    deterministic-only operation (no LLM hint)."""

    temperature: float = 0.0
    max_output_tokens: int = 512


@dataclass(frozen=True)
class LLMSettings:
    """Aggregate of the role settings + cross-cutting validators."""
    text: TextLLMSettings
    vision: VisionLLMSettings
    embedding: EmbeddingSettings
    # Optional fast role for the adaptive ingestion planner.
    # `fast.is_configured` may be False and the rest of the framework
    # still works (deterministic-only planning).
    fast: FastLLMSettings | None = None


# ---- Loaders --------------------------------------------------------


def load_llm_settings(env: Mapping[str, str] | None = None) -> LLMSettings:
    """Read every `J1_*_LLM_*` / `J1_EMBEDDING_*` env var into typed settings.

    Always returns an `LLMSettings` (no exceptions for missing roles —
    the composition root validates required roles separately, with
    actionable error messages naming the failing provider/use case).
    """
    source = env if env is not None else os.environ
    return LLMSettings(
        text=_load_text_settings(source),
        vision=_load_vision_settings(source),
        embedding=_load_embedding_settings(source),
        fast=_load_fast_settings(source),
    )


def _load_text_settings(env: Mapping[str, str]) -> TextLLMSettings:
    provider = _provider(env, ENV_TEXT_PROVIDER, PROVIDER_OPENAI_COMPAT)
    return TextLLMSettings(
        provider=provider,
        base_url=_str(env, ENV_TEXT_BASE_URL),
        api_key=_str(env, ENV_TEXT_API_KEY),
        model=_str(env, ENV_TEXT_MODEL),
        timeout_seconds=_float(env, ENV_TEXT_TIMEOUT, 60.0),
        max_retries=_int(env, ENV_TEXT_MAX_RETRIES, 3),
        provider_config=_json(env, ENV_TEXT_LANGCHAIN_CONFIG),
        temperature=_float(env, ENV_TEXT_TEMPERATURE, 0.2),
        max_output_tokens=_int(env, ENV_TEXT_MAX_OUTPUT_TOKENS, 4096),
        context_window_tokens=_int_or_none(env, ENV_TEXT_CONTEXT_WINDOW_TOKENS),
        safety_margin_tokens=_int(env, ENV_TEXT_SAFETY_MARGIN_TOKENS, 256),
    )


def _load_vision_settings(env: Mapping[str, str]) -> VisionLLMSettings:
    provider = _provider(env, ENV_VISION_PROVIDER, PROVIDER_OPENAI_COMPAT)
    return VisionLLMSettings(
        provider=provider,
        base_url=_str(env, ENV_VISION_BASE_URL),
        api_key=_str(env, ENV_VISION_API_KEY),
        model=_str(env, ENV_VISION_MODEL),
        timeout_seconds=_float(env, ENV_VISION_TIMEOUT, 90.0),
        max_retries=_int(env, ENV_VISION_MAX_RETRIES, 3),
        provider_config=_json(env, ENV_VISION_LANGCHAIN_CONFIG),
        temperature=_float(env, ENV_VISION_TEMPERATURE, 0.1),
        max_output_tokens=_int(env, ENV_VISION_MAX_OUTPUT_TOKENS, 4096),
        context_window_tokens=_int_or_none(env, ENV_VISION_CONTEXT_WINDOW_TOKENS),
        safety_margin_tokens=_int(env, ENV_VISION_SAFETY_MARGIN_TOKENS, 256),
    )


def _load_fast_settings(env: Mapping[str, str]) -> FastLLMSettings:
    """Load FAST role settings from env.

    Mirrors `_load_text_settings` shape so the same provider can serve
    fast and text with just a different model. Returns a settings
    object even when no env vars are set — `FastLLMSettings.is_configured`
    is False in that case (no `base_url` / `model` → planner will
    skip the LLM-fallback path)."""
    provider = _provider(env, ENV_FAST_PROVIDER, PROVIDER_OPENAI_COMPAT)
    return FastLLMSettings(
        provider=provider,
        base_url=_str(env, ENV_FAST_BASE_URL),
        api_key=_str(env, ENV_FAST_API_KEY),
        model=_str(env, ENV_FAST_MODEL),
        timeout_seconds=_float(env, ENV_FAST_TIMEOUT, 15.0),
        max_retries=_int(env, ENV_FAST_MAX_RETRIES, 1),
        provider_config=_json(env, ENV_FAST_LANGCHAIN_CONFIG),
        temperature=_float(env, ENV_FAST_TEMPERATURE, 0.0),
        max_output_tokens=_int(env, ENV_FAST_MAX_OUTPUT_TOKENS, 512),
        context_window_tokens=_int_or_none(env, ENV_FAST_CONTEXT_WINDOW_TOKENS),
        safety_margin_tokens=_int(env, ENV_FAST_SAFETY_MARGIN_TOKENS, 256),
    )


def _load_embedding_settings(env: Mapping[str, str]) -> EmbeddingSettings:
    provider = _provider(env, ENV_EMBEDDING_PROVIDER, PROVIDER_OPENAI_COMPAT)
    return EmbeddingSettings(
        provider=provider,
        base_url=_str(env, ENV_EMBEDDING_BASE_URL),
        api_key=_str(env, ENV_EMBEDDING_API_KEY),
        model=_str(env, ENV_EMBEDDING_MODEL),
        timeout_seconds=_float(env, ENV_EMBEDDING_TIMEOUT, 60.0),
        max_retries=_int(env, ENV_EMBEDDING_MAX_RETRIES, 3),
        provider_config=_json(env, ENV_EMBEDDING_LANGCHAIN_CONFIG),
        dimension=_int_or_none(env, ENV_EMBEDDING_DIM),
        max_input_tokens=_int(env, ENV_EMBEDDING_MAX_TOKENS, 8192),
        batch_size=_int(env, ENV_EMBEDDING_BATCH_SIZE, 32),
    )


# ---- Parsing helpers (validate + typecast) --------------------------


def _provider(env: Mapping[str, str], key: str, default: str) -> str:
    value = (env.get(key) or default).strip().lower()
    if value not in SUPPORTED_PROVIDERS:
        raise LLMConfigError(
            f"{key}={value!r} is not a supported LLM provider type "
            f"(supported: {sorted(SUPPORTED_PROVIDERS)})"
        )
    return value


def _str(env: Mapping[str, str], key: str) -> str | None:
    raw = env.get(key)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise LLMConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _int_or_none(env: Mapping[str, str], key: str) -> int | None:
    raw = env.get(key)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise LLMConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise LLMConfigError(f"{key} must be a number, got {raw!r}") from exc


def _json(env: Mapping[str, str], key: str) -> Mapping[str, Any]:
    raw = env.get(key)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMConfigError(
            f"{key} must be valid JSON object, got {raw[:80]!r}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise LLMConfigError(f"{key} must decode to an object, got {type(data).__name__}")
    return data
