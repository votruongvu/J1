from pathlib import Path

import pytest

from j1.config.settings import Settings
from j1.projects.context import ProjectContext
from j1.workspace.layout import WorkspaceArea
from j1.workspace.resolver import WorkspaceResolver


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_root=tmp_path.resolve())


@pytest.fixture
def resolver(settings: Settings) -> WorkspaceResolver:
    return WorkspaceResolver(settings)


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="acme", project_id="alpha")


def test_project_root_path(resolver, settings, ctx):
    expected = (settings.data_root / "tenants/acme/projects/alpha").resolve()
    assert resolver.project_root(ctx) == expected


def test_each_area_resolves_under_project(resolver, ctx):
    project_root = resolver.project_root(ctx)
    by_area = {
        WorkspaceArea.RAW: resolver.raw(ctx),
        WorkspaceArea.COMPILED: resolver.compiled(ctx),
        WorkspaceArea.ENRICHED: resolver.enriched(ctx),
        WorkspaceArea.GRAPH: resolver.graph(ctx),
        WorkspaceArea.SEARCH: resolver.search(ctx),
        WorkspaceArea.AUDIT: resolver.audit(ctx),
        WorkspaceArea.RUNTIME: resolver.runtime(ctx),
    }
    for area, path in by_area.items():
        assert path == project_root / area.value


def test_ensure_creates_full_layout(resolver, ctx):
    resolver.ensure(ctx)
    project_root = resolver.project_root(ctx)
    assert project_root.is_dir()
    for area in WorkspaceArea:
        assert (project_root / area.value).is_dir()


def test_ensure_is_idempotent(resolver, ctx):
    resolver.ensure(ctx)
    resolver.ensure(ctx)
    assert resolver.project_root(ctx).is_dir()


def test_data_root_property_is_resolved(tmp_path):
    settings = Settings(data_root=tmp_path.resolve())
    resolver = WorkspaceResolver(settings)
    assert resolver.data_root == tmp_path.resolve()


def test_distinct_projects_get_distinct_paths(resolver):
    a = ProjectContext(tenant_id="acme", project_id="alpha")
    b = ProjectContext(tenant_id="acme", project_id="beta")
    assert resolver.project_root(a) != resolver.project_root(b)


def test_distinct_tenants_get_distinct_paths(resolver):
    a = ProjectContext(tenant_id="acme", project_id="alpha")
    b = ProjectContext(tenant_id="zenith", project_id="alpha")
    assert resolver.project_root(a) != resolver.project_root(b)


def test_area_method_matches_named_accessors(resolver, ctx):
    assert resolver.area(ctx, WorkspaceArea.RAW) == resolver.raw(ctx)
    assert resolver.area(ctx, WorkspaceArea.AUDIT) == resolver.audit(ctx)
