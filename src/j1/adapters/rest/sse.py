"""Server-Sent Events wire formatting helpers.

The only place in the framework that knows about the
``text/event-stream`` content type. Per the spec, this module:

 * never touches a ``SecurityContext`` (auth happens upstream)
 * never decides which events to emit
 * never opens a connection

The legacy ``/answer`` streaming surface (and its
``AnswerStreamEvent`` / ``format_sse`` helpers) was removed when the
SmartQueryOrchestrator rolled out. The constants below are kept for
the surviving SSE consumer — ``GET /ingestion-runs/{id}/events/stream``.
"""

SSE_CONTENT_TYPE = "text/event-stream"

# Headers that go on every SSE response. `Cache-Control: no-cache` and
# `X-Accel-Buffering: no` are widely-recommended hints for proxies
# (e.g. nginx) so they don't buffer the chunked stream.
SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
