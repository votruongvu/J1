import json
import logging
from typing import Any

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactRegistry
from j1.connectors.graph.config import ARTIFACT_KIND_GRAPH_JSON
from j1.graph import NormalizedGraph, normalize_graph_bytes
from j1.enrichers import ARTIFACT_TYPE_CONSISTENCY_FINDINGS
from j1.errors.exceptions import DocumentNotFoundError
from j1.intake.registry import SourceRegistry
from j1.profiles.model import Profile
from j1.projects.context import ProjectContext
from j1.query.models import (
    GraphPath,
    QueryMode,
    QueryRequest,
    QueryResponse,
    SourceReference,
)
from j1.query.scope import RunScope
from j1.review.governance import WarningCategory
from j1.search.indexer import SearchHit, SqliteSearchIndexer
from j1.workspace.resolver import WorkspaceResolver


def _filter_by_scope(
    records: list[ArtifactRecord], request: QueryRequest,
) -> list[ArtifactRecord]:
    """Apply `RunScope` + the knowledge-state gate to a list of
 artifacts loaded straight from the registry (graph / consistency
 / report providers don't go through the FTS index, so they need
 their own filter).

 Default `WorkspaceScope` is the no-op for RUN scope, but the
 knowledge-state gate ALWAYS applies — detached/removed documents
 must never reach retrieval regardless of scope. `RunScope`
 additionally keeps only artifacts whose ``metadata.run_id``
 matches.
 """
    # Document-centric gate: drop artifacts tied to detached or
    # removed documents. Centralised in `j1.documents.lifecycle` so
    # this rule lives in exactly one place. No-op by default —
    # pre-refactor records have no `metadata.knowledge_state` and
    # default to "attached".
    from j1.documents.lifecycle import filter_to_attached_artifacts
    records = filter_to_attached_artifacts(records)
    if isinstance(request.scope, RunScope):
        run_id = request.scope.run_id
        return [
            r for r in records
            if str(r.metadata.get("run_id", "")) == run_id
        ]
    return records

_log = logging.getLogger("j1.query.providers")

GRAPH_JSON_KIND = ARTIFACT_KIND_GRAPH_JSON
DEFAULT_REPORT_TEMPLATE_NAME = "default"
PROVIDER_KIND_PREFIX = "query"

REVIEW_STATUS_PENDING = "pending"

_SNIPPET_MAX_CHARS = 240
_GRAPH_PATH_LIMIT = 5


def _paths_from_normalized(
    graphs: list[NormalizedGraph],
) -> list[GraphPath]:
    """Flatten normalised graphs into the `QueryResponse.graph_paths`
 list the runner's `_check_expected_graph_evidence` reads. Capped
 at `_GRAPH_PATH_LIMIT` to keep response payloads bounded."""
    paths: list[GraphPath] = []
    for graph in graphs:
        for rel in graph.relationships:
            paths.append(GraphPath(
                nodes=[rel.from_id, rel.to_id],
                edges=[rel.kind or "related_to"],
                description=rel.label,
            ))
            if len(paths) >= _GRAPH_PATH_LIMIT:
                return paths
    return paths
_GRAPH_FILE_LIMIT = 3


def _hit_to_source(hit: SearchHit) -> SourceReference:
    # Server-derived: `chunk_id` and `run_id` come from the FTS row,
    # which the indexer populated from the artifact's metadata at
    # index time. Never echoed from request input or LLM output.
    return SourceReference(
        artifact_id=hit.artifact_id,
        artifact_type=hit.artifact_type,
        title=hit.title,
        source_document_id=hit.source_document_id,
        source_location=hit.source_location,
        chunk_id=hit.chunk_id,
        run_id=hit.run_id,
    )


def _record_to_source(record: ArtifactRecord) -> SourceReference:
    # CRITICAL: ``run_id`` and ``chunk_id`` MUST be propagated from
    # the artifact's metadata into the projected ``SourceReference``.
    # Validation's ``retrieved_chunks_belong_to_run`` /
    # ``citations_belong_to_run`` checks read these fields straight
    # from ``SourceReference`` (via ``_retrieved_chunks_from_response``
    # in ``j1.validation.runner``). Earlier this function omitted
    # both, so every record-backed source (graph_json, consistency,
    # report providers) projected with ``run_id=None`` regardless
    # of the artifact's actual lineage — making the validator flag
    # them as run-id orphans even when ``metadata.run_id`` was
    # correctly stamped at registration. Classification of the bug:
    # ``retrieval_mapper_missing_run_id``, not
    # ``artifact_persistence_missing_run_id``.
    meta = record.metadata if isinstance(record.metadata, dict) else {}
    return SourceReference(
        artifact_id=record.artifact_id,
        artifact_type=record.kind,
        title=str(meta.get("title", f"{record.kind}/{record.artifact_id}")),
        source_document_id=(
            record.source_document_ids[0] if record.source_document_ids else None
        ),
        source_location=meta.get("source_location"),
        chunk_id=meta.get("chunk_id"),
        run_id=meta.get("run_id"),
    )


class KnowledgeQueryProvider:
    kind = f"{PROVIDER_KIND_PREFIX}.knowledge"
    mode = QueryMode.KNOWLEDGE_FIRST

    def __init__(self, indexer: SqliteSearchIndexer) -> None:
        self._indexer = indexer

    def query(self, ctx: ProjectContext, request: QueryRequest) -> QueryResponse:
        hits = self._indexer.search(
            ctx,
            request.question,
            artifact_types=request.artifact_types or None,
            max_results=request.max_results,
            scope=request.scope,
        )
        sources = [_hit_to_source(h) for h in hits]
        review_required = any(
            h.review_status == REVIEW_STATUS_PENDING for h in hits
        )
        warnings: list[str] = []
        categories: list[WarningCategory] = []
        if review_required:
            warnings.append("Some sources are pending human review.")
            categories.append(WarningCategory.REVIEW_REQUIRED)
        confidence = (
            sum(h.confidence for h in hits) / len(hits) if hits else 0.0
        )
        return QueryResponse(
            answer=self._compose_answer(hits, request.question),
            mode_used=self.mode.value,
            sources=sources,
            related_artifacts=[h.artifact_id for h in hits],
            confidence=confidence,
            review_required=review_required,
            warnings=warnings,
            warning_categories=categories,
        )

    @staticmethod
    def _compose_answer(hits: list[SearchHit], question: str) -> str:
        if not hits:
            return f"No knowledge results for: {question}"
        snippets = []
        for hit in hits[:3]:
            snippet = hit.extracted_text[:_SNIPPET_MAX_CHARS].strip()
            snippets.append(f"- {hit.title}: {snippet}" if snippet else f"- {hit.title}")
        return f"Knowledge results for: {question}\n\n" + "\n".join(snippets)


class GraphQueryProvider:
    kind = f"{PROVIDER_KIND_PREFIX}.graph"
    mode = QueryMode.GRAPH_FIRST

    def __init__(
        self,
        artifacts: ArtifactRegistry,
        workspace: WorkspaceResolver,
    ) -> None:
        self._artifacts = artifacts
        self._workspace = workspace

    def query(self, ctx: ProjectContext, request: QueryRequest) -> QueryResponse:
        records = self._artifacts.list_artifacts(ctx, kind=GRAPH_JSON_KIND)
        records = _filter_by_scope(records, request)
        sources = [_record_to_source(r) for r in records]
        related = [r.artifact_id for r in records]
        # Parse via the normalizer so we recognise canonical /
        # LightRAG / GraphML shapes uniformly. `parse_failures`
        # tracks artifacts that EXIST but couldn't be parsed — those
        # produce a more actionable warning than "no paths found".
        graphs, parse_failures = self._normalize_records(ctx, records)
        paths = _paths_from_normalized(graphs)
        warnings: list[str] = []
        categories: list[WarningCategory] = []
        if parse_failures:
            warnings.append(
                f"{len(parse_failures)} graph artifact(s) could not be "
                f"parsed (unrecognised shape): "
                f"{', '.join(parse_failures[:3])}"
            )
            categories.append(WarningCategory.INFORMATIONAL)
        if not paths and not parse_failures:
            warnings.append("No graph paths found for the question.")
            categories.append(WarningCategory.INFORMATIONAL)
        confidence = 0.5 if paths else 0.1
        return QueryResponse(
            answer=self._compose_answer(
                paths,
                request.question,
                parse_failures=parse_failures,
                graph_count=len(graphs),
            ),
            mode_used=self.mode.value,
            sources=sources,
            related_artifacts=related,
            graph_paths=paths,
            confidence=confidence,
            warnings=warnings,
            warning_categories=categories,
        )

    def _normalize_records(
        self,
        ctx: ProjectContext,
        records: list[ArtifactRecord],
    ) -> tuple[list[NormalizedGraph], list[str]]:
        """Read each graph artifact's bytes and normalise into the
 unified schema. Returns `(parsed, parse_failures)` where
 `parse_failures` is the list of artifact_ids whose files existed
 but didn't match any known graph format — surfaces in the
 warning + answer so the operator knows the artifact reached
 retrieval but couldn't be reasoned about."""
        parsed: list[NormalizedGraph] = []
        parse_failures: list[str] = []
        for record in records[:_GRAPH_FILE_LIMIT]:
            try:
                path = self._workspace.project_root(ctx) / record.location
            except Exception as exc:  # noqa: BLE001 — defensive
                _log.warning(
                    "graph artifact %s: path resolution failed: %s",
                    record.artifact_id, exc,
                )
                continue
            if not path.is_file():
                continue
            try:
                content = path.read_bytes()
            except OSError as exc:
                _log.warning(
                    "graph artifact %s: read failed: %s",
                    record.artifact_id, exc,
                )
                continue
            graph = normalize_graph_bytes(
                content,
                source_artifact_id=record.artifact_id,
                run_id=record.metadata.get("run_id") if record.metadata else None,
            )
            if graph is None:
                parse_failures.append(record.artifact_id)
                continue
            parsed.append(graph)
        return parsed, parse_failures

    @staticmethod
    def _compose_answer(
        paths: list[GraphPath],
        question: str,
        *,
        parse_failures: list[str] | None = None,
        graph_count: int = 0,
    ) -> str:
        if paths:
            rendered = [
                f"- {p.nodes[0]} → {p.nodes[1]} ({p.edges[0] if p.edges else ''})"
                for p in paths
            ]
            return (
                f"Graph relationships for: {question}\n\n"
                + "\n".join(rendered)
            )
        # No paths. Distinguish "no graph artifacts in run" (operator
        # may need to enable graph extraction) from "artifacts exist
        # but format wasn't recognised" (actionable parser bug).
        if parse_failures:
            return (
                f"Found {graph_count + len(parse_failures)} graph "
                f"artifact(s) but {len(parse_failures)} could not be "
                f"parsed into entities/relationships: "
                f"{', '.join(parse_failures[:3])}"
            )
        return f"No graph relationships found for: {question}"


class EvidenceProvider:
    kind = f"{PROVIDER_KIND_PREFIX}.evidence"
    mode = QueryMode.EVIDENCE_FIRST

    def __init__(
        self,
        indexer: SqliteSearchIndexer,
        sources: SourceRegistry,
    ) -> None:
        self._indexer = indexer
        self._sources = sources

    def query(self, ctx: ProjectContext, request: QueryRequest) -> QueryResponse:
        hits = self._indexer.search(
            ctx,
            request.question,
            artifact_types=request.artifact_types or None,
            max_results=request.max_results,
            scope=request.scope,
        )
        evidence: list[SourceReference] = []
        for hit in hits:
            if not hit.source_document_id:
                continue
            try:
                self._sources.get(ctx, hit.source_document_id)
            except DocumentNotFoundError:
                continue
            evidence.append(_hit_to_source(hit))
        confidence = (
            sum(h.confidence for h in hits) / len(hits) if hits else 0.0
        )
        warnings: list[str] = []
        categories: list[WarningCategory] = []
        if not evidence:
            warnings.append("No evidence with linked source documents found.")
            categories.append(WarningCategory.SOURCE_VERIFICATION_REQUIRED)
        return QueryResponse(
            answer=self._compose_answer(evidence, request.question),
            mode_used=self.mode.value,
            sources=evidence,
            related_artifacts=[h.artifact_id for h in hits],
            confidence=confidence,
            warnings=warnings,
            warning_categories=categories,
        )

    @staticmethod
    def _compose_answer(
        evidence: list[SourceReference], question: str
    ) -> str:
        if not evidence:
            return f"No evidence found for: {question}"
        rendered = [
            f"- {e.title} (document: {e.source_document_id}, "
            f"location: {e.source_location or 'n/a'})"
            for e in evidence
        ]
        return f"Evidence for: {question}\n\n" + "\n".join(rendered)


class ConsistencyProvider:
    kind = f"{PROVIDER_KIND_PREFIX}.consistency"
    mode = QueryMode.CONSISTENCY_CHECK

    def __init__(
        self,
        artifacts: ArtifactRegistry,
        workspace: WorkspaceResolver,
    ) -> None:
        self._artifacts = artifacts
        self._workspace = workspace

    def query(self, ctx: ProjectContext, request: QueryRequest) -> QueryResponse:
        records = self._artifacts.list_artifacts(
            ctx, kind=ARTIFACT_TYPE_CONSISTENCY_FINDINGS
        )
        records = _filter_by_scope(records, request)
        findings = self._collect_findings(ctx, records)
        warnings = [f"Consistency: {self._render_finding(f)}" for f in findings[:5]]
        if not warnings:
            warnings = ["No consistency findings recorded yet."]
        categories = [WarningCategory.REVIEW_REQUIRED] * len(warnings)
        return QueryResponse(
            answer=self._compose_answer(findings, request.question),
            mode_used=self.mode.value,
            sources=[_record_to_source(r) for r in records],
            related_artifacts=[r.artifact_id for r in records],
            confidence=0.5 if findings else 0.1,
            review_required=True,
            warnings=warnings,
            warning_categories=categories,
        )

    def _collect_findings(
        self,
        ctx: ProjectContext,
        records: list[ArtifactRecord],
    ) -> list[Any]:
        findings: list[Any] = []
        for record in records:
            if record.metadata.get("format") != "json":
                continue
            try:
                path = self._workspace.project_root(ctx) / record.location
                if not path.is_file():
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            findings.extend(data.get("findings", []))
        return findings

    @staticmethod
    def _render_finding(finding: Any) -> str:
        if isinstance(finding, dict):
            return finding.get("description") or json.dumps(finding)[:120]
        return str(finding)[:200]

    @staticmethod
    def _compose_answer(findings: list[Any], question: str) -> str:
        if not findings:
            return (
                f"No consistency findings recorded for: {question}. "
                "Re-run the consistency checker for fresh results."
            )
        rendered = [
            f"- {ConsistencyProvider._render_finding(f)}" for f in findings[:5]
        ]
        return f"Consistency review for: {question}\n\n" + "\n".join(rendered)


class ReportGenerator:
    kind = f"{PROVIDER_KIND_PREFIX}.report"
    mode = QueryMode.REPORT_GENERATION

    def __init__(
        self,
        indexer: SqliteSearchIndexer,
        profile: Profile,
        *,
        template_name: str = DEFAULT_REPORT_TEMPLATE_NAME,
    ) -> None:
        self._indexer = indexer
        self._profile = profile
        self._template_name = template_name

    def query(self, ctx: ProjectContext, request: QueryRequest) -> QueryResponse:
        hits = self._indexer.list_indexed(
            ctx, artifact_types=request.artifact_types or None
        )
        if isinstance(request.scope, RunScope):
            run_id = request.scope.run_id
            hits = [h for h in hits if (h.run_id or "") == run_id]
        hits = hits[: request.max_results * 2]
        template = self._profile.report_templates.get(self._template_name, "")
        warnings: list[str] = []
        categories: list[WarningCategory] = []
        if template:
            answer = self._render_template(template, hits, request.question)
        else:
            answer = self._stub_report(hits, request.question)
            warnings.append(
                "Profile has no report template; using built-in fallback layout."
            )
            categories.append(WarningCategory.INFORMATIONAL)
        review_required = any(
            h.review_status == REVIEW_STATUS_PENDING for h in hits
        )
        if review_required:
            warnings.append(
                "Report includes artifacts that are pending human review."
            )
            categories.append(WarningCategory.REVIEW_REQUIRED)
            categories.append(WarningCategory.NOT_FOR_FINAL_DECISION)
        return QueryResponse(
            answer=answer,
            mode_used=self.mode.value,
            sources=[_hit_to_source(h) for h in hits[:10]],
            related_artifacts=[h.artifact_id for h in hits],
            confidence=0.6 if hits else 0.0,
            review_required=review_required,
            warnings=warnings,
            warning_categories=categories,
        )

    @staticmethod
    def _stub_report(hits: list[SearchHit], question: str) -> str:
        if not hits:
            return f"# Report: {question}\n\n_No artifacts available._\n"
        lines = [f"- {h.title} ({h.artifact_type})" for h in hits[:20]]
        return f"# Report: {question}\n\n## Artifacts\n\n" + "\n".join(lines)

    @staticmethod
    def _render_template(
        template: str, hits: list[SearchHit], question: str
    ) -> str:
        artifacts_block = (
            "\n".join(f"- {h.title} ({h.artifact_type})" for h in hits[:20])
            if hits
            else "_No artifacts available._"
        )
        return (
            template.replace("{{question}}", question)
            .replace("{{artifacts}}", artifacts_block)
        )
