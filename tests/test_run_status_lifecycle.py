"""Unit tests for the Phase-1 lifecycle migration (RunStatus enum,
legacy/canonical mapping helper, additional failure-code constants).

The motivating concern is backward compatibility: existing JSONL
records were written with `created` / `plan_ready` / `running` and
must keep deserialising into the same enum after Phase 1 added the
canonical `received` / `assessment_ready` / `compiling` aliases.
These tests pin the equivalence contract so a future rename
doesn't silently break stored runs.
"""

from __future__ import annotations

import pytest

from j1.runs.models import (
    FAILURE_CODE_ASSESSMENT_FAILED,
    FAILURE_CODE_CHUNK_FAILED,
    FAILURE_CODE_COMPILE_FAILED,
    FAILURE_CODE_EMPTY_DOCUMENT,
    FAILURE_CODE_INDEX_FAILED,
    FAILURE_CODE_VERIFICATION_FAILED,
    LEGACY_TO_CANONICAL_STATUS,
    RunStatus,
    canonical_status,
    status_aliases,
)


# ---- Canonical names exist as enum members ------------------------


def test_canonical_run_statuses_are_defined():
    assert RunStatus.RECEIVED.value == "received"
    assert RunStatus.ASSESSMENT_READY.value == "assessment_ready"
    assert RunStatus.COMPILING.value == "compiling"


def test_legacy_run_statuses_remain_defined():
    """Phase 1 introduces canonical names alongside legacy values —
    legacy enum members MUST keep deserialising so JSONL records
    written by prior worker builds still parse."""
    assert RunStatus.CREATED.value == "created"
    assert RunStatus.PLAN_READY.value == "plan_ready"
    assert RunStatus.RUNNING.value == "running"


# ---- canonical_status() ------------------------------------------


def test_canonical_status_folds_created_to_received():
    assert canonical_status("created") == "received"
    assert canonical_status(RunStatus.CREATED) == "received"


def test_canonical_status_folds_plan_ready_to_assessment_ready():
    assert canonical_status("plan_ready") == "assessment_ready"
    assert canonical_status(RunStatus.PLAN_READY) == "assessment_ready"


def test_canonical_status_passes_through_canonical_names():
    """Canonical names map to themselves so a caller passing
    `received` or `assessment_ready` gets the same value back."""
    assert canonical_status("received") == "received"
    assert canonical_status("assessment_ready") == "assessment_ready"
    assert canonical_status("compiling") == "compiling"
    assert canonical_status("running") == "running"


def test_canonical_status_passes_through_unknown_values():
    """The helper must not reject unknown strings — the REST filter
    drops typo'd statuses silently and we want the same defensive
    behaviour at the helper boundary."""
    assert canonical_status("not_a_status") == "not_a_status"
    assert canonical_status("") == ""


# ---- status_aliases() --------------------------------------------


def test_status_aliases_expands_canonical_to_include_legacy():
    """REST status filters pass the result through to the JSONL
    store's `statuses=` argument. A `?status=received` query must
    expand to both `received` AND `created` so legacy records match."""
    assert set(status_aliases("received")) == {"received", "created"}
    assert set(status_aliases("assessment_ready")) == {"assessment_ready", "plan_ready"}


def test_status_aliases_expands_legacy_to_include_canonical():
    """Symmetric: querying with the legacy name finds canonical-named
    runs too, so a FE that hasn't migrated still sees new runs."""
    assert set(status_aliases("created")) == {"received", "created"}
    assert set(status_aliases("plan_ready")) == {"assessment_ready", "plan_ready"}


def test_status_aliases_for_status_with_no_legacy_pair():
    """Statuses with no legacy alias return a single-element tuple
    (just themselves)."""
    assert status_aliases("verifying") == ("verifying",)
    assert status_aliases("succeeded") == ("succeeded",)


def test_status_aliases_accepts_run_status_enum_directly():
    assert set(status_aliases(RunStatus.RECEIVED)) == {"received", "created"}
    assert set(status_aliases(RunStatus.FAILED)) == {"failed"}


# ---- Legacy table shape ------------------------------------------


def test_legacy_table_is_keyed_on_legacy_values():
    """Every key in the table is a real RunStatus value — guards
    against typos at construction time. Values must also resolve to
    real RunStatus members."""
    for legacy, canonical in LEGACY_TO_CANONICAL_STATUS.items():
        assert RunStatus(legacy)  # key resolves
        assert RunStatus(canonical)  # value resolves


# ---- Phase 1 failure codes ---------------------------------------


def test_phase_1_failure_codes_are_stable_string_constants():
    """The reason codes are part of the wire/audit contract — pin
    them so a rename is intentional and traceable. Phase 1 adds
    macro-stage failure codes; Phase 2 codes are pinned in
    test_compile_verification.py."""
    assert FAILURE_CODE_ASSESSMENT_FAILED == "ASSESSMENT_FAILED"
    assert FAILURE_CODE_COMPILE_FAILED == "COMPILE_FAILED"
    assert FAILURE_CODE_EMPTY_DOCUMENT == "EMPTY_DOCUMENT"


def test_failure_code_vocabulary_is_disjoint():
    """No two failure codes share a string value — operators filter
    audit logs on these and an overlap would conflate two distinct
    failure modes."""
    codes = {
        FAILURE_CODE_ASSESSMENT_FAILED,
        FAILURE_CODE_CHUNK_FAILED,
        FAILURE_CODE_COMPILE_FAILED,
        FAILURE_CODE_EMPTY_DOCUMENT,
        FAILURE_CODE_INDEX_FAILED,
        FAILURE_CODE_VERIFICATION_FAILED,
    }
    assert len(codes) == 6


# ---- Predicate: is_terminal still excludes non-terminal new states


@pytest.mark.parametrize(
    "non_terminal",
    [
        RunStatus.RECEIVED,
        RunStatus.ASSESSMENT_READY,
        RunStatus.COMPILE_PENDING,
        RunStatus.COMPILING,
        RunStatus.VERIFYING,
    ],
)
def test_new_states_are_not_terminal(non_terminal):
    """The new canonical states (and the Phase-2 intermediate ones)
    are all mid-flight — `is_terminal()` must return False so the
    workflow continues. A regression here would freeze runs."""
    from datetime import datetime, timezone

    from j1.runs.models import IngestionRun

    now = datetime.now(timezone.utc)
    run = IngestionRun(
        run_id="r", document_id="d", workflow_id="w",
        workflow_run_id="wr", status=non_terminal,
        started_at=now, updated_at=now,
    )
    assert run.is_terminal() is False
