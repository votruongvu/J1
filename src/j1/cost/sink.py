import json
from typing import Protocol

from j1._serialization import to_jsonable
from j1.cost.events import CostEvent
from j1.workspace.resolver import WorkspaceResolver

COST_LOG_FILENAME = "costs.jsonl"


class CostSink(Protocol):
    def write(self, event: CostEvent) -> None: ...


class JsonlCostSink:
    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    def write(self, event: CostEvent) -> None:
        path = self._workspace.audit(event.project) / COST_LOG_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(to_jsonable(event), separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")
