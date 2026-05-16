"""RAGAnything-backed `QueryProvider`.

Implements the framework's `QueryProvider` Protocol so the existing
`HybridQueryEngine` can route queries through RAGAnything's retrieval
features (vector / graph / hybrid).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from j1.llm.registry import LLMProviderRegistry
from j1.processing.results import QueryResult, ResultStatus
from j1.projects.context import ProjectContext
from j1.providers.errors import ProviderUnavailable, WorkspaceScopeMissing
from j1.providers.raganything.compiler import PROVIDER_NAME
from j1.providers.raganything.settings import RAGAnythingSettings


@dataclass(frozen=True)
class RAGAnythingQueryRequest:
    """Graph-aware query request.

    ``document_id`` + ``run_id`` are the per-run scoping inputs the
    bridge uses to select which LightRAG workspace to query.
    Per-run isolation means:

      * ``RunScope(run_id=X)`` validation queries → read from
        ``{workdir}/runs/{tenant}/{project}/{doc}/X/``.
      * ``ActiveScope(document_id=D)`` queries → after Phase 9 the
        resolver returns the sentinel run_id (visibility is
        snapshot-centered). Callers needing active-knowledge
        filtering should use the snapshot eligibility resolver in
        ``j1.query.eligibility`` directly.
      * ``WorkspaceScope`` (project-wide) → no per-run override;
        falls back to ``settings.workdir`` (the legacy unscoped
        graph). This case is rare for graph QA but kept for
        backward-compatibility.

    Both fields are optional — direct test callers can omit them
    and the bridge uses the legacy unscoped workdir.
    """
    ctx: ProjectContext
    question: str
    max_results: int | None
    settings: RAGAnythingSettings
    text_client: Any
    embedding_client: Any | None
    document_id: str | None = None
    run_id: str | None = None
    # Phase 6: snapshot-aware addressing — see RAGAnythingCompileRequest.
    snapshot_id: str | None = None
    working_dir_override: Any = None  # Path | None


QueryCallable = Callable[[RAGAnythingQueryRequest], QueryResult]


class RAGAnythingQueryProvider:
    kind: str = PROVIDER_NAME

    def __init__(
        self,
        *,
        llm_registry: LLMProviderRegistry,
        settings: RAGAnythingSettings,
        query_callable: QueryCallable,
    ) -> None:
        self._llm_registry = llm_registry
        self._settings = settings
        self._query_callable = query_callable

    @classmethod
    def from_default(
        cls,
        *,
        llm_registry: LLMProviderRegistry,
        settings: RAGAnythingSettings,
    ) -> "RAGAnythingQueryProvider":
        query_callable: QueryCallable
        if settings.retrieval_processor:
            from j1.llm.classloader import resolve_callable
            query_callable = resolve_callable(settings.retrieval_processor)
        else:
            query_callable = _build_default_query_callable()
        return cls(
            llm_registry=llm_registry,
            settings=settings,
            query_callable=query_callable,
        )

    def query(
        self,
        ctx: ProjectContext,
        question: str,
        *,
        max_results: int | None = None,
        document_id: str | None = None,
        run_id: str | None = None,
        snapshot_id: str | None = None,
        working_dir_override: Any = None,
    ) -> QueryResult:
        request = RAGAnythingQueryRequest(
            ctx=ctx,
            question=question,
            max_results=max_results,
            settings=self._settings,
            text_client=self._llm_registry.query(),
            embedding_client=self._llm_registry.try_embedding(),
            document_id=document_id,
            run_id=run_id,
            snapshot_id=snapshot_id,
            working_dir_override=working_dir_override,
        )
        try:
            return self._query_callable(request)
        except (ProviderUnavailable, WorkspaceScopeMissing):
            # Missing workspace scope is a caller bug — surface it to
            # the adapter so the trace can render the actual reason
            # instead of converting to a generic FAILED result that
            # operators have to reverse-engineer.
            raise
        except Exception as exc:
            return QueryResult(
                status=ResultStatus.FAILED,
                error=str(exc),
                message=type(exc).__name__,
                metadata={"provider": PROVIDER_NAME},
            )

    def workspace_path_for(
        self,
        ctx: ProjectContext,
        document_id: str | None,
        snapshot_id: str | None,
    ) -> str | None:
        """Return the per-snapshot LightRAG workspace path as a string,
        or ``None`` when the inputs can't form a scoped path.

        Exposed so the validation surface can stamp the resolved
        workspace into debug payloads — operators need to see WHICH
        directory native answered against without inferring it.
        """
        from j1.providers.raganything._bridge import _snapshot_workspace_path
        path = _snapshot_workspace_path(
            self._settings, ctx, document_id, snapshot_id,
        )
        return str(path) if path is not None else None


def _build_default_query_callable() -> QueryCallable:
    """Real default boundary — drives RAGAnything's `aquery`.

 Calls `RAGAnything(...).aquery(question, mode="hybrid")` via
 `asyncio.run`. Returns J1 canonical `QueryResult`.
 """

    def _delegate(request: RAGAnythingQueryRequest) -> QueryResult:
        from j1.providers.raganything._bridge import default_query
        return default_query(request)

    return _delegate
