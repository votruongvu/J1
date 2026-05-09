"""Regression tests for `_make_text_callable`.

The headline contract: the wrapper MUST forward `system_prompt`
and `history_messages` to the underlying client. Dropping them
caused two real-world failures:

  1. LightRAG's entity-extraction prompt (carried in
     `system_prompt`) never reached the model, producing
     degenerate outputs.
  2. Our token-budget pre-flight check at the OpenAI-compat
     boundary saw only the small `prompt` argument (not the
     bulky system+history) — underestimated → let the request
     through → LM Studio's HTTP-400 'Context size has been
     exceeded' fired downstream.

This file locks both contracts:

  * `system_prompt` and folded-`history_messages` content
    reach `text_client.generate`.
  * The token-budget check at the OpenAI-compat boundary sees
    the full payload (system + history + user) and raises
    `LLMContextOverflowError` BEFORE the HTTP request when
    the assembled content overflows.
"""

from __future__ import annotations

import asyncio

import pytest

from j1.llm import (
    LLMContextOverflowError,
    OpenAICompatTextLLMClient,
    TextLLMSettings,
)
from j1.providers.raganything._bridge import _make_text_callable


def _run(coro):
    """Run an awaitable from sync test code without creating
    cross-test loops. Each call gets its own loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _RecordingClient:
    """Stub client that records every kwarg. Mirrors the
    `TextLLMClient.generate(prompt, *, system_prompt, ...)`
    contract so the wrapper's forward path is exercised
    end-to-end without any HTTP."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate(self, prompt: str, *, system_prompt=None, **kwargs):
        self.calls.append({
            "prompt": prompt,
            "system_prompt": system_prompt,
            "kwargs": kwargs,
        })
        from j1.llm.clients import LLMUsage
        return ("OK", LLMUsage(0, 0, 0))


def test_wrapper_forwards_system_prompt():
    """LightRAG passes the entity-extraction template via
    `system_prompt=`. Wrapper MUST forward it; before the fix
    it dropped the kwarg.

    Wrapper also injects `/no_think` into both system + user
    prompts to suppress qwen3 reasoning mode. Original content
    must still reach the model unmodified."""
    client = _RecordingClient()
    callable_ = _make_text_callable(client)

    _run(callable_(
        prompt="user content",
        system_prompt="ENTITY EXTRACTION TEMPLATE",
    ))

    assert len(client.calls) == 1
    forwarded_system = client.calls[0]["system_prompt"]
    assert "ENTITY EXTRACTION TEMPLATE" in forwarded_system
    assert "/no_think" in forwarded_system
    # User content is forwarded; `/no_think` prepended.
    assert "user content" in client.calls[0]["prompt"]
    assert "/no_think" in client.calls[0]["prompt"]


def test_wrapper_folds_history_into_prompt():
    """LightRAG's gleaning pass also passes `history_messages=`
    with the prior turns. Wrapper folds them into the user
    prompt as a labelled block so (a) the model still sees
    every turn and (b) the budget check at the boundary
    estimates the FULL payload."""
    client = _RecordingClient()
    callable_ = _make_text_callable(client)

    _run(callable_(
        prompt="continue extracting",
        system_prompt="extract entities",
        history_messages=[
            {"role": "user", "content": "first chunk"},
            {"role": "assistant", "content": "(entity1, entity2)"},
        ],
    ))

    assert len(client.calls) == 1
    forwarded = client.calls[0]["prompt"]
    # History is present in the prompt.
    assert "first chunk" in forwarded
    assert "(entity1, entity2)" in forwarded
    # Original user content is also present (folded after history).
    assert "continue extracting" in forwarded
    # System prompt forwarded with `/no_think` prepended; original
    # template content must still reach the model.
    assert "extract entities" in client.calls[0]["system_prompt"]
    assert "/no_think" in client.calls[0]["system_prompt"]


def test_wrapper_skips_empty_history():
    """`history_messages=[]` or None → wrapper passes prompt
    through without folding history (no 'USER:' label). The
    `/no_think` prefix is unconditional; the user's original
    content must still be present and unaltered."""
    client = _RecordingClient()
    callable_ = _make_text_callable(client)

    _run(callable_(prompt="just user", system_prompt="sys"))

    assert "just user" in client.calls[0]["prompt"]
    assert "USER:" not in client.calls[0]["prompt"]


def test_wrapper_drops_malformed_history_entries():
    """Defensive: history_messages with non-dict entries or
    missing content must not crash the wrapper. LightRAG variants
    have shipped malformed history before."""
    client = _RecordingClient()
    callable_ = _make_text_callable(client)

    _run(callable_(
        prompt="x",
        history_messages=[
            None,  # not a dict
            {"role": "user"},  # missing content
            "string entry",  # not a dict
            {"role": "user", "content": "valid"},
        ],
    ))

    # Only the valid entry survives the filter.
    forwarded = client.calls[0]["prompt"]
    assert "valid" in forwarded
    # Junk didn't crash + isn't in the prompt.
    assert "None" not in forwarded


def test_wrapper_ignores_unknown_kwargs():
    """LightRAG passes extra kwargs (`_priority`, `cache_type`,
    etc.) we don't care about. Wrapper must not propagate them
    into `text_client.generate` — that would TypeError on
    unknown args."""
    client = _RecordingClient()
    callable_ = _make_text_callable(client)

    _run(callable_(
        prompt="x",
        system_prompt="sys",
        _priority=8,           # LightRAG cooperative-priority hint
        cache_type="extract",  # LightRAG cache routing
        chunk_id="c-1",
    ))

    # Got through without raising. Unknown kwargs swallowed.
    assert len(client.calls) == 1


def test_oversize_lightrag_prompt_now_caught_by_budget():
    """End-to-end regression: an oversize LightRAG-style call
    (small `prompt` + big `system_prompt`) must raise
    `LLMContextOverflowError` BEFORE any HTTP request. Before
    the fix, the wrapper dropped `system_prompt`, the budget
    check saw only the small user message, the HTTP request
    went through, and LM Studio rejected it with
    `'Context size has been exceeded'` HTTP 400."""
    # Real client (no HTTP stub needed — we never reach `_post`'s
    # httpx call because the boundary check raises first).
    client = OpenAICompatTextLLMClient(TextLLMSettings(
        provider="openai_compat",
        base_url="https://x", api_key="k", model="tiny",
        max_output_tokens=2048,
        context_window_tokens=4096,
        safety_margin_tokens=256,
    ))
    callable_ = _make_text_callable(client)

    # Big system prompt (LightRAG's entity-extraction template +
    # few-shot examples in real life). Small user prompt (the
    # chunk-content extraction continues here).
    huge_system = "ENTITY EXTRACTION CONTEXT. " * 1500  # ~6500 tokens
    user_prompt = "Continue extraction."

    with pytest.raises(LLMContextOverflowError) as excinfo:
        _run(callable_(prompt=user_prompt, system_prompt=huge_system))

    err = excinfo.value
    # Diagnostic dict carries the actionable knobs.
    assert err.diagnostic["contextWindowTokens"] == 4096
    assert err.diagnostic["model"] == "tiny"
    # Estimated input MUST include both system + user. Before
    # the fix, only the user message was estimated; that bug
    # would surface as a too-small `estimatedInputTokens`.
    assert err.diagnostic["estimatedInputTokens"] > 1000


def test_history_visible_to_budget_check():
    """Same shape as the system_prompt regression but for
    history. Big history → budget check sees the folded
    content → raises before HTTP."""
    client = OpenAICompatTextLLMClient(TextLLMSettings(
        provider="openai_compat",
        base_url="https://x", api_key="k", model="tiny",
        max_output_tokens=2048,
        context_window_tokens=4096,
    ))
    callable_ = _make_text_callable(client)

    huge_history = [
        {"role": "user", "content": "previous content. " * 2000},
        {"role": "assistant", "content": "previous answer. " * 500},
    ]

    with pytest.raises(LLMContextOverflowError):
        _run(callable_(
            prompt="next turn",
            system_prompt="sys",
            history_messages=huge_history,
        ))


def test_normal_sized_call_still_works():
    """Sanity check: well-sized prompts pass through. Locks the
    contract that the budget tightening doesn't accidentally
    block reasonable calls."""
    client = _RecordingClient()
    callable_ = _make_text_callable(client)

    out = _run(callable_(
        prompt="What is the proposal due date?",
        system_prompt="You are a helpful assistant.",
        history_messages=[
            {"role": "user", "content": "Hello."},
            {"role": "assistant", "content": "Hi there."},
        ],
    ))
    assert out == "OK"
    assert len(client.calls) == 1
