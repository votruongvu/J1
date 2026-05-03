from dataclasses import dataclass, field

from j1.orchestration.temporal.retries import RetryPolicySpec

ADAPTER_SUBPROCESS = "subprocess"
ADAPTER_CALLABLE = "callable"

ARTIFACT_KIND_GRAPH_JSON = "graph_json"
ARTIFACT_KIND_GRAPH_HTML = "graph_html"
ARTIFACT_KIND_GRAPH_REPORT = "graph_report"
ARTIFACT_KIND_GRAPH_CACHE = "graph_cache"
ARTIFACT_KIND_GRAPH_METADATA = "graph_metadata"

DEFAULT_GRAPH_OUTPUT_MAPPING: dict[str, str] = {
    "graph.json": ARTIFACT_KIND_GRAPH_JSON,
    "graph.html": ARTIFACT_KIND_GRAPH_HTML,
    "report.md": ARTIFACT_KIND_GRAPH_REPORT,
    "cache.bin": ARTIFACT_KIND_GRAPH_CACHE,
    "metadata.json": ARTIFACT_KIND_GRAPH_METADATA,
}

DEFAULT_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class GraphConfig:
    enabled: bool = True
    adapter: str = ADAPTER_CALLABLE
    command: tuple[str, ...] = ()
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    corpus_include: tuple[str, ...] = ()
    output_mapping: dict[str, str] = field(default_factory=dict)
    cache_enabled: bool = True
    retry_policy: RetryPolicySpec | None = None

    def effective_output_mapping(self) -> dict[str, str]:
        return dict(self.output_mapping) if self.output_mapping else dict(
            DEFAULT_GRAPH_OUTPUT_MAPPING
        )
