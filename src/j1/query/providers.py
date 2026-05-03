import json
from typing import Any

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactRegistry
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
from j1.review.governance import WarningCategory
from j1.search.indexer import SearchHit, SqliteSearchIndexer
from j1.workspace.resolver import WorkspaceResolver

GRAPH_JSON_KIND = "graph_json"
DEFAULT_REPORT_TEMPLATE_NAME = "default"
PROVIDER_KIND_PREFIX = "query"

REVIEW_STATUS_PENDING = "pending"

_SNIPPET_MAX_CHARS = 240
_GRAPH_PATH_LIMIT = 5
_GRAPH_FILE_LIMIT = 3


def _hit_to_source(hit: SearchHit) -> SourceReference:
    return SourceReference(
        artifact_id=hit.artifact_id,
        artifact_type=hit.artifact_type,
        title=hit.title,
        source_document_id=hit.source_document_id,
        source_location=hit.source_location,
    )


def _record_to_source(record: ArtifactRecord) -> SourceReference:
    return SourceReference(
        artifact_id=record.artifact_id,
        artifact_type=record.kind,
        title=str(record.metadata.get("title", f"{record.kind}/{record.artifact_id}")),
        source_document_id=(
            record.source_document_ids[0] if record.source_document_ids else None
        ),
        source_location=record.metadata.get("source_location"),
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
        sources = [_record_to_source(r) for r in records]
        related = [r.artifact_id for r in records]
        paths = self._extract_paths(ctx, records, request.question)
        warnings: list[str] = []
        categories: list[WarningCategory] = []
        if not paths:
            warnings.append("No graph paths found for the question.")
            categories.append(WarningCategory.INFORMATIONAL)
        confidence = 0.5 if paths else 0.1
        return QueryResponse(
            answer=self._compose_answer(paths, request.question),
            mode_used=self.mode.value,
            sources=sources,
            related_artifacts=related,
            graph_paths=paths,
            confidence=confidence,
            warnings=warnings,
            warning_categories=categories,
        )

    def _extract_paths(
        self,
        ctx: ProjectContext,
        records: list[ArtifactRecord],
        question: str,
    ) -> list[GraphPath]:
        paths: list[GraphPath] = []
        for record in records[:_GRAPH_FILE_LIMIT]:
            data = self._read_graph(ctx, record)
            if not data:
                continue
            for edge in data.get("edges", []):
                paths.append(
                    GraphPath(
                        nodes=[
                            str(edge.get("from", "")),
                            str(edge.get("to", "")),
                        ],
                        edges=[str(edge.get("type", "related_to"))],
                        description=edge.get("label"),
                    )
                )
                if len(paths) >= _GRAPH_PATH_LIMIT:
                    return paths
        return paths

    def _read_graph(
        self, ctx: ProjectContext, record: ArtifactRecord
    ) -> dict[str, Any] | None:
        try:
            path = self._workspace.project_root(ctx) / record.location
            if not path.is_file():
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _compose_answer(paths: list[GraphPath], question: str) -> str:
        if not paths:
            return f"No graph relationships found for: {question}"
        rendered = [
            f"- {p.nodes[0]} → {p.nodes[1]} ({p.edges[0] if p.edges else ''})"
            for p in paths
        ]
        return (
            f"Graph relationships for: {question}\n\n" + "\n".join(rendered)
        )


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
