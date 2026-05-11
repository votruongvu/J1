"""FAST LLM role plumbing regression tests.

The FAST role reuses the existing OpenAI-compat client class — the
contract is "add the role, no new adapter". These tests pin that:

 * Settings load FAST from `J1_FAST_LLM_*` env vars.
 * Bootstrap registers a FAST client when configured.
 * `LLMProviderRegistry.try_fast` returns it.
 * Absence is graceful: no FAST env vars → no FAST client → other
 roles still work, and `try_fast` returns None.
"""

from __future__ import annotations

from j1.llm.registry import (
    KNOWN_ROLES,
    LLM_ROLE_FAST,
    LLMProviderRegistry,
)
from j1.llm.settings import (
    FastLLMSettings,
    LLMSettings,
    load_llm_settings,
)


# ---- Role constant + KNOWN_ROLES set -----------------------------


def test_llm_role_fast_constant_is_fast():
    """Stable string — operators filter audit logs / search
 attributes on this value."""
    assert LLM_ROLE_FAST == "fast"


def test_known_roles_includes_fast():
    """The closed-set check at the bootstrap layer relies on
 `KNOWN_ROLES`. Adding FAST without updating the set would let
 an undefined role slip through validation."""
    assert LLM_ROLE_FAST in KNOWN_ROLES


# ---- Settings loader --------------------------------------------------


def test_load_fast_settings_from_env_when_provided():
    settings = load_llm_settings(env={
        "J1_FAST_LLM_PROVIDER": "openai_compat",
        "J1_FAST_LLM_BASE_URL": "https://api.example.com/v1",
        "J1_FAST_LLM_API_KEY": "sk-fast",
        "J1_FAST_LLM_MODEL": "gpt-4o-mini",
        "J1_FAST_LLM_TEMPERATURE": "0.0",
        "J1_FAST_LLM_TIMEOUT_SECONDS": "10",
        "J1_FAST_LLM_MAX_OUTPUT_TOKENS": "256",
    })
    assert settings.fast is not None
    assert settings.fast.provider == "openai_compat"
    assert settings.fast.base_url == "https://api.example.com/v1"
    assert settings.fast.model == "gpt-4o-mini"
    assert settings.fast.temperature == 0.0
    # Tighter defaults than text — fast role is for short structured tasks.
    assert settings.fast.max_output_tokens == 256
    assert settings.fast.timeout_seconds == 10.0
    assert settings.fast.is_configured is True


def test_load_fast_settings_unconfigured_when_env_absent():
    """No FAST env vars → settings.fast exists but is_configured=False.
 Bootstrap then silently skips registration — the other roles are
 unaffected."""
    settings = load_llm_settings(env={})
    assert settings.fast is not None
    assert settings.fast.is_configured is False


def test_fast_settings_default_temperature_is_zero():
    """Defaults reflect the role's purpose: deterministic
 classification / structured output, not creative generation."""
    s = FastLLMSettings(provider="openai_compat")
    assert s.temperature == 0.0
    assert s.max_output_tokens == 512


# ---- Bootstrap registry wiring -------------------------------------


def test_bootstrap_registers_fast_when_configured(monkeypatch):
    """When the operator sets `J1_FAST_LLM_*`, bootstrap registers a
 client under role=`fast`. The same OpenAI-compat client class
 serves text and fast — no new adapter."""
    from j1.compose.bootstrap import _build_llm_registry

    settings = load_llm_settings(env={
        "J1_TEXT_LLM_BASE_URL": "https://api.example.com/v1",
        "J1_TEXT_LLM_API_KEY": "sk-text",
        "J1_TEXT_LLM_MODEL": "gpt-4o",
        "J1_FAST_LLM_BASE_URL": "https://api.example.com/v1",
        "J1_FAST_LLM_API_KEY": "sk-fast",
        "J1_FAST_LLM_MODEL": "gpt-4o-mini",
    })
    registry = _build_llm_registry(settings)

    fast_client = registry.try_fast()
    assert fast_client is not None
    assert fast_client.model == "gpt-4o-mini"
    # Same OpenAI-compat client class used for text — confirms there's
    # no separate FastLLMAdapter class.
    text_client = registry.try_text()
    assert type(fast_client) is type(text_client), (
        "FAST and TEXT must reuse the same OpenAI-compat client class — "
        "the FAST role explicitly reuses the OpenAI-compat client "
        "rather than introducing a separate FastLLMAdapter"
    )


def test_bootstrap_skips_fast_when_unconfigured():
    """No FAST env vars → no fast client registered. The other roles
 stay configured and the framework remains fully functional."""
    from j1.compose.bootstrap import _build_llm_registry

    settings = load_llm_settings(env={
        "J1_TEXT_LLM_BASE_URL": "https://api.example.com/v1",
        "J1_TEXT_LLM_API_KEY": "sk-text",
        "J1_TEXT_LLM_MODEL": "gpt-4o",
    })
    registry = _build_llm_registry(settings)

    assert registry.try_fast() is None
    # Text role still works — FAST being missing must not affect the
    # other roles' registration.
    assert registry.try_text() is not None


# ---- Registry helpers --------------------------------------------


def test_try_fast_returns_none_for_empty_registry():
    """`try_fast` must be a no-op-on-absence — the planner depends on
 this to fall back to deterministic-only operation."""
    registry = LLMProviderRegistry()
    assert registry.try_fast() is None
