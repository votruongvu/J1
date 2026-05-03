import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from j1.audit.recorder import AuditRecorder
from j1.connectors.compiler.adapters import (
    AdapterRequest,
    CompilerAdapter,
)
from j1.connectors.compiler.config import CompilerConfig
from j1.errors.exceptions import DocumentNotFoundError
from j1.intake.registry import SourceRegistry
from j1.processing.results import ArtifactDraft, ArtifactProcessingResult
from j1.processing.status import ResultStatus
from j1.projects.context import ProjectContext
from j1.workspace.resolver import WorkspaceResolver

DEFAULT_KIND = "external_knowledge_compiler"

ACTION_INVOKED = "j1.connector.compiler.invoked"
ACTION_COMPLETED = "j1.connector.compiler.completed"
ACTION_FAILED = "j1.connector.compiler.failed"
ACTION_SKIPPED = "j1.connector.compiler.skipped"

TARGET_DOCUMENT = "document"


class ExternalKnowledgeCompiler:
    def __init__(
        self,
        config: CompilerConfig,
        adapter: CompilerAdapter,
        workspace: WorkspaceResolver,
        sources: SourceRegistry,
        audit: AuditRecorder | None = None,
        *,
        kind: str = DEFAULT_KIND,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.kind = kind
        self._config = config
        self._adapter = adapter
        self._workspace = workspace
        self._sources = sources
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def compile(
        self, ctx: ProjectContext, document_id: str
    ) -> ArtifactProcessingResult:
        if not self._config.enabled:
            self._emit_audit(
                ctx,
                ACTION_SKIPPED,
                document_id,
                payload={"reason": "compiler_disabled"},
            )
            return ArtifactProcessingResult(
                status=ResultStatus.SKIPPED,
                message="compiler disabled",
                metadata={"reason": "compiler_disabled"},
            )

        try:
            document = self._sources.get(ctx, document_id)
        except DocumentNotFoundError as exc:
            self._emit_audit(
                ctx,
                ACTION_FAILED,
                document_id,
                payload={"error": str(exc), "error_type": "DocumentNotFoundError"},
            )
            return ArtifactProcessingResult(
                status=ResultStatus.FAILED,
                error=str(exc),
                message="DocumentNotFoundError",
            )

        source_path = self._workspace.raw(ctx) / document.stored_filename
        if not source_path.is_file():
            error = f"source file missing for document {document_id}"
            self._emit_audit(
                ctx,
                ACTION_FAILED,
                document_id,
                payload={"error": error, "error_type": "SourceFileMissing"},
            )
            return ArtifactProcessingResult(
                status=ResultStatus.FAILED,
                error=error,
                message="SourceFileMissing",
            )

        self._emit_audit(
            ctx,
            ACTION_INVOKED,
            document_id,
            payload={"adapter": self._adapter.name, "kind": self.kind},
        )

        with tempfile.TemporaryDirectory(prefix="j1-compiler-") as tmpdir:
            request = AdapterRequest(
                workspace_dir=Path(tmpdir),
                input_file=source_path,
                config=self._config,
                metadata={
                    "document_id": document_id,
                    "tenant_id": ctx.tenant_id,
                    "project_id": ctx.project_id,
                    "mime_type": document.mime_type,
                },
            )
            try:
                response = self._adapter.execute(request)
            except Exception as exc:
                self._emit_audit(
                    ctx,
                    ACTION_FAILED,
                    document_id,
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

            drafts = self._build_drafts(response.output_files, document_id)

        self._emit_audit(
            ctx,
            ACTION_COMPLETED,
            document_id,
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
                "compiler_kind": self.kind,
            },
        )

    def _build_drafts(
        self, output_files: list[Path], document_id: str
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
                    source_document_ids=[document_id],
                )
            )
        return drafts

    def _emit_audit(
        self,
        ctx: ProjectContext,
        action: str,
        document_id: str,
        *,
        payload: dict | None = None,
    ) -> None:
        if self._audit is None:
            return
        self._audit.record(
            ctx,
            actor="system",
            action=action,
            target_kind=TARGET_DOCUMENT,
            target_id=document_id,
            payload=dict(payload or {}),
        )
