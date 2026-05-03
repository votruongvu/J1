import sys
from pathlib import Path

import pytest

from j1.config.settings import Settings
from j1.errors.exceptions import InvalidIdentifierError, PathTraversalError
from j1.projects.context import ProjectContext
from j1.workspace.resolver import WorkspaceResolver


@pytest.mark.parametrize(
    "value",
    [
        "",
        "..",
        "../etc",
        "a/b",
        "a\\b",
        "a b",
        "a.b",
        ".hidden",
        "a\x00b",
        "x" * 65,
        "-leading-hyphen",
        "_leading-underscore",
    ],
)
def test_invalid_tenant_id_rejected(value):
    with pytest.raises(InvalidIdentifierError):
        ProjectContext(tenant_id=value, project_id="alpha")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "..",
        "../../passwd",
        "a/b",
        "a\\b",
        "a\x00",
    ],
)
def test_invalid_project_id_rejected(value):
    with pytest.raises(InvalidIdentifierError):
        ProjectContext(tenant_id="acme", project_id=value)


def test_invalid_profile_rejected():
    with pytest.raises(InvalidIdentifierError):
        ProjectContext(tenant_id="acme", project_id="alpha", profile="../bad")


@pytest.mark.parametrize(
    "value",
    [
        "abc",
        "ABC",
        "a-b-c",
        "a_b_c",
        "0123",
        "tenant-1",
        "8f2c12c3-1f2e-4abc-9d1f-1a2b3c4d5e6f",
    ],
)
def test_valid_identifiers_accepted(value):
    ctx = ProjectContext(tenant_id=value, project_id=value)
    assert ctx.tenant_id == value
    assert ctx.project_id == value


def test_non_string_identifier_rejected():
    with pytest.raises(InvalidIdentifierError):
        ProjectContext(tenant_id=123, project_id="alpha")  # type: ignore[arg-type]


def test_resolved_paths_stay_under_data_root(tmp_path):
    settings = Settings(data_root=tmp_path.resolve())
    resolver = WorkspaceResolver(settings)
    ctx = ProjectContext(tenant_id="acme", project_id="alpha")
    assert resolver.project_root(ctx).is_relative_to(resolver.data_root)
    for getter in (
        resolver.raw,
        resolver.compiled,
        resolver.enriched,
        resolver.graph,
        resolver.search,
        resolver.audit,
        resolver.runtime,
    ):
        assert getter(ctx).is_relative_to(resolver.data_root)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="symlink creation requires elevated rights on Windows",
)
def test_symlink_escape_rejected(tmp_path: Path):
    data_root = (tmp_path / "data").resolve()
    data_root.mkdir()
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()

    settings = Settings(data_root=data_root)
    resolver = WorkspaceResolver(settings)
    ctx = ProjectContext(tenant_id="acme", project_id="alpha")

    project_root = data_root / "tenants/acme/projects/alpha"
    project_root.mkdir(parents=True)
    (project_root / "raw").symlink_to(outside)

    with pytest.raises(PathTraversalError):
        resolver.raw(ctx)
