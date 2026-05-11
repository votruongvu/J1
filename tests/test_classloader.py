"""Tests for the safe class-loader.

Covers:
 * Catalog aliases resolve to the right import path
 * Fully-qualified `module:Class` and `module.Class` both work
 * Allowlist rejects untrusted modules with an actionable error
 * Wildcard prefix (`langchain_`) accepts every `langchain_*` package
 * Missing module raises `LLMProviderUnavailable` with pip hint
 * Missing attribute raises `LLMConfigError`
 * `register_trusted_prefix` extends the allowlist (idempotent)
"""

import sys
import types

import pytest

from j1.llm import (
    CHAT_MODEL_CATALOG,
    EMBEDDING_CATALOG,
    LLMConfigError,
    LLMProviderUnavailable,
    register_trusted_prefix,
    resolve_callable,
    resolve_chat_model,
    resolve_embedding_model,
)


def _install_fake_module(monkeypatch, name: str, **attrs):
    """Inject a fake module into sys.modules with the given attrs."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


# ---- Catalog -------------------------------------------------------


def test_catalog_has_common_chat_models():
    assert "ChatOpenAI" in CHAT_MODEL_CATALOG
    assert "ChatAnthropic" in CHAT_MODEL_CATALOG
    assert "ChatOllama" in CHAT_MODEL_CATALOG


def test_catalog_has_common_embedding_models():
    assert "OpenAIEmbeddings" in EMBEDDING_CATALOG
    assert "OllamaEmbeddings" in EMBEDDING_CATALOG


def test_resolve_chat_model_via_alias(monkeypatch):
    class _FakeChat: ...
    _install_fake_module(monkeypatch, "langchain_openai", ChatOpenAI=_FakeChat)
    assert resolve_chat_model("ChatOpenAI") is _FakeChat


def test_resolve_embedding_via_alias(monkeypatch):
    class _FakeEmbed: ...
    _install_fake_module(
        monkeypatch, "langchain_openai", OpenAIEmbeddings=_FakeEmbed,
    )
    assert resolve_embedding_model("OpenAIEmbeddings") is _FakeEmbed


# ---- Fully-qualified specs -----------------------------------------


def test_resolve_via_colon_notation(monkeypatch):
    class _FakeChat: ...
    _install_fake_module(monkeypatch, "langchain_xx", MyModel=_FakeChat)
    assert resolve_chat_model("langchain_xx:MyModel") is _FakeChat


def test_resolve_via_dot_notation(monkeypatch):
    class _FakeEmbed: ...
    _install_fake_module(monkeypatch, "langchain_yy", MyEmb=_FakeEmbed)
    assert resolve_embedding_model("langchain_yy.MyEmb") is _FakeEmbed


def test_resolve_callable_works_for_arbitrary_callable(monkeypatch):
    def _myfn(): ...
    _install_fake_module(monkeypatch, "langchain_zz", my=_myfn)
    assert resolve_callable("langchain_zz:my") is _myfn


# ---- Allowlist enforcement -----------------------------------------


def test_rejects_untrusted_top_level_package():
    with pytest.raises(LLMConfigError, match="not on the trusted allowlist"):
        resolve_chat_model("os.path:join")


def test_rejects_random_imports():
    with pytest.raises(LLMConfigError, match="not on the trusted allowlist"):
        resolve_callable("subprocess:run")


# ---- Missing module / attribute ------------------------------------


def test_missing_module_raises_actionable_error():
    """The error must name the package + suggest pip install."""
    with pytest.raises(LLMProviderUnavailable, match="pip install"):
        resolve_chat_model("langchain_definitely_not_real:Model")


def test_missing_attribute_raises_config_error(monkeypatch):
    _install_fake_module(monkeypatch, "langchain_aa")
    with pytest.raises(LLMConfigError, match="has no attribute"):
        resolve_chat_model("langchain_aa:Missing")


# ---- register_trusted_prefix ---------------------------------------


def test_register_trusted_prefix_extends_allowlist(monkeypatch):
    register_trusted_prefix("mycustompkg")
    class _FakeFn: ...
    _install_fake_module(monkeypatch, "mycustompkg.processors", run=_FakeFn)
    assert resolve_callable("mycustompkg.processors:run") is _FakeFn


def test_register_trusted_prefix_idempotent():
    """Registering the same prefix twice is safe."""
    register_trusted_prefix("anothercustompkg")
    register_trusted_prefix("anothercustompkg")  # must not raise


def test_register_trusted_prefix_rejects_empty():
    with pytest.raises(LLMConfigError):
        register_trusted_prefix("   ")


# ---- Spec validation -----------------------------------------------


def test_resolve_rejects_empty_spec():
    with pytest.raises(LLMConfigError, match="non-empty"):
        resolve_chat_model("")


def test_resolve_rejects_unqualified_non_alias():
    """`Bare` names that aren't in the catalog must be rejected."""
    with pytest.raises(LLMConfigError, match="fully-qualified"):
        resolve_chat_model("SomeRandomClass")
