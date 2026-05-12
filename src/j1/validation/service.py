"""IngestionValidationService — read/write surface for validation.

synchronous manual test query (`run_manual_test_query`).
generate / list / get validation sets, run validation,
list / get validation runs.

All methods enforce run ownership via `_load_run` (raises
`ReviewNotFound` → REST 404 on cross-tenant / cross-project access).

The service is constructed from already-built dependencies (no
container / no facade) so tests wire it from `tmp_path` fixtures
the same way `IngestionResultReviewService` is wired.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from j1.artifacts.registry import ArtifactRegistry
from j1.audit.recorder import AuditRecorder
from j1.connectors.graph.config import ARTIFACT_KIND_GRAPH_JSON
from j1.ingestion_review.exceptions import ReviewNotFound
from j1.ingestion_review.projectors.chunks import ChunkProjector, _ChunkRecord
from j1.processing.results import ARTIFACT_KIND_CHUNK
from j1.projects.context import ProjectContext
from j1.query.engine import HybridQueryEngine
from j1.query.models import QueryMode, QueryRequest
from j1.query.scope import RunScope
from j1.runs.models import IngestionRun
from j1.runs.store import IngestionRunStore
from j1.validation.checks import aggregate_status, run_checks
from j1.validation.dtos import (
    EvidenceBlockDTO,
    LLMTraceDTO,
    ManualTestQueryRequest,
    ManualTestQueryResponseDTO,
    RetrievedChunkRefDTO,
    ValidationCheckDTO,
    ValidationCitationDTO,
    ValidationResultDTO,
    ValidationRunDTO,
    ValidationSetDTO,
)
from j1.validation.evidence import build_evidence_blocks
from j1.validation.generator import (
    DefaultTestCaseGenerator,
    GenerationOptions,
)
from j1.validation.judge import LLMJudge
from j1.validation.synthesis import AnswerSynthesizer
from j1.validation.runner import (
    DefaultValidationRunner,
    MAX_CASES_PER_RUN,
)
from j1.validation.store import ValidationRunStore, ValidationSetStore
from j1.workspace.layout import WorkspaceArea
from j1.workspace.resolver import WorkspaceResolver

_log = logging.getLogger("j1.validation")

_ACTION_MANUAL_QUERY = "j1.validation.manual_query.completed"
_ACTION_SET_GENERATED = "j1.validation.set_generated"
_ACTION_RUN_COMPLETED = "j1.validation.run_completed"
_ACTION_VERDICT_RECORDED = "j1.validation.verdict_recorded"
_TARGET_KIND_RUN = "ingestion_run"
_TARGET_KIND_VALIDATION_SET = "validation_set"
_TARGET_KIND_VALIDATION_RUN = "validation_run"
_TARGET_KIND_VALIDATION_RESULT = "validation_result"

# Allowed tester verdict values. Keeping this constant local to the
# service makes the validation tighter than just trusting the DTO's
# Literal type — the REST layer can re-use it for input validation.
_VALID_VERDICTS: frozenset[str] = frozenset({"pass", "warning", "fail"})

# Hard cap on `top_k` — 's manual query is synchronous and we
# don't want a tester accidentally requesting 10k results and blocking
# the worker. The REST layer also clamps via Pydantic but the service
# enforces too so stand-alone callers (tests, future async paths) get
# the same guarantee.
_TOP_K_HARD_CAP = 50

# Preview length for retrieved-chunk excerpts on the response. Mirrors
# the chunk projector's value so the UI layer renders consistent
# preview lengths across the Validation tab and the Chunks tab.
_PREVIEW_MAX_CHARS = 240


class IngestionValidationService:
    """Validation surface — manual queries + generated
 sets and runs.

 Verdicts / human overrides / async execution arrive in later
 phases; the constructor accepts the relevant dependencies as
 Optional so a only deployment can still wire just the
 manual-query path.
 """

    def __init__(
        self,
        *,
        run_store: IngestionRunStore,
        artifact_registry: ArtifactRegistry,
        query_engine: HybridQueryEngine,
        audit: AuditRecorder | None = None,
        workspace: WorkspaceResolver | None = None,
        validation_set_store: ValidationSetStore | None = None,
        validation_run_store: ValidationRunStore | None = None,
        test_case_generator: DefaultTestCaseGenerator | None = None,
        judge: LLMJudge | None = None,
        answer_synthesizer: AnswerSynthesizer | None = None,
    ) -> None:
        self._run_store = run_store
        self._artifacts = artifact_registry
        self._query_engine = query_engine
        self._audit = audit
        self._workspace = workspace
        self._set_store = validation_set_store
        self._run_store_v = validation_run_store
        self._generator = test_case_generator
        # Optional LLM judge for semantic checks. The runner
        # picks this up when it's configured; when None, optional
        # checks are simply omitted.
        self._judge = judge
        # Optional LLM answer synthesizer for the manual-query path.
        # When None, manual queries fall back to retrieval-only mode
        # and the response reports `llm.called=False`. Batch validation
        # runs do not consult this — they must stay deterministic.
        self._synthesizer = answer_synthesizer

    def run_manual_test_query(
        self,
        ctx: ProjectContext,
        run_id: str,
        request: ManualTestQueryRequest,
        *,
        actor: str = "system",
    ) -> ManualTestQueryResponseDTO:
        """Execute a single tester question against this run.

 synchronous. Calls `HybridQueryEngine.query` with
 `RunScope(run_id)` so retrieval is restricted to artifacts
 produced by this run. Builds deterministic check results
 from the engine output.

 Raises `ReviewNotFound` (→ 404 at REST) when the run doesn't
 exist in `(ctx.tenant_id, ctx.project_id)`. Cross-tenant /
 cross-project access produces an identical 404 — existence
 is never leakable.
 """
        run = self._load_run(ctx, run_id)
        # Reserve the request id up-front so the value the FE sees in
        # the response also lands in the audit log on the same row.
        request_id = f"tq-{uuid.uuid4().hex[:12]}"

        top_k = max(1, min(request.top_k, _TOP_K_HARD_CAP))
        mode = _coerce_mode(request.mode)

        query_request = QueryRequest(
            question=request.question,
            mode=mode,
            max_results=top_k,
            scope=RunScope(run_id=run.run_id),
        )

        try:
            response = self._query_engine.query(ctx, query_request)
        except Exception as exc:  # noqa: BLE001
            # Engine failures must not 500 — surface them as a structured
            # `inconclusive` response so the FE can render an actionable
            # message instead of a transport error.
            _log.warning(
                "validation manual query engine failure run_id=%s: %s",
                run.run_id, exc,
            )
            return _inconclusive_response(
                request_id=request_id,
                run_id=run.run_id,
                question=request.question,
                error=str(exc),
            )

        retrieved = _retrieved_chunks_from_response(response)
        citations = _citations_from_response(response)

        checks = run_checks(
            ctx=ctx,
            run_id=run.run_id,
            answer=response.answer,
            retrieved_chunks=retrieved,
            citations=citations,
            citation_required=request.citation_required,
            artifact_registry=self._artifacts,
        )
        validation_status = aggregate_status(checks)

        evidence_flags = {
            "graphUsed": bool(response.graph_paths),
            "tablesUsed": _has_artifact_kind(retrieved, "enriched.tables"),
            "imagesUsed": _has_artifact_kind(retrieved, "enriched.visuals"),
        }

        raw_response = (
            _engine_response_to_raw(response)
            if request.include_raw
            else None
        )

        evidence_blocks = self._build_evidence_blocks_for_run(
            ctx=ctx,
            request=request,
            retrieved=retrieved,
        )
        synthesized_answer, llm_trace = self._maybe_synthesize_answer(
            request=request,
            evidence=evidence_blocks,
        )

        self._audit_manual_query(
            ctx=ctx,
            run=run,
            request_id=request_id,
            request=request,
            validation_status=validation_status,
            retrieved_count=len(retrieved),
            citation_count=len(citations),
            actor=actor,
        )

        return ManualTestQueryResponseDTO(
            request_id=request_id,
            run_id=run.run_id,
            question=request.question,
            answer=response.answer,
            mode_used=response.mode_used,
            retrieved_chunks=retrieved,
            citations=[_citation_to_dict(c) for c in citations],
            checks=checks,
            validation_status=validation_status,
            evidence_flags=evidence_flags,
            raw_response=raw_response,
            synthesized_answer=synthesized_answer,
            llm=llm_trace,
            evidence_sent_to_llm=evidence_blocks,
        )

    def _build_evidence_blocks_for_run(
        self,
        *,
        ctx: ProjectContext,
        request: ManualTestQueryRequest,
        retrieved: list[RetrievedChunkRefDTO],
    ) -> list[EvidenceBlockDTO]:
        """Materialise the clean evidence blocks the synthesizer will
 actually see. Returns `[]` when synthesis is opted out (saves
 the file IO) or when the workspace isn't wired (legacy paths).
 The same list is echoed back on the response so the FE can
 render "Evidence Sent to LLM"."""
        if not request.synthesize or self._synthesizer is None:
            return []
        if self._workspace is None or not retrieved:
            return []

        def _resolver(record):
            from pathlib import Path, PurePosixPath
            location = record.location
            parts = PurePosixPath(location).parts
            if len(parts) < 2:
                return Path(location)
            area_name, *rest = parts
            area = WorkspaceArea(area_name)
            return self._workspace.area(ctx, area).joinpath(*rest)  # type: ignore[union-attr]

        return build_evidence_blocks(
            ctx=ctx,
            retrieved=retrieved,
            artifact_registry=self._artifacts,
            path_resolver=_resolver,
        )

    def _maybe_synthesize_answer(
        self,
        *,
        request: ManualTestQueryRequest,
        evidence: list[EvidenceBlockDTO],
    ) -> tuple[str | None, LLMTraceDTO]:
        """Run the LLM synthesizer when opted in AND wired.

 Three branches on the LLMTraceDTO:
   * `called=False, error=None`  — opt-out via request.synthesize=False
   * `called=False, error="no LLM client configured"` — deployment
     didn't pass `answer_synthesizer`. The FE shows an actionable
     message instead of silently dropping to retrieval-only.
   * `called=True`  — synthesis attempted; `answer` and `error`
     reflect outcome (success / no-evidence / client failure).
 """
        if not request.synthesize:
            return None, LLMTraceDTO(called=False)

        if self._synthesizer is None:
            return None, LLMTraceDTO(
                called=False,
                error="no LLM client configured",
            )

        result = self._synthesizer.synthesize(
            question=request.question,
            evidence=evidence,
        )
        return result.answer, LLMTraceDTO(
            called=True,
            provider=result.provider,
            model=result.model,
            latency_ms=result.latency_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            error=result.error,
        )

    # ---- validation sets ----------------------------------------

    def generate_validation_set(
        self,
        ctx: ProjectContext,
        run_id: str,
        *,
        max_cases: int = 25,
        citation_required: bool = False,
        force: bool = False,
        actor: str = "system",
    ) -> ValidationSetDTO:
        """Generate a fresh validation set from this run's chunks.

 Idempotent on `(run_id, generator_version, artifacts_hash)`:
 when an existing set in the store has a matching hash and
 `force=False`, the existing record is returned unchanged.
 Set `force=True` to bypass the cache (e.g. after editing the
 prompt or chunk content).

 Raises `ReviewNotFound` if the run isn't visible in the
 caller's `(tenant, project)`.
 Raises `RuntimeError` when dependencies aren't wired
 (set store / generator) — same shape as 's missing-
 deps degradation.
 """
        if self._set_store is None or self._generator is None or self._workspace is None:
            raise RuntimeError(
                "validation set generation not configured "
                "(pass validation_set_store, test_case_generator, "
                "workspace to IngestionValidationService)"
            )
        max_cases = max(1, min(max_cases, MAX_CASES_PER_RUN))

        run = self._load_run(ctx, run_id)
        chunks = self._project_run_chunks(ctx, run)
        # gather modality artifacts the generator can
        # author cases against. Single registry scan; the partition
        # below is O(n) over the run's artifact list.
        tables, visuals, graphs = self._modality_artifacts_for_run(
            ctx, run.run_id,
        )

        # Generate first so we can compute the artifacts hash off
        # the sampled chunks. Cheap — no LLM call yet on the empty
        # path; the real LLM cost is per chunk inside generate.
        vset = self._generator.generate(
            run_id=run.run_id,
            document_ids=_document_ids(run),
            chunks=chunks,
            options=GenerationOptions(
                max_cases=max_cases,
                citation_required=citation_required,
            ),
            actor=actor,
            table_artifacts=tables,
            visual_artifacts=visuals,
            graph_artifacts=graphs,
        )

        # Idempotency: scan existing sets for a hash match. Force
        # bypasses the cache.
        if not force:
            existing = self._find_existing_set(ctx, run.run_id, vset.artifacts_content_hash)
            if existing is not None:
                _log.debug(
                    "reusing existing validation set %s (hash match)",
                    existing.validation_set_id,
                )
                return existing

        self._set_store.upsert(ctx, vset)
        self._audit_set_generated(ctx, run, vset, actor)
        return vset

    def list_validation_sets(
        self, ctx: ProjectContext, run_id: str,
    ) -> list[ValidationSetDTO]:
        """List sets for a run, most-recent-first. Empty list when
 the run exists but no sets have been generated."""
        if self._set_store is None:
            return []
        # Run-ownership check first — cross-tenant access raises 404
        # rather than returning an empty list (which would leak
        # existence: missing run vs. no-sets-yet should not be
        # distinguishable).
        self._load_run(ctx, run_id)
        return self._set_store.list_for_run(ctx, run_id)

    def get_validation_set(
        self,
        ctx: ProjectContext,
        run_id: str,
        validation_set_id: str,
    ) -> ValidationSetDTO:
        """Fetch one set by id. Raises `ReviewNotFound` for missing /
 cross-tenant / set-belongs-to-different-run."""
        if self._set_store is None:
            raise ReviewNotFound(
                f"validation set {validation_set_id!r} not found"
            )
        self._load_run(ctx, run_id)
        vset = self._set_store.get(ctx, validation_set_id)
        if vset is None or vset.run_id != run_id:
            # Identical message regardless of cause — existence is
            # not probeable across runs.
            raise ReviewNotFound(
                f"validation set {validation_set_id!r} not found"
            )
        return vset

    # ---- validation runs ----------------------------------------

    def run_validation(
        self,
        ctx: ProjectContext,
        run_id: str,
        validation_set_id: str,
        *,
        actor: str = "system",
    ) -> ValidationRunDTO:
        """Execute a validation set. Synchronous — blocks
 until every case has run. Persists three lifecycle snapshots
 (pending → running → terminal) via the run store.

 Raises `ReviewNotFound` for unknown / cross-tenant set or run.
 Raises `RuntimeError` when the dependencies aren't
 wired."""
        if self._run_store_v is None:
            raise RuntimeError(
                "validation run execution not configured "
                "(pass validation_run_store to IngestionValidationService)"
            )
        # Both ownership gates first — `_load_run` then a set-scope
        # check. Cross-tenant probing for a known set under a wrong
        # project must still 404.
        vset = self.get_validation_set(ctx, run_id, validation_set_id)

        runner = DefaultValidationRunner(
            query_engine=self._query_engine,
            artifact_registry=self._artifacts,
            lifecycle_callback=lambda v: self._run_store_v.upsert(ctx, v),  # type: ignore[union-attr]
            judge=self._judge,
        )
        vrun = runner.run(ctx, vset, actor=actor)
        self._audit_run_completed(ctx, run_id, vrun, actor)
        return vrun

    def list_validation_runs(
        self, ctx: ProjectContext, run_id: str,
    ) -> list[ValidationRunDTO]:
        if self._run_store_v is None:
            return []
        self._load_run(ctx, run_id)
        return self._run_store_v.list_for_run(ctx, run_id)

    def get_validation_run(
        self,
        ctx: ProjectContext,
        run_id: str,
        validation_run_id: str,
    ) -> ValidationRunDTO:
        if self._run_store_v is None:
            raise ReviewNotFound(
                f"validation run {validation_run_id!r} not found"
            )
        self._load_run(ctx, run_id)
        vrun = self._run_store_v.get(ctx, validation_run_id)
        if vrun is None or vrun.run_id != run_id:
            raise ReviewNotFound(
                f"validation run {validation_run_id!r} not found"
            )
        return vrun

    # ---- tester verdict ---------------------------------------

    def record_tester_verdict(
        self,
        ctx: ProjectContext,
        run_id: str,
        validation_run_id: str,
        result_id: str,
        *,
        verdict: str,
        notes: str | None = None,
        actor: str = "system",
    ) -> ValidationRunDTO:
        """Record a human override on a single validation result.

 Tester verdict is INDEPENDENT of the automated
 `validation_status` — the deterministic checks stay
 reproducible, and the human verdict layers on top. The FE
 renders both side-by-side; downstream tooling can treat
 whichever it prefers as authoritative.

 Persists by upserting the parent `ValidationRunDTO` with
 the verdict-augmented result swapped in. JSONL latest-wins
 means subsequent reads see the updated record.

 Raises `ReviewNotFound` for missing run / cross-tenant /
 cross-run / unknown result. Raises `ValueError` for an
 invalid verdict string (REST layer translates to 422 via
 Pydantic; this guards stand-alone callers).
 """
        if self._run_store_v is None:
            raise ReviewNotFound(
                f"validation run {validation_run_id!r} not found"
            )
        if verdict not in _VALID_VERDICTS:
            raise ValueError(
                f"invalid tester verdict {verdict!r}; expected one of "
                f"{sorted(_VALID_VERDICTS)}"
            )
        # Run-ownership gates: load_run for tenant/project, then the
        # vrun-belongs-to-this-run check via get_validation_run.
        vrun = self.get_validation_run(ctx, run_id, validation_run_id)

        # Find + replace the result. List comprehension over results
        # keeps the rest of the run snapshot untouched — only the
        # verdict + notes change.
        new_results: list[ValidationResultDTO] = []
        found = False
        for r in vrun.results:
            if r.result_id == result_id:
                found = True
                new_results.append(
                    _replace_verdict(r, verdict=verdict, notes=notes),
                )
            else:
                new_results.append(r)
        if not found:
            raise ReviewNotFound(
                f"validation result {result_id!r} not found in run "
                f"{validation_run_id!r}"
            )

        updated = _replace_run_results(vrun, results=new_results)
        self._run_store_v.upsert(ctx, updated)
        self._audit_verdict_recorded(
            ctx=ctx,
            run_id=run_id,
            vrun=updated,
            result_id=result_id,
            verdict=verdict,
            actor=actor,
        )
        return updated

    # ---- export validation report -----------------------------

    def export_validation_run_report(
        self,
        ctx: ProjectContext,
        run_id: str,
        validation_run_id: str,
        *,
        format: str = "markdown",
    ) -> tuple[str, str]:
        """Compose a tester-friendly report from a terminal
 validation run.

 Returns `(content, media_type)` so the REST layer can set
 the right `Content-Type` header without re-deriving from
 the format. Two formats ship in v1:

 * `markdown` — narrative summary + per-case section. The
 default — copy-pastes cleanly into PR descriptions,
 release notes, etc.
 * `json` — projection of the same data; downstream
 automation should prefer the typed REST endpoints
 (`GET /validation-runs/{id}`) but JSON-export is here
 for parity with markdown.
 """
        vrun = self.get_validation_run(ctx, run_id, validation_run_id)
        fmt = (format or "markdown").lower()
        if fmt == "markdown" or fmt == "md":
            return _render_markdown_report(vrun), "text/markdown"
        if fmt == "json":
            import json
            from j1._serialization import to_jsonable
            return (
                json.dumps(to_jsonable(vrun), indent=2),
                "application/json",
            )
        raise ValueError(
            f"unsupported report format {format!r}; expected 'markdown' or 'json'"
        )

    def purge_for_run(
        self, ctx: ProjectContext, run_id: str,
    ) -> dict[str, int]:
        """Cascade-delete every validation set + run that references
 `run_id`. Used by the hard-delete (purge) orchestration in
 the REST layer so a purged ingestion run doesn't leave
 dangling validation history pointing at a missing run.

 Best-effort across both stores — a failure on one doesn't
 abort the other. Returns a count report:
 `{sets_removed: int, runs_removed: int}`."""
        sets_removed = 0
        runs_removed = 0
        if self._set_store is not None:
            purge = getattr(self._set_store, "purge_for_run", None)
            if callable(purge):
                try:
                    sets_removed = int(purge(ctx, run_id) or 0)
                except Exception:  # noqa: BLE001 — best-effort cascade
                    sets_removed = 0
        if self._run_store_v is not None:
            purge = getattr(self._run_store_v, "purge_for_run", None)
            if callable(purge):
                try:
                    runs_removed = int(purge(ctx, run_id) or 0)
                except Exception:  # noqa: BLE001 — best-effort cascade
                    runs_removed = 0
        return {
            "sets_removed": sets_removed,
            "runs_removed": runs_removed,
        }

    # ---- helpers (private) -------------------------------------

    def _project_run_chunks(
        self, ctx: ProjectContext, run: IngestionRun,
    ) -> list[_ChunkRecord]:
        """Use the existing `ChunkProjector` to flatten the run's
 chunk artifacts into a list of `_ChunkRecord`. Reuses the
 same `path_resolver` pattern the review service uses so
 the two surfaces see identical chunk text."""
        if self._workspace is None:
            return []
        # Resolve only chunk-kind artifacts that belong to this run.
        # + artifact tagging means we read directly from the
        # registry by run_id; 's lineage fallback is preserved
        # in `_resolve_run_artifacts` (we don't need that here yet).
        artifacts = [
            a for a in self._artifacts.list_artifacts(ctx)
            if a.kind == ARTIFACT_KIND_CHUNK and a.metadata.get("run_id") == run.run_id
        ]
        # Closure binds `ctx` so the projector can resolve paths
        # without knowing about the workspace.
        def _resolver(record):
            from pathlib import PurePosixPath
            location = record.location
            parts = PurePosixPath(location).parts
            if len(parts) < 2:
                from pathlib import Path
                return Path(location)
            area_name, *rest = parts
            area = WorkspaceArea(area_name)
            return self._workspace.area(ctx, area).joinpath(*rest)  # type: ignore[union-attr]

        projector = ChunkProjector(path_resolver=_resolver)
        return projector.project_records(artifacts)

    def _modality_artifacts_for_run(
        self, ctx: ProjectContext, run_id: str,
    ) -> tuple[list, list, list]:
        """Partition the run's artifacts into the three modality
 buckets (tables / visuals / graph). One pass over the
 registry; returns the three lists in fixed order so the
 generator's call site stays unambiguous.

 keeps the kind taxonomy in lockstep with
 `j1.ingestion_review.availability` — table/image/graph
 gating uses identical kind strings everywhere.
 """
        tables: list = []
        visuals: list = []
        graphs: list = []
        for record in self._artifacts.list_artifacts(ctx):
            if record.metadata.get("run_id") != run_id:
                continue
            if record.kind == "enriched.tables":
                tables.append(record)
            elif record.kind == "enriched.visuals":
                visuals.append(record)
            elif record.kind == ARTIFACT_KIND_GRAPH_JSON:
                graphs.append(record)
        return tables, visuals, graphs

    def _find_existing_set(
        self,
        ctx: ProjectContext,
        run_id: str,
        artifacts_content_hash: str | None,
    ) -> ValidationSetDTO | None:
        """Idempotency lookup — returns the most-recent set whose
 `artifacts_content_hash` matches. None when no match (caller
 proceeds to upsert the freshly generated set)."""
        if self._set_store is None or not artifacts_content_hash:
            return None
        for existing in self._set_store.list_for_run(ctx, run_id):
            if existing.artifacts_content_hash == artifacts_content_hash:
                return existing
        return None

    def _audit_set_generated(
        self,
        ctx: ProjectContext,
        run: IngestionRun,
        vset: ValidationSetDTO,
        actor: str,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(
                ctx,
                actor=actor,
                action=_ACTION_SET_GENERATED,
                target_kind=_TARGET_KIND_VALIDATION_SET,
                target_id=vset.validation_set_id,
                correlation_id=run.run_id,
                payload={
                    "validationSetId": vset.validation_set_id,
                    "runId": run.run_id,
                    "caseCount": len(vset.test_cases),
                    "source": vset.source,
                    "generatorVersion": vset.generator_version,
                },
            )
        except Exception:  # noqa: BLE001
            _log.warning("audit write failed for set generation", exc_info=True)

    def _audit_verdict_recorded(
        self,
        *,
        ctx: ProjectContext,
        run_id: str,
        vrun: ValidationRunDTO,
        result_id: str,
        verdict: str,
        actor: str,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(
                ctx,
                actor=actor,
                action=_ACTION_VERDICT_RECORDED,
                target_kind=_TARGET_KIND_VALIDATION_RESULT,
                target_id=result_id,
                correlation_id=run_id,
                payload={
                    "validationRunId": vrun.validation_run_id,
                    "validationSetId": vrun.validation_set_id,
                    "runId": run_id,
                    "resultId": result_id,
                    "verdict": verdict,
                },
            )
        except Exception:  # noqa: BLE001
            _log.warning("audit write failed for verdict recording", exc_info=True)

    def _audit_run_completed(
        self,
        ctx: ProjectContext,
        run_id: str,
        vrun: ValidationRunDTO,
        actor: str,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(
                ctx,
                actor=actor,
                action=_ACTION_RUN_COMPLETED,
                target_kind=_TARGET_KIND_VALIDATION_RUN,
                target_id=vrun.validation_run_id,
                correlation_id=run_id,
                payload={
                    "validationRunId": vrun.validation_run_id,
                    "validationSetId": vrun.validation_set_id,
                    "runId": run_id,
                    "executionStatus": vrun.execution_status,
                    "validationStatus": vrun.validation_status,
                    "total": vrun.summary.total,
                    "passed": vrun.summary.passed,
                    "failed": vrun.summary.failed,
                },
            )
        except Exception:  # noqa: BLE001
            _log.warning("audit write failed for run completion", exc_info=True)

    # ---- Internals -----------------------------------------------------

    def _load_run(self, ctx: ProjectContext, run_id: str) -> IngestionRun:
        """Run-ownership gate.

 Same shape and behaviour as `IngestionResultReviewService._load_run`:
 identical message on missing-vs-cross-tenant so existence is
 not probeable. Returning the typed `ReviewNotFound` lets the
 REST layer share the existing exception handler.
 """
        run = self._run_store.get(ctx, run_id)
        if run is None:
            raise ReviewNotFound(f"ingestion run {run_id!r} not found")
        return run

    def _audit_manual_query(
        self,
        *,
        ctx: ProjectContext,
        run: IngestionRun,
        request_id: str,
        request: ManualTestQueryRequest,
        validation_status: str,
        retrieved_count: int,
        citation_count: int,
        actor: str,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(
                ctx,
                actor=actor,
                action=_ACTION_MANUAL_QUERY,
                target_kind=_TARGET_KIND_RUN,
                target_id=run.run_id,
                correlation_id=run.run_id,
                payload={
                    "requestId": request_id,
                    "question": request.question,
                    "mode": request.mode,
                    "topK": request.top_k,
                    "citationRequired": request.citation_required,
                    "validationStatus": validation_status,
                    "retrievedCount": retrieved_count,
                    "citationCount": citation_count,
                },
            )
        except Exception:  # noqa: BLE001
            # Telemetry never fails the user-facing call.
            _log.warning("audit write failed for manual test query", exc_info=True)


# ---- Module-level helpers (easy to unit-test) --------------------------


def _document_ids(run: IngestionRun) -> list[str]:
    """Best-effort recovery of the run's target documents.

 Mirrors the helper in `j1.ingestion_review.service` so the
 validation set carries the same document_ids list the rest of
 the review surface surfaces. Inlined rather than imported to
 avoid coupling validation to review's private internals.
 """
    raw = run.metadata.get("target_document_ids")
    if isinstance(raw, list) and raw:
        seen: list[str] = []
        for entry in raw:
            text = str(entry)
            if text and text not in seen:
                seen.append(text)
        if run.document_id and run.document_id not in seen:
            seen.append(run.document_id)
        return seen
    return [run.document_id] if run.document_id else []


def _coerce_mode(raw: str) -> QueryMode:
    """Tolerantly map a request-supplied mode string to a `QueryMode`.

 Unknown values fall back to AUTO so a tester typo can't turn into
 a 500. The REST layer additionally validates upstream, but the
 service is the source of truth for the final dispatch.
 """
    try:
        return QueryMode(raw)
    except ValueError:
        return QueryMode.AUTO


def _retrieved_chunks_from_response(response: Any) -> list[RetrievedChunkRefDTO]:
    """Translate `QueryResponse.sources` into the public chunk-ref DTO.

 `score` is not surfaced on the engine's `SourceReference` today,
 so we default to 0.0 — the FE renders this as a neutral indicator.
 Hooking up real BM25 scores requires plumbing them through
 `KnowledgeQueryProvider`, which is a + concern.

 `artifact_kind` comes from the engine source's
 `artifact_type` (the indexer's column name for it). Used by
 evidence-flag detection + the modality-aware checks.
 """
    out: list[RetrievedChunkRefDTO] = []
    for source in getattr(response, "sources", []):
        title = str(getattr(source, "title", "") or "")
        out.append(
            RetrievedChunkRefDTO(
                artifact_id=source.artifact_id,
                chunk_id=getattr(source, "chunk_id", None),
                run_id=getattr(source, "run_id", None),
                document_id=getattr(source, "source_document_id", None),
                source_location=getattr(source, "source_location", None),
                score=0.0,
                preview=title[:_PREVIEW_MAX_CHARS],
                artifact_kind=getattr(source, "artifact_type", None),
            )
        )
    return out


def _citations_from_response(response: Any) -> list[ValidationCitationDTO]:
    """Project the engine's `SourceReference` list into the local
 validation citation DTO.

 's REST endpoint emits the same list as both
 `retrievedChunks[]` and `citations[]` because the underlying
 `HybridQueryEngine` doesn't yet distinguish "the chunks that
 matched" from "the chunks the answer cites." Splitting the two
 is a + concern (LLM-judge attribution).
 """
    out: list[ValidationCitationDTO] = []
    for source in getattr(response, "sources", []):
        out.append(
            ValidationCitationDTO(
                artifact_id=source.artifact_id,
                artifact_type=source.artifact_type,
                source_document_id=getattr(source, "source_document_id", None),
                source_location=getattr(source, "source_location", None),
                chunk_id=getattr(source, "chunk_id", None),
                run_id=getattr(source, "run_id", None),
            )
        )
    return out


def _citation_to_dict(citation: ValidationCitationDTO) -> dict[str, Any]:
    """REST schema-friendly camelCase dict.

 The validation service produces dataclasses; the REST adapter
 converts them to the response Pydantic models. Going through a
 plain dict here keeps the REST layer the only place that needs
 to know about CamelModel.
 """
    return {
        "artifactId": citation.artifact_id,
        "artifactType": citation.artifact_type,
        "sourceDocumentId": citation.source_document_id,
        "sourceLocation": citation.source_location,
        "chunkId": citation.chunk_id,
        "runId": citation.run_id,
    }


def _has_artifact_kind(
    retrieved: list[RetrievedChunkRefDTO], kind_prefix: str,
) -> bool:
    """Return True when any retrieved item's artifact_kind starts
 with the given prefix.

 honest signal. Reads the `artifact_kind` field
 surfaced by `_retrieved_chunks_from_response`. For
 runs predating that field — `artifact_kind` arrives as None —
 the function returns False, which matches the earlier
 "we don't know" stub behaviour.
 """
    for chunk in retrieved:
        kind = chunk.artifact_kind or ""
        if kind.startswith(kind_prefix):
            return True
    return False


def _engine_response_to_raw(response: Any) -> dict[str, Any]:
    """Project the engine response into a JSON-friendly dict.

 Callers asking for `?includeRaw=true` get the full server-side
 view of the engine result for debugging — citations, related
 artifacts, graph paths, warnings, mode used. The dict is shallow
 on purpose; deep introspection of vendor objects is out of
 scope.
 """
    return {
        "answer": response.answer,
        "modeUsed": response.mode_used,
        "confidence": response.confidence,
        "reviewRequired": response.review_required,
        "warnings": list(response.warnings),
        "warningCategories": [c.value for c in response.warning_categories],
        "relatedArtifacts": list(response.related_artifacts),
        "graphPaths": [
            {
                "nodes": list(p.nodes),
                "edges": list(p.edges),
                "description": p.description,
            }
            for p in response.graph_paths
        ],
        "sources": [
            {
                "artifactId": s.artifact_id,
                "artifactType": s.artifact_type,
                "title": s.title,
                "sourceDocumentId": s.source_document_id,
                "sourceLocation": s.source_location,
                "chunkId": getattr(s, "chunk_id", None),
                "runId": getattr(s, "run_id", None),
            }
            for s in response.sources
        ],
    }


def _replace_verdict(
    result: ValidationResultDTO,
    *,
    verdict: str,
    notes: str | None,
) -> ValidationResultDTO:
    """Return a new result DTO with `tester_verdict` + `tester_notes`
 swapped. Avoids `dataclasses.replace` so callers don't have to
 import dataclasses just to mutate two fields. Frozen dataclasses
 can't be edited in place — this is the supported pattern."""
    return ValidationResultDTO(
        result_id=result.result_id,
        test_case_id=result.test_case_id,
        status=result.status,
        question=result.question,
        answer=result.answer,
        retrieved_chunks=list(result.retrieved_chunks),
        citations=list(result.citations),
        checks=list(result.checks),
        judge_notes=result.judge_notes,
        failure_reason=result.failure_reason,
        tester_verdict=verdict,  # type: ignore[arg-type]
        tester_notes=notes,
    )


def _replace_run_results(
    vrun: ValidationRunDTO,
    *,
    results: list[ValidationResultDTO],
) -> ValidationRunDTO:
    """Return a new run DTO with `results` swapped. Same field-by-
 field copy pattern as `_replace_verdict`."""
    return ValidationRunDTO(
        validation_run_id=vrun.validation_run_id,
        validation_set_id=vrun.validation_set_id,
        run_id=vrun.run_id,
        execution_status=vrun.execution_status,
        validation_status=vrun.validation_status,
        started_at=vrun.started_at,
        completed_at=vrun.completed_at,
        actor=vrun.actor,
        summary=vrun.summary,
        results=results,
        failure_message=vrun.failure_message,
        metadata=dict(vrun.metadata),
    )


def _render_markdown_report(vrun: ValidationRunDTO) -> str:
    """Compose a Markdown validation report for one terminal run.

 Sections (in order):
 1. Header — run id, set id, status, timestamps.
 2. Summary — counters + recommendation + main issues.
 3. Coverage — by-type / by-priority counts.
 4. Per-case results — question, status, tester verdict,
 answer, citations, checks (failed first).

 Render rules:
 * `executionStatus` and `validationStatus` are surfaced
 side-by-side; the split is the operator's main signal.
 * Tester verdicts (when set) appear next to the auto status
 as `auto: failed → tester: pass` so the override is
 explicit.
 * Failed cases bubble to the top of the per-case list (the
 thing testers want to act on).
 * Long content is hard-wrapped to ~120 cols where reasonable;
 we don't actually re-wrap user-provided text — that's the
 producer's responsibility.
 """
    lines: list[str] = []
    lines.append(f"# Validation Report — {vrun.validation_run_id}")
    lines.append("")
    lines.append(
        f"- **Ingestion run:** `{vrun.run_id}`  ·  "
        f"**Validation set:** `{vrun.validation_set_id}`"
    )
    lines.append(
        f"- **Execution status:** `{vrun.execution_status}`  ·  "
        f"**Validation status:** `{vrun.validation_status}`"
    )
    lines.append(
        f"- **Started:** {vrun.started_at}  ·  "
        f"**Completed:** {vrun.completed_at or '—'}"
    )
    lines.append(f"- **Actor:** {vrun.actor}")
    if vrun.failure_message:
        lines.append(f"- **Failure message:** {vrun.failure_message}")
    lines.append("")

    # Summary counts.
    s = vrun.summary
    lines.append("## Summary")
    lines.append("")
    lines.append(
        f"- Total: **{s.total}**  ·  "
        f"Passed: **{s.passed}**  ·  Warning: **{s.warning}**  ·  "
        f"Failed: **{s.failed}**  ·  Skipped: **{s.skipped}**"
    )
    if s.recommended_action:
        lines.append(f"- **Recommendation:** {s.recommended_action}")
    if s.main_issues:
        lines.append("- **Main issues:**")
        for issue in s.main_issues:
            lines.append(f"    - {issue}")
    lines.append("")

    # Coverage.
    cov = s.coverage
    if cov.by_type or cov.by_priority:
        lines.append("## Coverage")
        lines.append("")
        if cov.by_type:
            lines.append("### By type")
            for k, v in sorted(cov.by_type.items()):
                lines.append(f"- `{k}`: {v}")
            lines.append("")
        if cov.by_priority:
            lines.append("### By priority")
            for k, v in sorted(cov.by_priority.items()):
                lines.append(f"- `{k}`: {v}")
            lines.append("")

    # Per-case results — failed first, then warning, then passed,
    # then skipped. Within each bucket: original execution order so
    # smoke-priority shows before the rest.
    lines.append("## Results")
    lines.append("")
    bucket_order = {
        "failed": 0, "warning": 1, "passed": 2, "skipped": 3,
    }
    sorted_results = sorted(
        enumerate(vrun.results),
        key=lambda pair: (bucket_order.get(pair[1].status, 99), pair[0]),
    )
    for _, r in sorted_results:
        lines.extend(_render_result_section(r))

    return "\n".join(lines).rstrip() + "\n"


def _render_result_section(r: ValidationResultDTO) -> list[str]:
    """Per-case Markdown block. Used by `_render_markdown_report`."""
    lines: list[str] = []
    status_marker = {
        "passed": "✓",
        "warning": "⚠",
        "failed": "✗",
        "skipped": "⊝",
    }.get(r.status, "?")
    title = f"{status_marker} {r.test_case_id} — `{r.status}`"
    if r.tester_verdict and r.tester_verdict != r.status:
        # Make the override explicit when it disagrees with auto.
        title += f" (tester: `{r.tester_verdict}`)"
    elif r.tester_verdict:
        title += f" · tester: `{r.tester_verdict}`"
    lines.append(f"### {title}")
    lines.append("")
    lines.append(f"**Question:** {r.question}")
    lines.append("")
    if r.failure_reason:
        lines.append(f"**Failure reason:** {r.failure_reason}")
        lines.append("")
    if r.answer:
        # Indent the answer as a quote block so multi-line answers
        # don't break the heading hierarchy.
        for ln in r.answer.splitlines() or [""]:
            lines.append(f"> {ln}")
        lines.append("")
    if r.tester_notes:
        lines.append(f"**Tester notes:** {r.tester_notes}")
        lines.append("")
    if r.checks:
        lines.append("**Checks:**")
        for c in r.checks:
            mark = "✓" if c.passed else "✗"
            sev = c.severity
            line = f"- {mark} `{c.name}` ({sev})"
            if c.detail:
                line += f" — {c.detail}"
            lines.append(line)
        lines.append("")
    if r.citations:
        lines.append("**Citations:**")
        for c in r.citations:
            piece = f"`{c.artifact_type}` · `{c.artifact_id}`"
            if c.chunk_id:
                piece += f" · chunk `{c.chunk_id}`"
            if c.source_location:
                piece += f" · {c.source_location}"
            lines.append(f"- {piece}")
        lines.append("")
    return lines


def _inconclusive_response(
    *,
    request_id: str,
    run_id: str,
    question: str,
    error: str,
) -> ManualTestQueryResponseDTO:
    """Build a response for the engine-failure path.

 `validation_status` is `inconclusive` (not `failed`) so the FE
 renders this as "couldn't determine" rather than "the document
 doesn't answer the question." Operators shouldn't act on a
 failed deterministic check that didn't actually run.
 """
    failure_check = ValidationCheckDTO(
        name="engine_invocation",
        severity="required",
        passed=False,
        detail=f"engine raised: {error}",
        expected="successful query",
        actual="exception",
    )
    return ManualTestQueryResponseDTO(
        request_id=request_id,
        run_id=run_id,
        question=question,
        answer="",
        mode_used="",
        retrieved_chunks=[],
        citations=[],
        checks=[failure_check],
        validation_status="inconclusive",
        evidence_flags={
            "graphUsed": False,
            "tablesUsed": False,
            "imagesUsed": False,
        },
        raw_response=None,
    )
