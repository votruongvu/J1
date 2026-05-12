"""Optional LLM-as-judge for semantic validation checks.

The judge produces ONLY optional-severity check inputs. Required
failures must stay deterministic so re-runs are reproducible (an
LLM that flips its mind between runs would create flapping
validation outcomes — the worst kind of test infrastructure).

Three judgements ship :

 1. `judge_answer_covers_points` — given the question, the answer,
 and a list of expected answer points, does the answer cover
 each point semantically?
 2. `judge_answer_grounded` — given the answer + the cited chunks,
 does the answer make claims that aren't supported by the
 citations?
 3. `judge_negative_abstain` — for a negative (out-of-scope) test
 case, does the answer fabricate facts that aren't in the
 citations? Distinct from regex-based abstain detection (which
 handles the deterministic part) — the judge only fires the
 "no fabrication" check.

All three calls go through `TextLLMClient.extract(prompt, schema)` so
the response is constrained to a known JSON shape. Failures (LLM
unavailable, malformed JSON, transport error) collapse to "judge
not consulted" — the optional check is then OMITTED rather than
included-and-passing, matching the same convention as 's
conditional `citation_present` check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

_log = logging.getLogger("j1.validation.judge")


# Truncation cap for chunk content sent to the judge. Small local
# LLMs choke on long contexts; we trade some context for reliability.
_MAX_CITATION_CHARS = 2000

# Coverage threshold: ≥80% of points must be marked covered for the
# `answer_covers_expected_points` check to pass. Below that we fail
# the optional check (→ `passed_with_warnings`). Tunable per
# deployment when the time comes — for it's a constant.
_COVERAGE_THRESHOLD = 0.8


# ---- Result dataclasses -------------------------------------------


@dataclass(frozen=True)
class CoverageJudgement:
    """Output of `judge_answer_covers_points`.

 `points` parallels the input `expected_answer_points` — same
 length, same order. Each entry's `covered` is the judge's call
 on whether the answer addresses that point. `coverage_ratio` is
 the count of covered points divided by total; the runner
 compares it against `_COVERAGE_THRESHOLD` to decide pass/fail.
 """

    @dataclass(frozen=True)
    class Point:
        text: str
        covered: bool
        rationale: str | None = None

    points: list[Point] = field(default_factory=list)
    rationale: str | None = None

    @property
    def coverage_ratio(self) -> float:
        if not self.points:
            return 1.0
        return sum(1 for p in self.points if p.covered) / len(self.points)


@dataclass(frozen=True)
class GroundingJudgement:
    """Output of `judge_answer_grounded`.

 `unsupported_claims` is the judge's list of statements in the
 answer that aren't backed by the citations. `severity` of each
 is one of `low`/`moderate`/`high` — the runner treats `moderate`
 or higher as a failure of the optional grounding check.
 """

    @dataclass(frozen=True)
    class Claim:
        text: str
        severity: str  # "low" | "moderate" | "high"
        rationale: str | None = None

    unsupported_claims: list[Claim] = field(default_factory=list)
    rationale: str | None = None

    def has_significant_issues(self) -> bool:
        """Any moderate-or-higher unsupported claim is a fail."""
        return any(
            c.severity.lower() in ("moderate", "high")
            for c in self.unsupported_claims
        )


@dataclass(frozen=True)
class FabricationJudgement:
    """Output of `judge_negative_abstain` — same shape as the
 grounding judgement; we use a separate class so the runner
 can branch on intent without inspecting check names. `fabricated`
 means the answer asserts facts beyond what the (possibly empty)
 citations support."""

    @dataclass(frozen=True)
    class Claim:
        text: str
        severity: str
        rationale: str | None = None

    fabricated_claims: list[Claim] = field(default_factory=list)
    rationale: str | None = None

    def has_fabrication(self) -> bool:
        return any(
            c.severity.lower() in ("moderate", "high")
            for c in self.fabricated_claims
        )


# ---- Protocol + default implementation ----------------------------


class LLMJudge(Protocol):
    """Type-only protocol so callers can inject a stub in tests
 without subclassing. `DefaultLLMJudge` is the production
 implementation."""

    def judge_answer_covers_points(
        self,
        *,
        question: str,
        answer: str,
        expected_points: list[str],
    ) -> CoverageJudgement | None: ...

    def judge_answer_grounded(
        self,
        *,
        question: str,
        answer: str,
        citations: list[dict[str, Any]],
    ) -> GroundingJudgement | None: ...

    def judge_negative_abstain(
        self,
        *,
        question: str,
        answer: str,
        citations: list[dict[str, Any]],
    ) -> FabricationJudgement | None: ...


_COVERAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "points": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "covered": {"type": "boolean"},
                    "rationale": {"type": "string"},
                },
                "required": ["text", "covered"],
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["points"],
}


_GROUNDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "unsupported_claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["low", "moderate", "high"],
                    },
                    "rationale": {"type": "string"},
                },
                "required": ["text", "severity"],
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["unsupported_claims"],
}


_FABRICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fabricated_claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["low", "moderate", "high"],
                    },
                    "rationale": {"type": "string"},
                },
                "required": ["text", "severity"],
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["fabricated_claims"],
}


_COVERAGE_PROMPT = (
    "You are judging whether a knowledge-base answer addresses a "
    "list of expected answer points. For each point, mark "
    "`covered=true` only when the answer explicitly addresses it "
    "(the answer doesn't have to use the same words — semantic "
    "coverage is enough). Return one entry per expected point in "
    "the original order.\n"
)

_GROUNDING_PROMPT = (
    "You are auditing a knowledge-base answer for unsupported "
    "claims.\n"
    "\n"
    "CRITICAL: Extract claims ONLY from the **Answer** section "
    "below. The **Question** section is NOT part of the answer — "
    "it is the prompt the system was asked, not a factual "
    "statement to verify. Do NOT list the question text "
    "(verbatim or paraphrased) as an unsupported claim. Do NOT "
    "list any heading or preamble text from the answer "
    "(e.g. 'Knowledge results:', 'Graph relationships:') as a "
    "claim — those are formatting, not facts.\n"
    "\n"
    "For each genuine factual claim in the Answer, mark severity:\n"
    "  - `low`: trivial filler / common knowledge background;\n"
    "  - `moderate`: a specific claim that should have a citation;\n"
    "  - `high`: a clearly fabricated fact contradicting or "
    "absent from the citations.\n"
    "\n"
    "Whitespace and line breaks are normalized — treat runs of "
    "spaces, tabs, and newlines as equivalent to a single space "
    "when matching answer text against citation text. Do not flag "
    "a claim as unsupported solely because of formatting "
    "differences (extra spaces, hyphenation across line breaks, "
    "missing/added punctuation).\n"
    "\n"
    "When a citation has no body excerpt (only lineage like "
    "`[N] artifact_id @ location`), use it as evidence-of-existence "
    "rather than evidence-of-content — claims whose only support "
    "would be a lineage-only citation get severity `low`, not "
    "`moderate`. Reserve `moderate`/`high` for claims that "
    "contradict the citation bodies you DO have.\n"
    "\n"
    "Return an empty list when every claim is grounded.\n"
)

# Known fallback phrases the synthesizer is instructed to emit when
# the retrieved evidence is insufficient to answer the question.
# These are NOT factual claims about the document — they are explicit
# admissions of insufficient evidence. The grounding judge previously
# treated them as a single unsupported claim ("the answer says X
# isn't in the evidence") and surfaced a moderate-severity warning,
# which polluted the validation report with false positives.
#
# Comparison is case-insensitive on whitespace-normalised text so
# the LLM's actual output ("Not in the retrieved evidence.\n",
# "not in the retrieved evidence", etc.) all match. Substring match
# is intentional — the LLM occasionally adds a hedge sentence after
# the fallback (e.g. "Not in the retrieved evidence. The chunks
# describe a different section."), and we still want to classify
# the answer as a fallback, not pretend the hedge is a factual claim.
_FALLBACK_PHRASES: tuple[str, ...] = (
    "not in the retrieved evidence",
    "not enough information",
    "no relevant information",
    "the evidence does not",
    "the provided evidence does not",
    "the retrieved evidence does not",
    "the context does not contain",
    "i don't have enough",
    "i do not have enough",
    "insufficient information",
    "no information is provided",
    "no information was provided",
)


def _is_fallback_answer(answer: str) -> bool:
    """Return True when ``answer`` is one of the synthesizer's
    canonical "I don't know" phrases. Whitespace-normalised,
    case-insensitive, substring match. See ``_FALLBACK_PHRASES`` for
    the catalogue."""
    if not answer:
        return False
    normalised = _normalise_text(answer).lower()
    if not normalised:
        return False
    return any(phrase in normalised for phrase in _FALLBACK_PHRASES)


_FABRICATION_PROMPT = (
    "You are auditing the answer to an out-of-scope question. The "
    "answer should either abstain or only state facts that are in "
    "the citations. Identify any claim in the answer that goes "
    "beyond the citations (or fabricates content when no citations "
    "exist). Severities:\n"
    "  - `low`: speculative hedging that's still safe;\n"
    "  - `moderate`: a concrete fabricated fact;\n"
    "  - `high`: a confidently asserted falsehood.\n"
    "Return an empty list when the answer is honest about its "
    "limits.\n"
)


class DefaultLLMJudge:
    """Production judge backed by a `TextLLMClient`-shaped object.

 Constructor takes the client directly so deployments can swap
 a slower-but-better text model for the FAST role; the judge
 doesn't care which role it's pointed at as long as the
 `extract(prompt, schema)` surface works.

 Every public method returns `None` on any failure path — no
 exceptions propagate to the runner. The runner translates a
 `None` return into "the optional check wasn't run" (omitted
 from `checks[]` rather than included-and-passing).
 """

    def __init__(self, *, text_client: Any) -> None:
        self._text_client = text_client

    def judge_answer_covers_points(
        self,
        *,
        question: str,
        answer: str,
        expected_points: list[str],
    ) -> CoverageJudgement | None:
        if not expected_points or not (answer or "").strip():
            return None
        prompt = (
            f"{_COVERAGE_PROMPT}\n"
            f"Question: {question}\n\n"
            f"Answer:\n---\n{answer}\n---\n\n"
            "Expected points:\n"
            + "\n".join(f"- {p}" for p in expected_points)
        )
        parsed = self._extract(prompt, _COVERAGE_SCHEMA)
        if parsed is None:
            return None
        raw_points = parsed.get("points")
        if not isinstance(raw_points, list):
            return None
        # Defensive normalization: align the judge's response with
        # the input list. If the judge dropped/added items we still
        # surface what came back, but the runner's coverage_ratio
        # computation stays sane (uses the response length).
        points: list[CoverageJudgement.Point] = []
        for entry in raw_points:
            if not isinstance(entry, dict):
                continue
            points.append(
                CoverageJudgement.Point(
                    text=str(entry.get("text") or ""),
                    covered=bool(entry.get("covered") or False),
                    rationale=_str_or_none(entry.get("rationale")),
                )
            )
        return CoverageJudgement(
            points=points,
            rationale=_str_or_none(parsed.get("rationale")),
        )

    def judge_answer_grounded(
        self,
        *,
        question: str,
        answer: str,
        citations: list[dict[str, Any]],
    ) -> GroundingJudgement | None:
        if not (answer or "").strip():
            return None
        # Fallback-phrase whitelist: when the synthesizer correctly
        # emits a "I don't know" answer (e.g. "Not in the retrieved
        # evidence."), there are NO factual claims to ground. The
        # judge previously asked the LLM to extract claims from
        # this fallback text and the LLM treated the admission of
        # insufficient evidence itself as an unsupported claim,
        # producing false-positive moderate-severity warnings on
        # otherwise-honest answers. Short-circuit with an empty
        # grounding judgement instead of round-tripping to the LLM.
        if _is_fallback_answer(answer):
            return GroundingJudgement(
                unsupported_claims=[],
                rationale=(
                    "answer is a fallback phrase (insufficient-evidence "
                    "admission) — no factual claims to ground"
                ),
            )
        # Normalise the answer + citation previews before they hit
        # the LLM. PDF compilers produce chunks with collapsed/
        # duplicated whitespace and inserted newlines (e.g.
        # "due\n  20  May 2026"); without normalisation the judge
        # flags answers like "due 20 May 2026" as unsupported on
        # purely cosmetic grounds. The normaliser preserves token
        # content, just collapses whitespace runs to a single space.
        normalised_answer = _normalise_text(answer)
        rendered_citations = _render_citations(
            citations, normalise_preview=True,
        )
        prompt = (
            f"{_GROUNDING_PROMPT}\n"
            f"Question: {question}\n\n"
            f"Answer:\n---\n{normalised_answer}\n---\n\n"
            f"Citations:\n---\n{rendered_citations}\n---"
        )
        parsed = self._extract(prompt, _GROUNDING_SCHEMA)
        if parsed is None:
            return None
        raw_claims = parsed.get("unsupported_claims")
        if not isinstance(raw_claims, list):
            return None
        claims: list[GroundingJudgement.Claim] = []
        for entry in raw_claims:
            if not isinstance(entry, dict):
                continue
            claims.append(
                GroundingJudgement.Claim(
                    text=str(entry.get("text") or ""),
                    severity=str(entry.get("severity") or "low").lower(),
                    rationale=_str_or_none(entry.get("rationale")),
                )
            )
        return GroundingJudgement(
            unsupported_claims=claims,
            rationale=_str_or_none(parsed.get("rationale")),
        )

    def judge_negative_abstain(
        self,
        *,
        question: str,
        answer: str,
        citations: list[dict[str, Any]],
    ) -> FabricationJudgement | None:
        if not (answer or "").strip():
            return None
        # Same short-circuit as ``judge_answer_grounded``: a
        # fallback phrase IS the correct behaviour for an
        # out-of-scope question. No fabrication to flag.
        if _is_fallback_answer(answer):
            return FabricationJudgement(
                fabricated_claims=[],
                rationale=(
                    "answer is a fallback phrase (the model abstained "
                    "as instructed) — no fabricated claims"
                ),
            )
        normalised_answer = _normalise_text(answer)
        rendered_citations = _render_citations(
            citations, normalise_preview=True,
        )
        prompt = (
            f"{_FABRICATION_PROMPT}\n"
            f"Question: {question}\n\n"
            f"Answer:\n---\n{normalised_answer}\n---\n\n"
            f"Citations:\n---\n{rendered_citations}\n---"
        )
        parsed = self._extract(prompt, _FABRICATION_SCHEMA)
        if parsed is None:
            return None
        raw_claims = parsed.get("fabricated_claims")
        if not isinstance(raw_claims, list):
            return None
        claims: list[FabricationJudgement.Claim] = []
        for entry in raw_claims:
            if not isinstance(entry, dict):
                continue
            claims.append(
                FabricationJudgement.Claim(
                    text=str(entry.get("text") or ""),
                    severity=str(entry.get("severity") or "low").lower(),
                    rationale=_str_or_none(entry.get("rationale")),
                )
            )
        return FabricationJudgement(
            fabricated_claims=claims,
            rationale=_str_or_none(parsed.get("rationale")),
        )

    # ---- Internals -------------------------------------------------

    def _extract(
        self, prompt: str, schema: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Wrapped `text_client.extract` that returns `None` on any
 failure. The runner branches on `None` to omit the optional
 check entirely — never to mark it failed. This keeps
 judge-availability separate from judge-verdict in the
 result accounting."""
        if self._text_client is None:
            return None
        try:
            parsed, _usage = self._text_client.extract(prompt, schema)
        except Exception as exc:  # noqa: BLE001 — degrade gracefully
            _log.debug("LLM judge call failed: %s", exc)
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed


# ---- Module helpers -----------------------------------------------


def coverage_threshold() -> float:
    """Public accessor for the runner so the threshold lives in one
 place. Tunable per profile in the future; constant for now."""
    return _COVERAGE_THRESHOLD


def _render_citations(
    citations: list[dict[str, Any]],
    *,
    normalise_preview: bool = False,
) -> str:
    """Compact, judge-friendly rendering of the citation list.

 Each citation gets one line: `[i] artifact_id @ source_location:
 body-truncated`. Body is sourced from the citation's `preview`
 field when present; otherwise the line is just the lineage.
 Truncation cap defends against pathological inputs that would
 blow the LLM's context window.

 `normalise_preview=True` collapses whitespace runs in the
 preview before rendering so the judge's grounding / fabrication
 checks compare apples-to-apples with the normalised answer text.
 """
    if not citations:
        return "(no citations)"
    lines: list[str] = []
    for i, c in enumerate(citations):
        artifact = c.get("artifact_id") or c.get("artifactId") or "?"
        location = c.get("source_location") or c.get("sourceLocation") or ""
        preview = c.get("preview") or ""
        body = str(preview)[:_MAX_CITATION_CHARS]
        if normalise_preview and body:
            body = _normalise_text(body)
        suffix = f" — {body}" if body else ""
        lines.append(f"[{i + 1}] {artifact} @ {location}{suffix}")
    return "\n".join(lines)


# Whitespace runs (including newlines) collapse to a single space.
# Same normalisation the generator's anti-hallucination quote check
# uses (`generator._normalise_for_match`); the judge now applies it
# too so groundedness checks don't false-positive on PDF-extracted
# text that contains "due\n  20  May 2026" vs "due 20 May 2026".
import re as _re  # noqa: E402 — local re-import so the helper is self-contained
_WS_RE = _re.compile(r"\s+")


def _normalise_text(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
