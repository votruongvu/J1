from enum import StrEnum


class ResultStatus(StrEnum):
    """Status of a single processor invocation (compile / enrich / graph /
 index / query). Returned inside `ArtifactProcessingResult` /
 `ProcessingResult` / `QueryResult` to signal whether the underlying
 adapter call produced output, was a no-op, or errored. Distinct from
 `StepStatus` (which describes a workflow stage from the workflow's
 perspective) and `FinalStatus` (which describes the workflow's
 overall outcome)."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class StepStatus(StrEnum):
    """Status of a workflow stage from the workflow's point of view.

 A workflow tracks one of these per planned stage (compile / enrich /
 graph / index, plus profile / plan when adaptive planning is on).
 Distinct from `ResultStatus` because the workflow knows things the
 adapter doesn't — e.g. that a stage was deliberately skipped by
 plan / policy / caller, with a recorded reason."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


class StepSource(StrEnum):
    """What drove the inclusion / exclusion / requirement of a step.

 Surfaced in audit + status output so operators can answer "why
 did this stage run / not run?" without reading code:
 * `caller` — explicit kind on the ingest request (highest priority)
 * `planner` — adaptive planner decision
 * `policy` — global / per-job ingest policy override
 * `default` — capability default (no caller, no planner)
 * `config` — operator-set deployment config (e.g. enrichment disabled)
 """

    CALLER = "caller"
    PLANNER = "planner"
    POLICY = "policy"
    DEFAULT = "default"
    CONFIG = "config"


class FinalStatus(StrEnum):
    """Workflow-level outcome reported back to operators / Temporal UI.

 The semantic guarantee:
 * `COMPLETED` is reported ONLY when every required enabled step
 completed successfully. A workflow that internally failed a
 required step but caught the exception MUST NOT report
 `COMPLETED` — that's the false-success bug this enum exists
 to make impossible to express.
 * `PARTIAL_COMPLETED` is reserved for the case where every
 required step succeeded AND at least one optional step failed
 AND the configured failure policy allows continuation.
 * `FAILED` is reported when any required step failed under a
 policy that does not allow continuation.

 Distinct from `StepStatus` (per-stage) and `ResultStatus`
 (per-adapter-call)."""

    COMPLETED = "completed"
    PARTIAL_COMPLETED = "partial_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class FailurePolicy(StrEnum):
    """How the workflow reacts to a step failure.

 `fail_fast` (default): any failure of any enabled step fails the
 workflow. Required vs. optional makes no difference.

 `continue_optional`: required-step failures fail the workflow;
 optional-step failures are recorded and the workflow continues to
 `PARTIAL_COMPLETED`. Optional steps that were skipped by plan /
 policy never count as failures.

 `best_effort`: even required-step failures are tolerated when
 later required steps can still be attempted. Final status is
 `PARTIAL_COMPLETED` if any required step failed but at least one
 succeeded; `FAILED` only if every required step failed. Use with
 care — `best_effort` is a deliberate "give me whatever you can"
 setting, not a default."""

    FAIL_FAST = "fail_fast"
    CONTINUE_OPTIONAL = "continue_optional"
    BEST_EFFORT = "best_effort"
