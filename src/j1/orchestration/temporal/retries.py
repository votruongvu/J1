from dataclasses import dataclass, field
from datetime import timedelta

from temporalio.common import RetryPolicy

from j1.errors.exceptions import ConfigError


@dataclass(frozen=True)
class RetryPolicySpec:
    initial_interval_seconds: float = 1.0
    backoff_coefficient: float = 2.0
    maximum_interval_seconds: float = 60.0
    maximum_attempts: int = 5
    non_retryable_error_types: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.initial_interval_seconds <= 0:
            raise ConfigError("initial_interval_seconds must be > 0")
        if self.backoff_coefficient < 1.0:
            raise ConfigError("backoff_coefficient must be >= 1.0")
        if self.maximum_interval_seconds < self.initial_interval_seconds:
            raise ConfigError(
                "maximum_interval_seconds must be >= initial_interval_seconds"
            )
        if self.maximum_attempts < 0:
            raise ConfigError("maximum_attempts must be >= 0")

    def to_temporal(self) -> RetryPolicy:
        return RetryPolicy(
            initial_interval=timedelta(seconds=self.initial_interval_seconds),
            backoff_coefficient=self.backoff_coefficient,
            maximum_interval=timedelta(seconds=self.maximum_interval_seconds),
            maximum_attempts=self.maximum_attempts,
            non_retryable_error_types=list(self.non_retryable_error_types),
        )


DEFAULT_RETRY = RetryPolicySpec()
