"""Tests for the composition root.

Covers:
 * Default selection: raganything compiler / graph / retrieval
 * Bootstrap fails clearly when RAGAnything compiler selected without
 text + embedding LLM roles
 * Bootstrap fails clearly when visual enrichment is enabled without
 a vision LLM
 * Bootstrap fails clearly when graphify is selected without
 `J1_GRAPHIFY_ENABLED=true`
 * Bootstrap succeeds when graphify is enabled AND selected (with
 fakes for the LLM clients)
 * Diagnostics snapshot reports compiler / graph / retrieval, the
 selected providers, the LLM roles, and never leaks secrets
 * `bootstrap_from_env` is the entrypoint shortcut
"""

import json

import pytest

from j1.errors.exceptions import ConfigError
from j1.compose import (
    Bootstrap,
    EnrichmentSettings,
    ProcessingSelection,
    bootstrap_from_env,
    load_enrichment_settings,
    load_processing_selection,
    render_startup_diagnostics,
)
from j1.llm import (
    LLM_ROLE_EMBEDDING,
    LLM_ROLE_TEXT,
    LLM_ROLE_VISION,
    LLMProviderRegistry,
)


# ---- Fakes used to skip real LLM construction -----------------------


class _FakeText:
    provider = "fake"
    model = "fake-text"


class _FakeVision:
    provider = "fake"
    model = "fake-vision"


class _FakeEmbed:
    provider = "fake"
    model = "fake-embed"

    def dimension(self) -> int:
        return 1024

    def max_tokens(self) -> int:
        return 8192


def _full_registry() -> LLMProviderRegistry:
    """All three roles registered with stubs."""
    reg = LLMProviderRegistry()
    reg.register(LLM_ROLE_TEXT, _FakeText())
    reg.register(LLM_ROLE_VISION, _FakeVision())
    reg.register(LLM_ROLE_EMBEDDING, _FakeEmbed())
    return reg


def _registry_no_vision() -> LLMProviderRegistry:
    reg = LLMProviderRegistry()
    reg.register(LLM_ROLE_TEXT, _FakeText())
    reg.register(LLM_ROLE_EMBEDDING, _FakeEmbed())
    return reg


# ---- Selection + enrichment env loading -----------------------------


def test_load_processing_selection_defaults_to_raganything():
    sel = load_processing_selection(env={"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1"})
    assert sel.compiler == "raganything"
    assert sel.graph == "raganything"
    assert sel.retrieval == "raganything"


def test_load_processing_selection_normalises_case():
    sel = load_processing_selection(env={
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_DEFAULT_GRAPH_PROVIDER": "  GRAPHIFY  ",
    })
    assert sel.graph == "graphify"


def test_load_enrichment_settings_defaults():
    e = load_enrichment_settings(env={"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1"})
    assert e.enabled is True
    assert e.confidence_threshold == 0.75
    assert e.images is True
    assert e.tables is True
    assert e.diagrams is True
    assert e.scanned_pages is True
    assert e.visual_modalities_enabled is True
    assert set(e.enabled_modalities()) == {
        "images", "tables", "diagrams", "scanned_pages",
    }


def test_load_enrichment_settings_disabled():
    e = load_enrichment_settings(env={"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1", "J1_ENRICH_ENABLED": "false"})
    assert e.enabled is False
    assert e.visual_modalities_enabled is False
    assert e.enabled_modalities() == ()


def test_load_enrichment_settings_partial_disable():
    """Disabling images + diagrams + scanned should still allow tables."""
    e = load_enrichment_settings(env={
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_ENRICH_IMAGES": "false",
        "J1_ENRICH_DIAGRAMS": "false",
        "J1_ENRICH_SCANNED_PAGES": "false",
    })
    assert e.visual_modalities_enabled is False  # all visual flags off
    assert e.enabled_modalities() == ("tables",)


def test_load_enrichment_settings_invalid_threshold():
    with pytest.raises(ConfigError, match="must be a number"):
        load_enrichment_settings(env={"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1", "J1_ENRICH_CONFIDENCE_THRESHOLD": "high"})


# ---- Bootstrap success path -----------------------------------------


def test_bootstrap_default_registers_raganything_everywhere():
    result = Bootstrap(env={"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1"}, llm_registry=_full_registry()).build()
    assert result.selection.compiler == "raganything"
    assert result.selection.graph == "raganything"
    assert result.selection.retrieval == "raganything"
    assert "raganything" in result.compilers
    assert "raganything" in result.graph_builders
    assert "raganything" in result.retrieval_providers
    assert result.diagnostics.selected_compiler == "raganything"
    assert result.diagnostics.graphify_enabled is False


def test_bootstrap_returns_actual_provider_instances():
    """Sanity: registered providers can be retrieved + carry the right kind."""
    result = Bootstrap(env={"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1"}, llm_registry=_full_registry()).build()
    compiler = result.compilers["raganything"]
    assert compiler.kind == "raganything"


def test_bootstrap_with_enrichment_disabled_does_not_require_vision():
    """Disabling visual modalities removes the vision-LLM requirement."""
    env = {
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_ENRICH_IMAGES": "false",
        "J1_ENRICH_DIAGRAMS": "false",
        "J1_ENRICH_SCANNED_PAGES": "false",
    }
    result = Bootstrap(env=env, llm_registry=_registry_no_vision()).build()
    assert result.diagnostics.enrichment_modalities == ("tables",)


# ---- Bootstrap validation failures ----------------------------------


def test_bootstrap_fails_when_raganything_selected_without_text_llm():
    reg = LLMProviderRegistry()
    reg.register(LLM_ROLE_VISION, _FakeVision())
    reg.register(LLM_ROLE_EMBEDDING, _FakeEmbed())
    with pytest.raises(ConfigError, match="text"):
        Bootstrap(env={"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1"}, llm_registry=reg).build()


def test_bootstrap_fails_when_raganything_selected_without_embedding():
    reg = LLMProviderRegistry()
    reg.register(LLM_ROLE_TEXT, _FakeText())
    reg.register(LLM_ROLE_VISION, _FakeVision())
    with pytest.raises(ConfigError, match="embedding"):
        Bootstrap(env={"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1"}, llm_registry=reg).build()


def test_bootstrap_fails_when_visual_enrichment_needs_vision_llm():
    """Default enrichment includes images/diagrams/scanned which all need vision."""
    with pytest.raises(ConfigError, match="vision LLM"):
        Bootstrap(env={"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1"}, llm_registry=_registry_no_vision()).build()


def test_bootstrap_fails_when_graphify_selected_but_disabled():
    env = {
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_DEFAULT_GRAPH_PROVIDER": "graphify",
        # J1_GRAPHIFY_ENABLED unset → disabled
        "J1_ENRICH_ENABLED": "false",  # avoid vision-LLM noise
    }
    with pytest.raises(ConfigError, match="not enabled"):
        Bootstrap(env=env, llm_registry=_full_registry()).build()


def test_bootstrap_fails_when_unknown_graph_provider_selected():
    env = {"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1", "J1_DEFAULT_GRAPH_PROVIDER": "totally-fake"}
    with pytest.raises(ConfigError, match="not a registered graph provider"):
        Bootstrap(env=env, llm_registry=_full_registry()).build()


def test_bootstrap_fails_when_unknown_compiler_selected():
    env = {"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1", "J1_DEFAULT_COMPILER": "totally-fake"}
    with pytest.raises(ConfigError, match="not a registered compiler"):
        Bootstrap(env=env, llm_registry=_full_registry()).build()


# ---- Bootstrap with Graphify ----------------------------------------


def test_bootstrap_with_graphify_enabled_and_selected():
    env = {
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_DEFAULT_GRAPH_PROVIDER": "graphify",
        "J1_GRAPHIFY_ENABLED": "true",
    }
    result = Bootstrap(env=env, llm_registry=_full_registry()).build()
    assert "graphify" in result.graph_builders
    assert result.diagnostics.graphify_enabled is True
    assert result.diagnostics.selected_graph == "graphify"
    # The Graphify provider is what's registered, but RAGAnything stays
    # as the compiler + retrieval default.
    assert "raganything" in result.compilers
    assert "raganything" in result.retrieval_providers


def test_bootstrap_with_graphify_enabled_but_raganything_selected():
    """`enabled=true` doesn't auto-register Graphify if not selected."""
    env = {"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1", "J1_GRAPHIFY_ENABLED": "true"}
    result = Bootstrap(env=env, llm_registry=_full_registry()).build()
    assert "graphify" not in result.graph_builders
    assert result.diagnostics.graphify_enabled is True


# ---- Diagnostics ----------------------------------------------------


def test_diagnostics_renders_secrets_safe_lines():
    result = Bootstrap(env={"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1"}, llm_registry=_full_registry()).build()
    lines = render_startup_diagnostics(result.diagnostics)
    blob = "\n".join(lines)
    assert "raganything" in blob
    assert "fake-text" in blob
    assert "fake-vision" in blob
    assert "fake-embed" in blob
    assert "dim=1024" in blob
    # No secrets / URLs leak — fakes don't have any, but the renderer
    # itself MUST never serialise base_url / api_key / provider_config
    assert "Authorization" not in blob
    assert "api_key" not in blob


def test_diagnostics_captures_enrichment_state():
    env = {
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_ENRICH_IMAGES": "false",
        "J1_ENRICH_DIAGRAMS": "false",
        "J1_ENRICH_SCANNED_PAGES": "false",
    }
    result = Bootstrap(env=env, llm_registry=_registry_no_vision()).build()
    diag = result.diagnostics
    assert diag.enrichment_enabled is True
    assert diag.enrichment_modalities == ("tables",)


# ---- bootstrap_from_env shortcut ------------------------------------


def test_bootstrap_from_env_returns_result(monkeypatch):
    """The shortcut function reads from process env when no env is passed."""
    # Use process env explicitly with a minimal-and-disabled config.
    monkeypatch.setenv("J1_ENRICH_ENABLED", "false")
    # We can't easily inject the LLM registry through this entrypoint,
    # so this test just confirms it constructs an LLMSettings without
    # raising — the actual provider validation requires text+embed
    # which we can't supply via env without real network calls.
    # Use a minimal env that intentionally fails to produce a clean
    # error, asserting the entrypoint actually executes.
    with pytest.raises(ConfigError, match="text"):
        bootstrap_from_env()


# ---- Mock-selection (zero-credentials smoke path) ------------------


def test_bootstrap_mock_selection_registers_all_three_without_llm():
    """`J1_DEFAULT_*=mock` everywhere should boot with no LLM credentials.

 This is the configuration the bundled `.env.example` ships with —
 it lets the dev Docker stack run a complete workflow end-to-end
 without forcing the developer to provision an LLM endpoint.
 """
    env = {
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_DEFAULT_COMPILER": "mock",
        "J1_DEFAULT_GRAPH_PROVIDER": "mock",
        "J1_DEFAULT_RETRIEVAL_PROVIDER": "mock",
        "J1_ENRICH_ENABLED": "false",
    }
    # No LLM registry needed — bootstrap with an empty registry.
    result = Bootstrap(env=env, llm_registry=LLMProviderRegistry()).build()

    assert result.selection.compiler == "mock"
    assert result.selection.graph == "mock"
    assert result.selection.retrieval == "mock"
    assert "mock" in result.compilers
    assert "mock" in result.graph_builders
    assert "mock" in result.retrieval_providers


def test_bootstrap_mock_compiler_carries_correct_kind():
    env = {
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_DEFAULT_COMPILER": "mock",
        "J1_DEFAULT_GRAPH_PROVIDER": "mock",
        "J1_DEFAULT_RETRIEVAL_PROVIDER": "mock",
        "J1_ENRICH_ENABLED": "false",
    }
    result = Bootstrap(env=env, llm_registry=LLMProviderRegistry()).build()
    assert result.compilers["mock"].kind == "mock"
    assert result.graph_builders["mock"].kind == "mock"
    assert result.retrieval_providers["mock"].kind == "mock"


def test_bootstrap_mock_compiler_runs_against_real_processing_service(tmp_path):
    """Sanity: the bootstrapped mock compiler can actually execute.

 Proves the dev-stack default produces a real successful workflow
 end-to-end (not just "starts up without error").
 """
    from j1.processing.results import ResultStatus
    from j1.projects.context import ProjectContext

    env = {
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_DEFAULT_COMPILER": "mock",
        "J1_DEFAULT_GRAPH_PROVIDER": "mock",
        "J1_DEFAULT_RETRIEVAL_PROVIDER": "mock",
        "J1_ENRICH_ENABLED": "false",
    }
    result = Bootstrap(env=env, llm_registry=LLMProviderRegistry()).build()
    compiler = result.compilers["mock"]

    ctx = ProjectContext(tenant_id="acme", project_id="alpha")
    out = compiler.compile(ctx, document_id="doc-1")
    assert out.status is ResultStatus.SUCCEEDED
    assert len(out.drafts) == 1
    assert out.drafts[0].kind == "compiled.text"


def test_bootstrap_mixed_selection_mock_compiler_raganything_graph():
    """Selections are independent — mocking the compiler while keeping
 a real graph provider must work (and the real provider's LLM
 requirements still apply)."""
    env = {
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_DEFAULT_COMPILER": "mock",
        "J1_DEFAULT_GRAPH_PROVIDER": "raganything",
        "J1_DEFAULT_RETRIEVAL_PROVIDER": "mock",
        "J1_ENRICH_ENABLED": "false",
    }
    # raganything graph still demands text+embedding LLM.
    result = Bootstrap(env=env, llm_registry=_full_registry()).build()
    assert "mock" in result.compilers
    assert "raganything" in result.graph_builders
    assert "mock" in result.retrieval_providers


def test_bootstrap_mock_compiler_alone_does_not_satisfy_raganything_retrieval():
    """`mock` doesn't auto-fill other roles — selecting raganything for
 a different stage still needs LLM credentials."""
    env = {
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_DEFAULT_COMPILER": "mock",
        "J1_DEFAULT_GRAPH_PROVIDER": "mock",
        "J1_DEFAULT_RETRIEVAL_PROVIDER": "raganything",
        "J1_ENRICH_ENABLED": "false",
    }
    with pytest.raises(ConfigError, match="text"):
        Bootstrap(env=env, llm_registry=LLMProviderRegistry()).build()


def test_bootstrap_unknown_compiler_error_lists_mock_as_option():
    """Error message helps operators discover the smoke-mode option."""
    env = {"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1", "J1_DEFAULT_COMPILER": "totally-fake", "J1_ENRICH_ENABLED": "false"}
    with pytest.raises(ConfigError, match="mock"):
        Bootstrap(env=env, llm_registry=_full_registry()).build()
