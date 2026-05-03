import json
from typing import Protocol

from j1._serialization import to_jsonable
from j1.audit.events import AuditEvent
from j1.workspace.resolver import WorkspaceResolver

AUDIT_LOG_FILENAME = "events.jsonl"


class AuditSink(Protocol):
    def write(self, event: AuditEvent) -> None: ...


class JsonlAuditSink:
    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    def write(self, event: AuditEvent) -> None:
        path = self._workspace.audit(event.project) / AUDIT_LOG_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(to_jsonable(event), separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")
