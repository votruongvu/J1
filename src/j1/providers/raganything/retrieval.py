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
from j1.providers.errors import ProviderUnavailable
from j1.providers.raganything.compiler import PROVIDER_NAME
from j1.providers.raganything.settings import RAGAnythingSettings


@dataclass(frozen=True)
class RAGAnythingQueryRequest:
    ctx: ProjectContext
    question: str
    max_results: int | None
    settings: RAGAnythingSettings
    text_client: Any
    embedding_client: Any | None


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
    ) -> QueryResult:
        request = RAGAnythingQueryRequest(
            ctx=ctx,
            question=question,
            max_results=max_results,
            settings=self._settings,
            text_client=self._llm_registry.text(),
            embedding_client=self._llm_registry.try_embedding(),
        )
        try:
            return self._query_callable(request)
        except ProviderUnavailable:
            raise
        except Exception as exc:
            return QueryResult(
                status=ResultStatus.FAILED,
                error=str(exc),
                message=type(exc).__name__,
                metadata={"provider": PROVIDER_NAME},
            )


def _build_default_query_callable() -> QueryCallable:
    def _delegate(request: RAGAnythingQueryRequest) -> QueryResult:
        try:
            import raganything  # noqa: F401
        except ImportError as exc:
            raise ProviderUnavailable(
                "RAGAnything retrieval requires the `raganything` package. "
                "Install with: pip install raganything"
            ) from exc
        raise ProviderUnavailable(
            "RAGAnything query() is not yet wired in this build. Provide a "
            "custom `query_callable` to RAGAnythingQueryProvider(...) until "
            "the default integration ships, or override the adapter."
        )

    return _delegate
