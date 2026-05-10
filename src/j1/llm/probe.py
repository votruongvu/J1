"""Startup + on-demand connectivity probe for the LLM provider stack.

Why: when J1 ingests a document the worker calls the LLM many times
(planner, entity extraction, vision captioning, embeddings). If the
LLM endpoint is unreachable, the run crashes mid-flight — the user
uploads a document, sees a generic failure, and the operator has to
dig through logs to find the cause.

This module exercises every CONFIGURED LLM role with a tiny,
idempotent request and caches the latest results in a process-local
state. The API surfaces the cached state via `/healthz/llm` so the
FE can render an "LLM unreachable" banner + disable uploads BEFORE
the user wastes time on an upload that's guaranteed to fail.

The probe is opt-out via `J1_LLM_STARTUP_PROBE=false` for tests and
mock-only deployments — production runs leave it enabled.

Probe failures are warn-only at startup: the worker + API still boot
and serve everything that doesn't depend on the LLM (run history,
audit logs, raw artifact downloads). Only NEW ingestion runs are
gated at the FE banner.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from j1.llm.errors import LLMProviderUnavailable
from j1.llm.registry import (
    LLM_ROLE_EMBEDDING,
    LLM_ROLE_FAST,
    LLM_ROLE_PREMIUM,
    LLM_ROLE_TEXT,
    LLM_ROLE_VISION,
    LLMProviderRegistry,
)

_log = logging.getLogger("j1.llm.probe")

ENV_LLM_STARTUP_PROBE = "J1_LLM_STARTUP_PROBE"
ENV_LLM_PROBE_TIMEOUT = "J1_LLM_PROBE_TIMEOUT_SECONDS"
ENV_LLM_HEALTH_MONITOR_INTERVAL = "J1_LLM_HEALTH_MONITOR_INTERVAL_SECONDS"

# Background re-probe interval. When the LLM endpoint goes down or
# back up after startup, we want the cached health snapshot to
# reflect that within a bounded window WITHOUT requiring an operator
# restart. 30s is the sweet spot: short enough that the FE banner
# clears quickly when the operator fixes the endpoint, long enough
# that the upstream LLM doesn't see meaningful probe traffic.
DEFAULT_HEALTH_MONITOR_INTERVAL_SECONDS = 30.0

# Hard ceiling per probe call. The underlying client's configured
# timeout (e.g. `J1_TEXT_LLM_TIMEOUT_SECONDS=300`) is sized for real
# generation; the probe is just a reachability check. Without this
# wrapping deadline, a down LLM blocks worker / API startup for the
# full 300s × N-roles before either container starts serving — which
# looks like "container is running but server is down" to operators.
# Override via `J1_LLM_PROBE_TIMEOUT_SECONDS` for slow cold-start
# environments (defaults to 5s, plenty for any reachable endpoint).
DEFAULT_PROBE_TIMEOUT_SECONDS = 5.0

# Roles probed when present. Order matters only for deterministic log
# output. Each role uses the cheapest possible API call to confirm
# connectivity + auth + model availability.
#
# The TEXT and FAST roles share a generate() probe with a 1-token
# response cap. EMBEDDING uses a 1-character embed_text() call.
# VISION + PREMIUM are intentionally NOT probed — vision needs a real
# image payload (no idempotent zero-cost shape) and PREMIUM is
# expensive per call. Both fail loudly at use-time if misconfigured;
# the startup probe focuses on the always-on path.
_PROBED_ROLES: tuple[str, ...] = (
    LLM_ROLE_TEXT,
    LLM_ROLE_FAST,
    LLM_ROLE_EMBEDDING,
)


@dataclass(frozen=True)
class ProbeResult:
    role: str
    ok: bool
    provider: str | None
    model: str | None
    error: str | None = None


@dataclass(frozen=True)
class LLMHealthSnapshot:
    """Snapshot of the cached probe results, surfaced by the API's
    `/healthz/llm` endpoint and consumed by the FE banner.

    `healthy` is True iff every probed role last reported `ok=True`.
    `checked_at` is the wall-clock time of the most recent probe;
    the FE shows it in the banner so operators know how stale the
    status is.
    """

    healthy: bool
    checked_at: str | None
    results: tuple[ProbeResult, ...]


# ---- Process-local cache ------------------------------------------
#
# Both the API and worker run a probe at startup and cache the
# results here. The API's `/healthz/llm` reads the cache (no
# re-probe per request — that would melt the upstream LLM under
# even modest FE polling). A future re-probe-on-demand endpoint
# can call `cache_probe_results(probe_registry(registry))` to
# refresh.
_cache_lock = threading.Lock()
_cached_results: tuple[ProbeResult, ...] = ()
_cached_checked_at: str | None = None


def cache_probe_results(results: list[ProbeResult] | tuple[ProbeResult, ...]) -> None:
    """Store the latest probe results so `current_health()` can read
    them. Called by the worker + API startup hooks; safe to call
    multiple times (latest call wins)."""
    global _cached_results, _cached_checked_at
    with _cache_lock:
        _cached_results = tuple(results)
        _cached_checked_at = datetime.now(timezone.utc).isoformat()


def current_health() -> LLMHealthSnapshot:
    """Return the most recently cached probe results.

    When no probe has run yet (e.g. probe disabled, or this process
    skipped the startup hook), returns `healthy=True` with empty
    results — matches the conservative "assume working until proven
    otherwise" behaviour of every other health check in the stack."""
    with _cache_lock:
        results = _cached_results
        checked_at = _cached_checked_at
    if not results:
        return LLMHealthSnapshot(
            healthy=True, checked_at=checked_at, results=(),
        )
    return LLMHealthSnapshot(
        healthy=all(r.ok for r in results),
        checked_at=checked_at,
        results=results,
    )


class LLMStartupProbeError(RuntimeError):
    """Raised when one or more required LLM roles are unreachable.

    Caller catches this and aborts startup with the error's message
    rendered to stderr. The message is operator-facing — names the
    role, provider, model, base URL when available, and the wrapped
    error string."""


def llm_probe_enabled(env: dict | None = None) -> bool:
    """Returns True when the startup probe should run.

    Defaults to True. Set `J1_LLM_STARTUP_PROBE=false` to opt out
    (tests and mock-only deployments)."""
    source = env if env is not None else os.environ
    raw = str(source.get(ENV_LLM_STARTUP_PROBE, "true")).strip().lower()
    return raw not in {"false", "0", "no", "off"}


def llm_probe_timeout(env: dict | None = None) -> float:
    """Per-probe-call deadline. Returns a float number of seconds."""
    source = env if env is not None else os.environ
    raw = source.get(ENV_LLM_PROBE_TIMEOUT)
    if not raw:
        return DEFAULT_PROBE_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_PROBE_TIMEOUT_SECONDS
    if value <= 0:
        return DEFAULT_PROBE_TIMEOUT_SECONDS
    return value


def llm_health_monitor_interval(env: dict | None = None) -> float:
    """Re-probe interval for the background health monitor.

    Set to 0 (or any non-positive value) to disable the monitor;
    only the startup probe runs in that case."""
    source = env if env is not None else os.environ
    raw = source.get(ENV_LLM_HEALTH_MONITOR_INTERVAL)
    if not raw:
        return DEFAULT_HEALTH_MONITOR_INTERVAL_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_HEALTH_MONITOR_INTERVAL_SECONDS
    return max(value, 0.0)


def probe_registry(
    registry: LLMProviderRegistry,
    *,
    roles: tuple[str, ...] | None = None,
    timeout_seconds: float | None = None,
) -> list[ProbeResult]:
    """Exercise each configured role with a minimal request.

    Returns a `ProbeResult` per probed role. Roles that aren't
    registered are skipped silently — the operator opted not to
    configure them. Roles that ARE registered but fail the probe
    return `ok=False` with the error string.

    Each probe call is wrapped in a hard deadline (default 5s,
    overridable via `J1_LLM_PROBE_TIMEOUT_SECONDS`). The underlying
    client's configured timeout is sized for real generation and is
    far too long to use for a startup reachability check — without
    this wrapping deadline, a down LLM would block worker / API
    startup for minutes per role and operators would see "container
    running but server unreachable" with no obvious cause.

    Caller decides how to handle failures (abort startup, log + warn,
    etc.). This function never raises for a probe failure — it
    returns the result so the caller controls the policy.
    """
    targets = roles if roles is not None else _PROBED_ROLES
    deadline = timeout_seconds if timeout_seconds is not None else llm_probe_timeout()
    results: list[ProbeResult] = []
    for role in targets:
        client = registry.try_resolve(role)
        if client is None:
            continue
        provider = getattr(client, "provider", None)
        model = getattr(client, "model", None)
        try:
            _run_with_deadline(role, client, deadline)
            results.append(ProbeResult(
                role=role, ok=True, provider=provider, model=model,
            ))
        except concurrent.futures.TimeoutError:
            results.append(ProbeResult(
                role=role, ok=False, provider=provider, model=model,
                error=(
                    f"probe timed out after {deadline:.1f}s — endpoint "
                    f"unreachable or extremely slow"
                ),
            ))
        except Exception as exc:  # noqa: BLE001 — probe converts every error
            results.append(ProbeResult(
                role=role, ok=False, provider=provider, model=model,
                error=f"{type(exc).__name__}: {exc}",
            ))
    return results


def _run_with_deadline(role: str, client: object, deadline: float) -> None:
    """Run `_exercise_role(role, client)` on a worker thread, raising
    `TimeoutError` when it doesn't finish within `deadline` seconds.

    Critical: we DON'T use the executor as a context manager.
    `ThreadPoolExecutor.__exit__` blocks until pending threads finish
    — which would defeat the entire point of the deadline against a
    hanging socket read. Instead, on timeout we call `shutdown(wait=
    False, cancel_futures=True)`. The hanging thread continues
    running in the background (it's a daemon thread, so it dies with
    the process) but the probe returns immediately with the timeout
    error, and the caller's startup hook proceeds.
    """
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix=f"j1-llm-probe-{role}",
    )
    try:
        future = pool.submit(_exercise_role, role, client)
        # `result(timeout)` raises `concurrent.futures.TimeoutError`
        # when the call hangs; raises any underlying exception
        # otherwise. We let both propagate to the caller.
        future.result(timeout=deadline)
        # Successful path: clean shutdown waits for the (already
        # completed) thread to die.
        pool.shutdown(wait=True)
    except BaseException:
        # Timeout or other exception: tear down the pool WITHOUT
        # waiting. The hanging thread can't be cancelled (Python
        # has no thread-kill primitive), but daemon-marking it via
        # the worker's `thread_name_prefix=j1-llm-probe-*` lets it
        # die when the process exits — and the probe doesn't
        # block startup in the meantime.
        pool.shutdown(wait=False, cancel_futures=True)
        raise


def assert_required_llm_reachable(
    registry: LLMProviderRegistry,
    *,
    roles: tuple[str, ...] | None = None,
) -> None:
    """Run the probe; raise `LLMStartupProbeError` on any failure.

    Convenience wrapper for the common case: 'fail startup if any
    configured-and-required role can't be reached.' Logs each result
    at INFO so the startup banner shows the LLM diagnostic state."""
    results = probe_registry(registry, roles=roles)
    if not results:
        _log.info(
            "LLM startup probe: no probed roles registered; skipping",
        )
        return
    failures: list[ProbeResult] = []
    for result in results:
        if result.ok:
            _log.info(
                "LLM probe ok: role=%s provider=%s model=%s",
                result.role, result.provider, result.model,
            )
        else:
            _log.error(
                "LLM probe FAILED: role=%s provider=%s model=%s error=%s",
                result.role, result.provider, result.model, result.error,
            )
            failures.append(result)
    if failures:
        lines = [
            "J1 cannot start: one or more configured LLM roles are "
            "unreachable. Fix the endpoint(s) below and restart.",
            "",
        ]
        for f in failures:
            lines.append(
                f"  - role={f.role}  provider={f.provider}  "
                f"model={f.model}\n      {f.error}"
            )
        lines.append("")
        lines.append(
            "Hints: confirm the LM Studio / vLLM / hosted endpoint is "
            "running, the API key is correct, and the model is loaded. "
            "Set J1_LLM_STARTUP_PROBE=false to bypass this check (NOT "
            "recommended for production — runs will fail mid-pipeline "
            "instead of at startup)."
        )
        raise LLMStartupProbeError("\n".join(lines))


# ---- Per-role exercise paths ---------------------------------------


def _exercise_role(role: str, client: object) -> None:
    """One-shot, idempotent probe for the given role.

    Each probe is the cheapest-possible call that confirms
    (a) network reachability, (b) auth, (c) model availability.
    On success, returns None. On any failure (timeout, HTTP error,
    auth error, model-not-loaded), raises so the caller's
    `try/except` collects the failure.
    """
    if role == LLM_ROLE_EMBEDDING:
        embed = getattr(client, "embed_text", None)
        if embed is None:
            raise LLMProviderUnavailable(
                f"embedding client {type(client).__name__} has no embed_text"
            )
        # Single-char input is the smallest valid embedding payload —
        # most providers tokenise it to one token.
        embed("a")
        return

    # TEXT / FAST / PREMIUM all share TextLLMClient.generate.
    generate = getattr(client, "generate", None)
    if generate is None:
        raise LLMProviderUnavailable(
            f"text client {type(client).__name__} has no generate"
        )
    # Use a small but non-trivial output cap (8 tokens). With
    # max_tokens=1 most chat models emit a single special / role
    # token and return EMPTY visible content + finish_reason=length,
    # which trips the "empty content" warning in the openai_compat
    # client every time the probe runs. 8 tokens leaves room for a
    # short word or two so the model returns visible content,
    # the warning stays quiet on healthy probes, and the cost is
    # still negligible. The prompt asks for a one-word reply to
    # encourage compact output.
    generate(
        "Reply with the single word: pong",
        max_output_tokens=8,
        temperature=0.0,
    )


# ---- Background re-probe monitor -----------------------------------
#
# The startup probe gives the FE banner an initial state, but if the
# operator fixes the LLM endpoint after boot the banner stays up
# forever (until restart). Same the other way: an LLM that goes down
# mid-session leaves the banner reporting "all good" until the next
# restart. The monitor runs a daemon thread that re-probes on a
# bounded interval and refreshes the cache, so the FE always reflects
# CURRENT reality within ~30 seconds.
#
# One process can run at most one monitor at a time — `start_health_monitor`
# is idempotent + thread-safe; subsequent calls are silent no-ops.

_monitor_lock = threading.Lock()
_monitor_thread: threading.Thread | None = None
_monitor_stop: threading.Event | None = None


def start_health_monitor(
    registry: LLMProviderRegistry,
    *,
    interval_seconds: float | None = None,
    roles: tuple[str, ...] | None = None,
) -> bool:
    """Start a daemon thread that re-probes the registry on a loop.

    Returns True when a new monitor was started, False when one was
    already running (idempotent — safe to call from API + worker
    bootstrap without coordination).

    The thread is a daemon, so it dies with the process. The monitor
    exits cleanly on `stop_health_monitor()`; tests use that for
    determinism. Production code never needs to stop it explicitly.
    """
    interval = (
        interval_seconds if interval_seconds is not None
        else llm_health_monitor_interval()
    )
    if interval <= 0:
        _log.info(
            "LLM health monitor: disabled (interval=%.1fs); cache will "
            "only reflect the startup probe result",
            interval,
        )
        return False

    global _monitor_thread, _monitor_stop
    with _monitor_lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return False
        stop = threading.Event()
        thread = threading.Thread(
            target=_monitor_loop,
            args=(registry, interval, roles, stop),
            name="j1-llm-health-monitor",
            daemon=True,
        )
        _monitor_thread = thread
        _monitor_stop = stop
        thread.start()
    _log.info(
        "LLM health monitor: started (interval=%.1fs)", interval,
    )
    return True


def stop_health_monitor(timeout: float = 1.0) -> None:
    """Signal the monitor to exit and wait briefly for it to die.

    Production rarely calls this — the daemon thread dies with the
    process. Tests call it for determinism so the next test starts
    from a clean slate."""
    global _monitor_thread, _monitor_stop
    with _monitor_lock:
        thread = _monitor_thread
        stop = _monitor_stop
        _monitor_thread = None
        _monitor_stop = None
    if stop is not None:
        stop.set()
    if thread is not None:
        thread.join(timeout=timeout)


def _monitor_loop(
    registry: LLMProviderRegistry,
    interval: float,
    roles: tuple[str, ...] | None,
    stop: threading.Event,
) -> None:
    """The daemon's main loop. Re-probes, caches, sleeps. The stop
    event is checked between sleeps so a test can shut it down
    promptly without waiting a full interval.

    Exceptions from `probe_registry` are caught + logged but never
    crash the loop — the monitor must outlive transient errors so it
    keeps refreshing the cached state."""
    while not stop.is_set():
        try:
            results = probe_registry(registry, roles=roles)
            cache_probe_results(results)
            failures = [r for r in results if not r.ok]
            if failures:
                # Per-role detail at WARNING so operators can tail
                # logs and see when the endpoint flips back.
                for f in failures:
                    _log.warning(
                        "LLM health monitor: role=%s provider=%s "
                        "model=%s STILL DOWN (%s)",
                        f.role, f.provider, f.model, f.error,
                    )
            else:
                # Healthy ticks at DEBUG so a long-running healthy
                # process doesn't spam INFO logs every 30s.
                _log.debug(
                    "LLM health monitor: %d roles all reachable",
                    len(results),
                )
        except Exception as exc:  # noqa: BLE001 — monitor must never die
            _log.warning("LLM health monitor tick raised: %s", exc)
        # Use Event.wait so an external stop() can break the sleep
        # immediately instead of waiting up to `interval` seconds.
        if stop.wait(interval):
            break
    _log.info("LLM health monitor: stopped")
