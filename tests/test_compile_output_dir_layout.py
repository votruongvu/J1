"""Pins the layout invariant of MinerU's compile scratch directory.

Two reindex attempts for the same document MUST land in different
scratch dirs — otherwise the candidate run's intermediate files
overwrite the previous active run's files (or vice-versa).

Layout:

    outputs/{document_id}/{run_id}/   ← run-scoped (production)
    outputs/{document_id}/            ← legacy fallback when run_id
                                        is None (direct test callers)
"""

from __future__ import annotations

from pathlib import Path

from j1.providers.raganything._bridge import _resolve_compile_output_dir


def test_run_scoped_layout_when_run_id_present():
    out = _resolve_compile_output_dir(
        workdir="/tmp/rag",
        document_id="doc-1",
        run_id="run-A",
    )
    assert out == Path("/tmp/rag/outputs/doc-1/run-A")


def test_two_runs_for_same_document_land_in_sibling_dirs():
    """The structural promise of the refactor: candidate runs do
    not stomp the previous active run's scratch."""
    a = _resolve_compile_output_dir(
        workdir="/tmp/rag", document_id="doc-1", run_id="run-A",
    )
    b = _resolve_compile_output_dir(
        workdir="/tmp/rag", document_id="doc-1", run_id="run-B",
    )
    assert a != b
    assert a.parent == b.parent  # both under outputs/doc-1/


def test_legacy_unscoped_layout_when_run_id_missing():
    out = _resolve_compile_output_dir(
        workdir="/tmp/rag", document_id="doc-1", run_id=None,
    )
    assert out == Path("/tmp/rag/outputs/doc-1")


def test_default_workdir_used_when_none():
    out = _resolve_compile_output_dir(
        workdir=None, document_id="doc-1", run_id="run-A",
    )
    assert out == Path("./data/raganything/outputs/doc-1/run-A").expanduser()


def test_workdir_path_expands_user_home():
    out = _resolve_compile_output_dir(
        workdir="~/raganything",
        document_id="doc-1",
        run_id="run-A",
    )
    assert str(out).startswith(str(Path("~/raganything").expanduser()))
    assert out.name == "run-A"
