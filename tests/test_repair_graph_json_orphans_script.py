"""Test that the orphan repair script invalidates the 7 known IDs.

Pins the documented artifact IDs from the 2026-05-12 validation
report so a future cleanup pass on the constant doesn't silently
drop them. Also exercises the targeted + project-wide sweep
composition in ``repair_graph_json_orphans.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.documents.artifact_state import (
    SEARCH_STATE_ACTIVE,
    SEARCH_STATE_INVALID,
)
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext

# Import via path-modified sys.path; the script lives at scripts/
# and uses ``deploy.dev._wiring`` (also importable from repo root).
from scripts.repair_graph_json_orphans import KNOWN_ORPHAN_IDS


_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1")


def test_known_orphan_id_catalogue_is_fourteen():
    """The script catalogues the IDs from BOTH validation reports
    — round 1 (7 IDs, 2026-05-12) and round 2 (7 NEW IDs,
    2026-05-13). The registry-level guard in
    ``JsonArtifactRegistry`` now prevents round 3, so the
    catalogue should not grow."""
    from scripts.repair_graph_json_orphans import (
        KNOWN_ORPHAN_IDS_ROUND_1,
        KNOWN_ORPHAN_IDS_ROUND_2,
    )
    assert len(KNOWN_ORPHAN_IDS_ROUND_1) == 7
    assert len(KNOWN_ORPHAN_IDS_ROUND_2) == 7
    assert len(KNOWN_ORPHAN_IDS) == 14
    assert set(KNOWN_ORPHAN_IDS_ROUND_1) == {
        "4e18439367214ebba1e574381c865dc5",
        "58e0330105004ed09e0b324471c77b12",
        "7cd322a9f6914f18b0f5c39d53d28540",
        "864f007845524b3ea86e54eb8a28154f",
        "e128fb3299b04fbf8310b8663502c650",
        "726c1ac859e741f393cd705b3aa5358c",
        "f791e6a61a0b429088ac83b348f1f568",
    }
    assert set(KNOWN_ORPHAN_IDS_ROUND_2) == {
        "ba061715712844efb1256e4347cd118e",
        "bf2c86a67f7e40bf97b4f6330ab5ed89",
        "da95e3c404b845b8a95e6e9eda1120e9",
        "6cf640b617e548b7966e63e281a56631",
        "ff72913f5c824bb886e42ec2661549d1",
        "009e5de5a0ed40ccb5394b33b8ee55a5",
        "87c0c75376434633892d05de9faae152",
    }
    # No overlap between the two rounds — round 2 is genuinely
    # new orphans created via a code path round 1 didn't cover.
    assert not (
        set(KNOWN_ORPHAN_IDS_ROUND_1) & set(KNOWN_ORPHAN_IDS_ROUND_2)
    )


class _Registry:
    """In-memory ArtifactNotFoundError-aware registry used to drive
    the repair logic without touching the JSONL store."""

    def __init__(self, records):
        self._by_id = {r.artifact_id: r for r in records}

    def get(self, ctx, artifact_id):  # noqa: ARG002
        from j1.artifacts.registry import ArtifactNotFoundError
        if artifact_id not in self._by_id:
            raise ArtifactNotFoundError(artifact_id)
        return self._by_id[artifact_id]

    def list_artifacts(self, ctx, *, kind=None):  # noqa: ARG002
        if kind is None:
            return list(self._by_id.values())
        return [r for r in self._by_id.values() if r.kind == kind]

    def update_metadata(self, ctx, artifact_id, new_metadata):  # noqa: ARG002
        prev = self._by_id[artifact_id]
        self._by_id[artifact_id] = ArtifactRecord(
            **{**prev.__dict__, "metadata": dict(new_metadata)},
        )


def _orphan_record(artifact_id, ctx):
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind="graph_json",
        location=f"graph/{artifact_id}.json",
        content_hash=f"hash-{artifact_id}",
        byte_size=10,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
        metadata={},  # no run_id — this is the bug
    )


def test_targeted_invalidation_path_exercises_known_ids(ctx):
    """The targeted path uses ``registry.get`` + ``update_metadata``
    for each known orphan ID. Build a registry containing all 14
    IDs (both rounds) and verify they all get flipped."""
    records = [_orphan_record(aid, ctx) for aid in KNOWN_ORPHAN_IDS]
    registry = _Registry(records)

    # Replay the script's targeted loop directly.
    from scripts.repair_graph_json_orphans import KNOWN_ORPHAN_IDS as IDS
    flipped = 0
    for artifact_id in IDS:
        record = registry.get(ctx, artifact_id)
        meta = dict(record.metadata or {})
        if meta.get("search_state") != SEARCH_STATE_INVALID:
            meta["search_state"] = SEARCH_STATE_INVALID
            meta["invalid_reason"] = "missing_run_id"
            registry.update_metadata(ctx, artifact_id, meta)
            flipped += 1

    assert flipped == 14
    for artifact_id in KNOWN_ORPHAN_IDS:
        rec = registry.get(ctx, artifact_id)
        assert rec.metadata["search_state"] == SEARCH_STATE_INVALID
        assert rec.metadata["invalid_reason"] == "missing_run_id"


def test_project_wide_sweep_catches_unlisted_orphans(ctx):
    """The repair script also calls the project-wide sweep so an
    orphan NOT on the explicit list (e.g. one created between the
    report and the repair run) still gets cleaned."""
    from j1.documents.artifact_state import (
        invalidate_lineage_missing_artifacts,
    )

    sibling = _orphan_record("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", ctx)
    sibling_ok = ArtifactRecord(
        **{**sibling.__dict__,
           "artifact_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
           "metadata": {"run_id": "run-ok",
                        "search_state": SEARCH_STATE_ACTIVE}},
    )
    registry = _Registry([sibling, sibling_ok])

    invalidated = invalidate_lineage_missing_artifacts(
        ctx=ctx, artifacts=registry,
    )
    assert invalidated == 1
    assert registry.get(
        ctx, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ).metadata["search_state"] == SEARCH_STATE_INVALID
    # The good row is untouched.
    assert registry.get(
        ctx, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ).metadata["search_state"] == SEARCH_STATE_ACTIVE
