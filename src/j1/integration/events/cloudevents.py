from typing import Any

from j1.integration.events.event import ApplicationEvent

CLOUDEVENTS_SPEC_VERSION = "1.0"
CLOUDEVENTS_CONTENT_TYPE = "application/cloudevents+json"
DATA_CONTENT_TYPE = "application/json"

# CloudEvents 1.0 extension attribute names: lowercase letters/digits only,
# max 20 characters. Mapping our internal fields to compliant attribute
# names so consumers can treat them as first-class CloudEvents extensions.
EXTENSION_TENANT = "kbtenantid"
EXTENSION_CORRELATION = "kbcorrelationid"
EXTENSION_ACTOR = "kbactor"
EXTENSION_AUTH_TYPE = "kbauthtype"


def to_cloudevent(event: ApplicationEvent) -> dict[str, Any]:
    """Format an `ApplicationEvent` as a CloudEvents 1.0 JSON envelope.

    Returns a plain dict — no JSON encoding here, leaving that to the
    transport adapter so it can choose its own serialiser/encoding.
    Optional fields with no value are omitted (per the spec, optional
    attributes SHOULD NOT be present when empty).
    """
    envelope: dict[str, Any] = {
        "specversion": CLOUDEVENTS_SPEC_VERSION,
        "type": event.type,
        "source": event.source,
        "id": event.id,
        "time": event.occurred_at.isoformat(),
        "datacontenttype": DATA_CONTENT_TYPE,
        "data": dict(event.data),
    }
    if event.subject:
        envelope["subject"] = event.subject
    if event.tenant_id:
        envelope[EXTENSION_TENANT] = event.tenant_id
    if event.correlation_id:
        envelope[EXTENSION_CORRELATION] = event.correlation_id
    if event.actor:
        envelope[EXTENSION_ACTOR] = event.actor
    if event.auth_type:
        envelope[EXTENSION_AUTH_TYPE] = event.auth_type
    return envelope
