"""Domain Pack registry + selection orchestrator.

Single entry point the planner uses to:

 1. List the registered domains (for the API/FE).
 2. Resolve the domain for a given run, applying the
 spec's selection precedence:
 user override → workspace default → auto-detect → fallback.
 3. Auto-detect by running each pack's detection function against a
 `DetectionContext` and picking the highest-confidence match
 above the configured threshold.

Pure data + small functions — no I/O, no Temporal coupling. The
activity layer constructs a `DetectionContext` from the workflow
state and calls `select_domain`."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from j1.domains.models import (
    DOMAIN_SELECTION_AUTO_DETECTED,
    DOMAIN_SELECTION_FALLBACK_GENERAL,
    DOMAIN_SELECTION_USER,
    DOMAIN_SELECTION_WORKSPACE,
    DomainContext,
    DomainDetectionResult,
    DomainPack,
)


__all__ = [
    "DOMAIN_GENERAL",
    "DOMAIN_SELECTION_AUTO_DETECTED",
    "DOMAIN_SELECTION_FALLBACK_GENERAL",
    "DOMAIN_SELECTION_USER",
    "DOMAIN_SELECTION_WORKSPACE",
    "DEFAULT_MIN_DETECTION_CONFIDENCE",
    "DomainRegistry",
    "default_registry",
    "select_domain",
]


_log = logging.getLogger("j1.domains")


# Generic-domain id is part of the wire vocabulary — keep stable.
DOMAIN_GENERAL = "general"


# Default detection threshold — overridden per call from the
# `J1_DOMAIN_DETECTION_MIN_CONFIDENCE` env var via PlanningSettings.
DEFAULT_MIN_DETECTION_CONFIDENCE = 0.65


# ---- Registry --------------------------------------------------------


class DomainRegistry:
    """In-process registry of `DomainPack` instances.

 Mutable on construction (`register`), immutable in production
 use — `default_registry` returns a singleton initialised at
 import time. Tests build their own to control which packs are
 visible."""

    def __init__(self) -> None:
        self._packs: dict[str, DomainPack] = {}

    def register(self, pack: DomainPack) -> None:
        """Add a pack. Re-registering the same id replaces the entry
 — used by tests to swap stub packs for the production ones."""
        self._packs[pack.id] = pack

    def get(self, domain_id: str) -> DomainPack | None:
        return self._packs.get(domain_id)

    def list(self) -> list[DomainPack]:
        return list(self._packs.values())

    def list_ids(self) -> list[str]:
        return sorted(self._packs.keys())

    def extended_document_types(self) -> set[str]:
        """Union of every registered pack's `extends_document_types`.

 Used by the planning-result validator so wire-schema
 `document_type` is accepted whenever ANY registered pack
 contributes the type — letting domain packs widen the
 taxonomy without core code changes."""
        out: set[str] = set()
        for pack in self._packs.values():
            out.update(pack.extends_document_types)
        return out


# ---- Selection orchestrator -----------------------------------------


@dataclass(frozen=True)
class _SelectionInputs:
    """Scratch struct grouping the resolver's inputs. Keeping the
 public function signature wide reduces coupling — a future
 deployment that supplies a different override scheme just calls
 the same function with different inputs."""

    user_override: str | None
    workspace_default: str | None
    detection_enabled: bool
    detection_threshold: float
    allowed_overrides: frozenset[str]


def select_domain(
    *,
    registry: DomainRegistry,
    detection_context: object,
    user_override: str | None = None,
    workspace_default: str | None = None,
    detection_enabled: bool = True,
    detection_threshold: float = DEFAULT_MIN_DETECTION_CONFIDENCE,
    allowed_overrides: frozenset[str] | None = None,
) -> DomainContext:
    """Resolve the selected domain for one run.

 Returns a `DomainContext` ready to attach to `planning_result.json`.
 Always returns a context — never None — so callers don't branch
 on absence. The fallback context's `selected_domain="general"`
 + `selection_source="fallback_general"` is the documented default.

 Selection precedence:

 1. **User override** (validated against `allowed_overrides`).
 Honored even when the evidence is weak; a warning is added
 when the chosen pack's confidence is below threshold.
 2. **Workspace default** (same validation).
 3. **Auto-detection** if enabled — run every pack's `detect`,
 pick the highest-confidence above the threshold.
 4. **Fallback** to `general`.
 """
    inputs = _SelectionInputs(
        user_override=_clean(user_override),
        workspace_default=_clean(workspace_default),
        detection_enabled=detection_enabled,
        detection_threshold=detection_threshold,
        allowed_overrides=allowed_overrides or frozenset({DOMAIN_GENERAL}),
    )

    # 1. User override wins.
    if inputs.user_override:
        return _resolve_override(
            registry=registry,
            domain_id=inputs.user_override,
            source=DOMAIN_SELECTION_USER,
            allowed_overrides=inputs.allowed_overrides,
            detection_context=detection_context,
            detection_threshold=inputs.detection_threshold,
        )

    # 2. Workspace default.
    if inputs.workspace_default and inputs.workspace_default != DOMAIN_GENERAL:
        return _resolve_override(
            registry=registry,
            domain_id=inputs.workspace_default,
            source=DOMAIN_SELECTION_WORKSPACE,
            allowed_overrides=inputs.allowed_overrides,
            detection_context=detection_context,
            detection_threshold=inputs.detection_threshold,
        )

    # 3. Auto-detection.
    if inputs.detection_enabled:
        candidates = _run_detection(registry, detection_context)
        if candidates:
            best = max(candidates, key=lambda c: c.confidence)
            if (
                best.confidence >= inputs.detection_threshold
                and best.domain_id != DOMAIN_GENERAL
            ):
                pack = registry.get(best.domain_id)
                version = pack.version if pack else "unknown"
                applied = (
                    (best.applied_rule_id,) if best.applied_rule_id else ()
                )
                return DomainContext(
                    selected_domain=best.domain_id,
                    selection_source=DOMAIN_SELECTION_AUTO_DETECTED,
                    confidence=best.confidence,
                    domain_pack_version=version,
                    evidence=best.evidence,
                    applied_domain_rules=applied,
                    candidates=tuple(candidates),
                )

            # Detection ran but every candidate fell short. Surface
            # the best evidence on the fallback context so reviewers
            # know what was considered.
            return _fallback_context(
                confidence=best.confidence,
                evidence=best.evidence,
                warnings=(
                    f"Best domain candidate {best.domain_id!r} confidence "
                    f"{best.confidence:.2f} below threshold "
                    f"{inputs.detection_threshold:.2f}; "
                    "falling back to generic planning.",
                ),
                candidates=tuple(candidates),
            )

    # 4. Generic fallback.
    return _fallback_context(
        confidence=0.0,
        evidence=("No domain-specific signals detected.",),
        warnings=("Falling back to generic planning.",),
        candidates=(),
    )


def _resolve_override(
    *,
    registry: DomainRegistry,
    domain_id: str,
    source: str,
    allowed_overrides: frozenset[str],
    detection_context: object,
    detection_threshold: float,
) -> DomainContext:
    """Apply a user/workspace override.

 Validates the override against the allowlist; falls back when
 the requested pack isn't registered or isn't allowed. Even when
 the override is honored we still run detection so the result
 carries audit-friendly evidence."""
    if domain_id == DOMAIN_GENERAL:
        return _fallback_context(
            confidence=1.0,
            evidence=(f"{source} override selected the generic domain.",),
            warnings=(),
            candidates=(),
        )

    pack = registry.get(domain_id)
    if pack is None:
        return _fallback_context(
            confidence=0.0,
            evidence=(),
            warnings=(
                f"{source} requested unknown domain {domain_id!r}; "
                "falling back to generic planning.",
            ),
            candidates=(),
        )

    if domain_id not in allowed_overrides:
        return _fallback_context(
            confidence=0.0,
            evidence=(),
            warnings=(
                f"{source} requested domain {domain_id!r} which is "
                f"not in the allow-list; falling back to generic planning.",
            ),
            candidates=(),
        )

    # Override is honored. We still run detection (best-effort) so
    # the context carries evidence + a warning when the chosen pack
    # has weak evidence.
    candidates = _run_detection(registry, detection_context)
    chosen_candidate = next(
        (c for c in candidates if c.domain_id == domain_id), None,
    )
    confidence = chosen_candidate.confidence if chosen_candidate else 1.0
    evidence = (
        chosen_candidate.evidence if chosen_candidate
        else (f"{source} override selected {pack.display_name}.",)
    )
    applied = (
        (chosen_candidate.applied_rule_id,)
        if chosen_candidate and chosen_candidate.applied_rule_id
        else ()
    )

    warnings: list[str] = []
    detected_confidence = (
        chosen_candidate.confidence if chosen_candidate else 0.0
    )
    if detected_confidence < detection_threshold:
        warnings.append(
            f"{source} override forced {domain_id!r} but auto-detect "
            f"confidence ({detected_confidence:.2f}) is below threshold "
            f"{detection_threshold:.2f}; processing continues with the "
            "override but evidence is weak."
        )

    return DomainContext(
        selected_domain=domain_id,
        selection_source=source,
        confidence=confidence,
        domain_pack_version=pack.version,
        evidence=evidence,
        applied_domain_rules=applied,
        warnings=tuple(warnings),
        candidates=tuple(candidates),
    )


def _run_detection(
    registry: DomainRegistry, detection_context: object,
) -> list[DomainDetectionResult]:
    """Run every registered pack's `detect` and collect results."""
    results: list[DomainDetectionResult] = []
    for pack in registry.list():
        if pack.id == DOMAIN_GENERAL or pack.detect is None:
            continue
        try:
            result = pack.detect(detection_context)
        except Exception as exc:  # noqa: BLE001 — pack errors are non-fatal
            _log.warning(
                "domain pack %s detection failed: %s", pack.id, exc,
            )
            continue
        if result is None:
            continue
        results.append(result)
    return results


def _fallback_context(
    *,
    confidence: float,
    evidence: tuple[str, ...],
    warnings: tuple[str, ...],
    candidates: tuple[DomainDetectionResult, ...],
) -> DomainContext:
    return DomainContext(
        selected_domain=DOMAIN_GENERAL,
        selection_source=DOMAIN_SELECTION_FALLBACK_GENERAL,
        confidence=confidence,
        domain_pack_version="generic",
        evidence=evidence,
        warnings=warnings,
        candidates=candidates,
    )


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip().lower()
    return text or None


# ---- Default-registry singleton --------------------------------------


_DEFAULT_REGISTRY: DomainRegistry | None = None


def default_registry() -> DomainRegistry:
    """Return the process-wide default registry.

 Lazy-built on first call so import-time circular imports stay
 impossible (the civil pack needs the registry models, the
 registry can build packs on demand). Tests may construct their
 own `DomainRegistry` instead of using the singleton."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        registry = DomainRegistry()
        from j1.domains.general import build_general_pack
        registry.register(build_general_pack())
        try:
            from j1.domains.civil_engineering import (
                build_civil_engineering_pack,
            )
            registry.register(build_civil_engineering_pack())
        except Exception as exc:  # noqa: BLE001 — pack load is best-effort
            _log.warning("civil_engineering pack failed to load: %s", exc)
        _DEFAULT_REGISTRY = registry
    return _DEFAULT_REGISTRY
