"""Tests for the `minimum_queryable` no-op `llm_model_func` injection.

Pins three contracts:

 1. The no-op callable returns LightRAG's "no entities found"
    sentinel (the default completion delimiter) so the library's
    extraction parser produces empty maybe_nodes / maybe_edges
    without warnings.
 2. Each invocation increments `call_count` so an audit hook can
    prove the no-op fired N times (and the real LLM fired 0 times).
 3. `_build_rag_instance(..., disable_entity_extraction=True)`
    actually wires the no-op into the constructed RAGAnything,
    AND drops the vision callable so no multimodal-LLM path leaks
    into a profile that promised none.

Pure unit tests — no I/O, no real LightRAG, no real LLM.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from j1.providers.raganything._noop_llm import make_noop_text_callable


# ---- No-op callable contract -------------------------------------


def test_noop_returns_lightrag_completion_delimiter():
    noop = make_noop_text_callable()
    result = asyncio.run(noop("any prompt"))
    # LightRAG's parser splits on this token; returning JUST this
    # produces an empty record list and no "Complete delimiter
    # cannot be found" warning. Pinned because a vendor change
    # here would surface as silent extraction warnings.
    assert result == "<|COMPLETE|>"


def test_noop_call_count_increments():
    noop = make_noop_text_callable()
    assert noop.call_count == 0
    asyncio.run(noop("first"))
    asyncio.run(noop("second"))
    asyncio.run(noop("third"))
    assert noop.call_count == 3


def test_noop_accepts_lightrag_callable_signature():
    """LightRAG awaits `llm_model_func(prompt, system_prompt=...,
    history_messages=[...], **kwargs)`. The no-op must accept this
    signature without raising — a `TypeError` here would crash
    compile."""
    noop = make_noop_text_callable()
    result = asyncio.run(
        noop(
            "extract entities from this chunk",
            system_prompt="You are an entity extractor.",
            history_messages=[{"role": "user", "content": "prior turn"}],
            arbitrary_vendor_kwarg="ignored",
        ),
    )
    assert result == "<|COMPLETE|>"


def test_noop_calls_on_call_hook_with_audit_payload():
    """The audit hook must see a narrow, sanitised payload — not
    full prompt strings. The 120-char preview cap is the
    contract."""
    captured: list[dict] = []
    noop = make_noop_text_callable(on_call=captured.append)
    long_prompt = "x" * 500
    asyncio.run(noop(long_prompt, system_prompt="y" * 200, history_messages=[{}, {}]))
    assert len(captured) == 1
    payload = captured[0]
    assert payload["prompt_preview"] == "x" * 120
    assert payload["system_prompt_preview"] == "y" * 120
    assert payload["history_messages_count"] == 2


def test_noop_swallows_on_call_hook_failures():
    """Audit hook failures must never crash compile. The library's
    error log is acceptable; raising is not."""
    def explodes(_payload: dict) -> None:
        raise RuntimeError("audit hook broken")

    noop = make_noop_text_callable(on_call=explodes)
    # Should not raise — even when the hook does.
    result = asyncio.run(noop("any prompt"))
    assert result == "<|COMPLETE|>"
    assert noop.call_count == 1


# ---- Bridge wiring -----------------------------------------------


def test_build_rag_instance_uses_noop_when_disable_entity_extraction_true():
    """When `disable_entity_extraction=True` the bridge MUST pass
    the no-op callable into the RAGAnything constructor — never
    the real text-LLM callable. Verifies the keystone wiring."""
    from j1.providers.raganything import _bridge

    captured_kwargs: dict = {}

    class _FakeRAGAnything:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

    fake_module = MagicMock()
    fake_module.RAGAnything = _FakeRAGAnything
    # `_build_rag_config` returns (None, []) when RAGAnythingConfig
    # is missing; mirror that on the fake module.
    fake_module.RAGAnythingConfig = None

    fake_settings = MagicMock()
    fake_settings.workdir = "/tmp/j1-fake-workdir"

    with patch.object(_bridge, "_import_raganything", return_value=fake_module):
        rag, _dropped = _bridge._build_rag_instance(
            text_client=MagicMock(),
            vision_client=MagicMock(),
            embedding_client=None,
            settings=fake_settings,
            config_overrides=None,
            working_dir_override=None,
            disable_entity_extraction=True,
        )

    llm_func = captured_kwargs.get("llm_model_func")
    assert llm_func is not None, "llm_model_func must always be wired"
    # The no-op callable carries the `call_count` attribute the
    # real callable does NOT — that's how the audit hook can
    # tell them apart.
    assert hasattr(llm_func, "call_count"), (
        "expected no-op llm_model_func (carries `call_count`) when "
        "disable_entity_extraction=True; got the real text callable"
    )
    # Confirm it actually short-circuits.
    assert asyncio.run(llm_func("probe")) == "<|COMPLETE|>"


def test_build_rag_instance_drops_vision_when_disable_entity_extraction_true():
    """`minimum_queryable` promises no multimodal LLM calls. Even
    if the caller supplies a `vision_client`, the bridge must drop
    `vision_model_func` from the constructor kwargs so no path
    leaks vision-LLM use."""
    from j1.providers.raganything import _bridge

    captured_kwargs: dict = {}

    class _FakeRAGAnything:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

    fake_module = MagicMock()
    fake_module.RAGAnything = _FakeRAGAnything
    fake_module.RAGAnythingConfig = None

    fake_settings = MagicMock()
    fake_settings.workdir = "/tmp/j1-fake-workdir"

    with patch.object(_bridge, "_import_raganything", return_value=fake_module):
        _bridge._build_rag_instance(
            text_client=MagicMock(),
            vision_client=MagicMock(),  # supplied but must be dropped
            embedding_client=None,
            settings=fake_settings,
            config_overrides=None,
            working_dir_override=None,
            disable_entity_extraction=True,
        )

    assert "vision_model_func" not in captured_kwargs, (
        "vision_model_func must be dropped when "
        "disable_entity_extraction=True so no vision-LLM call slips "
        "into a profile that promised none"
    )


def test_build_rag_instance_uses_real_llm_when_flag_false():
    """Negative case: with the flag off, the real text callable
    must be wired (call_count attribute is absent on the real one)."""
    from j1.providers.raganything import _bridge

    captured_kwargs: dict = {}

    class _FakeRAGAnything:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

    fake_module = MagicMock()
    fake_module.RAGAnything = _FakeRAGAnything
    fake_module.RAGAnythingConfig = None

    fake_settings = MagicMock()
    fake_settings.workdir = "/tmp/j1-fake-workdir"

    with patch.object(_bridge, "_import_raganything", return_value=fake_module):
        _bridge._build_rag_instance(
            text_client=MagicMock(),
            vision_client=MagicMock(),
            embedding_client=None,
            settings=fake_settings,
            config_overrides=None,
            working_dir_override=None,
            disable_entity_extraction=False,
        )

    llm_func = captured_kwargs.get("llm_model_func")
    assert llm_func is not None
    assert not hasattr(llm_func, "call_count"), (
        "expected real text callable when disable_entity_extraction=False; "
        "got the no-op (carrying `call_count`)"
    )
    # Vision should be wired in the normal case.
    assert "vision_model_func" in captured_kwargs


def test_build_rag_instance_defaults_keep_legacy_behaviour():
    """Backward compatibility: callers that don't pass the new flag
    get the real LLM and real vision callable, unchanged."""
    from j1.providers.raganything import _bridge

    captured_kwargs: dict = {}

    class _FakeRAGAnything:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

    fake_module = MagicMock()
    fake_module.RAGAnything = _FakeRAGAnything
    fake_module.RAGAnythingConfig = None

    fake_settings = MagicMock()
    fake_settings.workdir = "/tmp/j1-fake-workdir"

    with patch.object(_bridge, "_import_raganything", return_value=fake_module):
        # No `disable_entity_extraction` kwarg.
        _bridge._build_rag_instance(
            text_client=MagicMock(),
            vision_client=MagicMock(),
            embedding_client=None,
            settings=fake_settings,
        )

    llm_func = captured_kwargs.get("llm_model_func")
    assert llm_func is not None
    assert not hasattr(llm_func, "call_count")
    assert "vision_model_func" in captured_kwargs
