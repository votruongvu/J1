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
