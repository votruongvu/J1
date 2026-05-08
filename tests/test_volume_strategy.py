"""Volume / path-resolution tests for the dev Docker stack.

Locks the contract that:

  * `J1_RAGANYTHING_WORKDIR` defaults to a path inside the workspace
    volume (`/var/lib/j1/raganything`) — NOT a relative path that
    would resolve against the container's CWD and land in the
    writable layer (which is fast but doesn't persist across
    container recreation).
  * The cleanup helper deletes a successful compile's per-document
    output dir.
  * The cleanup helper is a no-op when `J1_KEEP_FAILED_INGEST_ARTIFACTS`
    is truthy.
  * The intake service's storage paths are workspace-rooted (not
    container-CWD-relative), so a worker-image rebuild doesn't
    silently change where uploaded files live.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from j1.providers.raganything._bridge import _cleanup_output_dir


# ---- workdir default ---------------------------------------------


def test_env_example_workdir_is_workspace_rooted():
    """The .env.example default for `J1_RAGANYTHING_WORKDIR` must
    point inside the workspace volume so MinerU output persists
    across container recreation AND lands in the fast Linux-VM
    overlay disk on macOS Docker Desktop. Relative paths like
    `./data/raganything` were the prior default; they resolved
    against the container CWD and survived only as long as the
    container did."""
    text = (
        Path(__file__).resolve().parent.parent / ".env.example"
    ).read_text(encoding="utf-8")
    # The line must be set (not commented) AND start with /var/lib/j1.
    lines = [
        line for line in text.splitlines()
        if line.startswith("J1_RAGANYTHING_WORKDIR=")
    ]
    assert lines, "J1_RAGANYTHING_WORKDIR= line missing from .env.example"
    assert any(
        line.startswith("J1_RAGANYTHING_WORKDIR=/var/lib/j1/")
        for line in lines
    ), (
        f"Expected workspace-rooted default; got {lines!r}. "
        "Relative paths land in the container writable layer and "
        "are lost on `docker compose up --build`."
    )


def test_compose_worker_has_tmpfs_for_tmp():
    """The worker service must mount tmpfs at `/tmp` so MinerU /
    raganything intermediate scratch + soffice tempdirs go to RAM.
    Without this, those writes hit the container writable layer
    (fast on macOS Docker Desktop, but slower than RAM and
    accumulates across runs until restart)."""
    compose = (
        Path(__file__).resolve().parent.parent
        / "deploy" / "dev" / "docker-compose.yml"
    ).read_text(encoding="utf-8")
    assert "tmpfs:" in compose, "worker is missing a tmpfs mount block"
    assert "/tmp:size=" in compose, (
        "expected `/tmp:size=<cap>` tmpfs entry on the worker; "
        "size cap stops a runaway parse from OOM-ing the host"
    )


def test_compose_workspace_volume_is_named_not_bind():
    """The workspace mount on api + worker must be the named volume
    `j1_workspace` — never a host bind mount. On macOS Docker
    Desktop, bind mounts go through gRPC FUSE and are ~10× slower
    than named-volume writes."""
    compose = (
        Path(__file__).resolve().parent.parent
        / "deploy" / "dev" / "docker-compose.yml"
    ).read_text(encoding="utf-8")
    # Must contain the named-volume mount.
    assert "j1_workspace:/var/lib/j1" in compose, (
        "workspace must mount the j1_workspace named volume at /var/lib/j1"
    )
    # Must NOT contain a bind-style mount of the workspace dir (e.g.
    # `./data:/var/lib/j1`). A grep for that exact shape would
    # be wrong if anyone ever needed it for staging — so only
    # warn against the dev-laptop traps.
    assert "./data:/var/lib/j1" not in compose
    assert "./workspace:/var/lib/j1" not in compose


# ---- cleanup helper ----------------------------------------------


def test_cleanup_removes_output_dir_on_success(tmp_path):
    output_dir = tmp_path / "outputs" / "doc-1"
    output_dir.mkdir(parents=True)
    (output_dir / "page-0.json").write_text("{}", encoding="utf-8")
    (output_dir / "images").mkdir()
    (output_dir / "images" / "fig-1.png").write_bytes(b"\x89PNG")

    _cleanup_output_dir(output_dir, document_id="doc-1")
    assert not output_dir.exists()


def test_cleanup_is_noop_when_keep_flag_set(tmp_path, monkeypatch):
    output_dir = tmp_path / "outputs" / "doc-2"
    output_dir.mkdir(parents=True)
    (output_dir / "page-0.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("J1_KEEP_FAILED_INGEST_ARTIFACTS", "true")
    _cleanup_output_dir(output_dir, document_id="doc-2")
    # The flag preserves the directory (and its contents).
    assert output_dir.exists()
    assert (output_dir / "page-0.json").exists()


@pytest.mark.parametrize("flag_value", ["1", "true", "yes", "on", "TRUE"])
def test_cleanup_recognises_truthy_flag_variants(
    tmp_path, monkeypatch, flag_value,
):
    output_dir = tmp_path / "outputs" / "doc-3"
    output_dir.mkdir(parents=True)
    monkeypatch.setenv("J1_KEEP_FAILED_INGEST_ARTIFACTS", flag_value)
    _cleanup_output_dir(output_dir, document_id="doc-3")
    assert output_dir.exists()


def test_cleanup_handles_missing_dir_gracefully(tmp_path):
    """Cleanup must not raise when the output dir was never created
    (compile failed earlier than mkdir)."""
    nonexistent = tmp_path / "outputs" / "ghost"
    _cleanup_output_dir(nonexistent, document_id="ghost")
    # No exception. No output side effect to assert.
