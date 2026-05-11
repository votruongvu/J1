from dataclasses import dataclass, field

# Validation / processing error codes used in BulkImportFailureRecord.
ERROR_CODE_INVALID_JSON = "INVALID_JSON"
ERROR_CODE_SCHEMA = "SCHEMA_VALIDATION_FAILED"
ERROR_CODE_PROJECT_MISMATCH = "PROJECT_MISMATCH"
ERROR_CODE_DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
ERROR_CODE_INTEGRITY_MISMATCH = "INTEGRITY_MISMATCH"
ERROR_CODE_UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class BulkImportFailureRecord:
    """One row of a partial-failure report.

 `line_number` is 1-based to match the way operators read NDJSON.
 `record_id` is whatever identifier the row tried to declare (a
 `documentId`, `sourceId`, etc.) — `None` when the row was unparseable
 before identity could be extracted.
 """

    line_number: int
    record_id: str | None
    code: str
    message: str


@dataclass(frozen=True)
class BulkImportResult:
    """Outcome of a bulk import call.

 `succeeded` counts rows that resulted in new state.
 `skipped_idempotent` counts rows that matched an existing record by
 its idempotency key (e.g. document checksum) — those are not failures.
 """

    succeeded: int = 0
    skipped_idempotent: int = 0
    failures: list[BulkImportFailureRecord] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.succeeded + self.skipped_idempotent + len(self.failures)

    @property
    def has_failures(self) -> bool:
        return bool(self.failures)
