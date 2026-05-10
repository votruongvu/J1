"""Tests for the LLM startup connectivity probe.

Locks two contracts:

  1. The probe exercises EVERY registered TEXT / FAST / EMBEDDING
     role with a minimal request and reports per-role results.
  2. `assert_required_llm_reachable` raises with an operator-readable
     message when any probed role fails — the worker / api startup
     hooks aborting on this raise is what gives us the fail-fast
     behaviour.
"""

from __future__ import annotations

import threading

import pytest

from j1.llm.clients import LLMUsage
from j1.llm.errors import LLMProviderUnavailable
from j1.llm.probe import (
    LLMStartupProbeError,
    ProbeResult,
    assert_required_llm_reachable,
    cache_probe_results,
    current_health,
    llm_health_monitor_interval,
    llm_probe_enabled,
    probe_registry,
    start_health_monitor,
    stop_health_monitor,
)
from j1.llm.registry import (
    LLM_ROLE_EMBEDDING,
    LLM_ROLE_FAST,
    LLM_ROLE_TEXT,
    LLMProviderRegistry,
)


class _OkText:
    provider = "openai_compat"
    model = "fake-model"

    def __init__(self):
        self.calls: list[dict] = []

    def generate(self, prompt, *, max_output_tokens=None, temperature=None, **_):
        self.calls.append({
            "prompt": prompt,
            "max_output_tokens": max_output_tokens,
            "temperature": temperature,
        })
        return ("pong", LLMUsage(provider=self.provider, model=self.model,
                                 input_tokens=1, output_tokens=1, total_tokens=2))


class _FailingText:
    provider = "openai_compat"
    model = "down-model"

    def generate(self, prompt, **_):
        raise LLMProviderUnavailable("connection refused: 192.168.1.85:1234")


class _OkEmbedding:
    provider = "openai_compat"
    model = "embed-model"

    def __init__(self):
        self.calls: list[str] = []

    def embed_text(self, text):
        self.calls.append(text)
        return ([0.1, 0.2, 0.3], LLMUsage(provider=self.provider,
                                          model=self.model,
                                          input_tokens=1, output_tokens=0,
                                          total_tokens=1))


class _FailingEmbedding:
    provider = "openai_compat"
    model = "embed-down"

    def embed_text(self, text):
        raise LLMProviderUnavailable("HTTP 503: model not loaded")


# ---- llm_probe_enabled --------------------------------------------------


def test_llm_probe_enabled_default_true():
    assert llm_probe_enabled(env={}) is True


@pytest.mark.parametrize("value", ["false", "False", "0", "no", "off"])
def test_llm_probe_enabled_respects_falsy_overrides(value):
    assert llm_probe_enabled(env={"J1_LLM_STARTUP_PROBE": value}) is False


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "anything-else"])
def test_llm_probe_enabled_respects_truthy_overrides(value):
    assert llm_probe_enabled(env={"J1_LLM_STARTUP_PROBE": value}) is True


# ---- probe_registry shape ----------------------------------------------


def test_probe_registry_returns_one_result_per_registered_role():
    """Three configured roles → three ProbeResults with provider/model
    populated. Roles that aren't registered are silently skipped (the
    operator opted not to configure them)."""
    registry = LLMProviderRegistry()
    registry.register(LLM_ROLE_TEXT, _OkText())
    registry.register(LLM_ROLE_EMBEDDING, _OkEmbedding())

    results = probe_registry(registry)

    assert len(results) == 2
    assert all(isinstance(r, ProbeResult) for r in results)
    assert {r.role for r in results} == {LLM_ROLE_TEXT, LLM_ROLE_EMBEDDING}
    assert all(r.ok for r in results)
    assert all(r.provider == "openai_compat" for r in results)


def test_probe_registry_calls_text_generate_with_small_token_cap():
    """The text probe uses a small but non-trivial output cap (≤16
    tokens). With max_tokens=1 most chat models emit a single
    special token and return EMPTY visible content with
    finish_reason=length, which trips the openai_compat client's
    'empty content' warning on every probe tick — pure log noise
    on a healthy LLM. A few tokens is still negligible cost but
    leaves room for a real one-word response.

    `temperature=0.0` keeps the probe reproducible — same response
    shape on every check."""
    text = _OkText()
    registry = LLMProviderRegistry()
    registry.register(LLM_ROLE_TEXT, text)

    probe_registry(registry)

    assert len(text.calls) == 1
    cap = text.calls[0]["max_output_tokens"]
    assert cap is not None
    assert 2 <= cap <= 16, f"probe cap {cap} should be small but not 1"
    assert text.calls[0]["temperature"] == 0.0


def test_probe_registry_calls_embedding_embed_text():
    embed = _OkEmbedding()
    registry = LLMProviderRegistry()
    registry.register(LLM_ROLE_EMBEDDING, embed)

    probe_registry(registry)

    assert embed.calls == ["a"]


def test_probe_registry_marks_failing_role_as_not_ok():
    registry = LLMProviderRegistry()
    registry.register(LLM_ROLE_TEXT, _OkText())
    registry.register(LLM_ROLE_FAST, _FailingText())

    results = probe_registry(registry)

    by_role = {r.role: r for r in results}
    assert by_role[LLM_ROLE_TEXT].ok is True
    assert by_role[LLM_ROLE_FAST].ok is False
    assert "connection refused" in by_role[LLM_ROLE_FAST].error
    assert "LLMProviderUnavailable" in by_role[LLM_ROLE_FAST].error


def test_probe_registry_empty_when_no_roles_configured():
    """No probed roles registered → no results. Caller's higher-level
    `assert_required_llm_reachable` then logs 'no probed roles
    registered; skipping' rather than raising."""
    registry = LLMProviderRegistry()
    assert probe_registry(registry) == []


# ---- assert_required_llm_reachable -------------------------------------


def test_assert_succeeds_when_all_roles_reachable():
    registry = LLMProviderRegistry()
    registry.register(LLM_ROLE_TEXT, _OkText())
    registry.register(LLM_ROLE_EMBEDDING, _OkEmbedding())
    # Must not raise.
    assert_required_llm_reachable(registry)


def test_assert_succeeds_when_no_probed_roles_registered():
    """An empty registry isn't a misconfiguration — `bootstrap_from_env`
    constructs an empty registry when no LLM is configured at all
    (mock-only deployments). Don't fail startup on that case."""
    registry = LLMProviderRegistry()
    assert_required_llm_reachable(registry)


def test_assert_raises_with_actionable_message_on_failure():
    registry = LLMProviderRegistry()
    registry.register(LLM_ROLE_TEXT, _FailingText())

    with pytest.raises(LLMStartupProbeError) as excinfo:
        assert_required_llm_reachable(registry)

    msg = str(excinfo.value)
    # Operator-facing: explains WHAT failed + WHERE to look + that
    # the bypass exists for tests.
    assert "cannot start" in msg
    assert LLM_ROLE_TEXT in msg
    assert "openai_compat" in msg
    assert "down-model" in msg
    assert "connection refused" in msg
    assert "J1_LLM_STARTUP_PROBE" in msg


def test_assert_raises_when_any_role_fails_even_if_others_pass():
    """One bad role poisons the well — startup must abort even when
    the other roles are healthy. We can't ingest documents with a
    half-functional LLM stack."""
    registry = LLMProviderRegistry()
    registry.register(LLM_ROLE_TEXT, _OkText())
    registry.register(LLM_ROLE_EMBEDDING, _FailingEmbedding())

    with pytest.raises(LLMStartupProbeError) as excinfo:
        assert_required_llm_reachable(registry)

    msg = str(excinfo.value)
    # Only the failing role gets named in the failures list (the
    # passing one is summarised in the INFO log lines, not the
    # raised message — keeps the error focused on what to fix).
    assert LLM_ROLE_EMBEDDING in msg
    assert "model not loaded" in msg


# ---- Cache + current_health snapshot (FE banner data source) -----


def test_current_health_returns_healthy_when_no_probe_run_yet():
    """Conservative default: when the probe hasn't run (probe disabled
    or process startup hook skipped), the cached snapshot reports
    healthy=True. Matches every other health check in the stack —
    'assume working until proven otherwise' so a missing probe
    doesn't trigger a false-alarm banner."""
    cache_probe_results([])  # reset
    snapshot = current_health()
    assert snapshot.healthy is True
    assert snapshot.results == ()


def test_current_health_reflects_cached_results_after_probe():
    """Calling `cache_probe_results` makes the next `current_health`
    return the cached results. The API's `/healthz/llm` endpoint
    reads through this cache so polling can't burn the upstream LLM."""
    cache_probe_results([
        ProbeResult(
            role=LLM_ROLE_TEXT, ok=True,
            provider="openai_compat", model="m-1",
        ),
        ProbeResult(
            role=LLM_ROLE_EMBEDDING, ok=False,
            provider="openai_compat", model="m-2",
            error="LLMProviderUnavailable: HTTP 503",
        ),
    ])

    snapshot = current_health()
    assert snapshot.healthy is False
    assert snapshot.checked_at is not None
    by_role = {r.role: r for r in snapshot.results}
    assert by_role[LLM_ROLE_TEXT].ok is True
    assert by_role[LLM_ROLE_EMBEDDING].ok is False
    assert "503" in (by_role[LLM_ROLE_EMBEDDING].error or "")


def test_current_health_healthy_true_when_all_cached_roles_ok():
    cache_probe_results([
        ProbeResult(role=LLM_ROLE_TEXT, ok=True, provider="p", model="m"),
        ProbeResult(role=LLM_ROLE_EMBEDDING, ok=True, provider="p", model="m"),
    ])

    snapshot = current_health()
    assert snapshot.healthy is True


def test_cache_probe_results_overwrites_previous_state():
    """Subsequent probes (e.g. a future re-probe-on-demand endpoint)
    must overwrite the cache so the FE never sees a stale healthy
    status after the LLM goes down."""
    cache_probe_results([
        ProbeResult(role=LLM_ROLE_TEXT, ok=True, provider="p", model="m"),
    ])
    assert current_health().healthy is True

    cache_probe_results([
        ProbeResult(
            role=LLM_ROLE_TEXT, ok=False, provider="p", model="m",
            error="endpoint dropped",
        ),
    ])
    snapshot = current_health()
    assert snapshot.healthy is False
    assert snapshot.results[0].error == "endpoint dropped"


# ---- Per-call deadline (the "container running but server hung" fix) -


class _HangingText:
    """Stub client that mimics a TextLLMClient.generate() blocked on a
    socket read against an unreachable LLM endpoint. Without the
    probe-side deadline this would hang for the client's full
    configured timeout (300s in the dev stack) and the worker / API
    container would appear to hang at startup."""

    provider = "openai_compat"
    model = "hung-model"

    def generate(self, prompt, **_):
        import time
        time.sleep(30)  # would be the LLM's configured timeout in prod


def test_probe_registry_enforces_short_deadline_against_hanging_client():
    """The probe MUST fail fast when the LLM endpoint hangs. Without
    this, worker / API startup blocks for minutes per unreachable
    role and operators see 'container is running but the server is
    not serving requests' with no clear cause. Pin the upper bound
    so the probe completes well within the deadline (we use a 0.5s
    deadline against a 30s hanging stub — anything more than ~3s
    means the deadline isn't actually enforced)."""
    import time

    registry = LLMProviderRegistry()
    registry.register(LLM_ROLE_TEXT, _HangingText())

    started = time.monotonic()
    results = probe_registry(registry, timeout_seconds=0.5)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, (
        f"probe should complete within deadline + small overhead, "
        f"took {elapsed:.2f}s — the deadline isn't being enforced"
    )
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].role == LLM_ROLE_TEXT
    assert "timed out" in (results[0].error or "").lower()


def test_probe_registry_passes_through_real_errors_under_deadline():
    """Real exceptions (auth failure, bad model name, etc.) must still
    propagate through the probe — the deadline only catches HANGS,
    not normal failures. A `connection refused` error fires
    immediately and should be reported as a normal probe failure,
    not a timeout."""
    registry = LLMProviderRegistry()
    registry.register(LLM_ROLE_TEXT, _FailingText())

    results = probe_registry(registry, timeout_seconds=5.0)

    assert len(results) == 1
    assert results[0].ok is False
    # NOT the timeout error — the real exception's message survived.
    assert "connection refused" in (results[0].error or "")
    assert "timed out" not in (results[0].error or "").lower()


# ---- Background health monitor (LLM up/down without restart) -------


@pytest.fixture(autouse=True)
def _stop_monitor_between_tests():
    """Ensure no leftover monitor leaks across tests. The monitor is
    a daemon thread; tests that start one must stop it so the next
    test starts with a clean process state. Yields to let the test
    run, then unconditionally tears down — safe to call even when no
    monitor is running."""
    yield
    stop_health_monitor(timeout=2.0)
    cache_probe_results([])


def test_llm_health_monitor_interval_default_30s():
    assert llm_health_monitor_interval(env={}) == 30.0


def test_llm_health_monitor_interval_zero_disables():
    """`0` is the documented kill-switch for the monitor. Negative
    values are clamped to 0 so a misconfiguration doesn't loop the
    monitor instantly."""
    assert llm_health_monitor_interval(env={"J1_LLM_HEALTH_MONITOR_INTERVAL_SECONDS": "0"}) == 0.0
    assert llm_health_monitor_interval(env={"J1_LLM_HEALTH_MONITOR_INTERVAL_SECONDS": "-5"}) == 0.0


def test_start_health_monitor_disabled_when_interval_zero():
    """When the operator sets the interval to 0, no background thread
    is spawned. The startup probe still ran (cache reflects that),
    but the cache won't auto-refresh."""
    registry = LLMProviderRegistry()
    registry.register(LLM_ROLE_TEXT, _OkText())

    started = start_health_monitor(registry, interval_seconds=0)
    assert started is False


def test_start_health_monitor_is_idempotent():
    """The startup hook may be called twice (e.g. uvicorn worker
    re-init in dev). Subsequent calls must be no-ops so we don't end
    up with multiple monitors all probing the same upstream LLM
    every 30s."""
    registry = LLMProviderRegistry()
    registry.register(LLM_ROLE_TEXT, _OkText())

    first = start_health_monitor(registry, interval_seconds=10)
    second = start_health_monitor(registry, interval_seconds=10)
    assert first is True
    assert second is False


def test_health_monitor_refreshes_cache_when_endpoint_recovers():
    """The whole point of the monitor: when the LLM endpoint goes
    from FAILING to HEALTHY (operator fixed it), the cached
    snapshot must reflect the new state within roughly one
    interval. Without the monitor, the cache stays stale until
    restart and the FE banner sticks around forever.

    Uses a stub client whose first call fails and subsequent calls
    succeed — mirrors the recovery scenario exactly."""
    import time

    class _FlipText:
        provider = "openai_compat"
        model = "flip-model"

        def __init__(self):
            self._calls = 0
            self._lock = threading.Lock()

        def generate(self, prompt, **_):
            with self._lock:
                self._calls += 1
                call_n = self._calls
            if call_n == 1:
                raise LLMProviderUnavailable("first call: endpoint down")
            return ("pong", LLMUsage(provider=self.provider, model=self.model,
                                     input_tokens=1, output_tokens=1, total_tokens=2))

    flip = _FlipText()
    registry = LLMProviderRegistry()
    registry.register(LLM_ROLE_TEXT, flip)

    # Tight 0.1s interval so the test doesn't block. Startup probe
    # records the FAILED first call; monitor's next tick records the
    # SUCCESSFUL second call.
    cache_probe_results(probe_registry(registry))
    assert current_health().healthy is False

    start_health_monitor(registry, interval_seconds=0.1)

    # Wait up to 2s for the cache to flip — way more headroom than
    # the 0.1s interval needs in practice but generous enough that
    # CI jitter doesn't flake.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if current_health().healthy:
            break
        time.sleep(0.05)

    assert current_health().healthy is True, (
        "monitor never refreshed the cache after the endpoint recovered"
    )


def test_health_monitor_keeps_running_when_a_tick_raises():
    """Defensive: an exception inside `probe_registry` must not kill
    the monitor — it has to outlive transient errors so it can keep
    refreshing once the system stabilises."""
    import threading as _threading
    import time

    raised = _threading.Event()

    class _RaisingRegistry:
        def __init__(self):
            self._fail_until = 1
            self._calls = 0

        def try_resolve(self, role):
            self._calls += 1
            if self._calls <= self._fail_until:
                raised.set()
                raise RuntimeError("tick raised")
            # After the first raising tick: return a healthy stub
            # ONLY for the text role; everything else returns None
            # so the probe loop skips them silently.
            if role == LLM_ROLE_TEXT:
                return _OkText()
            return None

    registry = _RaisingRegistry()
    start_health_monitor(registry, interval_seconds=0.05)

    # Wait for the raising tick to fire + at least one more tick.
    assert raised.wait(timeout=2.0), "first tick never ran"
    time.sleep(0.2)  # let subsequent ticks run

    snapshot = current_health()
    # Cache should have been populated by a SUBSEQUENT (non-raising)
    # tick — proving the monitor outlived the exception.
    # (Roles list is empty in current_health when no probed-role
    # client was returned; since our stub `try_resolve` returns
    # _OkText for every role, results are populated and healthy.)
    assert snapshot.healthy is True
    assert len(snapshot.results) >= 1
