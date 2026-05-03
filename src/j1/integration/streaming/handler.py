from collections.abc import Callable
from typing import Protocol

from j1.integration.streaming.events import AnswerStreamEvent


class AnswerStreamHandler(Protocol):
    """Receives `AnswerStreamEvent`s as the streaming service emits them.

    Implementations must not raise — the streaming service does not catch
    handler exceptions in the hot path (an event drop is preferable to a
    response abort). Long-running work belongs behind the handler, not in
    `handle` itself.
    """

    def handle(self, event: AnswerStreamEvent) -> None: ...


class CallbackStreamHandler:
    """Adapts a plain callable into the `AnswerStreamHandler` Protocol.

    Useful when an adapter (e.g. the REST `StreamingResponse`) wants to
    push events into a queue/iterator without defining a class.
    """

    def __init__(self, callback: Callable[[AnswerStreamEvent], None]) -> None:
        self._callback = callback

    def handle(self, event: AnswerStreamEvent) -> None:
        self._callback(event)


class BufferingStreamHandler:
    """Collects every event in memory — the test fixture default.

    Provides `clear()` so a single instance can be reused across multiple
    streaming runs in a parametrised test.
    """

    def __init__(self) -> None:
        self.events: list[AnswerStreamEvent] = []

    def handle(self, event: AnswerStreamEvent) -> None:
        self.events.append(event)

    def by_event(self, event_type: str) -> list[AnswerStreamEvent]:
        return [e for e in self.events if e.event == event_type]

    def clear(self) -> None:
        self.events.clear()
