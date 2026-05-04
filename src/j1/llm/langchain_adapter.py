"""LangChain-backed implementations of the J1 LLM client Protocols.

LangChain is treated as an **optional** infrastructure adapter — never
a runtime dep. The adapters import `langchain_core` lazily at
construction; if the package isn't installed the constructor raises
`LLMProviderUnavailable` with a clear pip-install hint.

The adapter never returns LangChain-native objects to callers. The
core sees only `(text, LLMUsage)` / `(vector, LLMUsage)` tuples — same
as every other client.

Two construction paths:

  * `LangChain*Client(model_object, settings=...)` — deployment
    instantiates the LangChain model directly and injects it. Use
    when the model needs constructor logic the env can't capture.
  * `LangChain*Client.from_settings(settings)` — env-driven. The
    adapter reads `settings.provider_config["class"]` (a short alias
    like `"ChatOpenAI"` or fully-qualified `"langchain_openai:ChatOpenAI"`),
    resolves it via the safe class-loader, and instantiates with the
    rest of `provider_config` as kwargs. Use this when the env
    config IS sufficient (most cases).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from j1.llm.classloader import (
    resolve_chat_model,
    resolve_embedding_model,
)
from j1.llm.clients import EmbeddingClient, LLMCapabilityError, LLMUsage
from j1.llm.errors import LLMConfigError, LLMProviderUnavailable
from j1.llm.settings import (
    PROVIDER_LANGCHAIN,
    EmbeddingSettings,
    TextLLMSettings,
    VisionLLMSettings,
)


_CONFIG_CLASS_KEY = "class"


def _extract_class_and_kwargs(
    provider_config: Mapping[str, Any], *, role: str,
) -> tuple[str, dict[str, Any]]:
    """Pull the `class` field out of provider_config; rest are kwargs.

    `role` is used only for the error message ("text", "vision",
    "embedding"). The class spec MUST be present and non-empty —
    deployments that want a fully-pre-built model use the constructor
    overload that takes a model object directly.
    """
    if not provider_config:
        raise LLMConfigError(
            f"LangChain {role} provider requires a provider_config — set "
            f"J1_{role.upper()}_LLM_LANGCHAIN_CONFIG to a JSON object "
            f"with at least a `class` field, e.g. "
            f'{{"class": "ChatOpenAI", "model": "gpt-4o-mini"}}'
        )
    config = dict(provider_config)
    class_spec = config.pop(_CONFIG_CLASS_KEY, None)
    if not class_spec or not isinstance(class_spec, str):
        raise LLMConfigError(
            f"LangChain {role} provider_config must include a non-empty "
            f"`class` field (got {class_spec!r})"
        )
    return class_spec, config


def _require_langchain() -> None:
    """Verify `langchain_core` is importable; raise an actionable error if not."""
    try:
        import langchain_core  # noqa: F401
    except ImportError as exc:
        raise LLMProviderUnavailable(
            "LangChain support requires the `langchain-core` package "
            "(plus any vendor-specific package such as "
            "`langchain-openai`). Install with: "
            "pip install langchain-core langchain-openai"
        ) from exc


# ---- Text ----------------------------------------------------------


class LangChainTextLLMClient:
    """Wraps a LangChain `BaseChatModel` instance.

    Two construction paths:
      * `LangChainTextLLMClient(chat_model, settings=...)` — inject a
        pre-built model.
      * `LangChainTextLLMClient.from_settings(settings)` — auto-
        instantiate from `settings.provider_config["class"]` via the
        safe class-loader. Use when the env config is sufficient.
    """

    def __init__(
        self,
        chat_model: Any,
        *,
        settings: TextLLMSettings,
        model_name: str | None = None,
    ) -> None:
        _require_langchain()
        self._chat_model = chat_model
        self._settings = settings
        # Many LangChain chat models expose `.model_name` or `.model`;
        # fall back to settings.model when neither is present.
        self._model = (
            model_name
            or getattr(chat_model, "model_name", None)
            or getattr(chat_model, "model", None)
            or settings.model
            or "unknown"
        )

    @classmethod
    def from_settings(cls, settings: TextLLMSettings) -> "LangChainTextLLMClient":
        """Auto-instantiate the LangChain chat model from env config.

        Looks up `settings.provider_config["class"]` in
        `CHAT_MODEL_CATALOG` (or accepts a fully-qualified path) and
        passes the rest of `provider_config` as kwargs.
        """
        _require_langchain()
        class_spec, kwargs = _extract_class_and_kwargs(
            settings.provider_config, role="text",
        )
        # Apply settings-level defaults if the config didn't override
        # them — keeps short configs sane.
        kwargs.setdefault("temperature", settings.temperature)
        if settings.model and "model" not in kwargs and "model_name" not in kwargs:
            kwargs["model"] = settings.model
        if settings.max_output_tokens and "max_tokens" not in kwargs:
            kwargs["max_tokens"] = settings.max_output_tokens
        cls_obj = resolve_chat_model(class_spec)
        chat_model = cls_obj(**kwargs)
        return cls(chat_model, settings=settings)

    @property
    def provider(self) -> str:
        return PROVIDER_LANGCHAIN

    @property
    def model(self) -> str:
        return self._model

    def generate(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str, LLMUsage]:
        from langchain_core.messages import HumanMessage, SystemMessage

        messages: list = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))
        messages.append(HumanMessage(content=prompt))
        response = self._chat_model.invoke(messages)
        return _coerce_text_and_usage(response, self.provider, self.model)

    def summarize(
        self, input_text: str, *,
        max_output_tokens: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str, LLMUsage]:
        return self.generate(
            f"Summarise the following content:\n\n{input_text}",
            max_output_tokens=max_output_tokens,
        )

    def extract(
        self, input_text: str, schema: Mapping[str, Any], *,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[Mapping[str, Any], LLMUsage]:
        import json
        text, usage = self.generate(
            f"Extract the requested fields and respond with JSON only:\n"
            f"{json.dumps(dict(schema))}\n\nInput:\n{input_text}",
            system_prompt="You return JSON only.",
        )
        try:
            return json.loads(text), usage
        except json.JSONDecodeError as exc:
            raise LLMProviderUnavailable(
                f"extract() response was not valid JSON: {text[:200]!r}"
            ) from exc

    def classify(
        self, input_text: str, labels: Sequence[str], *,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str, LLMUsage]:
        labels_str = ", ".join(labels)
        text, usage = self.generate(
            f"Classify the following input as exactly one of: {labels_str}.\n\n"
            f"Input:\n{input_text}\n\nRespond with only the chosen label.",
            system_prompt="You output a single label, nothing else.",
        )
        chosen = text.strip()
        for label in labels:
            if chosen.lower() == label.lower():
                return label, usage
        return chosen, usage


# ---- Vision --------------------------------------------------------


class LangChainVisionLLMClient:
    """Wraps a LangChain chat model that supports multimodal messages."""

    def __init__(
        self,
        chat_model: Any,
        *,
        settings: VisionLLMSettings,
        model_name: str | None = None,
    ) -> None:
        _require_langchain()
        self._chat_model = chat_model
        self._settings = settings
        self._model = (
            model_name
            or getattr(chat_model, "model_name", None)
            or getattr(chat_model, "model", None)
            or settings.model
            or "unknown"
        )

    @classmethod
    def from_settings(cls, settings: VisionLLMSettings) -> "LangChainVisionLLMClient":
        """Same env-driven auto-instantiation as `LangChainTextLLMClient`.

        The vision LLM uses LangChain's chat-model API too — multimodal
        content goes in the message body, not a separate class — so the
        catalog and kwargs treatment are identical.
        """
        _require_langchain()
        class_spec, kwargs = _extract_class_and_kwargs(
            settings.provider_config, role="vision",
        )
        kwargs.setdefault("temperature", settings.temperature)
        if settings.model and "model" not in kwargs and "model_name" not in kwargs:
            kwargs["model"] = settings.model
        if settings.max_output_tokens and "max_tokens" not in kwargs:
            kwargs["max_tokens"] = settings.max_output_tokens
        cls_obj = resolve_chat_model(class_spec)
        chat_model = cls_obj(**kwargs)
        return cls(chat_model, settings=settings)

    @property
    def provider(self) -> str:
        return PROVIDER_LANGCHAIN

    @property
    def model(self) -> str:
        return self._model

    def analyze_image(
        self, image: bytes, *, prompt: str,
        media_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str, LLMUsage]:
        import base64
        from langchain_core.messages import HumanMessage

        media_type = media_type or "image/png"
        b64 = base64.b64encode(image).decode("ascii")
        message = HumanMessage(
            content=[
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{b64}"},
                },
            ]
        )
        response = self._chat_model.invoke([message])
        return _coerce_text_and_usage(response, self.provider, self.model)

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
        import json
        text, usage = self.analyze_image(
            image,
            prompt=(
                f"{prompt}\n\nRespond with a single JSON object containing "
                "a `headers` array and a `rows` array of arrays."
            ),
            media_type=media_type,
        )
        try:
            return json.loads(text), usage
        except json.JSONDecodeError as exc:
            raise LLMProviderUnavailable(
                f"extract_visual_table() response was not valid JSON: "
                f"{text[:200]!r}"
            ) from exc


# ---- Embedding -----------------------------------------------------


class LangChainEmbeddingClient(EmbeddingClient):
    """Wraps a LangChain `Embeddings` instance."""

    def __init__(
        self,
        embeddings: Any,
        *,
        settings: EmbeddingSettings,
        model_name: str | None = None,
    ) -> None:
        _require_langchain()
        self._embeddings = embeddings
        self._settings = settings
        self._model = (
            model_name
            or getattr(embeddings, "model", None)
            or getattr(embeddings, "model_name", None)
            or settings.model
            or "unknown"
        )

    @classmethod
    def from_settings(cls, settings: EmbeddingSettings) -> "LangChainEmbeddingClient":
        """Auto-instantiate from `settings.provider_config["class"]`.

        Looks up the class in `EMBEDDING_CATALOG` (or accepts a fully-
        qualified path).
        """
        _require_langchain()
        class_spec, kwargs = _extract_class_and_kwargs(
            settings.provider_config, role="embedding",
        )
        if settings.model and "model" not in kwargs and "model_name" not in kwargs:
            kwargs["model"] = settings.model
        cls_obj = resolve_embedding_model(class_spec)
        embeddings = cls_obj(**kwargs)
        return cls(embeddings, settings=settings)

    @property
    def provider(self) -> str:
        return PROVIDER_LANGCHAIN

    @property
    def model(self) -> str:
        return self._model

    def embed_text(self, text: str) -> tuple[list[float], LLMUsage]:
        vec = self._embeddings.embed_query(text)
        return [float(x) for x in vec], LLMUsage(
            provider=self.provider, model=self.model,
        )

    def embed_batch(
        self, texts: Iterable[str]
    ) -> tuple[list[list[float]], LLMUsage]:
        items = list(texts)
        if not items:
            return [], LLMUsage(provider=self.provider, model=self.model)
        vectors = self._embeddings.embed_documents(items)
        return [[float(x) for x in v] for v in vectors], LLMUsage(
            provider=self.provider, model=self.model,
        )

    def dimension(self) -> int:
        if self._settings.dimension is None:
            raise LLMCapabilityError(
                "LangChain embedding client has no configured dimension; "
                "set J1_EMBEDDING_DIM or pass it to EmbeddingSettings."
            )
        return self._settings.dimension

    def max_tokens(self) -> int:
        return self._settings.max_input_tokens


# ---- Helpers -------------------------------------------------------


def _coerce_text_and_usage(
    response: Any, provider: str, model: str,
) -> tuple[str, LLMUsage]:
    """LangChain `AIMessage` → (text, LLMUsage), tolerant of variants."""
    text = getattr(response, "content", None)
    if isinstance(text, list):
        # Multimodal response — concatenate text parts.
        text = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in text
        )
    if not isinstance(text, str):
        text = str(response)

    metadata = getattr(response, "usage_metadata", None) or {}
    return text, LLMUsage(
        provider=provider,
        model=model,
        input_tokens=int(metadata.get("input_tokens", 0)),
        output_tokens=int(metadata.get("output_tokens", 0)),
        total_tokens=int(metadata.get("total_tokens", 0)),
    )
