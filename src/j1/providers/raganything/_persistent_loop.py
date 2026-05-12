"""Process-persistent asyncio event loop for the RAGAnything bridge.

Why this exists
---------------
LightRAG (the engine RAGAnything wraps) caches `asyncio.Lock` objects
in module-level state — `_async_locks`, `KeyedUnifiedLock._async_lock`,
and the various `pipeline_status` / `internal_lock` / `data_init_lock`
singletons live in `lightrag.kg.shared_storage`. Every
`asyncio.Lock` is **bound to the event loop that first awaited it**;
re-entering the lock from a different loop raises
``RuntimeError:... is bound to a different event loop``.

The bridge previously drove RAGAnything via ``asyncio.run(coro)``,
which creates a fresh loop per compile/query call. The first
compile runs on loop A, LightRAG's cached locks bind to loop A,
loop A closes when ``asyncio.run`` exits. The next compile creates
loop B and tries to reuse the locks — boom.

The fix is to run a single event loop for the lifetime of the
worker process and dispatch every RAGAnything coroutine onto it
via ``run_coroutine_threadsafe``. The cached locks stay bound to
that one loop forever; concurrent compiles cooperate via the
loop's own scheduling.

What this is NOT
----------------
* This is NOT a general-purpose persistent loop for J1. Reach for
 it only when you're crossing the boundary into a library that
 caches asyncio primitives module-wide (LightRAG today; consider
 others on a case-by-case basis).
* This does NOT spawn one loop per activity. The whole point is
 that there's one loop per process, shared.
* This does NOT auto-restart after a crash. If the loop thread
 dies, subsequent calls raise ``RuntimeError`` and the worker
 needs a restart. That matches Temporal-activity semantics:
 a poisoned worker should be cycled, not patched up at runtime.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import TimeoutError as _FuturesTimeoutError
from typing import Awaitable, TypeVar

_log = logging.getLogger("j1.providers.raganything._persistent_loop")

_T = TypeVar("_T")


class _PersistentEventLoop:
    """Owns one asyncio loop running on a daemon thread.

 Construction is lazy via :func:`get_persistent_loop` so test
 suites that never touch RAGAnything pay nothing for it.
 """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        # Daemon thread so the worker process exit isn't blocked
        # by a still-running loop. Loop is stopped explicitly in
        # `shutdown` for clean shutdowns; daemon flag is the
        # belt-and-braces.
        self._thread = threading.Thread(
            target=self._serve,
            name="j1-raganything-loop",
            daemon=True,
        )
        # `_started` flips once the thread enters `run_forever`.
        # Future calls to `run_coroutine` don't actually need to
        # wait on this — `run_coroutine_threadsafe` is safe even
        # before the loop is running — but having the signal
        # makes the diagnostics in tests less surprising.
        self._started = threading.Event()
        self._thread.start()

    def _serve(self) -> None:
        """Thread target: install loop, signal ready, run forever."""
        asyncio.set_event_loop(self._loop)
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            # Drain any pending callbacks before close — otherwise
            # `loop.close` raises noisy "Event loop is closed"
            # warnings on tasks that finish post-stop.
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:  # noqa: BLE001
                pass
            self._loop.close()

    def run_coroutine(
        self,
        coro: Awaitable[_T],
        *,
        timeout: float | None = None,
    ) -> _T:
        """Schedule `coro` on the persistent loop and block until done.

 Caller blocks the calling thread (a Temporal activity
 worker thread) — same blocking semantics as the previous
 ``asyncio.run`` call site. Raised exceptions inside the
 coroutine surface to the caller verbatim.

 ``timeout`` (seconds) bounds how long the caller will block.
 On expiry the underlying scheduled coroutine is cancelled
 and ``concurrent.futures.TimeoutError`` is raised. Used by
 the native RAGAnything query path so a stuck vendor call
 can't hang a validation request. ``None`` (default)
 preserves the historical "block indefinitely" semantics so
 every existing caller is unaffected.
 """
        # `run_coroutine_threadsafe` is the only correct way to
        # bridge a coroutine from one thread into a loop running
        # in another. ``Future.result`` blocks the calling
        # thread until the loop completes the coroutine — with
        # an optional timeout that maps onto the underlying
        # ``concurrent.futures.Future.result(timeout=…)``.
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except _FuturesTimeoutError:
            # Best-effort: cancel the in-flight task on the loop
            # so we don't keep doing LLM work the caller has
            # already given up on. The cancel propagates via
            # the loop thread; it may not interrupt LLM-side
            # blocking IO, but it stops further work from
            # scheduling.
            future.cancel()
            raise

    def shutdown(self) -> None:
        """Stop the loop and join the thread.

 Only used by tests today — production process shutdown
 relies on the daemon flag. Calling this twice is a no-op.
 """
        if not self._loop.is_running():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        # Generous timeout — pending coroutines may need to drain.
        # `join(None)` would block forever if a coroutine hangs.
        self._thread.join(timeout=10.0)


# Module-level singleton. Lazily constructed on first
# `get_persistent_loop` call so test runs that never touch
# RAGAnything don't spawn a thread.
_loop_singleton: _PersistentEventLoop | None = None
_loop_lock = threading.Lock()


def get_persistent_loop() -> _PersistentEventLoop:
    """Return the process-wide persistent event loop, creating it
 on first call. Thread-safe.

 Two `get_persistent_loop` callers from different threads
 will see the same loop — that's the whole point.
 """
    global _loop_singleton
    if _loop_singleton is not None:
        return _loop_singleton
    with _loop_lock:
        if _loop_singleton is None:
            _log.debug("starting persistent event loop for RAGAnything")
            _loop_singleton = _PersistentEventLoop()
        return _loop_singleton


def reset_persistent_loop_for_tests() -> None:
    """Test-only: shut down + clear the singleton.

 Tests that exercise the persistent loop should call this in
 teardown so a leaked loop from one test doesn't bleed locks
 into the next. Production code never calls this.
 """
    global _loop_singleton
    with _loop_lock:
        if _loop_singleton is not None:
            try:
                _loop_singleton.shutdown()
            except Exception:  # noqa: BLE001
                pass
            _loop_singleton = None
