import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from j1.connectors.graph.config import GraphConfig
from j1.cost.breakdown import CostBreakdown
from j1.errors.exceptions import GraphConfigError, GraphExecutionError


@dataclass(frozen=True)
class GraphArtifactInfo:
    artifact_id: str
    kind: str
    file_name: str
    byte_size: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphAdapterRequest:
    workspace_dir: Path
    corpus_dir: Path
    artifacts: list[GraphArtifactInfo]
    config: GraphConfig
    taxonomy: dict[str, Any] = field(default_factory=dict)
    cache_dir: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphAdapterResponse:
    output_files: list[Path] = field(default_factory=list)
    log: str = ""
    cost_breakdowns: list[CostBreakdown] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class GraphAdapter(Protocol):
    name: str

    def execute(self, request: GraphAdapterRequest) -> GraphAdapterResponse: ...


class CallableGraphAdapter:
    name = "callable"

    def __init__(
        self, fn: Callable[[GraphAdapterRequest], GraphAdapterResponse]
    ) -> None:
        self._fn = fn

    def execute(self, request: GraphAdapterRequest) -> GraphAdapterResponse:
        return self._fn(request)


class SubprocessGraphAdapter:
    name = "subprocess"

    def execute(self, request: GraphAdapterRequest) -> GraphAdapterResponse:
        if not request.config.command:
            raise GraphConfigError(
                "graph_command is required for subprocess adapter"
            )

        substitutions = {
            "{corpus_dir}": str(request.corpus_dir),
            "{outdir}": str(request.workspace_dir),
            "{cache_dir}": str(request.cache_dir) if request.cache_dir else "",
        }
        command = [_substitute(arg, substitutions) for arg in request.config.command]

        try:
            completed = subprocess.run(
                command,
                cwd=request.workspace_dir,
                timeout=request.config.timeout_seconds,
                capture_output=True,
                text=True,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GraphExecutionError(
                f"graph builder timed out after {request.config.timeout_seconds}s"
            ) from exc
        except FileNotFoundError as exc:
            raise GraphExecutionError(
                f"graph builder binary not found: {command[0]}"
            ) from exc

        log = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode != 0:
            raise GraphExecutionError(
                f"graph builder exited with code {completed.returncode}: {log.strip()[:500]}"
            )

        output_files = sorted(
            p
            for p in request.workspace_dir.iterdir()
            if p.is_file() and p.parent == request.workspace_dir
        )
        return GraphAdapterResponse(output_files=output_files, log=log)


def _substitute(arg: str, substitutions: dict[str, str]) -> str:
    for key, value in substitutions.items():
        arg = arg.replace(key, value)
    return arg
