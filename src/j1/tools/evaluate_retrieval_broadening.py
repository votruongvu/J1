"""Developer-facing A/B harness for alias-driven retrieval broadening.

Runs a fixed query set against a target scope twice — once with
``J1_QUERY_EXPANSION_ENABLED=false`` (baseline) and once with it
``true`` (variant) — and emits a structured JSON report comparing
retrieval counts, expansion diagnostics, and pack-vs-enrichment
provenance.

Scope:

* Read-only by contract. The harness never persists artifacts,
  promotes snapshots, mutates run state, or writes audit rows.
* Honours every gate the production query path enforces: snapshot
  scope filtering, ``document_id`` filtering on enrichment
  artifacts, queryability pre-check. No alias "shortcut loader"
  exists in this module.
* Does not implement graph expansion. The
  ``UnsupportedGraphExpansion`` default stays in place; the
  harness only evaluates alias broadening.

The runner is the seam tests inject. In production it wraps the
:class:`j1.validation.service.IngestionValidationService`; tests
inject a stub callable so the harness logic is verified without
standing up a full validation pipeline.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from j1.query.orchestrator import ENV_QUERY_EXPANSION_ENABLED


_log = logging.getLogger("j1.tools.evaluate_retrieval_broadening")


__all__ = [
    "QueryInput",
    "QueryOutcome",
    "EvaluationResult",
    "EvaluationReport",
    "RetrievalBroadeningEvaluator",
    "RunnerCallable",
    "load_queries",
    "main",
]


# ---- Types ---------------------------------------------------------


@dataclass(frozen=True)
class QueryInput:
    """One question + an opaque id the report keys on."""

    id: str
    question: str


# Runner signature: takes a question, returns a dict matching the
# validation surface's ``ManualTestQueryResponseDTO`` shape (or a
# stub with the same key set). The harness reads:
#
#   * ``retrieved_chunks`` — list, length = retrieved_count
#   * ``evidence_sent_to_llm`` — list, length = evidence_count
#   * ``debug.orchestrator_trace.augmentation`` — the diagnostics block
#
# Anything missing is recorded as ``None`` with a warning rather
# than raising.
RunnerCallable = Callable[[str], Mapping[str, Any]]


@dataclass
class QueryOutcome:
    """Per-mode capture for one query."""

    retrieved_count: int | None
    evidence_count: int | None
    diagnostics: dict[str, Any]
    top_k_preview: list[str]
    retrieval_latency_ms: int | None
    answer_latency_ms: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "retrieved_count": self.retrieved_count,
            "evidence_count": self.evidence_count,
            "diagnostics": dict(self.diagnostics),
            "top_k_preview": list(self.top_k_preview),
            "retrieval_latency_ms": self.retrieval_latency_ms,
            "answer_latency_ms": self.answer_latency_ms,
        }


@dataclass
class EvaluationResult:
    """Per-query outcome — baseline + variant + a delta block."""

    query_id: str
    question: str
    baseline: QueryOutcome
    alias_broadening: QueryOutcome
    delta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "question": self.question,
            "baseline": self.baseline.to_dict(),
            "alias_broadening": self.alias_broadening.to_dict(),
            "delta": dict(self.delta),
        }


@dataclass
class EvaluationReport:
    """Whole-run report: scope, config, summary, per-query results."""

    generated_at: str
    scope: dict[str, Any]
    config: dict[str, Any]
    summary: dict[str, Any]
    results: list[EvaluationResult]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "scope": dict(self.scope),
            "config": dict(self.config),
            "summary": dict(self.summary),
            "results": [r.to_dict() for r in self.results],
            "warnings": list(self.warnings),
        }


# ---- Evaluator -----------------------------------------------------


class RetrievalBroadeningEvaluator:
    """A/B harness orchestrator.

    Construction takes a ``runner`` callable + a scope dict. The
    scope is recorded verbatim in the report; the harness itself
    doesn't filter results by it (the runner is responsible for
    scope enforcement — the validation service already does this).

    ``evaluate`` runs every query twice, captures diagnostics, and
    returns a fully-populated :class:`EvaluationReport`. The
    returned report is JSON-serialisable via ``to_dict()``.
    """

    def __init__(
        self,
        *,
        runner: RunnerCallable,
        scope: Mapping[str, Any] | None = None,
        top_k_preview_limit: int = 5,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._runner = runner
        self._scope = dict(scope or {})
        self._top_k_preview_limit = max(0, int(top_k_preview_limit))
        self._now = now or (
            lambda: datetime.now(timezone.utc)
        )

    def evaluate(
        self, queries: Iterable[QueryInput],
    ) -> EvaluationReport:
        results: list[EvaluationResult] = []
        warnings: list[str] = []
        for query in queries:
            try:
                baseline = self._run_one(
                    query, expansion_enabled=False,
                    warnings=warnings,
                )
                variant = self._run_one(
                    query, expansion_enabled=True,
                    warnings=warnings,
                )
            except Exception as exc:  # noqa: BLE001 — never abort the batch
                warnings.append(
                    f"query {query.id!r} raised: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue
            results.append(EvaluationResult(
                query_id=query.id,
                question=query.question,
                baseline=baseline,
                alias_broadening=variant,
                delta=_compute_delta(baseline, variant),
            ))
        return EvaluationReport(
            generated_at=self._now().isoformat(),
            scope=self._scope,
            config={
                "baseline": {"alias_broadening_enabled": False},
                "variant": {"alias_broadening_enabled": True},
            },
            summary=_summarize(results),
            results=results,
            warnings=warnings,
        )

    def _run_one(
        self,
        query: QueryInput,
        *,
        expansion_enabled: bool,
        warnings: list[str],
    ) -> QueryOutcome:
        # The env-flag toggle gates the orchestrator's broadening
        # decision. Wrapping it in a context manager guarantees
        # restoration even if the runner raises — the harness
        # MUST NOT leave the process env in an unexpected state
        # after a partial run.
        with _expansion_env(expansion_enabled):
            start = time.perf_counter()
            response = self._runner(query.question)
            elapsed_ms = int((time.perf_counter() - start) * 1000)
        return _capture_outcome(
            response,
            top_k_preview_limit=self._top_k_preview_limit,
            elapsed_ms=elapsed_ms,
            warnings=warnings,
            query_id=query.id,
            mode="alias_broadening" if expansion_enabled else "baseline",
        )


# ---- Capture + summarize ------------------------------------------


def _capture_outcome(
    response: Mapping[str, Any],
    *,
    top_k_preview_limit: int,
    elapsed_ms: int,
    warnings: list[str],
    query_id: str,
    mode: str,
) -> QueryOutcome:
    """Project the runner's response into a :class:`QueryOutcome`.

    Forgiving — missing fields surface as ``None`` plus a logged
    warning. The harness never raises on a shape mismatch."""

    def _get(*keys, default=None):
        cur: Any = response
        for key in keys:
            if cur is None:
                return default
            if isinstance(cur, Mapping):
                cur = cur.get(key)
            else:
                cur = getattr(cur, key, default)
        return cur if cur is not None else default

    retrieved = _get("retrieved_chunks")
    retrieved_count: int | None
    if isinstance(retrieved, list):
        retrieved_count = len(retrieved)
    elif retrieved is None:
        retrieved_count = None
        warnings.append(
            f"query {query_id!r} ({mode}): missing retrieved_chunks"
        )
    else:
        retrieved_count = None
        warnings.append(
            f"query {query_id!r} ({mode}): retrieved_chunks was "
            f"{type(retrieved).__name__}, not list"
        )

    evidence = _get("evidence_sent_to_llm")
    evidence_count: int | None
    if isinstance(evidence, list):
        evidence_count = len(evidence)
    elif evidence is None:
        evidence_count = None
    else:
        evidence_count = None

    augmentation = _get("debug", "orchestrator_trace", "augmentation")
    if not isinstance(augmentation, Mapping):
        warnings.append(
            f"query {query_id!r} ({mode}): no augmentation diagnostics"
            " in response"
        )
        diagnostics: dict[str, Any] = {}
    else:
        diagnostics = _project_diagnostics(augmentation)

    top_k_preview: list[str] = []
    if isinstance(retrieved, list):
        for entry in retrieved[:top_k_preview_limit]:
            top_k_preview.append(_chunk_preview(entry))

    return QueryOutcome(
        retrieved_count=retrieved_count,
        evidence_count=evidence_count,
        diagnostics=diagnostics,
        top_k_preview=top_k_preview,
        retrieval_latency_ms=elapsed_ms,
        answer_latency_ms=None,  # validation surface doesn't split out
    )


def _project_diagnostics(augmentation: Mapping[str, Any]) -> dict[str, Any]:
    """Pick the diagnostic fields the spec asks for, naming them
    so a future trace-shape evolution doesn't silently break the
    harness."""
    source = augmentation.get("source") or ""
    expansions = augmentation.get("expansions") or []
    aliases = augmentation.get("aliases") or []
    retrieval_counts = augmentation.get("retrieval_counts") or {}
    distribution = (
        augmentation.get("final_evidence_distribution") or {}
    )
    enrichment_available = augmentation.get(
        "enrichment_aliases_available", 0,
    )
    enrichment_matched = augmentation.get(
        "enrichment_aliases_matched", [],
    ) or []
    # Pack-alias pairs = total aliases minus the enrichment-matched
    # set. Diagnostic is approximate (we don't tag each pair's
    # source on the trace today) but stable enough for the
    # baseline-vs-variant compare.
    pack_pairs_total = max(len(aliases) - len(enrichment_matched), 0)
    return {
        "applied_to_retrieval": bool(
            augmentation.get("applied_to_retrieval"),
        ),
        "source": source,
        "expansions_used": list(expansions),
        "alias_pairs_available": len(aliases),
        "enrichment_alias_pairs_available": int(enrichment_available),
        "enrichment_alias_pairs_applied": len(enrichment_matched),
        "pack_alias_pairs_available": pack_pairs_total,
        "retrieval_counts": dict(retrieval_counts),
        "final_evidence_distribution": dict(distribution),
        "enrichment_aliases_matched": [
            dict(m) for m in enrichment_matched
        ],
    }


def _chunk_preview(entry: Any) -> str:
    """Render a chunk-ref-like dict as a single string preview.

    Tolerant — works against the validation service's
    ``RetrievedChunkRefDTO`` shape (carries
    ``chunk_id``/``preview``) and against arbitrary dicts /
    objects."""
    if isinstance(entry, Mapping):
        chunk_id = entry.get("chunk_id") or entry.get("artifact_id") or ""
        preview = entry.get("preview") or entry.get("text_preview") or ""
    else:
        chunk_id = getattr(entry, "chunk_id", None) or getattr(
            entry, "artifact_id", "",
        )
        preview = getattr(entry, "preview", None) or getattr(
            entry, "text_preview", "",
        )
    chunk_id = str(chunk_id or "")
    preview = str(preview or "")
    if preview:
        return f"{chunk_id}: {preview[:80]}"
    return chunk_id


def _compute_delta(
    baseline: QueryOutcome,
    variant: QueryOutcome,
) -> dict[str, Any]:
    """Per-query baseline-vs-variant diff. Only fields the spec
    surfaces explicitly — keeps the report tight."""
    return {
        "retrieved_count": _subtract(
            variant.retrieved_count, baseline.retrieved_count,
        ),
        "evidence_count": _subtract(
            variant.evidence_count, baseline.evidence_count,
        ),
        "enrichment_alias_pairs_applied": (
            variant.diagnostics.get(
                "enrichment_alias_pairs_applied", 0,
            )
            - baseline.diagnostics.get(
                "enrichment_alias_pairs_applied", 0,
            )
        ),
    }


def _subtract(a: int | None, b: int | None) -> int | None:
    if a is None or b is None:
        return None
    return a - b


def _summarize(
    results: list[EvaluationResult],
) -> dict[str, Any]:
    """Roll up the per-query results into the spec's summary
    block. Simple counts — no statistical scoring."""
    if not results:
        return {
            "query_count": 0,
            "baseline_avg_retrieved_count": 0.0,
            "alias_broadening_avg_retrieved_count": 0.0,
            "queries_with_more_results": 0,
            "queries_with_same_results": 0,
            "queries_with_fewer_results": 0,
            "queries_with_enrichment_aliases_available": 0,
            "queries_with_enrichment_aliases_applied": 0,
        }
    baseline_total = 0
    baseline_n = 0
    variant_total = 0
    variant_n = 0
    more = same = fewer = 0
    enrichment_available_count = 0
    enrichment_applied_count = 0
    for result in results:
        b = result.baseline.retrieved_count
        v = result.alias_broadening.retrieved_count
        if b is not None:
            baseline_total += b
            baseline_n += 1
        if v is not None:
            variant_total += v
            variant_n += 1
        if b is not None and v is not None:
            if v > b:
                more += 1
            elif v == b:
                same += 1
            else:
                fewer += 1
        if result.alias_broadening.diagnostics.get(
            "enrichment_alias_pairs_available", 0,
        ) > 0:
            enrichment_available_count += 1
        if result.alias_broadening.diagnostics.get(
            "enrichment_alias_pairs_applied", 0,
        ) > 0:
            enrichment_applied_count += 1
    return {
        "query_count": len(results),
        "baseline_avg_retrieved_count": (
            round(baseline_total / baseline_n, 2) if baseline_n else 0.0
        ),
        "alias_broadening_avg_retrieved_count": (
            round(variant_total / variant_n, 2) if variant_n else 0.0
        ),
        "queries_with_more_results": more,
        "queries_with_same_results": same,
        "queries_with_fewer_results": fewer,
        "queries_with_enrichment_aliases_available": (
            enrichment_available_count
        ),
        "queries_with_enrichment_aliases_applied": (
            enrichment_applied_count
        ),
    }


# ---- Env toggle context manager -----------------------------------


@contextmanager
def _expansion_env(enabled: bool):
    """Set ``J1_QUERY_EXPANSION_ENABLED`` for the duration of the
    block. Restores the prior value (or absence) on exit, even on
    exception. Used per-query so the harness can A/B without
    leaving env state dirty between queries."""
    key = ENV_QUERY_EXPANSION_ENABLED
    prior_present = key in os.environ
    prior_value = os.environ.get(key)
    os.environ[key] = "true" if enabled else "false"
    try:
        yield
    finally:
        if prior_present:
            os.environ[key] = prior_value  # type: ignore[assignment]
        else:
            os.environ.pop(key, None)


# ---- Queries file parsing -----------------------------------------


def load_queries(path: Path) -> list[QueryInput]:
    """Parse the queries file. Accepts JSON ``{"queries": [...]}``
    or JSONL (one JSON object per line). Each entry must carry
    ``id`` + ``question``. Malformed entries raise ``ValueError``
    with a precise message — the harness fails fast on bad input
    rather than silently dropping queries."""
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    items: list[Mapping[str, Any]]
    # Detect JSON vs JSONL by trying JSON first. JSONL files have
    # one JSON object per line, so they fail the single-document
    # parse with "Extra data" — we then fall back to line-by-line.
    # A leading ``{`` plus a valid full-document parse with a
    # ``queries`` list is the JSON shape.
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, Mapping):
        items_field = payload.get("queries")
        if not isinstance(items_field, list):
            raise ValueError(
                "queries JSON must contain a top-level 'queries' "
                "array of objects"
            )
        items = [i for i in items_field if isinstance(i, Mapping)]
    else:
        items = []
        for lineno, line in enumerate(raw.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"queries file is not valid JSON or JSONL "
                    f"(line {lineno}): {exc}"
                ) from exc
            if not isinstance(entry, Mapping):
                raise ValueError(
                    f"queries JSONL line {lineno} is not a JSON object"
                )
            items.append(entry)
    out: list[QueryInput] = []
    for index, item in enumerate(items, start=1):
        question = item.get("question") or ""
        if not question:
            raise ValueError(
                f"queries entry {index} missing 'question'"
            )
        out.append(QueryInput(
            id=str(item.get("id") or f"q{index}"),
            question=str(question),
        ))
    return out


# ---- CLI -----------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="j1.tools.evaluate_retrieval_broadening",
        description=(
            "Run a fixed query set twice (baseline vs alias "
            "broadening) and emit a JSON A/B report."
        ),
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--document-id", default=None)
    parser.add_argument("--snapshot-id", default=None)
    parser.add_argument(
        "--queries-file", required=True, type=Path,
    )
    parser.add_argument(
        "--output", default=None, type=Path,
        help="Write the report JSON here; default stdout.",
    )
    parser.add_argument(
        "--tenant-id", default=None,
        help=(
            "Tenant id for scope. Falls back to "
            "``J1_DEFAULT_TENANT_ID`` env if unset."
        ),
    )
    return parser.parse_args(argv)


def _build_runner(
    args: argparse.Namespace,
) -> RunnerCallable:
    """Build the production runner that wraps the validation
    service. Imports happen here so test-only callers can use the
    evaluator class directly without dragging in the bootstrap
    layer."""
    from j1.projects.context import ProjectContext
    from j1.validation.dtos import (
        ManualTestQueryRequest,
        QueryScopeDTO,
    )

    tenant_id = (
        args.tenant_id
        or os.environ.get("J1_DEFAULT_TENANT_ID")
        or ""
    )
    if not tenant_id:
        raise SystemExit(
            "tenant id required: pass --tenant-id or set "
            "J1_DEFAULT_TENANT_ID"
        )

    # Lazy import — `deploy.dev._wiring` knows how to construct
    # the full validation service against the deployment's data
    # root. Not every deployment uses this; if the project has a
    # different bootstrap, a custom runner can be wired by
    # consumers calling the evaluator directly.
    try:
        from deploy.dev._wiring import (
            build_validation_service_for_tool,
        )
    except ImportError as exc:
        raise SystemExit(
            "CLI runner requires deploy.dev._wiring."
            f"build_validation_service_for_tool: {exc}"
        ) from exc

    service = build_validation_service_for_tool(
        tenant_id=tenant_id, project_id=args.project_id,
    )
    ctx = ProjectContext(tenant_id=tenant_id, project_id=args.project_id)

    def _runner(question: str) -> Mapping[str, Any]:
        scope_dto: QueryScopeDTO
        if args.document_id:
            scope_dto = QueryScopeDTO(
                type="document_active", document_id=args.document_id,
            )
            response = service.run_document_test_query(
                ctx, args.document_id,
                ManualTestQueryRequest(
                    question=question, scope=scope_dto,
                ),
            )
        else:
            scope_dto = QueryScopeDTO(type="project_active")
            response = service.run_project_query(
                ctx,
                ManualTestQueryRequest(
                    question=question, scope=scope_dto,
                ),
            )
        return _response_to_mapping(response)

    return _runner


def _response_to_mapping(response: Any) -> Mapping[str, Any]:
    """Project the typed validation-surface response into the
    Mapping shape the evaluator reads."""
    return {
        "retrieved_chunks": list(getattr(response, "retrieved_chunks", [])),
        "evidence_sent_to_llm": list(
            getattr(response, "evidence_sent_to_llm", []),
        ),
        "debug": dict(getattr(response, "debug", {}) or {}),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(level=os.environ.get("J1_LOG_LEVEL", "INFO"))
    queries = load_queries(args.queries_file)
    runner = _build_runner(args)
    evaluator = RetrievalBroadeningEvaluator(
        runner=runner,
        scope={
            "project_id": args.project_id,
            "document_id": args.document_id,
            "snapshot_id": args.snapshot_id,
        },
    )
    report = evaluator.evaluate(queries)
    payload = json.dumps(report.to_dict(), indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
        _log.info("wrote retrieval-broadening report to %s", args.output)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
