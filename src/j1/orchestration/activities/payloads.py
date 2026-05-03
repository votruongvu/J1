from dataclasses import dataclass, field

from j1.projects.context import ProjectContext


@dataclass(frozen=True)
class ProjectScope:
    tenant_id: str
    project_id: str
    profile: str | None = None

    def to_context(self) -> ProjectContext:
        return ProjectContext(
            tenant_id=self.tenant_id,
            project_id=self.project_id,
            profile=self.profile,
        )

    @classmethod
    def from_context(cls, ctx: ProjectContext) -> "ProjectScope":
        return cls(
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            profile=ctx.profile,
        )


@dataclass(frozen=True)
class CompileActivityInput:
    scope: ProjectScope
    document_id: str
    processor_kind: str
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class EnrichActivityInput:
    scope: ProjectScope
    artifact_id: str
    processor_kind: str
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class GraphActivityInput:
    scope: ProjectScope
    artifact_ids: list[str]
    processor_kind: str
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class IndexActivityInput:
    scope: ProjectScope
    artifact_ids: list[str]
    processor_kind: str
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class QueryActivityInput:
    scope: ProjectScope
    question: str
    processor_kind: str
    max_results: int | None = None
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class ArtifactActivityResult:
    status: str
    artifact_ids: list[str] = field(default_factory=list)
    error: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class ProcessingActivityResult:
    status: str
    error: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class QueryActivityResult:
    status: str
    answer: str | None = None
    citations: list[str] = field(default_factory=list)
    error: str | None = None
    message: str | None = None
