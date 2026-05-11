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
    """Real default boundary — drives Graphify via CLI subprocess
 or Python-package import (selected by `J1_GRAPHIFY_MODE`).

 Default mode is `cli`: invokes `J1_GRAPHIFY_COMMAND` (default
 `graphify`) as a subprocess, parses its JSON output, returns
 canonical `ArtifactDraft`s. See
 `j1.providers.graphify._bridge` for the full integration.

 Raises `ProviderUnavailable` only when:
 * `mode=cli` and the binary isn't on $PATH
 * `mode=python` and the `graphify` package isn't installed
 * an unsupported mode value is supplied
 * the vendor's API shape doesn't match (actionable: override
 via J1_GRAPHIFY_GRAPH_PROCESSOR)
 """

    def _delegate(request: GraphifyGraphRequest) -> ArtifactProcessingResult:
        from j1.providers.graphify._bridge import default_build_graph
        return default_build_graph(request)

    return _delegate
