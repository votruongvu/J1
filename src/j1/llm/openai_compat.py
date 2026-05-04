"""OpenAI-compatible HTTP clients.

Talks to any provider that implements the OpenAI REST surface
(`/chat/completions`, `/embeddings`). Uses `httpx` (already a dev /
runtime dep — pulled in by FastAPI's TestClient).

Why hand-rolled and not the `openai` SDK? Two reasons: (a) keeps the
framework's runtime dep set minimal, and (b) lets us treat any
OpenAI-compatible endpoint identically — vLLM, Together, Ollama,
Azure, DashScope, etc. — without per-vendor branches.

Retries: simple linear retry on `httpx.TimeoutException` /
`httpx.TransportError` / 5xx responses. No exponential backoff
(deployments wanting that wrap the client in a `tenacity`-style
retry; that's adapter-side concern).
"""

from __future__ import annotations

import base64
import json as _json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from j1.llm.clients import EmbeddingClient, LLMCapabilityError, LLMUsage
from j1.llm.errors import LLMConfigError, LLMProviderUnavailable
from j1.llm.settings import (
    EmbeddingSettings,
    PROVIDER_OPENAI_COMPAT,
    TextLLMSettings,
    VisionLLMSettings,
)

_USER_AGENT = "j1-llm/0.1"


class _BaseOpenAICompatClient:
    """Shared HTTP plumbing for the three role-specific subclasses."""

    def __init__(self, *, settings) -> None:
        if not settings.base_url:
            raise LLMConfigError(
                f"{type(self).__name__} requires a base_url"
            )
        if not settings.model:
            raise LLMConfigError(
                f"{type(self).__name__} requires a model"
            )
        self._settings = settings

    @property
    def provider(self) -> str:
        return PROVIDER_OPENAI_COMPAT

    @property
    def model(self) -> str:
        return self._settings.model

    def _post(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        # Lazy import — httpx is in `[dev]` extras but should be
        # available wherever the framework actually runs.
        try:
            import httpx
        except ImportError as exc:
            raise LLMProviderUnavailable(
                "httpx is not installed; install j1[dev] or "
                "add httpx to your environment"
            ) from exc

        url = self._settings.base_url.rstrip("/") + path
        headers = {
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"

        attempts = max(1, self._settings.max_retries + 1)
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                response = httpx.post(
                    url,
                    headers=headers,
                    json=dict(body),
                    timeout=self._settings.timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt + 1 == attempts:
                    raise LLMProviderUnavailable(
                        f"{self._settings.provider} call to {url} failed "
                        f"after {attempts} attempt(s): {type(exc).__name__}"
                    ) from exc
                continue

            if response.status_code >= 500 and attempt + 1 < attempts:
                last_exc = RuntimeError(f"http {response.status_code}")
                continue
            if response.status_code >= 400:
                # Body may carry a useful error message — surface only
                # the upstream-supplied text, never our own headers.
                detail = (response.text or "")[:500]
                raise LLMProviderUnavailable(
                    f"{self._settings.provider} returned HTTP "
                    f"{response.status_code}: {detail}"
                )
            return response.json()
        raise LLMProviderUnavailable(  # pragma: no cover — defensive
            f"{self._settings.provider} call to {url} failed: {last_exc}"
        )


# ---- Text ----------------------------------------------------------


class OpenAICompatTextLLMClient(_BaseOpenAICompatClient):
    """Talks to /chat/completions on any OpenAI-compatible endpoint."""

    def __init__(self, settings: TextLLMSettings) -> None:
        super().__init__(settings=settings)

    def generate(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str, LLMUsage]:
        body = self._build_chat_body(
            prompt=prompt,
            system_prompt=system_prompt,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )
        response = self._post("/chat/completions", body)
        return _extract_text(response, self._settings.model, self.provider)

    def summarize(
        self,
        input_text: str,
        *,
        max_output_tokens: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str, LLMUsage]:
        return self.generate(
            f"Summarise the following content:\n\n{input_text}",
            max_output_tokens=max_output_tokens,
        )

    def extract(
        self,
        input_text: str,
        schema: Mapping[str, Any],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[Mapping[str, Any], LLMUsage]:
        body = self._build_chat_body(
            prompt=(
                "Extract the requested fields from the input text. "
                "Respond with a single JSON object matching this schema:\n"
                f"{_json.dumps(schema)}\n\n"
                f"Input:\n{input_text}"
            ),
            system_prompt="You return JSON only.",
            response_format="json",
        )
        response = self._post("/chat/completions", body)
        text, usage = _extract_text(response, self._settings.model, self.provider)
        try:
            parsed = _json.loads(text)
        except _json.JSONDecodeError as exc:
            raise LLMProviderUnavailable(
                f"extract() response was not valid JSON: {text[:200]!r}"
            ) from exc
        return parsed, usage

    def classify(
        self,
        input_text: str,
        labels: Sequence[str],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str, LLMUsage]:
        labels_str = ", ".join(labels)
        body = self._build_chat_body(
            prompt=(
                f"Classify the following input as exactly one of these "
                f"labels: {labels_str}.\n\n"
                f"Input:\n{input_text}\n\n"
                "Respond with only the chosen label, nothing else."
            ),
            system_prompt="You output a single label, nothing else.",
        )
        response = self._post("/chat/completions", body)
        text, usage = _extract_text(response, self._settings.model, self.provider)
        chosen = text.strip()
        # Best-effort match — if the model echoes the label cleanly we
        # use it as-is; if it didn't, return the raw text and let the
        # caller decide how strict to be.
        for label in labels:
            if chosen.lower() == label.lower():
                return label, usage
        return chosen, usage

    def _build_chat_body(
        self,
        *,
        prompt: str,
        system_prompt: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        response_format: str | None = None,
    ) -> dict[str, Any]:
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        body: dict[str, Any] = {
            "model": self._settings.model,
            "messages": messages,
            "temperature": temperature
            if temperature is not None
            else self._settings.temperature,
            "max_tokens": max_output_tokens or self._settings.max_output_tokens,
        }
        if response_format == "json":
            body["response_format"] = {"type": "json_object"}
        return body


# ---- Vision --------------------------------------------------------


class OpenAICompatVisionLLMClient(_BaseOpenAICompatClient):
    """OpenAI-compatible vision: sends image-bytes inline as base64 data URIs."""

    def __init__(self, settings: VisionLLMSettings) -> None:
        super().__init__(settings=settings)

    def analyze_image(
        self,
        image: bytes,
        *,
        prompt: str,
        media_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str, LLMUsage]:
        body = self._build_vision_body(
            image=image, prompt=prompt, media_type=media_type,
        )
        response = self._post("/chat/completions", body)
        return _extract_text(response, self._settings.model, self.provider)

    def analyze_page(
        self, image: bytes, *, prompt: str,
        media_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str, LLMUsage]:
        return self.analyze_image(
            image, prompt=prompt, media_type=media_type, metadata=metadata,
        )

    def describe_diagram(
        self, image: bytes, *, prompt: str = "Describe this diagram.",
        media_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str, LLMUsage]:
        return self.analyze_image(
            image, prompt=prompt, media_type=media_type, metadata=metadata,
        )

    def extract_visual_table(
        self, image: bytes, *, prompt: str = "Extract the table as JSON.",
        media_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[Mapping[str, Any], LLMUsage]:
        body = self._build_vision_body(
            image=image,
            prompt=(
                f"{prompt}\n\nRespond with a single JSON object containing "
                "a `headers` array and a `rows` array of arrays."
            ),
            media_type=media_type,
            response_format="json",
        )
        response = self._post("/chat/completions", body)
        text, usage = _extract_text(response, self._settings.model, self.provider)
        try:
            parsed = _json.loads(text)
        except _json.JSONDecodeError as exc:
            raise LLMProviderUnavailable(
                f"extract_visual_table() response was not valid JSON: "
                f"{text[:200]!r}"
            ) from exc
        return parsed, usage

    def _build_vision_body(
        self,
        *,
        image: bytes,
        prompt: str,
        media_type: str | None,
        response_format: str | None = None,
    ) -> dict[str, Any]:
        media_type = media_type or "image/png"
        b64 = base64.b64encode(image).decode("ascii")
        data_url = f"data:{media_type};base64,{b64}"
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }]
        body: dict[str, Any] = {
            "model": self._settings.model,
            "messages": messages,
            "temperature": self._settings.temperature,
            "max_tokens": self._settings.max_output_tokens,
        }
        if response_format == "json":
            body["response_format"] = {"type": "json_object"}
        return body


# ---- Embedding -----------------------------------------------------


class OpenAICompatEmbeddingClient(_BaseOpenAICompatClient, EmbeddingClient):
    def __init__(self, settings: EmbeddingSettings) -> None:
        super().__init__(settings=settings)
        self._dimension = settings.dimension
        self._max_tokens = settings.max_input_tokens
        self._batch_size = settings.batch_size

    def embed_text(self, text: str) -> tuple[list[float], LLMUsage]:
        vectors, usage = self.embed_batch([text])
        return vectors[0], usage

    def embed_batch(
        self, texts: Iterable[str]
    ) -> tuple[list[list[float]], LLMUsage]:
        items = list(texts)
        if not items:
            return [], LLMUsage(provider=self.provider, model=self.model)

        all_vectors: list[list[float]] = []
        usage_input = 0
        for start in range(0, len(items), self._batch_size):
            chunk = items[start:start + self._batch_size]
            response = self._post(
                "/embeddings",
                {"model": self._settings.model, "input": chunk},
            )
            for row in response.get("data", []):
                vec = row.get("embedding")
                if not isinstance(vec, list):
                    raise LLMProviderUnavailable(
                        "embedding response missing `embedding` array"
                    )
                all_vectors.append([float(x) for x in vec])
            usage_input += int(response.get("usage", {}).get("prompt_tokens", 0))

        return all_vectors, LLMUsage(
            provider=self.provider,
            model=self.model,
            input_tokens=usage_input,
            total_tokens=usage_input,
        )

    def dimension(self) -> int:
        if self._dimension is None:
            raise LLMCapabilityError(
                "Embedding dimension is not configured; set "
                "J1_EMBEDDING_DIM or override `EmbeddingSettings.dimension`."
            )
        return self._dimension

    def max_tokens(self) -> int:
        return self._max_tokens


# ---- Helpers --------------------------------------------------------


def _extract_text(
    response: Mapping[str, Any], model: str, provider: str,
) -> tuple[str, LLMUsage]:
    """Pull `choices[0].message.content` + `usage` from a standard response."""
    choices = response.get("choices") or []
    if not choices:
        raise LLMProviderUnavailable(
            f"{provider} response had no choices: {response!r}"
        )
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str):
        raise LLMProviderUnavailable(
            f"{provider} response message.content was not a string"
        )
    usage = response.get("usage") or {}
    return content, LLMUsage(
        provider=provider,
        model=model,
        input_tokens=int(usage.get("prompt_tokens", 0)),
        output_tokens=int(usage.get("completion_tokens", 0)),
        total_tokens=int(usage.get("total_tokens", 0)),
    )
