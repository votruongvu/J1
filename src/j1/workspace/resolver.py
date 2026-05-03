from pathlib import Path

from j1.config.settings import Settings
from j1.errors.exceptions import PathTraversalError
from j1.projects.context import ProjectContext
from j1.workspace.layout import PROJECTS_DIR, TENANTS_DIR, WorkspaceArea


class WorkspaceResolver:
    def __init__(self, settings: Settings) -> None:
        self._data_root: Path = settings.data_root.resolve()

    @property
    def data_root(self) -> Path:
        return self._data_root

    def project_root(self, ctx: ProjectContext) -> Path:
        return self._safe_join(
            TENANTS_DIR, ctx.tenant_id, PROJECTS_DIR, ctx.project_id
        )

    def area(self, ctx: ProjectContext, area: WorkspaceArea) -> Path:
        return self._safe_join(
            TENANTS_DIR,
            ctx.tenant_id,
            PROJECTS_DIR,
            ctx.project_id,
            area.value,
        )

    def raw(self, ctx: ProjectContext) -> Path:
        return self.area(ctx, WorkspaceArea.RAW)

    def compiled(self, ctx: ProjectContext) -> Path:
        return self.area(ctx, WorkspaceArea.COMPILED)

    def enriched(self, ctx: ProjectContext) -> Path:
        return self.area(ctx, WorkspaceArea.ENRICHED)

    def graph(self, ctx: ProjectContext) -> Path:
        return self.area(ctx, WorkspaceArea.GRAPH)

    def search(self, ctx: ProjectContext) -> Path:
        return self.area(ctx, WorkspaceArea.SEARCH)

    def audit(self, ctx: ProjectContext) -> Path:
        return self.area(ctx, WorkspaceArea.AUDIT)

    def runtime(self, ctx: ProjectContext) -> Path:
        return self.area(ctx, WorkspaceArea.RUNTIME)

    def ensure(self, ctx: ProjectContext) -> Path:
        root = self.project_root(ctx)
        root.mkdir(parents=True, exist_ok=True)
        for area in WorkspaceArea:
            self.area(ctx, area).mkdir(parents=True, exist_ok=True)
        return root

    def _safe_join(self, *parts: str) -> Path:
        candidate = self._data_root.joinpath(*parts).resolve()
        try:
            candidate.relative_to(self._data_root)
        except ValueError as exc:
            raise PathTraversalError(
                f"resolved path {candidate} escapes data root {self._data_root}"
            ) from exc
        return candidate
