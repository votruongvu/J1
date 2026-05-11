"""Unit tests for the Phase-3 macro-stage event derivation
(`derive_macro_event_type` in `j1.runs.reporter`).

The backend keeps emitting flat `step.*` events; the FE projects
them onto canonical macro names (`compile.started`,
`verification.completed`, etc.) client-side via the matching
TypeScript helper in
`frontend/src/pages/run-detail/timeline-grouping.ts`. The two
implementations must agree on the (stage, step, event_type) →
macro mapping so a future server-side switch is a no-op.

These tests pin the mapping table so a rename here is intentional.
"""

from __future__ import annotations

import pytest

from j1.runs.reporter import (
    PROGRESS_EVENT_ASSESS_ENRICHMENT_COMPLETED,
    PROGRESS_EVENT_ASSESS_ENRICHMENT_SKIPPED,
    PROGRESS_EVENT_ASSESS_ENRICHMENT_STARTED,
    PROGRESS_EVENT_COMPILE_COMPLETED,
    PROGRESS_EVENT_COMPILE_FAILED,
    PROGRESS_EVENT_COMPILE_STARTED,
    PROGRESS_EVENT_ENRICH_COMPLETED,
    PROGRESS_EVENT_ENRICH_FAILED,
    PROGRESS_EVENT_ENRICH_SKIPPED,
    PROGRESS_EVENT_ENRICH_STARTED,
    PROGRESS_EVENT_STEP_COMPLETED,
    PROGRESS_EVENT_STEP_FAILED,
    PROGRESS_EVENT_STEP_SKIPPED,
    PROGRESS_EVENT_STEP_STARTED,
    PROGRESS_EVENT_VERIFICATION_COMPLETED,
    PROGRESS_EVENT_VERIFICATION_FAILED,
    PROGRESS_EVENT_VERIFICATION_STARTED,
    derive_macro_event_type,
)


# ---- Canonical event-type vocabulary ----------------------------


def test_macro_event_constants_are_stable_strings():
    """The constants are part of the wire/derived-vocabulary
    contract — the FE's `EVENT_TYPES.COMPILE_STARTED` etc. matches
    these. Pin them so a rename is intentional."""
    assert PROGRESS_EVENT_COMPILE_STARTED == "compile.started"
    assert PROGRESS_EVENT_COMPILE_COMPLETED == "compile.completed"
    assert PROGRESS_EVENT_COMPILE_FAILED == "compile.failed"
    assert PROGRESS_EVENT_VERIFICATION_STARTED == "verification.started"
    assert PROGRESS_EVENT_VERIFICATION_COMPLETED == "verification.completed"
    assert PROGRESS_EVENT_VERIFICATION_FAILED == "verification.failed"
    # Wave 9A — assess_enrichment + enrich macro vocabulary.
    assert PROGRESS_EVENT_ASSESS_ENRICHMENT_STARTED == "assess_enrichment.started"
    assert (
        PROGRESS_EVENT_ASSESS_ENRICHMENT_COMPLETED
        == "assess_enrichment.completed"
    )
    assert PROGRESS_EVENT_ASSESS_ENRICHMENT_SKIPPED == "assess_enrichment.skipped"
    assert PROGRESS_EVENT_ENRICH_STARTED == "enrich.started"
    assert PROGRESS_EVENT_ENRICH_COMPLETED == "enrich.completed"
    assert PROGRESS_EVENT_ENRICH_FAILED == "enrich.failed"
    assert PROGRESS_EVENT_ENRICH_SKIPPED == "enrich.skipped"


# ---- Compile macro stage ----------------------------------------


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        (PROGRESS_EVENT_STEP_STARTED, PROGRESS_EVENT_COMPILE_STARTED),
        (PROGRESS_EVENT_STEP_COMPLETED, PROGRESS_EVENT_COMPILE_COMPLETED),
        (PROGRESS_EVENT_STEP_FAILED, PROGRESS_EVENT_COMPILE_FAILED),
    ],
)
def test_compile_step_events_project_onto_macro_compile(event_type, expected):
    assert derive_macro_event_type("COMPILE", "compile", event_type) == expected


def test_compile_macro_is_case_insensitive_on_stage():
    """Legacy emitters wrote `compile` (lowercase) for the stage
    name. The helper must fold to uppercase before lookup."""
    assert (
        derive_macro_event_type("compile", "compile", PROGRESS_EVENT_STEP_STARTED)
        == PROGRESS_EVENT_COMPILE_STARTED
    )


def test_compile_macro_skipped_event_returns_none():
    """`step.skipped` and `step.progress` aren't part of the
    macro-event vocabulary today — they render as ungrouped sub-step
    rows under the macro header. Returning None signals that."""
    assert derive_macro_event_type("COMPILE", "compile", "step.skipped") is None
    assert derive_macro_event_type("COMPILE", "compile", "step.progress") is None


# ---- Verification macro stage -----------------------------------


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        (PROGRESS_EVENT_STEP_STARTED, PROGRESS_EVENT_VERIFICATION_STARTED),
        (PROGRESS_EVENT_STEP_COMPLETED, PROGRESS_EVENT_VERIFICATION_COMPLETED),
        (PROGRESS_EVENT_STEP_FAILED, PROGRESS_EVENT_VERIFICATION_FAILED),
    ],
)
def test_verify_compile_step_events_project_onto_macro_verification(
    event_type, expected,
):
    assert (
        derive_macro_event_type("VERIFY", "verify_compile", event_type) == expected
    )


# ---- Non-macro stages -------------------------------------------


@pytest.mark.parametrize(
    "stage",
    [
        "GRAPH",
        "INDEX",
        "FINALIZE",
        "ASSESS_COMPILE_STRATEGY",
    ],
)
def test_non_macro_stages_return_none(stage):
    """Stages outside the macro vocabulary return None so the FE
    renders them as ungrouped rows. A future phase that promotes one
    of these to a macro stage adds it to
    `_MACRO_STAGE_EVENT_TABLE`."""
    assert (
        derive_macro_event_type(stage, "any_step", PROGRESS_EVENT_STEP_STARTED)
        is None
    )


# ---- Wave 9A: assess_enrichment macro stage ----------------------


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        (PROGRESS_EVENT_STEP_STARTED, PROGRESS_EVENT_ASSESS_ENRICHMENT_STARTED),
        (
            PROGRESS_EVENT_STEP_COMPLETED,
            PROGRESS_EVENT_ASSESS_ENRICHMENT_COMPLETED,
        ),
        (
            PROGRESS_EVENT_STEP_SKIPPED,
            PROGRESS_EVENT_ASSESS_ENRICHMENT_SKIPPED,
        ),
    ],
)
def test_assess_enrichment_step_events_project_onto_macro(
    event_type, expected,
):
    assert (
        derive_macro_event_type(
            "ASSESS_ENRICHMENT", "assess_enrichment", event_type,
        )
        == expected
    )


def test_assess_enrichment_step_failure_is_not_a_macro_event():
    """The assessor is best-effort + never fails the run; the FE
    treats absence-of-completion as a SKIP, not a failure. So
    `step.failed` under ASSESS_ENRICHMENT doesn't project onto a
    macro event."""
    assert (
        derive_macro_event_type(
            "ASSESS_ENRICHMENT", "assess_enrichment", PROGRESS_EVENT_STEP_FAILED,
        )
        is None
    )


# ---- Wave 9A: enrich macro stage ----------------------------------


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        (PROGRESS_EVENT_STEP_STARTED, PROGRESS_EVENT_ENRICH_STARTED),
        (PROGRESS_EVENT_STEP_COMPLETED, PROGRESS_EVENT_ENRICH_COMPLETED),
        (PROGRESS_EVENT_STEP_FAILED, PROGRESS_EVENT_ENRICH_FAILED),
        (PROGRESS_EVENT_STEP_SKIPPED, PROGRESS_EVENT_ENRICH_SKIPPED),
    ],
)
def test_enrich_step_events_project_onto_macro_enrich(event_type, expected):
    assert (
        derive_macro_event_type("ENRICH", "enrich_stage", event_type) == expected
    )


def test_enrich_macro_is_case_insensitive_on_stage():
    """Same insensitivity as compile — legacy lowercase emitters
    must still match."""
    assert (
        derive_macro_event_type(
            "enrich", "enrich_stage", PROGRESS_EVENT_STEP_STARTED,
        )
        == PROGRESS_EVENT_ENRICH_STARTED
    )


# ---- Edge cases --------------------------------------------------


def test_missing_stage_or_step_returns_none():
    """A reporter that emits with a missing stage/step (legacy
    runs, malformed events) must not raise. None signals "ungrouped"
    so the FE falls back to the flat row layout."""
    assert derive_macro_event_type(None, "compile", PROGRESS_EVENT_STEP_STARTED) is None
    assert derive_macro_event_type("COMPILE", None, PROGRESS_EVENT_STEP_STARTED) is None
    assert derive_macro_event_type(None, None, PROGRESS_EVENT_STEP_STARTED) is None
    assert derive_macro_event_type("", "", PROGRESS_EVENT_STEP_STARTED) is None


def test_unknown_step_within_macro_stage_returns_none():
    """A `step.started` event with `stage=COMPILE` but a `step`
    name that isn't `compile` (a hypothetical sub-step) doesn't
    map onto the macro vocabulary — returns None and the FE renders
    the sub-step under the macro header without re-titling."""
    assert (
        derive_macro_event_type("COMPILE", "compile_attempt_1", PROGRESS_EVENT_STEP_STARTED)
        is None
    )


def test_unknown_event_type_returns_none():
    """An event type outside the macro-vocabulary (step.progress,
    step.warning, etc.) returns None even when stage+step match.
    Only the lifecycle triplet `started/completed/failed` projects
    onto the macro names."""
    assert derive_macro_event_type("COMPILE", "compile", "step.warning") is None
    assert derive_macro_event_type("VERIFY", "verify_compile", "step.progress") is None
