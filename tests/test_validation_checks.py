"""Unit tests for the deterministic check engine.

Each check is a pure function over a `_CheckContext`; these tests
exercise each one in isolation so a future regression names the
exact check that broke. The integration-level "do they all compose
correctly" verification lives in `test_validation_service.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.validation.dtos import ValidationCitationDTO
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext
from j1.validation.checks import aggregate_status, run_checks
from j1.validation.dtos import RetrievedChunkRefDTO, ValidationCheckDTO


def _chunk(
    *,
    artifact_id: str = "art-1",
    chunk_id: str | None = "chunk-1",
    run_id: str | None = "run-1",
) -> RetrievedChunkRefDTO:
    return RetrievedChunkRefDTO(
        artifact_id=artifact_id,
        chunk_id=chunk_id,
        run_id=run_id,
        document_id="doc-1",
        source_location="p.1",
        score=0.5,
        preview="…",
    )


def _citation(
    *,
    artifact_id: str = "art-1",
    run_id: str | None = "run-1",
) -> ValidationCitationDTO:
    return ValidationCitationDTO(
        artifact_id=artifact_id,
        artifact_type="chunk",
        source_document_id="doc-1",
        source_location="p.1",
        chunk_id="chunk-1",
        run_id=run_id,
    )


def _stage(workspace, ctx, artifact_registry, *, artifact_id: str) -> None:
    """Register a minimal artifact so registry lookups succeed."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record = ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind="chunk",
        location=f"compiled/{artifact_id}.json",
        content_hash=f"sha256:{artifact_id}",
        byte_size=10,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        source_document_ids=[],
        source_artifact_ids=[],
        metadata={},
    )
    artifact_registry.add(record)


# ---- answer_non_empty ---------------------------------------------------


def test_answer_non_empty_passes_for_real_answer(
    workspace, ctx, artifact_registry,
):
    """Five non-whitespace characters is the floor — a one-line
 answer ('Hello') is enough to pass."""
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    checks = run_checks(
        ctx=ctx, run_id="run-1",
        answer="Hello", retrieved_chunks=[_chunk()],
        citations=[_citation()], citation_required=False,
        artifact_registry=artifact_registry,
    )
    answer_check = next(c for c in checks if c.name == "answer_non_empty")
    assert answer_check.passed is True


def test_answer_non_empty_fails_for_empty_or_whitespace(
    workspace, ctx, artifact_registry,
):
    """Empty / whitespace-only / sub-five-char answers fail. This is
 the first signal a tester sees when the LLM returned nothing."""
    for body in ["", "   ", "\n\n", "abc"]:
        checks = run_checks(
            ctx=ctx, run_id="run-1",
            answer=body, retrieved_chunks=[_chunk()],
            citations=[_citation()], citation_required=False,
            artifact_registry=artifact_registry,
        )
        c = next(c for c in checks if c.name == "answer_non_empty")
        assert c.passed is False, f"expected fail for body={body!r}"


# ---- retrieved_chunks_present ------------------------------------------


def test_retrieved_chunks_present_passes_with_one(
    workspace, ctx, artifact_registry,
):
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    checks = run_checks(
        ctx=ctx, run_id="run-1",
        answer="x" * 10, retrieved_chunks=[_chunk()],
        citations=[_citation()], citation_required=False,
        artifact_registry=artifact_registry,
    )
    c = next(x for x in checks if x.name == "retrieved_chunks_present")
    assert c.passed is True


def test_retrieved_chunks_present_fails_with_none(
    workspace, ctx, artifact_registry,
):
    """No retrieval = the index couldn't match the question. Critical
 fail — even the best answer is unsupported without chunks."""
    checks = run_checks(
        ctx=ctx, run_id="run-1",
        answer="x" * 10, retrieved_chunks=[],
        citations=[], citation_required=False,
        artifact_registry=artifact_registry,
    )
    c = next(x for x in checks if x.name == "retrieved_chunks_present")
    assert c.passed is False


# ---- citation_present (conditional) -------------------------------------


def test_citation_present_skipped_when_not_required(
    workspace, ctx, artifact_registry,
):
    """`citationRequired=false` → the check is OMITTED from the
 result list entirely (not present-but-passed). This keeps the
 aggregate honest: a check that didn't run can't 'fail'."""
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    checks = run_checks(
        ctx=ctx, run_id="run-1",
        answer="x" * 10, retrieved_chunks=[_chunk()],
        citations=[],  # no citations, but check should be skipped
        citation_required=False,
        artifact_registry=artifact_registry,
    )
    assert all(c.name != "citation_present" for c in checks)


def test_citation_present_passes_when_required_and_present(
    workspace, ctx, artifact_registry,
):
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    checks = run_checks(
        ctx=ctx, run_id="run-1",
        answer="x" * 10, retrieved_chunks=[_chunk()],
        citations=[_citation()],
        citation_required=True,
        artifact_registry=artifact_registry,
    )
    c = next(x for x in checks if x.name == "citation_present")
    assert c.passed is True


def test_citation_present_fails_when_required_and_absent(
    workspace, ctx, artifact_registry,
):
    """The most common 'broken answer' shape — model produced an
 answer but didn't ground it. Test forces the check by setting
 `citationRequired=true`."""
    checks = run_checks(
        ctx=ctx, run_id="run-1",
        answer="x" * 10, retrieved_chunks=[_chunk()],
        citations=[],
        citation_required=True,
        artifact_registry=artifact_registry,
    )
    c = next(x for x in checks if x.name == "citation_present")
    assert c.passed is False


# ---- retrieved_chunks_belong_to_run -------------------------------------


def test_retrieved_chunks_belong_to_run_passes_when_aligned(
    workspace, ctx, artifact_registry,
):
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    checks = run_checks(
        ctx=ctx, run_id="run-A",
        answer="x" * 10,
        retrieved_chunks=[_chunk(run_id="run-A"), _chunk(run_id="run-A")],
        citations=[_citation(run_id="run-A")], citation_required=False,
        artifact_registry=artifact_registry,
    )
    c = next(x for x in checks if x.name == "retrieved_chunks_belong_to_run")
    assert c.passed is True


def test_retrieved_chunks_belong_to_run_fails_on_leaked_run(
    workspace, ctx, artifact_registry,
):
    """Defense-in-depth: if even one retrieved chunk has the wrong
 `run_id` (server-derived from the FTS column), something has
 leaked past the scope filter. This must always be a hard fail."""
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    leaked = _chunk(artifact_id="art-leaked", chunk_id="c-x", run_id="run-OTHER")
    checks = run_checks(
        ctx=ctx, run_id="run-A",
        answer="x" * 10,
        retrieved_chunks=[_chunk(run_id="run-A"), leaked],
        citations=[_citation(run_id="run-A")], citation_required=False,
        artifact_registry=artifact_registry,
    )
    c = next(x for x in checks if x.name == "retrieved_chunks_belong_to_run")
    assert c.passed is False
    assert "run-OTHER" in (c.detail or "")


def test_retrieved_chunks_belong_to_run_skipped_when_no_chunks(
    workspace, ctx, artifact_registry,
):
    """Empty retrieval is now SKIPPED rather than fake-passing. The
 previous "vacuous pass" rendered as a green check in the
 Validation tab next to an empty chunks list — misleading. The
 skipped state surfaces "this check did not run because there
 was nothing to check" and is excluded from
 ``aggregate_status``."""
    checks = run_checks(
        ctx=ctx, run_id="run-A",
        answer="x" * 10, retrieved_chunks=[], citations=[],
        citation_required=False, artifact_registry=artifact_registry,
    )
    c = next(x for x in checks if x.name == "retrieved_chunks_belong_to_run")
    assert c.skipped is True
    assert c.passed is False
    assert c.skipped_reason == "no retrieved chunks to check"


# ---- citations_belong_to_run --------------------------------------------


def test_citations_belong_to_run_fails_on_null_run_id(
    workspace, ctx, artifact_registry,
):
    """A citation with `run_id is None` is a fail — every citation
 that survived the run-scoped FTS filter must carry the run id.
 Null means the indexer didn't tag it, which means it shouldn't
 have been retrieved in the first place."""
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    checks = run_checks(
        ctx=ctx, run_id="run-A",
        answer="x" * 10, retrieved_chunks=[_chunk(run_id="run-A")],
        citations=[_citation(run_id=None)],
        citation_required=False,
        artifact_registry=artifact_registry,
    )
    c = next(x for x in checks if x.name == "citations_belong_to_run")
    assert c.passed is False


# ---- no_cross_tenant_or_cross_project_leak -----------------------------


def test_no_cross_tenant_leak_fails_on_unresolvable_artifact(
    workspace, ctx, artifact_registry,
):
    """If a citation's artifact_id can't be loaded under the
 caller's project, treat it as a leak — even if the run_id
 happens to match. This is the belt for the run-scope braces."""
    citation_to_unknown = _citation(artifact_id="not-in-this-project")
    checks = run_checks(
        ctx=ctx, run_id="run-A",
        answer="x" * 10, retrieved_chunks=[_chunk()],
        citations=[citation_to_unknown],
        citation_required=False,
        artifact_registry=artifact_registry,
    )
    c = next(
        x for x in checks
        if x.name == "no_cross_tenant_or_cross_project_leak"
    )
    assert c.passed is False
    assert "not-in-this-project" in str(c.detail)


def test_no_cross_tenant_leak_passes_when_artifacts_resolve(
    workspace, ctx, artifact_registry,
):
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    checks = run_checks(
        ctx=ctx, run_id="run-A",
        answer="x" * 10, retrieved_chunks=[_chunk()],
        citations=[_citation(artifact_id="art-1")],
        citation_required=False,
        artifact_registry=artifact_registry,
    )
    c = next(
        x for x in checks
        if x.name == "no_cross_tenant_or_cross_project_leak"
    )
    assert c.passed is True


def test_no_cross_tenant_leak_passes_when_no_citations(
    workspace, ctx, artifact_registry,
):
    """No citations means nothing to check — the check passes
 vacuously. The empty-citations case is interesting only when
 `citationRequired=true`, which is the citation_present check."""
    checks = run_checks(
        ctx=ctx, run_id="run-A",
        answer="x" * 10, retrieved_chunks=[],
        citations=[], citation_required=False,
        artifact_registry=artifact_registry,
    )
    c = next(
        x for x in checks
        if x.name == "no_cross_tenant_or_cross_project_leak"
    )
    assert c.passed is True


# ---- aggregate_status ---------------------------------------------------


def test_aggregate_status_passed_when_all_required_pass():
    checks = [
        ValidationCheckDTO(name="a", severity="required", passed=True),
        ValidationCheckDTO(name="b", severity="required", passed=True),
    ]
    assert aggregate_status(checks) == "passed"


def test_aggregate_status_failed_on_any_required_fail():
    checks = [
        ValidationCheckDTO(name="a", severity="required", passed=True),
        ValidationCheckDTO(name="b", severity="required", passed=False),
    ]
    assert aggregate_status(checks) == "failed"


def test_aggregate_status_passed_with_warnings_on_optional_fail():
    """Forward-compat: when ships optional judge checks, an
 optional failure should downgrade to warnings, not fail. Locked
 here so the aggregation rule survives 's required-only
 suite."""
    checks = [
        ValidationCheckDTO(name="a", severity="required", passed=True),
        ValidationCheckDTO(name="b", severity="optional", passed=False),
    ]
    assert aggregate_status(checks) == "passed_with_warnings"


def test_aggregate_status_required_fail_dominates_optional():
    """If both fail, required wins — the badge is `failed`, not
 `passed_with_warnings`."""
    checks = [
        ValidationCheckDTO(name="a", severity="required", passed=False),
        ValidationCheckDTO(name="b", severity="optional", passed=False),
    ]
    assert aggregate_status(checks) == "failed"
