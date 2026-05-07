"""Tests for the LLM role abstraction.

Covers:
  * `load_llm_settings` parses every J1_TEXT_LLM_*, J1_VISION_LLM_*,
    J1_EMBEDDING_* env var
  * Invalid provider / numeric / JSON values raise `LLMConfigError`
  * `is_configured` correctly distinguishes provider types
  * `LLMProviderRegistry` registration, resolution, validation,
    diagnostics — including secrets-safe diagnostic shape
  * `OpenAICompatTextLLMClient` / Vision / Embedding all enforce
    base_url + model presence
  * OpenAI-compat clients perform an HTTP POST with the bearer token
    and parse the standard response shape (using a stub httpx)
  * LangChain adapter is fully optional — constructor raises
    `LLMProviderUnavailable` when langchain-core isn't installed
  * Capability errors surface for unsupported features (e.g.
    embedding dimension when not configured)

Tests do NOT make real network calls. The OpenAI-compat tests stub
`httpx` at the module level so the fixture is hermetic.
"""

import json
import sys
import types
from collections.abc import Mapping
from typing import Any

import pytest

from j1.errors.exceptions import ConfigError
from j1.llm import (
    EmbeddingSettings,
    LLM_ROLE_EMBEDDING,
    LLM_ROLE_TEXT,
    LLM_ROLE_VISION,
    LLMCapabilityError,
    LLMConfigError,
    LLMProviderRegistry,
    LLMProviderUnavailable,
    LLMRoleNotRegistered,
    LLMUsage,
    OpenAICompatEmbeddingClient,
    OpenAICompatTextLLMClient,
    OpenAICompatVisionLLMClient,
    PROVIDER_LANGCHAIN,
    PROVIDER_OPENAI_COMPAT,
    SUPPORTED_PROVIDERS,
    TextLLMSettings,
    VisionLLMSettings,
    load_llm_settings,
)


# ---- Settings loading -----------------------------------------------


def test_load_llm_settings_defaults_when_env_empty():
    settings = load_llm_settings(env={})
    assert settings.text.provider == PROVIDER_OPENAI_COMPAT
    assert settings.vision.provider == PROVIDER_OPENAI_COMPAT
    assert settings.embedding.provider == PROVIDER_OPENAI_COMPAT
    # No base_url / model → not configured (composition root will skip)
    assert settings.text.is_configured is False
    assert settings.vision.is_configured is False
    assert settings.embedding.is_configured is False


def test_load_llm_settings_reads_text_role():
    settings = load_llm_settings(env={
        "J1_TEXT_LLM_PROVIDER": "openai_compat",
        "J1_TEXT_LLM_BASE_URL": "https://api.example.com/v1",
        "J1_TEXT_LLM_API_KEY": "secret",
        "J1_TEXT_LLM_MODEL": "qwen-plus",
        "J1_TEXT_LLM_TEMPERATURE": "0.5",
        "J1_TEXT_LLM_MAX_OUTPUT_TOKENS": "8192",
    })
    assert settings.text.base_url == "https://api.example.com/v1"
    assert settings.text.api_key == "secret"
    assert settings.text.model == "qwen-plus"
    assert settings.text.temperature == 0.5
    assert settings.text.max_output_tokens == 8192
    assert settings.text.is_configured is True


def test_load_llm_settings_reads_embedding_role_with_dim():
    settings = load_llm_settings(env={
        "J1_EMBEDDING_BASE_URL": "https://api.example.com/v1",
        "J1_EMBEDDING_MODEL": "text-embedding-v3",
        "J1_EMBEDDING_DIM": "1536",
        "J1_EMBEDDING_BATCH_SIZE": "16",
    })
    assert settings.embedding.dimension == 1536
    assert settings.embedding.batch_size == 16


def test_invalid_provider_raises_config_error():
    with pytest.raises(LLMConfigError, match="not a supported"):
        load_llm_settings(env={"J1_TEXT_LLM_PROVIDER": "made-up"})


def test_invalid_int_raises_config_error():
    with pytest.raises(LLMConfigError, match="must be an integer"):
        load_llm_settings(env={"J1_TEXT_LLM_MAX_OUTPUT_TOKENS": "lots"})


def test_invalid_json_config_raises():
    with pytest.raises(LLMConfigError, match="must be valid JSON"):
        load_llm_settings(env={
            "J1_TEXT_LLM_PROVIDER": "langchain",
            "J1_TEXT_LLM_LANGCHAIN_CONFIG": "{not json",
        })


def test_langchain_provider_uses_provider_config():
    settings = load_llm_settings(env={
        "J1_TEXT_LLM_PROVIDER": "langchain",
        "J1_TEXT_LLM_LANGCHAIN_CONFIG": '{"class": "ChatOpenAI"}',
    })
    assert settings.text.provider == PROVIDER_LANGCHAIN
    assert settings.text.provider_config == {"class": "ChatOpenAI"}
    # LangChain settings are "configured" once provider_config is non-empty
    assert settings.text.is_configured is True


def test_supported_providers_set():
    assert PROVIDER_OPENAI_COMPAT in SUPPORTED_PROVIDERS
    assert PROVIDER_LANGCHAIN in SUPPORTED_PROVIDERS
    assert len(SUPPORTED_PROVIDERS) == 2


# ---- Registry --------------------------------------------------------


class _StubText:
    provider = "stub"
    model = "stub-text"


class _StubVision:
    provider = "stub"
    model = "stub-vision"


class _StubEmbed:
    provider = "stub"
    model = "stub-embed"

    def dimension(self) -> int:
        return 768


def test_registry_register_and_resolve():
    reg = LLMProviderRegistry()
    text = _StubText()
    reg.register(LLM_ROLE_TEXT, text)
    assert reg.resolve(LLM_ROLE_TEXT) is text
    assert reg.has(LLM_ROLE_TEXT)
    assert reg.list() == ("text",)


def test_registry_resolve_missing_raises():
    reg = LLMProviderRegistry()
    with pytest.raises(LLMRoleNotRegistered):
        reg.resolve(LLM_ROLE_TEXT)


def test_registry_try_resolve_returns_none():
    reg = LLMProviderRegistry()
    assert reg.try_resolve(LLM_ROLE_TEXT) is None


def test_registry_validate_required_raises_for_missing():
    reg = LLMProviderRegistry({"text": _StubText()})
    reg.validate_required(["text"])  # ok
    with pytest.raises(LLMRoleNotRegistered):
        reg.validate_required(["text", "vision"])


def test_registry_diagnostics_does_not_leak_secrets():
    """The registry's diagnostic output MUST never include base_url / api_key."""
    settings = TextLLMSettings(
        provider="openai_compat",
        base_url="https://api.example.com/secret-path",
        api_key="sk-do-not-log-me",
        model="qwen-plus",
    )
    client = OpenAICompatTextLLMClient(settings)
    reg = LLMProviderRegistry({"text": client})
    diag = reg.diagnostics()
    assert "text" in diag
    assert diag["text"]["provider"] == "openai_compat"
    assert diag["text"]["model"] == "qwen-plus"
    # The hostname / api key MUST NOT appear anywhere in the dict
    serialised = json.dumps(diag)
    assert "sk-do-not-log-me" not in serialised
    assert "secret-path" not in serialised


def test_registry_typed_helpers_narrow():
    """`text() / vision() / embedding()` are typed convenience accessors."""
    reg = LLMProviderRegistry({
        "text": _StubText(),
        "vision": _StubVision(),
        "embedding": _StubEmbed(),
    })
    assert reg.text().model == "stub-text"  # type: ignore[union-attr]
    assert reg.vision().model == "stub-vision"  # type: ignore[union-attr]
    assert reg.embedding().model == "stub-embed"  # type: ignore[union-attr]
    # try_* variants return None when missing:
    empty = LLMProviderRegistry()
    assert empty.try_text() is None
    assert empty.try_vision() is None
    assert empty.try_embedding() is None


def test_registry_register_normalises_role_case():
    reg = LLMProviderRegistry()
    reg.register("TEXT", _StubText())
    assert reg.has("text")
    assert reg.resolve("Text") is not None


def test_registry_register_empty_role_rejected():
    reg = LLMProviderRegistry()
    with pytest.raises(LLMConfigError):
        reg.register("   ", _StubText())


# ---- OpenAI-compat clients ------------------------------------------


def test_text_client_requires_base_url():
    with pytest.raises(LLMConfigError, match="base_url"):
        OpenAICompatTextLLMClient(TextLLMSettings(
            provider="openai_compat", model="qwen-plus",
        ))


def test_text_client_requires_model():
    with pytest.raises(LLMConfigError, match="model"):
        OpenAICompatTextLLMClient(TextLLMSettings(
            provider="openai_compat", base_url="https://x",
        ))


def _stub_httpx(monkeypatch, *, response_json: dict | None = None,
                status_code: int = 200, raises: Exception | None = None):
    """Inject a stub `httpx` module; record every POST so tests can assert it."""
    calls: list[dict] = []

    class _StubResponse:
        def __init__(self, payload, code):
            self._payload = payload
            self.status_code = code
            self.text = json.dumps(payload) if payload else ""

        def json(self):
            return self._payload

    class _TimeoutException(Exception):
        pass

    class _TransportError(Exception):
        pass

    def _post(url, *, headers=None, json=None, timeout=None):
        calls.append({
            "url": url, "headers": dict(headers or {}),
            "body": json, "timeout": timeout,
        })
        if raises is not None:
            raise raises
        return _StubResponse(response_json or {}, status_code)

    stub = types.SimpleNamespace(
        post=_post,
        TimeoutException=_TimeoutException,
        TransportError=_TransportError,
    )
    monkeypatch.setitem(sys.modules, "httpx", stub)
    return calls


def test_text_client_generate_posts_to_chat_completions(monkeypatch):
    calls = _stub_httpx(monkeypatch, response_json={
        "choices": [{"message": {"content": "hello world"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    })
    client = OpenAICompatTextLLMClient(TextLLMSettings(
        provider="openai_compat",
        base_url="https://api.example.com/v1",
        api_key="topsecret",
        model="qwen-plus",
    ))
    text, usage = client.generate("hello?")
    assert text == "hello world"
    assert usage.input_tokens == 5
    assert usage.output_tokens == 2
    assert usage.total_tokens == 7
    assert calls[0]["url"] == "https://api.example.com/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer topsecret"
    assert calls[0]["body"]["model"] == "qwen-plus"
    assert calls[0]["body"]["messages"][-1]["content"] == "hello?"


def test_text_client_extract_returns_parsed_json(monkeypatch):
    _stub_httpx(monkeypatch, response_json={
        "choices": [{"message": {"content": '{"kind": "doc"}'}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })
    client = OpenAICompatTextLLMClient(TextLLMSettings(
        provider="openai_compat",
        base_url="https://x", api_key="k", model="m",
    ))
    parsed, usage = client.extract("input", schema={"kind": "string"})
    assert parsed == {"kind": "doc"}


def test_text_client_extract_sends_json_schema_response_format(monkeypatch):
    """Regression: when a schema is supplied, the body must carry the
    newer `response_format={type: 'json_schema', json_schema: {...}}`
    shape, not the older `{type: 'json_object'}`. LM Studio rejects
    `json_object` outright with `'response_format.type' must be
    'json_schema' or 'text'` — that 400 was breaking every enricher
    when pointed at LM Studio."""
    schema = {"type": "object", "properties": {"kind": {"type": "string"}}}
    calls = _stub_httpx(monkeypatch, response_json={
        "choices": [{"message": {"content": '{"kind": "doc"}'}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })
    client = OpenAICompatTextLLMClient(TextLLMSettings(
        provider="openai_compat",
        base_url="https://x", api_key="k", model="m",
    ))
    client.extract("input", schema=schema)
    rf = calls[0]["body"]["response_format"]
    assert rf["type"] == "json_schema"
    # The schema we passed in must round-trip into the request body so
    # strict-mode endpoints (real OpenAI, vLLM with guided decoding) can
    # actually constrain the output.
    assert rf["json_schema"]["schema"] == schema
    # `name` is required by OpenAI's strict structured-output contract.
    assert rf["json_schema"].get("name")


def test_text_client_4xx_raises_provider_unavailable(monkeypatch):
    _stub_httpx(monkeypatch, response_json={"error": "bad"}, status_code=400)
    client = OpenAICompatTextLLMClient(TextLLMSettings(
        provider="openai_compat",
        base_url="https://x", api_key="k", model="m",
    ))
    with pytest.raises(LLMProviderUnavailable, match="HTTP 400"):
        client.generate("hi")


def test_text_client_retries_on_transport_error(monkeypatch):
    """First call raises, second succeeds — retries up to max_retries."""
    import httpx as _stub  # noqa: F401  (stub installed by previous fixtures? no — install fresh)

    attempts = {"n": 0}

    class _StubResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {},
            }

    class _Timeout(Exception):
        pass

    class _Transport(Exception):
        pass

    def _post(url, *, headers=None, json=None, timeout=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _Transport("connection refused")
        return _StubResponse()

    stub = types.SimpleNamespace(
        post=_post, TimeoutException=_Timeout, TransportError=_Transport,
    )
    monkeypatch.setitem(sys.modules, "httpx", stub)

    client = OpenAICompatTextLLMClient(TextLLMSettings(
        provider="openai_compat", base_url="https://x", api_key="k",
        model="m", max_retries=2,
    ))
    text, _usage = client.generate("hi")
    assert text == "ok"
    assert attempts["n"] == 2


def test_embedding_client_batches_and_aggregates_usage(monkeypatch):
    calls = _stub_httpx(monkeypatch, response_json={
        "data": [
            {"embedding": [1.0, 2.0, 3.0]},
            {"embedding": [4.0, 5.0, 6.0]},
        ],
        "usage": {"prompt_tokens": 10},
    })
    client = OpenAICompatEmbeddingClient(EmbeddingSettings(
        provider="openai_compat",
        base_url="https://x", api_key="k",
        model="text-embedding-v3", dimension=3, batch_size=2,
    ))
    vectors, usage = client.embed_batch(["a", "b"])
    assert vectors == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    assert usage.input_tokens == 10
    assert calls[0]["body"]["model"] == "text-embedding-v3"
    assert calls[0]["body"]["input"] == ["a", "b"]


def test_embedding_dimension_unset_raises_capability_error():
    client = OpenAICompatEmbeddingClient(EmbeddingSettings(
        provider="openai_compat",
        base_url="https://x", api_key="k", model="m", dimension=None,
    ))
    with pytest.raises(LLMCapabilityError):
        client.dimension()


def test_vision_client_sends_image_inline(monkeypatch):
    calls = _stub_httpx(monkeypatch, response_json={
        "choices": [{"message": {"content": "a cat"}}],
        "usage": {},
    })
    client = OpenAICompatVisionLLMClient(VisionLLMSettings(
        provider="openai_compat",
        base_url="https://x", api_key="k", model="qwen-vl-plus",
    ))
    text, _usage = client.analyze_image(b"\x89PNG", prompt="what is this?")
    assert text == "a cat"
    body = calls[0]["body"]
    parts = body["messages"][0]["content"]
    assert parts[0]["type"] == "text"
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


# ---- LangChain adapter (optional, lazy) ------------------------------


def test_langchain_text_client_raises_clean_error_when_unavailable(monkeypatch):
    # Force the import to fail by removing langchain_core from sys.modules
    # AND inserting a sentinel that raises ImportError.
    monkeypatch.setitem(sys.modules, "langchain_core", None)
    from j1.llm.langchain_adapter import LangChainTextLLMClient

    class _FakeChatModel:
        pass

    with pytest.raises(LLMProviderUnavailable, match="langchain-core"):
        LangChainTextLLMClient(
            _FakeChatModel(),
            settings=TextLLMSettings(provider="langchain"),
        )


def test_langchain_text_client_with_stubbed_langchain(monkeypatch):
    """Inject a fake `langchain_core` so the adapter imports successfully."""
    fake_module = types.ModuleType("langchain_core")
    monkeypatch.setitem(sys.modules, "langchain_core", fake_module)

    fake_messages = types.ModuleType("langchain_core.messages")

    class _Msg:
        def __init__(self, content): self.content = content

    class _HumanMessage(_Msg): pass

    class _SystemMessage(_Msg): pass

    fake_messages.HumanMessage = _HumanMessage
    fake_messages.SystemMessage = _SystemMessage
    monkeypatch.setitem(sys.modules, "langchain_core.messages", fake_messages)

    from j1.llm.langchain_adapter import LangChainTextLLMClient

    class _FakeChatModel:
        model_name = "fake-model"

        def invoke(self, messages):
            joined = "|".join(m.content for m in messages)
            response = types.SimpleNamespace(
                content=f"echo: {joined}",
                usage_metadata={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
            )
            return response

    client = LangChainTextLLMClient(
        _FakeChatModel(),
        settings=TextLLMSettings(provider="langchain"),
    )
    assert client.provider == "langchain"
    assert client.model == "fake-model"
    text, usage = client.generate("hello")
    assert text == "echo: hello"
    assert usage.input_tokens == 4
    assert usage.output_tokens == 2


# ---- LangChain `from_settings` env-driven auto-construction ---------


def _stub_langchain_for_chat(monkeypatch, *, openai_class):
    """Install a fake `langchain_core` + `langchain_openai` for chat tests."""
    monkeypatch.setitem(sys.modules, "langchain_core", types.ModuleType("langchain_core"))
    fake_lc_openai = types.ModuleType("langchain_openai")
    fake_lc_openai.ChatOpenAI = openai_class
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_lc_openai)


def _stub_langchain_for_embedding(monkeypatch, *, embed_class):
    monkeypatch.setitem(sys.modules, "langchain_core", types.ModuleType("langchain_core"))
    fake_lc_openai = types.ModuleType("langchain_openai")
    fake_lc_openai.OpenAIEmbeddings = embed_class
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_lc_openai)


def test_langchain_text_from_settings_auto_instantiates(monkeypatch):
    """The shipped catalog alias resolves + the model is constructed with config kwargs."""
    captured: dict = {}

    class _FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.model_name = kwargs.get("model", "unknown")

    _stub_langchain_for_chat(monkeypatch, openai_class=_FakeChat)

    from j1.llm import LangChainTextLLMClient
    settings = TextLLMSettings(
        provider="langchain",
        model="gpt-4o-mini",
        provider_config={"class": "ChatOpenAI", "api_key": "sk-fake"},
        temperature=0.3,
    )
    client = LangChainTextLLMClient.from_settings(settings)
    assert client.model == "gpt-4o-mini"
    assert captured["api_key"] == "sk-fake"
    assert captured["model"] == "gpt-4o-mini"
    assert captured["temperature"] == 0.3
    assert captured["max_tokens"] == settings.max_output_tokens


def test_langchain_text_from_settings_uses_fully_qualified_class(monkeypatch):
    """`langchain_xx:CustomClass` form works without a catalog entry."""
    captured: dict = {}

    class _FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.model_name = "vendor-xx-model"

    monkeypatch.setitem(sys.modules, "langchain_core", types.ModuleType("langchain_core"))
    mod = types.ModuleType("langchain_xx")
    mod.CustomChat = _FakeChat
    monkeypatch.setitem(sys.modules, "langchain_xx", mod)

    from j1.llm import LangChainTextLLMClient
    settings = TextLLMSettings(
        provider="langchain",
        provider_config={"class": "langchain_xx:CustomChat", "api_key": "k"},
    )
    client = LangChainTextLLMClient.from_settings(settings)
    assert client.model == "vendor-xx-model"


def test_langchain_text_from_settings_requires_class_in_config(monkeypatch):
    """Missing `class` field → actionable LLMConfigError naming the env var."""
    monkeypatch.setitem(sys.modules, "langchain_core", types.ModuleType("langchain_core"))
    from j1.llm import LangChainTextLLMClient

    with pytest.raises(LLMConfigError, match="`class` field"):
        LangChainTextLLMClient.from_settings(TextLLMSettings(
            provider="langchain",
            provider_config={"api_key": "x"},  # no `class`
        ))


def test_langchain_embedding_from_settings(monkeypatch):
    captured: dict = {}

    class _FakeEmbed:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.model = kwargs.get("model", "unknown")

    _stub_langchain_for_embedding(monkeypatch, embed_class=_FakeEmbed)

    from j1.llm import LangChainEmbeddingClient
    settings = EmbeddingSettings(
        provider="langchain",
        model="text-embedding-3-small",
        dimension=1536,
        provider_config={"class": "OpenAIEmbeddings", "api_key": "sk-x"},
    )
    client = LangChainEmbeddingClient.from_settings(settings)
    assert client.dimension() == 1536
    assert captured["model"] == "text-embedding-3-small"
    assert captured["api_key"] == "sk-x"


def test_langchain_from_settings_propagates_pip_install_hint_when_module_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "langchain_core", types.ModuleType("langchain_core"))
    from j1.llm import LangChainTextLLMClient

    with pytest.raises(LLMProviderUnavailable, match="pip install"):
        LangChainTextLLMClient.from_settings(TextLLMSettings(
            provider="langchain",
            provider_config={"class": "langchain_definitely_not_a_real_pkg:M"},
        ))


# ---- base_url normalisation -------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    # Idempotent on a correctly-shaped URL.
    ("https://api.openai.com/v1", "https://api.openai.com/v1"),
    # Strip a trailing slash.
    ("https://api.openai.com/v1/", "https://api.openai.com/v1"),
    # Strip an accidentally-pasted full chat URL — the most common
    # operator footgun ("I copied the example from the OpenAI docs").
    ("https://api.openai.com/v1/chat/completions", "https://api.openai.com/v1"),
    # Same with trailing slash.
    ("https://api.openai.com/v1/chat/completions/", "https://api.openai.com/v1"),
    # Embeddings leaf.
    ("https://api.openai.com/v1/embeddings", "https://api.openai.com/v1"),
    # /completions (legacy) and /responses (new Responses API) — both stripped.
    ("https://api.openai.com/v1/completions", "https://api.openai.com/v1"),
    ("https://api.openai.com/v1/responses", "https://api.openai.com/v1"),
    # Whitespace tolerance — env-var values often pick up leading/trailing space.
    ("  https://api.openai.com/v1  ", "https://api.openai.com/v1"),
    # Self-hosted endpoints without `/v1` are passed through (we don't
    # auto-add — vendor-dependent).
    ("http://vllm-host:8000", "http://vllm-host:8000"),
])
def test_normalize_base_url(raw, expected):
    from j1.llm.openai_compat import _normalize_base_url
    assert _normalize_base_url(raw) == expected


def test_text_client_strips_full_chat_url_at_construction(monkeypatch):
    """Operator pastes the full `…/chat/completions` URL into `base_url`
    by mistake. Without normalisation this produces a request to
    `…/chat/completions/chat/completions` and 404s. We strip the
    leaf so the actual request lands on the right endpoint."""
    calls = _stub_httpx(monkeypatch, response_json={
        "choices": [{"message": {"content": "ok"}}],
        "usage": {},
    })
    client = OpenAICompatTextLLMClient(TextLLMSettings(
        provider="openai_compat",
        base_url="https://api.example.com/v1/chat/completions",
        api_key="k",
        model="m",
    ))
    client.generate("hi")
    assert calls[0]["url"] == "https://api.example.com/v1/chat/completions", (
        f"expected suffix to be stripped at construction, got URL: {calls[0]['url']!r}"
    )


def test_embedding_client_strips_full_embeddings_url_at_construction(monkeypatch):
    """Same shape as the text-client test but for the embeddings leaf."""
    calls = _stub_httpx(monkeypatch, response_json={
        "data": [{"embedding": [0.1, 0.2]}],
        "usage": {"prompt_tokens": 1},
    })
    client = OpenAICompatEmbeddingClient(EmbeddingSettings(
        provider="openai_compat",
        base_url="https://api.example.com/v1/embeddings/",
        api_key="k",
        model="m",
        dimension=2,
    ))
    client.embed_text("hi")
    assert calls[0]["url"] == "https://api.example.com/v1/embeddings"


# ---- 404 error message guides operator to the misconfiguration --------


def test_404_error_message_includes_url_and_base_url_hint(monkeypatch):
    """A 404 from the upstream endpoint must surface enough context that
    the operator can fix their config without reading the framework
    source — the constructed URL, the originally-configured base_url,
    and a hint about the most common cause (missing `/v1` or
    non-OpenAI-compatible endpoint)."""
    _stub_httpx(monkeypatch, response_json={"error": "not found"}, status_code=404)
    client = OpenAICompatTextLLMClient(TextLLMSettings(
        provider="openai_compat",
        base_url="https://api.example.com",  # missing /v1 — the bug
        api_key="k",
        model="m",
    ))
    with pytest.raises(LLMProviderUnavailable) as excinfo:
        client.generate("hi")
    msg = str(excinfo.value)

    # The constructed URL must be in the message — that's the
    # single most important piece of info for debugging.
    assert "https://api.example.com/chat/completions" in msg
    # The user's originally-configured base_url is also surfaced
    # so they can spot the typo / missing segment.
    assert "https://api.example.com" in msg
    # And there's a hint about the version segment.
    assert "/v1" in msg or "version segment" in msg


def test_non_404_4xx_still_raises_clean_error(monkeypatch):
    """The 404-specific hint must not bleed into other 4xx codes —
    a 401 (auth failure) shouldn't suggest the URL is wrong."""
    _stub_httpx(
        monkeypatch,
        response_json={"error": "invalid api key"},
        status_code=401,
    )
    client = OpenAICompatTextLLMClient(TextLLMSettings(
        provider="openai_compat",
        base_url="https://api.example.com/v1",
        api_key="bad",
        model="m",
    ))
    with pytest.raises(LLMProviderUnavailable) as excinfo:
        client.generate("hi")
    msg = str(excinfo.value)
    assert "HTTP 401" in msg
    assert "https://api.example.com/v1/chat/completions" in msg
    # The version-segment hint is reserved for 404s.
    assert "Common causes" not in msg
