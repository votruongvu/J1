"""Unit tests for the token-budget utility.

Covers:
 * `estimate_tokens` — char/word floor, safety bump, edge cases.
 * `estimate_messages_tokens` — per-message overhead, image
 cost, malformed parts.
 * `TokenBudget.available_input_tokens` — arithmetic, clamping,
 None passthrough.
 * `pack_text_for_budget` — fit short, truncate long, marker
 placement, empty-budget edge.
 * `pack_context_items` — keep highest-priority, drop overflow,
 return order.
 * `enforce_budget` — no-op when window is None, raises
 `LLMContextOverflowError` with diagnostic dict on overflow,
 succeeds + returns diagnostic on fit.
"""

from __future__ import annotations

import pytest

from j1.llm.budget import (
    PackResult,
    TokenBudget,
    enforce_budget,
    estimate_messages_tokens,
    estimate_tokens,
    pack_context_items,
    pack_text_for_budget,
)
from j1.llm.errors import LLMContextOverflowError


# ---- estimate_tokens ----------------------------------------------


def test_estimate_tokens_zero_for_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0  # type: ignore[arg-type]  — defensive


def test_estimate_tokens_returns_at_least_one_for_non_empty():
    """Single short token must not round to zero — that would
 let a one-char input look free under any budget."""
    assert estimate_tokens("a") >= 1


def test_estimate_tokens_grows_with_length():
    short = estimate_tokens("hello world")
    long = estimate_tokens("hello world " * 100)
    assert long > short
    # Linear-ish growth — locks the contract that doubling the
    # input doesn't accidentally collapse to a constant.
    assert long > 100


def test_estimate_tokens_includes_safety_bump():
    """The estimator multiplies by ~1.10 for safety. A 100-char
 pure-ASCII string should estimate to MORE than 25 tokens
 (raw len(text)/4 = 25; safety bump pushes higher)."""
    text = "a" * 100
    # Word-based path: 1 "word" → 1/0.75 = 1.33 tokens. Char-based
    # path: 100/4 = 25 tokens. Max is 25; *1.10 = 27.5 → 28.
    assert estimate_tokens(text) >= 27


def test_estimate_tokens_handles_unicode_safely():
    """CJK / Vietnamese text shouldn't round-trip to zero. The
 char path saves us when the word-tokenizer can't split.
 Locked here so a future regex change doesn't silently
 under-estimate non-ASCII content."""
    cjk = "你好世界" * 20  # 80 chars, no whitespace
    estimate = estimate_tokens(cjk)
    # 80 / 4 = 20; safety bump → ~22.
    assert estimate >= 22


# ---- estimate_messages_tokens -------------------------------------


def test_estimate_messages_includes_per_message_overhead():
    """Two empty messages should still cost 8 tokens (4 per
 message overhead). Locks the OpenAI-style chat-format
 framing accounting."""
    messages = [
        {"role": "system", "content": ""},
        {"role": "user", "content": ""},
    ]
    assert estimate_messages_tokens(messages) >= 8


def test_estimate_messages_sums_content_tokens():
    """Two messages with content → sum of content + framing."""
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Tell me about Python."},
    ]
    individual = (
        estimate_tokens("You are a helpful assistant.")
        + estimate_tokens("Tell me about Python.")
    )
    assert estimate_messages_tokens(messages) >= individual + 8


def test_estimate_messages_handles_vision_content_parts():
    """Vision API: `content` is a list with text + image_url
 parts. Image parts get a flat conservative cost so the
 boundary defends against most cases."""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What's in this image?"},
                {"type": "image_url", "image_url": {"url": "data:..."}},
            ],
        }
    ]
    estimate = estimate_messages_tokens(messages)
    # Image cost is large by design (1024) so the budget catches
    # vision overflow even before content arithmetic.
    assert estimate >= 1024


def test_estimate_messages_skips_unknown_part_types():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "audio_url", "audio_url": {"url": "..."}},
                "not a dict",
            ],
        }
    ]
    # Per-message overhead only — unknown parts contribute zero.
    assert estimate_messages_tokens(messages) == 4


# ---- TokenBudget --------------------------------------------------


def test_budget_arithmetic():
    """available = window - reserved_output - safety_margin."""
    budget = TokenBudget(
        context_window_tokens=8192,
        reserved_output_tokens=1024,
        safety_margin_tokens=256,
    )
    assert budget.available_input_tokens == 8192 - 1024 - 256


def test_budget_disabled_when_no_window():
    """`context_window_tokens=None` → available is None → boundary
 check is a no-op. Critical backward-compat path."""
    budget = TokenBudget(
        context_window_tokens=None,
        reserved_output_tokens=1024,
        safety_margin_tokens=256,
    )
    assert budget.available_input_tokens is None


def test_budget_clamps_at_zero_when_misconfigured():
    """If max_output_tokens > context_window, we'd otherwise go
 negative. Clamp at zero so the boundary rejects every prompt
 with a clear error — a misconfiguration shouldn't silently
 let absurdly small prompts through either."""
    budget = TokenBudget(
        context_window_tokens=512,
        reserved_output_tokens=2048,
        safety_margin_tokens=256,
    )
    assert budget.available_input_tokens == 0


def test_budget_diagnostic_dict_carries_all_fields():
    """Diagnostic shape locked — operators read these keys off
 error logs to know which knob to turn."""
    budget = TokenBudget(
        context_window_tokens=8192,
        reserved_output_tokens=1024,
        safety_margin_tokens=256,
    )
    info = budget.diagnostic_dict()
    assert info["contextWindowTokens"] == 8192
    assert info["reservedOutputTokens"] == 1024
    assert info["safetyMarginTokens"] == 256
    assert info["availableInputTokens"] == 6912


# ---- pack_text_for_budget ----------------------------------------


def test_pack_text_returns_unchanged_when_under_budget():
    text = "short"
    assert pack_text_for_budget(text, max_tokens=100) == text


def test_pack_text_truncates_when_over_budget():
    """Long text → truncated + marker appended. Caller sees the
 cut explicitly; downstream models can recognise the marker."""
    long_text = "alpha beta gamma delta " * 200  # ~1200 tokens
    out = pack_text_for_budget(long_text, max_tokens=50)
    assert len(out) < len(long_text)
    assert "truncated to fit token budget" in out
    # Result is at-budget by the same estimator the boundary uses.
    assert estimate_tokens(out) <= 50


def test_pack_text_empty_budget_returns_empty():
    """Zero-budget short-circuits to empty — caller will hit the
 boundary check next and surface the actionable error."""
    assert pack_text_for_budget("anything", max_tokens=0) == ""


def test_pack_text_marker_only_when_too_tight():
    """When budget is too small for even the marker, return just
 the marker — caller treats this as overflow."""
    out = pack_text_for_budget("alpha " * 1000, max_tokens=2)
    # The marker IS roughly that long; result is the marker
    # itself, no body content.
    assert "truncated to fit token budget" in out
    # No body content survived — only the marker.
    assert "alpha" not in out


# ---- pack_context_items ------------------------------------------


def test_pack_context_keeps_top_items():
    """First 2 fit; third pushes past — gets dropped. Locks the
 'rank order; never re-rank' contract.

 Budget computed from the actual estimator instead of a fixed
 integer so the test stays valid when the safety-bump constant
 changes (the contract — keep first two, drop third — does not).
 """
    items = ["aaaa" * 10, "bbbb" * 10, "cccc" * 10]
    # Set a budget that fits exactly two items but not three.
    per_item = estimate_tokens(items[0])
    budget = per_item * 2 + 1  # +1 for slack between items
    result = pack_context_items(items, max_tokens=budget)
    assert len(result.kept) == 2
    assert len(result.dropped) == 1
    assert result.kept[0] == items[0]
    assert result.kept[1] == items[1]
    assert result.dropped[0] == items[2]


def test_pack_context_returns_pack_result_with_used_and_budget():
    items = ["short"]
    result = pack_context_items(items, max_tokens=100)
    assert isinstance(result, PackResult)
    assert result.budget == 100
    assert result.used_tokens >= 1
    assert result.trimmed is False


def test_pack_context_marks_trimmed_when_anything_dropped():
    items = ["x" * 1000, "y"]
    result = pack_context_items(items, max_tokens=10)
    # First item too big → dropped; second fits.
    assert result.trimmed is True


def test_pack_context_empty_input():
    result = pack_context_items([], max_tokens=100)
    assert result.kept == []
    assert result.dropped == []
    assert result.used_tokens == 0


# ---- enforce_budget -----------------------------------------------


def test_enforce_budget_passes_when_under_budget():
    messages = [{"role": "user", "content": "hello"}]
    budget = TokenBudget(
        context_window_tokens=8192,
        reserved_output_tokens=1024,
        safety_margin_tokens=256,
    )
    info = enforce_budget(messages=messages, budget=budget, model="m")
    assert info["estimatedInputTokens"] >= 1
    assert info["model"] == "m"
    assert info["messageCount"] == 1


def test_enforce_budget_no_op_when_window_none():
    """The legacy compat path: no `context_window_tokens` → no
 raise, returns the diagnostic. Existing deployments that
 haven't opted in must NOT see new errors."""
    big = "x" * 100_000
    messages = [{"role": "user", "content": big}]
    budget = TokenBudget(
        context_window_tokens=None,
        reserved_output_tokens=1024,
    )
    info = enforce_budget(messages=messages, budget=budget, model="m")
    # No exception. Estimated input is huge; available is None.
    assert info["availableInputTokens"] is None
    assert info["estimatedInputTokens"] > 1000


def test_enforce_budget_raises_with_diagnostic_on_overflow():
    """Critical: raises BEFORE any HTTP call. Diagnostic dict on
 the exception carries the budget arithmetic so operators
 know which knob to turn."""
    messages = [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "x " * 5000},  # ~6000+ tokens
    ]
    budget = TokenBudget(
        context_window_tokens=4096,
        reserved_output_tokens=1024,
        safety_margin_tokens=256,
    )
    with pytest.raises(LLMContextOverflowError) as excinfo:
        enforce_budget(messages=messages, budget=budget, model="tiny")
    err = excinfo.value
    assert err.diagnostic["contextWindowTokens"] == 4096
    assert err.diagnostic["reservedOutputTokens"] == 1024
    assert err.diagnostic["safetyMarginTokens"] == 256
    assert err.diagnostic["estimatedInputTokens"] > 0
    assert err.diagnostic["model"] == "tiny"
    # Message must name the actionable knobs.
    assert "context_window" in str(err).lower() or "context window" in str(err).lower()


def test_enforce_budget_overflow_when_clamped_to_zero():
    """Misconfigured window (output reserve > window) → available
 clamps to 0 → every non-empty prompt overflows. This makes a
 misconfiguration visible immediately at the first call rather
 than letting it through silently."""
    messages = [{"role": "user", "content": "x"}]
    budget = TokenBudget(
        context_window_tokens=512,
        reserved_output_tokens=2048,
        safety_margin_tokens=256,
    )
    with pytest.raises(LLMContextOverflowError):
        enforce_budget(messages=messages, budget=budget, model="m")
