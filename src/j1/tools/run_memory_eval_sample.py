"""Phase 6B — sample memory query evaluation driver.

Assisted workflow tool for the Phase 6 A/B harness. Stages a
small reusable sample dataset on disk, runs a real preflight
against the dev runtime, and either invokes the Phase 6 evaluator
as a subprocess (when preflight passes) or prints exact manual
steps to unblock the run (when preflight fails).

The shipped sample dataset under
``dev_fixtures/memory_eval/<sample>/`` is a domain-specific
fixture authored outside the core source tree — this tool itself
stays domain-neutral and works against any dataset that follows
the same five-file + fixture layout.

Strict scope (Phase 6B):

  * **No backend changes.** This tool only orchestrates: stages
    files, validates environment, invokes the existing CLI.
  * **No faked results.** Preflight failures NEVER produce a
    placeholder evaluation report. The tool exits non-zero with
    a precise next-step.
  * **No LLM calls of its own.** Any LLM work happens inside the
    downstream CLI (`j1.tools.evaluate_memory_query`), which goes
    through the production validation surface.
  * **Defaults stay default-off.** When the tool sets memory env
    vars for the subprocess, they're scoped to that subprocess
    only via the subprocess `env=` argument — never written to
    the parent process or any persisted config.

Typical usage:

  python -m j1.tools.run_memory_eval_sample \\
      --data-root /tmp/j1-memory-eval \\
      --output-dir artifacts/memory_eval/sample \\
      --tenant-id <tenant> --project-id <project>

  # Optionally request the script to invoke the harness directly
  # after staging:
  python -m j1.tools.run_memory_eval_sample \\
      --data-root /tmp/j1-memory-eval \\
      --output-dir artifacts/memory_eval/sample \\
      --tenant-id <tenant> --project-id <project> \\
      --run-evaluation
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_log = logging.getLogger("j1.tools.run_memory_eval_sample")


__all__ = [
    "PreflightResult",
    "SAMPLE_FILES",
    "SAMPLE_FIXTURE_NAME",
    "build_run_command",
    "build_sample_manifest",
    "fixture_source_path",
    "main",
    "manual_next_steps",
    "run_preflight",
    "stage_sample_dataset",
]


# ---- Layout ----------------------------------------------------


# Sample dataset files — names + their declared "kind" for the
# manifest. The "kind" string is descriptive; the J1 dev pipeline
# decides the real artifact_kind during ingest.
SAMPLE_FILES: tuple[tuple[str, str], ...] = (
    ("CE-001_BOQ_quantity_schedule.md", "boq"),
    ("CE-002_site_inspection_report.md", "inspection_report"),
    ("CE-003_NCR_corrective_action.md", "ncr"),
    ("CE-004_structural_calculation_summary.md", "calculation"),
    ("CE-005_drawing_register_and_specification.md", "drawing_spec"),
)

SAMPLE_FIXTURE_NAME = "memory_query_eval.yaml"


SAMPLE_DIRECTORY_NAME = "sample"


def _sample_root() -> Path:
    """The repo-relative directory carrying the canonical sample
    dataset + fixture. The script copies from here to the caller's
    target ``--data-root``."""
    return (
        Path(__file__).resolve().parents[3]
        / "dev_fixtures"
        / "memory_eval"
        / SAMPLE_DIRECTORY_NAME
    )


def fixture_source_path() -> Path:
    """Path to the canonical fixture YAML in the repo. Tests use
    this to assert the fixture parses without depending on the
    caller's ``--data-root``."""
    return _sample_root() / SAMPLE_FIXTURE_NAME


# ---- Staging ---------------------------------------------------


def stage_sample_dataset(target_root: Path) -> dict[str, Any]:
    """Copy the canonical sample data + fixture into ``target_root``.

    Layout under ``target_root``:

      target_root/
        data/
          CE-001_*.md
          ...
        memory_query_eval.yaml
        sample_project_manifest.json

    Idempotent — re-running the staging step overwrites prior
    copies. The manifest records the file paths + intended
    metadata so a follow-up tool (REST upload, manual operator
    flow) can map each file to an expected project document."""
    src_dir = _sample_root()
    if not src_dir.exists():
        raise FileNotFoundError(
            f"sample dataset directory not found at {src_dir}. The "
            f"Phase 6B repo layout expects "
            f"dev_fixtures/memory_eval/{SAMPLE_DIRECTORY_NAME}/"
        )

    data_target = target_root / "data"
    data_target.mkdir(parents=True, exist_ok=True)
    for filename, _ in SAMPLE_FILES:
        src = src_dir / "data" / filename
        if not src.exists():
            raise FileNotFoundError(
                f"sample file missing: {src}"
            )
        shutil.copy2(src, data_target / filename)

    fixture_src = src_dir / SAMPLE_FIXTURE_NAME
    if not fixture_src.exists():
        raise FileNotFoundError(
            f"sample fixture missing: {fixture_src}"
        )
    fixture_target = target_root / SAMPLE_FIXTURE_NAME
    shutil.copy2(fixture_src, fixture_target)

    manifest = build_sample_manifest(target_root)
    manifest_path = target_root / "sample_project_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8",
    )
    return manifest


SAMPLE_INFO_FILENAME = "sample_info.json"


def build_sample_manifest(target_root: Path) -> dict[str, Any]:
    """Build the manifest dict written alongside the staged data.

    Records the absolute file paths + the intended "kind" string
    so the operator can upload each file to a real J1 project and
    see the expected document mapping. Includes no project / run /
    artifact ids — those are produced by the ingest pipeline.

    The `domain` + `description` fields come from a
    ``sample_info.json`` adjacent to the source dataset when
    present (so domain-specific labelling lives outside the tool's
    source); otherwise generic defaults are used."""
    data_target = target_root / "data"
    documents = []
    for filename, kind in SAMPLE_FILES:
        path = data_target / filename
        documents.append({
            "filename": filename,
            "kind": kind,
            "absolute_path": str(path),
            "exists": path.exists(),
            "byte_size": path.stat().st_size if path.exists() else None,
        })
    info = _load_sample_info()
    return {
        "domain": info.get("domain") or "generic",
        "description": info.get("description") or (
            "Phase 6B sample dataset — five linked documents "
            "exercising the Phase 4/5A/5B Knowledge Memory query "
            "path. See the dataset's README for the per-document "
            "purpose."
        ),
        "fixture": str(target_root / SAMPLE_FIXTURE_NAME),
        "data_directory": str(data_target),
        "documents": documents,
    }


def _load_sample_info() -> dict[str, Any]:
    """Read the optional ``sample_info.json`` from the source
    dataset directory. Used to pull domain-specific manifest
    labels without baking them into this module's source. Returns
    an empty dict when the file is absent or malformed."""
    info_path = _sample_root() / SAMPLE_INFO_FILENAME
    if not info_path.exists():
        return {}
    try:
        payload = json.loads(info_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


# ---- Preflight -------------------------------------------------


# Env keys checked by preflight. Mirror the failure modes Phase 6A
# discovered. Tests assert the set is stable.
PREFLIGHT_ENV_KEYS: tuple[str, ...] = (
    "J1_DATA_ROOT",
    "J1_RAGANYTHING_WORKDIR",
    # Either one of these two — operators may use the project-wide
    # vision LLM fallback rather than the RAGAnything-specific key.
    "J1_RAGANYTHING_VLM_HTTP_SERVER_URL",
    "J1_VISION_LLM_BASE_URL",
)


@dataclass
class PreflightResult:
    """Outcome of the runtime preflight. ``ready`` is True iff the
    sample dataset CAN be staged AND the harness can be invoked
    without an immediate runtime error. Each failure surfaces a
    precise human-readable ``action`` so the operator knows what
    to fix."""

    ready: bool = False
    env_present: dict[str, bool] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    validation_service_ready: bool = False
    orchestrator_ready: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "env_present": dict(self.env_present),
            "issues": list(self.issues),
            "actions": list(self.actions),
            "validation_service_ready": self.validation_service_ready,
            "orchestrator_ready": self.orchestrator_ready,
        }


def run_preflight(
    *,
    env: dict[str, str] | None = None,
    probe_validation_service: bool = True,
) -> PreflightResult:
    """Check whether the dev runtime is ready for a real evaluation.

    Returns a :class:`PreflightResult` that the caller renders. The
    function is split out of ``main`` so tests can drive it with a
    controlled env and verify the failure-mode messaging without
    mutating process state.

    ``probe_validation_service=False`` skips the import-and-construct
    step (useful for fast preflight inspection where the operator
    only wants the env-key audit). The full path is the default."""
    src = env if env is not None else os.environ
    result = PreflightResult()
    # ---- Env keys ---------------------------------------------
    for key in PREFLIGHT_ENV_KEYS:
        present = bool(src.get(key))
        result.env_present[key] = present
        if not present:
            result.issues.append(f"env var not set: {key}")
    if not src.get("J1_RAGANYTHING_VLM_HTTP_SERVER_URL") and not src.get(
        "J1_VISION_LLM_BASE_URL",
    ):
        result.actions.append(
            "Set J1_RAGANYTHING_VLM_HTTP_SERVER_URL (or the "
            "project-wide J1_VISION_LLM_BASE_URL fallback) to a "
            "running VLM endpoint. See docs/raganything-vlm-setup.md."
        )
    if not src.get("J1_DATA_ROOT"):
        result.actions.append(
            "Set J1_DATA_ROOT to a writable workspace path so the "
            "dev composition root can locate JSONL stores."
        )
    if not src.get("J1_RAGANYTHING_WORKDIR"):
        result.actions.append(
            "Set J1_RAGANYTHING_WORKDIR so RAGAnything writes its "
            "per-snapshot LightRAG workdirs in a predictable place."
        )

    # ---- Validation service construct probe -------------------
    if probe_validation_service:
        try:
            from deploy.dev._wiring import (
                build_validation_service_for_tool,
            )
        except ImportError as exc:
            result.issues.append(
                f"deploy.dev._wiring not importable: {exc}"
            )
            result.actions.append(
                "This script requires the dev composition root. "
                "Run from the repo with the dev install."
            )
        else:
            try:
                service = build_validation_service_for_tool(
                    tenant_id=src.get("J1_DEFAULT_TENANT_ID") or "preflight",
                    project_id=(
                        src.get("J1_DEFAULT_PROJECT_ID") or "preflight"
                    ),
                )
                result.validation_service_ready = service is not None
                if service is not None:
                    orch = getattr(
                        service, "_smart_query_orchestrator", None,
                    )
                    if orch is None:
                        orch = getattr(
                            service, "smart_query_orchestrator", None,
                        )
                    result.orchestrator_ready = orch is not None
                    if orch is None:
                        result.issues.append(
                            "validation_service constructed but "
                            "SmartQueryOrchestrator is unwired"
                        )
                        result.actions.append(
                            "The validation service can be built but"
                            " the query orchestrator is unavailable."
                            " Without it every query raises. Most"
                            " common cause is the VLM endpoint env"
                            " var; check the WARNING log lines"
                            " emitted during construction."
                        )
            except SystemExit as exc:
                # The wrapper raises SystemExit with the precise
                # cause; capture it verbatim.
                result.issues.append(str(exc))
                result.actions.append(
                    "Resolve the SystemExit cause above; "
                    "build_validation_service_for_tool documents "
                    "the failure modes."
                )
            except Exception as exc:  # noqa: BLE001
                result.issues.append(
                    f"validation_service construct raised: "
                    f"{type(exc).__name__}: {exc}"
                )
                result.actions.append(
                    "Inspect the traceback above; the dev wiring "
                    "raised before the validation service could be "
                    "constructed."
                )
    result.ready = (
        not result.issues
        and result.validation_service_ready
        and result.orchestrator_ready
    )
    return result


# ---- Manual next-steps --------------------------------------


def manual_next_steps(
    *,
    target_root: Path,
    tenant_id: str | None,
    project_id: str | None,
    fixture_path: Path,
    output_dir: Path,
) -> list[str]:
    """Build the operator-facing checklist printed when preflight
    isn't green AND the caller didn't ask the script to fail fast.

    Each step is self-contained and pinned to the staged data
    directory + the operator-supplied project/tenant ids."""
    project_label = project_id or "<PROJECT_ID>"
    tenant_label = tenant_id or "<TENANT_ID>"
    return [
        (
            f"1. Provision a project: ensure tenant '{tenant_label}' "
            f"+ project '{project_label}' exist in the dev "
            "deployment."
        ),
        (
            f"2. Upload the five sample files from "
            f"{target_root / 'data'} to that project via the dev "
            "UI / REST API (`POST /documents`). Wait for compile to "
            "succeed for each."
        ),
        (
            "3. Run post-compile domain enrichment for each "
            "document (manual action or auto-build trigger)."
        ),
        (
            "4. Build / rebuild Knowledge Memory for each document: "
            "manual action `build_knowledge_memory` or set "
            "J1_KNOWLEDGE_MEMORY_AUTO_BUILD_ENABLED=true + "
            "J1_KNOWLEDGE_MEMORY_REBUILD_AFTER_ENRICHMENT=true for "
            "the worker before step 3."
        ),
        (
            "5. Verify each document's knowledge_memory status is "
            "`updated_with_domain_insights` via `GET /documents/"
            "{id}/knowledge-memory` or the Document Detail UI."
        ),
        (
            "6. Run the Phase 6 A/B harness against the staged "
            "fixture — exact command:"
        ),
        f"     {' '.join(build_run_command(tenant_id=tenant_id, project_id=project_id, fixture_path=fixture_path, output_dir=output_dir))}",
        (
            "7. Inspect the generated reports "
            f"({output_dir / 'memory_query_eval_report.md'} + .json)."
            " Apply the Phase 6A analysis prompt to decide on "
            "default enablement."
        ),
    ]


def build_run_command(
    *,
    tenant_id: str | None,
    project_id: str | None,
    fixture_path: Path,
    output_dir: Path,
    document_id: str | None = None,
) -> list[str]:
    """Build the exact ``j1.tools.evaluate_memory_query`` argv the
    operator should run. Tests assert on this list directly so the
    script and the printed checklist stay in sync."""
    cmd = [
        sys.executable, "-m", "j1.tools.evaluate_memory_query",
        "--project-id", project_id or "<PROJECT_ID>",
        "--fixture", str(fixture_path),
        "--output-dir", str(output_dir),
    ]
    if tenant_id:
        cmd.extend(["--tenant-id", tenant_id])
    if document_id:
        cmd.extend(["--document-id", document_id])
    return cmd


# ---- Subprocess invocation ----------------------------------


def _run_evaluation_subprocess(
    *,
    tenant_id: str | None,
    project_id: str | None,
    fixture_path: Path,
    output_dir: Path,
    document_id: str | None,
    extra_env: dict[str, str] | None = None,
) -> int:
    """Invoke `j1.tools.evaluate_memory_query` as a subprocess with
    the memory + expansion flags set ONLY for the child env. The
    parent shell's env stays untouched — Phase 6B's "do not flip
    defaults" rule is enforced via env isolation."""
    cmd = build_run_command(
        tenant_id=tenant_id, project_id=project_id,
        fixture_path=fixture_path, output_dir=output_dir,
        document_id=document_id,
    )
    env = dict(os.environ)
    # Memory + expansion ON for the variant; the harness itself
    # toggles per-query within the child process. Setting them at
    # the subprocess level just guarantees a sane starting point.
    env.setdefault("J1_QUERY_KNOWLEDGE_MEMORY_ENABLED", "true")
    env.setdefault("J1_QUERY_EXPANSION_ENABLED", "true")
    if extra_env:
        env.update(extra_env)
    _log.info("invoking eval harness: %s", " ".join(cmd))
    result = subprocess.run(cmd, env=env, check=False)
    return int(result.returncode)


# ---- CLI ----------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="j1.tools.run_memory_eval_sample",
        description=(
            "Stage the Phase 6B sample dataset, run a preflight"
            " against the dev runtime, and optionally invoke the"
            " Phase 6 A/B evaluation harness."
        ),
    )
    parser.add_argument(
        "--data-root", type=Path, required=True,
        help=(
            "Directory the staged sample will live under. The script"
            " creates `data/` + the fixture YAML + the manifest"
            " under this path."
        ),
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True,
        help="Where the evaluation reports will be written.",
    )
    parser.add_argument(
        "--tenant-id", default=None,
        help="Tenant id for the eval target. Optional.",
    )
    parser.add_argument(
        "--project-id", default=None,
        help=(
            "Project id for the eval target. Required when "
            "--run-evaluation is set."
        ),
    )
    parser.add_argument(
        "--document-id", default=None,
        help="Optional document id for a document-scoped run.",
    )
    parser.add_argument(
        "--fixture", type=Path, default=None,
        help=(
            "Override the staged fixture path. Defaults to the YAML"
            " staged under --data-root."
        ),
    )
    parser.add_argument(
        "--skip-preflight", action="store_true",
        help=(
            "Skip the runtime preflight — useful when the operator"
            " has already verified the environment. Does NOT skip"
            " the staging step."
        ),
    )
    parser.add_argument(
        "--run-evaluation", action="store_true",
        help=(
            "After staging + preflight, invoke "
            "`j1.tools.evaluate_memory_query` as a subprocess. The"
            " subprocess inherits the parent env plus memory + "
            "expansion flags scoped to the child only."
        ),
    )
    parser.add_argument(
        "--strict", action="store_true",
        help=(
            "Pass --strict to the eval subprocess (non-zero exit on"
            " safety violations)."
        ),
    )
    return parser.parse_args(argv)


def _print_preflight(result: PreflightResult) -> None:
    print("=== Preflight ===")
    for key, present in result.env_present.items():
        print(
            f"  {key}: {'set' if present else 'NOT SET'}",
        )
    print(
        f"  validation_service_ready: "
        f"{'yes' if result.validation_service_ready else 'no'}",
    )
    print(
        f"  orchestrator_ready: "
        f"{'yes' if result.orchestrator_ready else 'no'}",
    )
    if result.issues:
        print("\nIssues:")
        for issue in result.issues:
            print(f"  - {issue}")
    if result.actions:
        print("\nNext steps:")
        for action in result.actions:
            print(f"  - {action}")
    print()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(level=os.environ.get("J1_LOG_LEVEL", "INFO"))
    data_root = args.data_root.resolve()
    output_dir = args.output_dir.resolve()

    print(f"Staging sample dataset under {data_root}...")
    try:
        manifest = stage_sample_dataset(data_root)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(
        f"Staged {len(manifest['documents'])} sample documents + "
        f"fixture + manifest at {data_root}",
    )
    fixture_path = args.fixture or (data_root / SAMPLE_FIXTURE_NAME)

    # Preflight — the operator MAY skip this when they're certain
    # the runtime is already correct, but the default is to run it.
    preflight: PreflightResult
    if args.skip_preflight:
        preflight = PreflightResult(ready=True)
        print("Preflight skipped (--skip-preflight).")
    else:
        preflight = run_preflight()
        _print_preflight(preflight)

    if args.run_evaluation:
        if not args.project_id:
            print(
                "ERROR: --run-evaluation requires --project-id.",
                file=sys.stderr,
            )
            return 2
        if not preflight.ready and not args.skip_preflight:
            print(
                "ERROR: preflight failed; refusing to invoke the"
                " evaluation harness because the run would only"
                " surface harness warnings, not real query data."
                " Resolve the preflight issues above or pass"
                " --skip-preflight if you've validated the runtime"
                " separately.",
                file=sys.stderr,
            )
            return 3
        rc = _run_evaluation_subprocess(
            tenant_id=args.tenant_id,
            project_id=args.project_id,
            fixture_path=fixture_path,
            output_dir=output_dir,
            document_id=args.document_id,
        )
        return rc

    # Print manual next-steps when not running the evaluation
    # ourselves — gives the operator the exact command + ordered
    # checklist.
    output_dir.mkdir(parents=True, exist_ok=True)
    steps = manual_next_steps(
        target_root=data_root,
        tenant_id=args.tenant_id,
        project_id=args.project_id,
        fixture_path=fixture_path,
        output_dir=output_dir,
    )
    print("=== Next steps ===")
    for step in steps:
        print(step)
    print()

    # Persist a small status artifact so subsequent automation can
    # discover what was staged. NOT a fake evaluation report — it
    # explicitly records `evaluation_run=false`.
    status_path = output_dir / "sample_ingest_status.json"
    status = {
        "staged": True,
        "preflight_ready": preflight.ready,
        "preflight": preflight.to_dict(),
        "evaluation_run": False,
        "manifest_path": str(data_root / "sample_project_manifest.json"),
        "fixture_path": str(fixture_path),
        "output_dir": str(output_dir),
    }
    status_path.write_text(
        json.dumps(status, indent=2) + "\n", encoding="utf-8",
    )
    return 0 if preflight.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
