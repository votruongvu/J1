import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from j1.connectors.compiler.config import CompilerConfig
from j1.cost.breakdown import CostBreakdown
from j1.errors.exceptions import CompilerConfigError, CompilerExecutionError


@dataclass(frozen=True)
class AdapterRequest:
    workspace_dir: Path
    input_file: Path
    config: CompilerConfig
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdapterResponse:
    output_files: list[Path] = field(default_factory=list)
    log: str = ""
    cost_breakdowns: list[CostBreakdown] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class CompilerAdapter(Protocol):
    name: str

    def execute(self, request: AdapterRequest) -> AdapterResponse: ...


class CallableCompilerAdapter:
    name = "callable"

    def __init__(
        self, fn: Callable[[AdapterRequest], AdapterResponse]
    ) -> None:
        self._fn = fn

    def execute(self, request: AdapterRequest) -> AdapterResponse:
        return self._fn(request)


class SubprocessCompilerAdapter:
    name = "subprocess"

    def execute(self, request: AdapterRequest) -> AdapterResponse:
        if not request.config.command:
            raise CompilerConfigError(
                "compiler_command is required for subprocess adapter"
            )

        substitutions = {
            "{input}": str(request.input_file),
            "{outdir}": str(request.workspace_dir),
            "{document_id}": str(request.metadata.get("document_id", "")),
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
            raise CompilerExecutionError(
                f"compiler timed out after {request.config.timeout_seconds}s"
            ) from exc
        except FileNotFoundError as exc:
            raise CompilerExecutionError(
                f"compiler binary not found: {command[0]}"
            ) from exc

        log = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode != 0:
            raise CompilerExecutionError(
                f"compiler exited with code {completed.returncode}: {log.strip()[:500]}"
            )

        output_files = sorted(
            p for p in request.workspace_dir.iterdir() if p.is_file()
        )
        return AdapterResponse(output_files=output_files, log=log)


def _substitute(arg: str, substitutions: dict[str, str]) -> str:
    for key, value in substitutions.items():
        arg = arg.replace(key, value)
    return arg
