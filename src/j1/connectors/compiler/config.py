from dataclasses import dataclass, field

from j1.orchestration.temporal.retries import RetryPolicySpec

ADAPTER_SUBPROCESS = "subprocess"
ADAPTER_CALLABLE = "callable"

ARTIFACT_KIND_INDEX = "compiled_index"
ARTIFACT_KIND_SUMMARY = "compiled_summary"
ARTIFACT_KIND_CONCEPT = "compiled_concept"
ARTIFACT_KIND_SOURCE = "compiled_source"
ARTIFACT_KIND_REPORT = "compiled_report"
ARTIFACT_KIND_LOG = "compiler_log"

DEFAULT_OUTPUT_MAPPING: dict[str, str] = {
    "index.json": ARTIFACT_KIND_INDEX,
    "summary.md": ARTIFACT_KIND_SUMMARY,
    "concepts.json": ARTIFACT_KIND_CONCEPT,
    "sources.json": ARTIFACT_KIND_SOURCE,
    "report.md": ARTIFACT_KIND_REPORT,
    "log.txt": ARTIFACT_KIND_LOG,
}

DEFAULT_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class CompilerConfig:
    enabled: bool = True
    adapter: str = ADAPTER_CALLABLE
    command: tuple[str, ...] = ()
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    output_mapping: dict[str, str] = field(default_factory=dict)
    retry_policy: RetryPolicySpec | None = None

    def effective_output_mapping(self) -> dict[str, str]:
        return dict(self.output_mapping) if self.output_mapping else dict(
            DEFAULT_OUTPUT_MAPPING
        )
