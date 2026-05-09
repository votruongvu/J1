"""Adapter: turn a registered LLM client into a planning `LLMPlanner`.

The planning core (`build_planning_result`) accepts a callable that
takes a `PlanningContext` dict and returns a parsed JSON dict. This
module supplies the production implementation backed by the existing
LLM registry — kept here (not in the activity) so it stays unit-
testable and the activity stays a thin wrapper.

Privacy invariant: every prompt the LLM sees is built from the
already-capped planning context. The full document never reaches
this module.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Mapping

from j1.llm.clients import TextLLMClient


__all__ = [
    "PLANNING_LLM_SYSTEM_PROMPT",
    "PLANNING_LLM_USER_PROMPT",
    "PlanningLLMError",
    "build_llm_planner",
]


_log = logging.getLogger("j1.planning.llm")


# Spec system prompt — see the user's "LLM PLANNER SYSTEM PROMPT"
# section. Kept in source so deployments can override by subclassing
# the adapter or by registering their own LLMPlanner callable.
PLANNING_LLM_SYSTEM_PROMPT = """\
You are the J1 Ingestion Planning Analyst.

Your job is to analyze a parsed document inventory and produce a practical ingestion execution plan.

You are NOT processing the full document.
You are NOT answering questions from the document.
You are NOT rewriting the document.
You are only planning the remaining ingestion steps after parsing.

The input you receive is a compact planning context generated after:
1. MinerU/RAGAnything/LightRAG compile completed.
2. Content Inventory was generated.
3. Document Intent & Type Assessment was generated.
4. A deterministic rule-based post-compile assessment was executed.

You must use:
- document metadata
- lightweight content digest
- document understanding
- content inventory statistics
- parse quality signals
- rule-based recommendations
- selected short previews only

You must NOT require full raw document content.

Before recommending execution steps, first verify the document's type, purpose, topic, likely business value, and expected information types.
Use the title when it is meaningful.
If the title is missing, generic, or ambiguous, use the first-page digest and early high-signal blocks.
This document understanding must influence the recommended ingestion mode and enrichment steps.

Primary goal:
Return a structured Planning Report and Execution Plan that helps the system decide:
- what type of document this is
- what the document is mainly about
- whether it is worth spending LLM cost
- how to chunk the document
- whether table enrichment is needed
- whether image/vision enrichment is needed
- whether requirement/risk/quality extraction is needed
- whether graph extraction is useful
- whether manual review is needed
- which model/profile should be used
- which steps should run or be skipped
- what risks exist
- what the expected cost/time level is

Important behavior:
- Be conservative.
- Prefer deterministic and explainable decisions.
- Do not enable expensive steps unless evidence supports them.
- Do not recommend vision enrichment for plain text documents.
- Do not recommend graph extraction unless the document contains useful relationships.
- Do not recommend requirement/risk extraction unless the document type or content supports it.
- If confidence is low, explain why and mark review candidates.
- If the rule-based assessment is strong and reasonable, preserve it.
- If you override rule-based assessment, explain why clearly.

Return ONLY valid JSON.
Do not return markdown.
Do not include comments.
Do not include extra prose outside JSON.
"""


PLANNING_LLM_USER_PROMPT = """\
/no_think

Analyze the following J1 ingestion planning context and return a Planning Report plus executable Execution Plan.

Input:

{planning_context_json}

Return a JSON object that matches the J1 PlanningResult schema (top-level keys: planning_version, recommended_profile, confidence, document_understanding, decision_summary, content_report, quality_report, execution_plan, rule_based_comparison, warnings, next_actions). Every enabled or disabled step in execution_plan.steps MUST carry a non-empty reason. Use only the page numbers present in the planning context. Return JSON only — no prose or markdown.
"""

# `/no_think` — qwen3 family hint to skip the chain-of-thought
# reasoning block. Without it, qwen3.5-9b emits up to 2K thinking
# tokens BEFORE the visible answer; hitting `max_tokens=2048` cap
# (LM Studio default) leaves the response.content empty and the
# planner falls back to rule-based even though the model "did
# something". The `/no_think` token is recognised by qwen3 chat
# templates and ignored by other models, so it's a safe hint to
# embed in the prompt unconditionally.


class PlanningLLMError(RuntimeError):
    """Raised when the LLM call itself fails — distinct from a
    schema-validation failure (which `validate_planning_result_dict`
    raises). The fail-open path treats both equivalently."""


def build_llm_planner(
    *,
    client: TextLLMClient,
    max_output_tokens: int | None = None,
    temperature: float | None = None,
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Return an `LLMPlanner` callable bound to `client`.

    The returned callable serialises the planning context to JSON,
    sends it via `client.generate`, parses the response, and returns
    a dict for the post-compile planner to validate.

    Failure modes:
      * Client raises (timeout, HTTP error, …) → re-raised as
        `PlanningLLMError`.
      * Client returns non-JSON / wrapped JSON / extra prose →
        we attempt one strict parse, then a fenced-block extraction;
        if both fail, raise `PlanningLLMError`.

    Validation of the *parsed* dict happens later in
    `validate_planning_result_dict` — this adapter only converts
    transport errors into a typed exception."""

    def _call(context: Mapping[str, Any]) -> dict[str, Any]:
        body = json.dumps(context, ensure_ascii=False, sort_keys=True)
        prompt = PLANNING_LLM_USER_PROMPT.format(
            planning_context_json=body,
        )
        try:
            text, _usage = client.generate(
                prompt,
                system_prompt=PLANNING_LLM_SYSTEM_PROMPT,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
            )
        except Exception as exc:  # noqa: BLE001 — provider raises various errors
            raise PlanningLLMError(
                f"planner LLM ({client.provider}/{client.model}) call failed: {exc}"
            ) from exc

        parsed = _parse_json_response(text)
        if parsed is None:
            raise PlanningLLMError(
                "planner LLM output was not valid JSON; "
                f"first 200 chars: {text[:200]!r}"
            )
        if not isinstance(parsed, dict):
            raise PlanningLLMError(
                f"planner LLM output was {type(parsed).__name__}, expected object"
            )
        return parsed

    return _call


def _parse_json_response(text: str) -> Any:
    """Tolerate a few common LLM formatting deviations.

    1. Strict JSON parse first — production-tuned planners typically
       respect `Return ONLY valid JSON`.
    2. Fenced code-block fallback (` ```json ... ``` `) — small set
       of widely-deployed local models still wrap output.

    Any other deviation (extra prose, multiple JSON blocks) is treated
    as failure; we deliberately don't try to "repair" malformed JSON
    because the validator can't trust the result."""
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Look for the first '{' and matching final '}' — handles
    # ` ```json {…} ``` ` and "Here is the plan: {…}" wrappers.
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = stripped[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None
