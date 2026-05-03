from j1.errors.exceptions import J1Error


class AuthenticationError(J1Error):
    """Raised when a credential is missing, malformed, or unrecognised."""


class AuthorizationError(J1Error):
    """Raised when an authenticated subject lacks a required scope."""

    def __init__(self, message: str, *, required_scope: str | None = None) -> None:
        super().__init__(message)
        self.required_scope = required_scope
