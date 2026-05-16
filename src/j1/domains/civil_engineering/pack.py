"""Civil Engineering pack loader + detection scorer.

`build_civil_engineering_pack` reads `domain.yaml` next to this
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
    DomainAssessmentCapabilityHint,
    DomainAssessmentCapabilityHints,
    DomainCompilePromptContext,
    DomainDetectionResult,
    DomainEnrichmentPolicy,
    DomainExtractionHints,
    EntityAlias,
    DomainPack,
    DomainPlanningOverlay,
    DomainPromptPack,
    DomainValidationRules,
    KeywordSignal,
    UnsupportedCapability,
)
from j1.domains.profile_rules import parse_document_profile_rules


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
        extraction_hints=_parse_extraction_hints(data.get("extraction_hints")),
        validation_rules=_parse_validation_rules(data.get("validation_rules")),
        prompt_pack=_parse_prompt_pack(data.get("prompt_pack")),
        detect=_make_detector(
            keyword_signals=keyword_signals,
            detection_rules=detection_rules,
            overlays=overlays,
        ),
        document_profile_rules=parse_document_profile_rules(
            data.get("document_profile_rules"),
        ),
        assessment_capability_hints=_parse_assessment_capability_hints(
            data.get("assessment_capability_hints"),
        ),
        compile_prompt_context=_parse_compile_prompt_context(
            data.get("compile_prompt_context"),
        ),
        compile_prompt_focus=_parse_compile_prompt_focus(
            data.get("compile_prompt_focus"),
        ),
    )


# ---- New section parsers ----------------------------------------


_VALID_CONFIDENCE_LEVELS: frozenset[str] = frozenset({"low", "medium", "high"})


def _parse_assessment_capability_hints(
    raw: Any,
) -> dict[str, DomainAssessmentCapabilityHints]:
    """Parse the `assessment_capability_hints` block.

    Shape: ``{document_type: {process_images: {recommended, confidence,
    reason}, process_tables: {...}, process_equations: {...}}}``.
    Tolerant of malformed entries — bad rows are skipped, not fatal,
    so a typo in one document type doesn't take down the pack load."""
    if not isinstance(raw, dict) or not raw:
        return {}
    out: dict[str, DomainAssessmentCapabilityHints] = {}
    for doc_type, capabilities in raw.items():
        if not isinstance(doc_type, str) or not doc_type.strip():
            continue
        if not isinstance(capabilities, dict):
            continue
        out[doc_type.strip()] = DomainAssessmentCapabilityHints(
            process_images=_parse_capability_hint(
                capabilities.get("process_images"),
            ),
            process_tables=_parse_capability_hint(
                capabilities.get("process_tables"),
            ),
            process_equations=_parse_capability_hint(
                capabilities.get("process_equations"),
            ),
        )
    return out


def _parse_capability_hint(raw: Any) -> DomainAssessmentCapabilityHint:
    """Build one `DomainAssessmentCapabilityHint`. Missing or malformed
    rows produce the zero value (low / not recommended / no reason)."""
    if not isinstance(raw, dict):
        return DomainAssessmentCapabilityHint()
    confidence = str(raw.get("confidence") or "low").strip().lower()
    if confidence not in _VALID_CONFIDENCE_LEVELS:
        # Defensive: typo'd confidence falls back to "low" rather than
        # crashing the pack load.
        confidence = "low"
    reason = str(raw.get("reason") or "").strip()
    return DomainAssessmentCapabilityHint(
        recommended=bool(raw.get("recommended", False)),
        confidence=confidence,
        reason=reason,
    )


def _parse_compile_prompt_context(
    raw: Any,
) -> DomainCompilePromptContext | None:
    """Parse the optional `compile_prompt_context` block. Returns
    ``None`` when missing so the resolver short-circuits."""
    if not isinstance(raw, dict):
        return None
    apply_to_raw = raw.get("apply_to") or ()
    if not isinstance(apply_to_raw, (list, tuple)):
        apply_to_raw = ()
    apply_to = tuple(
        str(s).strip() for s in apply_to_raw if str(s).strip()
    )
    max_budget = raw.get("max_tokens_budget_hint")
    try:
        max_budget_int = int(max_budget) if max_budget is not None else 0
    except (TypeError, ValueError):
        max_budget_int = 0
    return DomainCompilePromptContext(
        enabled=bool(raw.get("enabled", False)),
        system_addon=str(raw.get("system_addon") or ""),
        max_tokens_budget_hint=max_budget_int,
        apply_to=apply_to,
    )


def _parse_compile_prompt_focus(raw: Any) -> dict[str, tuple[str, ...]]:
    """Parse the optional per-document-type focus block. Each value
    is a list of short strings."""
    if not isinstance(raw, dict) or not raw:
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for doc_type, lines in raw.items():
        if not isinstance(doc_type, str) or not doc_type.strip():
            continue
        if not isinstance(lines, (list, tuple)):
            continue
        clean = tuple(
            str(line).strip() for line in lines if str(line).strip()
        )
        if clean:
            out[doc_type.strip()] = clean
    return out


def _parse_extraction_hints(raw: Any) -> DomainExtractionHints:
    """Build a `DomainExtractionHints` from the YAML sub-mapping.

 Missing block → defaults (all empty tuples). Tolerant of
 malformed entries: non-iterable categories silently become
 empty tuples."""
    if not isinstance(raw, dict) or not raw:
        return DomainExtractionHints()

    def _tuple(key: str) -> tuple[str, ...]:
        items = raw.get(key) or ()
        if not isinstance(items, (list, tuple)):
            return ()
        return tuple(str(s).strip() for s in items if str(s).strip())

    return DomainExtractionHints(
        metadata_fields=_tuple("metadata_fields"),
        entity_hints=_tuple("entity_hints"),
        table_hints=_tuple("table_hints"),
        image_hints=_tuple("image_hints"),
        terminology_hints=_tuple("terminology_hints"),
        retrieval_hints=_tuple("retrieval_hints"),
        entity_aliases=_parse_entity_aliases(raw.get("entity_aliases")),
    )


def _parse_entity_aliases(raw: Any) -> tuple[EntityAlias, ...]:
    """Build the static entity-alias bundle from the YAML block.

    Each YAML entry is ``{canonical_name, aliases, entity_type?,
    confidence?}``. ``source`` is always
    ``domain_config`` for pack-shipped entries (the resolver lets
    other sources populate at runtime).

    Tolerant: malformed entries are skipped rather than failing the
    whole pack load — the alias surface is advisory, never
    load-bearing for correctness."""
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[EntityAlias] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        canonical = str(entry.get("canonical_name") or "").strip()
        if not canonical:
            continue
        raw_aliases = entry.get("aliases") or ()
        if not isinstance(raw_aliases, (list, tuple)):
            raw_aliases = ()
        aliases = tuple(
            str(a).strip()
            for a in raw_aliases
            if str(a).strip() and str(a).strip() != canonical
        )
        entity_type = entry.get("entity_type")
        if entity_type is not None:
            entity_type = str(entity_type).strip() or None
        confidence_raw = entry.get("confidence", 1.0)
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = 1.0
        confidence = max(0.0, min(1.0, confidence))
        out.append(EntityAlias(
            canonical_name=canonical,
            aliases=aliases,
            entity_type=entity_type,
            confidence=confidence,
            source="domain_config",
        ))
    return tuple(out)


def _parse_validation_rules(raw: Any) -> DomainValidationRules:
    """Build a `DomainValidationRules` from the YAML sub-mapping.

 Same tolerance contract as `_parse_extraction_hints`."""
    if not isinstance(raw, dict) or not raw:
        return DomainValidationRules()

    def _tuple(key: str) -> tuple[str, ...]:
        items = raw.get(key) or ()
        if not isinstance(items, (list, tuple)):
            return ()
        return tuple(str(s).strip() for s in items if str(s).strip())

    return DomainValidationRules(
        required_metadata_fields=_tuple("required_metadata_fields"),
        expected_document_structure=_tuple("expected_document_structure"),
        low_quality_warning_conditions=_tuple("low_quality_warning_conditions"),
        enrichment_triggers=_tuple("enrichment_triggers"),
    )


def _parse_prompt_pack(raw: Any) -> DomainPromptPack:
    """Build a `DomainPromptPack` from the YAML sub-mapping.

 Each field is a single string. Missing / empty → None (the
 enricher uses its built-in default). Heredoc-style YAML scalars
 work; we strip surrounding whitespace."""
    if not isinstance(raw, dict) or not raw:
        return DomainPromptPack()

    def _str_or_none(key: str) -> str | None:
        value = raw.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    return DomainPromptPack(
        text_enrichment_prompt=_str_or_none("text_enrichment_prompt"),
        metadata_enrichment_prompt=_str_or_none("metadata_enrichment_prompt"),
        table_enrichment_prompt=_str_or_none("table_enrichment_prompt"),
        image_enrichment_prompt=_str_or_none("image_enrichment_prompt"),
        classification_prompt=_str_or_none("classification_prompt"),
        validation_prompt=_str_or_none("validation_prompt"),
    )


def _parse_enrichment_policy(raw: Any) -> DomainEnrichmentPolicy:
    """Build a `DomainEnrichmentPolicy` from the YAML sub-mapping.

 Missing block (None / empty dict) → policy=auto with empty lists.
 Tolerant of malformed entries: unknown keys are ignored, lists
 coerced via tuple, and the policy string passes through to
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
