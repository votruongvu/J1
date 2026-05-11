"""Graphify provider settings."""

import os
from collections.abc import Mapping
from dataclasses import dataclass

ENV_GRAPHIFY_ENABLED = "J1_GRAPHIFY_ENABLED"
ENV_GRAPHIFY_MODE = "J1_GRAPHIFY_MODE"
ENV_GRAPHIFY_COMMAND = "J1_GRAPHIFY_COMMAND"
ENV_GRAPHIFY_WORKDIR = "J1_GRAPHIFY_WORKDIR"
ENV_GRAPHIFY_PROCESSOR = "J1_GRAPHIFY_GRAPH_PROCESSOR"

DEFAULT_MODE = "cli"
DEFAULT_COMMAND = "graphify"
DEFAULT_WORKDIR = "./data/graphify"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class GraphifySettings:
    """Whether-and-how config for the optional Graphify provider.

 `enabled=False` means the composition root does NOT register the
 Graphify graph builder; selecting it via `J1_DEFAULT_GRAPH_PROVIDER`
 while disabled is the user error case (composition root raises a
 clear message).

 `graph_processor` is an importable callable spec (e.g.
 ``"mypkg.processors:graphify_build"``); when set, the adapter
 delegates to it via the safe class-loader. Otherwise the built-in
 stub raises `ProviderUnavailable`.
 """

    enabled: bool = False
    mode: str = DEFAULT_MODE
    command: str = DEFAULT_COMMAND
    workdir: str = DEFAULT_WORKDIR
    graph_processor: str | None = None


def load_graphify_settings(
    env: Mapping[str, str] | None = None,
) -> GraphifySettings:
    source = env if env is not None else os.environ
    return GraphifySettings(
        enabled=(source.get(ENV_GRAPHIFY_ENABLED, "").lower() in _TRUTHY),
        mode=source.get(ENV_GRAPHIFY_MODE, DEFAULT_MODE),
        command=source.get(ENV_GRAPHIFY_COMMAND, DEFAULT_COMMAND),
        workdir=source.get(ENV_GRAPHIFY_WORKDIR, DEFAULT_WORKDIR),
        graph_processor=source.get(ENV_GRAPHIFY_PROCESSOR) or None,
    )
