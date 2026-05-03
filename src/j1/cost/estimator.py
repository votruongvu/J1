from decimal import Decimal

from j1.cost.router import ModelRouter, TaskCategory

DEFAULT_CHARS_PER_TOKEN = 4.0


class CostEstimator:
    """Pre-flight cost estimator: predicts spend before an LLM call.

    The estimate is a Decimal in the model's currency (USD by default at
    `ModelSelection`-level). Pass either `input_chars` (auto-converted to
    tokens) or `input_tokens` directly. `expected_output_tokens` defaults
    to the selection's `max_output_tokens` so worst-case is bounded.
    """

    def __init__(
        self,
        router: ModelRouter,
        *,
        chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
    ) -> None:
        self._router = router
        self._chars_per_token = chars_per_token

    def estimate(
        self,
        task: TaskCategory,
        *,
        input_chars: int = 0,
        input_tokens: int | None = None,
        expected_output_tokens: int | None = None,
    ) -> Decimal:
        selection = self._router.select(task)
        if input_tokens is None:
            input_tokens = max(1, int(input_chars / self._chars_per_token))
        out_tokens = (
            expected_output_tokens
            if expected_output_tokens is not None
            else selection.max_output_tokens
        )
        return (
            Decimal(input_tokens) * selection.cost_per_input_token
            + Decimal(out_tokens) * selection.cost_per_output_token
        )
