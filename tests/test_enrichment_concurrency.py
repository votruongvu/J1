""" tests — enrichment LLM + concurrency controls.

Covers four contract surfaces:

1. `EnrichmentConcurrencySettings` — defaults, env-var parsing,
 dev-mode cap behaviour.
2. `LLMCallLimiter` — concurrency ceiling enforced, bounded retry,
 timeout raises `LLMCallTimeout`, `NonRetryableError`
 short-circuits the retry loop.
3. `select_model_tier` — precedence chain, vision gating, vision
 request on text input falls back to premium.
4. Integration: `_LLMBackedEnricher._produce` and
 `VisualContentDescriber._produce` route LLM calls through the
 limiter when one is configured.
"""

from __future__ import annotations

import threading
import time

import pytest

from j1.enrichers import DocumentClassifier, VisualContentDescriber
from j1.processing.compile_result import (
    DetectedImage,
    NormalizedCompileResult,
)
from j1.processing.enrichment_settings import (
    DEV_SAFE_MAX_CONCURRENT_LLM_CALLS,
    DEV_SAFE_RETRY_LIMIT,
    DEV_SAFE_TIMEOUT_SECONDS,
    ENV_DEFAULT_ENRICHMENT_MODEL_TIER,
    ENV_ENRICHMENT_DEV_MODE_CONSERVATIVE_LIMITS,
    ENV_ENRICHMENT_ENABLED,
    ENV_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS,
    ENV_ENRICHMENT_REQUIRE_SUCCESS,
    ENV_ENRICHMENT_RETRY_LIMIT,
    ENV_ENRICHMENT_TIMEOUT_SECONDS,
    MODEL_TIER_FAST,
    MODEL_TIER_PREMIUM,
    MODEL_TIER_VISION,
    EnrichmentConcurrencySettings,
    load_enrichment_settings,
)
from j1.processing.llm_call_limiter import (
    LLMCallLimiter,
    LLMCallTimeout,
    NonRetryableError,
    build_limiter_from_settings,
)
from j1.processing.model_tier import (
    MODULE_REQUIREMENT_NONE,
    MODULE_REQUIREMENT_TEXT,
    MODULE_REQUIREMENT_VISION,
    select_model_tier,
)
from j1.profiles.model import Profile


# ---- 1. EnrichmentConcurrencySettings -----------------------------


def test_settings_default_values_are_dev_safe():
    s = EnrichmentConcurrencySettings()
    assert s.enabled is True
    assert s.max_concurrent_llm_calls == DEV_SAFE_MAX_CONCURRENT_LLM_CALLS == 1
    assert s.timeout_seconds == DEV_SAFE_TIMEOUT_SECONDS == 120.0
    assert s.retry_limit == DEV_SAFE_RETRY_LIMIT == 1
    assert s.require_enrichment_success is False
    assert s.default_model_tier == MODEL_TIER_FAST


def test_load_returns_defaults_when_env_is_empty():
    s = load_enrichment_settings({})
    assert s == EnrichmentConcurrencySettings()


def test_load_reads_each_env_var():
    s = load_enrichment_settings({
        ENV_ENRICHMENT_ENABLED: "false",
        ENV_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS: "8",
        ENV_ENRICHMENT_TIMEOUT_SECONDS: "300",
        ENV_ENRICHMENT_RETRY_LIMIT: "3",
        ENV_ENRICHMENT_REQUIRE_SUCCESS: "true",
        ENV_DEFAULT_ENRICHMENT_MODEL_TIER: "premium",
        ENV_ENRICHMENT_DEV_MODE_CONSERVATIVE_LIMITS: "false",
    })
    assert s.enabled is False
    assert s.max_concurrent_llm_calls == 8
    assert s.timeout_seconds == 300.0
    assert s.retry_limit == 3
    assert s.require_enrichment_success is True
    assert s.default_model_tier == "premium"


def test_load_dev_mode_caps_concurrency_at_safe_ceiling():
    """Even when operator sets a high value, dev mode (default ON)
 caps the value at the safe ceiling — prevents accidental
 overload of dev/CI machines."""
    s = load_enrichment_settings({
        ENV_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS: "100",
        ENV_ENRICHMENT_TIMEOUT_SECONDS: "9999",
        ENV_ENRICHMENT_RETRY_LIMIT: "50",
    })
    # dev mode default = True (capping happens at load time)
    assert s.max_concurrent_llm_calls == DEV_SAFE_MAX_CONCURRENT_LLM_CALLS
    assert s.timeout_seconds == DEV_SAFE_TIMEOUT_SECONDS
    assert s.retry_limit == DEV_SAFE_RETRY_LIMIT


def test_load_production_mode_releases_cap():
    s = load_enrichment_settings({
        ENV_ENRICHMENT_DEV_MODE_CONSERVATIVE_LIMITS: "false",
        ENV_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS: "16",
        ENV_ENRICHMENT_TIMEOUT_SECONDS: "600",
    })
    assert s.max_concurrent_llm_calls == 16
    assert s.timeout_seconds == 600.0


def test_load_rejects_invalid_values_falls_back_to_defaults():
    s = load_enrichment_settings({
        ENV_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS: "not-a-number",
        ENV_ENRICHMENT_TIMEOUT_SECONDS: "-5",
        ENV_ENRICHMENT_RETRY_LIMIT: "-1",
        ENV_DEFAULT_ENRICHMENT_MODEL_TIER: "ultra_premium",
    })
    # Invalid values fall back to defaults.
    assert s.max_concurrent_llm_calls == DEV_SAFE_MAX_CONCURRENT_LLM_CALLS
    assert s.timeout_seconds == DEV_SAFE_TIMEOUT_SECONDS
    assert s.retry_limit == DEV_SAFE_RETRY_LIMIT
    assert s.default_model_tier == MODEL_TIER_FAST


# ---- 2. LLMCallLimiter --------------------------------------------


def test_limiter_run_returns_callable_result():
    limiter = LLMCallLimiter(
        max_concurrency=1, timeout_seconds=5.0, retry_limit=0,
    )
    assert limiter.run(lambda x: x * 2, 21) == 42


def test_limiter_enforces_max_concurrency():
    """A second concurrent call must wait for the first to release.
 Verify by serialising two threads through a limiter of 1."""
    limiter = LLMCallLimiter(
        max_concurrency=1, timeout_seconds=2.0, retry_limit=0,
    )
    barrier = threading.Event()
    in_progress = []
    completed = []

    def slow_call(label: str) -> str:
        in_progress.append(label)
        # Wait for the test to signal before completing.
        time.sleep(0.05)
        completed.append(label)
        return label

    def worker(label: str, results: list) -> None:
        results.append(limiter.run(slow_call, label))

    r1: list = []
    r2: list = []
    t1 = threading.Thread(target=worker, args=("first", r1))
    t2 = threading.Thread(target=worker, args=("second", r2))
    t1.start()
    t2.start()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)
    barrier.set()
    # The second call should only START after the first completes.
    assert in_progress == ["first", "second"]
    assert completed == ["first", "second"]


def test_limiter_retries_on_transient_failure_up_to_limit():
    """Retry budget is `retry_limit` extra attempts beyond the
 first call."""
    attempts = []

    def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("transient")
        return "OK"

    limiter = LLMCallLimiter(
        max_concurrency=1, timeout_seconds=5.0,
        retry_limit=2, retry_backoff_seconds=0.0,
    )
    result = limiter.run(flaky)
    assert result == "OK"
    assert len(attempts) == 3  # 1 initial + 2 retries


def test_limiter_exhausts_retries_then_raises():
    """When the retry budget is exhausted, the limiter re-raises
 the underlying exception."""
    def always_fail() -> None:
        raise RuntimeError("permanent failure")

    limiter = LLMCallLimiter(
        max_concurrency=1, timeout_seconds=5.0,
        retry_limit=1, retry_backoff_seconds=0.0,
    )
    with pytest.raises(RuntimeError, match="permanent failure"):
        limiter.run(always_fail)


def test_limiter_non_retryable_short_circuits_retry_loop():
    """`NonRetryableError` wrapper tells the limiter to surface
 the underlying cause immediately without retrying."""
    attempts = []

    def fail_with_non_retryable() -> None:
        attempts.append(1)
        cause = ValueError("don't retry me")
        wrapped = NonRetryableError("non-retryable")
        wrapped.__cause__ = cause
        raise wrapped

    limiter = LLMCallLimiter(
        max_concurrency=1, timeout_seconds=5.0, retry_limit=5,
    )
    with pytest.raises(ValueError, match="don't retry"):
        limiter.run(fail_with_non_retryable)
    # Only one attempt — retry loop short-circuited.
    assert len(attempts) == 1


def test_limiter_raises_llm_call_timeout_on_slow_callable():
    def slow_call() -> str:
        time.sleep(2.0)
        return "too slow"

    limiter = LLMCallLimiter(
        max_concurrency=1, timeout_seconds=0.2, retry_limit=0,
    )
    with pytest.raises(LLMCallTimeout):
        limiter.run(slow_call)


def test_limiter_run_with_stats_records_attempts_and_duration():
    attempts = []

    def flaky() -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("transient")
        return "done"

    limiter = LLMCallLimiter(
        max_concurrency=1, timeout_seconds=5.0,
        retry_limit=1, retry_backoff_seconds=0.0,
    )
    result, stats = limiter.run_with_stats(flaky)
    assert result == "done"
    assert stats.attempts == 2
    assert stats.retried is True
    assert stats.duration_ms >= 0


def test_limiter_rejects_invalid_construction():
    with pytest.raises(ValueError, match="max_concurrency"):
        LLMCallLimiter(max_concurrency=0)
    with pytest.raises(ValueError, match="timeout_seconds"):
        LLMCallLimiter(timeout_seconds=0)
    with pytest.raises(ValueError, match="retry_limit"):
        LLMCallLimiter(retry_limit=-1)


def test_build_limiter_from_settings_mirrors_field_values():
    s = EnrichmentConcurrencySettings(
        max_concurrent_llm_calls=3,
        timeout_seconds=45.0,
        retry_limit=2,
    )
    limiter = build_limiter_from_settings(s)
    assert limiter.max_concurrency == 3
    assert limiter.timeout_seconds == 45.0
    assert limiter.retry_limit == 2


# ---- 3. select_model_tier -----------------------------------------


def _settings(default_tier: str = MODEL_TIER_FAST) -> EnrichmentConcurrencySettings:
    return EnrichmentConcurrencySettings(
        default_model_tier=default_tier,
    )


def test_tier_uses_plan_override_when_present():
    decision = select_model_tier(
        settings=_settings(MODEL_TIER_FAST),
        module_requirement=MODULE_REQUIREMENT_TEXT,
        plan_tier_selection=MODEL_TIER_PREMIUM,
    )
    assert decision.selected_tier == MODEL_TIER_PREMIUM
    assert "premium" in decision.reason


def test_tier_uses_domain_default_when_no_plan_override():
    decision = select_model_tier(
        settings=_settings(MODEL_TIER_FAST),
        module_requirement=MODULE_REQUIREMENT_TEXT,
        domain_default_tier=MODEL_TIER_PREMIUM,
    )
    assert decision.selected_tier == MODEL_TIER_PREMIUM


def test_tier_uses_system_default_when_no_other_layer():
    decision = select_model_tier(
        settings=_settings(MODEL_TIER_PREMIUM),
        module_requirement=MODULE_REQUIREMENT_TEXT,
    )
    assert decision.selected_tier == MODEL_TIER_PREMIUM


def test_vision_module_skips_when_no_image_content():
    """A vision module on a doc with no images → skipped decision."""
    cr = NormalizedCompileResult(document_id="d")
    decision = select_model_tier(
        settings=_settings(),
        module_requirement=MODULE_REQUIREMENT_VISION,
        compile_result=cr,
    )
    assert decision.skipped is True
    assert decision.selected_tier is None
    assert "no image content" in decision.reason


def test_vision_module_runs_when_images_present():
    cr = NormalizedCompileResult(
        document_id="d",
        detected_images=(DetectedImage(image_id="img-1"),),
    )
    decision = select_model_tier(
        settings=_settings(),
        module_requirement=MODULE_REQUIREMENT_VISION,
        compile_result=cr,
    )
    assert decision.selected_tier == MODEL_TIER_VISION
    assert decision.skipped is False


def test_vision_module_runs_when_detected_content_includes_images():
    """Image content detected via `detected_content_types` (the
 list-based signal) should also unlock the vision tier even
 when `detected_images` is empty."""
    cr = NormalizedCompileResult(
        document_id="d",
        detected_content_types=("text", "images"),
    )
    decision = select_model_tier(
        settings=_settings(),
        module_requirement=MODULE_REQUIREMENT_VISION,
        compile_result=cr,
    )
    assert decision.selected_tier == MODEL_TIER_VISION


def test_text_module_requesting_vision_falls_back_to_premium():
    """A text module that's been handed a `vision` plan request
 should fall back to premium — vision is for image-input
 modules only."""
    cr = NormalizedCompileResult(
        document_id="d",
        detected_images=(DetectedImage(image_id="img-1"),),
    )
    decision = select_model_tier(
        settings=_settings(),
        module_requirement=MODULE_REQUIREMENT_TEXT,
        plan_tier_selection=MODEL_TIER_VISION,
        compile_result=cr,
    )
    assert decision.selected_tier == MODEL_TIER_PREMIUM
    assert "falling back" in decision.reason.lower()


def test_tier_falls_through_invalid_strings_to_default():
    """Garbage tier strings at every precedence layer fall through
 to system default (then FAST as ultimate backstop)."""
    decision = select_model_tier(
        settings=_settings(MODEL_TIER_PREMIUM),
        module_requirement=MODULE_REQUIREMENT_TEXT,
        plan_tier_selection="not-a-tier",
        domain_default_tier="also-not-a-tier",
    )
    assert decision.selected_tier == MODEL_TIER_PREMIUM


# ---- 4. Enricher integration --------------------------------------


def _profile() -> Profile:
    return Profile(profile_id="test", metadata={}, prompts={})


class _StubTextClient:
    """Mimics the text-client `.extract` contract enough for the
 test to assert the call site routes through the limiter."""

    model = "stub-text"

    def __init__(self) -> None:
        self.calls = []

    def extract(self, prompt, schema, metadata=None):
        self.calls.append((prompt, schema, metadata))
        return ({"classification": "test"}, {"input_tokens": 10})


def test_text_enricher_routes_call_through_limiter():
    """When a limiter is wired, the enricher calls it via
 `limiter.run` instead of directly. Verify by wrapping the
 client + counting limiter invocations."""
    client = _StubTextClient()
    profile = _profile()

    limiter_calls = []
    real_run = LLMCallLimiter(
        max_concurrency=2, timeout_seconds=5.0, retry_limit=0,
    ).run

    class _RecordingLimiter:
        def run(self, fn, *args, **kwargs):
            limiter_calls.append(fn.__name__ if hasattr(fn, "__name__") else str(fn))
            return real_run(fn, *args, **kwargs)

    enricher = DocumentClassifier(
        profile,
        text_client=client,
        content_source=lambda _ctx, _aid: b"some text content",
        llm_call_limiter=_RecordingLimiter(),
    )
    # The enricher reads the artifact bytes + calls extract.
    json_data, _md = enricher._produce(None, "art-1")
    # Limiter recorded the call.
    assert "extract" in limiter_calls
    # Client.extract was actually invoked.
    assert len(client.calls) == 1


def test_text_enricher_falls_back_to_direct_call_when_no_limiter():
    """Backward-compat: an enricher without a wired limiter calls
 the client directly. Pre- deployments don't have to
 wire a limiter to keep working."""
    client = _StubTextClient()
    profile = _profile()
    enricher = DocumentClassifier(
        profile,
        text_client=client,
        content_source=lambda _ctx, _aid: b"some text content",
        # No llm_call_limiter — backward-compat path
    )
    json_data, _md = enricher._produce(None, "art-1")
    assert len(client.calls) == 1


def test_visual_enricher_routes_call_through_limiter():
    """Same wiring check for the vision path."""

    class _StubVisionClient:
        model = "stub-vision"
        provider = "stub"
        def __init__(self) -> None:
            self.calls = []
        def analyze_image(self, content, prompt=None, metadata=None):
            self.calls.append((content, prompt, metadata))
            return ("a caption", {"input_tokens": 5})

    client = _StubVisionClient()
    profile = _profile()
    limiter_calls = []
    real_run = LLMCallLimiter(
        max_concurrency=2, timeout_seconds=5.0, retry_limit=0,
    ).run

    class _RecordingLimiter:
        def run(self, fn, *args, **kwargs):
            limiter_calls.append(fn.__name__ if hasattr(fn, "__name__") else str(fn))
            return real_run(fn, *args, **kwargs)

    enricher = VisualContentDescriber(
        profile,
        vision_client=client,
        content_source=lambda _ctx, _aid: b"fake_image_bytes",
        llm_call_limiter=_RecordingLimiter(),
    )
    enricher._produce(None, "art-1")
    assert "analyze_image" in limiter_calls
    assert len(client.calls) == 1


def test_limiter_timeout_is_caught_as_soft_skip_by_enricher():
    """When the limiter raises `LLMCallTimeout` from a slow vendor
 call, the enricher's outer try/except converts it into a
 soft-skip response (the existing failure handling). Run
 succeeds without the timeout escaping."""
    class _SlowTextClient:
        model = "slow"
        def extract(self, prompt, schema, metadata=None):
            time.sleep(2.0)
            return ({}, {})

    profile = _profile()
    limiter = LLMCallLimiter(
        max_concurrency=1, timeout_seconds=0.1, retry_limit=0,
    )
    enricher = DocumentClassifier(
        profile,
        text_client=_SlowTextClient(),
        content_source=lambda _ctx, _aid: b"text",
        llm_call_limiter=limiter,
    )
    json_data, _md = enricher._produce(None, "art-1")
    # The enricher converts the timeout into a soft-skip; the
    # response has empty extraction data and a `error` flag set
    # via the existing `_error_response` helper.
    assert "art-1" == json_data["source_artifact_id"]


# ---- 5. Compile retry / enrichment retry are separate -------------


def test_enrichment_retry_setting_is_independent_of_compile_retry():
    """Compile retry knobs live on `ProjectProcessingRequest.compile_*`
 fields; enrichment retry lives on `EnrichmentConcurrencySettings.
 retry_limit`. Setting one must NOT affect the other.

 The test is a contract / shape check: the two settings live in
 different modules with different env-var prefixes, and the
 enrichment-settings module never reads `J1_COMPILE_*`."""
    import ast
    import inspect
    from j1.processing import enrichment_settings as mod
    src = inspect.getsource(mod)
    # Scan string-constant identifiers for env-var names.
    tree = ast.parse(src)
    env_keys: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith("J1_"):
                env_keys.append(node.value)
    for key in env_keys:
        assert not key.startswith("J1_COMPILE_"), (
            f"enrichment_settings.py reads compile-retry env "
            f"{key!r}; retry budgets must stay separate."
        )
