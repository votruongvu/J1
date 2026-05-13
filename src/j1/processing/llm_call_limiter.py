"""concurrency limiter + bounded retry around LLM calls.

The enrichment stage's `_LLMBackedEnricher` classes call vendor
LLM clients synchronously. Without a global ceiling, a multi-doc
or parallel-module run can fire N enrichers × M docs LLM calls
at once — easy to swamp a local LM Studio instance or hit a
provider rate limit.

`LLMCallLimiter` wraps every LLM call in a single shared
`threading.Semaphore` so the worker (single-process or
multi-thread) never exceeds the configured ceiling. Adds a
wall-clock timeout (per call) + bounded retry with exponential
backoff. The limiter is composable: callers wrap any callable
via `limiter.run(fn, *args, **kwargs)`.

Design rules:

 1. **Single shared semaphore per worker.** Module-global +
 wired at bootstrap. Sharing between text + vision clients
 prevents one tier monopolising the slots; ops who want
 per-tier separation can wire two limiters explicitly.
 2. **Bounded retries.** `retry_limit=N` means at most `1 + N`
 total attempts. Exceptions that aren't retryable
 (`TimeoutError` from the limiter itself, `_NonRetryable`)
 re-raise immediately.
 3. **Timeout = soft skip at the call site.** The limiter
 raises `TimeoutError` after `timeout_seconds`; the legacy
 enricher base class already converts exceptions into a soft-
 skip response so the run continues.
 4. **No async coupling.** Enrichers are sync today;
 `threading.Semaphore` works for both single-threaded + multi-
 threaded workers. A future async migration can swap to
 `asyncio.Semaphore` without changing call sites.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar


__all__ = [
    "LLMCallLimiter",
    "LLMCallTimeout",
    "LimiterCallStats",
    "NonRetryableError",
    "build_limiter_from_settings",
]


T = TypeVar("T")


class LLMCallTimeout(TimeoutError):
    """Raised by `LLMCallLimiter.run` when a call exceeds the
 configured `timeout_seconds`. Separate from the stdlib
 `TimeoutError` so callers can distinguish limiter-timeouts
 from vendor-client timeouts that come up from below."""


class NonRetryableError(Exception):
    """Wrapper exception callers can raise inside the limited
 callable to short-circuit retry. The limiter unwraps and
 re-raises the original `__cause__` so the call site sees the
 underlying error untouched."""


@dataclass(frozen=True)
class LimiterCallStats:
    """One call's bookkeeping. Returned by `run_with_stats` so the
 `ModelUsageRecord.duration_ms` on the per-module outcome can
 be populated without timing the call separately."""

    attempts: int
    duration_ms: int
    retried: bool


class LLMCallLimiter:
    """Thread-safe limiter wrapping per-LLM-call concurrency,
 timeouts, and bounded retries.

 Single instance per worker — pass through to every enricher's
 constructor via dependency injection. Reading `max_concurrency`
 after construction reflects the original setting (it's the
 semaphore's initial value, not the current available slot count)."""

    def __init__(
        self,
        *,
        max_concurrency: int = 1,
        timeout_seconds: float = 120.0,
        retry_limit: int = 1,
        retry_backoff_seconds: float = 0.5,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError(
                f"max_concurrency must be >= 1; got {max_concurrency}"
            )
        if timeout_seconds <= 0:
            raise ValueError(
                f"timeout_seconds must be > 0; got {timeout_seconds}"
            )
        if retry_limit < 0:
            raise ValueError(
                f"retry_limit must be >= 0; got {retry_limit}"
            )
        self._max_concurrency = max_concurrency
        self._timeout_seconds = timeout_seconds
        self._retry_limit = retry_limit
        self._retry_backoff_seconds = retry_backoff_seconds
        self._semaphore = threading.BoundedSemaphore(value=max_concurrency)

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def retry_limit(self) -> int:
        return self._retry_limit

    def run(
        self,
        fn: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Run `fn(*args, **kwargs)` under the limiter. Returns
 `fn`'s return value. Raises `LLMCallTimeout` if the call
 couldn't acquire a slot OR exceeded the per-call timeout
 once running. Bounded retries are applied transparently;
 the caller sees a single return / raise."""
        result, _stats = self.run_with_stats(fn, *args, **kwargs)
        return result

    def run_with_stats(
        self,
        fn: Callable[..., T],
        *args: Any,
        purpose: str | None = None,
        **kwargs: Any,
    ) -> tuple[T, LimiterCallStats]:
        """Like `run` but also returns `LimiterCallStats` so the
 caller can populate `ModelUsageRecord.duration_ms` without
 timing the call separately.

 ``purpose`` (Phase-1 ingestion diagnostics, optional kwarg)
 tags this call with a coarse label like ``"chunk_metadata"``,
 ``"assessment_consult"``, ``"enrichment.images"``. When the
 ambient :func:`j1.processing.diagnostics.current_run_context`
 is set (the activity wrapper binds it on entry) the limiter
 emits a structured ``j1.ingestion.llm_call.completed`` audit
 event after each call. Default ``None`` (unlabelled) keeps
 every existing call site working unchanged.
 """
        attempts = 0
        last_exc: BaseException | None = None
        started_total = time.perf_counter()
        last_error: str | None = None
        try:
            for attempt in range(1 + self._retry_limit):
                attempts = attempt + 1
                try:
                    value = self._run_once(fn, args, kwargs)
                except NonRetryableError as exc:
                    cause = exc.__cause__ if exc.__cause__ else exc
                    last_error = type(cause).__name__
                    raise cause from None
                except LLMCallTimeout as exc:
                    last_error = type(exc).__name__
                    raise
                except Exception as exc:  # noqa: BLE001 — retry candidate
                    last_exc = exc
                    last_error = type(exc).__name__
                    if attempt >= self._retry_limit:
                        raise
                    # Exponential-ish backoff before the next attempt.
                    time.sleep(self._retry_backoff_seconds * (attempt + 1))
                    continue
                duration_ms = int(
                    (time.perf_counter() - started_total) * 1000
                )
                stats = LimiterCallStats(
                    attempts=attempts, duration_ms=duration_ms,
                    retried=attempts > 1,
                )
                _emit_diag_llm_call(
                    fn=fn,
                    purpose=purpose,
                    stats=stats,
                    error=None,
                )
                return value, stats
        except BaseException:
            # Failure paths: report the call (the exception is
            # already being re-raised) so the diagnostic report
            # counts retries + errored calls.
            duration_ms = int(
                (time.perf_counter() - started_total) * 1000
            )
            _emit_diag_llm_call(
                fn=fn,
                purpose=purpose,
                stats=LimiterCallStats(
                    attempts=attempts or 1,
                    duration_ms=duration_ms,
                    retried=(attempts or 1) > 1,
                ),
                error=last_error or "Exception",
            )
            raise
        # The retry loop exhausted; the final exception was already
        # raised in the loop. This line keeps mypy / pyright quiet
        # — execution doesn't reach it in practice.
        raise last_exc if last_exc else RuntimeError(
            "limiter exhausted retries with no exception captured",
        )

    def _run_once(
        self,
        fn: Callable[..., T],
        args: tuple,
        kwargs: dict,
    ) -> T:
        """Acquire one slot, run `fn`, release. Raises
 `LLMCallTimeout` when:
 * the slot wait exceeds `timeout_seconds`, OR
 * `fn` itself blocks longer than `timeout_seconds` after
 the slot was acquired.

 The acquire-side timeout is the semaphore's built-in
 timeout; the run-side timeout is enforced by running `fn`
 in a worker thread + joining with the remaining budget."""
        acquire_started = time.perf_counter()
        acquired = self._semaphore.acquire(
            blocking=True, timeout=self._timeout_seconds,
        )
        if not acquired:
            raise LLMCallTimeout(
                f"failed to acquire LLM-call slot within "
                f"{self._timeout_seconds:.1f}s"
            )
        remaining = max(
            0.0,
            self._timeout_seconds - (time.perf_counter() - acquire_started),
        )
        try:
            return _call_with_timeout(fn, args, kwargs, remaining)
        finally:
            self._semaphore.release()


def _call_with_timeout(
    fn: Callable[..., T],
    args: tuple,
    kwargs: dict,
    timeout_seconds: float,
) -> T:
    """Run `fn(*args, **kwargs)` with a wall-clock timeout.

 The implementation spins up a worker thread + joins with the
 deadline. If the join times out, raises `LLMCallTimeout` — the
 worker thread keeps running but the caller's call returns the
 timeout. This is the cleanest signal we can produce without
 cooperative cancellation from the vendor client."""
    if timeout_seconds <= 0:
        raise LLMCallTimeout(
            "no time remaining after slot acquisition"
        )
    result_box: dict[str, Any] = {}

    def _target() -> None:
        try:
            result_box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — propagate any error
            result_box["exc"] = exc

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join(timeout=timeout_seconds)
    if worker.is_alive():
        # The worker is stuck; raise so the caller gets a clear
        # timeout signal. The thread will keep running but daemon=
        # True means it won't keep the process alive on shutdown.
        raise LLMCallTimeout(
            f"LLM call exceeded {timeout_seconds:.1f}s timeout"
        )
    if "exc" in result_box:
        raise result_box["exc"]
    return result_box.get("value")  # type: ignore[return-value]


def build_limiter_from_settings(settings) -> LLMCallLimiter:
    """Construct an `LLMCallLimiter` from
 `EnrichmentConcurrencySettings`. Convenience wrapper used by
 the worker bootstrap so the limiter + settings stay in sync
 without the call-site spelling out every field."""
    return LLMCallLimiter(
        max_concurrency=settings.max_concurrent_llm_calls,
        timeout_seconds=settings.timeout_seconds,
        retry_limit=settings.retry_limit,
    )


def _emit_diag_llm_call(
    *,
    fn,
    purpose: str | None,
    stats: LimiterCallStats,
    error: str | None,
) -> None:
    """Forward a limiter call to the ambient diagnostic recorder.

    Reads the ``RunContext`` set by the activity wrapper from a
    ``contextvars.ContextVar`` — so we can attribute deep-stack
    LLM calls (chunk metadata, enrichment, refinement) to the
    active run without threading the recorder through every call
    site. Best-effort: any failure inside is swallowed (already
    logged inside the recorder).

    ``fn`` is the wrapped callable; we look for ``provider`` and
    ``model`` attributes on its ``__self__`` (typical for bound
    methods on LLM client instances) so the report can break
    down by provider/model without the caller spelling them out.
    """
    try:
        from j1.processing.diagnostics import current_run_context
    except Exception:  # noqa: BLE001 — circular-import defensive
        return
    rc = current_run_context()
    if rc is None:
        return
    provider = None
    model = None
    try:
        bound = getattr(fn, "__self__", None)
        if bound is not None:
            provider = getattr(bound, "provider", None)
            model = getattr(bound, "model", None)
    except Exception:  # noqa: BLE001
        pass
    try:
        rc.recorder.record_llm_call(
            ctx=rc.ctx,
            run_id=rc.run_id,
            stage=rc.stage,
            purpose=purpose or "unspecified",
            provider=provider,
            model=model,
            duration_ms=stats.duration_ms,
            attempts=stats.attempts,
            retried=stats.retried,
            error=error,
            document_id=rc.document_id,
        )
    except Exception:  # noqa: BLE001
        pass
