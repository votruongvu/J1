"""Tests for the LLM-call audit wrapper and the heavy-operation
detector.

Pins the operator-facing audit surface:

  * `wrap_audited_async` emits `ingest.stage.llm_call_started`
    BEFORE the wrapped callable runs.
  * It emits `ingest.stage.llm_call_completed` AFTER with
    `duration_ms` + `success` regardless of success or failure.
  * The env flag `J1_LLM_CALL_AUDIT_ENABLED` controls real-LLM
    audit emission; `always_audit=True` overrides (the no-op
    path uses this so the keystone event always fires).
  * Wrapped callables forward args + return values verbatim —
    auditing is observability, never a behaviour change.
  * `emit_heavy_operation_detected` fires unconditionally with
    the documented field shape.

No DOM, no Temporal — pure async unit tests over the wrapper.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from j1.providers.raganything._llm_audit import (
    ENV_LLM_CALL_AUDIT_ENABLED,
    LLMAuditConfig,
    PURPOSE_ENTITY_EXTRACTION,
    PURPOSE_ENTITY_EXTRACTION_NOOP,
    PURPOSE_VISION_ANALYSIS,
    emit_heavy_operation_detected,
    load_llm_audit_config,
    wrap_audited_async,
)


# ---- load_llm_audit_config ---------------------------------------


def test_audit_config_default_is_disabled():
    """Default-off avoids log-spam on workers running real-LLM
    paths. Pinned so a future refactor doesn't flip it."""
    cfg = load_llm_audit_config(env={})
    assert cfg.enabled is False
    assert cfg.should_audit_real_calls() is False


def test_audit_config_enable_via_env():
    cfg = load_llm_audit_config(env={ENV_LLM_CALL_AUDIT_ENABLED: "true"})
    assert cfg.enabled is True
    assert cfg.should_audit_real_calls() is True


def test_audit_config_accepts_canonical_truthy_strings():
    for value in ("true", "TRUE", "1", "yes", "on"):
        cfg = load_llm_audit_config(env={ENV_LLM_CALL_AUDIT_ENABLED: value})
        assert cfg.enabled is True, f"{value!r} should enable audit"


def test_audit_config_falsy_strings_disable():
    for value in ("false", "0", "no", "off", "", "FALSE"):
        cfg = load_llm_audit_config(env={ENV_LLM_CALL_AUDIT_ENABLED: value})
        assert cfg.enabled is False, f"{value!r} should leave audit off"


# ---- wrap_audited_async ------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_wrapper_emits_started_and_completed_when_audit_enabled(caplog):
    async def inner(x: int) -> int:
        return x * 2

    wrapped = wrap_audited_async(
        inner,
        purpose=PURPOSE_ENTITY_EXTRACTION,
        stage="compile",
        provider="openai",
        model="gpt-4o-mini",
        selected_profile="standard",
        config=LLMAuditConfig(enabled=True),
    )
    from j1.providers.raganything import _llm_audit
    with caplog.at_level(logging.INFO, logger=_llm_audit._log.name):
        result = _run(wrapped(21))
    assert result == 42
    events = [r.__dict__.get("event") for r in caplog.records]
    assert "ingest.stage.llm_call_started" in events
    assert "ingest.stage.llm_call_completed" in events


def test_wrapper_marks_success_false_on_exception(caplog):
    async def inner():
        raise RuntimeError("upstream LLM 500")

    wrapped = wrap_audited_async(
        inner,
        purpose=PURPOSE_ENTITY_EXTRACTION,
        stage="compile",
        provider="openai",
        model="gpt-4o-mini",
        selected_profile="standard",
        config=LLMAuditConfig(enabled=True),
    )
    from j1.providers.raganything import _llm_audit
    with caplog.at_level(logging.INFO, logger=_llm_audit._log.name):
        with pytest.raises(RuntimeError, match="upstream LLM 500"):
            _run(wrapped())
    completed = [
        r for r in caplog.records
        if r.__dict__.get("event") == "ingest.stage.llm_call_completed"
    ]
    assert len(completed) == 1
    assert completed[0].__dict__["success"] is False
    # Latency must still be recorded even when the call failed.
    assert isinstance(completed[0].__dict__["duration_ms"], int)


def test_wrapper_skips_emission_when_audit_disabled(caplog):
    """The whole point of the env-gated audit: real-LLM workers
    that haven't opted in must NOT see start/complete log lines
    on every chunk. The wrapper short-circuits to the underlying
    callable without emitting."""
    async def inner(x: int) -> int:
        return x + 1

    wrapped = wrap_audited_async(
        inner,
        purpose=PURPOSE_ENTITY_EXTRACTION,
        stage="compile",
        provider="openai",
        model="gpt-4o-mini",
        selected_profile="standard",
        config=LLMAuditConfig(enabled=False),
    )
    from j1.providers.raganything import _llm_audit
    with caplog.at_level(logging.INFO, logger=_llm_audit._log.name):
        result = _run(wrapped(1))
    assert result == 2
    events = [r.__dict__.get("event") for r in caplog.records]
    assert "ingest.stage.llm_call_started" not in events
    assert "ingest.stage.llm_call_completed" not in events


def test_wrapper_always_audit_overrides_disabled_config(caplog):
    """`always_audit=True` is the keystone honesty escape hatch —
    used by the no-op so its events always fire regardless of
    deployment env state."""
    async def inner():
        return "<|COMPLETE|>"

    wrapped = wrap_audited_async(
        inner,
        purpose=PURPOSE_ENTITY_EXTRACTION_NOOP,
        stage="compile",
        provider="noop",
        model="noop",
        selected_profile="minimum_queryable",
        always_audit=True,
        config=LLMAuditConfig(enabled=False),
    )
    from j1.providers.raganything import _llm_audit
    with caplog.at_level(logging.INFO, logger=_llm_audit._log.name):
        _run(wrapped())
    events = [r.__dict__.get("event") for r in caplog.records]
    assert "ingest.stage.llm_call_started" in events
    assert "ingest.stage.llm_call_completed" in events


def test_wrapper_records_full_audit_payload(caplog):
    """Pin the field shape — dashboards key off these names."""
    async def inner():
        return "ok"

    wrapped = wrap_audited_async(
        inner,
        purpose=PURPOSE_VISION_ANALYSIS,
        stage="compile",
        provider="openai",
        model="gpt-4o",
        selected_profile="advanced",
        config=LLMAuditConfig(enabled=True),
    )
    from j1.providers.raganything import _llm_audit
    with caplog.at_level(logging.INFO, logger=_llm_audit._log.name):
        _run(wrapped())
    started = next(
        r for r in caplog.records
        if r.__dict__.get("event") == "ingest.stage.llm_call_started"
    )
    extra = started.__dict__
    assert extra["purpose"] == "vision_analysis"
    assert extra["stage"] == "compile"
    assert extra["provider"] == "openai"
    assert extra["model"] == "gpt-4o"
    assert extra["selected_profile"] == "advanced"


def test_wrapper_forwards_args_and_kwargs():
    """Auditing is observability, NEVER a behaviour change. Pin
    that args + kwargs flow through unchanged."""
    captured: list[tuple] = []

    async def inner(a, b, *, c=None):
        captured.append((a, b, c))
        return "ok"

    wrapped = wrap_audited_async(
        inner,
        purpose=PURPOSE_ENTITY_EXTRACTION,
        stage="compile",
        provider="x",
        model=None,
        selected_profile=None,
        config=LLMAuditConfig(enabled=True),
    )
    _run(wrapped(1, 2, c="hello"))
    assert captured == [(1, 2, "hello")]


def test_wrapper_preserves_noop_call_count_attribute():
    """The no-op callable exposes `call_count` for the existing
    bridge tests. The wrapper must not hide it."""
    from j1.providers.raganything._noop_llm import make_noop_text_callable

    noop = make_noop_text_callable()
    wrapped = wrap_audited_async(
        noop,
        purpose=PURPOSE_ENTITY_EXTRACTION_NOOP,
        stage="compile",
        provider="noop",
        model="noop",
        selected_profile="minimum_queryable",
        always_audit=True,
        config=LLMAuditConfig(enabled=False),
    )
    _run(wrapped("probe"))
    _run(wrapped("probe again"))
    # The inner counter advanced — wrapper exposes a getter so
    # tests can peek without depending on private fields.
    assert wrapped.get_call_count() == 2  # type: ignore[attr-defined]


# ---- emit_heavy_operation_detected -------------------------------


def test_heavy_operation_detected_emits_with_documented_fields(caplog):
    from j1.providers.raganything import _llm_audit
    with caplog.at_level(logging.INFO, logger=_llm_audit._log.name):
        emit_heavy_operation_detected(
            stage="compile",
            operation="mineru_parse",
            selected_profile="standard",
            detail="parse_method=auto backend=default",
        )
    matching = [
        r for r in caplog.records
        if r.__dict__.get("event") == "ingest.stage.heavy_operation_detected"
    ]
    assert len(matching) == 1
    extra = matching[0].__dict__
    assert extra["stage"] == "compile"
    assert extra["operation"] == "mineru_parse"
    assert extra["selected_profile"] == "standard"
    assert extra["detail"].startswith("parse_method=")


def test_heavy_operation_detail_optional(caplog):
    """`detail` is operator-readable but optional; missing detail
    must NOT crash + must NOT surface a literal `None` field."""
    from j1.providers.raganything import _llm_audit
    with caplog.at_level(logging.INFO, logger=_llm_audit._log.name):
        emit_heavy_operation_detected(
            stage="compile",
            operation="vision_llm_invoke",
            selected_profile=None,
        )
    matching = [
        r for r in caplog.records
        if r.__dict__.get("event") == "ingest.stage.heavy_operation_detected"
    ]
    assert len(matching) == 1
    assert "detail" not in matching[0].__dict__


# ---- Bridge integration: real-LLM path emits real-LLM purpose ----


def test_bridge_wires_real_llm_purpose_when_extraction_enabled(caplog):
    """End-to-end check on the keystone wiring: when
    `disable_entity_extraction=False` (the standard/advanced
    path) and the audit flag is on, every text-LLM invocation
    emits `purpose=entity_extraction` (not the noop purpose)."""
    from unittest.mock import MagicMock, patch
    from j1.providers.raganything import _bridge, _llm_audit

    captured_kwargs: dict = {}

    class _FakeRAGAnything:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

    fake_module = MagicMock()
    fake_module.RAGAnything = _FakeRAGAnything
    fake_module.RAGAnythingConfig = None
    fake_settings = MagicMock()
    fake_settings.workdir = "/tmp/j1-fake-workdir"

    text_client = MagicMock()
    text_client.provider = "lm_studio"
    text_client.model = "qwen2.5-coder-32b"
    text_client.generate = MagicMock(return_value=("entity<|#|>X<|#|>...", None))

    with patch.object(_bridge, "_import_raganything", return_value=fake_module), \
         patch.object(_bridge, "load_llm_audit_config", return_value=LLMAuditConfig(enabled=True)):
        _bridge._build_rag_instance(
            text_client=text_client,
            vision_client=None,
            embedding_client=None,
            settings=fake_settings,
            disable_entity_extraction=False,
            selected_profile="standard",
        )

    llm_func = captured_kwargs["llm_model_func"]
    with caplog.at_level(logging.INFO, logger=_llm_audit._log.name):
        _run(llm_func("extract entities"))
    started = [
        r for r in caplog.records
        if r.__dict__.get("event") == "ingest.stage.llm_call_started"
    ]
    assert len(started) == 1
    extra = started[0].__dict__
    assert extra["purpose"] == "entity_extraction"
    assert extra["provider"] == "lm_studio"
    assert extra["model"] == "qwen2.5-coder-32b"
    assert extra["selected_profile"] == "standard"


def test_bridge_noop_path_always_audits_regardless_of_env_flag(caplog):
    """Even with the audit env flag OFF, the no-op path emits
    its keystone event. This is the honesty guarantee for
    `minimum_queryable` — operators see the no-op fire even on
    workers where real-LLM audit is suppressed."""
    from unittest.mock import MagicMock, patch
    from j1.providers.raganything import _bridge, _llm_audit

    captured_kwargs: dict = {}

    class _FakeRAGAnything:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

    fake_module = MagicMock()
    fake_module.RAGAnything = _FakeRAGAnything
    fake_module.RAGAnythingConfig = None
    fake_settings = MagicMock()
    fake_settings.workdir = "/tmp/j1-fake-workdir"

    with patch.object(_bridge, "_import_raganything", return_value=fake_module), \
         patch.object(_bridge, "load_llm_audit_config", return_value=LLMAuditConfig(enabled=False)):
        _bridge._build_rag_instance(
            text_client=MagicMock(),
            vision_client=MagicMock(),
            embedding_client=None,
            settings=fake_settings,
            disable_entity_extraction=True,
            selected_profile="minimum_queryable",
        )

    llm_func = captured_kwargs["llm_model_func"]
    with caplog.at_level(logging.INFO, logger=_llm_audit._log.name):
        _run(llm_func("any prompt"))
    started = [
        r for r in caplog.records
        if r.__dict__.get("event") == "ingest.stage.llm_call_started"
    ]
    assert len(started) == 1
    extra = started[0].__dict__
    assert extra["purpose"] == "entity_extraction_noop_minimum_queryable"
    assert extra["provider"] == "noop"
    assert extra["selected_profile"] == "minimum_queryable"
