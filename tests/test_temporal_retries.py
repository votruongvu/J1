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


# ---- DEFAULT_RETRY classifies known-deterministic failures as
# non-retryable so they don't burn the 5-attempt budget before the
# real cause surfaces.


def test_default_retry_excludes_required_step_failed_from_retry():
    """`J1_INGEST_REQUIRED_STEP_FAILED` is the type the workflow raises
    when a required ingestion step (compile / index / etc.) failed.
    Retrying it is meaningless — the step's input is unchanged."""
    policy = DEFAULT_RETRY.to_temporal()
    assert "J1_INGEST_REQUIRED_STEP_FAILED" in policy.non_retryable_error_types


def test_default_retry_excludes_lookup_errors_from_retry():
    """Missing document / artifact / processor kind = caller bug.
    Retrying re-fails identically; surface the cause immediately."""
    policy = DEFAULT_RETRY.to_temporal()
    for name in ("DocumentNotFoundError", "UnknownProcessorError"):
        assert name in policy.non_retryable_error_types


def test_default_retry_excludes_validation_and_config_errors():
    """Operator-reachable bugs (config typo, schema validation
    failure) are deterministic — retry doesn't help."""
    policy = DEFAULT_RETRY.to_temporal()
    for name in ("ConfigError", "ValidationError", "LLMConfigError"):
        assert name in policy.non_retryable_error_types


def test_default_retry_keeps_transient_network_errors_retryable():
    """Counter-test: actually-transient errors (connection blips,
    HTTP timeouts, LLM endpoint 5xx) MUST stay retryable so we don't
    lose resilience. Note `LLMProviderUnavailable` covers the HTTP
    transient path; the related `ProviderUnavailable` covers
    permanent vendor-init failures and IS non-retryable (see
    `test_default_retry_excludes_provider_init_failures`)."""
    policy = DEFAULT_RETRY.to_temporal()
    for transient_name in (
        "ConnectionError",
        "TimeoutError",
        "LLMProviderUnavailable",
        "HTTPError",
    ):
        assert transient_name not in policy.non_retryable_error_types


def test_default_retry_excludes_provider_init_failures():
    """Regression for C7: `ProviderUnavailable` is raised for
    deterministic vendor-init failures (vendor module not installed,
    LibreOffice binary missing, persistent loop dead). Retrying with
    the same env burns the budget for nothing."""
    policy = DEFAULT_RETRY.to_temporal()
    assert "ProviderUnavailable" in policy.non_retryable_error_types


def test_default_retry_excludes_deterministic_llm_errors():
    """Regression for C7: context overflow is a pure function of
    the prompt; missing LLM role is a config error. Both reproduce
    on every retry — exclude from the budget."""
    policy = DEFAULT_RETRY.to_temporal()
    for name in ("LLMContextOverflowError", "LLMRoleNotRegistered"):
        assert name in policy.non_retryable_error_types
