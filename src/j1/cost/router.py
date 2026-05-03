from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from j1.errors.exceptions import CostControlError

if TYPE_CHECKING:
    # Type-only import — `cost` initializes before `processing` in the chain
    # triggered by `j1.cost.__init__`, so a runtime import here creates a
    # cycle (`processing.contracts → processing.results → cost.breakdown →
    # cost.router`). Documented in feedback_import_layering memory.
    from j1.processing.contracts import ModelProvider

DEFAULT_PROVIDER_KIND = "default"


class TaskCategory(StrEnum):
    CLASSIFICATION = "classification"
    SUMMARIZATION = "summarization"
    EXTRACTION = "extraction"
    VISUAL_DESCRIPTION = "visual_description"
    FORMULA_ANALYSIS = "formula_analysis"
    GRAPH_EXTRACTION = "graph_extraction"
    QUERY_ANSWERING = "query_answering"
    REPORT_GENERATION = "report_generation"


@dataclass(frozen=True)
class ModelSelection:
    provider_kind: str
    model_name: str
    enabled: bool = True
    cost_per_input_token: Decimal = Decimal("0")
    cost_per_output_token: Decimal = Decimal("0")
    max_output_tokens: int = 1024
    metadata: dict[str, str] = field(default_factory=dict)


# Approximate per-token prices (USD). Real prices change frequently — these
# are reasonable order-of-magnitude defaults, fully overridable via the
# `mapping` argument to `ModelRouter`.
_HAIKU_INPUT = Decimal("0.0000008")
_HAIKU_OUTPUT = Decimal("0.000004")
_SONNET_INPUT = Decimal("0.000003")
_SONNET_OUTPUT = Decimal("0.000015")
_OPUS_INPUT = Decimal("0.000015")
_OPUS_OUTPUT = Decimal("0.000075")


DEFAULT_TASK_TO_MODEL: dict[TaskCategory, ModelSelection] = {
    # Cheap, deterministic-ish work → small model.
    TaskCategory.CLASSIFICATION: ModelSelection(
        provider_kind=DEFAULT_PROVIDER_KIND,
        model_name="claude-haiku-4-5",
        cost_per_input_token=_HAIKU_INPUT,
        cost_per_output_token=_HAIKU_OUTPUT,
        max_output_tokens=512,
    ),
    # Balanced reasoning → mid-tier.
    TaskCategory.SUMMARIZATION: ModelSelection(
        provider_kind=DEFAULT_PROVIDER_KIND,
        model_name="claude-sonnet-4-6",
        cost_per_input_token=_SONNET_INPUT,
        cost_per_output_token=_SONNET_OUTPUT,
        max_output_tokens=2048,
    ),
    TaskCategory.EXTRACTION: ModelSelection(
        provider_kind=DEFAULT_PROVIDER_KIND,
        model_name="claude-sonnet-4-6",
        cost_per_input_token=_SONNET_INPUT,
        cost_per_output_token=_SONNET_OUTPUT,
        max_output_tokens=4096,
    ),
    TaskCategory.GRAPH_EXTRACTION: ModelSelection(
        provider_kind=DEFAULT_PROVIDER_KIND,
        model_name="claude-sonnet-4-6",
        cost_per_input_token=_SONNET_INPUT,
        cost_per_output_token=_SONNET_OUTPUT,
        max_output_tokens=4096,
    ),
    TaskCategory.FORMULA_ANALYSIS: ModelSelection(
        provider_kind=DEFAULT_PROVIDER_KIND,
        model_name="claude-sonnet-4-6",
        cost_per_input_token=_SONNET_INPUT,
        cost_per_output_token=_SONNET_OUTPUT,
        max_output_tokens=2048,
    ),
    TaskCategory.QUERY_ANSWERING: ModelSelection(
        provider_kind=DEFAULT_PROVIDER_KIND,
        model_name="claude-sonnet-4-6",
        cost_per_input_token=_SONNET_INPUT,
        cost_per_output_token=_SONNET_OUTPUT,
        max_output_tokens=2048,
    ),
    TaskCategory.REPORT_GENERATION: ModelSelection(
        provider_kind=DEFAULT_PROVIDER_KIND,
        model_name="claude-sonnet-4-6",
        cost_per_input_token=_SONNET_INPUT,
        cost_per_output_token=_SONNET_OUTPUT,
        max_output_tokens=4096,
    ),
    # Expensive, vision-capable, off by default — opt-in per spec.
    TaskCategory.VISUAL_DESCRIPTION: ModelSelection(
        provider_kind=DEFAULT_PROVIDER_KIND,
        model_name="claude-opus-4-7",
        enabled=False,
        cost_per_input_token=_OPUS_INPUT,
        cost_per_output_token=_OPUS_OUTPUT,
        max_output_tokens=2048,
    ),
}


class ModelRouter:
    """Maps a `TaskCategory` to a `ModelSelection` and resolves the provider.

    Both the task→model mapping and the provider registry are constructor-
    injected. No vendor is hardcoded — `provider_kind` is just a string key
    that callers wire to whatever `ModelProvider` they configure.
    """

    def __init__(
        self,
        *,
        mapping: Mapping[TaskCategory, ModelSelection] | None = None,
        providers: Mapping[str, ModelProvider] | None = None,
    ) -> None:
        self._mapping: dict[TaskCategory, ModelSelection] = (
            dict(mapping) if mapping is not None else dict(DEFAULT_TASK_TO_MODEL)
        )
        self._providers: dict[str, ModelProvider] = dict(providers or {})

    def select(self, task: TaskCategory) -> ModelSelection:
        try:
            return self._mapping[task]
        except KeyError as exc:
            raise CostControlError(
                f"no model selection configured for task {task.value!r}"
            ) from exc

    def provider_for(self, task: TaskCategory) -> ModelProvider:
        selection = self.select(task)
        if not selection.enabled:
            raise CostControlError(
                f"task {task.value!r} is disabled "
                f"(model {selection.model_name!r} not enabled)"
            )
        try:
            return self._providers[selection.provider_kind]
        except KeyError as exc:
            raise CostControlError(
                f"no model provider registered with kind "
                f"{selection.provider_kind!r} for task {task.value!r}"
            ) from exc

    def is_enabled(self, task: TaskCategory) -> bool:
        try:
            return self.select(task).enabled
        except CostControlError:
            return False

    def register_provider(self, kind: str, provider: ModelProvider) -> None:
        self._providers[kind] = provider

    @property
    def known_tasks(self) -> tuple[TaskCategory, ...]:
        return tuple(self._mapping.keys())
