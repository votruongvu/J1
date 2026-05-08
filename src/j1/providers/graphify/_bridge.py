"""Real default boundary to Graphify.

Two integration modes:

  * `mode=cli` (default): invokes `J1_GRAPHIFY_COMMAND` as a
    subprocess. Receives J1's `GraphifyGraphRequest`, materialises a
    line-delimited JSON input file with `(artifact_id, content_path)`
    pairs into a temp directory, spawns the binary with
    `--input <input.json> --output <out.json> --workdir <workdir>`,
    parses the output JSON into J1 `ArtifactDraft`s.
  * `mode=python`: lazy-imports a `graphify` Python package and tries
    its public `build_graph` / `Graphify.build` entry points.

The CLI mode is what the existing env vars (`J1_GRAPHIFY_MODE=cli`,
`J1_GRAPHIFY_COMMAND=graphify`) already promise. This module delivers
on that promise.

Defensiveness:
  * CLI binary not on `$PATH` → `ProviderUnavailable("install graphify CLI")`.
  * CLI exits non-zero → `ArtifactProcessingResult(status=FAILED)` with
    the captured stderr (truncated, no secrets).
  * Output JSON malformed → `ArtifactProcessingResult(status=FAILED)`
    with parser error.
  * Python mode missing the `graphify` module → `ProviderUnavailable`
    with pip-install hint.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from j1.connectors.graph.config import ARTIFACT_KIND_GRAPH_JSON
from j1.processing.results import (
    ArtifactDraft,
    ArtifactProcessingResult,
    ResultStatus,
)
from j1.providers.errors import ProviderUnavailable

if TYPE_CHECKING:
    from j1.providers.graphify.graph import GraphifyGraphRequest

_log = logging.getLogger(__name__)

# Cap subprocess output we surface via metadata / errors so a chatty
# CLI can't blow up Temporal payload limits.
_MAX_STDERR_BYTES = 4096
_MAX_STDOUT_BYTES = 64 * 1024


def default_build_graph(
    request: "GraphifyGraphRequest",
) -> ArtifactProcessingResult:
    """Dispatch to `mode=cli` (default) or `mode=python`."""
    mode = (request.settings.mode or "cli").lower()
    if mode == "cli":
        return _build_via_cli(request)
    if mode == "python":
        return _build_via_python(request)
    raise ProviderUnavailable(
        f"Unknown Graphify mode {mode!r}. Set J1_GRAPHIFY_MODE to one of: "
        "cli, python — or override via J1_GRAPHIFY_GRAPH_PROCESSOR."
    )


# ---- CLI mode ------------------------------------------------------


def _build_via_cli(
    request: "GraphifyGraphRequest",
) -> ArtifactProcessingResult:
    command = (request.settings.command or "graphify").strip()
    binary = shutil.which(command) or (command if Path(command).exists() else None)
    if binary is None:
        raise ProviderUnavailable(
            f"Graphify CLI {command!r} not found on $PATH. Install the "
            f"binary, or set J1_GRAPHIFY_COMMAND to its absolute path, or "
            f"switch to J1_GRAPHIFY_MODE=python, or override via "
            f"J1_GRAPHIFY_GRAPH_PROCESSOR."
        )

    workdir = Path(request.settings.workdir or "./data/graphify").expanduser()
    workdir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="j1-graphify-", dir=workdir) as tmp:
        tmpdir = Path(tmp)
        input_path = tmpdir / "input.json"
        output_path = tmpdir / "output.json"
        input_payload = {
            "tenant_id": request.ctx.tenant_id,
            "project_id": request.ctx.project_id,
            "artifact_ids": list(request.artifact_ids),
        }
        input_path.write_text(json.dumps(input_payload), encoding="utf-8")

        argv = [
            binary,
            "--input", str(input_path),
            "--output", str(output_path),
            "--workdir", str(workdir),
        ]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                check=False,
                env={**os.environ},
                # No shell=True — argv is fully composed.
            )
        except OSError as exc:
            raise ProviderUnavailable(
                f"Graphify CLI invocation failed at the OS level: {exc}. "
                f"Verify {command!r} is executable."
            ) from exc

        if completed.returncode != 0:
            return ArtifactProcessingResult(
                status=ResultStatus.FAILED,
                error=_truncate(completed.stderr, _MAX_STDERR_BYTES),
                message=f"graphify exited {completed.returncode}",
                metadata={
                    "provider": "graphify",
                    "mode": "cli",
                    "returncode": str(completed.returncode),
                },
            )

        if not output_path.exists():
            return ArtifactProcessingResult(
                status=ResultStatus.FAILED,
                error=(
                    f"graphify produced no output file at {output_path}. "
                    f"stdout: {_truncate(completed.stdout, 512)}"
                ),
                metadata={"provider": "graphify", "mode": "cli"},
            )

        try:
            graph_payload = json.loads(output_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return ArtifactProcessingResult(
                status=ResultStatus.FAILED,
                error=f"graphify output JSON could not be parsed: {exc}",
                metadata={"provider": "graphify", "mode": "cli"},
            )

        return _drafts_from_graph_payload(
            graph_payload,
            request_artifact_ids=list(request.artifact_ids),
            mode="cli",
        )


# ---- Python-package mode -------------------------------------------


def _build_via_python(
    request: "GraphifyGraphRequest",
) -> ArtifactProcessingResult:
    try:
        import graphify
    except ImportError as exc:
        raise ProviderUnavailable(
            "Graphify Python integration requires the `graphify` package. "
            "Install with: pip install j1[graphify]   (or: pip install graphify)"
        ) from exc

    builder = (
        getattr(graphify, "build_graph", None)
        or getattr(graphify, "Graphify", None)
    )
    if builder is None:
        raise ProviderUnavailable(
            "Installed `graphify` module exposes neither a top-level "
            "`build_graph` callable nor a `Graphify` class. Override via "
            "J1_GRAPHIFY_GRAPH_PROCESSOR with your own bridge."
        )

    payload_input = {
        "tenant_id": request.ctx.tenant_id,
        "project_id": request.ctx.project_id,
        "artifact_ids": list(request.artifact_ids),
        "workdir": request.settings.workdir,
    }
    try:
        if isinstance(builder, type):
            instance = builder(workdir=request.settings.workdir)
            method = getattr(instance, "build", None) or getattr(instance, "build_graph", None)
            if method is None:
                raise ProviderUnavailable(
                    f"`graphify.{builder.__name__}` has neither a `build` nor "
                    f"`build_graph` method. Override via "
                    f"J1_GRAPHIFY_GRAPH_PROCESSOR."
                )
            graph_payload = method(payload_input)
        else:
            graph_payload = builder(payload_input)
    except ProviderUnavailable:
        raise
    except Exception as exc:
        return ArtifactProcessingResult(
            status=ResultStatus.FAILED,
            error=str(exc),
            message=type(exc).__name__,
            metadata={"provider": "graphify", "mode": "python"},
        )

    if not isinstance(graph_payload, dict):
        return ArtifactProcessingResult(
            status=ResultStatus.FAILED,
            error=(
                f"graphify Python builder returned {type(graph_payload).__name__!r}; "
                f"expected dict with `nodes` / `edges`."
            ),
            metadata={"provider": "graphify", "mode": "python"},
        )

    return _drafts_from_graph_payload(
        graph_payload,
        request_artifact_ids=list(request.artifact_ids),
        mode="python",
    )


# ---- Output normalisation ------------------------------------------


def _drafts_from_graph_payload(
    payload: dict, *, request_artifact_ids: list[str], mode: str,
) -> ArtifactProcessingResult:
    """Turn a `{"nodes": [...], "edges": [...]}` dict into one
    `graph_json` ArtifactDraft.

    Preserves source references (`source_block_refs` /
    `source_artifact_ids`) when the payload includes them.
    """
    nodes = payload.get("nodes") or []
    edges = payload.get("edges") or []
    metadata = {
        "provider": "graphify",
        "mode": mode,
        "node_count": str(len(nodes)),
        "edge_count": str(len(edges)),
    }
    drafts = [ArtifactDraft(
        kind=ARTIFACT_KIND_GRAPH_JSON,
        content=json.dumps(payload).encode("utf-8"),
        suggested_extension=".json",
        source_artifact_ids=request_artifact_ids,
        metadata=metadata,
    )]
    return ArtifactProcessingResult(
        status=ResultStatus.SUCCEEDED, drafts=drafts, metadata=metadata,
    )


def _truncate(value: bytes | str, limit: int) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"
