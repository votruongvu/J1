"""Contract — Phase 6B sample memory eval.

Pins:

  * Sample dataset exists on disk and contains the expected file
    set, with cross-document anchors (NCR-007, F-12, D-302, slab
    S-205, C30/37, etc.) present so the evaluation fixture's
    expected_terms can match.
  * Evaluation fixture loads via the Phase 6 loader, contains the
    required category set, and every fixture entry has the
    minimum shape (unique id, non-empty question, scope, category
    matching).
  * Script's staging step copies files + fixture + writes a
    manifest under the target ``--data-root``; the staged copy
    matches the canonical source byte-for-byte.
  * Preflight inspects the right env keys, reports missing keys
    with operator-readable action items, and the result is True
    only when all keys + validation_service + orchestrator are
    ready.
  * `build_run_command` produces the exact `evaluate_memory_query`
    argv the operator needs.
  * Script does NOT fabricate evaluation results — `evaluation_run`
    on the status artifact is False when staging-only is invoked.
  * Script's CLI surface (--help) works.
  * No-LLM-imports regression guard on the script module.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from j1.tools.evaluate_memory_query import (
    load_memory_query_fixture,
)
from j1.tools.run_memory_eval_sample import (
    PREFLIGHT_ENV_KEYS,
    PreflightResult,
    SAMPLE_FILES,
    SAMPLE_FIXTURE_NAME,
    build_run_command,
    build_sample_manifest,
    fixture_source_path,
    manual_next_steps,
    run_preflight,
    stage_sample_dataset,
)


# ---- Sample dataset on-disk shape -----------------------------


def _sample_dir() -> Path:
    return fixture_source_path().parent


def test_sample_directory_exists():
    sample_dir = _sample_dir()
    assert sample_dir.exists(), (
        f"Phase 6B sample dataset directory missing: {sample_dir}"
    )
    assert (sample_dir / "README.md").exists()


def test_all_five_sample_files_are_present():
    data_dir = _sample_dir() / "data"
    for filename, _ in SAMPLE_FILES:
        assert (data_dir / filename).exists(), (
            f"missing sample file: {filename}"
        )


def test_sample_dataset_carries_cross_document_anchors():
    """The cross-document linkage in the README:
        F-12 → NCR-007 → C30/37 / C40/50 → D-302 → slab S-205
    Each anchor MUST appear in at least one sample file so the
    fixture's expected_terms can match real content."""
    text = "\n".join(
        (_sample_dir() / "data" / fname).read_text(encoding="utf-8")
        for fname, _ in SAMPLE_FILES
    )
    for anchor in (
        "F-12", "NCR-007", "C30/37", "C40/50",
        "D-302", "S-205", "03 30 00",
    ):
        assert anchor in text, (
            f"cross-document anchor '{anchor}' not found in any "
            "sample file"
        )


def test_specific_anchor_lives_in_originating_file():
    """Each anchor's primary source file should literally carry
    it — a defensive check that prevents accidental copy-pastes
    that scatter all anchors across all files."""
    data_dir = _sample_dir() / "data"
    boq = (data_dir / "CE-001_BOQ_quantity_schedule.md").read_text(
        encoding="utf-8",
    )
    inspection = (
        data_dir / "CE-002_site_inspection_report.md"
    ).read_text(encoding="utf-8")
    ncr = (data_dir / "CE-003_NCR_corrective_action.md").read_text(
        encoding="utf-8",
    )
    calc = (
        data_dir / "CE-004_structural_calculation_summary.md"
    ).read_text(encoding="utf-8")
    drawing = (
        data_dir / "CE-005_drawing_register_and_specification.md"
    ).read_text(encoding="utf-8")
    assert "BOQ" in boq
    assert "F-12" in inspection
    assert "NCR-007" in ncr
    assert "deflection" in calc
    assert "D-302" in drawing


# ---- Fixture shape --------------------------------------------


def test_fixture_loads_via_phase6_loader():
    queries = load_memory_query_fixture(fixture_source_path())
    assert len(queries) >= 10, (
        "fixture must carry at least 10 queries per Phase 6B spec"
    )


def test_fixture_query_ids_are_unique():
    queries = load_memory_query_fixture(fixture_source_path())
    ids = [q.id for q in queries]
    assert len(set(ids)) == len(ids), (
        "fixture query ids must be unique"
    )


_REQUIRED_CATEGORIES = frozenset({
    "risk",
    "requirement",
    "action_item",
    "boq",
    "inspection_finding",
    "ncr",
    "drawing_revision",
    "test_result",
    "calculation",
    "general_summary",
    "source_lookup",
    "cross_document",
})


def test_fixture_covers_required_categories():
    queries = load_memory_query_fixture(fixture_source_path())
    categories = {q.category for q in queries if q.category}
    missing = _REQUIRED_CATEGORIES - categories
    assert not missing, (
        f"fixture missing required categories: {sorted(missing)}"
    )


def test_fixture_entries_have_non_empty_questions():
    queries = load_memory_query_fixture(fixture_source_path())
    assert all(q.question.strip() for q in queries)


def test_fixture_expected_terms_appear_in_sample_files():
    """The expected_terms phrases used as quality proxies should
    actually appear in at least one sample file — otherwise the
    proxy reports "missing" even when memory works correctly.

    Exceptions are tolerated for general / source-lookup phrases
    that may need stemming or be generic ('inspection' is fine);
    but for domain-specific anchors (F-12, NCR-007, D-302,
    C30/37, C40/50) the check is strict.
    """
    queries = load_memory_query_fixture(fixture_source_path())
    all_text = "\n".join(
        (_sample_dir() / "data" / fname).read_text(encoding="utf-8")
        for fname, _ in SAMPLE_FILES
    )
    domain_specific = {
        "F-12", "NCR-007", "D-302", "C30/37", "C40/50",
        "03 30 00",
    }
    for query in queries:
        for term in query.expected_terms:
            if term in domain_specific:
                assert term in all_text, (
                    f"expected term '{term}' on query "
                    f"'{query.id}' is not in any sample file"
                )


# ---- Staging --------------------------------------------------


def test_staging_copies_files_and_writes_manifest(tmp_path: Path):
    manifest = stage_sample_dataset(tmp_path)
    # All five files copied.
    for filename, kind in SAMPLE_FILES:
        staged = tmp_path / "data" / filename
        assert staged.exists()
        # Byte-identical to source.
        assert staged.read_bytes() == (
            _sample_dir() / "data" / filename
        ).read_bytes()
    # Fixture copied.
    assert (tmp_path / SAMPLE_FIXTURE_NAME).exists()
    # Manifest written + matches the in-memory build.
    manifest_path = tmp_path / "sample_project_manifest.json"
    assert manifest_path.exists()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload == manifest
    # Manifest references the right files.
    filenames_in_manifest = {
        d["filename"] for d in manifest["documents"]
    }
    expected = {f for f, _ in SAMPLE_FILES}
    assert filenames_in_manifest == expected


def test_staging_is_idempotent(tmp_path: Path):
    """Re-running staging on the same root should overwrite cleanly
    — no leftover state, no exception."""
    stage_sample_dataset(tmp_path)
    stage_sample_dataset(tmp_path)
    # Files still present after second staging.
    for filename, _ in SAMPLE_FILES:
        assert (tmp_path / "data" / filename).exists()


def test_build_sample_manifest_records_byte_sizes(tmp_path: Path):
    stage_sample_dataset(tmp_path)
    manifest = build_sample_manifest(tmp_path)
    for doc in manifest["documents"]:
        assert doc["exists"] is True
        assert doc["byte_size"] is not None
        assert doc["byte_size"] > 0


# ---- Preflight ------------------------------------------------


def test_preflight_env_keys_set_is_stable():
    # Stability matters because operators / docs may grep for the
    # exact set of keys checked.
    assert PREFLIGHT_ENV_KEYS == (
        "J1_DATA_ROOT",
        "J1_RAGANYTHING_WORKDIR",
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL",
        "J1_VISION_LLM_BASE_URL",
    )


def test_preflight_with_empty_env_reports_missing_keys():
    result = run_preflight(env={}, probe_validation_service=False)
    assert isinstance(result, PreflightResult)
    assert not result.ready
    # Each missing env key surfaces as an issue.
    for key in PREFLIGHT_ENV_KEYS:
        assert any(key in issue for issue in result.issues), (
            f"expected an issue mentioning {key}"
        )
    # VLM action item is concrete + actionable.
    assert any(
        "VLM" in action or "raganything" in action.lower()
        for action in result.actions
    )


def test_preflight_with_full_env_clears_env_issues():
    result = run_preflight(
        env={
            "J1_DATA_ROOT": "/tmp/data",
            "J1_RAGANYTHING_WORKDIR": "/tmp/raganything",
            "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://vlm:8000",
            "J1_VISION_LLM_BASE_URL": "http://vlm:8000",
        },
        probe_validation_service=False,
    )
    # No env-related issues.
    assert all("env var" not in issue for issue in result.issues)
    # Note: ready is still False because we skipped the service
    # probe — the contract is that env satisfaction is necessary
    # but not sufficient.
    assert result.env_present == {
        "J1_DATA_ROOT": True,
        "J1_RAGANYTHING_WORKDIR": True,
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": True,
        "J1_VISION_LLM_BASE_URL": True,
    }


def test_preflight_either_vlm_var_suffices():
    """Operators may set only the project-wide fallback. The
    action message should NOT scold them in that case."""
    result = run_preflight(env={
        "J1_VISION_LLM_BASE_URL": "http://vlm:8000",
    }, probe_validation_service=False)
    # The VLM-specific action item should NOT appear when the
    # fallback is set.
    assert not any(
        "Set J1_RAGANYTHING_VLM_HTTP_SERVER_URL" in a
        for a in result.actions
    )


def test_preflight_to_dict_is_serialisable():
    result = run_preflight(env={}, probe_validation_service=False)
    payload = json.dumps(result.to_dict())
    assert "env_present" in payload
    assert "issues" in payload


# ---- Run command builder --------------------------------------


def test_build_run_command_uses_python_dash_m():
    cmd = build_run_command(
        tenant_id="t1", project_id="p1",
        fixture_path=Path("/tmp/eval/fixture.yaml"),
        output_dir=Path("/tmp/eval/out"),
    )
    assert cmd[0] == sys.executable
    assert cmd[1] == "-m"
    assert cmd[2] == "j1.tools.evaluate_memory_query"
    assert "--project-id" in cmd
    assert "p1" in cmd
    assert "--tenant-id" in cmd
    assert "t1" in cmd
    assert "--fixture" in cmd
    assert "/tmp/eval/fixture.yaml" in cmd
    assert "--output-dir" in cmd
    assert "/tmp/eval/out" in cmd


def test_build_run_command_omits_tenant_when_not_provided():
    cmd = build_run_command(
        tenant_id=None, project_id="p1",
        fixture_path=Path("/tmp/fixture.yaml"),
        output_dir=Path("/tmp/out"),
    )
    assert "--tenant-id" not in cmd


def test_build_run_command_supports_document_id():
    cmd = build_run_command(
        tenant_id="t1", project_id="p1",
        fixture_path=Path("/tmp/fixture.yaml"),
        output_dir=Path("/tmp/out"),
        document_id="doc-1",
    )
    assert "--document-id" in cmd
    assert "doc-1" in cmd


def test_build_run_command_uses_placeholders_when_project_id_missing():
    cmd = build_run_command(
        tenant_id=None, project_id=None,
        fixture_path=Path("/tmp/fixture.yaml"),
        output_dir=Path("/tmp/out"),
    )
    # Placeholder helps operators see the exact substitution point.
    assert "<PROJECT_ID>" in cmd


# ---- Manual next-steps ----------------------------------------


def test_manual_next_steps_lists_seven_items():
    steps = manual_next_steps(
        target_root=Path("/tmp/eval"),
        tenant_id="t1", project_id="p1",
        fixture_path=Path("/tmp/eval/memory_query_eval.yaml"),
        output_dir=Path("/tmp/out"),
    )
    # Seven numbered checklist items plus the embedded command
    # line — be tolerant of small wording drift, just assert the
    # critical anchors are present.
    text = "\n".join(steps)
    assert "Provision a project" in text
    assert "Upload" in text
    assert "compile" in text
    assert "enrichment" in text
    assert "knowledge_memory" in text.lower() or "Knowledge Memory" in text
    assert "evaluate_memory_query" in text


def test_manual_next_steps_embeds_run_command():
    steps = manual_next_steps(
        target_root=Path("/tmp/eval"),
        tenant_id="t1", project_id="p1",
        fixture_path=Path("/tmp/eval/memory_query_eval.yaml"),
        output_dir=Path("/tmp/out"),
    )
    text = "\n".join(steps)
    assert "p1" in text
    assert "/tmp/eval/memory_query_eval.yaml" in text


# ---- CLI surface ----------------------------------------------


def test_cli_help_runs():
    """`--help` must not require any runtime config."""
    result = subprocess.run(
        [
            sys.executable, "-m", "j1.tools.run_memory_eval_sample",
            "--help",
        ],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Stage the Phase 6B sample" in result.stdout
    assert "--data-root" in result.stdout
    assert "--run-evaluation" in result.stdout


def test_cli_staging_writes_status_artifact_without_running_eval(
    tmp_path: Path,
):
    """When the operator invokes the script WITHOUT
    `--run-evaluation`, the script stages + writes a status file
    + DOES NOT fabricate an evaluation report."""
    data_root = tmp_path / "data-root"
    output_dir = tmp_path / "out"
    result = subprocess.run(
        [
            sys.executable, "-m", "j1.tools.run_memory_eval_sample",
            "--data-root", str(data_root),
            "--output-dir", str(output_dir),
            "--skip-preflight",
        ],
        capture_output=True, text=True, check=False,
    )
    # Skip-preflight + no eval requested → return code 0.
    assert result.returncode == 0, result.stderr
    # Status artifact recorded, evaluation_run is False.
    status_path = output_dir / "sample_ingest_status.json"
    assert status_path.exists()
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["staged"] is True
    assert status["evaluation_run"] is False
    # No evaluation report was fabricated.
    assert not (output_dir / "memory_query_eval_report.json").exists()
    assert not (output_dir / "memory_query_eval_report.md").exists()
    # Sample files actually staged.
    for filename, _ in SAMPLE_FILES:
        assert (data_root / "data" / filename).exists()


def test_cli_refuses_to_run_evaluation_when_preflight_fails(
    tmp_path: Path,
):
    """Without --skip-preflight and without a real runtime, the
    script must refuse to invoke the harness — never fake."""
    data_root = tmp_path / "data-root"
    output_dir = tmp_path / "out"
    result = subprocess.run(
        [
            sys.executable, "-m", "j1.tools.run_memory_eval_sample",
            "--data-root", str(data_root),
            "--output-dir", str(output_dir),
            "--run-evaluation",
            "--project-id", "p1",
        ],
        capture_output=True, text=True, check=False,
        env={
            # Strip every preflight env key so preflight fails.
            "PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
        },
    )
    # Non-zero exit — refused to run.
    assert result.returncode != 0
    # No evaluation report was fabricated.
    assert not (output_dir / "memory_query_eval_report.json").exists()


def test_cli_run_evaluation_requires_project_id(tmp_path: Path):
    data_root = tmp_path / "data-root"
    output_dir = tmp_path / "out"
    result = subprocess.run(
        [
            sys.executable, "-m", "j1.tools.run_memory_eval_sample",
            "--data-root", str(data_root),
            "--output-dir", str(output_dir),
            "--run-evaluation",
            "--skip-preflight",
        ],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "--project-id" in result.stderr or "project" in result.stderr.lower()


# ---- No-LLM-imports regression guard --------------------------


def test_run_memory_eval_sample_has_no_llm_imports():
    """The script documents RAGAnything env var names in its
    preflight action messages — those strings are intentional and
    not imports. The guard scans only actual `import` / `from`
    lines to catch real LLM-client dependencies."""
    import importlib
    import inspect
    import re
    mod = importlib.import_module("j1.tools.run_memory_eval_sample")
    source = inspect.getsource(mod)
    import_lines = [
        line for line in source.splitlines()
        if re.match(r"^\s*(import|from)\s", line)
    ]
    import_blob = "\n".join(import_lines)
    forbidden = {
        "openai", "langchain", "anthropic", "raganything", "lightrag",
        "TextLLMClient", "VisionLLMClient",
    }
    leaked = [name for name in forbidden if name in import_blob]
    assert not leaked, (
        f"j1.tools.run_memory_eval_sample leaks LLM imports: "
        f"{leaked}"
    )
