"""Tests for the planner-LLM adapter (`build_llm_planner`)."""

from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from j1.processing.planning_llm import (
    PLANNING_LLM_SYSTEM_PROMPT,
    PlanningLLMError,
    build_llm_planner,
)


class _StubClient:
    """Fakes the `TextLLMClient.generate` shape — captures the prompt
    and returns a canned response."""

    provider = "stub"
    model = "stub-model"

    def __init__(self, response_text: str | Exception) -> None:
        self._response = response_text
        self.calls: list[tuple[str, str | None]] = []

    def generate(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ):
        self.calls.append((prompt, system_prompt))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response, _StubUsage()


class _StubUsage:
    input_tokens = 0
    output_tokens = 0
    cost_usd = None
    metadata: Mapping[str, Any] = {}


def test_planner_serialises_context_and_uses_planner_system_prompt():
    response = json.dumps({"ok": True})
    client = _StubClient(response)
    planner = build_llm_planner(client=client)
    out = planner({"run_id": "r1", "document": {"document_id": "d1"}})
    assert out == {"ok": True}
    prompt, system = client.calls[0]
    assert system == PLANNING_LLM_SYSTEM_PROMPT
    assert "run_id" in prompt
    # Strict JSON serialisation — no Python repr leakage.
    assert "'r1'" not in prompt


def test_planner_extracts_json_from_fenced_response():
    """Tolerate ` ```json …``` ` wrappers some local models use."""
    response = "```json\n{\"recommended_profile\": \"fast\"}\n```"
    client = _StubClient(response)
    planner = build_llm_planner(client=client)
    out = planner({"run_id": "r1"})
    assert out == {"recommended_profile": "fast"}


def test_planner_raises_on_non_json_response():
    client = _StubClient("Sure! Here is your plan: it should run.")
    planner = build_llm_planner(client=client)
    with pytest.raises(PlanningLLMError, match="not valid JSON"):
        planner({"run_id": "r1"})


def test_planner_raises_on_array_response():
    """Top-level arrays don't match the schema; reject early."""
    client = _StubClient("[1,2,3]")
    planner = build_llm_planner(client=client)
    with pytest.raises(PlanningLLMError, match="expected object"):
        planner({"run_id": "r1"})


def test_planner_wraps_provider_exception():
    client = _StubClient(RuntimeError("timeout"))
    planner = build_llm_planner(client=client)
    with pytest.raises(PlanningLLMError, match="timeout"):
        planner({"run_id": "r1"})
