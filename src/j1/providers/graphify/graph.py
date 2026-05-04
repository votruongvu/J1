"""Graphify-backed `GraphBuilder` (optional alternative)."""

from collections.abc import Callable
from dataclasses import dataclass

from j1.processing.results import ArtifactProcessingResult, ResultStatus
from j1.projects.context import ProjectContext
from j1.providers.errors import ProviderUnavailable
from j1.providers.graphify.settings import GraphifySettings

PROVIDER_NAME = "graphify"


@dataclass(frozen=True)
class GraphifyGraphRequest:
    ctx: ProjectContext
    artifact_ids: list[str]
    settings: GraphifySettings


GraphCallable = Callable[[GraphifyGraphRequest], ArtifactProcessingResult]


class GraphifyGraphBuilder:
    kind: str = PROVIDER_NAME

    def __init__(
        self,
        *,
        settings: GraphifySettings,
        graph_callable: GraphCallable,
    ) -> None:
        self._settings = settings
        self._graph_callable = graph_callable

    @classmethod
    def from_default(
        cls, *, settings: GraphifySettings,
    ) -> "GraphifyGraphBuilder":
        graph_callable: GraphCallable
        if settings.graph_processor:
            from j1.llm.classloader import resolve_callable
            graph_callable = resolve_callable(settings.graph_processor)
        else:
            graph_callable = _build_default_graph_callable()
        return cls(
            settings=settings,
            graph_callable=graph_callable,
        )

    def build(
        self, ctx: ProjectContext, artifact_ids: list[str],
    ) -> ArtifactProcessingResult:
        request = GraphifyGraphRequest(
            ctx=ctx,
            artifact_ids=list(artifact_ids),
            settings=self._settings,
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
    def _delegate(request: GraphifyGraphRequest) -> ArtifactProcessingResult:
        try:
            import graphify  # noqa: F401
        except ImportError as exc:
            raise ProviderUnavailable(
                "Graphify provider requires the `graphify` package. "
                "Install with: pip install graphify"
            ) from exc
        raise ProviderUnavailable(
            "Graphify build() is not yet wired in this build. Provide a "
            "custom `graph_callable` to GraphifyGraphBuilder(...) until "
            "the default integration ships, or override the adapter."
        )

    return _delegate
