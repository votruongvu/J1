"""Provider-layer exceptions."""

from j1.errors.exceptions import J1Error


class ProviderUnavailable(J1Error):
    """Raised when an optional vendor library isn't installed.

 Mirrors `LLMProviderUnavailable` for the LLM layer. The error
 message MUST name the missing package and the recommended
 install command.
 """


class WorkspaceScopeMissing(J1Error):
    """Raised on the query path when neither a snapshot_id nor a safe
    explicit workspace override is supplied to a snapshot-scoped
    provider. Distinct from generic provider failure so the query
    route layer can surface "scope was not resolved" as a first-class
    diagnostic rather than collapsing it to a vague FAILED result.

    The active query flow MUST resolve eligible snapshot ids before
    calling the provider — this error indicates the caller forgot to,
    or that eligibility resolution returned empty and the caller
    proceeded anyway. The fix is always at the caller (orchestrator /
    adapter), never to widen provider scope.
    """
