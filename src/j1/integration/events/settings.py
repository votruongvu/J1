import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from j1.errors.exceptions import ConfigError
from j1.integration.events.subscriptions import WebhookSubscription

ENV_WEBHOOK_ENABLED = "J1_WEBHOOK_ENABLED"
ENV_WEBHOOK_SUBSCRIPTIONS = "J1_WEBHOOK_SUBSCRIPTIONS"
ENV_WEBHOOK_SUBSCRIPTIONS_FILE = "J1_WEBHOOK_SUBSCRIPTIONS_FILE"
ENV_WEBHOOK_DEFAULT_TIMEOUT = "J1_WEBHOOK_DEFAULT_TIMEOUT_SECONDS"
ENV_WEBHOOK_DEFAULT_MAX_ATTEMPTS = "J1_WEBHOOK_DEFAULT_MAX_ATTEMPTS"

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_INITIAL_DELAY_SECONDS = 1.0
DEFAULT_BACKOFF = 2.0
DEFAULT_MAX_DELAY_SECONDS = 60.0

_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class WebhookSettings:
    """Webhook configuration loaded from the environment.

 `subscriptions` is the resolved list of static `WebhookSubscription`
 records. Deployments wanting an API-managed registry replace this
 with their own `WebhookSubscriptionRegistry` implementation and
 ignore these settings.
 """

    enabled: bool = False
    subscriptions: tuple[WebhookSubscription, ...] = field(default_factory=tuple)
    default_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    default_max_attempts: int = DEFAULT_MAX_ATTEMPTS


def load_webhook_settings(
    env: Mapping[str, str] | None = None,
) -> WebhookSettings:
    source = env if env is not None else os.environ
    enabled = source.get(ENV_WEBHOOK_ENABLED, "").lower() in _TRUTHY

    timeout = _read_float(
        source, ENV_WEBHOOK_DEFAULT_TIMEOUT, DEFAULT_TIMEOUT_SECONDS
    )
    max_attempts = _read_int(
        source, ENV_WEBHOOK_DEFAULT_MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS
    )
    subs = _load_subscriptions(source, timeout, max_attempts)
    return WebhookSettings(
        enabled=enabled,
        subscriptions=subs,
        default_timeout_seconds=timeout,
        default_max_attempts=max_attempts,
    )


def _read_float(source: Mapping[str, str], key: str, default: float) -> float:
    raw = source.get(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc


def _read_int(source: Mapping[str, str], key: str, default: int) -> int:
    raw = source.get(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _load_subscriptions(
    source: Mapping[str, str],
    default_timeout: float,
    default_max_attempts: int,
) -> tuple[WebhookSubscription, ...]:
    inline = source.get(ENV_WEBHOOK_SUBSCRIPTIONS)
    file_path = source.get(ENV_WEBHOOK_SUBSCRIPTIONS_FILE)
    if inline and file_path:
        raise ConfigError(
            f"set only one of {ENV_WEBHOOK_SUBSCRIPTIONS} or "
            f"{ENV_WEBHOOK_SUBSCRIPTIONS_FILE}"
        )
    raw: str | None = None
    if inline:
        raw = inline
    elif file_path:
        try:
            raw = Path(file_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(
                f"failed to read webhook subscriptions file {file_path!r}: {exc}"
            ) from exc
    if raw is None:
        return ()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"webhook subscriptions must be valid JSON: {exc}"
        ) from exc
    if not isinstance(data, list):
        raise ConfigError(
            "webhook subscriptions JSON must be a list of subscription objects"
        )
    return tuple(
        _subscription_from_dict(item, default_timeout, default_max_attempts)
        for item in data
    )


def _subscription_from_dict(
    item: object,
    default_timeout: float,
    default_max_attempts: int,
) -> WebhookSubscription:
    if not isinstance(item, Mapping):
        raise ConfigError("webhook subscription entry must be an object")
    sub_id = item.get("id")
    url = item.get("url")
    event_types_raw = item.get("event_types") or item.get("eventTypes") or []
    if not isinstance(sub_id, str) or not sub_id:
        raise ConfigError("webhook subscription is missing a non-empty 'id'")
    if not isinstance(url, str) or not url:
        raise ConfigError(
            f"webhook subscription {sub_id!r} is missing a non-empty 'url'"
        )
    if not isinstance(event_types_raw, list) or not all(
        isinstance(t, str) for t in event_types_raw
    ):
        raise ConfigError(
            f"webhook subscription {sub_id!r} 'event_types' must be a list of strings"
        )
    secret = item.get("secret", "")
    if not isinstance(secret, str):
        raise ConfigError(
            f"webhook subscription {sub_id!r} 'secret' must be a string"
        )
    tenant_id = item.get("tenant_id") or item.get("tenantId")
    if tenant_id is not None and not isinstance(tenant_id, str):
        raise ConfigError(
            f"webhook subscription {sub_id!r} 'tenant_id' must be a string or null"
        )
    enabled = item.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(
            f"webhook subscription {sub_id!r} 'enabled' must be boolean"
        )
    headers_raw = item.get("headers", {})
    if not isinstance(headers_raw, Mapping):
        raise ConfigError(
            f"webhook subscription {sub_id!r} 'headers' must be an object"
        )

    return WebhookSubscription(
        id=sub_id,
        url=url,
        event_types=frozenset(event_types_raw),
        secret=secret,
        enabled=enabled,
        tenant_id=tenant_id,
        timeout_seconds=float(item.get("timeout_seconds", default_timeout)),
        retry_max_attempts=int(item.get("retry_max_attempts", default_max_attempts)),
        retry_initial_delay_seconds=float(
            item.get("retry_initial_delay_seconds", DEFAULT_INITIAL_DELAY_SECONDS)
        ),
        retry_backoff=float(item.get("retry_backoff", DEFAULT_BACKOFF)),
        retry_max_delay_seconds=float(
            item.get("retry_max_delay_seconds", DEFAULT_MAX_DELAY_SECONDS)
        ),
        headers={str(k): str(v) for k, v in headers_raw.items()},
    )
