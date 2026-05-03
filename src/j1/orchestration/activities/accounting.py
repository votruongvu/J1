import json
from decimal import Decimal

from temporalio import activity

from j1.audit.recorder import AuditRecorder
from j1.cost.sink import COST_LOG_FILENAME
from j1.orchestration.activities.payloads import (
    CalculateCostInput,
    CalculateCostResult,
    WriteAuditInput,
    WriteAuditResult,
)
from j1.workspace.resolver import WorkspaceResolver

ACTIVITY_CALCULATE_COST = "j1.accounting.calculate_cost"
ACTIVITY_WRITE_AUDIT = "j1.accounting.write_audit"

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"


class AccountingActivities:
    def __init__(
        self,
        workspace: WorkspaceResolver,
        audit: AuditRecorder,
    ) -> None:
        self._workspace = workspace
        self._audit = audit

    def all_activities(self) -> list:
        return [self.calculate_cost_activity, self.write_audit_activity]

    @activity.defn(name=ACTIVITY_CALCULATE_COST)
    def calculate_cost_activity(
        self, input: CalculateCostInput
    ) -> CalculateCostResult:
        ctx = input.scope.to_context()
        path = self._workspace.audit(ctx) / COST_LOG_FILENAME
        if not path.exists():
            return CalculateCostResult(
                status=STATUS_SUCCEEDED,
                total_amount="0",
                currency="USD",
                event_count=0,
            )
        total = Decimal("0")
        currency = "USD"
        count = 0
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            total += Decimal(data["amount"])
            currency = data.get("currency", currency)
            count += 1
        return CalculateCostResult(
            status=STATUS_SUCCEEDED,
            total_amount=str(total),
            currency=currency,
            event_count=count,
        )

    @activity.defn(name=ACTIVITY_WRITE_AUDIT)
    def write_audit_activity(self, input: WriteAuditInput) -> WriteAuditResult:
        ctx = input.scope.to_context()
        event_id = self._audit.record(
            ctx,
            actor=input.actor,
            action=input.action,
            target_kind=input.target_kind,
            target_id=input.target_id,
            payload=dict(input.payload),
            correlation_id=input.correlation_id,
        )
        return WriteAuditResult(status=STATUS_SUCCEEDED, audit_event_id=event_id)
