import os
from collections.abc import Mapping
from dataclasses import dataclass

from j1.errors.exceptions import ConfigError

DEFAULT_TARGET = "localhost:7233"
DEFAULT_NAMESPACE = "default"
# Default task queue. The dev `.env.example` ships the same value
# as `J1_TEMPORAL_TASK_QUEUE`, so a worker started without an .env
# still lands on the queue the API submits to.
DEFAULT_TASK_QUEUE = "j1-processing"

ENV_TARGET = "J1_TEMPORAL_TARGET"
ENV_NAMESPACE = "J1_TEMPORAL_NAMESPACE"
ENV_TASK_QUEUE = "J1_TEMPORAL_TASK_QUEUE"
ENV_TLS = "J1_TEMPORAL_TLS"
ENV_API_KEY = "J1_TEMPORAL_API_KEY"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class TemporalSettings:
    target: str = DEFAULT_TARGET
    namespace: str = DEFAULT_NAMESPACE
    task_queue: str = DEFAULT_TASK_QUEUE
    tls: bool = False
    api_key: str | None = None

    def __post_init__(self) -> None:
        if not self.target:
            raise ConfigError("temporal target must not be empty")
        if not self.namespace:
            raise ConfigError("temporal namespace must not be empty")
        if not self.task_queue:
            raise ConfigError("temporal task_queue must not be empty")


def load_temporal_settings(env: Mapping[str, str] | None = None) -> TemporalSettings:
    source = env if env is not None else os.environ
    return TemporalSettings(
        target=source.get(ENV_TARGET, DEFAULT_TARGET),
        namespace=source.get(ENV_NAMESPACE, DEFAULT_NAMESPACE),
        task_queue=source.get(ENV_TASK_QUEUE, DEFAULT_TASK_QUEUE),
        tls=source.get(ENV_TLS, "").lower() in _TRUTHY,
        api_key=source.get(ENV_API_KEY) or None,
    )
