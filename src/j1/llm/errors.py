"""LLM-layer exceptions.

Rooted in the framework's existing `J1Error` so callers can catch
"any J1 problem" with one import.
"""

from j1.errors.exceptions import ConfigError, J1Error


class LLMError(J1Error):
    """Base for every LLM-layer error."""


class LLMConfigError(ConfigError):
    """Raised when LLM-related env / settings are missing or invalid.

    Subclass of `ConfigError` so existing config-error handling at the
    framework boundary catches it without an extra clause.
    """


class LLMProviderUnavailable(LLMError):
    """Raised when an optional provider library isn't installed.

    Example: a `LangChainTextLLMClient` is constructed but the
    `langchain-core` package isn't on `sys.path`. The error message
    SHOULD name the missing import and the recommended fix
    (e.g. `pip install langchain-core`).
    """


class LLMRoleNotRegistered(LLMError):
    """Raised by `LLMProviderRegistry.resolve(role)` when the role is empty."""

    def __init__(self, role: str, *, registered: tuple[str, ...]) -> None:
        super().__init__(
            f"no LLM client registered for role {role!r}; "
            f"registered roles: {registered or '(none)'}"
        )
        self.role = role
        self.registered = registered
