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
