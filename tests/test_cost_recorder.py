import json
from decimal import Decimal

from j1.cost.sink import COST_LOG_FILENAME
from j1.processing.results import CostBreakdown, ResultStatus


def _breakdown(**overrides) -> CostBreakdown:
    base = dict(
        vendor="anthropic",
        model="claude-sonnet-4-6",
        unit_kind="input_tokens",
        units=1234,
        amount=Decimal("0.0123"),
    )
    base.update(overrides)
    return CostBreakdown(**base)


def test_record_writes_cost_event(cost_recorder, workspace, ctx):
    result = cost_recorder.record(ctx, _breakdown(), job_id="j1", correlation_id="c1")
    assert result.status is ResultStatus.SUCCEEDED
    assert result.event_id

    line = (workspace.audit(ctx) / COST_LOG_FILENAME).read_text().splitlines()[0]
    parsed = json.loads(line)
    assert parsed["event_id"] == result.event_id
    assert parsed["vendor"] == "anthropic"
    assert parsed["units"] == 1234
    assert parsed["amount"] == "0.0123"
    assert parsed["currency"] == "USD"
    assert parsed["job_id"] == "j1"
    assert parsed["correlation_id"] == "c1"


def test_record_without_job_or_correlation(cost_recorder, workspace, ctx):
    cost_recorder.record(ctx, _breakdown())
    parsed = json.loads(
        (workspace.audit(ctx) / COST_LOG_FILENAME).read_text().splitlines()[0]
    )
    assert parsed["job_id"] is None
    assert parsed["correlation_id"] is None


def test_multiple_events_appended(cost_recorder, workspace, ctx):
    cost_recorder.record(ctx, _breakdown(units=10))
    cost_recorder.record(ctx, _breakdown(units=20))
    lines = (workspace.audit(ctx) / COST_LOG_FILENAME).read_text().splitlines()
    assert [json.loads(line)["units"] for line in lines] == [10, 20]
