"""Server-Sent Events wire formatting.

The only place in the framework that knows about the `text/event-stream`
content type or the `event:` / `data:` line layout. Per the spec, this
module:

  * never touches a `SecurityContext` (auth happens upstream)
  * never decides which events to emit (that's the streaming service)
  * never opens a connection (that's the transport adapter)
"""

import json

from j1.integration.streaming.events import AnswerStreamEvent

SSE_CONTENT_TYPE = "text/event-stream"

# Headers that go on every SSE response. `Cache-Control: no-cache` and
# `X-Accel-Buffering: no` are widely-recommended hints for proxies (e.g.
# nginx) so they don't buffer the chunked stream. `Connection: keep-alive`
# is informational — the actual lifetime is controlled by the ASGI host.
SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def format_sse(event: AnswerStreamEvent) -> bytes:
    """Format `event` as one SSE message.

    Wire format::

        event: <event-name>
        data: <json-payload>
        \\n

    The payload JSON contains the standard `{requestId, event, data}`
    envelope so consumers always have those values without splitting
    `event:` from `data:` themselves.
    """
    payload = json.dumps(event.to_payload(), separators=(",", ":"))
    return (
        f"event: {event.event}\n"
        f"data: {payload}\n"
        "\n"
    ).encode("utf-8")
