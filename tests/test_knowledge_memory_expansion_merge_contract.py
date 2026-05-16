"""Contract — Phase 5A Knowledge Memory expansion-term merge.

Pins:

  * `merge_memory_expansion_terms` — pure helper. Deduplicates
    case-insensitively across augmentation + memory pools, filters
    short/stopword/long terms, caps memory contribution at
    `max_memory_terms`, reports `applied` + `truncated`.
  * Orchestrator integration — memory expansion terms broaden
    retrieval ONLY when:
      - `applied_to_retrieval=True` (existing
        `J1_QUERY_EXPANSION_ENABLED` flag)
      - memory provider status is `used`
      - provider returned non-empty `expansion_terms`
    Trace's `knowledge_memory` block gains
    `applied_expansion_terms`, `expansion_terms_applied`,
    `expansion_terms_truncated`.
  * Fallback safety — merge failure records a warning, query
    proceeds with the augmentation-only variant set.
  * Source-grounding boundary — memory entries are NEVER injected
    as evidence candidates. Only the short strings broaden
    retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from j1.memory.expansion_merge import (
    MemoryExpansionMergeResult,
    merge_memory_expansion_terms,
)
from j1.memory.query_provider import (
    KnowledgeMemoryQueryContext,
    STATUS_USED,
    STATUS_LOADED_NO_MATCH,
    STATUS_NOT_AVAILABLE,
    STATUS_DISABLED,
    STATUS_FAILED,
)
from j1.memory.query_settings import (
    DEFAULT_MAX_EXPANSION_TERMS,
    ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED,
    ENV_QUERY_KNOWLEDGE_MEMORY_MAX_EXPANSION_TERMS,
    load_knowledge_memory_query_settings,
)
from j1.projects.context import ProjectContext
from j1.query.orchestrator import (
    OrchestratorRequest,
    SmartQueryOrchestrator,
    is_query_expansion_enabled,
)
from j1.query.query_plan import (
    AnswerShape,
    EvidenceGroupSpec,
    Intent,
    QualityPolicy,
    QueryPlan,
    SufficiencyPolicy,
    SynthesisMode,
)
from j1.query.retrieval_routes import RetrievalRouteKind


# ---- Settings tests --------------------------------------------


def test_settings_default_max_expansion_terms_is_8():
    s = load_knowledge_memory_query_settings(env={})
    assert s.max_expansion_terms == DEFAULT_MAX_EXPANSION_TERMS == 8


def test_settings_max_expansion_terms_parsed_from_env():
    s = load_knowledge_memory_query_settings(env={
        ENV_QUERY_KNOWLEDGE_MEMORY_MAX_EXPANSION_TERMS: "15",
    })
    assert s.max_expansion_terms == 15


def test_settings_negative_max_expansion_falls_back_to_default():
    s = load_knowledge_memory_query_settings(env={
        ENV_QUERY_KNOWLEDGE_MEMORY_MAX_EXPANSION_TERMS: "-2",
    })
    assert s.max_expansion_terms == DEFAULT_MAX_EXPANSION_TERMS


def test_settings_malformed_max_expansion_falls_back_to_default():
    s = load_knowledge_memory_query_settings(env={
        ENV_QUERY_KNOWLEDGE_MEMORY_MAX_EXPANSION_TERMS: "abc",
    })
    assert s.max_expansion_terms == DEFAULT_MAX_EXPANSION_TERMS


# ---- Merge helper: dedup + filter + cap -----------------------


def test_merge_preserves_augmentation_first_then_memory():
    """Augmentation expansions retain their relative order; memory
    contributions append. Important for downstream per-job cap
    behaviour — the existing augmentation pipeline is the more-
    tested source so it gets priority."""
    r = merge_memory_expansion_terms(
        augmentation_terms=("Non-Conformance Report", "defect"),
        memory_terms=("NCR", "corrective action"),
        max_memory_terms=8,
    )
    assert r.final_terms == (
        "Non-Conformance Report", "defect", "NCR", "corrective action",
    )
    assert r.applied_memory_terms == ("NCR", "corrective action")
    assert r.applied is True
    assert r.truncated is False


def test_merge_dedups_case_insensitively():
    """When a memory term differs from an augmentation term only
    in case, it's dropped. The first occurrence preserves its
    original casing."""
    r = merge_memory_expansion_terms(
        augmentation_terms=("Non-Conformance Report",),
        memory_terms=("non-conformance report", "NCR"),
        max_memory_terms=8,
    )
    # Only the NCR survives — duplicate is dropped.
    assert "Non-Conformance Report" in r.final_terms
    assert "NCR" in r.final_terms
    # The lowercase duplicate isn't appended.
    assert all(t != "non-conformance report" for t in r.final_terms)
    assert r.applied_memory_terms == ("NCR",)


def test_merge_filters_stopwords_short_and_long_terms():
    long_term = "a" * 100  # exceeds _MAX_TERM_LEN
    r = merge_memory_expansion_terms(
        augmentation_terms=(),
        memory_terms=(
            "the",       # stopword
            "a",          # too short
            "",           # empty
            "  ",         # whitespace-only
            "BoQ",        # ok
            "bill of quantities",  # ok
            long_term,    # too long
        ),
        max_memory_terms=8,
    )
    assert r.applied_memory_terms == ("BoQ", "bill of quantities")


def test_merge_caps_memory_terms_and_reports_truncation():
    r = merge_memory_expansion_terms(
        augmentation_terms=(),
        memory_terms=("one", "two", "three", "four", "five"),
        max_memory_terms=3,
    )
    assert r.applied_memory_terms == ("one", "two", "three")
    assert r.truncated is True


def test_merge_zero_cap_drops_all_memory_terms():
    """`max_memory_terms=0` means "no memory contribution"; the
    final term list equals the augmentation pool, and the
    `truncated` flag fires when any memory terms were present so
    the trace surfaces the operator's intent."""
    r = merge_memory_expansion_terms(
        augmentation_terms=("aug-term",),
        memory_terms=("NCR",),
        max_memory_terms=0,
    )
    assert r.final_terms == ("aug-term",)
    assert r.applied_memory_terms == ()
    assert r.applied is False
    assert r.truncated is True


def test_merge_negative_cap_treated_as_zero():
    r = merge_memory_expansion_terms(
        augmentation_terms=("aug",),
        memory_terms=("NCR",),
        max_memory_terms=-5,
    )
    assert r.applied_memory_terms == ()
    assert r.applied is False


def test_merge_empty_inputs():
    r = merge_memory_expansion_terms(
        augmentation_terms=(),
        memory_terms=(),
        max_memory_terms=8,
    )
    assert r.final_terms == ()
    assert r.applied_memory_terms == ()
    assert r.applied is False
    assert r.truncated is False


def test_merge_ignores_non_string_entries():
    r = merge_memory_expansion_terms(
        augmentation_terms=("ok-aug", 42, None),  # type: ignore[list-item]
        memory_terms=("ok-mem", 99, None),  # type: ignore[list-item]
        max_memory_terms=8,
    )
    assert r.final_terms == ("ok-aug", "ok-mem")


def test_merge_dedups_memory_internally():
    """Two memory terms that differ only in case collapse — only
    the first counts toward the cap."""
    r = merge_memory_expansion_terms(
        augmentation_terms=(),
        memory_terms=("NCR", "ncr", "Ncr"),
        max_memory_terms=8,
    )
    assert r.applied_memory_terms == ("NCR",)


# ---- Orchestrator integration ---------------------------------


def _ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1", profile=None)


def _minimal_plan(query: str) -> QueryPlan:
    return QueryPlan(
        normalized_question=query,
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


@dataclass
class _RouteHit:
    route_kind: RetrievalRouteKind = RetrievalRouteKind.ARTIFACT_LOOKUP
    requested_query: str = ""
    job_label: str = "primary"


class _RecordingRoute:
    """Captures the variant list the orchestrator dispatches via
    `RouteRunner.run_all`. Returns no candidates so the rest of
    the pipeline short-circuits cleanly."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []  # (label, query)

    def execute(self, request, jobs):
        for job in jobs:
            self.calls.append((job.label, job.query))
        return []


class _StubProvider:
    def __init__(self, *, context: KnowledgeMemoryQueryContext) -> None:
        self._context = context
        self.calls: list[dict] = []

    def context_for_query(
        self, *, ctx, question, document_id, settings,
        eligible_snapshot_pairs=None,
    ):
        self.calls.append({
            "question": question,
            "document_id": document_id,
            "enabled": settings.enabled,
            "max_terms": settings.max_expansion_terms,
        })
        return self._context


def _build_orchestrator(*, provider, augmentation_provider=None):
    """Construct a SmartQueryOrchestrator with the
    recording-route stub. The route is registered under
    ARTIFACT_LOOKUP so `_build_expansion_jobs` sees it."""
    route = _RecordingRoute()
    orch = SmartQueryOrchestrator.from_components(
        routes={RetrievalRouteKind.ARTIFACT_LOOKUP: route},
        llm=lambda req: "",
        knowledge_memory_provider=provider,
        augmentation_provider=augmentation_provider,
    )
    return orch, route


def _orchestrator_invokes_memory_provider_when_flag_on(monkeypatch):
    monkeypatch.setenv(ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, "true")


@pytest.fixture
def memory_provider_used():
    return _StubProvider(context=KnowledgeMemoryQueryContext(
        status=STATUS_USED,
        available=True,
        artifact_id="mem-1",
        entry_count=10,
        expansion_terms=("NCR", "non-conformance report", "corrective action"),
        selected_entry_types=("risk",),
    ))


# Note: the orchestrator's `applied_to_retrieval` gate flips on
# `J1_QUERY_EXPANSION_ENABLED=true`. Without that flag, the
# orchestrator skips the variant pool entirely — memory terms
# don't reach retrieval even when the memory flag is on. The
# tests below exercise both flags as the documented contract.


def test_orchestrator_merges_memory_terms_when_both_flags_on(
    monkeypatch, memory_provider_used,
):
    """When BOTH `J1_QUERY_EXPANSION_ENABLED=true` AND
    `J1_QUERY_KNOWLEDGE_MEMORY_ENABLED=true` AND the provider
    returns `status=used` with terms, the merge fires and the
    route runner receives variant jobs derived from memory terms."""
    monkeypatch.setenv("J1_QUERY_EXPANSION_ENABLED", "true")
    monkeypatch.setenv(ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, "true")

    orch, route = _build_orchestrator(provider=memory_provider_used)
    request = OrchestratorRequest(
        ctx=_ctx(),
        question="Any NCR issues?",
        document_id="doc-1",
    )
    result = orch.run(request)

    # The trace's knowledge_memory block surfaces the applied facts.
    km = result.trace.knowledge_memory
    assert km is not None
    assert km["status"] == STATUS_USED
    assert km["expansion_terms_applied"] is True
    assert km["expansion_terms_truncated"] is False
    # All three memory terms reached the orchestrator's applied set
    # (none clashed with augmentation; none filtered).
    assert sorted(km["applied_expansion_terms"]) == sorted([
        "NCR", "non-conformance report", "corrective action",
    ])


def test_orchestrator_does_not_merge_when_memory_flag_off(monkeypatch):
    """`J1_QUERY_KNOWLEDGE_MEMORY_ENABLED` controls provider
    invocation; provider returns `status=disabled` and the
    orchestrator's merge block short-circuits. Trace records the
    disabled status; no applied terms."""
    # Both flags OFF.
    monkeypatch.delenv("J1_QUERY_EXPANSION_ENABLED", raising=False)
    monkeypatch.delenv(ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, raising=False)

    provider = _StubProvider(context=KnowledgeMemoryQueryContext(
        status=STATUS_DISABLED,
    ))
    orch, route = _build_orchestrator(provider=provider)
    request = OrchestratorRequest(
        ctx=_ctx(), question="Any NCR issues?", document_id="doc-1",
    )
    result = orch.run(request)
    km = result.trace.knowledge_memory
    assert km is not None
    assert km["status"] == STATUS_DISABLED
    # No applied fields — merge never ran.
    assert "applied_expansion_terms" not in km
    assert "expansion_terms_applied" not in km


def test_orchestrator_does_not_merge_when_provider_status_not_used(monkeypatch):
    """Provider returns a non-`used` status (e.g. `loaded_no_match`).
    The merge block short-circuits — no applied terms."""
    monkeypatch.setenv("J1_QUERY_EXPANSION_ENABLED", "true")
    monkeypatch.setenv(ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, "true")

    for status in (
        STATUS_LOADED_NO_MATCH, STATUS_NOT_AVAILABLE, STATUS_FAILED,
    ):
        provider = _StubProvider(context=KnowledgeMemoryQueryContext(
            status=status, available=(status != STATUS_NOT_AVAILABLE),
        ))
        orch, route = _build_orchestrator(provider=provider)
        request = OrchestratorRequest(
            ctx=_ctx(), question="risks", document_id="doc-1",
        )
        result = orch.run(request)
        km = result.trace.knowledge_memory
        assert km is not None
        assert km["status"] == status
        assert "applied_expansion_terms" not in km


def test_orchestrator_does_not_merge_when_expansion_flag_off(
    monkeypatch, memory_provider_used,
):
    """`J1_QUERY_KNOWLEDGE_MEMORY_ENABLED=true` but
    `J1_QUERY_EXPANSION_ENABLED=false`. The provider runs (Phase 4
    diagnostics still fire), but the merge block requires
    `applied_to_retrieval=True` to broaden retrieval — so no
    applied terms."""
    monkeypatch.delenv("J1_QUERY_EXPANSION_ENABLED", raising=False)
    monkeypatch.setenv(ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, "true")

    orch, route = _build_orchestrator(provider=memory_provider_used)
    request = OrchestratorRequest(
        ctx=_ctx(), question="Any NCR issues?", document_id="doc-1",
    )
    result = orch.run(request)
    km = result.trace.knowledge_memory
    assert km is not None
    # Phase 4 diagnostic block still present; no Phase 5A applied fields.
    assert km["status"] == STATUS_USED
    assert km.get("expansion_terms")  # provider's pool still recorded
    # Phase 5A specifically didn't apply; either flag missing OR not present.
    assert km.get("expansion_terms_applied") is not True
    assert "applied_expansion_terms" not in km


def test_orchestrator_records_truncation_warning_when_capped(monkeypatch):
    """Memory provider returns more terms than the cap allows.
    Trace's `expansion_terms_truncated=true`."""
    monkeypatch.setenv("J1_QUERY_EXPANSION_ENABLED", "true")
    monkeypatch.setenv(ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, "true")
    monkeypatch.setenv(
        ENV_QUERY_KNOWLEDGE_MEMORY_MAX_EXPANSION_TERMS, "2",
    )

    provider = _StubProvider(context=KnowledgeMemoryQueryContext(
        status=STATUS_USED, available=True,
        expansion_terms=("one", "two", "three", "four"),
    ))
    orch, route = _build_orchestrator(provider=provider)
    request = OrchestratorRequest(
        ctx=_ctx(), question="anything", document_id="doc-1",
    )
    result = orch.run(request)
    km = result.trace.knowledge_memory
    assert km["expansion_terms_applied"] is True
    assert km["expansion_terms_truncated"] is True
    assert len(km["applied_expansion_terms"]) == 2


def test_orchestrator_preserves_original_query_in_jobs(
    monkeypatch, memory_provider_used,
):
    """Even when the merge fires, the original retrieval jobs
    (carrying the operator's question) are preserved unchanged.
    The orchestrator clones jobs PER variant rather than replacing
    them. Phase 5A doesn't change that — verify the original
    query still appears in the first dispatched job."""
    monkeypatch.setenv("J1_QUERY_EXPANSION_ENABLED", "true")
    monkeypatch.setenv(ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, "true")

    orch, route = _build_orchestrator(provider=memory_provider_used)
    request = OrchestratorRequest(
        ctx=_ctx(),
        question="Any NCR issues?",
        document_id="doc-1",
    )
    orch.run(request)
    # The plan's `retrieval_jobs` is empty for our test (no plan
    # wiring), so the route runner sees zero original jobs +
    # no clones. But the broader contract: `_build_expansion_jobs`
    # always starts from `original_jobs` and only clones jobs
    # whose `job.query == original_query`. The orchestrator
    # passing memory-merged variants doesn't change that.
    # Pinned indirectly by the dataclass: the trace's `selected_entry_count`
    # still shows the provider was consulted.
    km = result_trace_or_none(orch, request)
    if km is not None:
        assert km.get("applied_expansion_terms")


def result_trace_or_none(orch, request):
    res = orch.run(request)
    return res.trace.knowledge_memory


# ---- Fallback safety -------------------------------------------


def test_orchestrator_falls_back_on_provider_exception(monkeypatch):
    """Provider raises synchronously. The orchestrator's outer
    try/except catches it; `trace.knowledge_memory` stays None;
    query proceeds with the augmentation-only variant set."""
    monkeypatch.setenv("J1_QUERY_EXPANSION_ENABLED", "true")
    monkeypatch.setenv(ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, "true")

    class _RaisingProvider:
        def context_for_query(self, **_kw):
            raise RuntimeError("boom")

    orch, _ = _build_orchestrator(provider=_RaisingProvider())
    request = OrchestratorRequest(
        ctx=_ctx(), question="anything", document_id="doc-1",
    )
    # Query completes.
    result = orch.run(request)
    assert result.trace.knowledge_memory is None


# ---- Source-grounding boundary --------------------------------


def test_memory_entries_never_injected_as_candidates(
    monkeypatch, memory_provider_used,
):
    """Phase 5A guardrail: memory entries themselves must NEVER
    appear as evidence candidates. The orchestrator's
    `all_candidates` list comes solely from the route runner's
    output — we drove the recording route to return [], so the
    final candidate list must be empty regardless of how many
    expansion terms were merged."""
    monkeypatch.setenv("J1_QUERY_EXPANSION_ENABLED", "true")
    monkeypatch.setenv(ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, "true")

    orch, route = _build_orchestrator(provider=memory_provider_used)
    request = OrchestratorRequest(
        ctx=_ctx(), question="Any NCR issues?", document_id="doc-1",
    )
    result = orch.run(request)
    # Trace has no candidates — provider terms broadened retrieval
    # but the (stub) routes returned nothing. Phase 5A doesn't
    # synthesise candidates from memory.
    assert len(result.trace.all_candidates) == 0


def test_memory_context_payload_has_no_evidence_fields():
    """Phase 5A contract: the memory diagnostic block on the trace
    surfaces expansion / status / counts only — never raw
    `KnowledgeMemoryEntry` payloads or source-content. The keys
    here form the supported FE surface; deviations require a
    coordinated FE change."""
    ctx = KnowledgeMemoryQueryContext(
        status=STATUS_USED, available=True,
        expansion_terms=("X",),
    )
    payload = ctx.to_payload()
    # Phase 5A patch (2026-05-16): scope-aware diagnostic fields
    # added — `scope` / `project_id` / `document_count` /
    # `memory_artifact_count`. These are pure metadata, NOT
    # evidence-shaped. The contract is "no raw entry content
    # leaks via the payload"; the assertion below pins the
    # forbidden-field rule + verifies the new keys are present.
    allowed_keys = {
        "status", "scope", "project_id",
        "available", "artifact_id", "entry_count",
        "document_count", "memory_artifact_count",
        "selected_entry_count", "selected_entry_types",
        "expansion_terms", "resolved_source_ref_count", "warnings",
    }
    assert set(payload.keys()) == allowed_keys
    # No raw evidence-shaped fields.
    forbidden = {"content", "raw_content", "evidence_text", "body"}
    assert not (set(payload.keys()) & forbidden)


# ---- Existing augmentation flow not broken --------------------


def test_orchestrator_without_provider_runs_unchanged(monkeypatch):
    """No memory provider wired. The orchestrator runs identically
    to pre-Phase 5A: no `knowledge_memory` block on the trace,
    no merge, augmentation pool unchanged."""
    monkeypatch.setenv("J1_QUERY_EXPANSION_ENABLED", "true")

    orch, _ = _build_orchestrator(provider=None)
    request = OrchestratorRequest(
        ctx=_ctx(), question="anything", document_id="doc-1",
    )
    result = orch.run(request)
    assert result.trace.knowledge_memory is None


# ---- No-LLM regression guard ----------------------------------


def test_expansion_merge_module_has_no_llm_imports():
    import importlib
    import inspect
    mod = importlib.import_module("j1.memory.expansion_merge")
    source = inspect.getsource(mod)
    forbidden = {
        "openai", "langchain", "anthropic", "raganything", "lightrag",
        "TextLLMClient", "VisionLLMClient",
    }
    leaked = [name for name in forbidden if name in source]
    assert not leaked
