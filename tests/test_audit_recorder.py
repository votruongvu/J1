import json

from j1.audit.sink import AUDIT_LOG_FILENAME


def test_record_writes_event_and_returns_id(audit_recorder, workspace, ctx):
    event_id = audit_recorder.record(
        ctx,
        actor="system",
        action="thing.happened",
        target_kind="thing",
        target_id="t1",
        payload={"foo": 1},
    )
    assert event_id

    line = (workspace.audit(ctx) / AUDIT_LOG_FILENAME).read_text().splitlines()[0]
    parsed = json.loads(line)
    assert parsed["event_id"] == event_id
    assert parsed["action"] == "thing.happened"
    assert parsed["payload"] == {"foo": 1}
    assert parsed["project"]["tenant_id"] == "acme"


def test_record_without_payload_writes_empty_dict(audit_recorder, workspace, ctx):
    audit_recorder.record(
        ctx,
        actor="system",
        action="x",
        target_kind="thing",
        target_id="t",
    )
    line = (workspace.audit(ctx) / AUDIT_LOG_FILENAME).read_text().splitlines()[0]
    assert json.loads(line)["payload"] == {}


def test_correlation_id_round_trips(audit_recorder, workspace, ctx):
    audit_recorder.record(
        ctx,
        actor="system",
        action="x",
        target_kind="thing",
        target_id="t",
        correlation_id="run-9",
    )
    line = (workspace.audit(ctx) / AUDIT_LOG_FILENAME).read_text().splitlines()[0]
    assert json.loads(line)["correlation_id"] == "run-9"


def test_sequence_increments_per_correlation_id(
    audit_recorder, workspace, ctx,
):
    """Each ``record`` call against the same ``correlation_id`` gets a
    monotonically-increasing ``sequence`` so consumers can detect
    out-of-order delivery and reorder ties on ``occurred_at``."""
    for i in range(3):
        audit_recorder.record(
            ctx, actor="system", action=f"a-{i}",
            target_kind="thing", target_id="t",
            correlation_id="run-7",
        )
    audit_recorder.record(
        ctx, actor="system", action="other",
        target_kind="thing", target_id="t",
        correlation_id="run-other",
    )
    lines = (
        workspace.audit(ctx) / AUDIT_LOG_FILENAME
    ).read_text().splitlines()
    parsed = [json.loads(line) for line in lines]
    run7_events = [e for e in parsed if e["correlation_id"] == "run-7"]
    other_events = [e for e in parsed if e["correlation_id"] == "run-other"]
    assert [e["sequence"] for e in run7_events] == [1, 2, 3]
    # A separate correlation_id has its own counter starting at 1.
    assert other_events[0]["sequence"] == 1


def test_sequence_is_none_when_no_correlation_id(
    audit_recorder, workspace, ctx,
):
    """Unscoped events (no correlation_id) skip the sequencer — the
    feature only makes sense within a run-scoped event stream."""
    audit_recorder.record(
        ctx, actor="system", action="x",
        target_kind="thing", target_id="t",
    )
    line = (
        workspace.audit(ctx) / AUDIT_LOG_FILENAME
    ).read_text().splitlines()[0]
    assert json.loads(line)["sequence"] is None
