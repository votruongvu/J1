"""Contract — stage-keyed LLM roles (indexing / query / enrichment).

Pins the load-bearing seam: the registry's `indexing() / query() /
enrichment()` helpers resolve the stage-specific client when wired,
falling back to the base `LLM_ROLE_TEXT` client otherwise. Single-
model deployments (only `J1_TEXT_LLM_*` set) keep working unchanged;
multi-model deployments can split indexing-cheap vs. query-large vs.
enrichment-accurate without every call site learning about it.

Two surfaces are covered:

  * Registry resolution + fallback semantics.
  * Settings env-var loaders + bootstrap registration via
    `_build_llm_registry` (the deployment-time wiring path).
"""

from __future__ import annotations

import pytest

from j1.llm.registry import (
    LLM_ROLE_ENRICHMENT,
    LLM_ROLE_INDEXING,
    LLM_ROLE_QUERY,
    LLM_ROLE_TEXT,
    LLMProviderRegistry,
)
from j1.llm.errors import LLMRoleNotRegistered
from j1.llm.settings import (
    EnrichmentLLMSettings,
    IndexingLLMSettings,
    QueryLLMSettings,
    load_llm_settings,
)


# ---- Stub client + fixture --------------------------------------


class _StubClient:
    """Bare-bones stand-in matching the registry's introspection
    fields (`provider`, `model`). The framework's real clients are
    OpenAI-compat / LangChain; tests don't need either to pin the
    role resolution contract."""

    def __init__(self, model: str, provider: str = "stub") -> None:
        self.provider = provider
        self.model = model


# ---- Resolution + fallback --------------------------------------


def test_indexing_falls_back_to_text_when_unset():
    registry = LLMProviderRegistry()
    text = _StubClient(model="text-default")
    registry.register(LLM_ROLE_TEXT, text)
    # No INDEXING client registered — must return the TEXT one.
    assert registry.indexing() is text


def test_query_falls_back_to_text_when_unset():
    registry = LLMProviderRegistry()
    text = _StubClient(model="text-default")
    registry.register(LLM_ROLE_TEXT, text)
    assert registry.query() is text


def test_enrichment_falls_back_to_text_when_unset():
    registry = LLMProviderRegistry()
    text = _StubClient(model="text-default")
    registry.register(LLM_ROLE_TEXT, text)
    assert registry.enrichment() is text


def test_indexing_returns_indexing_client_when_set():
    registry = LLMProviderRegistry()
    text = _StubClient(model="text-default")
    indexing = _StubClient(model="indexing-cheap")
    registry.register(LLM_ROLE_TEXT, text)
    registry.register(LLM_ROLE_INDEXING, indexing)
    assert registry.indexing() is indexing
    # Other stages remain on TEXT.
    assert registry.query() is text
    assert registry.enrichment() is text


def test_query_returns_query_client_when_set():
    registry = LLMProviderRegistry()
    text = _StubClient(model="text-default")
    query = _StubClient(model="query-large")
    registry.register(LLM_ROLE_TEXT, text)
    registry.register(LLM_ROLE_QUERY, query)
    assert registry.query() is query
    assert registry.indexing() is text


def test_enrichment_returns_enrichment_client_when_set():
    registry = LLMProviderRegistry()
    text = _StubClient(model="text-default")
    enrichment = _StubClient(model="enrichment-accurate")
    registry.register(LLM_ROLE_TEXT, text)
    registry.register(LLM_ROLE_ENRICHMENT, enrichment)
    assert registry.enrichment() is enrichment


def test_all_three_stages_independent():
    """A deployment with three distinct stage clients routes each
    stage to the right one — no cross-pollination."""
    registry = LLMProviderRegistry()
    text = _StubClient(model="text-default")
    idx = _StubClient(model="idx-cheap")
    qry = _StubClient(model="qry-large")
    enr = _StubClient(model="enr-accurate")
    registry.register(LLM_ROLE_TEXT, text)
    registry.register(LLM_ROLE_INDEXING, idx)
    registry.register(LLM_ROLE_QUERY, qry)
    registry.register(LLM_ROLE_ENRICHMENT, enr)
    assert registry.indexing() is idx
    assert registry.query() is qry
    assert registry.enrichment() is enr


def test_indexing_raises_when_neither_role_wired():
    """When neither INDEXING nor TEXT is registered, the resolver
    raises — silent fallback to None would let the bridge build a
    LightRAG instance with no LLM and surface deep inside the
    vendor stack as a confusing AttributeError."""
    registry = LLMProviderRegistry()
    with pytest.raises(LLMRoleNotRegistered):
        registry.indexing()


def test_try_helpers_return_none_when_unset():
    """`try_*` variants are the introspection surface — they don't
    fall back, so callers that need to know whether the
    stage-specific role is wired can branch on `None`."""
    registry = LLMProviderRegistry()
    text = _StubClient(model="text-default")
    registry.register(LLM_ROLE_TEXT, text)
    assert registry.try_indexing() is None
    assert registry.try_query() is None
    assert registry.try_enrichment() is None


# ---- Diagnostics -------------------------------------------------


def test_stage_diagnostics_reports_text_fallback():
    registry = LLMProviderRegistry()
    text = _StubClient(model="text-default", provider="openai")
    registry.register(LLM_ROLE_TEXT, text)
    diag = registry.stage_diagnostics()
    # All three stages report the TEXT client (fallback path).
    for stage in ("indexing", "query", "enrichment"):
        assert diag[stage]["model"] == "text-default"
        assert diag[stage]["provider"] == "openai"


def test_stage_diagnostics_reports_stage_specific_when_wired():
    registry = LLMProviderRegistry()
    text = _StubClient(model="text-default")
    idx = _StubClient(model="idx-cheap")
    registry.register(LLM_ROLE_TEXT, text)
    registry.register(LLM_ROLE_INDEXING, idx)
    diag = registry.stage_diagnostics()
    assert diag["indexing"]["model"] == "idx-cheap"
    assert diag["query"]["model"] == "text-default"
    assert diag["enrichment"]["model"] == "text-default"


def test_stage_diagnostics_with_empty_registry():
    """No TEXT, no stage roles — every stage reports `None` so the
    operator-facing log line still renders without raising."""
    registry = LLMProviderRegistry()
    diag = registry.stage_diagnostics()
    for stage in ("indexing", "query", "enrichment"):
        assert diag[stage]["model"] is None
        assert diag[stage]["client_type"] is None


# ---- Settings env-var loaders ------------------------------------


def test_load_llm_settings_returns_stage_settings_when_env_unset():
    """Settings loader always materialises stage settings objects;
    `is_configured=False` signals 'fall back to TEXT' to bootstrap."""
    settings = load_llm_settings(env={})
    assert isinstance(settings.indexing, IndexingLLMSettings)
    assert isinstance(settings.query, QueryLLMSettings)
    assert isinstance(settings.enrichment, EnrichmentLLMSettings)
    assert settings.indexing.is_configured is False
    assert settings.query.is_configured is False
    assert settings.enrichment.is_configured is False


def test_load_indexing_settings_from_env():
    settings = load_llm_settings(env={
        "J1_INDEXING_LLM_BASE_URL": "http://idx.example/v1",
        "J1_INDEXING_LLM_API_KEY": "k",
        "J1_INDEXING_LLM_MODEL": "idx-cheap",
    })
    assert settings.indexing.is_configured is True
    assert settings.indexing.model == "idx-cheap"
    assert settings.indexing.base_url == "http://idx.example/v1"


def test_load_query_settings_from_env():
    settings = load_llm_settings(env={
        "J1_QUERY_LLM_BASE_URL": "http://qry.example/v1",
        "J1_QUERY_LLM_API_KEY": "k",
        "J1_QUERY_LLM_MODEL": "qry-large",
        "J1_QUERY_LLM_TEMPERATURE": "0.7",
    })
    assert settings.query.is_configured is True
    assert settings.query.model == "qry-large"
    assert settings.query.temperature == pytest.approx(0.7)


def test_load_enrichment_settings_from_env():
    settings = load_llm_settings(env={
        "J1_ENRICHMENT_LLM_BASE_URL": "http://enr.example/v1",
        "J1_ENRICHMENT_LLM_API_KEY": "k",
        "J1_ENRICHMENT_LLM_MODEL": "enr-accurate",
        "J1_ENRICHMENT_LLM_MAX_OUTPUT_TOKENS": "8192",
    })
    assert settings.enrichment.is_configured is True
    assert settings.enrichment.model == "enr-accurate"
    assert settings.enrichment.max_output_tokens == 8192


def test_stage_settings_share_text_default_temperature():
    """Defaults mirror TextLLMSettings so a deployment that bumps
    `J1_TEXT_LLM_TEMPERATURE` doesn't have to also override each
    stage role's temperature to keep behaviour the same. The defaults
    are anchored explicitly so a future refactor of the base class
    doesn't quietly change them."""
    settings = load_llm_settings(env={})
    assert settings.indexing.temperature == pytest.approx(0.2)
    assert settings.query.temperature == pytest.approx(0.2)
    assert settings.enrichment.temperature == pytest.approx(0.2)


# ---- Bootstrap wiring --------------------------------------------


def test_bootstrap_registers_stage_clients_when_settings_configured():
    """End-to-end check that `_build_llm_registry` actually
    registers stage-specific clients when their env vars are set.
    Uses OpenAI-compat client; we don't need a live endpoint —
    construction succeeds and the registry's `has()` check is the
    contract surface."""
    from j1.compose.bootstrap import _build_llm_registry

    settings = load_llm_settings(env={
        "J1_TEXT_LLM_BASE_URL": "http://text.example/v1",
        "J1_TEXT_LLM_API_KEY": "k",
        "J1_TEXT_LLM_MODEL": "text-default",
        "J1_INDEXING_LLM_BASE_URL": "http://idx.example/v1",
        "J1_INDEXING_LLM_API_KEY": "k",
        "J1_INDEXING_LLM_MODEL": "idx-cheap",
        "J1_QUERY_LLM_BASE_URL": "http://qry.example/v1",
        "J1_QUERY_LLM_API_KEY": "k",
        "J1_QUERY_LLM_MODEL": "qry-large",
        "J1_ENRICHMENT_LLM_BASE_URL": "http://enr.example/v1",
        "J1_ENRICHMENT_LLM_API_KEY": "k",
        "J1_ENRICHMENT_LLM_MODEL": "enr-accurate",
        # Embedding required by some downstream consumers, but
        # not by this test's contract surface.
        "J1_EMBEDDING_BASE_URL": "http://emb.example/v1",
        "J1_EMBEDDING_API_KEY": "k",
        "J1_EMBEDDING_MODEL": "emb-default",
    })
    registry = _build_llm_registry(settings)
    assert registry.has(LLM_ROLE_TEXT)
    assert registry.has(LLM_ROLE_INDEXING)
    assert registry.has(LLM_ROLE_QUERY)
    assert registry.has(LLM_ROLE_ENRICHMENT)
    # Each helper resolves the stage client (not the TEXT fallback).
    assert registry.indexing().model == "idx-cheap"
    assert registry.query().model == "qry-large"
    assert registry.enrichment().model == "enr-accurate"


def test_bootstrap_skips_stage_clients_when_only_text_configured():
    """Single-model deployment: only TEXT env vars set. Stage roles
    must not register, and resolution falls back to TEXT."""
    from j1.compose.bootstrap import _build_llm_registry

    settings = load_llm_settings(env={
        "J1_TEXT_LLM_BASE_URL": "http://text.example/v1",
        "J1_TEXT_LLM_API_KEY": "k",
        "J1_TEXT_LLM_MODEL": "text-default",
        "J1_EMBEDDING_BASE_URL": "http://emb.example/v1",
        "J1_EMBEDDING_API_KEY": "k",
        "J1_EMBEDDING_MODEL": "emb-default",
    })
    registry = _build_llm_registry(settings)
    assert registry.has(LLM_ROLE_TEXT)
    assert not registry.has(LLM_ROLE_INDEXING)
    assert not registry.has(LLM_ROLE_QUERY)
    assert not registry.has(LLM_ROLE_ENRICHMENT)
    # All three stage helpers fall back to TEXT.
    assert registry.indexing().model == "text-default"
    assert registry.query().model == "text-default"
    assert registry.enrichment().model == "text-default"
