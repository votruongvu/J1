"""Civil Engineering pack loader + detection scorer.

`build_civil_engineering_pack()` reads `domain.yaml` next to this
module and returns a `DomainPack` with the detection callable
bound. Pure construction — no environment lookups.

The detector is a deterministic keyword + structural scorer:
keyword catalogue scored against the detection corpus, plus a
table-header bonus for BOQ-shaped tables. Each rule has its own
threshold; the pack returns the highest-scoring rule's result."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from j1.domains.models import (
    DomainDetectionResult,
    DomainEnrichmentPolicy,
    DomainPack,
    DomainPlanningOverlay,
    KeywordSignal,
    UnsupportedCapability,
)


__all__ = ["build_civil_engineering_pack"]


_log = logging.getLogger("j1.domains.civil_engineering")

_PACK_YAML = Path(__file__).resolve().parent / "domain.yaml"


def build_civil_engineering_pack() -> DomainPack:
    """Construct the Civil Engineering pack from the bundled YAML."""
    data = _load_pack_data(_PACK_YAML)

    keyword_signals = tuple(
        KeywordSignal(
            text=str(s["text"]).strip(),
            weight=float(s.get("weight", 0.5)),
            category=s.get("category"),
        )
        for s in data.get("keyword_signals") or []
        if s.get("text")
    )

    detection_rules = tuple(
        _DetectionRule.from_dict(r)
        for r in data.get("detection_rules") or []
    )

    overlays: dict[str, DomainPlanningOverlay] = {}
    for doc_type, raw in (data.get("overlays") or {}).items():
        if not isinstance(raw, dict):
            continue
        overlays[doc_type] = DomainPlanningOverlay(
            document_type=doc_type,
            recommended_profile=raw.get("recommended_profile"),
            chunking_strategy=raw.get("chunking_strategy"),
            step_overrides=dict(raw.get("step_overrides") or {}),
            extraction_targets=tuple(raw.get("extraction_targets") or ()),
            candidate_entity_types=tuple(raw.get("candidate_entity_types") or ()),
            applied_rule_id=f"civil_engineering.plan.{doc_type}",
            notes=raw.get("notes"),
        )

    unsupported = tuple(
        UnsupportedCapability(
            capability=str(u["capability"]),
            reason=str(u.get("reason") or ""),
        )
        for u in data.get("unsupported_capabilities") or []
        if u.get("capability")
    )

    return DomainPack(
        id=str(data["id"]),
        display_name=str(data.get("display_name") or "Civil Engineering"),
        version=str(data.get("version") or "0.1"),
        extends_document_types=tuple(data.get("document_types") or ()),
        keyword_signals=keyword_signals,
        extraction_targets=tuple(data.get("extraction_targets") or ()),
        graph_entity_types=tuple(data.get("graph_entity_types") or ()),
        graph_relationship_types=tuple(
            data.get("graph_relationship_types") or ()
        ),
        prompt_addon=str(data.get("prompt_addon") or "").strip(),
        overlays=overlays,
        unsupported_capabilities=unsupported,
        enrichment_policy=_parse_enrichment_policy(data.get("enrichment_policy")),
        detect=_make_detector(
            keyword_signals=keyword_signals,
            detection_rules=detection_rules,
            overlays=overlays,
        ),
    )


def _parse_enrichment_policy(raw: Any) -> DomainEnrichmentPolicy:
    """Build a `DomainEnrichmentPolicy` from the YAML sub-mapping.

    Missing block (None / empty dict) → policy=auto with empty lists.
    Tolerant of malformed entries: unknown keys are ignored, lists
    coerced via tuple(), and the policy string passes through to
    the dataclass which raises on invalid vocabulary at startup."""
    if not isinstance(raw, dict) or not raw:
        return DomainEnrichmentPolicy()
    return DomainEnrichmentPolicy(
        policy=str(raw.get("policy") or "auto"),
        force_recommended_tasks=tuple(
            str(t).strip()
            for t in (raw.get("force_recommended_tasks") or ())
            if str(t).strip()
        ),
        optional_tasks=tuple(
            str(t).strip()
            for t in (raw.get("optional_tasks") or ())
            if str(t).strip()
        ),
        denied_tasks=tuple(
            str(t).strip()
            for t in (raw.get("denied_tasks") or ())
            if str(t).strip()
        ),
        require_enrichment_success=bool(
            raw.get("require_enrichment_success") or False
        ),
        default_model_tier=(
            str(raw["default_model_tier"]).strip()
            if raw.get("default_model_tier") else None
        ),
        reasoning=str(raw.get("reasoning") or "").strip(),
    )


# ---- YAML loader -----------------------------------------------------


def _load_pack_data(path: Path) -> dict[str, Any]:
    """Load + sanity-check the pack YAML.

    Tolerates missing optional sections; raises when the required
    `id` / `version` fields are absent so misconfiguration surfaces
    at startup rather than at request time."""
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"pack file {path} did not parse to a mapping")
    if not data.get("id"):
        raise ValueError(f"pack file {path} missing required `id`")
    if not data.get("version"):
        raise ValueError(f"pack file {path} missing required `version`")
    return data


# ---- Detection rule + scorer -----------------------------------------


@dataclass(frozen=True)
class _DetectionRule:
    """One detection rule from the YAML.

    `min_score` gates whether the rule fires; `bonus` is added to
    the kept score so a strong-signal hit can clear the registry's
    detection threshold even when the corpus is short."""

    id: str
    document_type: str
    min_score: float
    bonus: float
    signals: tuple[str, ...]
    table_header_signals: tuple[tuple[str, ...], ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "_DetectionRule":
        signals = tuple(
            str(s).strip().lower() for s in (raw.get("signals") or [])
        )
        table_headers = tuple(
            tuple(str(h).strip().lower() for h in row)
            for row in (raw.get("table_header_signals") or [])
        )
        return cls(
            id=str(raw.get("id") or ""),
            document_type=str(raw.get("document_type") or ""),
            min_score=float(raw.get("min_score", 0.5)),
            bonus=float(raw.get("bonus", 0.0)),
            signals=signals,
            table_header_signals=table_headers,
        )


def _make_detector(
    *,
    keyword_signals: tuple[KeywordSignal, ...],
    detection_rules: tuple[_DetectionRule, ...],
    overlays: dict[str, DomainPlanningOverlay],
):
    """Build the detection callable bound to this pack's data."""

    def _detect(ctx) -> DomainDetectionResult | None:
        corpus, table_headers, image_caption_corpus = _build_corpus(ctx)

        # 1. Score the per-rule signals — bag-of-keywords with the
        # rule's `bonus` added when at least one signal hits.
        rule_results: list[tuple[_DetectionRule, float, list[str]]] = []
        for rule in detection_rules:
            score, hits = _score_rule(rule, corpus, table_headers)
            if score <= 0:
                continue
            rule_results.append((rule, score, hits))

        # 2. Score the pack-level keyword catalogue — provides a
        # baseline confidence even when no specific rule fires (e.g.
        # a generic civil narrative).
        baseline_score, baseline_hits = _score_keywords(
            keyword_signals, corpus + " " + image_caption_corpus,
        )

        # 3. Pick the winner: rule with the highest combined score
        # if any rule fired; otherwise the pack-level baseline.
        if rule_results:
            best_rule, best_score, best_hits = max(
                rule_results, key=lambda r: r[1]
            )
            applied_rule_id = f"civil_engineering.{best_rule.id}"
            evidence = _format_evidence(
                hits=best_hits,
                table_header_match=_matches_table_header(
                    best_rule, table_headers,
                ),
                rule=best_rule,
                ctx=ctx,
            )
            overlay = overlays.get(best_rule.document_type)
            return DomainDetectionResult(
                domain_id="civil_engineering",
                confidence=min(best_score, 1.0),
                evidence=evidence,
                detected_document_type=best_rule.document_type,
                applied_rule_id=applied_rule_id,
                overlay=overlay,
            )

        if baseline_score > 0:
            return DomainDetectionResult(
                domain_id="civil_engineering",
                confidence=min(baseline_score, 1.0),
                evidence=tuple(
                    f"Detected term: {h!r}" for h in baseline_hits[:8]
                ),
                detected_document_type=None,
                applied_rule_id=None,
                overlay=None,
            )

        return None

    return _detect


def _build_corpus(ctx) -> tuple[str, tuple[tuple[str, ...], ...], str]:
    """Concatenate detection corpus from the context.

    Returns `(text_corpus, table_header_rows, image_captions_corpus)`.
    Title gets repeated 3x so a strong title signal weighs more
    heavily than the same term buried in a paragraph."""
    title = (getattr(ctx, "title", "") or "")
    early_pages = getattr(ctx, "early_page_text", "") or ""
    filename = getattr(ctx, "filename", "") or ""
    headings = " ".join(
        text for _, text, _ in (getattr(ctx, "heading_outline", ()) or ())
    )
    table_caps = " ".join(getattr(ctx, "table_captions", ()) or ())
    image_caps = " ".join(getattr(ctx, "image_captions", ()) or ())
    text = " ".join([
        title, title, title,  # 3x weight on title
        headings, headings,    # 2x weight on heading outline
        early_pages, filename, table_caps,
    ]).lower()
    table_headers = tuple(
        tuple(h.strip().lower() for h in row)
        for row in (getattr(ctx, "table_header_rows", ()) or ())
    )
    return text, table_headers, image_caps.lower()


def _score_rule(
    rule: _DetectionRule,
    corpus: str,
    table_headers: tuple[tuple[str, ...], ...],
) -> tuple[float, list[str]]:
    """Score one detection rule.

    Score = sum of rule-signal matches (each capped at 1.0) +
    `rule.bonus` when at least one signal hits + table-header bonus
    when the rule's `table_header_signals` overlap a real header
    row (e.g. BOQ tables)."""
    hits: list[str] = []
    score = 0.0
    for signal in rule.signals:
        if signal and signal in corpus:
            hits.append(signal)
            score += 0.55  # per-keyword contribution; capped via min()
    if not hits:
        # Even a strong table-header match should require at least
        # one keyword somewhere — guards against false positives on
        # generic spreadsheets that happen to use the words 'item'
        # / 'description' / 'unit'.
        if not _matches_table_header(rule, table_headers):
            return 0.0, []

    if _matches_table_header(rule, table_headers):
        score += 0.4
        hits.append("table_header_match")

    if hits:
        score = min(score + rule.bonus, 1.0)
    if score < rule.min_score:
        return 0.0, hits
    return score, hits


def _matches_table_header(
    rule: _DetectionRule,
    table_headers: tuple[tuple[str, ...], ...],
) -> bool:
    """True when ANY observed header row contains every term of
    ANY required header signature (case-insensitive)."""
    if not rule.table_header_signals or not table_headers:
        return False
    for required in rule.table_header_signals:
        for actual in table_headers:
            if all(any(req in cell for cell in actual) for req in required):
                return True
    return False


def _score_keywords(
    signals: tuple[KeywordSignal, ...], corpus: str,
) -> tuple[float, list[str]]:
    """Pack-level baseline: cumulative keyword weight, capped at 1.0.

    Returns the score + the hit list (truncated to a small set so
    the evidence string stays operator-readable)."""
    hits: list[str] = []
    score = 0.0
    for sig in signals:
        if not sig.text:
            continue
        if sig.text in corpus:
            hits.append(sig.text)
            score += sig.weight
    return min(score, 1.0), hits


def _format_evidence(
    *,
    hits: list[str],
    table_header_match: bool,
    rule: _DetectionRule,
    ctx,
) -> tuple[str, ...]:
    """Convert a hit list + structural cues into operator-readable
    evidence strings — drives the FE Planning Report's "why this
    domain" panel."""
    out: list[str] = []
    keyword_hits = [h for h in hits if h != "table_header_match"][:6]
    if keyword_hits:
        out.append(
            f"Matched signals: {', '.join(repr(h) for h in keyword_hits)}."
        )
    if table_header_match:
        out.append("Table headers match BOQ-shaped row structure.")
    title = getattr(ctx, "title", "") or ""
    if title and any(h in title.lower() for h in keyword_hits):
        out.append(f"Title '{title[:120]}' carries the strongest signal.")
    if rule.document_type:
        out.append(
            f"Rule {rule.id} → document_type={rule.document_type}."
        )
    return tuple(out)
