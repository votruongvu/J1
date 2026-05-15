"""PR-01: operator-facing warnings about missing diagnostic fields
on the manual-test query trace.

The builder must:

  * Never raise.
  * Return [] when every expected diagnostic is present.
  * Return one short string per expected-but-absent field.
  * Distinguish "stage didn't run" from "field dropped by
    projection" in the message.
"""

from __future__ import annotations

import pytest

from j1.validation.diagnostic_warnings import build_diagnostic_warnings


def _full_trace() -> dict:
    """Trace dict shape with every expected diagnostic populated.
    Used as the "happy path" fixture every test mutates from."""
    return {
        "question": "What is BOQ?",
        "normalized_question": "what is boq",
        "routes_executed": [{"route": "raganything", "ok": True}],
        "all_candidates": [],
        "selected": [],
        "dropped": [],
        "groups_covered": [],
        "groups_missing": [],
        "llm_evidence": [],
        "answer": "",
        "citations": [],
        "gate_results": [],
        "final_status": "passed",
        "duration_ms": 42,
        "snapshot_scope": {
            "eligible_snapshot_ids": ["snap-active"],
            "queried_raganything_snapshot_ids": ["snap-active"],
            "bm25_allowed_snapshot_ids": [],
            "used_global_workspace": False,
        },
        "augmentation": {
            "source": "domain_pack",
            "terms": ["bill of quantities"],
            "aliases": [["BOQ", "bill of quantities"]],
            "expansions": ["bill of quantities"],
            "applied_to_retrieval": True,
            "retrieval_counts": {
                "original": 5, "expanded": 12, "deduplicated_total": 9,
            },
            "final_evidence_distribution": {
                "original_only": 3, "expanded_only": 4, "both": 2,
            },
            "enrichment_aliases_available": 0,
            "enrichment_aliases_matched": [],
        },
    }


# ---- Happy path -------------------------------------------------


def test_full_trace_produces_no_warnings():
    warnings = build_diagnostic_warnings(_full_trace())
    assert warnings == []


def test_augmentation_disabled_produces_no_warning():
    """Augmentation explicitly disabled is a valid steady state, not
    a missing-diagnostic gap. Operators with the flag off shouldn't
    see noise."""
    trace = _full_trace()
    trace["augmentation"]["source"] = "disabled"
    trace["augmentation"]["terms"] = []
    trace["augmentation"]["aliases"] = []
    trace["augmentation"]["expansions"] = []
    trace["augmentation"]["applied_to_retrieval"] = False
    warnings = build_diagnostic_warnings(trace)
    assert warnings == []


# ---- Absent / empty trace ---------------------------------------


def test_none_trace_emits_one_absent_warning():
    warnings = build_diagnostic_warnings(None)
    assert len(warnings) == 1
    assert "orchestrator_trace" in warnings[0]
    assert "absent" in warnings[0].lower()


def test_empty_dict_emits_one_absent_warning():
    warnings = build_diagnostic_warnings({})
    assert len(warnings) == 1
    assert "orchestrator_trace" in warnings[0]


def test_non_mapping_trace_emits_one_absent_warning():
    """Bad-shaped input — list, string, int — produces the same
    single absent-warning, NEVER raises."""
    for bad in ([], "garbage", 42, 3.14):
        warnings = build_diagnostic_warnings(bad)
        assert len(warnings) == 1
        assert "orchestrator_trace" in warnings[0]


# ---- Top-level required keys ------------------------------------


@pytest.mark.parametrize("missing_key", [
    "snapshot_scope", "augmentation", "routes_executed", "final_status",
])
def test_missing_top_level_key_produces_warning(missing_key: str):
    trace = _full_trace()
    del trace[missing_key]
    warnings = build_diagnostic_warnings(trace)
    [match] = [w for w in warnings if w.startswith(f"{missing_key}:")]
    assert "absent" in match.lower()


@pytest.mark.parametrize("empty_key,empty_value", [
    ("routes_executed", []),
    ("final_status", ""),
])
def test_empty_top_level_key_produces_distinct_warning(
    empty_key: str, empty_value,
):
    """Empty-but-present is a different signal from absent: the
    stage's projection ran but produced nothing. Message must say
    'present but empty'."""
    trace = _full_trace()
    trace[empty_key] = empty_value
    warnings = build_diagnostic_warnings(trace)
    [match] = [w for w in warnings if w.startswith(f"{empty_key}:")]
    assert "present but empty" in match.lower()


# ---- duration_ms ------------------------------------------------


def test_missing_duration_ms_warns():
    trace = _full_trace()
    del trace["duration_ms"]
    warnings = build_diagnostic_warnings(trace)
    assert any("duration_ms" in w and "absent" in w.lower() for w in warnings)


def test_zero_duration_ms_warns_as_unwired():
    """A zero timing means the orchestrator's start/finish seam
    didn't fire — distinct from a sub-millisecond real run, which
    rounds to >0."""
    trace = _full_trace()
    trace["duration_ms"] = 0
    warnings = build_diagnostic_warnings(trace)
    assert any(
        "duration_ms" in w and "non-positive" in w.lower()
        for w in warnings
    )


def test_negative_duration_ms_warns_as_unwired():
    trace = _full_trace()
    trace["duration_ms"] = -1
    warnings = build_diagnostic_warnings(trace)
    assert any("duration_ms" in w for w in warnings)


# ---- snapshot_scope sub-keys ------------------------------------


def test_empty_eligible_snapshot_ids_warns():
    trace = _full_trace()
    trace["snapshot_scope"]["eligible_snapshot_ids"] = []
    warnings = build_diagnostic_warnings(trace)
    assert any(
        "snapshot_scope.eligible_snapshot_ids" in w
        and "empty" in w.lower()
        for w in warnings
    )


def test_missing_eligible_snapshot_ids_warns_as_absent():
    trace = _full_trace()
    del trace["snapshot_scope"]["eligible_snapshot_ids"]
    warnings = build_diagnostic_warnings(trace)
    assert any(
        "snapshot_scope.eligible_snapshot_ids" in w
        and "absent" in w.lower()
        for w in warnings
    )


# ---- augmentation sub-keys --------------------------------------


def test_missing_applied_to_retrieval_warns():
    """`applied_to_retrieval` absence is a wiring-bug signal — the
    provider didn't stamp its decision. Operators MUST see this."""
    trace = _full_trace()
    del trace["augmentation"]["applied_to_retrieval"]
    warnings = build_diagnostic_warnings(trace)
    assert any(
        "augmentation.applied_to_retrieval" in w
        and "absent" in w.lower()
        for w in warnings
    )


def test_augmentation_source_set_but_empty_terms_warns():
    """When the source advertises an enabled provider but no terms
    / aliases / expansions made it through, the provider ran but
    produced nothing — operators should see that distinct state."""
    trace = _full_trace()
    trace["augmentation"]["terms"] = []
    trace["augmentation"]["aliases"] = []
    trace["augmentation"]["expansions"] = []
    warnings = build_diagnostic_warnings(trace)
    assert any(
        "augmentation" in w and "no terms" in w
        for w in warnings
    )


# ---- Compound / mixed-missing ---------------------------------


def test_multiple_missing_fields_produce_one_warning_each():
    """Operators page through the list — each missing field gets
    one entry. No deduplication, no rolling up."""
    trace = _full_trace()
    del trace["augmentation"]
    del trace["routes_executed"]
    trace["duration_ms"] = 0
    warnings = build_diagnostic_warnings(trace)
    keys_warned = {w.split(":")[0] for w in warnings}
    assert "augmentation" in keys_warned
    assert "routes_executed" in keys_warned
    assert "duration_ms" in keys_warned
