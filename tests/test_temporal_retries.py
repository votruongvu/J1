from datetime import timedelta

import pytest
from temporalio.common import RetryPolicy

from j1.errors.exceptions import ConfigError
from j1.orchestration.temporal.retries import DEFAULT_RETRY, RetryPolicySpec


def test_to_temporal_returns_retry_policy():
    spec = RetryPolicySpec(
        initial_interval_seconds=2.0,
        backoff_coefficient=3.0,
        maximum_interval_seconds=120.0,
        maximum_attempts=10,
        non_retryable_error_types=("ValueError", "TypeError"),
    )
    policy = spec.to_temporal()
    assert isinstance(policy, RetryPolicy)
    assert policy.initial_interval == timedelta(seconds=2.0)
    assert policy.backoff_coefficient == 3.0
    assert policy.maximum_interval == timedelta(seconds=120.0)
    assert policy.maximum_attempts == 10
    assert policy.non_retryable_error_types == ["ValueError", "TypeError"]


def test_default_retry_is_usable():
    policy = DEFAULT_RETRY.to_temporal()
    assert policy.maximum_attempts == 5
    assert policy.initial_interval == timedelta(seconds=1.0)


def test_zero_initial_interval_rejected():
    with pytest.raises(ConfigError):
        RetryPolicySpec(initial_interval_seconds=0)


def test_backoff_below_one_rejected():
    with pytest.raises(ConfigError):
        RetryPolicySpec(backoff_coefficient=0.5)


def test_max_below_initial_rejected():
    with pytest.raises(ConfigError):
        RetryPolicySpec(initial_interval_seconds=10, maximum_interval_seconds=5)


def test_negative_attempts_rejected():
    with pytest.raises(ConfigError):
        RetryPolicySpec(maximum_attempts=-1)
