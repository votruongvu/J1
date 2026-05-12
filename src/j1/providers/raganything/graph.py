"""RAGAnything-backed `GraphBuilder`.

Same construction pattern as the compiler: `from_default` lazy-
imports the vendor library; tests inject a callable directly.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from j1.llm.registry import LLM_ROLE_TEXT, LLMProviderRegistry
from j1.processing.results import ArtifactProcessingResult, ResultStatus
from j1.projects.context import ProjectContext
from j1.providers.errors import ProviderUnavailable
from j1.providers.raganything.compiler import PROVIDER_NAME
from j1.providers.raganything.settings import RAGAnythingSettings


@dataclass(frozen=True)
class RAGAnythingGraphRequest:
    """Graph-build request.

    ``document_id`` + ``run_id`` are the per-run scoping inputs the
    bridge needs to (a) point LightRAG at the right ``working_dir``,
    and (b) stamp every emitted ``graph_json`` draft with the
    document/run lineage so retrieval and validation can scope
    correctly without falling back to ``run_id=None``. Both are
    optional — legacy callers / direct tests can omit them, in which
    case the bridge uses the historical unscoped workdir AND emits
    drafts with empty ``run_id`` (caught by the orchestration-layer
    fail-fast gate). Production callers ALWAYS pass them.
    """
    ctx: ProjectContext
    artifact_ids: list[str]
    settings: RAGAnythingSettings
    text_client: Any
    embedding_client: Any | None
    document_id: str | None = None
    run_id: str | None = None


GraphCallable = Callable[[RAGAnythingGraphRequest], ArtifactProcessingResult]


class RAGAnythingGraphBuilder:
    kind: str = PROVIDER_NAME

    def __init__(
        self,
        *,
        llm_registry: LLMProviderRegistry,
        settings: RAGAnythingSettings,
        graph_callable: GraphCallable,
    ) -> None:
        self._llm_registry = llm_registry
        self._settings = settings
        self._graph_callable = graph_callable

    @classmethod
    def from_default(
        cls,
        *,
        llm_registry: LLMProviderRegistry,
        settings: RAGAnythingSettings,
    ) -> "RAGAnythingGraphBuilder":
        graph_callable: GraphCallable
        if settings.graph_processor:
            from j1.llm.classloader import resolve_callable
            graph_callable = resolve_callable(settings.graph_processor)
        else:
            graph_callable = _build_default_graph_callable()
        return cls(
            llm_registry=llm_registry,
            settings=settings,
            graph_callable=graph_callable,
        )

    def build(
        self,
        ctx: ProjectContext,
        artifact_ids: list[str],
        *,
        document_id: str | None = None,
        run_id: str | None = None,
    ) -> ArtifactProcessingResult:
        request = RAGAnythingGraphRequest(
            ctx=ctx,
            artifact_ids=list(artifact_ids),
            settings=self._settings,
            text_client=self._llm_registry.text(),
            embedding_client=self._llm_registry.try_embedding(),
            document_id=document_id,
            run_id=run_id,
        )
        try:
            return self._graph_callable(request)
        except ProviderUnavailable:
            raise
        except Exception as exc:
            return ArtifactProcessingResult(
                status=ResultStatus.FAILED,
                error=str(exc),
                message=type(exc).__name__,
                drafts=[],
                metadata={"provider": PROVIDER_NAME},
            )


def _build_default_graph_callable() -> GraphCallable:
    """Real default boundary — collects RAGAnything's graph artifacts.

 RAGAnything builds the knowledge graph as a side-effect of
 `process_document_complete`. This callable scans the
 storage_dir for graph-shaped output files (graph_*.json, entity-
 relation files, etc.) and surfaces them as `graph_json`
 `ArtifactDraft`s.
 """

    def _delegate(request: RAGAnythingGraphRequest) -> ArtifactProcessingResult:
        from j1.providers.raganything._bridge import default_build_graph
        return default_build_graph(request)

    return _delegate
