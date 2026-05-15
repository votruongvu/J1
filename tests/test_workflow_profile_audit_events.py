"""Tests for the profile + LLM-call audit events emitted by the
ingestion workflow.

These events are the operator's `grep` surface for answering
"what profile did this run use, and did any hidden LLM call fire
that the profile said was disabled?" Pin them here so the field
names stay stable.

Three event types are covered:

  - `ingest.profile.selected`   — emitted at workflow start
  - `ingest.profile.recommended` — emitted after the planner runs
  - `ingest.stage.llm_call_started` — emitted by the no-op LLM
    when it short-circuits an entity-extraction call (proves the
    keystone is firing instead of a real LLM)

The workflow-side events are tested by invoking `_log_profile_event`
directly and asserting on the logger payload — no Temporal runtime
required. The bridge-side LLM event is tested by exercising the
no-op callable's `on_call` audit hook.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

from j1.orchestration.workflows.project_processing import (
    ProjectProcessingRequest,
    ProjectProcessingWorkflow,
)
from j1.orchestration.activities.payloads import ProjectScope


def _request(**overrides) -> ProjectProcessingRequest:
    base = {
        "scope": ProjectScope(tenant_id="acme", project_id="alpha"),
        "compiler_kind": "raganything",
        "correlation_id": "run-xyz",
    }
    base.update(overrides)
    return ProjectProcessingRequest(**base)


def _capture_workflow_logger() -> list[dict]:
    """Capture every `workflow.logger.info` payload emitted during
    a `_log_profile_event` call. Returns the list of `extra` dicts
    in call order so tests can assert on event name + fields.

    Replaces the Temporal `workflow` module's logger with a stub —
    the workflow code path tolerates that because it's wrapped in
    a try/except that swallows runtime errors. Without the stub,
    `workflow.logger.info` raises `WorkflowNotInWorkflowError`
    outside the sandbox and the audit event is silently lost,
    making the test green for the wrong reason.
    """
    captured: list[dict] = []

    class _StubLogger:
        def info(self, _event: str, *, extra: dict | None = None) -> None:
            if extra is not None:
                captured.append(dict(extra))

    from j1.orchestration.workflows import project_processing as wf_module
    return captured, _StubLogger, wf_module


# ---- ingest.profile.selected -------------------------------------


def test_profile_selected_event_carries_run_id_and_profile():
    captured, StubLogger, wf_module = _capture_workflow_logger()
    wf = ProjectProcessingWorkflow()
    with patch.object(wf_module.workflow, "logger", StubLogger()):
        wf._log_profile_event(
            _request(selected_execution_profile="minimum_queryable"),
            event="ingest.profile.selected",
            selected_profile="minimum_queryable",
            reason="profile threaded from REST request",
        )
    assert len(captured) == 1
    payload = captured[0]
    assert payload["event"] == "ingest.profile.selected"
    assert payload["selected_profile"] == "minimum_queryable"
    assert payload["run_id"] == "run-xyz"
    assert payload["reason"] == "profile threaded from REST request"


def test_profile_selected_event_omits_optional_fields_when_unset():
    """Hygiene: a missing field MUST NOT appear as `None` in the
    payload (that would surface as a literal `"None"` string in
    log aggregators with permissive JSON formatters)."""
    captured, StubLogger, wf_module = _capture_workflow_logger()
    wf = ProjectProcessingWorkflow()
    with patch.object(wf_module.workflow, "logger", StubLogger()):
        wf._log_profile_event(
            _request(),
            event="ingest.profile.selected",
            selected_profile="standard",
        )
    payload = captured[0]
    assert "recommended_profile" not in payload
    assert "purpose" not in payload
    assert "model" not in payload


# ---- ingest.profile.recommended ---------------------------------


def test_profile_recommended_event_carries_both_profiles():
    """The whole point: a divergence between what the planner
    recommended and what the user selected must be visible in a
    single log line."""
    captured, StubLogger, wf_module = _capture_workflow_logger()
    wf = ProjectProcessingWorkflow()
    with patch.object(wf_module.workflow, "logger", StubLogger()):
        wf._log_profile_event(
            _request(selected_execution_profile="minimum_queryable"),
            event="ingest.profile.recommended",
            document_id="doc-a",
            recommended_profile="advanced",
            selected_profile="minimum_queryable",
            reason="Document contains tables",
        )
    payload = captured[0]
    assert payload["event"] == "ingest.profile.recommended"
    assert payload["recommended_profile"] == "advanced"
    assert payload["selected_profile"] == "minimum_queryable"
    assert payload["document_id"] == "doc-a"


# ---- ingest.stage.llm_call_started (bridge side) -----------------


def test_noop_llm_emits_audit_event_per_call(caplog):
    """When the no-op `llm_model_func` short-circuits an entity
    extraction call, it must log a structured event so the
    operator can prove `minimum_queryable` is honest. Without
    this, a regression that re-wires the real LLM into the
    minimum profile would be invisible."""
    # Build the bridge instance with the no-op + an audit hook,
    # mirroring what `_build_rag_instance` does in production.
    from j1.providers.raganything import _bridge
    from j1.providers.raganything._noop_llm import make_noop_text_callable

    events: list[dict] = []

    def _hook(payload: dict) -> None:
        events.append(payload)

    noop = make_noop_text_callable(on_call=_hook)
    with caplog.at_level(logging.INFO, logger=_bridge._log.name):
        asyncio.run(noop("extract entities from chunk"))
        asyncio.run(noop("another chunk"))

    assert len(events) == 2
    # Per-call hook fired with the documented payload shape.
    for evt in events:
        assert "prompt_preview" in evt
        assert "history_messages_count" in evt


def test_bridge_logs_llm_call_started_when_noop_runs(caplog):
    """Integration view: when `_build_rag_instance` wires the
    no-op AND a real call would happen, the bridge logger emits
    `ingest.stage.llm_call_started` with the canonical purpose
    string. Asserts on the worker log surface that operators
    will actually grep."""
    from j1.providers.raganything import _bridge
    from unittest.mock import MagicMock

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
            disable_entity_extraction=True,
        )

    # The bridge installed the no-op; now fire it and confirm the
    # audit log line lands with the canonical event name + purpose.
    # The unified `_llm_audit` wrapper logs through its own
    # module-level logger now (post-Phase-C refactor), so we
    # subscribe to that logger rather than the bridge's.
    from j1.providers.raganything import _llm_audit
    llm_func = captured_kwargs["llm_model_func"]
    with caplog.at_level(logging.INFO, logger=_llm_audit._log.name):
        asyncio.run(llm_func("any prompt"))

    matching = [
        r for r in caplog.records
        if r.msg == "ingest.stage.llm_call_started"
    ]
    assert len(matching) == 1
    extras = matching[0].__dict__
    assert extras.get("event") == "ingest.stage.llm_call_started"
    assert extras.get("purpose") == "entity_extraction_noop_minimum_queryable"
    assert extras.get("selected_profile") == "minimum_queryable"
    assert extras.get("provider") == "noop"
