"""PR-02: pin the ``FinalIngestionReport.to_dict()`` wire shape so
the FE + downstream consumers don't see silent regressions.

The Phase 2 prompt's example JSON listed these top-level keys:
``run_id``, ``tenant_id``, ``project_id``, ``document_id``,
``snapshot_id``, ``status``, ``assessment``, ``compile``,
``enrichment``, ``aliases``, ``timings``, ``warnings``, ``errors``,
``artifacts``.

PR-02 added the two genuinely-missing concepts:

* ``snapshot_id`` — top-level field; load-bearing for cross-
  referencing the artifact registry.
* ``alias_summary`` — typed block covering the persisted
  ``domain_enrichment_aliases`` artifact.

Naming of the other blocks (``compile_summary`` vs ``compile``,
``stages`` vs ``timings``, etc.) is deliberately kept from the
existing schema — operators and FE code already read those names,
and renaming would be churn for no functional gain. This snapshot
test pins BOTH names + the new ones.
"""

from __future__ import annotations

import pytest

from j1.processing.final_ingestion_report import (
    AliasSummary,
    FINAL_INGESTION_REPORT_SCHEMA_VERSION,
    ReportSourceInputs,
    build_final_ingestion_report,
)


_REQUIRED_TOP_LEVEL_KEYS = (
    "schema_version",
    "run_id",
    "document_id",
    "document_name",
    "tenant_id",
    "project_id",
    "snapshot_id",          # PR-02 — new
    "domain_profile_id",
    "started_at",
    "completed_at",
    "duration_ms",
    "final_status",
    "final_status_reason",
    "stages",
    "compile_summary",
    "enrichment_summary",
    "alias_summary",        # PR-02 — new
    "artifact_refs",
    "warnings",
    "errors",
    "retry_counts",
    "operator_notes",
)


def _minimal_inputs(**overrides) -> ReportSourceInputs:
    base = dict(
        run_id="run-abc",
        document_id="doc-xyz",
        document_name="contract.pdf",
        tenant_id="acme",
        project_id="alpha",
        started_at="2026-05-15T10:00:00+00:00",
        completed_at="2026-05-15T10:05:00+00:00",
        framework_final_status="completed",
        failure_code=None,
        failure_message=None,
    )
    base.update(overrides)
    return ReportSourceInputs(**base)


# ---- Schema version bump ----------------------------------------


def test_schema_version_bumped_for_new_fields():
    """PR-02 introduced new top-level keys; the schema version
    MUST advertise the change so legacy consumers can detect
    shape evolution."""
    assert FINAL_INGESTION_REPORT_SCHEMA_VERSION == "1.1"


# ---- Top-level shape pin ----------------------------------------


def test_to_dict_contains_every_required_top_level_key():
    report = build_final_ingestion_report(_minimal_inputs())
    payload = report.to_dict()
    missing = [k for k in _REQUIRED_TOP_LEVEL_KEYS if k not in payload]
    assert not missing, (
        f"final-ingestion-report wire shape regressed: missing "
        f"keys {missing!r}"
    )


def test_to_dict_does_not_introduce_unexpected_top_level_keys():
    """If a key lands that this test doesn't know about, either the
    pin needs updating or the change wasn't intentional. The list
    is the source of truth."""
    report = build_final_ingestion_report(_minimal_inputs())
    payload = report.to_dict()
    unexpected = sorted(set(payload) - set(_REQUIRED_TOP_LEVEL_KEYS))
    assert not unexpected, (
        f"final-ingestion-report grew unexpected top-level keys: "
        f"{unexpected!r}. Either remove them or add to the pinned "
        "list (and bump the schema version)."
    )


# ---- snapshot_id surfaces from inputs ---------------------------


def test_snapshot_id_threads_from_inputs_to_top_level():
    report = build_final_ingestion_report(
        _minimal_inputs(snapshot_id="snap-active-7"),
    )
    payload = report.to_dict()
    assert payload["snapshot_id"] == "snap-active-7"


def test_snapshot_id_is_none_when_inputs_omit_it():
    """Legacy runs that pre-date snapshot allocation produce a
    report with ``snapshot_id: null`` — preserved so the FE can
    distinguish "missing snapshot id" from "no run found"."""
    report = build_final_ingestion_report(_minimal_inputs())
    payload = report.to_dict()
    assert payload["snapshot_id"] is None


# ---- alias_summary surfaces from artifact -----------------------


def test_alias_summary_empty_when_no_alias_artifact():
    report = build_final_ingestion_report(_minimal_inputs())
    summary = report.to_dict()["alias_summary"]
    assert summary == {
        "persisted": False,
        "alias_count": 0,
        "artifact_id": None,
        "snapshot_id": None,
        "warnings": [],
    }


def test_alias_summary_populated_from_payload():
    """A real alias artifact payload (per ``build_alias_payload``)
    populates the typed summary block."""
    aliases_payload = {
        "schema_version": "1",
        "aliases": [
            {"canonical": "bill of quantities", "alias": "BOQ",
             "confidence": 0.95, "source": "domain_enrichment",
             "evidence": {}},
            {"canonical": "request for information", "alias": "RFI",
             "confidence": 0.95, "source": "domain_enrichment",
             "evidence": {}},
        ],
        "snapshot_id": "snap-active-7",
    }
    report = build_final_ingestion_report(_minimal_inputs(
        enrichment_aliases=aliases_payload,
        enrichment_aliases_artifact_id="alias-abc123",
    ))
    summary = report.to_dict()["alias_summary"]
    assert summary["persisted"] is True
    assert summary["alias_count"] == 2
    assert summary["artifact_id"] == "alias-abc123"
    assert summary["snapshot_id"] == "snap-active-7"


def test_alias_summary_tolerates_malformed_payload():
    """A malformed payload (non-dict, missing ``aliases`` key, list
    in the wrong place) must not raise. Worst case: produce an
    empty summary with ``persisted=True`` so operators see the
    artifact existed but couldn't be projected."""
    report = build_final_ingestion_report(_minimal_inputs(
        enrichment_aliases="garbage",  # type: ignore[arg-type]
        enrichment_aliases_artifact_id="alias-abc123",
    ))
    summary = report.to_dict()["alias_summary"]
    # Non-mapping → falsey → empty summary, artifact_id preserved.
    assert summary["persisted"] is False
    assert summary["artifact_id"] == "alias-abc123"


def test_alias_summary_includes_warnings_from_payload():
    """Persisted alias artifacts may carry their own ``warnings``
    array (e.g. "stoplist rejected 3 candidates"). Surface verbatim
    so operators see them on the final report."""
    aliases_payload = {
        "schema_version": "1",
        "aliases": [{"canonical": "x", "alias": "X", "confidence": 0.5,
                     "source": "domain_enrichment", "evidence": {}}],
        "warnings": ["alias producer skipped 2 candidates"],
    }
    report = build_final_ingestion_report(_minimal_inputs(
        enrichment_aliases=aliases_payload,
        enrichment_aliases_artifact_id="alias-zzz",
    ))
    summary = report.to_dict()["alias_summary"]
    assert summary["warnings"] == ["alias producer skipped 2 candidates"]


# ---- Builder is still pure (no I/O) -----------------------------


def test_builder_remains_pure_with_alias_payload(tmp_path):
    """Adding the alias-summary branch must not introduce I/O.
    Same inputs MUST produce identical output across calls."""
    inputs = _minimal_inputs(
        snapshot_id="snap-1",
        enrichment_aliases={
            "schema_version": "1",
            "aliases": [{"canonical": "x", "alias": "X", "confidence": 0.5,
                         "source": "domain_enrichment", "evidence": {}}],
        },
        enrichment_aliases_artifact_id="alias-1",
    )
    first = build_final_ingestion_report(inputs).to_dict()
    second = build_final_ingestion_report(inputs).to_dict()
    assert first == second


# ---- AliasSummary defaults --------------------------------------


def test_alias_summary_dataclass_defaults():
    summary = AliasSummary()
    assert summary.persisted is False
    assert summary.alias_count == 0
    assert summary.artifact_id is None
    assert summary.snapshot_id is None
    assert summary.warnings == ()
    assert summary.to_dict() == {
        "persisted": False,
        "alias_count": 0,
        "artifact_id": None,
        "snapshot_id": None,
        "warnings": [],
    }
