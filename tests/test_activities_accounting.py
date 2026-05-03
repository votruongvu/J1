import json
from decimal import Decimal

from j1.audit.sink import AUDIT_LOG_FILENAME
from j1.cost.breakdown import CostBreakdown
from j1.orchestration.activities.accounting import (
    ACTIVITY_CALCULATE_COST,
    ACTIVITY_WRITE_AUDIT,
)
from j1.orchestration.activities.payloads import (
    CalculateCostInput,
    ProjectScope,
    WriteAuditInput,
)


def test_activity_names(accounting_activities):
    names = [
        a.__temporal_activity_definition.name
        for a in accounting_activities.all_activities()
    ]
    assert ACTIVITY_CALCULATE_COST in names
    assert ACTIVITY_WRITE_AUDIT in names


# calculate_cost


def test_calculate_cost_empty_log(accounting_activities, ctx):
    result = accounting_activities.calculate_cost_activity(
        CalculateCostInput(scope=ProjectScope.from_context(ctx))
    )
    assert result.status == "succeeded"
    assert result.total_amount == "0"
    assert result.event_count == 0


def test_calculate_cost_sums_log(accounting_activities, cost_recorder, ctx):
    cost_recorder.record(
        ctx,
        CostBreakdown(
            vendor="anthropic",
            model="m",
            unit_kind="input_tokens",
            units=1,
            amount=Decimal("0.10"),
        ),
    )
    cost_recorder.record(
        ctx,
        CostBreakdown(
            vendor="anthropic",
            model="m",
            unit_kind="input_tokens",
            units=1,
            amount=Decimal("0.20"),
        ),
    )
    result = accounting_activities.calculate_cost_activity(
        CalculateCostInput(scope=ProjectScope.from_context(ctx))
    )
    assert result.status == "succeeded"
    assert Decimal(result.total_amount) == Decimal("0.30")
    assert result.event_count == 2


# write_audit


def test_write_audit_writes_event(accounting_activities, workspace, ctx):
    result = accounting_activities.write_audit_activity(
        WriteAuditInput(
            scope=ProjectScope.from_context(ctx),
            actor="system",
            action="custom.event",
            target_kind="thing",
            target_id="t-1",
            payload={"k": "v"},
            correlation_id="run-7",
        )
    )
    assert result.status == "succeeded"
    assert result.audit_event_id

    line = (workspace.audit(ctx) / AUDIT_LOG_FILENAME).read_text().splitlines()[0]
    parsed = json.loads(line)
    assert parsed["action"] == "custom.event"
    assert parsed["target_id"] == "t-1"
    assert parsed["payload"] == {"k": "v"}
    assert parsed["correlation_id"] == "run-7"
    assert parsed["event_id"] == result.audit_event_id


def test_write_audit_handles_empty_payload(accounting_activities, workspace, ctx):
    accounting_activities.write_audit_activity(
        WriteAuditInput(
            scope=ProjectScope.from_context(ctx),
            actor="system",
            action="x",
            target_kind="t",
            target_id="t",
        )
    )
    parsed = json.loads(
        (workspace.audit(ctx) / AUDIT_LOG_FILENAME).read_text().splitlines()[0]
    )
    assert parsed["payload"] == {}
