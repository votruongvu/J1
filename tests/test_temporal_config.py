import pytest

from j1.errors.exceptions import ConfigError
from j1.orchestration.temporal.config import (
    DEFAULT_NAMESPACE,
    DEFAULT_TARGET,
    DEFAULT_TASK_QUEUE,
    ENV_API_KEY,
    ENV_NAMESPACE,
    ENV_TARGET,
    ENV_TASK_QUEUE,
    ENV_TLS,
    TemporalSettings,
    load_temporal_settings,
)


def test_defaults():
    settings = load_temporal_settings(env={})
    assert settings.target == DEFAULT_TARGET
    assert settings.namespace == DEFAULT_NAMESPACE
    assert settings.task_queue == DEFAULT_TASK_QUEUE
    assert settings.tls is False
    assert settings.api_key is None


def test_env_override():
    settings = load_temporal_settings(
        env={
            ENV_TARGET: "temporal.example:7233",
            ENV_NAMESPACE: "j1-prod",
            ENV_TASK_QUEUE: "j1-knowledge",
            ENV_TLS: "true",
            ENV_API_KEY: "secret",
        }
    )
    assert settings.target == "temporal.example:7233"
    assert settings.namespace == "j1-prod"
    assert settings.task_queue == "j1-knowledge"
    assert settings.tls is True
    assert settings.api_key == "secret"


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "Yes"])
def test_tls_truthy_values(value):
    assert load_temporal_settings(env={ENV_TLS: value}).tls is True


@pytest.mark.parametrize("value", ["0", "false", "no", "", "off"])
def test_tls_falsy_values(value):
    assert load_temporal_settings(env={ENV_TLS: value}).tls is False


def test_empty_target_rejected():
    with pytest.raises(ConfigError):
        TemporalSettings(target="")


def test_empty_namespace_rejected():
    with pytest.raises(ConfigError):
        TemporalSettings(namespace="")


def test_empty_task_queue_rejected():
    with pytest.raises(ConfigError):
        TemporalSettings(task_queue="")


def test_task_queue_is_configurable():
    settings = load_temporal_settings(env={ENV_TASK_QUEUE: "custom-queue"})
    assert settings.task_queue == "custom-queue"
