import json
from datetime import datetime, timezone

from j1.audit.events import AuditEvent
from j1.audit.sink import AUDIT_LOG_FILENAME


def _event(ctx, *, action="something.happened", target_id="t1") -> AuditEvent:
    return AuditEvent(
        event_id="e1",
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        project=ctx,
        actor="system",
        action=action,
        target_kind="thing",
        target_id=target_id,
        payload={"k": "v"},
    )


def test_writes_jsonl_under_audit_area(audit_sink, workspace, ctx):
    audit_sink.write(_event(ctx))
    path = workspace.audit(ctx) / AUDIT_LOG_FILENAME
    assert path.is_file()
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["action"] == "something.happened"
    assert parsed["project"]["tenant_id"] == "acme"
    assert parsed["payload"] == {"k": "v"}


def test_appends_multiple_events(audit_sink, workspace, ctx):
    audit_sink.write(_event(ctx, action="a", target_id="1"))
    audit_sink.write(_event(ctx, action="b", target_id="2"))
    path = workspace.audit(ctx) / AUDIT_LOG_FILENAME
    actions = [json.loads(line)["action"] for line in path.read_text().splitlines()]
    assert actions == ["a", "b"]


def test_isolates_projects(audit_sink, workspace, ctx, other_ctx):
    audit_sink.write(_event(ctx, action="a"))
    audit_sink.write(_event(other_ctx, action="b"))
    a = (workspace.audit(ctx) / AUDIT_LOG_FILENAME).read_text().splitlines()
    b = (workspace.audit(other_ctx) / AUDIT_LOG_FILENAME).read_text().splitlines()
    assert json.loads(a[0])["action"] == "a"
    assert json.loads(b[0])["action"] == "b"
