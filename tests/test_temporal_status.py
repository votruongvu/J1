import pytest
from temporalio.client import WorkflowExecutionStatus

from j1.jobs.status import ProcessingStatus
from j1.orchestration.temporal.status import map_workflow_status


@pytest.mark.parametrize(
    "temporal_status,expected",
    [
        (WorkflowExecutionStatus.RUNNING, ProcessingStatus.RUNNING),
        (WorkflowExecutionStatus.CONTINUED_AS_NEW, ProcessingStatus.RUNNING),
        (WorkflowExecutionStatus.COMPLETED, ProcessingStatus.SUCCEEDED),
        (WorkflowExecutionStatus.FAILED, ProcessingStatus.FAILED),
        (WorkflowExecutionStatus.TIMED_OUT, ProcessingStatus.FAILED),
        (WorkflowExecutionStatus.CANCELED, ProcessingStatus.CANCELLED),
        (WorkflowExecutionStatus.TERMINATED, ProcessingStatus.CANCELLED),
    ],
)
def test_status_mapping(temporal_status, expected):
    assert map_workflow_status(temporal_status) is expected


def test_every_temporal_status_is_mapped():
    for status in WorkflowExecutionStatus:
        result = map_workflow_status(status)
        assert isinstance(result, ProcessingStatus)
