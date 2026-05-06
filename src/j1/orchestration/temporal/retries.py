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


# Error types Temporal must NOT retry. These are deterministic failures
# where retry cannot help — re-running the same activity with the same
# input will fail the same way. Retrying them just burns the 5-attempt
# budget before surfacing the real cause.
#
# Names match the conventions used in the codebase:
#   * `ApplicationError.type=` strings (J1_INGEST_*) — raised by
#     workflows / activities when a required step fails.
#   * Bare exception class names — Temporal compares
#     `exception.__class__.__name__` against this list when no typed
#     `ApplicationError` was raised.
#
# Provider / network / 5xx errors are deliberately NOT here — those
# are transient and benefit from retry.
_NON_RETRYABLE_ERROR_TYPES: tuple[str, ...] = (
    # Typed ApplicationError emissions from J1 workflows / activities.
    "J1_INGEST_REQUIRED_STEP_FAILED",
    # Validation / config / lookup failures: deterministic by nature.
    "ConfigError",
    "ValidationError",
    "DocumentNotFoundError",
    "UnknownProcessorError",
    "LLMConfigError",
    # Parser / source-document failures. These are deterministic with
    # respect to the input bytes — re-running the same parser against
    # the same source produces the same failure. Retrying just wastes
    # parse time (which can be many minutes per attempt for MinerU)
    # without any chance of recovery. Names are conventional;
    # parser implementations should raise `ApplicationError(type=...)`
    # with one of these to opt their permanent failures out of retry.
    "UnsupportedFileType",
    "CorruptedDocument",
    "ParserValidationError",
    "MissingSourceObject",
)


DEFAULT_RETRY = RetryPolicySpec(
    non_retryable_error_types=_NON_RETRYABLE_ERROR_TYPES,
)


# Compile-stage retry policy. Compile is the most expensive activity
# in the pipeline (MinerU parse can take many minutes per document),
# so we bound retries tightly: one second-attempt covers transient
# infrastructure blips (worker crash, brief network failure) without
# multiplying parse cost. Combined with the heartbeat ticker, a
# successful compile shouldn't need to retry at all in normal
# operation; this policy is the safety net.
COMPILE_RETRY = RetryPolicySpec(
    maximum_attempts=2,
    non_retryable_error_types=_NON_RETRYABLE_ERROR_TYPES,
)
