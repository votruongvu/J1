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
    "ImageProviderResult",
    "PerImageVisionAdapter",
    "TextLLMClientAdapter",
    "WorkspaceImageBytesProvider",
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
    bootstrap reference this type directly.

    `image_id` is the operator-visible identifier the FE renders;
    it can equal `source_artifact_id` (typical when the provider
    sources bytes from the artifact registry) or it can be the
    parser's internal id when a producer correlates them. The
    `image_summary.image_id` on the typed overlay mirrors this
    field verbatim.

    `source_artifact_id` is the artifact-registry id the bytes
    came from (Wave 11A) — surfaced so the FE can deep-link to the
    raw artifact endpoint. `None` when the provider sourced bytes
    from a non-registry source (rare; in-memory tests). `metadata`
    is free-form, optional, and not serialised through the
    enrichment-result wire payload — providers attach diagnostic
    info here that surfaces in adapter logs."""

    image_id: str
    image_bytes: bytes
    media_type: str | None = None
    source_artifact_id: str | None = None
    metadata: Mapping[str, Any] | None = None


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
      1. Acquires the shared `LLMCallLimiter` (Wave 11B) when one
         was supplied — so each external vision call gets its own
         semaphore slot, mirroring how the text path treats each
         `text_client.extract` invocation.
      2. Calls `vision_client.analyze_image(image=..., prompt=...,
         media_type=...)` exactly once for that image.
      3. Attempts `json.loads` on the text response; falls back to
         treating the whole response as a plain caption.
      4. Builds an `{image_id, caption, …}` entry per image.
      5. Returns `({"images": [entries...]}, usage)` shaped per the
         `VisionAnalysisClient` Protocol.

    Usage aggregation: input/output token counts are summed across
    the per-image calls; provider + model are inherited from the
    last call (the wrapper records aggregate usage on the outcome).

    Limiter ownership: the adapter holds an optional reference to
    the shared limiter so EACH per-image LLM call gets its own
    acquisition. The wrapping image-enrichment module no longer
    needs to limit the adapter's outer `analyze()` call — that's
    the caller's choice. When the limiter is None, calls bypass
    the gate.

    Failure handling: an individual per-image LLM call may raise
    (network blip, vendor 429). The adapter swallows the exception
    for that image, records a fallback caption-only entry with a
    short error reason in `metadata`, and continues to the next
    image. The aggregate `analyze()` call never raises — the
    operator sees the per-image misses through the entry's
    `metadata.error` field. This keeps the limiter acquisition /
    release symmetry tight (every acquire has a corresponding
    release in the limiter's `run()` even on raise).
    """

    def __init__(
        self,
        vision_client: Any,
        *,
        image_provider: ImageBytesProvider,
        llm_call_limiter: Any | None = None,
    ) -> None:
        self._vision = vision_client
        self._image_provider = image_provider
        self._llm_call_limiter = llm_call_limiter

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
        per_call_metadata_template = dict(metadata or {})
        for image in self._image_provider() or ():
            entry: dict[str, Any] = {"image_id": image.image_id}
            per_call_metadata = dict(per_call_metadata_template)
            per_call_metadata["image_id"] = image.image_id
            try:
                text, usage = self._invoke_one(
                    prompt=prompt,
                    image=image,
                    metadata=per_call_metadata,
                )
            except Exception as exc:  # noqa: BLE001 — per-image fault
                # Continue to the next image so a single failure
                # doesn't lose every result. Record a structured
                # error on the entry's metadata so the operator
                # sees the miss through the typed overlay.
                entry["caption"] = None
                entry["metadata"] = {
                    "error": f"{type(exc).__name__}: {exc}",
                }
                entries.append(entry)
                continue
            # Try JSON-first; if the model returned a dict matching
            # the schema we forward it verbatim. Most vision models
            # in J1 today return prose — we degrade to a caption-only
            # entry in that case.
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

    def _invoke_one(
        self,
        *,
        prompt: str,
        image: "VisionImagePayload",
        metadata: Mapping[str, Any],
    ) -> tuple[str, Any]:
        """Acquire the limiter (when wired) + call the underlying
        vision client for exactly one image. Returns `(text, usage)`
        from the client; raises if the client raises.

        Limiter semantics: the limiter's `run(callable, *args,
        metadata=...)` acquires the semaphore + invokes the
        callable + releases on return OR raise. So one image →
        one acquisition either way."""
        def _call() -> tuple[str, Any]:
            return self._vision.analyze_image(
                image.image_bytes,
                prompt=prompt,
                media_type=image.media_type,
                metadata=dict(metadata),
            )

        if self._llm_call_limiter is not None:
            return self._llm_call_limiter.run(_call, metadata=metadata)
        return _call()


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


# ---- Wave 11A — workspace-aware image-bytes provider ------------


@dataclass(frozen=True)
class ImageProviderResult:
    """Return type for `WorkspaceImageBytesProvider`. Carries the
    loaded payloads PLUS structured warnings explaining any
    missing-bytes outcomes.

    `payloads` is the input to the `PerImageVisionAdapter` — empty
    when no image artifacts could be loaded.

    `warnings` is operator-readable strings describing per-image
    misses (e.g. "artifact art-1: bytes not loadable; check
    workspace permissions"). The activity surfaces these on the
    image module's `EnrichmentModuleOutcome.warnings` so they reach
    the final report."""

    payloads: tuple["VisionImagePayload", ...]
    warnings: tuple[str, ...] = ()


class WorkspaceImageBytesProvider:
    """Resolves the current run's detected compile-image artifacts
    into `VisionImagePayload` records the
    `PerImageVisionAdapter` consumes.

    Construction: per-run inside the enrichment activity. Closes
    over the artifact registry + workspace + project context +
    document id. Workspace-side path resolution mirrors
    `IngestionResultReviewService._resolve_artifact_path` — full
    path-traversal guard so a tampered registry entry can never
    read outside the project workspace.

    Skip semantics: a registry without `compile.image` artifacts
    for the document yields `payloads=()` so the
    `ImageEnrichmentModule.can_run` check still skips with
    "compile detected no images" (the image module's check looks
    at `compile_result.detected_images` first; if THAT is empty
    the provider is never invoked anyway). When images WERE
    detected but their bytes can't be loaded (file missing /
    permissions denied / decoded mismatch), the provider returns
    `payloads=()` plus a populated `warnings` list — the activity
    forwards the warnings to the operator-facing module outcome.

    Pure I/O — no LLM calls. Same workspace+registry inputs →
    same payloads."""

    # Artifact kinds the provider considers "image-bearing".
    # Mirrors `_is_image_kind` in `j1/enrichers.py` minus the
    # `enriched.visuals` re-enrich loop guard (this provider
    # produces the bytes for the image module; not for re-enriching
    # an existing overlay).
    _IMAGE_ARTIFACT_KINDS: tuple[str, ...] = ("compile.image",)

    # Best-effort extension → media-type table. Adapter-side
    # consumers tolerate `None` so a missing entry isn't fatal.
    _EXT_TO_MEDIA_TYPE: dict[str, str] = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".tiff": "image/tiff",
        ".bmp": "image/bmp",
    }

    def __init__(
        self,
        *,
        artifact_registry: Any,
        workspace: Any,
        ctx: Any,
        document_id: str,
        run_id: str | None = None,
    ) -> None:
        self._artifacts = artifact_registry
        self._workspace = workspace
        self._ctx = ctx
        self._document_id = document_id
        self._run_id = run_id
        # Per-construction cache so the adapter calling the
        # provider multiple times in one stage doesn't re-list /
        # re-read on each call.
        self._cached: ImageProviderResult | None = None

    # ---- Public API ----

    def __call__(self) -> Iterable[VisionImagePayload]:
        """Adapter-facing entrypoint. Returns the payload iterable
        directly so the `ImageBytesProvider` callable type
        contract is preserved. Warnings are accessible via
        `last_result()` after the call."""
        result = self.load_all()
        return result.payloads

    def last_result(self) -> ImageProviderResult | None:
        """Return the most-recent `ImageProviderResult` so the
        caller (the activity) can read structured warnings to
        attach to the module outcome."""
        return self._cached

    def load_all(self) -> ImageProviderResult:
        """Walk the artifact registry + workspace, build the
        payload list, accumulate warnings, cache + return."""
        if self._cached is not None:
            return self._cached
        try:
            records = self._artifacts.list_artifacts(self._ctx) or []
        except Exception as exc:  # noqa: BLE001 — registry errors → no images
            self._cached = ImageProviderResult(
                payloads=(),
                warnings=(
                    f"image-artifact registry lookup failed: "
                    f"{type(exc).__name__}",
                ),
            )
            return self._cached

        # Filter to per-document image-bearing artifacts.
        image_records = [
            r for r in records
            if getattr(r, "kind", "") in self._IMAGE_ARTIFACT_KINDS
            and self._belongs_to_document(r)
        ]
        payloads: list[VisionImagePayload] = []
        warnings: list[str] = []
        for record in image_records:
            payload, warn = self._record_to_payload(record)
            if payload is not None:
                payloads.append(payload)
            if warn:
                warnings.append(warn)
        self._cached = ImageProviderResult(
            payloads=tuple(payloads),
            warnings=tuple(warnings),
        )
        return self._cached

    # ---- Internals ----

    def _belongs_to_document(self, record: Any) -> bool:
        """A registry record is associated with this document when
        either:
          * `record.source_document_ids` lists the document_id, OR
          * `record.metadata["document_id"]` matches.
        Either signal is enough — producers populate one or the
        other depending on the writer."""
        if not self._document_id:
            return True
        sources = getattr(record, "source_document_ids", None) or []
        if self._document_id in sources:
            return True
        meta = getattr(record, "metadata", None) or {}
        return meta.get("document_id") == self._document_id

    def _record_to_payload(
        self, record: Any,
    ) -> tuple[VisionImagePayload | None, str | None]:
        """Build the typed payload for one image-bearing artifact.

        Returns (None, warning) when the bytes couldn't be loaded.
        Returns (payload, None) on success."""
        from pathlib import PurePosixPath

        artifact_id = getattr(record, "artifact_id", None) or "<unknown>"
        location = (getattr(record, "location", "") or "").strip()
        if not location:
            return (
                None,
                f"image artifact {artifact_id}: registry has no location",
            )
        parts = PurePosixPath(location).parts
        if len(parts) < 2:
            return (
                None,
                f"image artifact {artifact_id}: location malformed",
            )
        area_name, *rest = parts
        # Resolve workspace area + path-traversal guard.
        try:
            from j1.workspace.layout import WorkspaceArea
            area = WorkspaceArea(area_name)
        except (ImportError, ValueError):
            return (
                None,
                f"image artifact {artifact_id}: unknown workspace area "
                f"{area_name!r}",
            )
        try:
            area_root = self._workspace.area(
                self._ctx, area,
            ).resolve()
            candidate = area_root.joinpath(*rest).resolve()
            candidate.relative_to(area_root)  # path-traversal guard
            image_bytes = candidate.read_bytes()
        except Exception as exc:  # noqa: BLE001 — file IO / traversal
            return (
                None,
                f"image artifact {artifact_id}: bytes not loadable "
                f"({type(exc).__name__})",
            )
        if not image_bytes:
            return (
                None,
                f"image artifact {artifact_id}: file is empty",
            )
        suffix = candidate.suffix.lower()
        media_type = self._EXT_TO_MEDIA_TYPE.get(suffix)
        return (
            VisionImagePayload(
                image_id=artifact_id,
                image_bytes=image_bytes,
                media_type=media_type,
                source_artifact_id=artifact_id,
                metadata={
                    "document_id": self._document_id,
                    "kind": getattr(record, "kind", "") or "",
                    "byte_size": len(image_bytes),
                },
            ),
            None,
        )
