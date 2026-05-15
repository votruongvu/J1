"""Shared parser for ``document_profile_rules`` YAML blocks.

Both the civil-engineering pack and the generic pack populate this
list — the generic pack with cross-domain patterns (RFP, meeting
minutes, memo, notes), domain packs with their own specifics.

Why this lives alongside the data model rather than under
``j1.processing``: it's pure data extraction, no profiler /
planner coupling. The resolver under ``j1.processing`` consumes
the parsed rules without caring how they were authored.
"""

from __future__ import annotations

import re
from typing import Any

from j1.domains.models import (
    DocumentProfileRule,
    DocumentProfileRuleHints,
)


__all__ = [
    "DocumentProfileRuleLintError",
    "lint_document_profile_rule",
    "parse_document_profile_rules",
]


_VALID_PROFILES = frozenset({"minimum_queryable", "standard", "advanced"})


class DocumentProfileRuleLintError(ValueError):
    """Pack-load-time linter rejection. Distinct from
    ``ValueError`` so deployment bootstrap can surface a clear
    "the YAML is malformed" message vs a runtime regex fault."""


# Catch-all regex patterns we reject by default. A rule that matches
# everything voids the "named-pattern recommendation" semantic and
# silently elevates every doc into the rule's profile — almost
# always an authoring mistake.
_CATCHALL_PATTERNS: frozenset[str] = frozenset({
    ".", ".*", ".+", "^.*$", "^.+$",
    r"\w+", r"\w*", r"\S+", r"\S*",
    r"[\s\S]*", r"[\s\S]+",
    ".*?", ".+?",
})


def lint_document_profile_rule(
    rule_id: str,
    *,
    priority: Any,
    recommended_profile: Any,
    reason: Any,
    filename_regex: str | None,
    title_regex: str | None,
    allow_catchall: bool = False,
) -> None:
    """Raise ``DocumentProfileRuleLintError`` if the rule is malformed.

    Strict checks:
      * ``id`` non-empty
      * ``priority`` is an int / coercible to int
      * ``recommended_profile`` is one of the wire enums
      * ``reason`` non-empty (operator-readable explanation is REQUIRED;
        the FE renders it verbatim, so an empty reason is a UX bug)
      * at least one of ``filename_regex`` / ``title_regex`` is set
      * supplied regexes compile
      * regex is NOT a catch-all unless ``allow_catchall=True``
    """
    if not rule_id:
        raise DocumentProfileRuleLintError(
            "document_profile_rule is missing an id"
        )
    try:
        int(priority)
    except (TypeError, ValueError) as exc:
        raise DocumentProfileRuleLintError(
            f"rule {rule_id!r}: priority must be an integer, got "
            f"{priority!r}"
        ) from exc
    if recommended_profile not in _VALID_PROFILES:
        raise DocumentProfileRuleLintError(
            f"rule {rule_id!r}: unknown recommended_profile "
            f"{recommended_profile!r} (valid: "
            f"{', '.join(sorted(_VALID_PROFILES))})"
        )
    if not isinstance(reason, str) or not reason.strip():
        raise DocumentProfileRuleLintError(
            f"rule {rule_id!r}: ``reason`` is required and must be "
            "a non-empty string (the FE renders it verbatim)"
        )
    if not filename_regex and not title_regex:
        raise DocumentProfileRuleLintError(
            f"rule {rule_id!r}: at least one of filename_regex / "
            "title_regex must be set"
        )
    for label, pattern in (
        ("filename_regex", filename_regex),
        ("title_regex", title_regex),
    ):
        if not pattern:
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            raise DocumentProfileRuleLintError(
                f"rule {rule_id!r}: {label} does not compile "
                f"({exc!s})"
            ) from exc
        if not allow_catchall:
            stripped = pattern.strip()
            # Strip a leading flag block like "(?i)" / "(?im)" so
            # case-insensitive variants of catch-alls are caught too.
            without_flags = re.sub(r"^\(\?[aiLmsux]+\)", "", stripped)
            if without_flags.strip() in _CATCHALL_PATTERNS:
                raise DocumentProfileRuleLintError(
                    f"rule {rule_id!r}: {label}={pattern!r} is a "
                    "catch-all pattern. A rule that matches every "
                    "document is almost certainly a mistake; set "
                    "``allow_catchall: true`` on the rule if this "
                    "is intentional."
                )


def parse_document_profile_rules(
    raw: Any,
) -> tuple[DocumentProfileRule, ...]:
    """Parse the ``document_profile_rules:`` block of a domain YAML.

    Tolerant of:
      * missing block (returns empty tuple)
      * a single mapping (wrapped to a list)
      * malformed entries (skipped with no exception)

    Strict on:
      * unknown profile names — raises ``ValueError`` at load time
        so the deployment fails to start rather than silently mis-
        routing recommendations.

    The output is sorted by ``priority`` ascending so consumers
    can iterate in match order without re-sorting.
    """
    if raw is None:
        return ()
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return ()

    parsed: list[DocumentProfileRule] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        rule_id = str(entry.get("id") or "").strip()
        recommended = str(entry.get("recommended_profile") or "").strip()
        if not rule_id or not recommended:
            # No id / no profile → can't act on it. Skip silently
            # so a half-authored rule doesn't break the whole pack.
            continue
        if recommended not in _VALID_PROFILES:
            raise ValueError(
                f"document_profile_rule {rule_id!r} declares unknown "
                f"recommended_profile {recommended!r}; valid values: "
                f"{', '.join(sorted(_VALID_PROFILES))}"
            )
        filename_regex = entry.get("filename_regex")
        title_regex = entry.get("title_regex")
        # At least one matcher is required — a rule without any
        # pattern would match everything, which is almost certainly
        # an authoring error.
        if not filename_regex and not title_regex:
            continue
        priority = entry.get("priority")
        try:
            priority_int = int(priority) if priority is not None else 100
        except (TypeError, ValueError):
            priority_int = 100
        confidence = entry.get("confidence")
        try:
            confidence_float = (
                float(confidence) if confidence is not None else 0.7
            )
        except (TypeError, ValueError):
            confidence_float = 0.7
        confidence_float = max(0.0, min(1.0, confidence_float))
        reason = str(entry.get("reason") or "").strip()
        allow_catchall = bool(entry.get("allow_catchall", False))
        # Strict lint at load time so authoring mistakes surface at
        # deployment bootstrap, not at request time.
        lint_document_profile_rule(
            rule_id,
            priority=priority_int,
            recommended_profile=recommended,
            reason=reason,
            filename_regex=(
                str(filename_regex).strip() if filename_regex else None
            ),
            title_regex=(
                str(title_regex).strip() if title_regex else None
            ),
            allow_catchall=allow_catchall,
        )
        hints = _parse_hints(entry.get("hints"))
        parsed.append(DocumentProfileRule(
            id=rule_id,
            priority=priority_int,
            recommended_profile=recommended,
            confidence=confidence_float,
            reason=reason,
            filename_regex=(
                str(filename_regex).strip() if filename_regex else None
            ),
            title_regex=(
                str(title_regex).strip() if title_regex else None
            ),
            hints=hints,
        ))
    parsed.sort(key=lambda r: (r.priority, r.id))
    return tuple(parsed)


def _parse_hints(raw: Any) -> DocumentProfileRuleHints:
    """Build a hints dataclass. Unknown keys are silently dropped —
    forward-compat for future hint flags."""
    if not isinstance(raw, dict):
        return DocumentProfileRuleHints()
    def _bool(key: str) -> bool:
        return bool(raw.get(key, False))
    return DocumentProfileRuleHints(
        likely_tables=_bool("likely_tables"),
        likely_images=_bool("likely_images"),
        likely_requirements=_bool("likely_requirements"),
        likely_scanned=_bool("likely_scanned"),
        likely_long_document=_bool("likely_long_document"),
    )
