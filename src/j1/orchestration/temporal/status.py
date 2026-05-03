from temporalio.client import WorkflowExecutionStatus

from j1.jobs.status import ProcessingStatus

_MAPPING: dict[WorkflowExecutionStatus, ProcessingStatus] = {
    WorkflowExecutionStatus.RUNNING: ProcessingStatus.RUNNING,
    WorkflowExecutionStatus.CONTINUED_AS_NEW: ProcessingStatus.RUNNING,
    WorkflowExecutionStatus.COMPLETED: ProcessingStatus.SUCCEEDED,
    WorkflowExecutionStatus.FAILED: ProcessingStatus.FAILED,
    WorkflowExecutionStatus.TIMED_OUT: ProcessingStatus.FAILED,
    WorkflowExecutionStatus.CANCELED: ProcessingStatus.CANCELLED,
    WorkflowExecutionStatus.TERMINATED: ProcessingStatus.CANCELLED,
}


def map_workflow_status(status: WorkflowExecutionStatus) -> ProcessingStatus:
    try:
        return _MAPPING[status]
    except KeyError as exc:
        raise ValueError(f"unknown WorkflowExecutionStatus: {status}") from exc
