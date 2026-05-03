import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from j1.adapters.webhook.client import (
    WebhookHttpClient,
    WebhookResponse,
    WebhookTransportError,
)
from j1.integration.events.cloudevents import (
    CLOUDEVENTS_CONTENT_TYPE,
    to_cloudevent,
)
from j1.integration.events.delivery import (
    DELIVERY_STATUS_FAILED,
    DELIVERY_STATUS_RETRYING,
    DELIVERY_STATUS_SUCCEEDED,
    WebhookDeliveryRecord,
    WebhookDeliveryStore,
)
from j1.integration.events.event import ApplicationEvent
from j1.integration.events.signing import SIGNATURE_HEADER, sign_payload
from j1.integration.events.subscriptions import WebhookSubscription

_log = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"
EVENT_ID_HEADER = "X-KB-Event-Id"
EVENT_TYPE_HEADER = "X-KB-Event-Type"


class WebhookDeliveryService:
    """HTTP delivery with HMAC signing, exponential backoff, and per-attempt logging.

    Synchronous by design — the `WebhookEventSubscriber` runs `deliver`
    in a worker pool so publication never blocks. `deliver` never raises:
    every outcome (success, retry exhausted, transport error) becomes a
    `WebhookDeliveryRecord` written to the store. That isolation is what
    keeps webhook failures from breaking core ingestion.
    """

    def __init__(
        self,
        client: WebhookHttpClient,
        store: WebhookDeliveryStore,
        *,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        delivery_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._client = client
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleeper = sleeper or time.sleep
        self._delivery_id_factory = delivery_id_factory or (lambda: uuid.uuid4().hex)

    def deliver(
        self,
        subscription: WebhookSubscription,
        event: ApplicationEvent,
    ) -> WebhookDeliveryRecord:
        """Deliver `event` to `subscription`. Returns the *final* record.

        Per-attempt records are appended to the store as the retry loop
        progresses; the returned record is the last one (success or
        terminal failure).
        """
        delivery_id = self._delivery_id_factory()
        body = json.dumps(to_cloudevent(event)).encode("utf-8")
        headers = self._build_headers(subscription, event, body)

        delay = subscription.retry_initial_delay_seconds
        last_record: WebhookDeliveryRecord | None = None

        for attempt in range(1, subscription.retry_max_attempts + 1):
            attempted_at = self._clock()
            started = time.monotonic()
            try:
                response = self._client.post(
                    subscription.url,
                    body=body,
                    headers=headers,
                    timeout=subscription.timeout_seconds,
                )
            except WebhookTransportError as exc:
                last_record = self._record_failure(
                    delivery_id, subscription, event, attempt,
                    attempted_at, started, error=str(exc),
                )
            else:
                last_record = self._record_response(
                    delivery_id, subscription, event, attempt,
                    attempted_at, started, response,
                )

            self._store.append(last_record)
            if last_record.status == DELIVERY_STATUS_SUCCEEDED:
                return last_record
            if attempt < subscription.retry_max_attempts:
                self._sleeper(delay)
                delay = min(
                    delay * subscription.retry_backoff,
                    subscription.retry_max_delay_seconds,
                )

        # All attempts exhausted — promote the last "retrying" record to
        # a terminal "failed" status so consumers reading the log can see
        # a clear final outcome.
        if last_record is not None and last_record.status != DELIVERY_STATUS_SUCCEEDED:
            terminal = _with_status(last_record, DELIVERY_STATUS_FAILED)
            self._store.append(terminal)
            return terminal
        # Shouldn't reach here; defensive fallback.
        return last_record  # type: ignore[return-value]

    def _build_headers(
        self,
        subscription: WebhookSubscription,
        event: ApplicationEvent,
        body: bytes,
    ) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": CLOUDEVENTS_CONTENT_TYPE,
            EVENT_ID_HEADER: event.id,
            EVENT_TYPE_HEADER: event.type,
        }
        if event.correlation_id:
            headers[REQUEST_ID_HEADER] = event.correlation_id
        signature = sign_payload(subscription.secret, body)
        if signature:
            headers[SIGNATURE_HEADER] = signature
        # Per-subscription headers override defaults — useful for auth
        # tokens at the receiving side.
        headers.update(subscription.headers)
        return headers

    def _record_response(
        self,
        delivery_id: str,
        subscription: WebhookSubscription,
        event: ApplicationEvent,
        attempt: int,
        attempted_at: datetime,
        started: float,
        response: WebhookResponse,
    ) -> WebhookDeliveryRecord:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        success = 200 <= response.status_code < 300
        if success:
            status = DELIVERY_STATUS_SUCCEEDED
            error = None
        else:
            status = (
                DELIVERY_STATUS_RETRYING
                if attempt < subscription.retry_max_attempts
                else DELIVERY_STATUS_FAILED
            )
            error = f"http {response.status_code}"
        return WebhookDeliveryRecord(
            delivery_id=delivery_id,
            subscription_id=subscription.id,
            event_id=event.id,
            event_type=event.type,
            attempted_at=attempted_at,
            attempt=attempt,
            status=status,
            response_status=response.status_code,
            error=error,
            elapsed_ms=elapsed_ms,
            tenant_id=event.tenant_id,
            correlation_id=event.correlation_id,
        )

    def _record_failure(
        self,
        delivery_id: str,
        subscription: WebhookSubscription,
        event: ApplicationEvent,
        attempt: int,
        attempted_at: datetime,
        started: float,
        *,
        error: str,
    ) -> WebhookDeliveryRecord:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        status = (
            DELIVERY_STATUS_RETRYING
            if attempt < subscription.retry_max_attempts
            else DELIVERY_STATUS_FAILED
        )
        return WebhookDeliveryRecord(
            delivery_id=delivery_id,
            subscription_id=subscription.id,
            event_id=event.id,
            event_type=event.type,
            attempted_at=attempted_at,
            attempt=attempt,
            status=status,
            response_status=None,
            error=error,
            elapsed_ms=elapsed_ms,
            tenant_id=event.tenant_id,
            correlation_id=event.correlation_id,
        )


def _with_status(
    record: WebhookDeliveryRecord, status: str
) -> WebhookDeliveryRecord:
    return WebhookDeliveryRecord(
        delivery_id=record.delivery_id,
        subscription_id=record.subscription_id,
        event_id=record.event_id,
        event_type=record.event_type,
        attempted_at=record.attempted_at,
        attempt=record.attempt,
        status=status,
        response_status=record.response_status,
        error=record.error,
        elapsed_ms=record.elapsed_ms,
        tenant_id=record.tenant_id,
        correlation_id=record.correlation_id,
        metadata=dict(record.metadata),
    )
