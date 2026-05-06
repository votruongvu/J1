from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from j1.orchestration.activities.payloads import (
        ArtifactActivityResult,
        CompileActivityInput,
        EnrichActivityInput,
        IndexActivityInput,
        ProcessingActivityResult,
        ProjectScope,
    )
    from j1.orchestration.activities.processing import ProcessingActivities
    from j1.orchestration.errors import ERROR_TYPE_REQUIRED_STEP_FAILED
    from j1.orchestration.temporal.retries import COMPILE_RETRY, DEFAULT_RETRY

DEFAULT_ACTIVITY_TIMEOUT = timedelta(minutes=10)
# Compile is the most expensive activity (MinerU + raganything routinely
# parse for many minutes per real PDF). The mirror values in
# `project_processing.py` are the source of truth — kept in sync here so
# `DocumentProcessingWorkflow` cannot regress to the "10-minute timeout
# fires mid-parse → Temporal retries → fresh MinerU subprocess each time"
# failure mode. The activity's `_heartbeating` ticker keeps liveness
# alive within HEARTBEAT_TIMEOUT; COMPILE_ACTIVITY_TIMEOUT is the upper
# bound on a single attempt.
COMPILE_ACTIVITY_TIMEOUT = timedelta(hours=1)
HEARTBEAT_TIMEOUT = timedelta(minutes=5)


@dataclass(frozen=True)
class DocumentProcessingRequest:
    scope: ProjectScope
    document_id: str
    compiler_kind: str
    enricher_kind: str | None = None
    indexer_kind: str | None = None
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class DocumentProcessingResult:
    status: str
    document_id: str
    artifact_ids: list[str] = field(default_factory=list)
    error: str | None = None


@workflow.defn
class DocumentProcessingWorkflow:
    @workflow.run
    async def run(
        self, request: DocumentProcessingRequest
    ) -> DocumentProcessingResult:
        retry = DEFAULT_RETRY.to_temporal()

        compile_result: ArtifactActivityResult = await workflow.execute_activity_method(
            ProcessingActivities.compile,
            CompileActivityInput(
                scope=request.scope,
                document_id=request.document_id,
                processor_kind=request.compiler_kind,
                actor=request.actor,
                correlation_id=request.correlation_id,
            ),
            # Compile gets compile-specific knobs even in the slim
            # workflow: a 1-hour upper bound, a 5-minute heartbeat
            # liveness check (paired with the activity's 30-second
            # heartbeat ticker), and the bounded `COMPILE_RETRY`
            # (2 attempts) policy. Without these, real PDFs would
            # exceed the 10-minute default, time out mid-parse, and
            # Temporal would retry up to 5× — re-spawning MinerU
            # for the same document on every retry. See
            # `project_processing.py` for the same configuration.
            start_to_close_timeout=COMPILE_ACTIVITY_TIMEOUT,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=COMPILE_RETRY.to_temporal(),
        )

        if compile_result.status != "succeeded":
            # Earlier versions returned a result with `status="failed"`,
            # leaving Temporal UI showing "Completed" for a workflow
            # whose required compile step failed. Raise instead so
            # Temporal sees the workflow as Failed. The error string
            # carries the original message so operators / status
            # queries can still surface the cause.
            raise ApplicationError(
                f"compile failed for document {request.document_id}: "
                f"{compile_result.error or 'unspecified'}",
                type=ERROR_TYPE_REQUIRED_STEP_FAILED,
                non_retryable=True,
            )

        produced_ids: list[str] = list(compile_result.artifact_ids)

        if request.enricher_kind:
            enriched_ids: list[str] = []
            for artifact_id in compile_result.artifact_ids:
                enrich_result: ArtifactActivityResult = (
                    await workflow.execute_activity_method(
                        ProcessingActivities.enrich,
                        EnrichActivityInput(
                            scope=request.scope,
                            artifact_id=artifact_id,
                            processor_kind=request.enricher_kind,
                            actor=request.actor,
                            correlation_id=request.correlation_id,
                        ),
                        start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
                        retry_policy=retry,
                    )
                )
                if enrich_result.status != "succeeded":
                    # Caller explicitly opted into enrichment via
                    # `enricher_kind`; treat its failure as a
                    # workflow-level failure (raise → Temporal Failed).
                    raise ApplicationError(
                        f"enrich failed for artifact {artifact_id}: "
                        f"{enrich_result.error or 'unspecified'}",
                        type=ERROR_TYPE_REQUIRED_STEP_FAILED,
                        non_retryable=True,
                    )
                enriched_ids.extend(enrich_result.artifact_ids)
            produced_ids = produced_ids + enriched_ids

        if request.indexer_kind:
            index_result: ProcessingActivityResult = (
                await workflow.execute_activity_method(
                    ProcessingActivities.index,
                    IndexActivityInput(
                        scope=request.scope,
                        artifact_ids=produced_ids,
                        processor_kind=request.indexer_kind,
                        actor=request.actor,
                        correlation_id=request.correlation_id,
                    ),
                    start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
                    retry_policy=retry,
                )
            )
            if index_result.status != "succeeded":
                raise ApplicationError(
                    f"index failed for document {request.document_id}: "
                    f"{index_result.error or 'unspecified'}",
                    type=ERROR_TYPE_REQUIRED_STEP_FAILED,
                    non_retryable=True,
                )

        return DocumentProcessingResult(
            status="succeeded",
            document_id=request.document_id,
            artifact_ids=produced_ids,
        )
