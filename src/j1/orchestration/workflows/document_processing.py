from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow

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
    from j1.orchestration.temporal.retries import DEFAULT_RETRY

DEFAULT_ACTIVITY_TIMEOUT = timedelta(minutes=10)


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
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=retry,
        )

        if compile_result.status != "succeeded":
            return DocumentProcessingResult(
                status=compile_result.status,
                document_id=request.document_id,
                artifact_ids=list(compile_result.artifact_ids),
                error=compile_result.error,
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
                    return DocumentProcessingResult(
                        status=enrich_result.status,
                        document_id=request.document_id,
                        artifact_ids=produced_ids,
                        error=enrich_result.error,
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
                return DocumentProcessingResult(
                    status=index_result.status,
                    document_id=request.document_id,
                    artifact_ids=produced_ids,
                    error=index_result.error,
                )

        return DocumentProcessingResult(
            status="succeeded",
            document_id=request.document_id,
            artifact_ids=produced_ids,
        )
