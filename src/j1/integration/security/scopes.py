SCOPE_READ = "kb:read"
SCOPE_SEARCH = "kb:search"
SCOPE_RETRIEVE = "kb:retrieve"
SCOPE_ANSWER = "kb:answer"
SCOPE_INGEST = "kb:ingest"
SCOPE_FEEDBACK = "kb:feedback"
SCOPE_ADMIN = "kb:admin"
SCOPE_DELETE = "kb:delete"
SCOPE_AUDIT_READ = "kb:audit.read"
# Post-ingestion validation surface — manual test queries today,
# generated validation sets / runs in later phases. Read = view a
# validation set or result. Write = run a manual test query, generate
# a set, execute a validation run, record a tester verdict.
SCOPE_VALIDATION_READ = "kb:validation.read"
SCOPE_VALIDATION_WRITE = "kb:validation.write"


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
        SCOPE_VALIDATION_READ,
        SCOPE_VALIDATION_WRITE,
    }
)
