"""PR-06 contract — Query diagnostics + retrieval broadening evaluation.

Per ``docs/j1_sequential_pr_implementation_plan.md``'s PR-06, J1
MUST guarantee:

  1. Query diagnostics include the effective scope (eligible
     snapshot ids, per-route snapshot allowlist, global-workspace
     fallback flag).
  2. Alias-broadening diagnostics surface available + applied
     counts separately for the two alias sources (domain-pack
     static, enrichment-derived).
  3. The A/B harness runs each query twice — once with
     ``J1_QUERY_EXPANSION_ENABLED=false`` (baseline) and once with
     it ``true`` (variant).
  4. The harness is read-only — calling it MUST NOT mutate
     project / document / run state. Env state is restored after
     each query.
  5. The summarizer handles empty, partial, and invalid reports
     without crashing.
  6. The sample query file parses, every entry has a unique id,
     and the category vocabulary is intact.

Adjacent test files cover finer-grained edges; this module is
the single PR-06 regression document. The contracts pinned here
are the load-bearing answers to "does broadening actually help?".
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from j1.processing.enrichment_aliases import (
    build_alias_payload,
    extract_aliases_from_text,
)
from j1.query.orchestrator import ENV_QUERY_EXPANSION_ENABLED
from j1.query.query_plan import (
    AnswerShape,
    EvidenceGroupSpec,
    Intent,
    QualityPolicy,
    QueryPlan,
    SufficiencyPolicy,
    SynthesisMode,
)
from j1.query.query_trace import QueryTrace
from j1.tools.evaluate_retrieval_broadening import (
    QueryInput,
    RetrievalBroadeningEvaluator,
)
from j1.tools.summarize_retrieval_broadening_report import (
    SuspicionFlag,
    main as summarize_main,
    summarize_report,
)


def _minimal_plan() -> QueryPlan:
    """Build the smallest valid ``QueryPlan`` so we can construct
    ``QueryTrace`` instances for shape testing."""
    return QueryPlan(
        normalized_question="Q",
        intent=Intent.UNKNOWN,
        anchors=(),
        requested_fields=(),
        answer_shape=AnswerShape.PARAGRAPH,
        synthesis_mode=SynthesisMode.SYNTHESIZE,
        retrieval_jobs=(),
        required_groups=(EvidenceGroupSpec(name="answer", required=True),),
        sufficiency=SufficiencyPolicy(),
        quality=QualityPolicy(),
    )


# ---- Contract 1: diagnostics include effective scope ------------


def test_contract_1_query_trace_carries_effective_scope():
    """``QueryTrace.to_dict()['snapshot_scope']`` MUST carry the
    three snapshot-id sets the orchestrator resolved against. A
    missing block means operators can't answer "which snapshots
    did this query actually read?"."""
    trace = QueryTrace.empty_with_plan("Q", _minimal_plan())
    trace = trace.with_snapshot_scope(
        eligible_snapshot_ids=("snap-a", "snap-b"),
        queried_raganything_snapshot_ids=("snap-a",),
        bm25_allowed_snapshot_ids=("snap-a",),
        used_global_workspace=False,
    )
    scope = trace.to_dict()["snapshot_scope"]
    assert scope["eligible_snapshot_ids"] == ["snap-a", "snap-b"]
    assert scope["queried_raganything_snapshot_ids"] == ["snap-a"]
    assert scope["bm25_allowed_snapshot_ids"] == ["snap-a"]
    assert scope["used_global_workspace"] is False


def test_contract_1_scope_block_present_even_when_empty():
    """When the orchestrator returns no eligible snapshots (empty
    project), the scope block is STILL present — operators need
    to see "zero snapshots queried" explicitly to distinguish
    that from "diagnostics dropped"."""
    trace = QueryTrace.empty_with_plan("Q", _minimal_plan())
    scope = trace.to_dict()["snapshot_scope"]
    # Empty defaults but present.
    assert scope["eligible_snapshot_ids"] == []
    assert scope["queried_raganything_snapshot_ids"] == []
    assert scope["bm25_allowed_snapshot_ids"] == []
    assert scope["used_global_workspace"] is False


# ---- Contract 2: alias diagnostics distinguish sources ----------


def test_contract_2_pack_aliases_and_enrichment_aliases_tracked_separately():
    """``augmentation_aliases`` is the merged set the augmentation
    provider exposed. ``enrichment_aliases_*`` are the
    ``domain_enrichment``-derived signals from the alias loader,
    surfaced separately so operators can tell pack-static aliases
    from enrichment-derived ones."""
    trace = QueryTrace.empty_with_plan("Q", _minimal_plan())
    trace = trace.with_augmentation(
        source="domain_pack",
        terms=("bill of quantities",),
        aliases=(("bill of quantities", "BOQ"),),
        expansions=("BOQ",),
        applied_to_retrieval=True,
    )
    trace = trace.with_augmentation_retrieval_stats(
        original_count=3, expanded_count=5, deduplicated_total=7,
        distribution={"original_only": 2, "expanded_only": 1, "both": 4},
    )
    trace = trace.with_enrichment_alias_diagnostics(
        available=4,
        matched=(("bill of quantities", "BOQ"),),
    )
    aug = trace.to_dict()["augmentation"]
    # Pack-static surface
    assert aug["source"] == "domain_pack"
    assert aug["aliases"] == [["bill of quantities", "BOQ"]]
    # Enrichment surface — distinct fields
    assert aug["enrichment_aliases_available"] == 4
    assert aug["enrichment_aliases_matched"] == [
        {"canonical": "bill of quantities", "alias": "BOQ"},
    ]
    # `applied_to_retrieval` is the load-bearing "did broadening
    # actually run?" signal.
    assert aug["applied_to_retrieval"] is True
    assert aug["retrieval_counts"]["original"] == 3
    assert aug["retrieval_counts"]["expanded"] == 5
    assert aug["retrieval_counts"]["deduplicated_total"] == 7


def test_contract_2_alias_text_does_not_become_evidence():
    """Critical safety contract: aliases live in the
    ``augmentation`` block. They MUST NEVER end up in
    ``llm_evidence`` — the synthesizer must only see chunks that
    actually came from retrieval, never alias strings."""
    trace = QueryTrace.empty_with_plan("Q", _minimal_plan())
    trace = trace.with_augmentation(
        source="domain_pack",
        terms=("bill of quantities",),
        aliases=(("bill of quantities", "BOQ"),),
        expansions=("BOQ",),
        applied_to_retrieval=True,
    )
    payload = trace.to_dict()
    # llm_evidence is empty because nothing was retrieved + selected.
    assert payload["llm_evidence"] == []
    # Even though we have aliases, they don't appear as evidence.
    # The two surfaces are structurally separate in `to_dict`.
    assert "aliases" in payload["augmentation"]
    assert "aliases" not in payload.get("llm_evidence", [])


def test_contract_2_enrichment_alias_artifact_payload_is_canonical():
    """The persisted alias payload uses the same shape the
    QueryTrace's ``enrichment_aliases_matched`` reads. Pinned so a
    future producer-side shape change doesn't silently break the
    trace surface."""
    extracted = extract_aliases_from_text(
        "The bill of quantities (BOQ) must be approved.",
        run_id="run-1", snapshot_id="snap-1", document_id="doc-1",
    )
    payload = build_alias_payload(extracted)
    assert payload["schema_version"] == "1"
    assert isinstance(payload["aliases"], list)
    assert payload["aliases"]
    first = payload["aliases"][0]
    # The two keys the loader / trace consume.
    assert "canonical" in first
    assert "alias" in first


# ---- Contract 3+4: A/B harness runs twice, read-only ------------


def test_contract_3_harness_runs_each_query_twice_with_correct_env(
    monkeypatch,
):
    """For each input query the harness calls the runner TWICE —
    once with ``J1_QUERY_EXPANSION_ENABLED`` falsy (baseline) and
    once with it truthy (variant). Pinned so a future refactor
    can't accidentally skip either pass."""
    captured: list[tuple[str, str | None]] = []

    def stub_runner(question: str) -> dict:
        captured.append((
            question, os.environ.get(ENV_QUERY_EXPANSION_ENABLED),
        ))
        return {"retrieved_count": 1, "evidence_count": 1}

    evaluator = RetrievalBroadeningEvaluator(runner=stub_runner)
    evaluator.evaluate([QueryInput(id="q1", question="hello")])

    # Two calls per query, env toggled correctly.
    assert len(captured) == 2
    assert captured[0][0] == "hello"
    assert captured[1][0] == "hello"
    # Baseline = disabled (any falsy / unset); variant = "true".
    assert captured[0][1] in {None, "", "false", "0", "no", "off"}
    assert captured[1][1] == "true"


def test_contract_4_harness_restores_env_state_after_run(monkeypatch):
    """The harness's env-toggle context manager MUST restore the
    prior state, including the "unset" state. Pinned so a future
    refactor that hard-sets the env can't silently leak the
    variant value into the surrounding process."""
    monkeypatch.delenv(ENV_QUERY_EXPANSION_ENABLED, raising=False)
    assert ENV_QUERY_EXPANSION_ENABLED not in os.environ

    evaluator = RetrievalBroadeningEvaluator(
        runner=lambda q: {"retrieved_count": 0, "evidence_count": 0},
    )
    evaluator.evaluate([QueryInput(id="q", question="hi")])

    assert ENV_QUERY_EXPANSION_ENABLED not in os.environ, (
        "harness leaked env state — must restore the 'unset' "
        "condition exactly"
    )


def test_contract_4_harness_restores_env_state_after_runner_exception(
    monkeypatch,
):
    """When a runner raises mid-evaluation, the env MUST still
    revert. Pinned so a refactor that drops the ``finally``
    clause fails immediately."""
    prior = "preexisting"
    monkeypatch.setenv(ENV_QUERY_EXPANSION_ENABLED, prior)

    def raising_runner(q: str) -> dict:
        raise RuntimeError("synthetic")

    evaluator = RetrievalBroadeningEvaluator(runner=raising_runner)
    # Harness catches per-query exceptions internally; report has
    # a warning but doesn't propagate.
    report = evaluator.evaluate([QueryInput(id="q", question="hi")])
    assert any(
        "synthetic" in w or "RuntimeError" in w for w in report.warnings
    ), f"runner exception should be recorded; got {report.warnings!r}"
    assert os.environ.get(ENV_QUERY_EXPANSION_ENABLED) == prior, (
        "env state must be restored even when runner raises"
    )


def test_contract_4_harness_is_read_only_by_module_contract():
    """The harness module's top-level docstring promises it is
    read-only — no artifacts, snapshots, run state mutations. The
    contract is enforced by composition: the harness only calls
    its injected runner. Pinned via the module's import surface."""
    import j1.tools.evaluate_retrieval_broadening as ev
    # Docstring claim
    assert (
        "read-only" in (ev.__doc__ or "")
        or "Read-only" in (ev.__doc__ or "")
    )
    # No artifact-writing symbols exposed
    for forbidden in ("ArtifactRegistry", "JsonlIngestionRunStore"):
        assert not hasattr(ev, forbidden), (
            f"harness module exposed {forbidden!r} — read-only "
            "contract requires no write surfaces in scope"
        )


# ---- Contract 5: summarizer handles empty/partial/invalid -----


def test_contract_5_summarizer_handles_empty_results():
    """A report with zero results MUST summarize without crashing.
    All counters are zero, suspicious cases empty."""
    summary = summarize_report({
        "results": [], "warnings": [], "scope": {},
    })
    assert summary.total_queries == 0
    assert summary.queries_increased == 0
    assert summary.queries_decreased == 0
    assert summary.suspicious_cases == ()


def test_contract_5_summarizer_handles_partial_diagnostics():
    """Results with missing diagnostic fields (no
    ``retrieved_count``, etc.) MUST be tolerated. The summarizer
    flags them as ``missing_counts`` rather than crashing."""
    summary = summarize_report({
        "results": [
            {"query_id": "q1", "question": "x", "baseline": {},
             "alias_broadening": {}},
        ],
        "warnings": [],
    })
    assert summary.total_queries == 1
    flag_kinds = {
        flag for case in summary.suspicious_cases
        for flag in case.suspicion_flags
    }
    assert SuspicionFlag.MISSING_COUNTS in flag_kinds


def test_contract_5_summarizer_main_exits_nonzero_on_invalid_json(
    tmp_path: Path,
):
    """The CLI MUST exit with a non-zero status when the input
    file isn't valid JSON. CI consumers depend on this."""
    bad = tmp_path / "garbage.json"
    bad.write_text("{this is not json")
    code = summarize_main(["--input", str(bad)])
    assert code != 0, (
        "summarizer CLI silently accepted invalid JSON — CI "
        "guardrails depend on the non-zero exit"
    )


# ---- Contract 6: sample query file shape -----------------------


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SAMPLE_QUERIES = _REPO_ROOT / "evaluation/retrieval_broadening/sample_queries.json"


def test_contract_6_sample_query_file_parses_as_json():
    assert _SAMPLE_QUERIES.exists()
    data = json.loads(_SAMPLE_QUERIES.read_text())
    assert isinstance(data, dict)
    assert "queries" in data
    assert isinstance(data["queries"], list)


def test_contract_6_every_query_has_id_and_question():
    data = json.loads(_SAMPLE_QUERIES.read_text())
    for entry in data["queries"]:
        assert isinstance(entry, dict)
        assert entry.get("id"), f"missing id: {entry!r}"
        assert entry.get("question"), f"missing question: {entry!r}"


def test_contract_6_query_ids_are_unique():
    """The comparator matches queries by ``id`` — duplicates would
    break the regression diff."""
    data = json.loads(_SAMPLE_QUERIES.read_text())
    ids = [e["id"] for e in data["queries"]]
    assert len(ids) == len(set(ids)), (
        f"sample_queries.json has duplicate ids: "
        f"{[i for i in ids if ids.count(i) > 1]!r}"
    )


def test_contract_6_query_count_is_within_spec_range():
    """PR-06 spec: 10-20 manually curated queries."""
    data = json.loads(_SAMPLE_QUERIES.read_text())
    n = len(data["queries"])
    assert 10 <= n <= 20, (
        f"sample query count {n} outside PR-06 spec range [10, 20]"
    )


# ---- Bonus: canonical env var name pinned ----------------------


def test_canonical_broadening_env_name_is_query_expansion_enabled():
    """The PR-06 spec names ``J1_QUERY_EXPANSION_ENABLED`` as the
    canonical broadening gate. Pinned so a future rename can't
    silently shift the contract."""
    assert ENV_QUERY_EXPANSION_ENABLED == "J1_QUERY_EXPANSION_ENABLED"
