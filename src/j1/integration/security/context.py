from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

AUTH_TYPE_ANONYMOUS = "anonymous"
AUTH_TYPE_API_KEY = "api_key"
AUTH_TYPE_JWT = "jwt"


_EMPTY_METADATA: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class SecurityContext:
    """Application-level identity passed inward from external interfaces.

    Vendor-neutral: contains no raw HTTP headers, JWT library objects, or
    OAuth provider objects. Adapters map their wire format into this and
    nothing else crosses the integration boundary.
    """

    subject: str
    tenant_id: str | None
    scopes: frozenset[str] = field(default_factory=frozenset)
    auth_type: str = AUTH_TYPE_ANONYMOUS
    request_id: str | None = None
    metadata: Mapping[str, str] = field(default_factory=lambda: _EMPTY_METADATA)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def has_any_scope(self, scopes: Iterable[str]) -> bool:
        return any(s in self.scopes for s in scopes)

    @property
    def is_anonymous(self) -> bool:
        return self.auth_type == AUTH_TYPE_ANONYMOUS


ANONYMOUS_CONTEXT: SecurityContext = SecurityContext(
    subject="anonymous",
    tenant_id=None,
    scopes=frozenset(),
    auth_type=AUTH_TYPE_ANONYMOUS,
)
