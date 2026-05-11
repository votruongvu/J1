"""Wave 7.5 — bootstrap wiring + require_enrichment_success
precedence tests.

Pins the integration contract that Wave-7's `LLMCallLimiter` +
`EnrichmentConcurrencySettings` actually reach the production
composition path, plus the new `require_enrichment_success`
fallback resolver.
"""

from __future__ import annotations

import pytest

from j1.compose.bootstrap import (
    BootstrapResult,
    Bootstrap,
    bootstrap_from_env,
    build_composite_enricher_from_bootstrap,
)
from j1.domains.models import DomainEnrichmentPolicy
from j1.enrichers import (
    CompositeEnricher,
    DocumentClassifier,
    VisualContentDescriber,
)
from j1.processing.enrichment_modules import (
    MetadataEnrichmentModule,
    TerminologyEnrichmentModule,
    ValidationEnrichmentModule,
)
from j1.processing.enrichment_overlay import EnrichmentModule
from j1.processing.enrichment_policy import (
    REQUIRE_SUCCESS_SOURCE_DOMAIN,
    REQUIRE_SUCCESS_SOURCE_ENV,
    REQUIRE_SUCCESS_SOURCE_PROJECT,
    REQUIRE_SUCCESS_SOURCE_REQUEST,
    REQUIRE_SUCCESS_SOURCE_SYSTEM_DEFAULT,
    ResolvedRequireSuccess,
    SYSTEM_DEFAULT_REQUIRE_SUCCESS,
    resolve_require_enrichment_success,
)
from j1.processing.enrichment_settings import (
    ENV_ENRICHMENT_DEV_MODE_CONSERVATIVE_LIMITS,
    ENV_ENRICHMENT_ENABLED,
    ENV_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS,
    EnrichmentConcurrencySettings,
)
from j1.processing.llm_call_limiter import LLMCallLimiter
from j1.profiles.model import Profile


# ---- Bootstrap env fixture ---------------------------------------


_BOOTSTRAP_ENV_BASE = {
    "J1_DEFAULT_COMPILER": "mock",
    "J1_DEFAULT_GRAPH": "mock",
    "J1_DEFAULT_RETRIEVAL": "mock",
    "J1_TEXT_LLM_PROVIDER": "openai_compat",
    "J1_TEXT_LLM_BASE_URL": "http://example.com",
    "J1_TEXT_LLM_API_KEY": "k",
    "J1_TEXT_LLM_MODEL": "m",
    "J1_VISION_LLM_PROVIDER": "openai_compat",
    "J1_VISION_LLM_BASE_URL": "http://example.com",
    "J1_VISION_LLM_API_KEY": "k",
    "J1_VISION_LLM_MODEL": "m",
    "J1_EMBEDDING_PROVIDER": "openai_compat",
    "J1_EMBEDDING_BASE_URL": "http://example.com",
    "J1_EMBEDDING_API_KEY": "k",
    "J1_EMBEDDING_MODEL": "m",
    "J1_EMBEDDING_DIMENSION": "1536",
}


def _bootstrap(env_extra: dict | None = None) -> BootstrapResult:
    env = dict(_BOOTSTRAP_ENV_BASE)
    if env_extra:
        env.update(env_extra)
    return bootstrap_from_env(env)


# ---- 1. Bootstrap surfaces a limiter ----------------------------


def test_bootstrap_result_carries_an_llm_call_limiter():
    result = _bootstrap()
    assert isinstance(result.llm_call_limiter, LLMCallLimiter)


def test_bootstrap_result_carries_concurrency_settings():
    result = _bootstrap()
    assert isinstance(
        result.enrichment_concurrency_settings,
        EnrichmentConcurrencySettings,
    )


def test_bootstrap_limiter_matches_settings():
    """The limiter's knobs mirror the loaded settings."""
    result = _bootstrap()
    settings = result.enrichment_concurrency_settings
    limiter = result.llm_call_limiter
    assert limiter.max_concurrency == settings.max_concurrent_llm_calls
    assert limiter.timeout_seconds == settings.timeout_seconds
    assert limiter.retry_limit == settings.retry_limit


def test_bootstrap_limiter_disabled_returns_none():
    """Operators who explicitly disable enrichment shouldn't get
    a limiter — the field is None and downstream code skips wiring."""
    result = _bootstrap({ENV_ENRICHMENT_ENABLED: "false"})
    assert result.llm_call_limiter is None


def test_bootstrap_dev_mode_caps_limiter_concurrency():
    """Even when env asks for 50, dev mode caps the limiter at 1."""
    result = _bootstrap({
        ENV_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS: "50",
        # Default = dev mode on
    })
    assert result.llm_call_limiter.max_concurrency == 1


def test_bootstrap_production_mode_releases_cap():
    result = _bootstrap({
        ENV_ENRICHMENT_DEV_MODE_CONSERVATIVE_LIMITS: "false",
        ENV_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS: "8",
    })
    assert result.llm_call_limiter.max_concurrency == 8


# ---- 2. CompositeEnricher receives + shares the limiter ----------


def test_composite_enricher_helper_threads_limiter_through_every_child():
    result = _bootstrap()
    profile = Profile(profile_id="test", metadata={}, prompts={})
    composite = build_composite_enricher_from_bootstrap(
        result,
        profile=profile,
        text_client=object(),
        vision_client=object(),
    )
    # Composite itself records the limiter for inspection.
    assert composite._llm_call_limiter is result.llm_call_limiter
    # Every LLM-capable child sees the SAME limiter instance.
    children_with_limiter = [
        c for c in composite._enrichers
        if hasattr(c, "_llm_call_limiter")
    ]
    assert children_with_limiter, "expected at least one LLM-capable child"
    for child in children_with_limiter:
        assert child._llm_call_limiter is result.llm_call_limiter, (
            f"{child.kind} did not receive the shared limiter"
        )


def test_composite_enricher_helper_works_when_limiter_disabled():
    """When bootstrap says enrichment is disabled, the helper still
    builds a composite — just without a limiter. Legacy back-compat
    behaviour kicks in (direct LLM calls)."""
    result = _bootstrap({ENV_ENRICHMENT_ENABLED: "false"})
    profile = Profile(profile_id="test", metadata={}, prompts={})
    composite = build_composite_enricher_from_bootstrap(
        result, profile=profile, text_client=object(), vision_client=object(),
    )
    assert composite._llm_call_limiter is None
    for child in composite._enrichers:
        assert getattr(child, "_llm_call_limiter", None) is None


def test_composite_default_factory_still_works_without_limiter():
    """Direct `CompositeEnricher.from_default` (used by tests +
    legacy callers) keeps working without a limiter — no Wave-7.5
    regression for code that wasn't wired through bootstrap."""
    profile = Profile(profile_id="test", metadata={}, prompts={})
    composite = CompositeEnricher.from_default(
        profile, text_client=object(), vision_client=object(),
    )
    assert composite._llm_call_limiter is None


# ---- 3. Skeleton modules don't require the limiter ---------------


@pytest.mark.parametrize(
    "module_class",
    [
        MetadataEnrichmentModule,
        TerminologyEnrichmentModule,
        ValidationEnrichmentModule,
    ],
)
def test_skeleton_modules_construct_without_limiter(module_class):
    """Wave-6 skeletons don't call LLMs — they MUST construct +
    operate without a limiter being passed in. The bootstrap layer
    only threads the limiter into LLM-capable enrichers."""
    instance = module_class()
    assert isinstance(instance, EnrichmentModule)
    # No `_llm_call_limiter` attribute expected — skeletons don't
    # accept it.
    assert not hasattr(instance, "_llm_call_limiter") or (
        instance._llm_call_limiter is None
    )


# ---- 4. require_enrichment_success precedence -------------------


def test_require_success_request_override_wins():
    """Operator's per-run choice beats every lower layer."""
    resolved = resolve_require_enrichment_success(
        request_override=True,
        project_default=False,
        domain_policy=DomainEnrichmentPolicy(require_enrichment_success=False),
        env_default=False,
        system_default=False,
    )
    assert resolved.require_enrichment_success is True
    assert resolved.source == REQUIRE_SUCCESS_SOURCE_REQUEST


def test_require_success_request_override_false_explicit():
    """Explicit `False` from request override still counts as
    opinion — not the same as `None` which falls through."""
    resolved = resolve_require_enrichment_success(
        request_override=False,
        domain_policy=DomainEnrichmentPolicy(require_enrichment_success=True),
    )
    assert resolved.require_enrichment_success is False
    assert resolved.source == REQUIRE_SUCCESS_SOURCE_REQUEST


def test_require_success_project_default_wins_when_no_request():
    resolved = resolve_require_enrichment_success(
        project_default=True,
        domain_policy=DomainEnrichmentPolicy(require_enrichment_success=False),
        env_default=False,
    )
    assert resolved.require_enrichment_success is True
    assert resolved.source == REQUIRE_SUCCESS_SOURCE_PROJECT


def test_require_success_domain_pack_opinion_wins_over_env():
    """A pack with require=True OR non-auto policy wins over env."""
    resolved = resolve_require_enrichment_success(
        domain_policy=DomainEnrichmentPolicy(require_enrichment_success=True),
        env_default=False,
    )
    assert resolved.require_enrichment_success is True
    assert resolved.source == REQUIRE_SUCCESS_SOURCE_DOMAIN


def test_require_success_domain_pack_with_non_auto_policy_counts_as_opinion():
    """Pack with policy=always but require=False is still
    expressing an opinion via the policy literal — falls through
    to its require=False value, not env."""
    resolved = resolve_require_enrichment_success(
        domain_policy=DomainEnrichmentPolicy(
            policy="always", require_enrichment_success=False,
        ),
        env_default=True,
    )
    assert resolved.require_enrichment_success is False
    assert resolved.source == REQUIRE_SUCCESS_SOURCE_DOMAIN


def test_require_success_falls_through_to_env_when_pack_is_noop():
    """A pack with default (policy=auto, require=False) has no
    opinion — env_default takes over so deployments can set a
    fleet-wide default without modifying every pack."""
    resolved = resolve_require_enrichment_success(
        domain_policy=DomainEnrichmentPolicy(),  # default no-op
        env_default=True,
    )
    assert resolved.require_enrichment_success is True
    assert resolved.source == REQUIRE_SUCCESS_SOURCE_ENV


def test_require_success_falls_through_to_system_default_when_nothing_set():
    """No layer expresses an opinion → system default (False)."""
    resolved = resolve_require_enrichment_success()
    assert resolved.require_enrichment_success is SYSTEM_DEFAULT_REQUIRE_SUCCESS == False
    assert resolved.source == REQUIRE_SUCCESS_SOURCE_SYSTEM_DEFAULT


def test_require_success_resolved_to_dict_carries_source():
    resolved = resolve_require_enrichment_success(
        domain_policy=DomainEnrichmentPolicy(require_enrichment_success=True),
    )
    d = resolved.to_dict()
    assert d == {"require_enrichment_success": True, "source": "domain"}


# ---- 5. Legacy regression checks -------------------------------


def test_bootstrap_module_has_no_split_mode_strings():
    """Wave 7.5's bootstrap additions must not reintroduce
    split-mode vocabulary."""
    import inspect
    from j1.compose import bootstrap
    src = inspect.getsource(bootstrap)
    for forbidden in ("split_mode", "SplitMode", "insert_content"):
        assert forbidden not in src


def test_enrichment_policy_module_has_no_pre_compile_gating():
    """The policy resolver must not reach back into pre-compile
    graph/index gating vocabulary."""
    import inspect
    from j1.processing import enrichment_policy
    src = inspect.getsource(enrichment_policy)
    for forbidden in (
        "graph_required", "index_required", "pre_compile_gating",
    ):
        assert forbidden not in src


# ---- 6. Per-tier deferral is documented ------------------------


def test_enrichment_settings_docstring_documents_one_shared_semaphore():
    """The module-level docstring explicitly explains why per-tier
    semaphores are deferred. A regression-check that the comment
    survives future edits."""
    from j1.processing import enrichment_settings
    docstring = enrichment_settings.__doc__ or ""
    assert "shared semaphore" in docstring.lower() or "one shared" in docstring.lower()
    assert "per-tier" in docstring.lower() or "deferred" in docstring.lower()
