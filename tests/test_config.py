from pathlib import Path

import pytest

from j1.config.settings import (
    DEFAULT_DATA_ROOT,
    ENV_DATA_ROOT,
    Settings,
    load_settings,
)
from j1.errors.exceptions import ConfigError


def test_default_data_root():
    settings = load_settings(env={})
    assert settings.data_root == DEFAULT_DATA_ROOT


def test_env_override():
    settings = load_settings(env={ENV_DATA_ROOT: "/var/lib/j1"})
    assert settings.data_root == Path("/var/lib/j1")


def test_relative_data_root_rejected():
    with pytest.raises(ConfigError):
        Settings(data_root=Path("relative/path"))


def test_non_path_data_root_rejected():
    with pytest.raises(ConfigError):
        Settings(data_root="/data/j1")  # type: ignore[arg-type]
