"""Unit tests for the Phase-4 macro-stage projection that backs
the `J1IngestStage` Temporal search attribute.

The workflow writes one of the `INGEST_STAGE_*` macro values into
the search attribute at every stage transition. `_macro_ingest_stage()`
is the pure helper that projects the per-doc workflow operation
string (`compile:doc-1`, `assess_compile_strategy:doc-7`, …) onto
the canonical macro vocabulary so the cardinality stays bounded.

These tests pin the mapping table — a rename of an `INGEST_STAGE_*`
constant or a workflow op string MUST land here too.
"""

from __future__ import annotations

import pytest

from j1.orchestration.workflows.project_processing import (
    INGEST_STAGE_ASSESSING,
    INGEST_STAGE_ASSESSMENT_READY,
    INGEST_STAGE_CANCELLED,
    INGEST_STAGE_COMPILE_PENDING,
    INGEST_STAGE_COMPILING,
    INGEST_STAGE_COMPLETED,
    INGEST_STAGE_FAILED,
    INGEST_STAGE_RECEIVED,
    INGEST_STAGE_RUNNING,
    INGEST_STAGE_STARTING,
    INGEST_STAGE_VERIFYING,
    _macro_ingest_stage,
)


# ---- Canonical vocabulary ----------------------------------------


def test_macro_stage_constants_are_stable_strings():
    """The values are operator-facing — they show up in Temporal
    UI filters and ops dashboards. Pin them so a rename here is
    intentional and traceable."""
    assert INGEST_STAGE_RECEIVED == "received"
    assert INGEST_STAGE_ASSESSING == "assessing"
    assert INGEST_STAGE_ASSESSMENT_READY == "assessment_ready"
    assert INGEST_STAGE_COMPILE_PENDING == "compile_pending"
    assert INGEST_STAGE_COMPILING == "compiling"
    assert INGEST_STAGE_VERIFYING == "verifying"
    assert INGEST_STAGE_RUNNING == "running"
    # Legacy + terminal values retained
    assert INGEST_STAGE_STARTING == "starting"
    assert INGEST_STAGE_COMPLETED == "completed"
    assert INGEST_STAGE_FAILED == "failed"
    assert INGEST_STAGE_CANCELLED == "cancelled"


# ---- Per-op projection -------------------------------------------


@pytest.mark.parametrize(
    ("op", "expected"),
    [
        ("compile", INGEST_STAGE_COMPILING),
        ("compile:doc-1", INGEST_STAGE_COMPILING),
        ("compile:doc-abc-XYZ-123", INGEST_STAGE_COMPILING),
        ("assess_compile_strategy", INGEST_STAGE_ASSESSING),
        ("assess_compile_strategy:doc-1", INGEST_STAGE_ASSESSING),
        ("compile_pending", INGEST_STAGE_COMPILE_PENDING),
        ("compile_pending:doc-1", INGEST_STAGE_COMPILE_PENDING),
        ("verify_compile", INGEST_STAGE_VERIFYING),
        ("verify_compile:doc-1", INGEST_STAGE_VERIFYING),
        ("post_compile_assess", INGEST_STAGE_VERIFYING),
        ("post_compile_assess:doc-1", INGEST_STAGE_VERIFYING),
        ("assess_enrichment", INGEST_STAGE_VERIFYING),
        ("assess_enrichment:doc-1", INGEST_STAGE_VERIFYING),
        ("validate", INGEST_STAGE_RECEIVED),
        ("list_documents", INGEST_STAGE_RECEIVED),
    ],
)
def test_macro_ingest_stage_projects_known_ops(op, expected):
    assert _macro_ingest_stage(op) == expected


# ---- Per-doc suffix stripping ------------------------------------


def test_macro_ingest_stage_strips_document_id_suffix():
    """`compile:doc-1` and `compile:doc-2` collapse onto one value
    so search-attribute cardinality doesn't grow with run count."""
    assert _macro_ingest_stage("compile:doc-1") == _macro_ingest_stage("compile:doc-2")
    assert _macro_ingest_stage("assess_compile_strategy:doc-1") == _macro_ingest_stage(
        "assess_compile_strategy:doc-9",
    )


# ---- Fallback path -----------------------------------------------


@pytest.mark.parametrize(
    "op",
    [
        "enrich",
        "enrich:doc-1",
        "build_graph",
        "build_graph:doc-1",
        "index",
        "finalize",
        "budget_check",
        "review_gate:after_compile",
        "unknown_future_op",
    ],
)
def test_macro_ingest_stage_falls_back_to_running_for_non_macro_ops(op):
    """Ops outside the macro vocabulary (enrich, graph, index,
    finalize, budget gate) fall back to a generic `running` so
    dashboards still see a coarse "something's happening" signal.
    Promoting one of these to its own macro stage = a one-line
    addition to `_OP_TO_MACRO_INGEST_STAGE`."""
    assert _macro_ingest_stage(op) == INGEST_STAGE_RUNNING


def test_macro_ingest_stage_handles_missing_op():
    """The helper is also called from `_begin()` where `op` is the
    current operation — and from edge paths where it may be empty.
    Must not raise; returns the generic `running` value."""
    assert _macro_ingest_stage(None) == INGEST_STAGE_RUNNING
    assert _macro_ingest_stage("") == INGEST_STAGE_RUNNING


# ---- Cardinality bound -------------------------------------------


def test_known_op_table_yields_bounded_macro_vocabulary():
    """The point of Phase 4: search-attribute cardinality should
    not grow with documents or runs. The set of distinct values the
    helper can return is bounded by the macro-vocabulary constants."""
    all_known_ops = [
        "validate", "list_documents",
        "compile", "compile:doc-1", "compile:doc-2", "compile:doc-99",
        "assess_compile_strategy", "assess_compile_strategy:doc-1",
        "compile_pending", "compile_pending:doc-1",
        "verify_compile", "verify_compile:doc-1",
        "post_compile_assess", "post_compile_assess:doc-1",
        "assess_enrichment", "assess_enrichment:doc-1",
        "enrich:doc-1", "build_graph:doc-1", "index", "finalize",
    ]
    values = {_macro_ingest_stage(op) for op in all_known_ops}
    macro_vocabulary = {
        INGEST_STAGE_RECEIVED,
        INGEST_STAGE_ASSESSING,
        INGEST_STAGE_ASSESSMENT_READY,
        INGEST_STAGE_COMPILE_PENDING,
        INGEST_STAGE_COMPILING,
        INGEST_STAGE_VERIFYING,
        INGEST_STAGE_RUNNING,
    }
    assert values.issubset(macro_vocabulary), (
        f"projection produced out-of-vocabulary values: "
        f"{values - macro_vocabulary}"
    )
