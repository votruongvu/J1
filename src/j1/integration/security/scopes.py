SCOPE_READ = "kb:read"
SCOPE_SEARCH = "kb:search"
SCOPE_RETRIEVE = "kb:retrieve"
SCOPE_ANSWER = "kb:answer"
SCOPE_INGEST = "kb:ingest"
SCOPE_FEEDBACK = "kb:feedback"
SCOPE_ADMIN = "kb:admin"
SCOPE_DELETE = "kb:delete"
SCOPE_AUDIT_READ = "kb:audit.read"


DEFAULT_KB_SCOPES: frozenset[str] = frozenset(
    {
        SCOPE_READ,
        SCOPE_SEARCH,
        SCOPE_RETRIEVE,
        SCOPE_ANSWER,
        SCOPE_INGEST,
        SCOPE_FEEDBACK,
        SCOPE_ADMIN,
        SCOPE_DELETE,
        SCOPE_AUDIT_READ,
    }
)
