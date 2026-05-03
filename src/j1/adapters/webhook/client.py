from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


class WebhookTransportError(Exception):
    """Raised by clients on connect/timeout/network failures.

    The delivery service treats this as retryable; HTTP responses with
    non-2xx status codes are reported via `WebhookResponse.status_code`
    instead of raised.
    """


@dataclass(frozen=True)
class WebhookResponse:
    status_code: int
    body: str = ""


class WebhookHttpClient(Protocol):
    """Pluggable HTTP transport for webhook delivery.

    Implementations must:
      * raise `WebhookTransportError` for transport-level failures
        (DNS, connect, timeout) — those are retryable
      * return a `WebhookResponse` for any HTTP response, including
        non-2xx. The delivery service decides what to do with the
        status code.
    """

    def post(
        self,
        url: str,
        *,
        body: bytes,
        headers: Mapping[str, str],
        timeout: float,
    ) -> WebhookResponse: ...


class HttpxWebhookClient:
    """Default `httpx`-backed client.

    `httpx` is already a dev dependency (used by FastAPI's TestClient).
    Production deployments wanting a different stack supply their own
    client implementing `WebhookHttpClient`.
    """

    def __init__(self) -> None:
        # Lazy import so `j1.adapters.webhook` can be imported in
        # environments that don't have httpx installed (the user can
        # still wire a custom WebhookHttpClient).
        import httpx  # noqa: F401

    def post(
        self,
        url: str,
        *,
        body: bytes,
        headers: Mapping[str, str],
        timeout: float,
    ) -> WebhookResponse:
        import httpx

        try:
            response = httpx.post(
                url, content=body, headers=dict(headers), timeout=timeout
            )
        except httpx.TimeoutException as exc:
            raise WebhookTransportError(f"timeout calling {url}: {exc}") from exc
        except httpx.TransportError as exc:
            raise WebhookTransportError(
                f"transport error calling {url}: {exc}"
            ) from exc
        return WebhookResponse(
            status_code=response.status_code,
            body=response.text[:2000],  # cap so we don't log huge bodies
        )
