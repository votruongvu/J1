import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

WEBHOOK_DELIVERY_FILENAME = "webhook_deliveries.jsonl"

DELIVERY_STATUS_SUCCEEDED = "succeeded"
DELIVERY_STATUS_FAILED = "failed"
DELIVERY_STATUS_RETRYING = "retrying"
DELIVERY_STATUS_SKIPPED = "skipped"


@dataclass(frozen=True)
class WebhookDeliveryRecord:
    """Per-attempt delivery log entry. Append-only."""

    delivery_id: str
    subscription_id: str
    event_id: str
    event_type: str
    attempted_at: datetime
    attempt: int
    status: str
    response_status: int | None = None
    error: str | None = None
    elapsed_ms: int = 0
    tenant_id: str | None = None
    correlation_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class WebhookDeliveryStore(Protocol):
    """Append-only sink for delivery attempts.

    Implementations must be safe for sequential writes from the delivery
    service. Concurrent writers are out of scope (mirrors the existing
    JSONL audit / cost sinks).
    """

    def append(self, record: WebhookDeliveryRecord) -> None: ...

    def list_all(self) -> list[WebhookDeliveryRecord]: ...


class JsonlWebhookDeliveryStore:
    """Writes one JSON line per delivery attempt under `path`.

    `path` should be a stable filesystem location — typically inside the
    workspace's `runtime/` area. The file is created lazily on first
    write.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: WebhookDeliveryRecord) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_record_to_dict(record), default=str))
            fh.write("\n")

    def list_all(self) -> list[WebhookDeliveryRecord]:
        if not self._path.exists():
            return []
        out: list[WebhookDeliveryRecord] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            out.append(_record_from_dict(json.loads(line)))
        return out

    def list_for_subscription(self, subscription_id: str) -> list[WebhookDeliveryRecord]:
        return [r for r in self.list_all() if r.subscription_id == subscription_id]


def _record_to_dict(record: WebhookDeliveryRecord) -> dict:
    return {
        "delivery_id": record.delivery_id,
        "subscription_id": record.subscription_id,
        "event_id": record.event_id,
        "event_type": record.event_type,
        "attempted_at": record.attempted_at.isoformat(),
        "attempt": record.attempt,
        "status": record.status,
        "response_status": record.response_status,
        "error": record.error,
        "elapsed_ms": record.elapsed_ms,
        "tenant_id": record.tenant_id,
        "correlation_id": record.correlation_id,
        "metadata": dict(record.metadata),
    }


def _record_from_dict(data: dict) -> WebhookDeliveryRecord:
    return WebhookDeliveryRecord(
        delivery_id=data["delivery_id"],
        subscription_id=data["subscription_id"],
        event_id=data["event_id"],
        event_type=data["event_type"],
        attempted_at=datetime.fromisoformat(data["attempted_at"]),
        attempt=int(data["attempt"]),
        status=data["status"],
        response_status=data.get("response_status"),
        error=data.get("error"),
        elapsed_ms=int(data.get("elapsed_ms") or 0),
        tenant_id=data.get("tenant_id"),
        correlation_id=data.get("correlation_id"),
        metadata=dict(data.get("metadata") or {}),
    )


class InMemoryWebhookDeliveryStore:
    """Bounded in-memory sink — handy for tests and ephemeral deployments."""

    def __init__(self, records: Iterable[WebhookDeliveryRecord] | None = None) -> None:
        self._records: list[WebhookDeliveryRecord] = list(records or ())

    def append(self, record: WebhookDeliveryRecord) -> None:
        self._records.append(record)

    def list_all(self) -> list[WebhookDeliveryRecord]:
        return list(self._records)
