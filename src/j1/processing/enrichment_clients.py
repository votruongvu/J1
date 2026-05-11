"""Wave 10.6 — analysis-client contracts for the post-compile
enrichment modules + adapters that bridge production LLM clients
to the module-facing protocol.

The new `EnrichmentModule` adapters in `legacy_enricher_modules.py`
call into a typed `TextAnalysisClient` / `VisionAnalysisClient`
contract:

  * `TextAnalysisClient.extract(prompt, schema, metadata) -> (dict, usage)`
  * `VisionAnalysisClient.analyze(prompt, schema, metadata) -> (dict, usage)`

Production LLM clients (`j1.llm.clients.TextLLMClient` /
`VisionLLMClient`) match the text contract already (same `extract`
shape). The vision contract diverges — the production vision
client operates per-image with bytes input and returns text. This
module provides a `PerImageVisionAdapter` that loops over a
caller-supplied image stream + parses each per-image response into
the batch JSON shape the wrapper expects.

The adapter NEVER calls real LLMs — its `analyze` method drives a
provided `VisionLLMClient` instance, which in turn talks to the
vendor. Tests substitute fakes for both the adapter's image stream
and the underlying client.

Protocol-only design: callers code against the typed interface;
the bootstrap supplies the concrete client (production) or a fake
(tests). Misconfigured deployments (no client wired) skip the
modules cleanly with documented reasons (see
`legacy_enricher_modules.py::can_run`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol, runtime_checkable


__all__ = [
    "TextAnalysisClient",
    "VisionAnalysisClient",
    "VisionImagePayload",
    "ImageBytesProvider",
    "PerImageVisionAdapter",
    "TextLLMClientAdapter",
]


@runtime_checkable
class TextAnalysisClient(Protocol):
    """The text-analysis contract every text-shaped enrichment
    module consumes.

    Production: `j1.llm.clients.TextLLMClient` matches this
    structurally — the wire signature is `(input_text, schema, *,
    metadata)`. The wrapper passes its prompt as the first
    positional argument, which the production client interprets as
    `input_text`. Same return shape: `(parsed_dict, usage)`.

    Tests: substitute any object with a callable `.extract`
    matching this Protocol.
    """

    def extract(
        self,
        prompt: str,
        schema: Mapping[str, Any],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[Mapping[str, Any], Any]: ...


@runtime_checkable
class VisionAnalysisClient(Protocol):
    """The vision-analysis contract the image enrichment module
    consumes.

    Production: NOT matched by `j1.llm.clients.VisionLLMClient`
    directly — see `PerImageVisionAdapter` below for the bridge.

    Returns a JSON-shaped dict carrying `images: [{image_id, caption,
    role, confidence}, …]` — the wrapper iterates this list.
    """

    def analyze(
        self,
        prompt: str,
        schema: Mapping[str, Any],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[Mapping[str, Any], Any]: ...


# ---- Image-byte provider -----------------------------------------


@dataclass(frozen=True)
class VisionImagePayload:
    """One image + its identity + content-type hint, as supplied
    to the `PerImageVisionAdapter`.

    Public class — image-bytes-providers constructed by the
    bootstrap reference this type directly."""

    image_id: str
    image_bytes: bytes
    media_type: str | None = None


ImageBytesProvider = Callable[[], Iterable[VisionImagePayload]]
"""Callable supplied by the activity that yields per-image content
for the adapter to send to the vision LLM. Bound at construction
time so the adapter stays stateless wrt. compile artifacts."""


# ---- Per-image vision adapter ------------------------------------


class PerImageVisionAdapter:
    """Bridges the production `VisionLLMClient` (per-image bytes →
    text response) onto the `VisionAnalysisClient` Protocol (batch
    prompt + schema → JSON dict).

    For each image in the provider, the adapter:
      1. Calls `vision_client.analyze_image(image=..., prompt=...,
         media_type=...)` once per image.
      2. Attempts `json.loads` on the text response; falls back to
         treating the whole response as a plain caption.
      3. Builds an `{image_id, caption, …}` entry per image.
      4. Returns `({"images": [entries...]}, usage)` shaped per the
         `VisionAnalysisClient` Protocol.

    Usage aggregation: input/output token counts are summed across
    the per-image calls; provider + model are inherited from the
    last call (the wrapper records aggregate usage on the outcome).

    The limiter is NOT held inside the adapter — the enrichment
    module wraps the adapter call with the limiter. So a single
    enrichment-stage acquisition covers the full batch of images.
    This bounds CONCURRENT enrichment stages, not concurrent
    per-image LLM calls. Per-image rate limiting is the vendor SDK's
    responsibility today.
    """

    def __init__(
        self,
        vision_client: Any,
        *,
        image_provider: ImageBytesProvider,
    ) -> None:
        self._vision = vision_client
        self._image_provider = image_provider

    def analyze(
        self,
        prompt: str,
        schema: Mapping[str, Any],  # noqa: ARG002 — Protocol contract; vision client doesn't take a schema
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[Mapping[str, Any], Any]:
        entries: list[dict[str, Any]] = []
        agg_input_tokens = 0
        agg_output_tokens = 0
        provider: str | None = None
        model: str | None = None
        for image in self._image_provider() or ():
            text, usage = self._vision.analyze_image(
                image.image_bytes,
                prompt=prompt,
                media_type=image.media_type,
                metadata=dict(metadata or {}),
            )
            # Try JSON-first; if the model returned a dict matching
            # the schema we forward it verbatim. Most vision models
            # in J1 today return prose — we degrade to a caption-only
            # entry in that case.
            entry: dict[str, Any] = {"image_id": image.image_id}
            parsed_dict = _try_parse_image_response(text)
            if parsed_dict is not None:
                # Merge the model's parsed fields into the entry.
                for k, v in parsed_dict.items():
                    if k != "image_id":
                        entry[k] = v
                # Always preserve the caller's image_id over the
                # model's (hallucinated ids are a known failure mode).
                entry["image_id"] = image.image_id
            else:
                entry["caption"] = (text or "").strip() or None
            entries.append(entry)
            # Aggregate usage fields (best-effort; fields may be
            # missing on some clients).
            agg_input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            agg_output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
            provider = getattr(usage, "provider", None) or provider
            model = getattr(usage, "model", None) or model
        agg_usage = _AggregateVisionUsage(
            provider=provider, model=model,
            input_tokens=agg_input_tokens,
            output_tokens=agg_output_tokens,
        )
        return ({"images": entries}, agg_usage)


@dataclass(frozen=True)
class _AggregateVisionUsage:
    """Tiny usage record the adapter emits so the wrapper sees a
    `model_usage`-shaped object. Field names match
    `_model_usage_from`'s reader in
    `legacy_enricher_modules.py` (provider / model / input_tokens
    / output_tokens / duration_ms)."""
    provider: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int | None = None


def _try_parse_image_response(text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction from a vision-LLM text response.

    Most production vision models return either:
      * a fenced ```json``` code block, or
      * raw JSON (when prompted), or
      * prose (the most common fallback)

    Returns the parsed dict on success; None when the response is
    unstructured. The adapter then falls back to a caption-only
    entry."""
    if not text:
        return None
    stripped = text.strip()
    # Strip a leading/trailing markdown code fence if present.
    if stripped.startswith("```"):
        # Remove first ```[lang]\n and the trailing ```
        first_newline = stripped.find("\n")
        if first_newline > 0:
            stripped = stripped[first_newline + 1:]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ---- Text adapter (thin pass-through) ----------------------------


class TextLLMClientAdapter:
    """Thin wrapper around a production `TextLLMClient` so callers
    that want to be explicit about the `TextAnalysisClient` Protocol
    can construct one. The adapter adds nothing functional — the
    production client already matches the Protocol structurally —
    but having an explicit adapter at the bootstrap boundary makes
    the dependency arrow visible in the code.

    Deployments that prefer to pass the raw client straight through
    can do that; the wrappers don't care."""

    def __init__(self, text_client: Any) -> None:
        self._client = text_client

    def extract(
        self,
        prompt: str,
        schema: Mapping[str, Any],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[Mapping[str, Any], Any]:
        return self._client.extract(prompt, schema, metadata=metadata)
