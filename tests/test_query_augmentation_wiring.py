"""Phase-4 augmentation wiring tests.

Pins the diagnostics-only contract for the orchestrator + provider:

  * When no provider is wired, augmentation fields stay at their
    empty defaults.
  * When a provider is wired AND a memory_view is supplied, the
    orchestrator captures hints + capped expansions into the trace.
  * ``applied_to_retrieval`` stays ``False`` unless the env flag is
    flipped (deferred work). Retrieval inputs are NOT broadened by
    this PR.
  * A provider that raises must not regress the answer path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import pytest

from j1.domains.models import (
    DomainEnrichmentPolicy,
    DomainExtractionHints,
    DomainPack,
    DomainPromptPack,
    DomainValidationRules,
    EntityAlias,
)
from j1.memory import (
    DocumentMemoryView,
    DomainPackAugmentationProvider,
    MemoryScope,
    QueryableStatus,
)
from j1.query.orchestrator import (
    ENV_QUERY_AUGMENTATION_APPLIED_TO_RETRIEVAL,
    OrchestratorRequest,
    SmartQueryOrchestrator,
)
from j1.query.query_plan import EvidenceCandidate, RetrievalRouteKind
from j1.projects.context import ProjectContext


# ---- Pipeline stubs ----------------------------------------------


class _NoOpRoute:
    def __init__(self, kind: RetrievalRouteKind):
        self.kind = kind

    def execute(self, job, context):
        return []


def _llm(_request) -> str:
    """LLM stub — answer-quality gate refuses thin packs anyway, so
    this is only invoked on a non-empty evidence path. Our tests
    drive empty packs to short-circuit at sufficiency."""
    return "stub answer"


def _build_orchestrator(*, provider=None) -> SmartQueryOrchestrator:
    routes: Mapping[RetrievalRouteKind, _NoOpRoute] = {
        RetrievalRouteKind.RAGANYTHING: _NoOpRoute(
            RetrievalRouteKind.RAGANYTHING,
        ),
    }
    orch = SmartQueryOrchestrator.from_components(
        routes=routes, llm=_llm,
    )
    if provider is not None:
        # The constructor is the canonical wire point. Re-construct
        # with the same internals + the provider so we test the
        # public surface, not a private setter.
        orch = SmartQueryOrchestrator(
            classifier=orch._classifier,
            route_runner=orch._routes,
            builder=orch._builder,
            sufficiency=orch._sufficiency,
            synthesizer=orch._synth,
            binder=orch._binder,
            quality=orch._quality,
            augmentation_provider=provider,
        )
    return orch


def _pack_with_alias() -> DomainPack:
    return DomainPack(
        id="example.test", display_name="Test", version="1",
        extends_document_types=(), keyword_signals=(),
        extraction_targets=(), graph_entity_types=(),
        graph_relationship_types=(), prompt_addon="", overlays={},
        unsupported_capabilities=(),
        enrichment_policy=DomainEnrichmentPolicy(),
        extraction_hints=DomainExtractionHints(
            terminology_hints=("glossary",),
            entity_aliases=(
                EntityAlias(
                    canonical_name="RFI",
                    aliases=("request for information",),
                ),
            ),
        ),
        validation_rules=DomainValidationRules(),
        prompt_pack=DomainPromptPack(),
    )


def _memory_view() -> DocumentMemoryView:
    return DocumentMemoryView(
        scope=MemoryScope.DOCUMENT_ACTIVE,
        project_id="alpha",
        document_id="doc-1",
        snapshot_id="snap-active",
        queryable_status=QueryableStatus.QUERYABLE,
    )


def _request(*, question: str = "Show me every RFI", memory_view=None):
    return OrchestratorRequest(
        ctx=ProjectContext(tenant_id="acme", project_id="alpha"),
        question=question,
        memory_view=memory_view,
    )


# ---- Tests --------------------------------------------------------


def test_orchestrator_without_provider_leaves_augmentation_empty():
    """Backward-compat: an orchestrator constructed without a
    provider produces a trace whose augmentation fields are at
    their empty defaults."""
    orch = _build_orchestrator()
    result = orch.run(_request())
    trace = result.trace
    assert trace.augmentation_source == ""
    assert trace.augmentation_terms == ()
    assert trace.augmentation_aliases == ()
    assert trace.augmentation_expansions == ()
    assert trace.augmentation_applied_to_retrieval is False


def test_orchestrator_with_provider_but_no_memory_view_skips_augmentation():
    """The provider needs the memory view to fan out enrichment-
    derived hints; without one, the orchestrator skips augmentation
    gracefully (no error, no trace pollution)."""
    provider = DomainPackAugmentationProvider(pack=_pack_with_alias())
    orch = _build_orchestrator(provider=provider)
    result = orch.run(_request(memory_view=None))
    assert result.trace.augmentation_source == ""
    assert result.trace.augmentation_expansions == ()


def test_orchestrator_captures_augmentation_diagnostics_into_trace():
    """Happy path: provider + memory_view both present. The
    orchestrator stamps the provider's hints + capped expansion
    into the trace. The expansion list omits the original query
    (callers consume it separately)."""
    provider = DomainPackAugmentationProvider(pack=_pack_with_alias())
    orch = _build_orchestrator(provider=provider)
    result = orch.run(_request(
        question="Tell me about RFI",
        memory_view=_memory_view(),
    ))
    trace = result.trace

    assert trace.augmentation_source == "domain_pack"
    # Pack-shipped terminology + the canonical/alias pair from the
    # entity_aliases tuple.
    assert "glossary" in trace.augmentation_terms
    assert ("RFI", "request for information") in trace.augmentation_aliases
    # Query mentions "RFI" → expansion should include the
    # canonical + the long form (minus the original query).
    forms = set(trace.augmentation_expansions)
    assert "request for information" in forms
    assert "Tell me about RFI" not in forms  # original is NOT in expansions


def test_orchestrator_diagnostics_mode_does_not_apply_to_retrieval(
    monkeypatch,
):
    """Default env: ``applied_to_retrieval`` is always False even
    when expansions are populated. This pins the diagnostics-only
    promise — flipping the broadening on is a separate opt-in."""
    monkeypatch.delenv(
        ENV_QUERY_AUGMENTATION_APPLIED_TO_RETRIEVAL, raising=False,
    )
    provider = DomainPackAugmentationProvider(pack=_pack_with_alias())
    orch = _build_orchestrator(provider=provider)
    result = orch.run(_request(
        question="Tell me about RFI", memory_view=_memory_view(),
    ))
    assert result.trace.augmentation_expansions  # populated
    assert result.trace.augmentation_applied_to_retrieval is False


def test_orchestrator_env_flag_lets_diagnostics_report_applied(monkeypatch):
    """When the deployment flips the broadening flag on, the
    diagnostic field reports ``applied_to_retrieval=True``. This is
    the honest signal; retrieval-side broadening is wired
    separately (deferred). Today, the flag controls what the trace
    REPORTS, so a future retrieval consumer that reads this flag
    can branch consistently."""
    monkeypatch.setenv(
        ENV_QUERY_AUGMENTATION_APPLIED_TO_RETRIEVAL, "true",
    )
    provider = DomainPackAugmentationProvider(pack=_pack_with_alias())
    orch = _build_orchestrator(provider=provider)
    result = orch.run(_request(
        question="Tell me about RFI", memory_view=_memory_view(),
    ))
    assert result.trace.augmentation_applied_to_retrieval is True


def test_orchestrator_swallows_provider_failure():
    """A misconfigured provider must NOT regress the answer path.
    The augmentation step is wrapped in a broad ``except``; the
    trace stays at its empty defaults and the rest of the pipeline
    runs as if no provider were wired."""

    class _BoomProvider:
        def hints_for(self, memory_view, query):
            raise RuntimeError("provider exploded")

    orch = _build_orchestrator(provider=_BoomProvider())
    result = orch.run(_request(memory_view=_memory_view()))
    # No augmentation captured.
    assert result.trace.augmentation_source == ""
    # The pipeline still terminated — sufficiency gate refuses
    # the empty pack but the call did not raise.
    assert result.final_status  # any non-empty status string


def test_trace_to_dict_surfaces_augmentation_section():
    """The wire shape stays stable. The manual-test view renders
    ``trace.to_dict()`` verbatim; pin the new ``augmentation``
    section so a future field addition is intentional."""
    provider = DomainPackAugmentationProvider(pack=_pack_with_alias())
    orch = _build_orchestrator(provider=provider)
    result = orch.run(_request(
        question="Tell me about RFI", memory_view=_memory_view(),
    ))
    doc = result.trace.to_dict()
    assert "augmentation" in doc
    aug = doc["augmentation"]
    assert aug["source"] == "domain_pack"
    assert isinstance(aug["terms"], list)
    assert isinstance(aug["aliases"], list)
    assert isinstance(aug["expansions"], list)
    assert aug["applied_to_retrieval"] is False
