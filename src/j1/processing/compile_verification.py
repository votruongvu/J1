"""Post-compile output-health gate.

A narrow, cheap check that runs immediately after the compile activity
when the workflow opted in via `request.verify_after_compile`. It does
NOT read artifact contents — only inspects the KINDS the compile
produced — and fails fast when a clean "no chunks at all" or "index
ran but produced no manifest" symptom would otherwise slip through to
terminal SUCCEEDED.

Extracted from the old per-stage validator framework. This is the
ONLY validation-style check the v1 ingestion workflow still runs.
"""

from __future__ import annotations

from j1.processing.results import ARTIFACT_KIND_CHUNK


def verify_compile_output_health(
    *,
    artifact_kinds: tuple[str, ...],
    min_chunks: int = 1,
    require_index_manifest: bool = False,
) -> tuple[bool, str | None, str, int]:
    """Inspect the kinds of artifacts produced by compile (and index)
 and decide whether the run passes the post-compile verification
 gate.

 Returns `(passed, reason_code, message, chunk_count)`. `reason_code`
 is None on pass; otherwise one of the `FAILURE_CODE_*` strings
 defined in `j1.runs.models`. The reason-code vocabulary is the
 user-visible failure category — keep it stable.

 The check is intentionally narrow: no artifact reads, no schema
 validation. Catches the cheap cases that should never reach
 terminal SUCCEEDED:
   * compile reported success but produced zero chunk artifacts
   * index activity ran but no `index_manifest` artifact was produced
 """
    from j1.runs.models import (
        FAILURE_CODE_CHUNK_FAILED,
        FAILURE_CODE_INDEX_FAILED,
    )

    chunk_count = sum(1 for k in artifact_kinds if k == ARTIFACT_KIND_CHUNK)
    if chunk_count < min_chunks:
        return (
            False,
            FAILURE_CODE_CHUNK_FAILED,
            (
                f"compile produced {chunk_count} chunk artifact(s) but "
                f"verification requires at least {min_chunks}"
            ),
            chunk_count,
        )
    if require_index_manifest:
        has_index_manifest = any(
            k == "index_manifest" for k in artifact_kinds
        )
        if not has_index_manifest:
            return (
                False,
                FAILURE_CODE_INDEX_FAILED,
                (
                    "index activity ran but no `index_manifest` artifact "
                    "was produced — index health cannot be verified"
                ),
                chunk_count,
            )
    return (True, None, "compile output passed verification", chunk_count)


__all__ = ["verify_compile_output_health"]
