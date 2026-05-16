"""Knowledge Memory auto-build / rebuild hooks — Phase 3A.

Two side-effect-free helpers that the orchestration layer calls
at the end of the compile-activity-success path and the
enrichment-activity-success path:

  * `maybe_build_after_compile(...)` — feature-gated by
    `J1_KNOWLEDGE_MEMORY_AUTO_BUILD_ENABLED`. Produces a base
    `knowledge_memory` artifact from compile + domain pack
    signals.
  * `maybe_build_after_enrichment(...)` — feature-gated by
    `J1_KNOWLEDGE_MEMORY_REBUILD_AFTER_ENRICHMENT`. Rebuilds the
    same snapshot's memory with enrichment insights folded in;
    Phase 2's supersede sweep handles the prior base build.

Hard contract on the hooks:

  * **Never raise** into the calling activity. A memory-build
    failure must not fail a successful compile / enrichment run.
    Failures return a `KnowledgeMemoryBuildAttempt(status="failed")`
    record and emit a structured log event; the activity returns
    normally.
  * **Best-effort dependencies.** If the `KnowledgeMemoryService`
    isn't wired into the activity (legacy bootstrap path),
    the hook returns a `status="skipped"` attempt with a clear
    reason. Test wiring + minimal deployments don't need to
    construct the service.
  * **Deterministic.** No LLM calls. The hook just dispatches
    to the existing `KnowledgeMemoryService.build_and_persist`
    seam.

The `KnowledgeMemoryBuildAttempt` is a serialisable record so it
can ride into the final ingestion report when Phase 3B wires
metadata persistence. Phase 3A keeps the attempt visible via the
structured log + the artifact's own `metadata.trigger` stamp.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any


_log = logging.getLogger(__name__)


# ---- Trigger vocabulary ----------------------------------------


# Stable strings stamped onto the `knowledge_memory` artifact's
# `metadata.trigger` AND emitted on structured log events. Add
# values, don't rename — dashboards filter on these.
TRIGGER_AFTER_COMPILE = "after_compile"
TRIGGER_AFTER_DOMAIN_ENRICHMENT = "after_domain_enrichment"
TRIGGER_MANUAL = "manual"


# ---- Source vocabulary -----------------------------------------


# `source` field on the attempt mirrors the prompt's contract
# vocabulary. Phase 3A only emits these two; later phases may
# add `manual_with_overrides` / `auto_with_overrides` etc.
SOURCE_BASE_COMPILE = "base_compile"
SOURCE_BASE_COMPILE_PLUS_DOMAIN_INSIGHTS = "base_compile_plus_domain_insights"


# ---- Status vocabulary -----------------------------------------


STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


# Common skip reasons — pinned strings so a downstream consumer
# can branch on them rather than parsing free-form English.
SKIP_REASON_DISABLED_BY_SETTINGS = "disabled_by_settings"
SKIP_REASON_SERVICE_NOT_WIRED = "service_not_wired"
SKIP_REASON_NO_ACTIVE_SNAPSHOT = "no_active_snapshot"


@dataclass(frozen=True)
class KnowledgeMemoryBuildAttempt:
    """One attempt to build the knowledge_memory artifact.

    Returned by `maybe_build_after_compile` and
    `maybe_build_after_enrichment`. The activity layer can choose
    to forward this into the final ingestion report (Phase 3B);
    for Phase 3A it's emitted as a structured log event AND
    serialised onto the artifact's own metadata.

    Field semantics:

      * `trigger` — which lifecycle hook fired (`after_compile`,
        `after_domain_enrichment`, `manual`).
      * `status` — `completed` / `failed` / `skipped`.
      * `artifact_id` — populated on `completed`; `None` otherwise.
      * `entry_count` — populated on `completed`.
      * `source` — `base_compile` after compile-only builds,
        `base_compile_plus_domain_insights` after enrichment
        rebuilds. `None` on `failed` / `skipped`.
      * `includes_domain_insights` — convenience boolean for FE
        consumers that don't want to string-match on `source`.
        Mirrors the `metadata.includes_domain_insights` stamp on
        the artifact.
      * `error` — short operator-readable message on `failed`.
      * `reason` — pinned short string on `skipped` (e.g.
        `disabled_by_settings`, `service_not_wired`).
      * `warnings` — pass-through warnings from the underlying
        `KnowledgeMemoryBuildResult`.
    """

    trigger: str
    status: str
    artifact_id: str | None = None
    entry_count: int = 0
    snapshot_id: str | None = None
    run_id: str | None = None
    source: str | None = None
    includes_domain_insights: bool = False
    error: str | None = None
    reason: str | None = None
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict for log events / final
        report attempts list."""
        return {
            "trigger": self.trigger,
            "status": self.status,
            "artifact_id": self.artifact_id,
            "entry_count": self.entry_count,
            "snapshot_id": self.snapshot_id,
            "run_id": self.run_id,
            "source": self.source,
            "includes_domain_insights": self.includes_domain_insights,
            "error": self.error,
            "reason": self.reason,
            "warnings": list(self.warnings),
        }


# ---- Hooks -----------------------------------------------------


def maybe_build_after_compile(
    *,
    ctx: Any,
    document_id: str,
    service: Any | None,
    settings: Any,
    actor: str = "system",
) -> KnowledgeMemoryBuildAttempt:
    """Optionally build the base knowledge_memory artifact after
    a successful compile.

    Behaviour:

      * ``settings.auto_build_after_compile`` is False → return a
        ``skipped`` attempt with reason
        ``disabled_by_settings``. No log spam beyond a DEBUG line.
      * ``service is None`` → ``skipped`` with reason
        ``service_not_wired``. Useful when the activity is wired
        in a legacy bootstrap that didn't construct the service.
      * Service raises ``NoActiveSnapshotError`` → ``skipped``
        with reason ``no_active_snapshot``. (The artifact-promotion
        order in some workflow branches can leave a window where
        the activity has run but the snapshot isn't yet active.)
      * Any other exception → ``failed`` with the short error
        message; emits a structured log event.
      * Success → ``completed`` with artifact id + entry count;
        emits a structured log event.

    The hook never re-raises into the caller. Phase 3A guarantee:
    a memory-build failure never converts a successful compile
    into a failed run.
    """
    return _run_hook(
        ctx=ctx,
        document_id=document_id,
        service=service,
        enabled=bool(getattr(settings, "auto_build_after_compile", False)),
        trigger=TRIGGER_AFTER_COMPILE,
        actor=actor,
        # Compile-time build never has enrichment artifacts present
        # in the registry yet (enrichment runs AFTER compile), so
        # the expected source is base_compile. The service still
        # writes whatever it finds — if the deployment somehow has
        # stale enrichment for the same snapshot in the registry
        # the `source` derived from the result will be the
        # enriched flavour; we honour what actually happened
        # rather than the trigger's expectation.
    )


def maybe_build_after_enrichment(
    *,
    ctx: Any,
    document_id: str,
    service: Any | None,
    settings: Any,
    actor: str = "system",
) -> KnowledgeMemoryBuildAttempt:
    """Optionally rebuild the knowledge_memory artifact after a
    successful post-compile Domain Enrichment. Same contract as
    `maybe_build_after_compile`: feature-gated, best-effort,
    non-raising.

    Phase 2's supersede sweep in
    ``ProcessingService.persist_knowledge_memory`` handles the
    one-active-per-snapshot invariant — when this rebuild
    succeeds, the prior base-only artifact for the same snapshot
    is flipped to ``search_state="superseded"``.
    """
    return _run_hook(
        ctx=ctx,
        document_id=document_id,
        service=service,
        enabled=bool(getattr(settings, "rebuild_after_enrichment", False)),
        trigger=TRIGGER_AFTER_DOMAIN_ENRICHMENT,
        actor=actor,
    )


# ---- Internal -------------------------------------------------


def _run_hook(
    *,
    ctx: Any,
    document_id: str,
    service: Any | None,
    enabled: bool,
    trigger: str,
    actor: str,
) -> KnowledgeMemoryBuildAttempt:
    """Common hook body. Splits skip / fail / success paths and
    emits the structured log event uniformly so dashboards see
    the same event shape regardless of trigger."""
    if not enabled:
        attempt = KnowledgeMemoryBuildAttempt(
            trigger=trigger,
            status=STATUS_SKIPPED,
            reason=SKIP_REASON_DISABLED_BY_SETTINGS,
        )
        _log.debug(
            "knowledge_memory.build_attempted",
            extra={
                "event": "knowledge_memory.build_attempted",
                **attempt.to_payload(),
            },
        )
        return attempt

    if service is None:
        attempt = KnowledgeMemoryBuildAttempt(
            trigger=trigger,
            status=STATUS_SKIPPED,
            reason=SKIP_REASON_SERVICE_NOT_WIRED,
        )
        _log.warning(
            "knowledge_memory.build_attempted",
            extra={
                "event": "knowledge_memory.build_attempted",
                **attempt.to_payload(),
            },
        )
        return attempt

    # Import the no-snapshot signal lazily to keep this module
    # free of `j1.memory.service` at import time — tests that
    # build the helper with a stub service don't need to install
    # the real service module.
    try:
        from j1.memory.service import NoActiveSnapshotError
    except ImportError:  # pragma: no cover — defensive
        NoActiveSnapshotError = RuntimeError  # type: ignore[assignment,misc]

    try:
        result = service.build_and_persist(
            ctx, document_id, actor=actor, trigger=trigger,
        )
    except NoActiveSnapshotError:
        attempt = KnowledgeMemoryBuildAttempt(
            trigger=trigger,
            status=STATUS_SKIPPED,
            reason=SKIP_REASON_NO_ACTIVE_SNAPSHOT,
        )
        _log.info(
            "knowledge_memory.build_attempted",
            extra={
                "event": "knowledge_memory.build_attempted",
                **attempt.to_payload(),
            },
        )
        return attempt
    except Exception as exc:  # noqa: BLE001 — best-effort hook
        attempt = KnowledgeMemoryBuildAttempt(
            trigger=trigger,
            status=STATUS_FAILED,
            error=f"{type(exc).__name__}: {exc}",
        )
        _log.warning(
            "knowledge_memory.build_attempted",
            extra={
                "event": "knowledge_memory.build_attempted",
                **attempt.to_payload(),
            },
            exc_info=True,
        )
        return attempt

    source, includes_insights = _resolve_source(trigger, result)
    attempt = KnowledgeMemoryBuildAttempt(
        trigger=trigger,
        status=STATUS_COMPLETED,
        artifact_id=getattr(result, "artifact_id", None),
        entry_count=int(getattr(result, "entry_count", 0)),
        snapshot_id=getattr(result, "snapshot_id", None),
        run_id=getattr(result, "run_id", None),
        source=source,
        includes_domain_insights=includes_insights,
        warnings=tuple(getattr(result, "warnings", ()) or ()),
    )
    _log.info(
        "knowledge_memory.build_attempted",
        extra={
            "event": "knowledge_memory.build_attempted",
            **attempt.to_payload(),
        },
    )
    return attempt


def _resolve_source(
    trigger: str, result: Any,
) -> tuple[str, bool]:
    """Derive the `source` + `includes_domain_insights` fields
    from the trigger and the build result.

    Phase 3A rule:

      * Trigger ``after_compile`` → ``base_compile``,
        ``includes=False`` UNLESS the build payload actually
        carries enrichment artifacts (defensive: a deployment
        with very fast enrichment could land artifacts before
        the compile activity returns; we honour what was
        included).
      * Trigger ``after_domain_enrichment`` → if the build saw at
        least one enrichment artifact in
        ``source.built_from.enrichment_artifact_ids``, it's
        ``base_compile_plus_domain_insights``; otherwise (the
        rebuild ran but enrichment was empty) we fall back to
        ``base_compile``.
      * Trigger ``manual`` → derived the same way as the
        post-enrichment path; honours the actual artifact set.
    """
    enriched = _enrichment_artifact_count(result) > 0
    if enriched:
        return SOURCE_BASE_COMPILE_PLUS_DOMAIN_INSIGHTS, True
    return SOURCE_BASE_COMPILE, False


def _enrichment_artifact_count(result: Any) -> int:
    """Best-effort read of the enrichment-artifact count from the
    build result. The result is a `KnowledgeMemoryBuildResult`
    today; we use `getattr` chains so stub results in tests don't
    need to mirror the full dataclass."""
    # The result message format includes the count, but we shouldn't
    # parse English. The simplest reliable signal is asking the
    # underlying `KnowledgeMemoryBuildResult` whether its
    # `built_from.enrichment_artifact_ids` is non-empty. The
    # result object doesn't expose that directly today; for Phase
    # 3A we infer from `entry_count` heuristically — when the
    # build is post-enrichment AND the count went up beyond the
    # base build, we assume enrichment contributed.
    #
    # Cleaner alternative used here: read the count from
    # `result.message` only as a backstop. The primary signal
    # comes from a `built_from` attribute when the result object
    # exposes it. We accept either path so future result-shape
    # evolutions don't require lockstep changes.
    built_from = getattr(result, "built_from", None)
    if built_from is not None:
        ids = getattr(built_from, "enrichment_artifact_ids", None)
        if ids:
            return len(ids)
    # Fallback: scan the message string for the marker the
    # service emits ("from N enrichment artifact(s)"). Best-effort
    # — if the format changes the attempt simply marks
    # `includes_domain_insights=False`, which is the safe default.
    message = str(getattr(result, "message", "") or "")
    import re
    match = re.search(r"(\d+)\s+enrichment\s+artifact", message)
    if match:
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return 0
    return 0
