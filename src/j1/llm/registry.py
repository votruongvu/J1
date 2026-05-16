"""LLM client registry.

Three roles ship: text, vision, embedding. The composition root
constructs a registry once at startup and hands it to every adapter
that needs an LLM client. Adapters never read env vars directly —
that's the registry's only DI seam.

Optional roles return `None` from `try_resolve(role)`. Required roles
use `resolve(role)` and raise `LLMRoleNotRegistered` when missing.
"""

from collections.abc import Iterable, Mapping

from j1.llm.clients import EmbeddingClient, TextLLMClient, VisionLLMClient
from j1.llm.errors import LLMConfigError, LLMRoleNotRegistered

LLM_ROLE_TEXT = "text"
LLM_ROLE_VISION = "vision"
LLM_ROLE_EMBEDDING = "embedding"
# Cheap / structured-output role for the adaptive ingestion planner
# and other short, deterministic tasks (document classification,
# light metadata, heading normalisation). Implementations are free
# to point this at the same provider/base_url as `text` with just a
# different model. `LLM_ROLE_FAST` is OPTIONAL — deterministic
# planning works without it; consumers must call `try_fast` rather
# than `fast` so missing config is a no-op rather than a startup
# failure.
LLM_ROLE_FAST = "fast"
# High-accuracy role for runs whose `IngestPlan.requires_premium_llm`
# is True (today: documents the planner picked under
# `force_full` / `high_accuracy` policy where extraction quality
# matters more than cost). `LLM_ROLE_PREMIUM` is OPTIONAL — the
# `try_premium_or_text` helper below falls back to the standard
# text role when no premium client is registered, so deployments
# without a separate premium provider keep working.
LLM_ROLE_PREMIUM = "premium"

# Stage-keyed roles. Each is OPTIONAL — the resolver helpers below
# fall back to `LLM_ROLE_TEXT` when the stage-specific client isn't
# registered, so single-model deployments keep working unchanged.
# Operators wire a separate client per stage when indexing,
# user-facing query, and post-compile enrichment have different
# cost / latency / accuracy budgets:
#
#   * `LLM_ROLE_INDEXING`   — drives LightRAG's `llm_model_func`
#     during compile (entity / relationship extraction) and the
#     post-compile graph build. The volume of calls is dominated
#     by chunk count; deployments typically want a cheap+fast
#     model here.
#   * `LLM_ROLE_QUERY`      — drives the synthesizer during
#     user-facing question answering (RAGAnything `aquery`). Often
#     the largest / most-accurate model.
#   * `LLM_ROLE_ENRICHMENT` — drives post-compile enrichment
#     modules (Domain Enrichment, future entity normalisation,
#     etc.). Triggered by operator action, so latency matters less
#     than accuracy.
LLM_ROLE_INDEXING = "indexing"
LLM_ROLE_QUERY = "query"
LLM_ROLE_ENRICHMENT = "enrichment"

KNOWN_ROLES: frozenset[str] = frozenset(
    {
        LLM_ROLE_TEXT,
        LLM_ROLE_VISION,
        LLM_ROLE_EMBEDDING,
        LLM_ROLE_FAST,
        LLM_ROLE_PREMIUM,
        LLM_ROLE_INDEXING,
        LLM_ROLE_QUERY,
        LLM_ROLE_ENRICHMENT,
    }
)


class LLMProviderRegistry:
    """Maps role names → LLM clients.

 Construct empty, then `register(role, client)` for each role the
 deployment configures. `try_resolve` returns None for missing
 roles (callers that *need* the role use `resolve`, which raises).
 """

    def __init__(
        self,
        clients: Mapping[str, object] | None = None,
    ) -> None:
        self._clients: dict[str, object] = {}
        if clients:
            for role, client in clients.items():
                self.register(role, client)

    def register(self, role: str, client: object) -> None:
        normalized = role.strip().lower()
        if not normalized:
            raise LLMConfigError("LLM role name must be non-empty")
        self._clients[normalized] = client

    def resolve(self, role: str) -> object:
        client = self.try_resolve(role)
        if client is None:
            raise LLMRoleNotRegistered(role, registered=self.list())
        return client

    def try_resolve(self, role: str) -> object | None:
        return self._clients.get(role.strip().lower())

    def has(self, role: str) -> bool:
        return role.strip().lower() in self._clients

    def list(self) -> tuple[str, ...]:
        """Roles currently registered, alphabetised for stable output."""
        return tuple(sorted(self._clients))

    def validate_required(self, required_roles: Iterable[str]) -> None:
        """Raise `LLMRoleNotRegistered` if any required role is missing.

 Used by the composition root to fail startup early when a
 selected provider needs a role nothing satisfies.
 """
        missing = [r for r in required_roles if not self.has(r)]
        if missing:
            registered = self.list()
            details = ", ".join(missing)
            raise LLMRoleNotRegistered(details, registered=registered)

    def diagnostics(self) -> dict[str, dict]:
        """Per-role provider + model summary, secrets-safe.

 Adapters publish `provider` + `model` properties; we only
 expose those plus the type-name. API keys / base URLs / config
 dicts are NEVER included (callers log this dict at startup).
 """
        out: dict[str, dict] = {}
        for role, client in self._clients.items():
            out[role] = {
                "client_type": type(client).__name__,
                "provider": getattr(client, "provider", None),
                "model": getattr(client, "model", None),
            }
        return out

    # ---- Typed convenience helpers (return type narrowing) -----------

    def text(self) -> TextLLMClient:
        return self.resolve(LLM_ROLE_TEXT)  # type: ignore[return-value]

    def vision(self) -> VisionLLMClient:
        return self.resolve(LLM_ROLE_VISION)  # type: ignore[return-value]

    def embedding(self) -> EmbeddingClient:
        return self.resolve(LLM_ROLE_EMBEDDING)  # type: ignore[return-value]

    def try_text(self) -> TextLLMClient | None:
        return self.try_resolve(LLM_ROLE_TEXT)  # type: ignore[return-value]

    def try_vision(self) -> VisionLLMClient | None:
        return self.try_resolve(LLM_ROLE_VISION)  # type: ignore[return-value]

    def try_embedding(self) -> EmbeddingClient | None:
        return self.try_resolve(LLM_ROLE_EMBEDDING)  # type: ignore[return-value]

    def try_fast(self) -> TextLLMClient | None:
        """Optional FAST role. Falls back to text when not configured;
 consumers (typically the planner's LLM-fallback path) handle
 the absence themselves."""
        return self.try_resolve(LLM_ROLE_FAST)  # type: ignore[return-value]

    def try_premium(self) -> TextLLMClient | None:
        """Optional PREMIUM role. Returns None when no premium client
 is registered — callers should usually use
 `try_premium_or_text` instead so a missing premium config
 falls back to TEXT rather than disabling the call."""
        return self.try_resolve(LLM_ROLE_PREMIUM)  # type: ignore[return-value]

    def try_premium_or_text(self) -> TextLLMClient | None:
        """Resolve PREMIUM if configured, otherwise TEXT.

 The runtime contract for premium-LLM resolution: when a
 caller asks for premium accuracy, prefer the premium client;
 when no premium client is wired, TEXT is the documented
 fallback. Either way, the call still happens — the flag
 never silently disables work."""
        client = self.try_resolve(LLM_ROLE_PREMIUM)
        if client is not None:
            return client  # type: ignore[return-value]
        return self.try_resolve(LLM_ROLE_TEXT)  # type: ignore[return-value]

    # ---- Stage-keyed helpers -----------------------------------------
    #
    # Each helper returns the stage-specific client when registered,
    # otherwise falls back to TEXT. Callers use these instead of
    # `text()` so a deployment can split indexing / query /
    # enrichment onto separate models without every call site
    # learning about the split.

    def indexing(self) -> TextLLMClient:
        """Resolve INDEXING, falling back to TEXT.
 Raises `LLMRoleNotRegistered` only when neither role is wired."""
        client = self.try_resolve(LLM_ROLE_INDEXING)
        if client is not None:
            return client  # type: ignore[return-value]
        return self.resolve(LLM_ROLE_TEXT)  # type: ignore[return-value]

    def query(self) -> TextLLMClient:
        """Resolve QUERY, falling back to TEXT.
 Raises `LLMRoleNotRegistered` only when neither role is wired."""
        client = self.try_resolve(LLM_ROLE_QUERY)
        if client is not None:
            return client  # type: ignore[return-value]
        return self.resolve(LLM_ROLE_TEXT)  # type: ignore[return-value]

    def enrichment(self) -> TextLLMClient:
        """Resolve ENRICHMENT, falling back to TEXT.
 Raises `LLMRoleNotRegistered` only when neither role is wired."""
        client = self.try_resolve(LLM_ROLE_ENRICHMENT)
        if client is not None:
            return client  # type: ignore[return-value]
        return self.resolve(LLM_ROLE_TEXT)  # type: ignore[return-value]

    def try_indexing(self) -> TextLLMClient | None:
        """Optional INDEXING role — returns None when not wired AND
 no TEXT fallback exists. Most call sites should use
 `indexing()` instead so they pick up the TEXT fallback."""
        return self.try_resolve(LLM_ROLE_INDEXING)  # type: ignore[return-value]

    def try_query(self) -> TextLLMClient | None:
        return self.try_resolve(LLM_ROLE_QUERY)  # type: ignore[return-value]

    def try_enrichment(self) -> TextLLMClient | None:
        return self.try_resolve(LLM_ROLE_ENRICHMENT)  # type: ignore[return-value]

    def stage_diagnostics(self) -> dict[str, dict]:
        """Per-stage resolved-client summary (which model fires for
 indexing / query / enrichment after fallback resolution).
 Useful at startup so operators can log the effective plan
 even when stage-specific roles aren't wired."""
        def _summary(client: object | None) -> dict[str, object | None]:
            if client is None:
                return {"client_type": None, "provider": None, "model": None}
            return {
                "client_type": type(client).__name__,
                "provider": getattr(client, "provider", None),
                "model": getattr(client, "model", None),
            }
        text_fallback = self.try_resolve(LLM_ROLE_TEXT)
        return {
            "indexing": _summary(
                self.try_resolve(LLM_ROLE_INDEXING) or text_fallback,
            ),
            "query": _summary(
                self.try_resolve(LLM_ROLE_QUERY) or text_fallback,
            ),
            "enrichment": _summary(
                self.try_resolve(LLM_ROLE_ENRICHMENT) or text_fallback,
            ),
        }
