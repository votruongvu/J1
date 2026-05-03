from decimal import Decimal

import pytest

from j1.cost.aggregator import CostAggregator
from j1.cost.breakdown import CostBreakdown
from j1.cost.budget import (
    ACTION_BUDGET_ALLOW,
    ACTION_BUDGET_BLOCK,
    ACTION_BUDGET_WARN,
    BudgetCheck,
    BudgetDecision,
    BudgetGuard,
    BudgetLevel,
    BudgetPolicy,
)
from j1.cost.estimator import CostEstimator
from j1.cost.router import (
    DEFAULT_PROVIDER_KIND,
    DEFAULT_TASK_TO_MODEL,
    ModelRouter,
    ModelSelection,
    TaskCategory,
)
from j1.errors.exceptions import CostControlError


# ---- ModelRouter ------------------------------------------------------


def test_default_mapping_covers_all_task_categories():
    for task in TaskCategory:
        assert task in DEFAULT_TASK_TO_MODEL


def test_default_visual_description_is_disabled():
    """Per spec: visual analysis must be selectively enabled (off by default)."""
    assert DEFAULT_TASK_TO_MODEL[TaskCategory.VISUAL_DESCRIPTION].enabled is False


def test_default_classification_uses_cheap_model():
    """Per spec: don't use expensive models for deterministic tasks."""
    classify = DEFAULT_TASK_TO_MODEL[TaskCategory.CLASSIFICATION]
    summarize = DEFAULT_TASK_TO_MODEL[TaskCategory.SUMMARIZATION]
    assert classify.cost_per_input_token < summarize.cost_per_input_token
    assert classify.cost_per_output_token < summarize.cost_per_output_token


def test_router_select_returns_default_mapping():
    router = ModelRouter()
    selection = router.select(TaskCategory.CLASSIFICATION)
    assert selection.model_name == "claude-haiku-4-5"


def test_router_select_unknown_task_raises():
    router = ModelRouter(mapping={})
    with pytest.raises(CostControlError):
        router.select(TaskCategory.CLASSIFICATION)


def test_router_provider_for_disabled_raises():
    router = ModelRouter()
    with pytest.raises(CostControlError) as exc:
        router.provider_for(TaskCategory.VISUAL_DESCRIPTION)
    assert "disabled" in str(exc.value)


def test_router_provider_for_unknown_kind_raises():
    router = ModelRouter()
    with pytest.raises(CostControlError) as exc:
        router.provider_for(TaskCategory.CLASSIFICATION)
    assert "no model provider registered" in str(exc.value)


def test_router_provider_for_returns_registered_provider():
    class _StubProvider:
        kind = DEFAULT_PROVIDER_KIND

        def complete(self, ctx, prompt, *, model=None, max_tokens=None, metadata=None):
            return None

    provider = _StubProvider()
    router = ModelRouter(providers={DEFAULT_PROVIDER_KIND: provider})
    assert router.provider_for(TaskCategory.CLASSIFICATION) is provider


def test_router_register_provider():
    router = ModelRouter()

    class _Provider:
        kind = DEFAULT_PROVIDER_KIND

        def complete(self, *_, **__): ...

    p = _Provider()
    router.register_provider(DEFAULT_PROVIDER_KIND, p)
    assert router.provider_for(TaskCategory.SUMMARIZATION) is p


def test_router_is_enabled():
    router = ModelRouter()
    assert router.is_enabled(TaskCategory.CLASSIFICATION) is True
    assert router.is_enabled(TaskCategory.VISUAL_DESCRIPTION) is False


def test_router_custom_mapping_overrides_defaults():
    custom = {
        TaskCategory.CLASSIFICATION: ModelSelection(
            provider_kind="my_provider",
            model_name="my-model",
            cost_per_input_token=Decimal("0.01"),
            cost_per_output_token=Decimal("0.02"),
        ),
    }
    router = ModelRouter(mapping=custom)
    selection = router.select(TaskCategory.CLASSIFICATION)
    assert selection.model_name == "my-model"
    assert selection.provider_kind == "my_provider"


def test_router_does_not_lock_to_one_provider():
    """Provider kind is just a string key — multiple providers coexist."""
    custom = {
        TaskCategory.CLASSIFICATION: ModelSelection(
            provider_kind="cheap_vendor", model_name="x"
        ),
        TaskCategory.SUMMARIZATION: ModelSelection(
            provider_kind="quality_vendor", model_name="y"
        ),
    }
    router = ModelRouter(mapping=custom)
    assert router.select(TaskCategory.CLASSIFICATION).provider_kind == "cheap_vendor"
    assert router.select(TaskCategory.SUMMARIZATION).provider_kind == "quality_vendor"


# ---- CostEstimator ----------------------------------------------------


def test_estimate_uses_chars_per_token():
    router = ModelRouter()
    estimator = CostEstimator(router)
    cost = estimator.estimate(
        TaskCategory.CLASSIFICATION,
        input_chars=4000,  # ≈ 1000 tokens
        expected_output_tokens=100,
    )
    expected = (
        Decimal(1000) * Decimal("0.0000008")
        + Decimal(100) * Decimal("0.000004")
    )
    assert cost == expected


def test_estimate_with_explicit_input_tokens():
    router = ModelRouter()
    estimator = CostEstimator(router)
    cost = estimator.estimate(
        TaskCategory.SUMMARIZATION,
        input_tokens=500,
        expected_output_tokens=200,
    )
    expected = (
        Decimal(500) * Decimal("0.000003")
        + Decimal(200) * Decimal("0.000015")
    )
    assert cost == expected


def test_estimate_defaults_to_max_output_tokens():
    """Without expected_output_tokens, the estimator uses selection.max_output_tokens
    so the worst-case bound is preserved."""
    router = ModelRouter()
    estimator = CostEstimator(router)
    selection = router.select(TaskCategory.CLASSIFICATION)
    cost = estimator.estimate(TaskCategory.CLASSIFICATION, input_tokens=100)
    expected_input = Decimal(100) * selection.cost_per_input_token
    expected_output = (
        Decimal(selection.max_output_tokens) * selection.cost_per_output_token
    )
    assert cost == expected_input + expected_output


def test_expensive_models_estimate_higher():
    router = ModelRouter()
    estimator = CostEstimator(router)
    cheap = estimator.estimate(TaskCategory.CLASSIFICATION, input_tokens=1000)
    expensive = estimator.estimate(TaskCategory.VISUAL_DESCRIPTION, input_tokens=1000)
    assert expensive > cheap


# ---- BudgetPolicy -----------------------------------------------------


def test_budget_policy_defaults_to_no_limits():
    policy = BudgetPolicy()
    for level in BudgetLevel:
        assert policy.limit_for(level) is None
    assert policy.has_any_limit() is False


def test_budget_policy_limit_for_each_level():
    policy = BudgetPolicy(
        tenant_amount=Decimal("100"),
        project_amount=Decimal("50"),
        document_amount=Decimal("10"),
        workflow_run_amount=Decimal("20"),
        query_amount=Decimal("5"),
    )
    assert policy.limit_for(BudgetLevel.TENANT) == Decimal("100")
    assert policy.limit_for(BudgetLevel.PROJECT) == Decimal("50")
    assert policy.limit_for(BudgetLevel.DOCUMENT) == Decimal("10")
    assert policy.limit_for(BudgetLevel.WORKFLOW_RUN) == Decimal("20")
    assert policy.limit_for(BudgetLevel.QUERY) == Decimal("5")
    assert policy.has_any_limit() is True


# ---- BudgetGuard ------------------------------------------------------


def test_guard_with_no_limits_returns_allow(ctx):
    guard = BudgetGuard(BudgetPolicy())
    check = guard.evaluate(ctx, current_spend={}, estimated=Decimal("100"))
    assert check.decision is BudgetDecision.ALLOW


def test_guard_under_budget_allows(ctx):
    policy = BudgetPolicy(project_amount=Decimal("10.00"))
    guard = BudgetGuard(policy)
    check = guard.evaluate(
        ctx,
        current_spend={BudgetLevel.PROJECT: Decimal("1.00")},
        estimated=Decimal("0.50"),
    )
    assert check.decision is BudgetDecision.ALLOW


def test_guard_warns_at_threshold(ctx):
    policy = BudgetPolicy(
        project_amount=Decimal("10.00"), warn_threshold=0.8
    )
    guard = BudgetGuard(policy)
    check = guard.evaluate(
        ctx,
        current_spend={BudgetLevel.PROJECT: Decimal("7.50")},
        estimated=Decimal("1.00"),  # projected = 8.50, 85% of 10
    )
    assert check.decision is BudgetDecision.WARN
    assert check.level is BudgetLevel.PROJECT
    assert check.warnings


def test_guard_blocks_when_exceeded(ctx):
    policy = BudgetPolicy(project_amount=Decimal("10.00"))
    guard = BudgetGuard(policy)
    check = guard.evaluate(
        ctx,
        current_spend={BudgetLevel.PROJECT: Decimal("9.00")},
        estimated=Decimal("2.00"),  # projected = 11
    )
    assert check.decision is BudgetDecision.BLOCK
    assert check.level is BudgetLevel.PROJECT
    assert "exceeded" in check.message
    assert check.allowed is False


def test_guard_blocks_on_tightest_constraint(ctx):
    policy = BudgetPolicy(
        project_amount=Decimal("100.00"),
        query_amount=Decimal("0.50"),  # tighter
    )
    guard = BudgetGuard(policy)
    check = guard.evaluate(
        ctx,
        current_spend={
            BudgetLevel.PROJECT: Decimal("10.00"),
            BudgetLevel.QUERY: Decimal("0.40"),
        },
        estimated=Decimal("0.20"),  # under project, over query
    )
    assert check.decision is BudgetDecision.BLOCK
    assert check.level is BudgetLevel.QUERY


def test_guard_emits_audit_on_block(ctx, audit_recorder, workspace):
    import json

    from j1.audit.sink import AUDIT_LOG_FILENAME

    policy = BudgetPolicy(project_amount=Decimal("1.00"))
    guard = BudgetGuard(policy, audit=audit_recorder)
    guard.evaluate(
        ctx,
        current_spend={BudgetLevel.PROJECT: Decimal("0.90")},
        estimated=Decimal("0.50"),
        task=TaskCategory.SUMMARIZATION,
        correlation_id="run-1",
    )
    line = (workspace.audit(ctx) / AUDIT_LOG_FILENAME).read_text().splitlines()[-1]
    parsed = json.loads(line)
    assert parsed["action"] == ACTION_BUDGET_BLOCK
    assert parsed["payload"]["level"] == "project"
    assert parsed["payload"]["task"] == "summarization"
    assert parsed["correlation_id"] == "run-1"


def test_guard_emits_audit_on_warn(ctx, audit_recorder, workspace):
    import json

    from j1.audit.sink import AUDIT_LOG_FILENAME

    policy = BudgetPolicy(
        project_amount=Decimal("10.00"), warn_threshold=0.5
    )
    guard = BudgetGuard(policy, audit=audit_recorder)
    guard.evaluate(
        ctx,
        current_spend={BudgetLevel.PROJECT: Decimal("4.00")},
        estimated=Decimal("2.00"),
    )
    parsed = [
        json.loads(line)
        for line in (workspace.audit(ctx) / AUDIT_LOG_FILENAME).read_text().splitlines()
    ]
    actions = [p["action"] for p in parsed]
    assert ACTION_BUDGET_WARN in actions


def test_guard_does_not_emit_audit_when_recorder_missing(ctx):
    """Audit recorder is optional; guard works without it."""
    policy = BudgetPolicy(project_amount=Decimal("1.00"))
    guard = BudgetGuard(policy, audit=None)
    check = guard.evaluate(
        ctx,
        current_spend={BudgetLevel.PROJECT: Decimal("0.90")},
        estimated=Decimal("0.50"),
    )
    assert check.decision is BudgetDecision.BLOCK


def test_budget_check_allowed_property():
    assert BudgetCheck(decision=BudgetDecision.ALLOW, estimated=Decimal("0")).allowed
    assert BudgetCheck(decision=BudgetDecision.WARN, estimated=Decimal("0")).allowed
    assert not BudgetCheck(
        decision=BudgetDecision.BLOCK, estimated=Decimal("0")
    ).allowed


# ---- CostAggregator ---------------------------------------------------


def _record_cost(
    cost_recorder,
    ctx,
    *,
    amount: str,
    correlation_id: str | None = None,
    document_id: str | None = None,
    query_id: str | None = None,
    task_category: TaskCategory | None = None,
):
    metadata: dict[str, str] = {}
    if document_id:
        metadata["document_id"] = document_id
    if query_id:
        metadata["query_id"] = query_id
    if task_category:
        metadata["task_category"] = task_category.value
    breakdown = CostBreakdown(
        vendor="anthropic",
        model="claude-sonnet-4-6",
        unit_kind="input_tokens",
        units=1,
        amount=Decimal(amount),
        metadata=metadata,
    )
    cost_recorder.record(ctx, breakdown, correlation_id=correlation_id)


def test_aggregator_empty_log_returns_zero(workspace, ctx):
    agg = CostAggregator(workspace)
    assert agg.aggregate(ctx) == Decimal("0")


def test_aggregator_sums_total(workspace, ctx, cost_recorder):
    _record_cost(cost_recorder, ctx, amount="0.10")
    _record_cost(cost_recorder, ctx, amount="0.25")
    agg = CostAggregator(workspace)
    assert agg.aggregate(ctx) == Decimal("0.35")


def test_aggregator_filters_by_correlation_id(workspace, ctx, cost_recorder):
    _record_cost(cost_recorder, ctx, amount="0.10", correlation_id="run-1")
    _record_cost(cost_recorder, ctx, amount="0.20", correlation_id="run-2")
    _record_cost(cost_recorder, ctx, amount="0.05", correlation_id="run-1")
    agg = CostAggregator(workspace)
    assert agg.aggregate(ctx, correlation_id="run-1") == Decimal("0.15")
    assert agg.aggregate(ctx, correlation_id="run-2") == Decimal("0.20")


def test_aggregator_filters_by_document_id(workspace, ctx, cost_recorder):
    _record_cost(cost_recorder, ctx, amount="0.10", document_id="doc-A")
    _record_cost(cost_recorder, ctx, amount="0.20", document_id="doc-B")
    agg = CostAggregator(workspace)
    assert agg.aggregate(ctx, document_id="doc-A") == Decimal("0.10")


def test_aggregator_filters_by_query_id(workspace, ctx, cost_recorder):
    _record_cost(cost_recorder, ctx, amount="0.10", query_id="q-1")
    _record_cost(cost_recorder, ctx, amount="0.20", query_id="q-2")
    agg = CostAggregator(workspace)
    assert agg.aggregate(ctx, query_id="q-1") == Decimal("0.10")


def test_aggregator_filters_by_task_category(workspace, ctx, cost_recorder):
    _record_cost(
        cost_recorder, ctx, amount="0.10", task_category=TaskCategory.CLASSIFICATION
    )
    _record_cost(
        cost_recorder, ctx, amount="0.20", task_category=TaskCategory.SUMMARIZATION
    )
    agg = CostAggregator(workspace)
    assert (
        agg.aggregate(ctx, task_category=TaskCategory.CLASSIFICATION)
        == Decimal("0.10")
    )


def test_aggregator_by_levels_returns_per_level_spend(
    workspace, ctx, cost_recorder
):
    _record_cost(
        cost_recorder, ctx, amount="0.10",
        correlation_id="run-1", document_id="doc-A",
    )
    _record_cost(
        cost_recorder, ctx, amount="0.05",
        correlation_id="run-1", document_id="doc-A",
    )
    _record_cost(
        cost_recorder, ctx, amount="0.20",
        correlation_id="run-2", document_id="doc-B",
    )
    agg = CostAggregator(workspace)
    levels = agg.by_levels(
        ctx, correlation_id="run-1", document_id="doc-A"
    )
    assert levels[BudgetLevel.PROJECT] == Decimal("0.35")
    assert levels[BudgetLevel.WORKFLOW_RUN] == Decimal("0.15")
    # DOCUMENT is "all spend on this document" across runs — both records
    # have doc-A so the sum is 0.15.
    assert levels[BudgetLevel.DOCUMENT] == Decimal("0.15")


# ---- End-to-end: estimator → aggregator → guard ----------------------


def test_estimate_then_guard_blocks_when_projected_over(
    workspace, ctx, cost_recorder, audit_recorder
):
    """Realistic flow: record some past spend, estimate the next call,
    let the guard decide allow/warn/block."""
    # Past spend on this run.
    _record_cost(cost_recorder, ctx, amount="0.045", correlation_id="run-1")

    router = ModelRouter()
    estimator = CostEstimator(router)
    aggregator = CostAggregator(workspace)
    policy = BudgetPolicy(
        workflow_run_amount=Decimal("0.04"),  # tight workflow run budget
    )
    guard = BudgetGuard(policy, audit=audit_recorder)

    estimated = estimator.estimate(
        TaskCategory.SUMMARIZATION,
        input_tokens=500,
        expected_output_tokens=200,
    )
    spend = aggregator.by_levels(ctx, correlation_id="run-1")
    check = guard.evaluate(
        ctx,
        current_spend=spend,
        estimated=estimated,
        task=TaskCategory.SUMMARIZATION,
        correlation_id="run-1",
    )
    assert check.decision is BudgetDecision.BLOCK
    assert check.level is BudgetLevel.WORKFLOW_RUN
