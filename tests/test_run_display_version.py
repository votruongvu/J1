"""Tests for ``allocate_display_version``.

The chip the FE shows next to each run for a document is purely
UI metadata — uniqueness is per-document per-day, formatted as
``DDMMYYYY-NN``. These tests pin the allocation rules so the FE
can rely on:

  * the first run of the day is always ``NN=01``;
  * the second run of the day is ``NN=02`` (zero-padded);
  * runs on different days reset the counter;
  * runs on different documents don't interfere;
  * the counter dedupes by ``run_id`` so re-counting the same run
    doesn't allocate a new slot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from j1.runs.models import IngestionRun, RunStatus, allocate_display_version


def _run(
    *, run_id: str, document_id: str, started_at: datetime,
) -> IngestionRun:
    return IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id="wf",
        workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=started_at,
        updated_at=started_at,
    )


def test_first_run_of_day_is_01():
    started = datetime(2026, 5, 13, 9, 0, 0, tzinfo=timezone.utc)
    version = allocate_display_version(
        started_at=started, existing_runs=[], document_id="doc-1",
    )
    assert version == "13052026-01"


def test_second_run_of_day_increments_to_02():
    started_first = datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc)
    started_second = datetime(2026, 5, 13, 14, 30, tzinfo=timezone.utc)
    existing = [
        _run(run_id="r-1", document_id="doc-1", started_at=started_first),
    ]
    version = allocate_display_version(
        started_at=started_second,
        existing_runs=existing,
        document_id="doc-1",
    )
    assert version == "13052026-02"


def test_runs_on_different_days_reset_counter():
    day1 = datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 5, 14, 9, 0, tzinfo=timezone.utc)
    existing = [
        _run(run_id="r-1", document_id="doc-1", started_at=day1),
        _run(run_id="r-2", document_id="doc-1", started_at=day1),
    ]
    # Third run, but starts on a fresh day → 01.
    version = allocate_display_version(
        started_at=day2, existing_runs=existing, document_id="doc-1",
    )
    assert version == "14052026-01"


def test_different_documents_do_not_interfere():
    day = datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc)
    existing = [
        _run(run_id="r-1", document_id="doc-A", started_at=day),
    ]
    version = allocate_display_version(
        started_at=day, existing_runs=existing, document_id="doc-B",
    )
    assert version == "13052026-01"


def test_third_run_of_day_zero_pads_to_02_then_03():
    day = datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc)
    existing = [
        _run(run_id="r-1", document_id="doc-1", started_at=day),
        _run(
            run_id="r-2", document_id="doc-1",
            started_at=day.replace(hour=12),
        ),
    ]
    version = allocate_display_version(
        started_at=day.replace(hour=23),
        existing_runs=existing,
        document_id="doc-1",
    )
    assert version == "13052026-03"


def test_dedupes_by_run_id_when_existing_list_includes_candidate():
    """If the caller passes the candidate run inside ``existing_runs``
    by mistake (or because the store appended a snapshot already),
    the candidate's ``run_id`` is counted ONCE — not twice — so the
    chip doesn't skip a slot."""
    day = datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc)
    candidate_run = _run(
        run_id="r-candidate", document_id="doc-1", started_at=day,
    )
    existing = [
        _run(run_id="r-1", document_id="doc-1", started_at=day),
        candidate_run,
        candidate_run,  # second copy of the same run_id
    ]
    version = allocate_display_version(
        started_at=day, existing_runs=existing, document_id="doc-1",
    )
    # Two unique run_ids in existing list → next slot is 03.
    assert version == "13052026-03"


def test_runs_from_other_timezones_share_utc_date():
    """If two operators kick runs in different timezones but the
    UTC date is the same, both runs share the same date prefix."""
    started_tokyo = datetime(
        2026, 5, 13, 23, 0, 0, tzinfo=timezone.utc,
    )  # 08:00 next day local in JST — but UTC stays 13 May
    existing = [
        _run(
            run_id="r-1",
            document_id="doc-1",
            started_at=datetime(2026, 5, 13, 1, 0, tzinfo=timezone.utc),
        ),
    ]
    version = allocate_display_version(
        started_at=started_tokyo,
        existing_runs=existing,
        document_id="doc-1",
    )
    assert version == "13052026-02"
