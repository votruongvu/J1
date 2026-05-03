"""Helpers that turn REST handler outcomes into ApplicationEvents.

This is the bridge between the security layer and the event/webhook layer:
each helper takes the active `SecurityContext` and the handler-relevant
inputs, builds a transport-neutral `ApplicationEvent`, and publishes it on
the bus. The bus' subscriber list (typically including a
`WebhookEventSubscriber`) decides where it goes from there.

Design choices:
- All helpers accept `bus: ApplicationEventBus | None`. When the bus is
  `None`, the helpers are no-ops — that preserves the existing handlers'
  behaviour for deployments that haven't opted into event publication.
- Publication happens *after* the handler's primary work succeeds, so a
  failed event publication can never affect the response.
- `actor` and `auth_type` come from the `SecurityContext`. Anonymous
  contexts emit events with `actor=None`, which signals "system" to
  receivers.
- `correlation_id` carries the request's X-Request-Id so receivers can
  tie an inbound HTTP call to its outbound webhook delivery.
"""

import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from j1.integration.events import (
    EVENT_ANSWER_GENERATED,
    EVENT_DOCUMENT_INGESTION_STARTED,
    EVENT_DOCUMENT_UPLOADED,
    EVENT_QUERY_COMPLETED,
    ApplicationEvent,
    ApplicationEventBus,
)
from j1.integration.security import SecurityContext

_log = logging.getLogger(__name__)

REST_EVENT_SOURCE = "j1/rest"


def publish_event(
    bus: ApplicationEventBus | None,
    *,
    event_type: str,
    security: SecurityContext,
    request_id: str | None,
    tenant_id: str | None,
    subject: str | None,
    data: Mapping[str, Any] | None = None,
    source: str = REST_EVENT_SOURCE,
) -> None:
    """Build and publish one `ApplicationEvent`. No-op when `bus is None`.

    Wraps publication in a try/except so a misconfigured bus or a
    misbehaving subscriber cannot affect the calling handler.
    """
    if bus is None:
        return
    actor = None if security.is_anonymous else security.subject
    auth_type = None if security.is_anonymous else security.auth_type
    event = ApplicationEvent(
        id=uuid.uuid4().hex,
        type=event_type,
        occurred_at=datetime.now(timezone.utc),
        source=source,
        subject=subject,
        tenant_id=tenant_id,
        correlation_id=request_id,
        actor=actor,
        auth_type=auth_type,
        data=dict(data) if data else {},
    )
    try:
        bus.publish(event)
    except Exception:
        # Belt-and-suspenders: ApplicationEventBus.publish already swallows
        # subscriber failures, but if the bus itself is broken we still
        # don't want to surface that to the HTTP caller.
        _log.exception(
            "failed to publish event %s/%s from REST adapter",
            event.type, event.id,
        )


def publish_document_uploaded(
    bus: ApplicationEventBus | None,
    *,
    security: SecurityContext,
    request_id: str | None,
    tenant_id: str,
    document_id: str,
    checksum: str,
    file_size: int,
    mime_type: str | None = None,
    duplicate: bool = False,
) -> None:
    publish_event(
        bus,
        event_type=EVENT_DOCUMENT_UPLOADED,
        security=security,
        request_id=request_id,
        tenant_id=tenant_id,
        subject=document_id,
        data={
            "documentId": document_id,
            "checksum": checksum,
            "fileSize": file_size,
            "mimeType": mime_type,
            "duplicate": duplicate,
        },
    )


def publish_document_ingestion_started(
    bus: ApplicationEventBus | None,
    *,
    security: SecurityContext,
    request_id: str | None,
    tenant_id: str,
    job_id: str,
    document_id: str | None = None,
    project_wide: bool = False,
) -> None:
    publish_event(
        bus,
        event_type=EVENT_DOCUMENT_INGESTION_STARTED,
        security=security,
        request_id=request_id,
        tenant_id=tenant_id,
        subject=document_id or job_id,
        data={
            "jobId": job_id,
            "documentId": document_id,
            "projectWide": project_wide,
        },
    )


def publish_query_completed(
    bus: ApplicationEventBus | None,
    *,
    security: SecurityContext,
    request_id: str | None,
    tenant_id: str,
    query: str,
    result_count: int,
    surface: str,
) -> None:
    """Emit `query.completed`. `surface` is `"search"` or `"retrieve"`."""
    publish_event(
        bus,
        event_type=EVENT_QUERY_COMPLETED,
        security=security,
        request_id=request_id,
        tenant_id=tenant_id,
        subject=None,
        data={
            "query": query,
            "resultCount": result_count,
            "surface": surface,
        },
    )


def publish_answer_generated(
    bus: ApplicationEventBus | None,
    *,
    security: SecurityContext,
    request_id: str | None,
    tenant_id: str,
    question: str,
    mode_used: str,
    citation_count: int,
    confidence: float,
    review_required: bool,
) -> None:
    publish_event(
        bus,
        event_type=EVENT_ANSWER_GENERATED,
        security=security,
        request_id=request_id,
        tenant_id=tenant_id,
        subject=None,
        data={
            "question": question,
            "modeUsed": mode_used,
            "citationCount": citation_count,
            "confidence": confidence,
            "reviewRequired": review_required,
        },
    )
