class J1Error(Exception):
    """Base error for the J1 framework."""


class ConfigError(J1Error):
    pass


class WorkspaceError(J1Error):
    pass


class InvalidIdentifierError(WorkspaceError):
    pass


class PathTraversalError(WorkspaceError):
    pass


class WorkspaceLockedError(WorkspaceError):
    def __init__(
        self,
        message: str,
        *,
        owner: str | None = None,
        area: str | None = None,
    ) -> None:
        super().__init__(message)
        self.owner = owner
        self.area = area


class ChecksumMismatchError(J1Error):
    def __init__(
        self,
        message: str,
        *,
        expected: str | None = None,
        actual: str | None = None,
    ) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual


class IntakeError(J1Error):
    pass


class DuplicateDocumentError(IntakeError):
    def __init__(
        self,
        message: str,
        *,
        existing_document_id: str,
        checksum: str,
    ) -> None:
        super().__init__(message)
        self.existing_document_id = existing_document_id
        self.checksum = checksum


class UploadTooLargeError(IntakeError):
    """Raised when an upload exceeds the configured size cap.

    Carries the observed size + the cap so the REST adapter can
    surface a 413 response with actionable diagnostics.
    """

    def __init__(
        self,
        message: str,
        *,
        size_bytes: int,
        max_bytes: int,
    ) -> None:
        super().__init__(message)
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes


class DocumentNotFoundError(J1Error):
    pass


class CompilerConfigError(J1Error):
    pass


class CompilerExecutionError(J1Error):
    pass


class GraphConfigError(J1Error):
    pass


class GraphExecutionError(J1Error):
    pass


class SearchIndexerError(J1Error):
    pass


class QueryRoutingError(J1Error):
    pass


class CostControlError(J1Error):
    pass


class ProfileError(J1Error):
    pass


class ProfileNotFoundError(ProfileError):
    pass


class ProfileLoadError(ProfileError):
    pass
