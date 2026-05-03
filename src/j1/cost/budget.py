from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from j1.audit.recorder import AuditRecorder
from j1.cost.router import TaskCategory
from j1.projects.context import ProjectContext


class BudgetLevel(StrEnum):
    TENANT = "tenant"
    PROJECT = "project"
    DOCUMENT = "document"
    WORKFLOW_RUN = "workflow_run"
    QUERY = "query"


class BudgetDecision(StrEnum):
    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"


# Order in which levels are evaluated. Tightest scope last so a tighter
# limit (e.g. per-query) blocks before a broader one (per-tenant) — this
# matches the user expectation that the most specific budget wins.
_LEVEL_ORDER: tuple[BudgetLevel, ...] = (
    BudgetLevel.TENANT,
    BudgetLevel.PROJECT,
    BudgetLevel.WORKFLOW_RUN,
    BudgetLevel.DOCUMENT,
    BudgetLevel.QUERY,
)


@dataclass(frozen=True)
class BudgetPolicy:
    tenant_amount: Decimal | None = None
    project_amount: Decimal | None = None
    document_amount: Decimal | None = None
    workflow_run_amount: Decimal | None = None
    query_amount: Decimal | None = None
    currency: str = "USD"
    warn_threshold: float = 0.8

    def limit_for(self, level: BudgetLevel) -> Decimal | None:
        return getattr(self, f"{level.value}_amount", None)

    def has_any_limit(self) -> bool:
        return any(self.limit_for(level) is not None for level in BudgetLevel)


@dataclass(frozen=True)
class BudgetCheck:
    decision: BudgetDecision
    estimated: Decimal
    level: BudgetLevel | None = None
    current: Decimal = Decimal("0")
    limit: Decimal | None = None
    message: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.decision != BudgetDecision.BLOCK


ACTION_BUDGET_ALLOW = "j1.budget.allow"
ACTION_BUDGET_WARN = "j1.budget.warn"
ACTION_BUDGET_BLOCK = "j1.budget.block"
TARGET_PROJECT = "project"


class BudgetGuard:
    """Pre-flight guard that decides whether an expensive activity may run.

    Inputs:
      * `current_spend`: per-level spend (typically computed by `CostAggregator`)
      * `estimated`: pre-flight cost estimate (typically from `CostEstimator`)

    Output: a `BudgetCheck` whose `decision` is one of:
      * `ALLOW` — under all configured limits
      * `WARN` — projected spend crosses `warn_threshold` on at least one
        configured level (caller may continue, but the warning is audited)
      * `BLOCK` — projected spend exceeds at least one configured limit
        (caller should pause / wait for approval / abort)
    """

    def __init__(
        self,
        policy: BudgetPolicy,
        *,
        audit: AuditRecorder | None = None,
    ) -> None:
        self._policy = policy
        self._audit = audit

    def evaluate(
        self,
        ctx: ProjectContext,
        *,
        current_spend: Mapping[BudgetLevel, Decimal],
        estimated: Decimal,
        task: TaskCategory | None = None,
        correlation_id: str | None = None,
    ) -> BudgetCheck:
        if not self._policy.has_any_limit():
            return BudgetCheck(
                decision=BudgetDecision.ALLOW,
                estimated=estimated,
                message="no budget configured",
            )

        warnings_list: list[str] = []
        warn_levels: list[BudgetLevel] = []
        warn_thresh = Decimal(str(self._policy.warn_threshold))

        for level in _LEVEL_ORDER:
            limit = self._policy.limit_for(level)
            if limit is None:
                continue
            current = current_spend.get(level, Decimal("0"))
            projected = current + estimated

            if projected > limit:
                check = BudgetCheck(
                    decision=BudgetDecision.BLOCK,
                    estimated=estimated,
                    level=level,
                    current=current,
                    limit=limit,
                    message=(
                        f"{level.value} budget would be exceeded: "
                        f"projected={projected} {self._policy.currency} "
                        f"limit={limit} {self._policy.currency}"
                    ),
                    warnings=warnings_list,
                )
                self._emit(ctx, check, task, correlation_id)
                return check

            if projected >= limit * warn_thresh:
                pct = (projected / limit) * Decimal(100)
                warnings_list.append(
                    f"{level.value} budget at {pct:.1f}% "
                    f"({projected}/{limit} {self._policy.currency})"
                )
                warn_levels.append(level)

        if warn_levels:
            level = warn_levels[0]
            limit = self._policy.limit_for(level)
            current = current_spend.get(level, Decimal("0"))
            check = BudgetCheck(
                decision=BudgetDecision.WARN,
                estimated=estimated,
                level=level,
                current=current,
                limit=limit,
                message=warnings_list[0],
                warnings=warnings_list,
            )
            self._emit(ctx, check, task, correlation_id)
            return check

        return BudgetCheck(
            decision=BudgetDecision.ALLOW,
            estimated=estimated,
            message="under all budgets",
        )

    def _emit(
        self,
        ctx: ProjectContext,
        check: BudgetCheck,
        task: TaskCategory | None,
        correlation_id: str | None,
    ) -> None:
        if self._audit is None:
            return
        action = {
            BudgetDecision.ALLOW: ACTION_BUDGET_ALLOW,
            BudgetDecision.WARN: ACTION_BUDGET_WARN,
            BudgetDecision.BLOCK: ACTION_BUDGET_BLOCK,
        }[check.decision]
        self._audit.record(
            ctx,
            actor="system",
            action=action,
            target_kind=TARGET_PROJECT,
            target_id=ctx.project_id,
            correlation_id=correlation_id,
            payload={
                "decision": check.decision.value,
                "level": check.level.value if check.level else None,
                "estimated": str(check.estimated),
                "current": str(check.current),
                "limit": str(check.limit) if check.limit is not None else None,
                "currency": self._policy.currency,
                "task": task.value if task else None,
                "warnings": list(check.warnings),
            },
        )
