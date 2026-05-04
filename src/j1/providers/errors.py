"""Provider-layer exceptions."""

from j1.errors.exceptions import J1Error


class ProviderUnavailable(J1Error):
    """Raised when an optional vendor library isn't installed.

    Mirrors `LLMProviderUnavailable` for the LLM layer. The error
    message MUST name the missing package and the recommended
    install command.
    """
