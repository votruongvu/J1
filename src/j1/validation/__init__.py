"""Post-ingestion validation surface.

After the 2026-05-14 product decision, this package exposes two
flows:

* **Manual Test Query** — synchronous single-question lookup against
  a specific run. Detailed inspection tool inside the Validation Tab.
* **Imported Test Cases** — CSV-imported questions executed against
  the document's latest succeeded run. Compact summary inside the
  Validation Tab. No generation, no judge, no draft lifecycle.

Audit events are written via the existing audit recorder; the
imported-set store keeps one set + one execution snapshot per
document on disk.
"""

from j1.validation.dtos import (
    EvidenceBlockDTO,
    LLMTraceDTO,
    ManualTestQueryRequest,
    ManualTestQueryResponseDTO,
    NativeDebugQueryResponseDTO,
    RetrievedChunkRefDTO,
    ValidationCheckDTO,
    ValidationCitationDTO,
    ValidationStatus,
)
from j1.validation.imported_test_cases import (
    CSVImportError,
    ImportedTestCase,
    ImportedTestCaseExecution,
    ImportedTestCaseExecutor,
    ImportedTestCaseResult,
    ImportedTestCaseSet,
    ImportedTestCaseStatus,
    ImportedTestCaseStore,
    ImportedTestCaseSummary,
    JsonlImportedTestCaseStore,
    OverallStatus,
    compute_summary,
    parse_csv_bytes,
)
from j1.validation.service import IngestionValidationService

__all__ = [
    "CSVImportError",
    "EvidenceBlockDTO",
    "ImportedTestCase",
    "ImportedTestCaseExecution",
    "ImportedTestCaseExecutor",
    "ImportedTestCaseResult",
    "ImportedTestCaseSet",
    "ImportedTestCaseStatus",
    "ImportedTestCaseStore",
    "ImportedTestCaseSummary",
    "IngestionValidationService",
    "JsonlImportedTestCaseStore",
    "LLMTraceDTO",
    "ManualTestQueryRequest",
    "ManualTestQueryResponseDTO",
    "NativeDebugQueryResponseDTO",
    "OverallStatus",
    "RetrievedChunkRefDTO",
    "ValidationCheckDTO",
    "ValidationCitationDTO",
    "ValidationStatus",
    "compute_summary",
    "parse_csv_bytes",
]
