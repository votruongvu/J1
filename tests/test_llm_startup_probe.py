"""Tests for the LLM startup connectivity probe.

Locks two contracts:

  1. The probe exercises EVERY registered TEXT / FAST / EMBEDDING
     role with a minimal request and reports per-role results.
  2. `assert_required_llm_reachable` raises with an operator-readable
     message when any probed role fails — the worker / api startup
     hooks aborting on this raise is what gives us the fail-fast
     behaviour.
"""

from __future__ import annotations

import pytest

from j1.llm.clients import LLMUsage
from j1.llm.errors import LLMProviderUnavailable
from j1.llm.probe import (
    LLMStartupProbeError,
    ProbeResult,
    assert_required_llm_reachable,
    llm_probe_enabled,
    probe_registry,
)
from j1.llm.registry import (
    LLM_ROLE_EMBEDDING,
    LLM_ROLE_FAST,
    LLM_ROLE_TEXT,
    LLMProviderRegistry,
)


class _OkText:
    provider = "openai_compat"
    model = "fake-model"

    def __init__(self):
        self.calls: list[dict] = []

    def generate(self, prompt, *, max_output_tokens=None, temperature=None, **_):
        self.calls.append({
            "prompt": prompt,
            "max_output_tokens": max_output_tokens,
            "temperature": temperature,
        })
        return ("pong", LLMUsage(provider=self.provider, model=self.model,
                                 input_tokens=1, output_tokens=1, total_tokens=2))


class _FailingText:
    provider = "openai_compat"
    model = "down-model"

    def generate(self, prompt, **_):
        raise LLMProviderUnavailable("connection refused: 192.168.1.85:1234")


class _OkEmbedding:
    provider = "openai_compat"
    model = "embed-model"

    def __init__(self):
        self.calls: list[str] = []

    def embed_text(self, text):
        self.calls.append(text)
        return ([0.1, 0.2, 0.3], LLMUsage(provider=self.provider,
                                          model=self.model,
                                          input_tokens=1, output_tokens=0,
                                          total_tokens=1))


class _FailingEmbedding:
    provider = "openai_compat"
    model = "embed-down"

    def embed_text(self, text):
        raise LLMProviderUnavailable("HTTP 503: model not loaded")


# ---- llm_probe_enabled --------------------------------------------------


def test_llm_probe_enabled_default_true():
    assert llm_probe_enabled(env={}) is True


@pytest.mark.parametrize("value", ["false", "False", "0", "no", "off"])
def test_llm_probe_enabled_respects_falsy_overrides(value):
    assert llm_probe_enabled(env={"J1_LLM_STARTUP_PROBE": value}) is False


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "anything-else"])
def test_llm_probe_enabled_respects_truthy_overrides(value):
    assert llm_probe_enabled(env={"J1_LLM_STARTUP_PROBE": value}) is True


# ---- probe_registry shape ----------------------------------------------


def test_probe_registry_returns_one_result_per_registered_role():
    """Three configured roles → three ProbeResults with provider/model
    populated. Roles that aren't registered are silently skipped (the
    operator opted not to configure them)."""
    registry = LLMProviderRegistry()
    registry.register(LLM_ROLE_TEXT, _OkText())
    registry.register(LLM_ROLE_EMBEDDING, _OkEmbedding())

    results = probe_registry(registry)

    assert len(results) == 2
    assert all(isinstance(r, ProbeResult) for r in results)
    assert {r.role for r in results} == {LLM_ROLE_TEXT, LLM_ROLE_EMBEDDING}
    assert all(r.ok for r in results)
    assert all(r.provider == "openai_compat" for r in results)


def test_probe_registry_calls_text_generate_with_one_token_cap():
    """The text probe must use `max_output_tokens=1` so it never burns
    budget. The deterministic `temperature=0.0` keeps the probe
    reproducible — same response shape on every check."""
    text = _OkText()
    registry = LLMProviderRegistry()
    registry.register(LLM_ROLE_TEXT, text)

    probe_registry(registry)

    assert len(text.calls) == 1
    assert text.calls[0]["max_output_tokens"] == 1
    assert text.calls[0]["temperature"] == 0.0


def test_probe_registry_calls_embedding_embed_text():
    embed = _OkEmbedding()
    registry = LLMProviderRegistry()
    registry.register(LLM_ROLE_EMBEDDING, embed)

    probe_registry(registry)

    assert embed.calls == ["a"]


def test_probe_registry_marks_failing_role_as_not_ok():
    registry = LLMProviderRegistry()
    registry.register(LLM_ROLE_TEXT, _OkText())
    registry.register(LLM_ROLE_FAST, _FailingText())

    results = probe_registry(registry)

    by_role = {r.role: r for r in results}
    assert by_role[LLM_ROLE_TEXT].ok is True
    assert by_role[LLM_ROLE_FAST].ok is False
    assert "connection refused" in by_role[LLM_ROLE_FAST].error
    assert "LLMProviderUnavailable" in by_role[LLM_ROLE_FAST].error


def test_probe_registry_empty_when_no_roles_configured():
    """No probed roles registered → no results. Caller's higher-level
    `assert_required_llm_reachable` then logs 'no probed roles
    registered; skipping' rather than raising."""
    registry = LLMProviderRegistry()
    assert probe_registry(registry) == []


# ---- assert_required_llm_reachable -------------------------------------


def test_assert_succeeds_when_all_roles_reachable():
    registry = LLMProviderRegistry()
    registry.register(LLM_ROLE_TEXT, _OkText())
    registry.register(LLM_ROLE_EMBEDDING, _OkEmbedding())
    # Must not raise.
    assert_required_llm_reachable(registry)


def test_assert_succeeds_when_no_probed_roles_registered():
    """An empty registry isn't a misconfiguration — `bootstrap_from_env`
    constructs an empty registry when no LLM is configured at all
    (mock-only deployments). Don't fail startup on that case."""
    registry = LLMProviderRegistry()
    assert_required_llm_reachable(registry)


def test_assert_raises_with_actionable_message_on_failure():
    registry = LLMProviderRegistry()
    registry.register(LLM_ROLE_TEXT, _FailingText())

    with pytest.raises(LLMStartupProbeError) as excinfo:
        assert_required_llm_reachable(registry)

    msg = str(excinfo.value)
    # Operator-facing: explains WHAT failed + WHERE to look + that
    # the bypass exists for tests.
    assert "cannot start" in msg
    assert LLM_ROLE_TEXT in msg
    assert "openai_compat" in msg
    assert "down-model" in msg
    assert "connection refused" in msg
    assert "J1_LLM_STARTUP_PROBE" in msg


def test_assert_raises_when_any_role_fails_even_if_others_pass():
    """One bad role poisons the well — startup must abort even when
    the other roles are healthy. We can't ingest documents with a
    half-functional LLM stack."""
    registry = LLMProviderRegistry()
    registry.register(LLM_ROLE_TEXT, _OkText())
    registry.register(LLM_ROLE_EMBEDDING, _FailingEmbedding())

    with pytest.raises(LLMStartupProbeError) as excinfo:
        assert_required_llm_reachable(registry)

    msg = str(excinfo.value)
    # Only the failing role gets named in the failures list (the
    # passing one is summarised in the INFO log lines, not the
    # raised message — keeps the error focused on what to fix).
    assert LLM_ROLE_EMBEDDING in msg
    assert "model not loaded" in msg
