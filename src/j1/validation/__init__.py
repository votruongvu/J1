"""Post-ingestion validation surface.

Phase 1 ships a single capability: a tester runs a manual question
against a completed ingestion run, gets back the answer + retrieved
chunks + citations + deterministic check results scoped to that run.

Public exports:
  - `IngestionValidationService` — the service the REST handler calls.
  - DTOs that cross the REST boundary (`ManualTestQueryRequest`,
    `ManualTestQueryResponseDTO`, `ValidationCheckDTO`,
    `RetrievedChunkRefDTO`).
  - `ValidationStatus` literal alias.

Phase 1 has no persistence — every manual query is stateless. Audit
events are written via the existing audit recorder; no validation-
specific store yet.
"""

from j1.validation.dtos import (
    ExecutionStatus,
    ExpectedBehavior,
    ManualTestQueryRequest,
    ManualTestQueryResponseDTO,
    RetrievedChunkRefDTO,
    ValidationCheckDTO,
    ValidationCitationDTO,
    ValidationCoverageDTO,
    ValidationPriority,
    ValidationResultDTO,
    ValidationRunDTO,
    ValidationSetDTO,
    ValidationSetSource,
    ValidationSetStatus,
    ValidationStatus,
    ValidationSummaryDTO,
    ValidationTestCaseDTO,
    ValidationTestType,
)
from j1.validation.generator import (
    DefaultTestCaseGenerator,
    GENERATOR_VERSION,
    GenerationOptions,
)
from j1.validation.runner import (
    DefaultValidationRunner,
    MAX_CASES_PER_RUN,
)
from j1.validation.service import IngestionValidationService
from j1.validation.store import (
    JsonlValidationRunStore,
    JsonlValidationSetStore,
    ValidationRunStore,
    ValidationSetStore,
)

__all__ = [
    "DefaultTestCaseGenerator",
    "DefaultValidationRunner",
    "ExecutionStatus",
    "ExpectedBehavior",
    "GENERATOR_VERSION",
    "GenerationOptions",
    "IngestionValidationService",
    "JsonlValidationRunStore",
    "JsonlValidationSetStore",
    "MAX_CASES_PER_RUN",
    "ManualTestQueryRequest",
    "ManualTestQueryResponseDTO",
    "RetrievedChunkRefDTO",
    "ValidationCheckDTO",
    "ValidationCitationDTO",
    "ValidationCoverageDTO",
    "ValidationPriority",
    "ValidationResultDTO",
    "ValidationRunDTO",
    "ValidationRunStore",
    "ValidationSetDTO",
    "ValidationSetSource",
    "ValidationSetStatus",
    "ValidationSetStore",
    "ValidationStatus",
    "ValidationSummaryDTO",
    "ValidationTestCaseDTO",
    "ValidationTestType",
]
