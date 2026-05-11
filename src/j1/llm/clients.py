"""Provider-neutral LLM role interfaces.

Three roles ship: text, vision, embedding. Each is a small `Protocol`
matching the framework's existing style (`KnowledgeCompiler`,
`EnrichmentProcessor`, `EventPublisher` are all Protocols).

Optional methods (`stream`, `summarize`, …) raise `LLMCapabilityError`
when an implementation declines to support them, so callers can branch
on capability without isinstance / hasattr checks.
"""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class LLMUsage:
    """Token + cost accounting from a single LLM call."""

    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float | None = None


class LLMCapabilityError(NotImplementedError):
    """Raised by an LLM client when asked for an unsupported capability.

 Subclass of NotImplementedError so generic catch-blocks still work,
 but distinct enough that callers can tell "this provider doesn't
 do that" from "this is a bug".
 """


# ---- Text -----------------------------------------------------------


class TextLLMClient(Protocol):
    """Text-generation role.

 Implementations MUST return both the generated text and an
 `LLMUsage` record (token counts may be zero if the provider
 doesn't surface them).
 """

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    def generate(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str, LLMUsage]: ...

    def summarize(
        self,
        input_text: str,
        *,
        max_output_tokens: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str, LLMUsage]: ...

    def extract(
        self,
        input_text: str,
        schema: Mapping[str, Any],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[Mapping[str, Any], LLMUsage]: ...

    def classify(
        self,
        input_text: str,
        labels: Sequence[str],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str, LLMUsage]: ...


# ---- Vision ---------------------------------------------------------


class VisionLLMClient(Protocol):
    """Vision-analysis role.

 Image inputs are bytes — leaves it to the caller whether to read
 from disk, S3, or somewhere else. The optional `media_type`
 (`image/png`, `image/jpeg`, …) is forwarded to providers that
 care about content-type negotiation.
 """

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    def analyze_image(
        self,
        image: bytes,
        *,
        prompt: str,
        media_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str, LLMUsage]: ...

    def analyze_page(
        self,
        image: bytes,
        *,
        prompt: str,
        media_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str, LLMUsage]: ...

    def describe_diagram(
        self,
        image: bytes,
        *,
        prompt: str = "Describe this diagram.",
        media_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str, LLMUsage]: ...

    def extract_visual_table(
        self,
        image: bytes,
        *,
        prompt: str = "Extract the table as JSON.",
        media_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[Mapping[str, Any], LLMUsage]: ...


# ---- Embedding ------------------------------------------------------


class EmbeddingClient(Protocol):
    """Embedding-generation role.

 `dimension` and `max_tokens` are introspection helpers that
 callers (e.g. the search indexer) use to size their downstream
 storage / vector index.
 """

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    def embed_text(self, text: str) -> tuple[list[float], LLMUsage]: ...

    def embed_batch(
        self,
        texts: Iterable[str],
    ) -> tuple[list[list[float]], LLMUsage]: ...

    def dimension(self) -> int: ...

    def max_tokens(self) -> int: ...
