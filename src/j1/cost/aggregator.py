import json
from decimal import Decimal

from j1.cost.budget import BudgetLevel
from j1.cost.router import TaskCategory
from j1.cost.sink import COST_LOG_FILENAME
from j1.projects.context import ProjectContext
from j1.workspace.resolver import WorkspaceResolver


class CostAggregator:
    """Reads `audit/costs.jsonl` and sums spend with optional filters.

    Filtering convention: callers populate `CostBreakdown.metadata` with
    `task_category`, `document_id`, `query_id` etc. when recording cost.
    The aggregator looks for those keys in the persisted event metadata.

    Note: the aggregator is scoped to a single `ProjectContext`. True
    cross-project tenant aggregation needs an external pass over multiple
    project logs and is not implemented here — `by_levels` returns the
    project total for the `TENANT` level.
    """

    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    def aggregate(
        self,
        ctx: ProjectContext,
        *,
        correlation_id: str | None = None,
        document_id: str | None = None,
        query_id: str | None = None,
        task_category: TaskCategory | None = None,
    ) -> Decimal:
        path = self._workspace.audit(ctx) / COST_LOG_FILENAME
        if not path.exists():
            return Decimal("0")
        total = Decimal("0")
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                correlation_id is not None
                and data.get("correlation_id") != correlation_id
            ):
                continue
            metadata = data.get("metadata") or {}
            if document_id is not None and metadata.get("document_id") != document_id:
                continue
            if query_id is not None and metadata.get("query_id") != query_id:
                continue
            if (
                task_category is not None
                and metadata.get("task_category") != task_category.value
            ):
                continue
            total += Decimal(str(data.get("amount", "0")))
        return total

    def by_levels(
        self,
        ctx: ProjectContext,
        *,
        correlation_id: str | None = None,
        document_id: str | None = None,
        query_id: str | None = None,
    ) -> dict[BudgetLevel, Decimal]:
        project_total = self.aggregate(ctx)
        result: dict[BudgetLevel, Decimal] = {
            # Tenant-wide aggregation across projects is out of scope here;
            # the tenant level effectively equals the project total for now.
            BudgetLevel.TENANT: project_total,
            BudgetLevel.PROJECT: project_total,
        }
        if correlation_id is not None:
            result[BudgetLevel.WORKFLOW_RUN] = self.aggregate(
                ctx, correlation_id=correlation_id
            )
        if document_id is not None:
            result[BudgetLevel.DOCUMENT] = self.aggregate(
                ctx, document_id=document_id
            )
        if query_id is not None:
            result[BudgetLevel.QUERY] = self.aggregate(ctx, query_id=query_id)
        return result
