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


class DocumentNotFoundError(J1Error):
    pass
