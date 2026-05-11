"""Per-stage validation check functions.

Each validator takes the artifacts a stage produced + a read-back
function (so the validator can verify content, not just registry
existence) and returns a list of `StageValidationCheck`. The
caller (the `validate_stage` activity) aggregates the checks into
a `StageValidationResult` via `aggregate_status`.

Validators are PURE (no I/O of their own beyond the injected
read-back function) so they're easy to unit-test without standing
up a workspace. The activity is the I/O boundary.

Adding a new stage validator:
 1. Add the per-stage check function here (e.g. `validate_index`).
 2. Wire it into `validate_stage` activity's stage dispatch.
 3. Add the workflow gate that calls `validate_stage` for that
 step + records COMPLETED only on `result.passed`.
 4. Add unit tests in `tests/test_stage_validators.py`.
 5. Update the architecture docs (under `docs/architecture/`)
 with the new stage's required-output + quality checks.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from j1.artifacts.models import ArtifactRecord
from j1.processing.results import (
    ARTIFACT_KIND_CHUNK,
    ARTIFACT_KIND_PARSED_CONTENT_MANIFEST,
    ARTIFACT_KIND_PARSED_SOURCE,
)
from j1.processing.stage_validation import (
    CHECK_STATUS_FAILED,
    CHECK_STATUS_PASSED,
    CHECK_STATUS_WARNING,
    StageValidationCheck,
)

# Type alias for the read-back function injected by the activity.
# Returns the raw bytes of the artifact's persisted file, or None
# when the file is missing / unreadable. Validators check `is None`
# to surface "artifact registered but storage empty" failures.
ReadBack = Callable[[ArtifactRecord], bytes | None]


def _passed(name: str, message: str | None = None) -> StageValidationCheck:
    return StageValidationCheck(
        name=name, status=CHECK_STATUS_PASSED, message=message,
    )


def _warning(name: str, message: str) -> StageValidationCheck:
    return StageValidationCheck(
        name=name, status=CHECK_STATUS_WARNING, message=message,
    )


def _failed(name: str, message: str) -> StageValidationCheck:
    return StageValidationCheck(
        name=name, status=CHECK_STATUS_FAILED, message=message,
    )


def _check_scope(
    artifact: ArtifactRecord,
    *,
    expected_tenant: str,
    expected_project: str,
    expected_run_id: str | None,
    expected_document_id: str | None,
) -> list[StageValidationCheck]:
    """Common scope check applied to every artifact: tenant, project,
 run_id (via metadata), document_id (via source_document_ids).
 Catches "artifact landed in the wrong project" bugs that would
 otherwise bleed across tenants — same defense the artifact
 registry's reads enforce, but at validation time so the gate
 fires before the stage is marked COMPLETED."""
    out: list[StageValidationCheck] = []
    if artifact.project.tenant_id != expected_tenant:
        out.append(_failed(
            "scope_tenant_match",
            f"artifact {artifact.artifact_id} tenant "
            f"{artifact.project.tenant_id!r} != expected {expected_tenant!r}",
        ))
    elif artifact.project.project_id != expected_project:
        out.append(_failed(
            "scope_project_match",
            f"artifact {artifact.artifact_id} project "
            f"{artifact.project.project_id!r} != expected {expected_project!r}",
        ))
    else:
        out.append(_passed("scope_tenant_project_match"))
    # run_id is metadata.run_id by convention. Don't fail when
    # expected_run_id is None (the resolver's lineage path can
    # surface artifacts without explicit tagging) — just skip.
    if expected_run_id is not None:
        tagged = (
            artifact.metadata.get("run_id")
            if isinstance(artifact.metadata, dict) else None
        )
        if tagged is not None and tagged != expected_run_id:
            out.append(_failed(
                "scope_run_id_match",
                f"artifact {artifact.artifact_id} metadata.run_id "
                f"{tagged!r} != expected {expected_run_id!r}",
            ))
    if expected_document_id is not None:
        if (
            artifact.source_document_ids
            and expected_document_id not in artifact.source_document_ids
        ):
            out.append(_failed(
                "scope_document_link",
                f"artifact {artifact.artifact_id} source_document_ids "
                f"{artifact.source_document_ids!r} missing expected "
                f"{expected_document_id!r}",
            ))
    return out


def _check_readable(
    artifact: ArtifactRecord, read_back: ReadBack,
) -> tuple[bytes | None, StageValidationCheck]:
    """Read-back gate. Returns (content, check) — content is None on
 failure so downstream content checks can short-circuit."""
    try:
        content = read_back(artifact)
    except Exception as exc:  # noqa: BLE001 — defensive
        return None, _failed(
            "artifact_readable",
            f"artifact {artifact.artifact_id} read raised "
            f"{type(exc).__name__}: {exc}",
        )
    if content is None:
        return None, _failed(
            "artifact_readable",
            f"artifact {artifact.artifact_id} file missing or unreadable "
            f"(registry says location={artifact.location!r})",
        )
    if len(content) == 0:
        return content, _failed(
            "artifact_non_empty",
            f"artifact {artifact.artifact_id} file is zero bytes",
        )
    return content, _passed("artifact_readable")


# ---- Compile ---------------------------------------------------------


def validate_compile(
    *,
    artifacts: list[ArtifactRecord],
    expected_tenant: str,
    expected_project: str,
    expected_run_id: str,
    expected_document_id: str,
    read_back: ReadBack,
) -> list[StageValidationCheck]:
    """Compile produces parsed_content_manifest / chunk artifacts
 (RAGAnything's `process_document_complete` returns parse + chunk
 output together). Required:
 * at least one artifact registered.
 * each artifact's file readable + non-empty.
 * scope (tenant, project, run_id, document_id) matches.
 * at least one of {parsed_source, parsed_content_manifest, chunk}
 kinds present — empty compile is a contract violation.

 Quality checks: warn when no `parsed_content_manifest` is present
 (Content Inventory tab will be unavailable) but at least one
 chunk is."""
    checks: list[StageValidationCheck] = []
    if not artifacts:
        checks.append(_failed(
            "compile_artifacts_present",
            "compile reported succeeded but produced ZERO artifacts",
        ))
        return checks
    checks.append(_passed("compile_artifacts_present"))
    kinds_present = {a.kind for a in artifacts}
    canonical = {
        ARTIFACT_KIND_PARSED_SOURCE,
        ARTIFACT_KIND_PARSED_CONTENT_MANIFEST,
        ARTIFACT_KIND_CHUNK,
    }
    if not kinds_present & canonical:
        checks.append(_failed(
            "compile_canonical_kinds",
            "compile produced artifacts but none of "
            f"{sorted(canonical)!r} — downstream stages will see no input",
        ))
    else:
        checks.append(_passed("compile_canonical_kinds"))
    if ARTIFACT_KIND_PARSED_CONTENT_MANIFEST not in kinds_present:
        checks.append(_warning(
            "compile_manifest_present",
            "no parsed_content_manifest artifact — Content Inventory "
            "tab will report 'unavailable' for this run",
        ))
    for a in artifacts:
        checks.extend(_check_scope(
            a,
            expected_tenant=expected_tenant,
            expected_project=expected_project,
            expected_run_id=expected_run_id,
            expected_document_id=expected_document_id,
        ))
        _content, read_check = _check_readable(a, read_back)
        checks.append(read_check)
    return checks


# ---- Generate chunks ------------------------------------------------


def _decode_chunks(content: bytes) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Tolerant chunk decoder. Mirrors `_parse_artifact` in the
 chunks projector but flat — returns the list of chunk dicts +
 an optional error message. Used by the chunks validator to
 enforce count / id-uniqueness / scope checks."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, f"chunk artifact bytes are not utf-8: {exc}"
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        # Try NDJSON.
        chunks: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                return None, "chunk artifact is neither valid JSON nor NDJSON"
            if isinstance(entry, dict):
                chunks.append(entry)
        return chunks, None
    if isinstance(raw, list):
        return [c for c in raw if isinstance(c, dict)], None
    if isinstance(raw, dict):
        if isinstance(raw.get("chunks"), list):
            return [
                c for c in raw["chunks"] if isinstance(c, dict)
            ], None
        return [raw], None
    return None, f"chunk artifact top-level type {type(raw).__name__} not recognised"


def validate_chunks(
    *,
    artifacts: list[ArtifactRecord],
    expected_tenant: str,
    expected_project: str,
    expected_run_id: str,
    expected_document_id: str,
    read_back: ReadBack,
) -> list[StageValidationCheck]:
    """Generate-chunks produces one or more `chunk` artifacts.
 Required:
 * at least one chunk artifact registered.
 * each chunk file readable, non-empty, parses as JSON / NDJSON.
 * total chunk count > 0 across all artifacts.
 * every chunk has an id (or is at a position the projector can
 synthesise an id for).
 * chunk ids are unique across the run.
 * scope matches.

 Quality warnings:
 * `chunk_text_present` — chunks where every entry has empty
 `body`/`content` are flagged (might be a parser regression).
 * `chunk_count_distribution` — one giant chunk vs. many tiny
 chunks. Both signal a chunking misconfiguration."""
    checks: list[StageValidationCheck] = []
    chunk_artifacts = [
        a for a in artifacts if a.kind == ARTIFACT_KIND_CHUNK
    ]
    if not chunk_artifacts:
        checks.append(_failed(
            "chunk_artifacts_present",
            "generate_chunks reported succeeded but produced ZERO "
            "chunk artifacts — downstream graph + index have no input",
        ))
        return checks
    checks.append(_passed("chunk_artifacts_present"))
    total_chunks = 0
    seen_ids: set[str] = set()
    duplicate_ids: list[str] = []
    body_present = 0
    body_missing = 0
    body_lengths: list[int] = []
    for a in artifacts:
        # Scope checks fire on EVERY artifact (chunk or otherwise) the
        # stage produced. Lets a misconfigured run that emits one
        # chunk in the right project + one in the wrong project be
        # caught instead of slipping through.
        checks.extend(_check_scope(
            a,
            expected_tenant=expected_tenant,
            expected_project=expected_project,
            expected_run_id=expected_run_id,
            expected_document_id=expected_document_id,
        ))
    for a in chunk_artifacts:
        content, read_check = _check_readable(a, read_back)
        checks.append(read_check)
        if content is None:
            continue
        decoded, err = _decode_chunks(content)
        if decoded is None or err is not None:
            checks.append(_failed(
                "chunk_artifact_parses",
                f"chunk artifact {a.artifact_id}: {err}",
            ))
            continue
        if not decoded:
            checks.append(_failed(
                "chunk_artifact_non_empty",
                f"chunk artifact {a.artifact_id} parsed but contains "
                "zero chunks",
            ))
            continue
        for index, entry in enumerate(decoded):
            total_chunks += 1
            chunk_id = (
                entry.get("chunk_id")
                or entry.get("chunkId")
                or entry.get("id")
                or f"{a.artifact_id}#{index}"
            )
            if chunk_id in seen_ids:
                duplicate_ids.append(str(chunk_id))
            else:
                seen_ids.add(str(chunk_id))
            body = (
                entry.get("body")
                or entry.get("content")
                or entry.get("text")
                or ""
            )
            if isinstance(body, str) and body.strip():
                body_present += 1
                body_lengths.append(len(body))
            else:
                body_missing += 1

    if total_chunks == 0:
        checks.append(_failed(
            "chunk_count_positive",
            "no chunks parsed across any chunk artifact — stage cannot "
            "be marked succeeded with zero chunks",
        ))
    else:
        checks.append(_passed(
            "chunk_count_positive",
            f"{total_chunks} chunks total across {len(chunk_artifacts)} "
            "artifact(s)",
        ))
    if duplicate_ids:
        checks.append(_failed(
            "chunk_ids_unique",
            f"{len(duplicate_ids)} duplicate chunk id(s); first few: "
            + ", ".join(duplicate_ids[:3]),
        ))
    elif total_chunks > 0:
        checks.append(_passed("chunk_ids_unique"))
    if total_chunks > 0:
        if body_missing > 0:
            severity = (
                CHECK_STATUS_FAILED if body_present == 0
                else CHECK_STATUS_WARNING
            )
            checks.append(StageValidationCheck(
                name="chunk_text_present",
                status=severity,
                message=(
                    f"{body_missing}/{total_chunks} chunks have empty "
                    "body/content/text"
                ),
            ))
        else:
            checks.append(_passed("chunk_text_present"))
        # Distribution check — only meaningful with more than 1 chunk.
        if len(body_lengths) > 1:
            biggest = max(body_lengths)
            total_size = sum(body_lengths)
            if total_size > 0 and biggest / total_size > 0.9:
                checks.append(_warning(
                    "chunk_size_distribution",
                    "one chunk holds >90% of the total body bytes — "
                    "chunking likely misconfigured",
                ))
    return checks


# ---- Enrich ---------------------------------------------------------


def validate_enrich(
    *,
    artifacts: list[ArtifactRecord],
    expected_tenant: str,
    expected_project: str,
    expected_run_id: str,
    expected_document_id: str | None,
    enrich_required: bool,
    read_back: ReadBack,
) -> list[StageValidationCheck]:
    """Enrich produces enriched.* artifacts (e.g. enriched.tables,
 enriched.images). Validation depends on whether enrich was
 required:

 * `enrich_required=True`: at least one enriched artifact must
 be present, readable, scoped correctly, and reference an
 upstream chunk/page/table id (via `source_artifact_ids`).
 * `enrich_required=False`: stage was skipped — no checks
 beyond "no orphan enriched artifacts" (defensive)."""
    checks: list[StageValidationCheck] = []
    enriched = [
        a for a in artifacts if a.kind.startswith("enriched.")
    ]
    if not enrich_required:
        # Permissive: the workflow's skip path records the SKIPPED
        # step with a reason; the validator just confirms nothing
        # snuck through.
        if enriched:
            checks.append(_warning(
                "enrich_skip_no_artifacts",
                f"enrich was not required but {len(enriched)} enriched "
                "artifact(s) present — likely a planner/policy mismatch",
            ))
        else:
            checks.append(_passed(
                "enrich_skipped",
                "enrich not required; no enriched artifacts present",
            ))
        return checks
    if not enriched:
        checks.append(_failed(
            "enrich_artifacts_present",
            "enrich was required but produced ZERO enriched.* artifacts",
        ))
        return checks
    checks.append(_passed("enrich_artifacts_present"))
    for a in enriched:
        checks.extend(_check_scope(
            a,
            expected_tenant=expected_tenant,
            expected_project=expected_project,
            expected_run_id=expected_run_id,
            expected_document_id=expected_document_id,
        ))
        _content, read_check = _check_readable(a, read_back)
        checks.append(read_check)
        if not a.source_artifact_ids:
            checks.append(_failed(
                "enrich_links_upstream",
                f"enriched artifact {a.artifact_id} has empty "
                "source_artifact_ids — orphaned from upstream chunks",
            ))
        else:
            checks.append(_passed("enrich_links_upstream"))
    return checks


# ---- Graph ----------------------------------------------------------


def validate_graph(
    *,
    artifacts: list[ArtifactRecord],
    expected_tenant: str,
    expected_project: str,
    expected_run_id: str,
    expected_document_id: str | None,
    graph_required: bool,
    chunk_artifact_ids: set[str],
    read_back: ReadBack,
) -> list[StageValidationCheck]:
    """Graph produces a single `graph_json` artifact carrying nodes
 + edges. Validation:

 * `graph_required=True`: graph_json artifact present, readable,
 non-empty; node count > 0; every edge references valid nodes;
 every node referencing chunks references one of the run's
 chunk_artifact_ids (i.e. graph is grounded in the run, not
 free-floating).
 * `graph_required=False`: skipped path — confirm no orphan
 graph artifacts."""
    checks: list[StageValidationCheck] = []
    graph_artifacts = [a for a in artifacts if a.kind == "graph_json"]
    if not graph_required:
        if graph_artifacts:
            checks.append(_warning(
                "graph_skip_no_artifacts",
                f"graph was not required but {len(graph_artifacts)} "
                "graph_json artifact(s) present",
            ))
        else:
            checks.append(_passed(
                "graph_skipped",
                "graph not required; no graph_json artifacts present",
            ))
        return checks
    if not graph_artifacts:
        checks.append(_failed(
            "graph_artifact_present",
            "graph was required but no graph_json artifact was produced",
        ))
        return checks
    checks.append(_passed("graph_artifact_present"))
    for a in graph_artifacts:
        checks.extend(_check_scope(
            a,
            expected_tenant=expected_tenant,
            expected_project=expected_project,
            expected_run_id=expected_run_id,
            expected_document_id=expected_document_id,
        ))
        content, read_check = _check_readable(a, read_back)
        checks.append(read_check)
        if content is None:
            continue
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            checks.append(_failed(
                "graph_artifact_parses",
                f"graph artifact {a.artifact_id} not valid JSON: {exc}",
            ))
            continue
        if not isinstance(payload, dict):
            checks.append(_failed(
                "graph_artifact_shape",
                f"graph artifact {a.artifact_id} top-level not an object",
            ))
            continue
        nodes = payload.get("nodes") or []
        edges = payload.get("edges") or []
        if not isinstance(nodes, list) or not isinstance(edges, list):
            checks.append(_failed(
                "graph_artifact_shape",
                f"graph artifact {a.artifact_id} nodes/edges not lists",
            ))
            continue
        if len(nodes) == 0:
            checks.append(_failed(
                "graph_node_count_positive",
                f"graph artifact {a.artifact_id} has zero nodes — "
                "graph stage cannot succeed without grounded entities",
            ))
            continue
        checks.append(_passed(
            "graph_node_count_positive",
            f"{len(nodes)} nodes, {len(edges)} edges",
        ))
        # Edge → node validity
        node_ids: set[str] = set()
        for n in nodes:
            if isinstance(n, dict):
                nid = n.get("id") or n.get("node_id") or n.get("nodeId")
                if nid is not None:
                    node_ids.add(str(nid))
        dangling_edges = 0
        for e in edges:
            if not isinstance(e, dict):
                dangling_edges += 1
                continue
            src = e.get("source") or e.get("from")
            dst = e.get("target") or e.get("to")
            if src is None or dst is None:
                dangling_edges += 1
                continue
            if str(src) not in node_ids or str(dst) not in node_ids:
                dangling_edges += 1
        if dangling_edges > 0:
            checks.append(_failed(
                "graph_edges_reference_nodes",
                f"{dangling_edges} edge(s) reference missing nodes",
            ))
        elif edges:
            checks.append(_passed("graph_edges_reference_nodes"))
        # Chunk grounding (best-effort): if the graph carries
        # `source_artifact_ids` it MUST point at this run's chunks.
        sources = a.source_artifact_ids or []
        if sources and chunk_artifact_ids:
            stranded = [
                s for s in sources if s not in chunk_artifact_ids
            ]
            if stranded:
                checks.append(_failed(
                    "graph_grounded_in_chunks",
                    f"{len(stranded)} graph source_artifact_id(s) "
                    "don't match any chunk artifact in this run",
                ))
            else:
                checks.append(_passed("graph_grounded_in_chunks"))
    return checks


# ---- Post-compile verification gate -----------------------------------
# Coarse health check the workflow runs after compile (and optionally
# after index) before declaring the run successful. Unlike the per-
# stage validators above — which read every artifact back from disk
# and check schema integrity — this is a fast, kind-only check on the
# artifact list the compile activity returned. Its job is to surface
# silent failures (compile reported "succeeded" but produced zero
# chunks, or the index activity failed to write a manifest) as
# structured failure codes the FE can render and operators can filter.


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
 validation. The full per-stage validators (`validate_compile`,
 `validate_chunks`, etc.) handle deep integrity checks; this gate
 catches the cheap cases that should never reach terminal SUCCEEDED.
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


__all__ = [
    "ReadBack",
    "validate_chunks",
    "validate_compile",
    "validate_enrich",
    "validate_graph",
    "verify_compile_output_health",
]
