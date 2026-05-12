"""Idempotent backfill for the document-centric refactor.

Pre-refactor, J1 stored runs and documents as siblings — every run
carries a ``document_id`` FK, but no document carries an
``active_run_id`` pointer or a ``knowledge_state``. The backfill
walks an existing project's documents + runs and stamps the new
fields on each ``DocumentRecord`` so the document-centric surfaces
(retrieval gate, REST projectors, UI list) have something to read.

Three goals:

1. **Idempotent.** Running the backfill twice over the same data
   must produce identical output — no duplicate writes, no state
   churn. Callers can safely run it on every worker startup.

2. **Read-only on first pass.** The backfill never deletes anything
   and never overwrites a field that's already been set by a more
   recent action (e.g. an operator who manually detached a document
   between runs of the backfill).

3. **Conservative defaults.** Every backfilled document gets
   ``knowledge_state="attached"`` so the new retrieval gate is a
   no-op for pre-refactor projects. The active-run selection rule
   matches the spec:

       1. latest succeeded run, else
       2. latest failed run with a compile checkpoint, else
       3. latest run by ``created_at``.

This module DOES NOT touch ``IngestionRun.run_type`` /
``document_version_id`` / ``parent_run_id``. The dataclass defaults
already handle that on read (every legacy run reads back as
``run_type="initial"``). Stamping the new run fields requires
rewriting the JSONL log, which is a separate, riskier operation —
we leave it for Phase 4 when re-index/resume start emitting the
correct ``run_type`` for new runs and we can backfill old runs at
the same time.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone

from j1.documents.models import DocumentRecord
from j1.intake.registry import JsonSourceRegistry
from j1.jobs.status import ProcessingStatus
from j1.projects.context import ProjectContext
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import IngestionRunStore

_log = logging.getLogger("j1.documents.backfill")


# Run statuses that count as "succeeded enough to be the document's
# current usable result." Mirrors `IngestionRun.is_terminal()` but
# excludes failure / cancelled — those don't carry a usable final.
_USABLE_TERMINAL_STATUSES: frozenset[RunStatus] = frozenset({
    RunStatus.SUCCEEDED,
    RunStatus.SUCCEEDED_WITH_WARNINGS,
})


def select_active_run_id(runs: list[IngestionRun]) -> str | None:
    """Apply the document-centric active-run selection rule.

    Returns ``None`` when no runs satisfy any tier — typical for a
    just-uploaded document whose first ingestion is still queued.

    The rule is deterministic + auditable: every selection is
    traceable to (status, has_compile_checkpoint, started_at). No
    LLM, no heuristic — this is the contract the FE depends on.
    """
    if not runs:
        return None

    # Tier 1: latest succeeded run. SUCCEEDED_WITH_WARNINGS counts
    # because the result is still usable as knowledge — the
    # warnings are operational hints, not failure modes.
    succeeded = [r for r in runs if r.status in _USABLE_TERMINAL_STATUSES]
    if succeeded:
        return _latest_by_started(succeeded).run_id

    # Tier 2: latest failed run with a compile checkpoint. Resume
    # is only meaningful when compile produced usable artifacts.
    with_checkpoint = [
        r for r in runs
        if r.status == RunStatus.FAILED and _has_compile_checkpoint(r)
    ]
    if with_checkpoint:
        return _latest_by_started(with_checkpoint).run_id

    # Tier 3: fall back to latest run regardless of status. Lets
    # the FE show SOMETHING while a run is mid-flight or while a
    # failed-pre-compile attempt is still the user's most recent
    # interaction with the document.
    return _latest_by_started(runs).run_id


def _has_compile_checkpoint(run: IngestionRun) -> bool:
    """True iff the run's metadata carries a usable compile
    checkpoint — the gate the Phase 5 resume flow requires.

    Reads the same ``resume_snapshot`` blob the existing
    ``resume-from-checkpoint`` REST handler validates, so the
    backfill and the runtime agree on what "compile succeeded" means.
    """
    snapshot = run.metadata.get("resume_snapshot")
    if not isinstance(snapshot, dict):
        return False
    # The runtime stamps `completed_steps` on the snapshot when
    # compile finishes successfully. An empty/missing list means
    # no checkpoint.
    completed = snapshot.get("completed_steps")
    if isinstance(completed, list) and "compile" in completed:
        return True
    # Fallback: check `step_results` if the runtime ever stops
    # writing `completed_steps` (defensive).
    step_results = run.metadata.get("step_results")
    if isinstance(step_results, dict):
        compile_status = step_results.get("compile", {}).get("status")
        if compile_status == "completed":
            return True
    return False


def _latest_by_started(runs: list[IngestionRun]) -> IngestionRun:
    """Stable pick: most recent ``started_at`` wins; ties broken by
    ``updated_at`` (just in case two runs claim the same start ts)."""
    return max(runs, key=lambda r: (r.started_at, r.updated_at))


def backfill_project(
    ctx: ProjectContext,
    *,
    registry: JsonSourceRegistry,
    run_store: IngestionRunStore,
    now: datetime | None = None,
) -> dict[str, int]:
    """Backfill the document-centric fields for one project.

    Returns a summary dict suitable for logging:

    ::

        {
            "documents_inspected": 12,
            "documents_updated":   7,
            "documents_unchanged": 5,
        }

    Idempotent: if every document already carries the correct
    ``active_run_id`` and a non-empty ``knowledge_state``, this is
    a no-op and ``documents_updated`` is 0.

    Concurrency: the registry's atomic-rename write pattern means
    two backfill processes running simultaneously will produce a
    correct final state (the later writer wins, both produce the
    same target) but may waste work. Operators that care about
    cost should run the backfill once on worker startup, not
    on-demand.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)
    runs_by_doc = _index_runs_by_document(ctx, run_store)
    documents = registry.list_documents(ctx)
    updated = 0
    for doc in documents:
        runs = runs_by_doc.get(doc.document_id, [])
        new_active = select_active_run_id(runs)
        # Default to "attached" only when the field is empty —
        # never overwrite a state set by a more recent action.
        new_state = doc.knowledge_state or "attached"
        updates: dict = {}
        if doc.knowledge_state != new_state:
            updates["knowledge_state"] = new_state
        if doc.active_run_id != new_active and new_active is not None:
            updates["active_run_id"] = new_active
        if doc.updated_at is None:
            updates["updated_at"] = now
        if not updates:
            continue
        _replace_record(registry, ctx, doc, updates)
        updated += 1
        _log.info(
            "backfilled document %s: %s",
            doc.document_id,
            {k: str(v) for k, v in updates.items()},
        )
    return {
        "documents_inspected": len(documents),
        "documents_updated": updated,
        "documents_unchanged": len(documents) - updated,
    }


def _index_runs_by_document(
    ctx: ProjectContext, run_store: IngestionRunStore,
) -> dict[str, list[IngestionRun]]:
    """O(n) scan over the run log → grouped by ``document_id``. The
    run store's `list_runs` returns one snapshot per run_id (latest
    wins) so we're not double-counting in-progress mutations."""
    grouped: dict[str, list[IngestionRun]] = {}
    # `list()` returns latest-snapshot-per-run-id which is exactly
    # what we want for the active-run picker — no double counting
    # of mid-flight state mutations.
    for run in run_store.list(ctx):
        if not run.document_id:
            # Defensive: a malformed legacy run with no document_id
            # would silently get lost in a group. Log + skip.
            _log.warning(
                "backfill: skipping run %s — no document_id",
                run.run_id,
            )
            continue
        grouped.setdefault(run.document_id, []).append(run)
    return grouped


def _replace_record(
    registry: JsonSourceRegistry,
    ctx: ProjectContext,
    doc: DocumentRecord,
    updates: dict,
) -> None:
    """In-place update via the registry's underlying file. The
    public registry surface doesn't expose a generic ``update()``
    method (only ``update_status``) so we read+write the list. This
    is the same pattern the registry uses internally."""
    # Pull all records, swap the one we care about, write back.
    records = registry._read(ctx)  # type: ignore[attr-defined]  # ctrl'd access
    for i, existing in enumerate(records):
        if existing.document_id != doc.document_id:
            continue
        records[i] = replace(existing, **updates)
        registry._write(ctx, records)  # type: ignore[attr-defined]
        return
    # Shouldn't happen — we just read the doc from the registry —
    # but fail loudly if it does so we catch the bug in tests.
    raise RuntimeError(
        f"backfill: document {doc.document_id} disappeared mid-write",
    )


__all__ = ["backfill_project", "select_active_run_id"]
