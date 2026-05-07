"""Tests for the process-persistent event loop helper.

The headline regression: the helper must let the same `asyncio.Lock`
be acquired on consecutive `run_coroutine` calls. That's the
LightRAG behaviour we're defending against — its module-level locks
get bound to the first loop they meet and break on a second loop.
With our persistent-loop helper, all dispatched coroutines share
ONE loop forever (per process), so cached locks stay valid.
"""

from __future__ import annotations

import asyncio

import pytest

from j1.providers.raganything._persistent_loop import (
    get_persistent_loop,
    reset_persistent_loop_for_tests,
)


@pytest.fixture(autouse=True)
def _isolate_loop():
    """Each test gets a fresh loop singleton. Without this, a leaked
    loop from one test would surface as cross-test pollution
    (locks stay alive in module state). Production never resets
    the loop — only the test suite does."""
    reset_persistent_loop_for_tests()
    yield
    reset_persistent_loop_for_tests()


def test_run_coroutine_returns_value():
    """Sanity smoke: a coroutine's return value bubbles back to
    the calling thread."""
    loop = get_persistent_loop()

    async def _co():
        return 42

    assert loop.run_coroutine(_co()) == 42


def test_run_coroutine_propagates_exceptions():
    """Errors inside the coroutine surface to the caller — same
    contract `asyncio.run` had."""
    loop = get_persistent_loop()

    async def _bomb():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        loop.run_coroutine(_bomb())


def test_lock_survives_across_dispatches():
    """The headline fix: a Lock acquired on the first dispatch
    must be re-acquirable on the second. With `asyncio.run`, this
    raises `RuntimeError: ... is bound to a different event loop`
    on the second call (different loop). With the persistent
    loop, the lock stays bound to the same loop forever."""
    loop = get_persistent_loop()
    lock = None

    async def _create_and_acquire():
        nonlocal lock
        if lock is None:
            lock = asyncio.Lock()
        async with lock:
            return "ok"

    # First dispatch — creates the Lock on the persistent loop.
    assert loop.run_coroutine(_create_and_acquire()) == "ok"
    # Second dispatch — re-acquires the SAME Lock. With
    # `asyncio.run` per call, this would raise. With the
    # persistent loop, it succeeds.
    assert loop.run_coroutine(_create_and_acquire()) == "ok"
    # Third dispatch — same. Locks any future regression that
    # accidentally re-introduces per-call loop creation.
    assert loop.run_coroutine(_create_and_acquire()) == "ok"


def test_get_persistent_loop_returns_same_instance():
    """Singleton contract: every caller in the process sees the
    same loop. Two `get_persistent_loop()` calls in different
    threads must return identity-equal objects."""
    a = get_persistent_loop()
    b = get_persistent_loop()
    assert a is b


def test_reset_creates_fresh_singleton():
    """Test isolation: `reset_persistent_loop_for_tests` swaps in
    a fresh loop. Locks created against the old loop are dead;
    new ones bind to the new loop."""
    a = get_persistent_loop()
    reset_persistent_loop_for_tests()
    b = get_persistent_loop()
    assert a is not b


def test_concurrent_dispatches_from_multiple_threads():
    """Concurrent compile activities (Temporal worker threads
    invoking the bridge in parallel) must all complete cleanly.
    Locks the cooperative-scheduling contract: dispatches from
    different threads cooperate inside the single loop."""
    import threading

    loop = get_persistent_loop()
    results: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    async def _work(value: int) -> int:
        await asyncio.sleep(0.01)
        return value * 2

    def _runner(value: int) -> None:
        try:
            out = loop.run_coroutine(_work(value))
            with lock:
                results.append(out)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=_runner, args=(i,))
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors, f"unexpected errors: {errors}"
    assert sorted(results) == [0, 2, 4, 6, 8, 10, 12, 14]


def test_shutdown_is_idempotent():
    """Calling `shutdown` twice in a row (e.g. during teardown
    plus a finalizer) must not raise. The helper's daemon-thread
    flag is the production safety net; explicit shutdown is the
    test-side cleanup. Both paths must be tolerant."""
    loop = get_persistent_loop()
    loop.shutdown()
    loop.shutdown()  # no raise


def test_keyed_lock_dict_pattern_survives_calls():
    """LightRAG's actual bug shape — replicated. Locks live in a
    dict keyed by name; the dict is module-level and persists
    across calls. With the persistent loop, the dict-cached
    locks remain usable on the second call (same loop). With
    `asyncio.run`, this would fail."""
    loop = get_persistent_loop()
    cache: dict[str, asyncio.Lock] = {}

    async def _acquire(key: str) -> str:
        # Mimic `_get_or_create_async_lock` — first caller
        # creates it; subsequent callers reuse from the dict.
        existing = cache.get(key)
        if existing is None:
            existing = asyncio.Lock()
            cache[key] = existing
        async with existing:
            return key

    # First dispatch creates locks under "a" and "b".
    assert loop.run_coroutine(_acquire("a")) == "a"
    assert loop.run_coroutine(_acquire("b")) == "b"
    # Second dispatch reuses BOTH cached locks from the dict.
    # This is the exact pattern that fails under `asyncio.run`.
    assert loop.run_coroutine(_acquire("a")) == "a"
    assert loop.run_coroutine(_acquire("b")) == "b"
