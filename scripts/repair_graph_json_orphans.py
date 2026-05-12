"""One-off repair tool for graph_json orphans flagged in the
validation report from 2026-05-12.

Background
----------

The validation report listed 7 ``graph_json`` artifacts in the
retrieval index with ``run_id=None``:

  4e18439367214ebba1e574381c865dc5
  58e0330105004ed09e0b324471c77b12
  7cd322a9f6914f18b0f5c39d53d28540
  864f007845524b3ea86e54eb8a28154f
  e128fb3299b04fbf8310b8663502c650
  726c1ac859e741f393cd705b3aa5358c
  f791e6a61a0b429088ac83b348f1f568

These predate the producer-layer lineage stamping in
``_graph_drafts_from_storage``. The producer fix prevents NEW
orphans; this script cleans up the existing ones so retrieval +
validation stop surfacing them while the affected runs are
reindexed.

Behaviour
---------

For each known orphan ID:

  * Mark ``metadata.search_state = "invalid"`` so the retrieval
    layer's lifecycle filter drops the row.
  * Tag ``metadata.invalid_reason = "missing_run_id"`` so the
    audit trail records the cause.

After targeted invalidation, runs the project-wide
``invalidate_lineage_missing_artifacts`` sweep to catch any
sibling orphans not on the explicit list.

The artifact files themselves stay on disk (audit) — only the
retrieval visibility flips. Re-running this script is idempotent
(already-invalid rows are skipped).

Usage
-----

  python scripts/repair_graph_json_orphans.py \\
      --tenant <tenant-id> --project <project-id>

The tenant/project pair is required because the JSONL registry is
project-scoped. Re-run for each project that holds orphans.
"""

from __future__ import annotations

import argparse
import logging
import sys

# The 7 orphan IDs from the 2026-05-12 validation report. If a
# future report lists new IDs, append them here OR rely on the
# project-wide sweep — the targeted list is a belt-and-braces
# layer for the exact bugs we know about, not the only signal.
KNOWN_ORPHAN_IDS = (
    "4e18439367214ebba1e574381c865dc5",
    "58e0330105004ed09e0b324471c77b12",
    "7cd322a9f6914f18b0f5c39d53d28540",
    "864f007845524b3ea86e54eb8a28154f",
    "e128fb3299b04fbf8310b8663502c650",
    "726c1ac859e741f393cd705b3aa5358c",
    "f791e6a61a0b429088ac83b348f1f568",
)


def repair(*, tenant: str, project: str) -> int:
    from j1.artifacts.registry import ArtifactNotFoundError, JsonArtifactRegistry
    from j1.documents.artifact_state import (
        SEARCH_STATE_INVALID,
        invalidate_lineage_missing_artifacts,
    )
    from j1.projects.context import ProjectContext
    from j1.workspace.resolver import WorkspaceResolver

    # Build the same registry the REST app uses so we read/write the
    # same JSONL file. Mirrors deploy/dev/_wiring.py.
    from deploy.dev._wiring import build_settings, build_workspace
    settings = build_settings()
    workspace = build_workspace(settings)
    artifacts = JsonArtifactRegistry(workspace)
    ctx = ProjectContext(tenant_id=tenant, project_id=project)

    # 1) Targeted invalidation for the known IDs.
    targeted = 0
    for artifact_id in KNOWN_ORPHAN_IDS:
        try:
            record = artifacts.get(ctx, artifact_id)
        except ArtifactNotFoundError:
            logging.info(
                "orphan %s not found in (tenant=%s, project=%s); skipping",
                artifact_id, tenant, project,
            )
            continue
        meta = dict(record.metadata or {})
        if meta.get("search_state") == SEARCH_STATE_INVALID:
            logging.info("orphan %s already invalidated; skipping", artifact_id)
            continue
        meta["search_state"] = SEARCH_STATE_INVALID
        meta["invalid_reason"] = "missing_run_id"
        meta["invalidation_source"] = "repair_graph_json_orphans.py"
        try:
            artifacts.update_metadata(ctx, artifact_id, meta)
            targeted += 1
            logging.info("invalidated orphan %s", artifact_id)
        except Exception:
            logging.exception("failed to invalidate orphan %s", artifact_id)

    # 2) Project-wide sweep — catches any siblings the explicit
    # list missed.
    swept = invalidate_lineage_missing_artifacts(
        ctx=ctx, artifacts=artifacts,
    )

    print(
        f"repair complete · targeted={targeted} · "
        f"project_wide_swept={swept} · total={targeted + swept}"
    )
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True, help="Tenant id")
    parser.add_argument("--project", required=True, help="Project id")
    args = parser.parse_args()
    return repair(tenant=args.tenant, project=args.project)


if __name__ == "__main__":
    sys.exit(main())
