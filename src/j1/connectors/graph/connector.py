import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactRegistry
from j1.audit.recorder import AuditRecorder
from j1.connectors.graph.adapters import (
    GraphAdapter,
    GraphAdapterRequest,
    GraphArtifactInfo,
)
from j1.connectors.graph.config import GraphConfig
from j1.processing.results import ArtifactDraft, ArtifactProcessingResult
from j1.processing.status import ResultStatus
from j1.profiles.model import Profile
from j1.projects.context import ProjectContext
from j1.workspace.resolver import WorkspaceResolver

DEFAULT_KIND = "external_graph_builder"

ACTION_INVOKED = "j1.connector.graph.invoked"
ACTION_COMPLETED = "j1.connector.graph.completed"
ACTION_FAILED = "j1.connector.graph.failed"
ACTION_SKIPPED = "j1.connector.graph.skipped"

TARGET_ARTIFACT_SET = "artifact_set"

GRAPH_CACHE_DIRNAME = "graph_cache"


class ExternalGraphBuilder:
    def __init__(
        self,
        config: GraphConfig,
        adapter: GraphAdapter,
        workspace: WorkspaceResolver,
        artifacts: ArtifactRegistry,
        profile: Profile,
        audit: AuditRecorder | None = None,
        *,
        kind: str = DEFAULT_KIND,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.kind = kind
        self._config = config
        self._adapter = adapter
        self._workspace = workspace
        self._artifacts = artifacts
        self._profile = profile
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def build(
        self, ctx: ProjectContext, artifact_ids: list[str]
    ) -> ArtifactProcessingResult:
        target_id = _set_target(artifact_ids)

        if not self._config.enabled:
            self._emit_audit(
                ctx,
                ACTION_SKIPPED,
                target_id,
                payload={"reason": "graph_disabled"},
            )
            return ArtifactProcessingResult(
                status=ResultStatus.SKIPPED,
                message="graph builder disabled",
                metadata={"reason": "graph_disabled"},
            )

        try:
            selected = self._collect_corpus(ctx, artifact_ids)
        except Exception as exc:
            self._emit_audit(
                ctx,
                ACTION_FAILED,
                target_id,
                payload={"error": str(exc), "error_type": type(exc).__name__},
            )
            return ArtifactProcessingResult(
                status=ResultStatus.FAILED,
                error=str(exc),
                message=type(exc).__name__,
            )

        self._emit_audit(
            ctx,
            ACTION_INVOKED,
            target_id,
            payload={
                "adapter": self._adapter.name,
                "corpus_size": len(selected),
                "kind": self.kind,
            },
        )

        with tempfile.TemporaryDirectory(prefix="j1-graph-") as tmpdir:
            workspace_dir = Path(tmpdir) / "out"
            workspace_dir.mkdir()
            corpus_dir = Path(tmpdir) / "corpus"
            corpus_dir.mkdir()

            artifact_infos: list[GraphArtifactInfo] = []
            for record, content in selected:
                stored_name = Path(record.location).name
                (corpus_dir / stored_name).write_bytes(content)
                artifact_infos.append(
                    GraphArtifactInfo(
                        artifact_id=record.artifact_id,
                        kind=record.kind,
                        file_name=stored_name,
                        byte_size=len(content),
                        metadata=dict(record.metadata),
                    )
                )

            cache_dir: Path | None = None
            if self._config.cache_enabled:
                cache_dir = self._workspace.runtime(ctx) / GRAPH_CACHE_DIRNAME
                cache_dir.mkdir(parents=True, exist_ok=True)

            request = GraphAdapterRequest(
                workspace_dir=workspace_dir,
                corpus_dir=corpus_dir,
                artifacts=artifact_infos,
                config=self._config,
                taxonomy=self._profile.graph_taxonomy,
                cache_dir=cache_dir,
                metadata={
                    "tenant_id": ctx.tenant_id,
                    "project_id": ctx.project_id,
                    "profile_id": self._profile.profile_id,
                },
            )

            try:
                response = self._adapter.execute(request)
            except Exception as exc:
                self._emit_audit(
                    ctx,
                    ACTION_FAILED,
                    target_id,
                    payload={
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "adapter": self._adapter.name,
                    },
                )
                return ArtifactProcessingResult(
                    status=ResultStatus.FAILED,
                    error=str(exc),
                    message=type(exc).__name__,
                )

            drafts = self._build_drafts(response.output_files, artifact_ids)

        self._emit_audit(
            ctx,
            ACTION_COMPLETED,
            target_id,
            payload={
                "adapter": self._adapter.name,
                "draft_count": len(drafts),
                "cost_event_count": len(response.cost_breakdowns),
            },
        )
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=drafts,
            cost_events=list(response.cost_breakdowns),
            metadata={
                "adapter": self._adapter.name,
                "graph_builder_kind": self.kind,
            },
        )

    def _collect_corpus(
        self, ctx: ProjectContext, artifact_ids: list[str]
    ) -> list[tuple[ArtifactRecord, bytes]]:
        include = set(self._config.corpus_include)
        result: list[tuple[ArtifactRecord, bytes]] = []
        for aid in artifact_ids:
            record = self._artifacts.get(ctx, aid)
            if include and record.kind not in include:
                continue
            path = self._workspace.project_root(ctx) / record.location
            if not path.is_file():
                raise FileNotFoundError(
                    f"artifact content missing on disk for {aid}: {path}"
                )
            result.append((record, path.read_bytes()))
        return result

    def _build_drafts(
        self, output_files: list[Path], source_artifact_ids: list[str]
    ) -> list[ArtifactDraft]:
        mapping = self._config.effective_output_mapping()
        drafts: list[ArtifactDraft] = []
        for path in output_files:
            kind = mapping.get(path.name)
            if kind is None:
                continue
            drafts.append(
                ArtifactDraft(
                    kind=kind,
                    content=path.read_bytes(),
                    suggested_extension=path.suffix,
                    source_artifact_ids=list(source_artifact_ids),
                )
            )
        return drafts

    def _emit_audit(
        self,
        ctx: ProjectContext,
        action: str,
        target_id: str,
        *,
        payload: dict | None = None,
    ) -> None:
        if self._audit is None:
            return
        self._audit.record(
            ctx,
            actor="system",
            action=action,
            target_kind=TARGET_ARTIFACT_SET,
            target_id=target_id,
            payload=dict(payload or {}),
        )


def _set_target(ids: list[str]) -> str:
    if not ids:
        return "empty"
    return f"set:{','.join(ids)}"
