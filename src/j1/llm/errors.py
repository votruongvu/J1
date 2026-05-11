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


class LLMContextOverflowError(LLMProviderUnavailable):
    """Raised by the LLM client BEFORE sending an HTTP request when
 the assembled prompt's estimated tokens would exceed the
 configured context window's available input budget.

 Subclasses `LLMProviderUnavailable` so existing error handling
 that catches "LLM is not usable for this call" (and surfaces
 a controlled error to the user instead of a workflow crash)
 catches this case too. Callers that want to branch on overflow
 specifically should test `isinstance(exc, LLMContextOverflowError)`
 and inspect `exc.diagnostic` for the budget arithmetic.
 """

    def __init__(self, message: str, *, diagnostic: dict | None = None) -> None:
        super().__init__(message)
        # `diagnostic` carries `estimatedInputTokens`,
        # `availableInputTokens`, `contextWindowTokens`,
        # `reservedOutputTokens`, `safetyMarginTokens`, `messageCount`,
        # `model`. Operators read this off the exception (or the log)
        # to know exactly which knob to turn.
        self.diagnostic = diagnostic or {}
