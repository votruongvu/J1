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
    ManualTestQueryRequest,
    ManualTestQueryResponseDTO,
    RetrievedChunkRefDTO,
    ValidationCheckDTO,
    ValidationCitationDTO,
    ValidationStatus,
)
from j1.validation.service import IngestionValidationService

__all__ = [
    "IngestionValidationService",
    "ManualTestQueryRequest",
    "ManualTestQueryResponseDTO",
    "RetrievedChunkRefDTO",
    "ValidationCheckDTO",
    "ValidationCitationDTO",
    "ValidationStatus",
]
