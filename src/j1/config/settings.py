import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from j1.errors.exceptions import ConfigError

DEFAULT_DATA_ROOT = Path("/data/j1")
ENV_DATA_ROOT = "J1_DATA_ROOT"


@dataclass(frozen=True)
class Settings:
    data_root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.data_root, Path):
            raise ConfigError("data_root must be a pathlib.Path")
        if not self.data_root.is_absolute():
            raise ConfigError(f"data_root must be absolute: {self.data_root}")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    source = env if env is not None else os.environ
    raw = source.get(ENV_DATA_ROOT, str(DEFAULT_DATA_ROOT))
    return Settings(data_root=Path(raw))
