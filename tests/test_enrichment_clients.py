"""protocol + adapter tests for `enrichment_clients.py`.

Pins:
 1. `TextAnalysisClient` + `VisionAnalysisClient` Protocols are
 `runtime_checkable` so isinstance works.
 2. `TextLLMClientAdapter` is a thin pass-through.
 3. `PerImageVisionAdapter` loops per image, parses JSON when
 present, falls back to caption-only otherwise.
 4. `PerImageVisionAdapter` aggregates usage tokens across images.
 5. Empty provider produces an empty `images: []` payload — adapter
 never raises on no-images.
"""

from __future__ import annotations

import json

from j1.processing.enrichment_clients import (
    ImageBytesProvider,
    PerImageVisionAdapter,
    TextAnalysisClient,
    TextLLMClientAdapter,
    VisionAnalysisClient,
    VisionImagePayload,
)


# ---- Fakes ---------------------------------------------------------


class _FakeUsage:
    def __init__(
        self, model: str = "vision-fake", input_tokens: int = 10,
        output_tokens: int = 20, provider: str = "fake-vendor",
    ) -> None:
        self.model = model
        self.provider = provider
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeVisionLLMClient:
    """Mimics the production `VisionLLMClient.analyze_image` —
 per-image bytes input, text response."""

    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = list(responses or [])
        self.calls: list[dict] = []

    def analyze_image(
        self, image: bytes, *, prompt: str,
        media_type: str | None = None,
        metadata=None,
    ):
        self.calls.append({
            "image_bytes_len": len(image), "prompt": prompt,
            "media_type": media_type, "metadata": dict(metadata or {}),
        })
        text = self._responses.pop(0) if self._responses else "a generic image"
        return text, _FakeUsage()


class _FakeTextClient:
    def extract(self, prompt, schema, *, metadata=None):
        return ({"requirements": []}, _FakeUsage())


# ---- Protocols ----------------------------------------------------


def test_text_analysis_client_protocol_is_runtime_checkable():
    """`runtime_checkable` lets isinstance flag misconfigured
 callers. Production clients should match the Protocol
 structurally."""
    assert isinstance(_FakeTextClient(), TextAnalysisClient)


def test_vision_analysis_client_protocol_is_runtime_checkable():
    adapter = PerImageVisionAdapter(
        _FakeVisionLLMClient(),
        image_provider=lambda: [],
    )
    assert isinstance(adapter, VisionAnalysisClient)


def test_raw_vision_llm_client_does_not_match_analysis_protocol():
    """Production `VisionLLMClient.analyze_image` is per-image bytes;
 `VisionAnalysisClient` expects `analyze(prompt, schema, metadata)`.
 Without the adapter, isinstance would correctly reject the raw
 client — that's the design check the adapter exists to satisfy."""
    raw = _FakeVisionLLMClient()
    assert not isinstance(raw, VisionAnalysisClient)


# ---- TextLLMClientAdapter ----------------------------------------


def test_text_adapter_passes_through_to_underlying_client():
    captured: list[tuple] = []

    class _Client:
        def extract(self, prompt, schema, *, metadata=None):
            captured.append((prompt, schema, dict(metadata or {})))
            return ({"k": "v"}, _FakeUsage())

    adapter = TextLLMClientAdapter(_Client())
    parsed, usage = adapter.extract("hi", {"type": "object"}, metadata={"x": 1})
    assert parsed == {"k": "v"}
    assert captured == [("hi", {"type": "object"}, {"x": 1})]


def test_text_adapter_implements_protocol():
    adapter = TextLLMClientAdapter(_FakeTextClient())
    assert isinstance(adapter, TextAnalysisClient)


# ---- PerImageVisionAdapter ---------------------------------------


def test_vision_adapter_returns_empty_images_for_empty_provider():
    """No images → adapter never calls the LLM; returns an empty
 list rather than raising. The image module's `can_run` should
 skip BEFORE this is reached, so this is a defence-in-depth path."""
    fake = _FakeVisionLLMClient()
    adapter = PerImageVisionAdapter(fake, image_provider=lambda: [])
    parsed, _ = adapter.analyze("describe", {})
    assert parsed == {"images": []}
    assert fake.calls == []


def test_vision_adapter_calls_client_once_per_image():
    images = [
        VisionImagePayload(image_id="i-1", image_bytes=b"\x01\x02",
                           media_type="image/png"),
        VisionImagePayload(image_id="i-2", image_bytes=b"\x03",
                           media_type="image/jpeg"),
    ]
    fake = _FakeVisionLLMClient(responses=[
        json.dumps({"caption": "site plan", "role": "diagram"}),
        "decorative footer image",
    ])
    adapter = PerImageVisionAdapter(fake, image_provider=lambda: images)
    parsed, _ = adapter.analyze("describe", {})
    assert len(fake.calls) == 2
    assert fake.calls[0]["media_type"] == "image/png"
    assert fake.calls[1]["media_type"] == "image/jpeg"
    assert len(parsed["images"]) == 2


def test_vision_adapter_parses_json_response_and_preserves_image_id():
    images = [
        VisionImagePayload(
            image_id="canonical-id",
            image_bytes=b"x",
        ),
    ]
    # Model hallucinates a different image_id — caller's id wins.
    fake = _FakeVisionLLMClient(responses=[
        json.dumps({"image_id": "model-says-something-else",
                    "caption": "plan", "role": "figure"}),
    ])
    adapter = PerImageVisionAdapter(fake, image_provider=lambda: images)
    parsed, _ = adapter.analyze("describe", {})
    assert parsed["images"][0]["image_id"] == "canonical-id"
    assert parsed["images"][0]["caption"] == "plan"
    assert parsed["images"][0]["role"] == "figure"


def test_vision_adapter_falls_back_to_caption_for_unstructured_response():
    """Models that return prose (not JSON) should still produce
 usable image summaries."""
    images = [
        VisionImagePayload(image_id="i-1", image_bytes=b"x"),
    ]
    fake = _FakeVisionLLMClient(
        responses=["This image shows a construction site."],
    )
    adapter = PerImageVisionAdapter(fake, image_provider=lambda: images)
    parsed, _ = adapter.analyze("describe", {})
    assert parsed["images"][0]["caption"] == "This image shows a construction site."


def test_vision_adapter_handles_markdown_fenced_json():
    """Some models wrap JSON in ```json``` fences. The adapter
 strips the fence and parses the inner payload."""
    images = [
        VisionImagePayload(image_id="i-1", image_bytes=b"x"),
    ]
    fake = _FakeVisionLLMClient(responses=[
        "```json\n{\"caption\": \"diagram\", \"confidence\": 0.9}\n```",
    ])
    adapter = PerImageVisionAdapter(fake, image_provider=lambda: images)
    parsed, _ = adapter.analyze("describe", {})
    assert parsed["images"][0]["caption"] == "diagram"
    assert parsed["images"][0]["confidence"] == 0.9


def test_vision_adapter_aggregates_usage_tokens():
    """Per-image token usage should sum across the batch so the
 enrichment module records a representative total."""
    images = [
        VisionImagePayload(image_id="i-1", image_bytes=b"x"),
        VisionImagePayload(image_id="i-2", image_bytes=b"x"),
    ]

    class _UsageClient:
        def __init__(self):
            self._n = 0

        def analyze_image(self, image, *, prompt, media_type=None,
                          metadata=None):
            self._n += 1
            return f"caption {self._n}", _FakeUsage(
                input_tokens=10 * self._n,
                output_tokens=20 * self._n,
            )

    adapter = PerImageVisionAdapter(_UsageClient(), image_provider=lambda: images)
    _, usage = adapter.analyze("describe", {})
    assert usage.input_tokens == 30   # 10 + 20
    assert usage.output_tokens == 60  # 20 + 40
    assert usage.model == "vision-fake"
    assert usage.provider == "fake-vendor"


def test_image_bytes_provider_type_alias_is_exported():
    """`ImageBytesProvider` is exposed so deployment wiring can
 type its closures explicitly."""

    def _ok() -> list[VisionImagePayload]:
        return [VisionImagePayload(image_id="x", image_bytes=b"y")]

    # Should be a callable type-alias; checking the runtime type is
    # the simplest assertion.
    provider: ImageBytesProvider = _ok
    assert callable(provider)
    assert len(list(provider())) == 1


# ---- Vocabulary guard --------------------------------------------


def test_enrichment_clients_source_has_no_legacy_vocabulary():
    """The new client/adapter module is operator-visible (used in
 deployment wiring). Must stay free of legacy gating /
 split-mode terminology."""
    import inspect
    from j1.processing import enrichment_clients
    src = inspect.getsource(enrichment_clients)
    for forbidden in (
        "split_mode", "SplitMode",
        "pre_compile_gating", "graph gating", "index gating",
    ):
        assert forbidden not in src
