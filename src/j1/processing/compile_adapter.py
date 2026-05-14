"""CompileEngineAdapter — the seam between the snapshot lifecycle
and a compile engine (RAGAnything, in our case).

Phase 2 fixes the cross-run contamination bug by routing compile
output into a **snapshot-scoped** workspace. The bridge's existing
The legacy resolver (now deleted in Phase 5) returned
``{workdir}/runs/{t}/{p}/{d}/{r}``
— that path is named after the run, which makes the storage shared
across re-indexes of the same document. The new adapter computes
``{workdir}/snapshots/{snapshot_id}`` and hands that to RAGAnything.

Design: the adapter does NOT re-implement RAGAnything. It wraps the
existing ``default_compile`` entrypoint and overrides the workspace
path resolution. RAGAnything internals stay a black box.

Why a Protocol: lets us swap engines (a future LlamaIndex compile,
a test-only deterministic compile) without changing the lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from j1.documents.snapshot_layout import SnapshotArea, SnapshotLayout
from j1.projects.context import ProjectContext


# ---- Request / response shapes ----------------------------------


@dataclass(frozen=True)
class CompileRequest:
    """Everything a compile engine needs to know about a single
    snapshot-scoped compile. Engine-specific config rides in
    ``compile_config`` (free-form dict — the adapter passes it
    through)."""

    ctx: ProjectContext
    document_id: str
    snapshot_id: str
    created_by_run_id: str
    profile_id: str | None
    source_path: Path
    snapshot_workspace: Path
    compile_config: dict[str, Any]


@dataclass(frozen=True)
class CompileResult:
    """Adapter-neutral compile result. Adapters that return native
    types (e.g. RAGAnything's ``ArtifactProcessingResult``) wrap them
    in this shape so the lifecycle code only depends on these
    fields."""

    success: bool
    artifacts: tuple[Any, ...] = ()           # adapter-native draft list
    error: str | None = None
    metadata: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # frozen dataclasses can't mutate; assign defaults via
        # object.__setattr__ as the dataclass cookbook recommends.
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})


# ---- Adapter protocol -------------------------------------------


class CompileEngineAdapter(Protocol):
    """Compile one document into a snapshot-scoped workspace. The
    adapter MUST NOT touch any storage outside ``request.snapshot_workspace``
    (or its provider-native subdirs) — concurrent runs against the
    same document write into different snapshot workspaces, never
    the same one."""

    name: str

    def compile(self, request: CompileRequest) -> CompileResult: ...


# ---- RAGAnything adapter ----------------------------------------


class RAGAnythingCompileAdapter:
    """Routes compile through the existing
    ``j1.providers.raganything._bridge.default_compile`` but pins
    LightRAG's working dir to ``{snapshot_workspace}/compile``
    instead of ``{global_workdir}/runs/{run_id}``.

    The adapter does NOT modify the bridge internals — it builds a
    ``RAGAnythingCompileRequest`` with an override path. Phase 2
    ships the adapter; Phase 3 will retire the legacy
    legacy resolver once every caller routes through this adapter
    (now done in Phase 5).
    """

    name = "raganything"

    def __init__(
        self,
        *,
        layout: SnapshotLayout,
        # Function pointer to the bridge entrypoint — injected so
        # tests can stub it out without dragging RAGAnything's
        # transitive deps into the test environment.
        bridge_compile: Any = None,
    ) -> None:
        self._layout = layout
        self._bridge_compile = bridge_compile

    def compile(self, request: CompileRequest) -> CompileResult:
        # The snapshot workspace is the operator-visible root. The
        # bridge writes LightRAG storage into the ``compile`` area;
        # the parsed/source/indexes/etc. siblings stay reserved for
        # other stages.
        compile_root = (
            request.snapshot_workspace / SnapshotArea.COMPILE.value
        )
        compile_root.mkdir(parents=True, exist_ok=True)

        if self._bridge_compile is None:
            # Bridge wasn't injected — return a clean "not configured"
            # result so callers can decide whether to fail the run or
            # treat compile as optional in a test harness.
            return CompileResult(
                success=False,
                error="raganything compile bridge not configured",
                metadata={
                    "compile_workdir": str(compile_root),
                    "snapshot_id": request.snapshot_id,
                },
            )

        try:
            bridge_result = self._bridge_compile(
                ctx=request.ctx,
                document_id=request.document_id,
                source_path=request.source_path,
                working_dir_override=compile_root,
                compile_config=dict(request.compile_config),
                # The bridge still wants a run_id today (for legacy
                # cleanup / output naming). Pass ``created_by_run_id``
                # so it can stamp lineage on its drafts without us
                # exposing snapshot_id to the bridge surface yet.
                run_id=request.created_by_run_id,
            )
        except Exception as exc:  # noqa: BLE001 — surface bridge failure
            return CompileResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                metadata={
                    "compile_workdir": str(compile_root),
                    "snapshot_id": request.snapshot_id,
                },
            )

        # The bridge result shape (``ArtifactProcessingResult``) carries
        # ``status`` + ``drafts`` + ``metadata``. Translate to the
        # adapter-neutral shape without importing the bridge types
        # so this module stays decoupled from the provider package.
        status = getattr(bridge_result, "status", None)
        drafts = tuple(getattr(bridge_result, "drafts", ()) or ())
        error = getattr(bridge_result, "error", None)
        meta = dict(getattr(bridge_result, "metadata", {}) or {})
        meta.setdefault("compile_workdir", str(compile_root))
        meta.setdefault("snapshot_id", request.snapshot_id)
        # The status enum comparison is by string value to avoid an
        # import dependency on the bridge.
        success = (
            getattr(status, "value", str(status))
            in {"succeeded", "succeeded_with_warnings"}
        )
        return CompileResult(
            success=success,
            artifacts=drafts,
            error=error,
            metadata=meta,
        )


__all__ = [
    "CompileEngineAdapter",
    "CompileRequest",
    "CompileResult",
    "RAGAnythingCompileAdapter",
]
