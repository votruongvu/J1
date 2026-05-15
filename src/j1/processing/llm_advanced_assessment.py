"""Optional LLM-based Advanced Assessment.

Triggered EXPLICITLY via ``POST /documents/{id}/advanced-assessment``.
Never runs as part of the default ingest path. Even when the
operator clicks "Run Advanced Assessment", the service refuses
inputs that exceed any of the deployment guardrails (file size,
page count, sampled-text char cap, timeout) and returns a
structured refusal so the FE asks the user to pick manually.

The output is strict JSON. The LLM is NOT asked to answer document
questions — it estimates document complexity and recommends a
profile + downstream manual actions only. Anything outside the
schema is dropped silently.

This module is pure orchestration: text sampling + prompt
construction + JSON parsing + result normalisation. It does NOT
read files from disk on its own — the REST handler supplies the
already-parsed sampled text. That keeps the service unit-testable
without spinning up a real LLM or a real PDF parser.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from j1.processing.llm_advanced_assessment_settings import (
    LLMAdvancedAssessmentSettings,
)


__all__ = [
    "LLMAdvancedAssessmentInputs",
    "LLMAdvancedAssessmentResult",
    "LLMAdvancedAssessmentService",
    "REFUSAL_DOCUMENT_TOO_LARGE",
    "REFUSAL_LLM_DISABLED",
    "REFUSAL_LLM_UNAVAILABLE",
    "REFUSAL_LLM_ERROR",
    "REFUSAL_MALFORMED_RESPONSE",
    "REFUSAL_NOT_RUN",
    "MANUAL_SELECTION_HINT",
    "STATUS_OK",
    "STATUS_REFUSED",
]


_log = logging.getLogger("j1.llm_advanced_assessment")


# Wire vocabulary -----------------------------------------------------

STATUS_OK = "ok"
STATUS_REFUSED = "refused"

REFUSAL_LLM_DISABLED = "llm_disabled"
REFUSAL_LLM_UNAVAILABLE = "llm_unavailable"
REFUSAL_DOCUMENT_TOO_LARGE = "document_too_large"
REFUSAL_NOT_RUN = "not_run"  # operator never clicked "Run Advanced".
REFUSAL_LLM_ERROR = "llm_error"
REFUSAL_MALFORMED_RESPONSE = "malformed_response"

MANUAL_SELECTION_HINT = (
    "This document is too large for Advanced Assessment. Please "
    "choose a profile manually based on visible document complexity."
)


# Allowed schema vocabulary. Anything outside these sets is dropped
# during normalisation so a hallucinating LLM can't smuggle in extra
# verdicts.

_ALLOWED_COMPLEXITY = ("simple", "moderate", "complex", "very_complex")
_ALLOWED_CONFIDENCE = ("low", "medium", "high")
_ALLOWED_SIGNAL = ("no", "suspected", "likely")
_ALLOWED_LAYOUT = ("low", "medium", "high")
_ALLOWED_PROFILE = (
    "quick_index", "standard_index", "deep_knowledge_index",
)
_ALLOWED_NEXT_STEPS = (
    "run_domain_enrichment",
    "build_knowledge_memory",
    "normalize_entities",
    "build_deep_knowledge_index",
    "run_multimodal_enrichment",
)


# Default standard-warning surfaced on every OK response: keeps the
# "this is an estimate" framing in front of the operator at all
# times. The LLM can add more warnings; they're appended.
DEFAULT_OK_WARNING = (
    "This is an LLM-based estimate from sampled content, not a "
    "full parse."
)


@dataclass(frozen=True)
class LLMAdvancedAssessmentInputs:
    """Everything the service needs to build a prompt + decide.

    Decoupled from the REST handler so tests don't have to mock a
    document registry. The handler builds this dataclass from the
    document profile / sampled text / decision context."""

    document_id: str
    filename: str | None = None
    title: str | None = None
    file_size_bytes: int | None = None
    page_count: int | None = None
    sampled_text: str | None = None
    lightweight_assessment_payload: dict | None = None
    matched_rules: tuple[dict, ...] = ()
    domain_id: str | None = None


@dataclass(frozen=True)
class LLMAdvancedAssessmentResult:
    """Service output. ``status`` distinguishes OK vs refusal:
    consumers test ``status == STATUS_OK`` before reading the rest.

    The refusal branch carries ``refusal_reason`` (one of the
    ``REFUSAL_*`` wire strings above) plus an operator-readable
    ``message``. The FE renders the message verbatim — keep it
    short + action-oriented."""

    status: str
    refusal_reason: str | None = None
    message: str | None = None
    document_complexity: str | None = None
    recommended_profile: str | None = None
    confidence: str | None = None
    detected_signals: dict[str, str] = field(default_factory=dict)
    recommended_next_steps: tuple[str, ...] = ()
    reasoning_summary: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "refusalReason": self.refusal_reason,
            "message": self.message,
            "documentComplexity": self.document_complexity,
            "recommendedProfile": self.recommended_profile,
            "confidence": self.confidence,
            "detectedSignals": dict(self.detected_signals),
            "recommendedNextSteps": list(self.recommended_next_steps),
            "reasoningSummary": list(self.reasoning_summary),
            "warnings": list(self.warnings),
        }


# ---- Service --------------------------------------------------------


# Type alias for the text-LLM call. ``(prompt, system_prompt) -> text``
# returning the raw response string. Matches the existing
# ``OpenAICompatTextLLMClient.generate`` shape minus the usage tuple.
LLMCall = Callable[[str, str], str]


class LLMAdvancedAssessmentService:
    """Drives one round of Advanced Assessment.

    Pure orchestration. The REST handler builds the inputs, this
    service applies guardrails, builds the prompt, calls the LLM,
    and normalises the JSON response.

    Construction:

      * ``settings`` — operator-tunable guardrails.
      * ``llm_call`` — a callable that takes ``(prompt, system_prompt)``
        and returns the raw text response. Pass ``None`` (the default)
        in test wirings without an LLM; the service then refuses with
        ``REFUSAL_LLM_UNAVAILABLE``.
    """

    def __init__(
        self,
        *,
        settings: LLMAdvancedAssessmentSettings,
        llm_call: LLMCall | None = None,
    ) -> None:
        self._settings = settings
        self._llm_call = llm_call

    # ---- Public surface --------------------------------------------

    def run(
        self, inputs: LLMAdvancedAssessmentInputs,
    ) -> LLMAdvancedAssessmentResult:
        """Run one assessment. Synchronous (the LLM call may block
        for up to ``settings.timeout_seconds``; the underlying
        client should honour that)."""
        # 1. Guardrails BEFORE we touch the LLM.
        refusal = self._guardrails_refusal(inputs)
        if refusal is not None:
            return refusal
        # 2. Build the prompt + call the LLM.
        prompt = self._build_prompt(inputs)
        try:
            raw = self._llm_call(prompt, _SYSTEM_PROMPT)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 — folded into refusal
            _log.warning(
                "LLM Advanced Assessment failed: %s: %s",
                type(exc).__name__, exc,
            )
            return _refusal(
                REFUSAL_LLM_ERROR,
                f"LLM call failed: {type(exc).__name__}.",
            )
        if not isinstance(raw, str) or not raw.strip():
            return _refusal(
                REFUSAL_MALFORMED_RESPONSE,
                "LLM returned an empty response.",
            )
        # 3. Parse + normalise.
        try:
            return self._parse_response(raw)
        except _ParseError as exc:
            return _refusal(
                REFUSAL_MALFORMED_RESPONSE,
                f"LLM response did not match the schema: {exc}.",
            )

    # ---- Guardrails -----------------------------------------------

    def _guardrails_refusal(
        self, inputs: LLMAdvancedAssessmentInputs,
    ) -> LLMAdvancedAssessmentResult | None:
        s = self._settings
        if not s.enabled:
            return _refusal(
                REFUSAL_LLM_DISABLED,
                "Advanced Assessment is disabled in this deployment.",
            )
        if self._llm_call is None:
            return _refusal(
                REFUSAL_LLM_UNAVAILABLE,
                "No LLM is configured in this deployment.",
            )
        if (
            inputs.file_size_bytes is not None
            and inputs.file_size_bytes > s.max_file_size_bytes
        ):
            return _refusal(
                REFUSAL_DOCUMENT_TOO_LARGE, MANUAL_SELECTION_HINT,
            )
        if (
            inputs.page_count is not None
            and inputs.page_count > s.max_page_count
        ):
            return _refusal(
                REFUSAL_DOCUMENT_TOO_LARGE, MANUAL_SELECTION_HINT,
            )
        if (
            inputs.sampled_text
            and len(inputs.sampled_text) > s.max_text_chars
        ):
            return _refusal(
                REFUSAL_DOCUMENT_TOO_LARGE, MANUAL_SELECTION_HINT,
            )
        return None

    # ---- Prompt construction --------------------------------------

    def _build_prompt(
        self, inputs: LLMAdvancedAssessmentInputs,
    ) -> str:
        s = self._settings
        text_snippet = (inputs.sampled_text or "")[: s.max_text_chars]
        lightweight = json.dumps(
            inputs.lightweight_assessment_payload or {},
            ensure_ascii=False, indent=2,
        )[:4000]
        rules = json.dumps(
            list(inputs.matched_rules)[:10],
            ensure_ascii=False, indent=2,
        )[:2000]
        return (
            f"Document id: {inputs.document_id!r}\n"
            f"Filename: {inputs.filename or '(unknown)'!r}\n"
            f"Title: {inputs.title or '(unknown)'!r}\n"
            f"Active domain: {inputs.domain_id or 'general'}\n"
            f"Page count: {inputs.page_count if inputs.page_count is not None else 'unknown'}\n"
            f"File size bytes: {inputs.file_size_bytes if inputs.file_size_bytes is not None else 'unknown'}\n"
            "\nLightweight assessment signals (deterministic, "
            "pypdf-only):\n"
            f"{lightweight}\n"
            "\nDomain / general rules already matched (advisory):\n"
            f"{rules}\n"
            "\nSampled text from first / middle / end pages (may be empty):\n"
            "----\n"
            f"{text_snippet}\n"
            "----\n"
            "\nReturn a SINGLE JSON object exactly matching the "
            "schema described in the system prompt. Do NOT include "
            "any other text. Do NOT answer questions about the "
            "document. Estimate complexity + recommend a profile + "
            "manual next steps only."
        )

    # ---- Response parsing -----------------------------------------

    def _parse_response(
        self, raw: str,
    ) -> LLMAdvancedAssessmentResult:
        payload = _extract_json_object(raw)
        if not isinstance(payload, dict):
            raise _ParseError("response was not a JSON object")
        complexity = _normalise_choice(
            payload.get("document_complexity"),
            _ALLOWED_COMPLEXITY, default="moderate",
        )
        recommended = _normalise_choice(
            payload.get("recommended_profile"),
            _ALLOWED_PROFILE, default="standard_index",
        )
        confidence = _normalise_choice(
            payload.get("confidence"),
            _ALLOWED_CONFIDENCE, default="low",
        )
        signals_raw = (
            payload.get("detected_signals")
            if isinstance(payload.get("detected_signals"), Mapping)
            else {}
        )
        detected_signals: dict[str, str] = {}
        for key in (
            "likely_tables", "likely_images_or_diagrams",
            "likely_equations", "likely_requirements",
        ):
            detected_signals[key] = _normalise_choice(
                signals_raw.get(key),
                _ALLOWED_SIGNAL, default="no",
            )
        detected_signals["layout_complexity"] = _normalise_choice(
            signals_raw.get("layout_complexity"),
            _ALLOWED_LAYOUT, default="low",
        )
        next_steps_raw = payload.get("recommended_next_steps") or []
        next_steps: list[str] = []
        if isinstance(next_steps_raw, list):
            for step in next_steps_raw:
                if not isinstance(step, str):
                    continue
                v = step.strip().lower()
                if v in _ALLOWED_NEXT_STEPS and v not in next_steps:
                    next_steps.append(v)
        reasoning_raw = payload.get("reasoning_summary") or []
        reasoning: list[str] = []
        if isinstance(reasoning_raw, list):
            for line in reasoning_raw:
                if isinstance(line, str) and line.strip():
                    reasoning.append(line.strip())
        # Cap reasoning so a verbose LLM can't blow up the audit log.
        reasoning = reasoning[:8]
        warnings_raw = payload.get("warnings") or []
        warnings: list[str] = [DEFAULT_OK_WARNING]
        if isinstance(warnings_raw, list):
            for w in warnings_raw:
                if isinstance(w, str) and w.strip() and w not in warnings:
                    warnings.append(w.strip())
        return LLMAdvancedAssessmentResult(
            status=STATUS_OK,
            document_complexity=complexity,
            recommended_profile=recommended,
            confidence=confidence,
            detected_signals=detected_signals,
            recommended_next_steps=tuple(next_steps),
            reasoning_summary=tuple(reasoning),
            warnings=tuple(warnings),
        )


# ---- Helpers --------------------------------------------------------


_SYSTEM_PROMPT = (
    "You estimate document complexity and recommend a downstream "
    "processing profile. You DO NOT answer questions about the "
    "document. Respond with a SINGLE JSON object, no prose. Schema:\n"
    "{\n"
    '  "document_complexity": "simple | moderate | complex | very_complex",\n'
    '  "recommended_profile": "quick_index | standard_index | deep_knowledge_index",\n'
    '  "confidence": "low | medium | high",\n'
    '  "detected_signals": {\n'
    '    "likely_tables": "no | suspected | likely",\n'
    '    "likely_images_or_diagrams": "no | suspected | likely",\n'
    '    "likely_equations": "no | suspected | likely",\n'
    '    "likely_requirements": "no | suspected | likely",\n'
    '    "layout_complexity": "low | medium | high"\n'
    "  },\n"
    '  "recommended_next_steps": ['
    '"run_domain_enrichment" | "build_knowledge_memory" | '
    '"normalize_entities" | "build_deep_knowledge_index" | '
    '"run_multimodal_enrichment"'
    "],\n"
    '  "reasoning_summary": ["short reason 1", "short reason 2"],\n'
    '  "warnings": ["string"]\n'
    "}\n"
    "Use hedged language: suspected / likely / no. Never claim "
    "exact table or equation counts. The sampled text may not "
    "represent the full document."
)


class _ParseError(ValueError):
    pass


def _refusal(reason: str, message: str) -> LLMAdvancedAssessmentResult:
    return LLMAdvancedAssessmentResult(
        status=STATUS_REFUSED,
        refusal_reason=reason,
        message=message,
        warnings=(message,),
    )


def _extract_json_object(raw: str) -> Any:
    r"""Tolerant JSON extraction. LLMs sometimes wrap JSON in
    \`\`\`json ... \`\`\` fences or prefix it with a short
    preamble. Find the first ``{...}`` block and parse that."""
    stripped = raw.strip()
    # Strip code fences.
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Last-ditch: greedy match.
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if match is None:
        raise _ParseError("no JSON object in response")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise _ParseError(f"invalid JSON: {exc}") from exc


def _normalise_choice(
    value: Any, allowed: tuple[str, ...], *, default: str,
) -> str:
    if not isinstance(value, str):
        return default
    v = value.strip().lower()
    return v if v in allowed else default
