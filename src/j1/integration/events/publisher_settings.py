import os
from collections.abc import Mapping
from dataclasses import dataclass

from j1.errors.exceptions import ConfigError

ENV_PUBLISHER_TYPE = "J1_EVENT_PUBLISHER_TYPE"
ENV_PUBLISHER_PRODUCER = "J1_EVENT_PUBLISHER_PRODUCER"
ENV_PUBLISHER_SCHEMA_VERSION = "J1_EVENT_PUBLISHER_SCHEMA_VERSION"
ENV_INCLUDE_SENSITIVE = "J1_EVENT_INCLUDE_SENSITIVE_PAYLOADS"

PUBLISHER_TYPE_NOOP = "noop"
PUBLISHER_TYPE_MEMORY = "memory"
PUBLISHER_TYPE_BUS = "bus"

# Allowed values mirror the publishers shipped with the framework. Real
# broker adapters (kafka, rabbitmq, sqs, ...) extend this set in their
# own packages — they map their type string to their own factory.
_BUILT_IN_TYPES = frozenset({
    PUBLISHER_TYPE_NOOP,
    PUBLISHER_TYPE_MEMORY,
    PUBLISHER_TYPE_BUS,
})

_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class EventPublisherSettings:
    """Publisher configuration read from the environment.

    `publisher_type` selects which built-in publisher to construct.
    Default is `noop` — production-safe, no external integration.
    `bus` requires a deployment-supplied `ApplicationEventBus`;
    construction is the deployment's responsibility.

    `include_sensitive_payloads` is **disabled** by default. When false
    the publisher / mappers must omit potentially sensitive fields
    (full document content, full answer text, raw query content beyond
    a short summary, feedback comments). When true the deployment has
    explicitly opted in — typically only for trusted internal brokers.
    """

    publisher_type: str = PUBLISHER_TYPE_NOOP
    producer: str = "j1"
    schema_version: str = "1.0"
    include_sensitive_payloads: bool = False


def load_event_publisher_settings(
    env: Mapping[str, str] | None = None,
) -> EventPublisherSettings:
    source = env if env is not None else os.environ
    publisher_type = (
        source.get(ENV_PUBLISHER_TYPE, PUBLISHER_TYPE_NOOP).lower().strip()
        or PUBLISHER_TYPE_NOOP
    )
    if publisher_type not in _BUILT_IN_TYPES:
        # Deployments using a real broker adapter define their own
        # types — we just refuse the built-in factory's lookup.
        raise ConfigError(
            f"unsupported {ENV_PUBLISHER_TYPE} value {publisher_type!r}: "
            f"built-in types are {sorted(_BUILT_IN_TYPES)}"
        )
    return EventPublisherSettings(
        publisher_type=publisher_type,
        producer=source.get(ENV_PUBLISHER_PRODUCER, "j1") or "j1",
        schema_version=source.get(ENV_PUBLISHER_SCHEMA_VERSION, "1.0") or "1.0",
        include_sensitive_payloads=source.get(ENV_INCLUDE_SENSITIVE, "").lower()
        in _TRUTHY,
    )
