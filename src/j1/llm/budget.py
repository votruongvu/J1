"""Token-budgeting utility for LLM call sites.

Centralised so prompt builders + the OpenAI-compat boundary share
one estimator, one budget arithmetic, and one packing helper. No
external tokenizer (`tiktoken` / `transformers` are intentionally
not added — for early-stage J1 the conservative fallback is good
enough and the dep churn isn't worth it). The interface is shaped
so a real tokenizer can drop in behind `estimate_tokens()` later
without touching call sites.

The boundary in `OpenAICompatTextLLMClient` calls `enforce_budget`
once per request, BEFORE the HTTP call leaves J1. That turns
LM Studio's terse `'Context size has been exceeded'` HTTP 400
into an actionable `LLMContextOverflowError` raised by J1 with
the operator-readable budget breakdown attached.

Centralisation rule: ANY new code that builds prompts longer than
a few hundred chars must go through `pack_text_for_budget` /
`pack_context_items` — not roll its own char/token cap.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

_log = logging.getLogger("j1.llm.budget")


# ---- Estimator -----------------------------------------------------
#
# Two-signal conservative estimator:
#
#   * char_ratio = len(text) / 4   — typical English ~4 chars/token
#   * word_ratio = words / 0.75   — typical English ~0.75 words/token
#
# We take `max(char_ratio, word_ratio)` so neither pure-CJK text
# (which has very few "words" but many chars) nor pure-English
# (where char_ratio under-counts vs word_ratio) trips us up. Plus a
# 10% safety bump because tokenizer drift is real and overestimating
# is preferable to overflow.
#
# This is good enough for the boundary check. When real tokenization
# lands (tiktoken / model-specific BPE), the function gets swapped
# behind the same interface.

_WORD_RE = re.compile(r"\S+")
_CHARS_PER_TOKEN = 4.0
_WORDS_PER_TOKEN = 0.75
# Multiplier on the raw estimate. 1.25 absorbs ~25% drift, which
# covers the difference between this heuristic and a real
# tokenizer (tiktoken's o200k_base / model-specific BPE) for
# technical content where chars/4 frequently underestimates —
# things like dense LightRAG entity-extraction prompts with
# many short tokens (commas, hyphens, JSON keys), or content
# heavy with non-ASCII characters. We'd rather refuse a
# borderline prompt and surface the actionable J1 error than
# slip past our boundary and trip LM Studio's HTTP-400.
_SAFETY_BUMP = 1.25
# Per-message overhead on chat completion APIs (every message
# carries role/content framing). 4 tokens/message is the usual
# tiktoken-derived constant for OpenAI-style chat formats.
_MESSAGE_OVERHEAD_TOKENS = 4


def estimate_tokens(text: str) -> int:
    """Conservative upper-bound estimate of `text`'s token count.

    Returns 0 for empty / whitespace-only input. Always returns
    at least 1 for non-empty input so a single short token doesn't
    round to zero.
    """
    if not text:
        return 0
    char_estimate = len(text) / _CHARS_PER_TOKEN
    words = _WORD_RE.findall(text)
    word_estimate = len(words) / _WORDS_PER_TOKEN
    estimate = max(char_estimate, word_estimate) * _SAFETY_BUMP
    return max(1, int(estimate + 0.5))


def estimate_messages_tokens(
    messages: Sequence[dict],
) -> int:
    """Estimate tokens for an OpenAI-style `messages[]` array.

    Sums per-message content tokens plus a fixed per-message
    overhead for the role/structural framing. `content` may be a
    string or a list of content parts (vision API shape) — we
    walk the list and pick out string `text` / `image_url`
    components.

    Image content is approximated at a fixed cost (~1024 tokens
    per image) — high enough to defend against most-common cases
    without needing the model's actual image-token math.
    """
    total = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    total += estimate_tokens(str(part.get("text", "")))
                elif part.get("type") == "image_url":
                    # Conservative flat cost — different VLMs have
                    # different actual costs (Claude ~1500, GPT-4o
                    # ~700-1100 depending on detail). Pick a value
                    # that's high enough to defend the boundary
                    # without being absurd.
                    total += 1024
        total += _MESSAGE_OVERHEAD_TOKENS
    return total


# ---- TokenBudget ---------------------------------------------------


@dataclass(frozen=True)
class TokenBudget:
    """Budget arithmetic for one LLM call.

    Built from the role's settings (`context_window_tokens`,
    `max_output_tokens`, `safety_margin_tokens`). Callers compute
    `available_input_tokens` once and use it both for pack-time
    decisions (caller-side) and for the boundary safety net (the
    OpenAI-compat client's pre-flight check).

    `context_window_tokens=None` means "unbounded / unknown" and
    `available_input_tokens` returns None — the boundary then
    no-ops (legacy backward compat).
    """

    context_window_tokens: int | None
    reserved_output_tokens: int
    safety_margin_tokens: int = 0

    @property
    def available_input_tokens(self) -> int | None:
        """Tokens the caller may spend on system + user content.

        `None` when no `context_window_tokens` is configured —
        boundary check is then disabled.
        """
        if self.context_window_tokens is None:
            return None
        budget = (
            self.context_window_tokens
            - self.reserved_output_tokens
            - self.safety_margin_tokens
        )
        # Guard against misconfiguration: if the operator set
        # max_output_tokens > context_window_tokens, available
        # would go negative. Surface that as zero so the boundary
        # rejects every prompt with a clear error rather than
        # silently sending tiny ones.
        return max(0, budget)

    def diagnostic_dict(self) -> dict:
        """Compact dict for logging. Excludes anything sensitive."""
        return {
            "contextWindowTokens": self.context_window_tokens,
            "reservedOutputTokens": self.reserved_output_tokens,
            "safetyMarginTokens": self.safety_margin_tokens,
            "availableInputTokens": self.available_input_tokens,
        }


# ---- Packing helpers -----------------------------------------------


@dataclass(frozen=True)
class PackResult:
    """Outcome of `pack_context_items`. Surfaces what was kept and
    what was dropped so callers can log + report the trim decision."""

    kept: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    used_tokens: int = 0
    budget: int = 0

    @property
    def trimmed(self) -> bool:
        return bool(self.dropped)


def pack_text_for_budget(
    text: str,
    max_tokens: int,
    *,
    truncation_marker: str = "\n\n[…truncated to fit token budget…]",
) -> str:
    """Truncate `text` so its estimated tokens fit `max_tokens`.

    Truncation is from the END (we keep the head — usually the
    instruction/question) and a marker is appended so a downstream
    LLM can see the cut explicitly. When `text` already fits, it's
    returned unchanged. When the budget is too small to even
    accommodate the marker, returns just the marker (last-resort
    behaviour — caller should treat this as overflow).

    Estimator is the upper-bound `estimate_tokens` so the resulting
    text is guaranteed-under-budget by the same metric the boundary
    uses.
    """
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    marker_tokens = estimate_tokens(truncation_marker)
    if marker_tokens >= max_tokens:
        # Caller's budget is so tight even the marker doesn't fit.
        # Return the marker alone; caller should treat this as
        # overflow.
        return truncation_marker

    # Binary-search the prefix length whose estimated tokens leave
    # room for the marker. Cheap — text length is bounded by the
    # caller and `estimate_tokens` is O(n).
    target_tokens = max_tokens - marker_tokens
    lo, hi = 0, len(text)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if estimate_tokens(text[:mid]) <= target_tokens:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return text[:best].rstrip() + truncation_marker


def pack_context_items(
    items: Iterable[str],
    max_tokens: int,
) -> PackResult:
    """Pack ranked items into `max_tokens`, highest-priority first.

    Items are consumed in the order given; the first item that
    would push past `max_tokens` (and every item after it) is
    dropped. This is the right behaviour when items arrive
    pre-sorted by score — keep the top-k that fit, never re-rank.

    For the J1 retrieval surface, callers pass `[chunk_body for
    chunk in retrieved_chunks_sorted_by_score]`; the result's
    `kept` is the prompt-ready context, and `dropped` lets the
    caller log "n chunks excluded due to token budget."
    """
    kept: list[str] = []
    dropped: list[str] = []
    used = 0
    for item in items:
        cost = estimate_tokens(item)
        if used + cost <= max_tokens:
            kept.append(item)
            used += cost
        else:
            dropped.append(item)
    return PackResult(kept=kept, dropped=dropped, used_tokens=used, budget=max_tokens)


def enforce_budget(
    *,
    messages: Sequence[dict],
    budget: TokenBudget,
    model: str | None = None,
) -> dict:
    """Raise on overflow; return diagnostics on success.

    The OpenAI-compat client calls this once, immediately before
    `httpx.post`. It returns a dict the client can log at DEBUG
    after a successful call (`estimatedInputTokens`, etc.) so
    operators see the budget arithmetic without having to enable
    verbose logging on the LLM library itself.

    When `budget.available_input_tokens` is None, the function is
    a no-op (returns the diagnostic dict; raises nothing). That's
    the legacy-compat path: deployments that haven't set
    `J1_*_LLM_CONTEXT_WINDOW_TOKENS` keep working unchanged.
    """
    available = budget.available_input_tokens
    estimated = estimate_messages_tokens(messages)
    info = {
        **budget.diagnostic_dict(),
        "model": model,
        "messageCount": len(messages),
        "estimatedInputTokens": estimated,
    }
    if available is None:
        return info
    if estimated > available:
        # Don't reach for an HTTP fail later; turn this into a
        # typed J1 error here so callers see the actionable
        # message + the diagnostic dict, not LM Studio's HTTP 400
        # one round-trip later.
        from j1.llm.errors import LLMContextOverflowError

        _log.warning(
            "LLM context overflow before send: "
            "estimated=%s available=%s window=%s reserved=%s margin=%s "
            "messages=%s model=%s",
            estimated, available,
            budget.context_window_tokens,
            budget.reserved_output_tokens,
            budget.safety_margin_tokens,
            len(messages),
            model,
        )
        raise LLMContextOverflowError(
            f"Estimated input tokens ({estimated}) exceed available "
            f"input budget ({available}) for model {model!r}. "
            f"Configured window={budget.context_window_tokens}, "
            f"reserved output={budget.reserved_output_tokens}, "
            f"safety margin={budget.safety_margin_tokens}. "
            "Reduce retrieval top_k, shrink chunk content, "
            "increase J1_*_LLM_CONTEXT_WINDOW_TOKENS, or pick a "
            "larger-context model.",
            diagnostic=info,
        )
    _log.debug(
        "LLM call within budget: estimated=%s available=%s model=%s "
        "messages=%s",
        estimated, available, model, len(messages),
    )
    return info
