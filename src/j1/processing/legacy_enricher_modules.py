"""Wave 10.5 + 10.6 — legacy-compatible `EnrichmentModule`
adapters for the post-compile enrichment stage.

These adapters MIGRATE the legacy LLM-backed enrichment behaviour
(text / classification / table / image) onto the new typed module
protocol. They DO NOT invoke the legacy enricher classes in
`j1/enrichers.py` directly — instead they re-implement the same
prompt + JSON-schema vocabulary against the new typed analysis-
client contracts in `enrichment_clients.py`. The result is the
same operator-facing output (classifications, table summaries,
image summaries, retrieval hints), now produced through the
protocol-based pipeline so domain packs + the shared LLM-call
limiter both reach them.

What each adapter provides:

  * `module_id` + `can_run(ctx)` + `run(ctx)` — the Wave-6
    `EnrichmentModule` protocol, so the adapter slots into
    `CompositeEnrichmentRunner` next to the existing skeleton
    modules.
  * Prompt resolution through `resolve_module_prompt(domain_pack,
    prompt_field, builtin_default)` so `DomainPromptPack`
    overrides + `prompt_addon` reach the model.
  * LLM call routing through the shared `LLMCallLimiter` (Wave 7).
    When the limiter is None, calls bypass the gate — same
    behaviour the legacy `CompositeEnricher` already has.
  * Typed output projection: `ClassificationResult`,
    `TableSummary`, `ImageSummary`, `retrieval_hints[]`,
    `confidence_notes[]` — each carrying explicit
    `ProvenanceLink`s back to the source compile artifact.

Skip behaviour: every adapter short-circuits when its required
input is missing — no text → text/classification skip; no
detected tables → table skip; no detected images → image skip;
no analysis client wired → all four skip with the same "no LLM
client configured" reason. The runner records each skip as a
SKIPPED outcome with the adapter's reason. Final ingestion
reports surface these as SKIPPED module outcomes so missing
clients are NEVER silent.

Failure behaviour: an adapter that raises mid-`run()` is caught
by the runner (existing Wave-6 behaviour). When the active
policy is `require_enrichment_success=True` and the adapter's
outcome is FAILED, the workflow surfaces it as
`failed_enrichment_required`; otherwise the run completes with
`completed_with_enrichment_warnings`.

The adapters are NOT frozen dataclasses — they cache the typed
outputs of the most recent `run()` so the runner can pick them
up via `get_typed_outputs()`. This keeps `EnrichmentModuleOutcome`
small + serialisable and avoids re-running the LLM inside the
runner's projection step.

Terminology note: this module previously described itself as
"wrappers". They are more precisely PROTOCOL-BASED ADAPTERS:
they don't wrap a legacy class instance; they speak the new
protocol while preserving the legacy prompt + schema contracts.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

from j1.domains.models import DomainPack
from j1.processing.enrichment_modules import resolve_module_prompt
from j1.processing.enrichment_overlay import (
    ClassificationResult,
    EnrichmentModuleOutcome,
    EnrichmentModuleStatus,
    ImageSummary,
    ModelUsageRecord,
    ProvenanceLink,
    TableSummary,
)


__all__ = [
    "MODULE_ID_TEXT_ENRICHMENT",
    "MODULE_ID_CLASSIFICATION_ENRICHMENT",
    "MODULE_ID_TABLE_ENRICHMENT",
    "MODULE_ID_IMAGE_ENRICHMENT",
    "TextEnrichmentModule",
    "ClassificationEnrichmentModule",
    "TableEnrichmentModule",
    "ImageEnrichmentModule",
    "build_legacy_enricher_modules",
    # Re-exported prompts (tests pin against these — drift between
    # the legacy file + the wrappers is loud).
    "DEFAULT_TEXT_ENRICHMENT_PROMPT",
    "DEFAULT_CLASSIFICATION_PROMPT",
    "DEFAULT_TABLE_ENRICHMENT_PROMPT",
    "DEFAULT_IMAGE_ENRICHMENT_PROMPT",
]


# ---- Stable module ids (mirror the post-compile enrich plan task
# vocabulary in `enrich_assessment.py` where they overlap) ---------

MODULE_ID_TEXT_ENRICHMENT = "text_enrichment"
MODULE_ID_CLASSIFICATION_ENRICHMENT = "classification_enrichment"
# Matches `TASK_TABLE_ENRICHMENT` in `enrich_assessment.py`.
MODULE_ID_TABLE_ENRICHMENT = "table_enrichment"
# Matches `TASK_VISION_ENRICHMENT` (image_captioning is a deprecated
# alias kept for old plan payloads — the wrapper handles both).
MODULE_ID_IMAGE_ENRICHMENT = "image_enrichment"


# ---- Default prompts (operator-friendly, NOT domain-specific) ----
#
# These are the LEGACY default prompts pulled from
# `j1/enrichers.py` so wrappers + legacy enricher classes stay in
# lockstep. A test asserts each starts with "Extract" / "Classify"
# / "Summarise" so a future rewrite that introduces a structural
# divergence trips immediately.

DEFAULT_TEXT_ENRICHMENT_PROMPT = (
    "Extract key requirements, obligations, and constraints from "
    "the document. Return JSON with a `requirements[]` array; each "
    "entry MUST have `id`, `text`, `priority` (one of MUST / SHOULD "
    "/ MAY / informative), and optional `section` and `page` fields. "
    "Confidence flags + ambiguity notes go in a sibling "
    "`confidence_notes[]` array (operator-readable strings). The "
    "model MUST NOT invent requirements not present in the source."
)

DEFAULT_CLASSIFICATION_PROMPT = (
    "Classify the document. Return JSON with `category` (single "
    "top-level type), optional `subcategory`, `confidence` (0..1 "
    "float), and a `candidates[]` array of (category, confidence) "
    "pairs ordered by descending confidence. Include a short "
    "`reasoning` string. Use the document's title, sections, and "
    "early-page text — never invent classifications outside the "
    "evidence."
)

DEFAULT_TABLE_ENRICHMENT_PROMPT = (
    "Summarise each detected table in the document. For every "
    "table, return JSON with a `tables[]` array; each entry MUST "
    "have `table_id`, optional `title`, a one-sentence `summary` "
    "describing what the table conveys, `column_names[]`, and "
    "`row_count`. Do not transcribe the table cells — only "
    "summarise."
)

DEFAULT_IMAGE_ENRICHMENT_PROMPT = (
    "Describe each detected image / figure / diagram in the "
    "document. For every image, return JSON with an `images[]` "
    "array; each entry MUST have `image_id`, a one-sentence "
    "`caption`, an optional `role` (one of figure / diagram / "
    "photograph / chart / icon / decorative), and a `confidence` "
    "(0..1 float). Decorative / icon-class images should be "
    "marked as such — do not fabricate detail."
)


# ---- JSON output schemas (passed to the text/vision clients) -----
#
# Kept loose — the wrappers tolerate extra keys + missing keys
# gracefully so a model that returns slightly different shape
# still produces useful typed records.

_TEXT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "priority": {"type": "string"},
                    "section": {"type": "string"},
                    "page": {"type": "integer"},
                },
            },
        },
        "confidence_notes": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}

_CLASSIFICATION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {"type": "string"},
        "subcategory": {"type": "string"},
        "confidence": {"type": "number"},
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        },
        "reasoning": {"type": "string"},
    },
}

_TABLE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "table_id": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "column_names": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "row_count": {"type": "integer"},
                },
            },
        },
    },
}

_IMAGE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "images": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "image_id": {"type": "string"},
                    "caption": {"type": "string"},
                    "role": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        },
    },
}


# ---- Wrapper base ------------------------------------------------


class _LegacyWrapperBase:
    """Shared wiring for legacy-enricher wrappers.

    Carries the LLM client + limiter + per-run typed output cache.
    Subclasses set `module_id`, `_PROMPT_FIELD`, `_BUILTIN_PROMPT`,
    `_OUTPUT_SCHEMA`, and implement `can_run` + `run`."""

    module_id: str = "legacy_wrapper"
    _PROMPT_FIELD: str = ""
    _BUILTIN_PROMPT: str = ""
    _OUTPUT_SCHEMA: dict[str, Any] = {}

    def __init__(
        self,
        *,
        text_client: object | None = None,
        vision_client: object | None = None,
        llm_call_limiter: object | None = None,
    ) -> None:
        self._text_client = text_client
        self._vision_client = vision_client
        self._llm_call_limiter = llm_call_limiter
        # Per-run typed output cache — the runner reads this via
        # `get_typed_outputs()` after `run()` returns. Reset at the
        # start of each `run()`.
        self._typed_outputs: dict[str, Any] = {}

    def _resolve_prompt(self, domain_pack: DomainPack | None) -> str:
        return resolve_module_prompt(
            domain_pack=domain_pack,
            prompt_field=self._PROMPT_FIELD,
            builtin_default=self._BUILTIN_PROMPT,
        )

    def _llm_call(
        self,
        callable_: Any,
        *args: Any,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[Any, Any]:
        """Route an LLM call through the shared limiter when wired.

        The limiter's `run()` returns the wrapped callable's value
        unchanged; we unpack the (parsed, usage) tuple the
        text/vision clients return either way."""
        if self._llm_call_limiter is not None:
            return self._llm_call_limiter.run(
                callable_, *args, metadata=metadata or {},
            )
        return callable_(*args)

    def get_typed_outputs(self) -> dict[str, Any]:
        """Return the typed records produced by the most recent
        `run()` so `CompositeEnrichmentRunner` can merge them into
        the aggregated `EnrichmentResult`. Returns an empty dict
        when `run()` hasn't been called or produced no output."""
        return dict(self._typed_outputs)

    def _make_provenance(self, ctx: Any) -> ProvenanceLink:
        first_raw = (
            ctx.compile_result.raw_artifact_refs[0]
            if ctx.compile_result.raw_artifact_refs
            else None
        )
        return ProvenanceLink(
            source_artifact_id=first_raw,
            source_kind="compile",
            relation="extracted_from",
        )


# ---- Text enrichment wrapper -------------------------------------


class TextEnrichmentModule(_LegacyWrapperBase):
    """Wraps the requirement-extraction style of text enrichment
    into the `EnrichmentModule` protocol.

    Output projection:
      * Each extracted requirement's text → `retrieval_hints[]`.
      * Each `confidence_notes[]` entry → `confidence_notes[]`.
    """

    module_id: str = MODULE_ID_TEXT_ENRICHMENT
    _PROMPT_FIELD = "text_enrichment_prompt"
    _BUILTIN_PROMPT = DEFAULT_TEXT_ENRICHMENT_PROMPT
    _OUTPUT_SCHEMA = _TEXT_OUTPUT_SCHEMA

    def can_run(self, ctx: Any) -> tuple[bool, str]:
        if self._text_client is None:
            return False, "no text LLM client configured"
        chars = ctx.compile_result.extracted_text_chars or 0
        if chars <= 0:
            return False, "compile produced no extracted text"
        if (ctx.compile_result.chunks_count or 0) == 0:
            return False, "compile produced no chunks"
        return True, "text available for enrichment"

    def run(self, ctx: Any) -> EnrichmentModuleOutcome:
        self._typed_outputs = {}
        started = perf_counter()
        prompt = self._resolve_prompt(ctx.domain_pack)
        try:
            parsed, usage = self._llm_call(
                self._text_client.extract,
                prompt, self._OUTPUT_SCHEMA,
                metadata={
                    "module_id": self.module_id,
                    "document_id": ctx.document_id,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return EnrichmentModuleOutcome(
                module_id=self.module_id,
                status=EnrichmentModuleStatus.FAILED,
                reason=f"text enrichment LLM call failed: {type(exc).__name__}",
                duration_ms=int((perf_counter() - started) * 1000),
                errors=(str(exc),),
            )

        hints: list[str] = []
        notes: list[str] = []
        for req in (parsed or {}).get("requirements") or []:
            if not isinstance(req, dict):
                continue
            text = (req.get("text") or "").strip()
            if text:
                hints.append(text[:200])
        for note in (parsed or {}).get("confidence_notes") or []:
            text = str(note).strip()
            if text:
                notes.append(text)

        provenance = self._make_provenance(ctx)
        self._typed_outputs = {
            "retrieval_hints": tuple(hints),
            "confidence_notes": tuple(notes),
        }
        return EnrichmentModuleOutcome(
            module_id=self.module_id,
            status=EnrichmentModuleStatus.RUN,
            reason=(
                f"extracted {len(hints)} retrieval hint(s) and "
                f"{len(notes)} confidence note(s) from compile text"
            ),
            duration_ms=int((perf_counter() - started) * 1000),
            source_refs=(provenance,),
            model_usage=_model_usage_from(usage, role="text"),
        )


# ---- Classification wrapper --------------------------------------


class ClassificationEnrichmentModule(_LegacyWrapperBase):
    """Wraps the document-classifier enricher.

    Output projection: a single `ClassificationResult` carrying
    category + subcategory + confidence + candidate list."""

    module_id: str = MODULE_ID_CLASSIFICATION_ENRICHMENT
    _PROMPT_FIELD = "classification_prompt"
    _BUILTIN_PROMPT = DEFAULT_CLASSIFICATION_PROMPT
    _OUTPUT_SCHEMA = _CLASSIFICATION_OUTPUT_SCHEMA

    def can_run(self, ctx: Any) -> tuple[bool, str]:
        if self._text_client is None:
            return False, "no text LLM client configured"
        chars = ctx.compile_result.extracted_text_chars or 0
        if chars <= 0:
            return False, "compile produced no extracted text"
        return True, "text available for classification"

    def run(self, ctx: Any) -> EnrichmentModuleOutcome:
        self._typed_outputs = {}
        started = perf_counter()
        prompt = self._resolve_prompt(ctx.domain_pack)
        try:
            parsed, usage = self._llm_call(
                self._text_client.extract,
                prompt, self._OUTPUT_SCHEMA,
                metadata={
                    "module_id": self.module_id,
                    "document_id": ctx.document_id,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return EnrichmentModuleOutcome(
                module_id=self.module_id,
                status=EnrichmentModuleStatus.FAILED,
                reason=(
                    f"classification LLM call failed: {type(exc).__name__}"
                ),
                duration_ms=int((perf_counter() - started) * 1000),
                errors=(str(exc),),
            )

        parsed = parsed or {}
        provenance = self._make_provenance(ctx)
        category = _optional_str(parsed.get("category"))
        if category is None:
            return EnrichmentModuleOutcome(
                module_id=self.module_id,
                status=EnrichmentModuleStatus.PARTIAL,
                reason="classifier produced no top-level category",
                duration_ms=int((perf_counter() - started) * 1000),
                source_refs=(provenance,),
                model_usage=_model_usage_from(usage, role="text"),
                warnings=("classifier output missing `category`",),
            )

        result = ClassificationResult(
            category=category,
            subcategory=_optional_str(parsed.get("subcategory")),
            confidence=_optional_float(parsed.get("confidence")),
            candidates=_extract_candidates(parsed.get("candidates")),
            reasoning=_optional_str(parsed.get("reasoning")),
            provenance=provenance,
        )
        self._typed_outputs = {"classification_result": result}
        return EnrichmentModuleOutcome(
            module_id=self.module_id,
            status=EnrichmentModuleStatus.RUN,
            reason=f"classified as {result.category!r}",
            duration_ms=int((perf_counter() - started) * 1000),
            source_refs=(provenance,),
            model_usage=_model_usage_from(usage, role="text"),
        )


# ---- Table enrichment wrapper ------------------------------------


class TableEnrichmentModule(_LegacyWrapperBase):
    """Wraps the table-extractor enricher.

    Output projection: one `TableSummary` per detected table.
    Skips when `compile_result.detected_tables` is empty so the
    operator gets a clear "no tables detected" reason instead of
    an "LLM said nothing" outcome."""

    module_id: str = MODULE_ID_TABLE_ENRICHMENT
    _PROMPT_FIELD = "table_enrichment_prompt"
    _BUILTIN_PROMPT = DEFAULT_TABLE_ENRICHMENT_PROMPT
    _OUTPUT_SCHEMA = _TABLE_OUTPUT_SCHEMA

    def can_run(self, ctx: Any) -> tuple[bool, str]:
        if self._text_client is None:
            return False, "no text LLM client configured"
        if not ctx.compile_result.detected_tables:
            return False, "compile detected no tables"
        return True, (
            f"{len(ctx.compile_result.detected_tables)} table(s) "
            "available for enrichment"
        )

    def run(self, ctx: Any) -> EnrichmentModuleOutcome:
        self._typed_outputs = {}
        started = perf_counter()
        prompt = self._resolve_prompt(ctx.domain_pack)
        try:
            parsed, usage = self._llm_call(
                self._text_client.extract,
                prompt, self._OUTPUT_SCHEMA,
                metadata={
                    "module_id": self.module_id,
                    "document_id": ctx.document_id,
                    "table_count": len(ctx.compile_result.detected_tables),
                },
            )
        except Exception as exc:  # noqa: BLE001
            return EnrichmentModuleOutcome(
                module_id=self.module_id,
                status=EnrichmentModuleStatus.FAILED,
                reason=f"table enrichment LLM call failed: {type(exc).__name__}",
                duration_ms=int((perf_counter() - started) * 1000),
                errors=(str(exc),),
            )

        parsed = parsed or {}
        provenance = self._make_provenance(ctx)
        summaries: list[TableSummary] = []
        for raw in parsed.get("tables") or []:
            if not isinstance(raw, dict):
                continue
            table_id = _optional_str(raw.get("table_id"))
            if not table_id:
                continue
            summaries.append(TableSummary(
                table_id=table_id,
                title=_optional_str(raw.get("title")),
                summary=_optional_str(raw.get("summary")),
                column_names=_str_tuple(raw.get("column_names")),
                row_count=_optional_int(raw.get("row_count")),
                provenance=provenance,
            ))

        if not summaries:
            return EnrichmentModuleOutcome(
                module_id=self.module_id,
                status=EnrichmentModuleStatus.PARTIAL,
                reason="LLM produced no parseable table summaries",
                duration_ms=int((perf_counter() - started) * 1000),
                source_refs=(provenance,),
                model_usage=_model_usage_from(usage, role="text"),
                warnings=("LLM returned `tables=[]` for detected tables",),
            )

        self._typed_outputs = {"table_summaries": tuple(summaries)}
        return EnrichmentModuleOutcome(
            module_id=self.module_id,
            status=EnrichmentModuleStatus.RUN,
            reason=f"summarised {len(summaries)} table(s)",
            duration_ms=int((perf_counter() - started) * 1000),
            source_refs=(provenance,),
            model_usage=_model_usage_from(usage, role="text"),
        )


# ---- Image enrichment wrapper ------------------------------------


class ImageEnrichmentModule(_LegacyWrapperBase):
    """Wraps the visual-content-describer enricher.

    Output projection: one `ImageSummary` per detected image.
    Routes through the vision LLM client (text client is unused).
    Skips cleanly when `compile_result.detected_images` is empty."""

    module_id: str = MODULE_ID_IMAGE_ENRICHMENT
    _PROMPT_FIELD = "image_enrichment_prompt"
    _BUILTIN_PROMPT = DEFAULT_IMAGE_ENRICHMENT_PROMPT
    _OUTPUT_SCHEMA = _IMAGE_OUTPUT_SCHEMA

    def can_run(self, ctx: Any) -> tuple[bool, str]:
        if self._vision_client is None:
            return False, "no vision LLM client configured"
        if not ctx.compile_result.detected_images:
            return False, "compile detected no images"
        return True, (
            f"{len(ctx.compile_result.detected_images)} image(s) "
            "available for enrichment"
        )

    def run(self, ctx: Any) -> EnrichmentModuleOutcome:
        self._typed_outputs = {}
        started = perf_counter()
        prompt = self._resolve_prompt(ctx.domain_pack)
        try:
            # The vision client speaks the `VisionAnalysisClient`
            # Protocol — `analyze(prompt, schema, metadata)`
            # returning a JSON dict + usage. Production vision LLMs
            # don't natively expose that shape; the activity wires
            # a `PerImageVisionAdapter` around the per-image
            # `VisionLLMClient`.
            #
            # Wave 11B — the adapter owns the per-image limiter
            # acquisition. We DO NOT wrap the outer `analyze` call
            # with our own limiter (`_llm_call`) — that would
            # double-acquire (one outer + one per image). Calling
            # the adapter directly lets it gate each per-image
            # vision call with its own semaphore slot.
            parsed, usage = self._vision_client.analyze(
                prompt, self._OUTPUT_SCHEMA,
                metadata={
                    "module_id": self.module_id,
                    "document_id": ctx.document_id,
                    "image_count": len(ctx.compile_result.detected_images),
                },
            )
        except Exception as exc:  # noqa: BLE001
            return EnrichmentModuleOutcome(
                module_id=self.module_id,
                status=EnrichmentModuleStatus.FAILED,
                reason=f"image enrichment LLM call failed: {type(exc).__name__}",
                duration_ms=int((perf_counter() - started) * 1000),
                errors=(str(exc),),
            )

        parsed = parsed or {}
        provenance = self._make_provenance(ctx)
        summaries: list[ImageSummary] = []
        for raw in parsed.get("images") or []:
            if not isinstance(raw, dict):
                continue
            image_id = _optional_str(raw.get("image_id"))
            if not image_id:
                continue
            summaries.append(ImageSummary(
                image_id=image_id,
                caption=_optional_str(raw.get("caption")),
                role=_optional_str(raw.get("role")),
                confidence=_optional_float(raw.get("confidence")),
                provenance=provenance,
            ))

        if not summaries:
            return EnrichmentModuleOutcome(
                module_id=self.module_id,
                status=EnrichmentModuleStatus.PARTIAL,
                reason="LLM produced no parseable image summaries",
                duration_ms=int((perf_counter() - started) * 1000),
                source_refs=(provenance,),
                model_usage=_model_usage_from(usage, role="vision"),
                warnings=("LLM returned `images=[]` for detected images",),
            )

        self._typed_outputs = {"image_summaries": tuple(summaries)}
        return EnrichmentModuleOutcome(
            module_id=self.module_id,
            status=EnrichmentModuleStatus.RUN,
            reason=f"described {len(summaries)} image(s)",
            duration_ms=int((perf_counter() - started) * 1000),
            source_refs=(provenance,),
            model_usage=_model_usage_from(usage, role="vision"),
        )


# ---- Factory -----------------------------------------------------


def build_legacy_enricher_modules(
    *,
    text_client: object | None = None,
    vision_client: object | None = None,
    llm_call_limiter: object | None = None,
) -> list[Any]:
    """Return the four legacy-wrapper modules wired with the
    deployment's text + vision clients + shared limiter.

    Activities call this at construction time; when any client is
    None, the matching wrappers still construct cleanly — their
    `can_run()` returns False with `"no LLM client configured"`
    so the runner records them as SKIPPED. This keeps the worker
    safe to construct in dev / test environments without real LLM
    credentials."""
    return [
        TextEnrichmentModule(
            text_client=text_client,
            llm_call_limiter=llm_call_limiter,
        ),
        ClassificationEnrichmentModule(
            text_client=text_client,
            llm_call_limiter=llm_call_limiter,
        ),
        TableEnrichmentModule(
            text_client=text_client,
            llm_call_limiter=llm_call_limiter,
        ),
        ImageEnrichmentModule(
            vision_client=vision_client,
            llm_call_limiter=llm_call_limiter,
        ),
    ]


# ---- Parsing helpers ---------------------------------------------


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(v) for v in value if v is not None)


def _extract_candidates(
    value: object,
) -> tuple[tuple[str, float], ...]:
    if not isinstance(value, list):
        return ()
    out: list[tuple[str, float]] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        cat = _optional_str(entry.get("category"))
        score = _optional_float(entry.get("confidence"))
        if cat is None or score is None:
            continue
        out.append((cat, score))
    return tuple(out)


def _model_usage_from(usage: object, *, role: str) -> ModelUsageRecord:
    """Project the legacy LLM client's `usage` return into a typed
    `ModelUsageRecord`. The text/vision clients return objects with
    `model`, `input_tokens`, `output_tokens`, etc. fields — we read
    via `getattr` so the helper tolerates either dict or attr
    access without coupling to a specific client type."""
    if usage is None:
        return ModelUsageRecord(role=role)

    def _read(key: str) -> object | None:
        if isinstance(usage, dict):
            return usage.get(key)
        return getattr(usage, key, None)

    model = _optional_str(_read("model"))
    provider = _optional_str(_read("provider"))
    in_tok = _optional_int(_read("input_tokens"))
    out_tok = _optional_int(_read("output_tokens"))
    duration = _optional_int(_read("duration_ms"))
    return ModelUsageRecord(
        model=model,
        provider=provider,
        role=role,
        input_tokens=in_tok,
        output_tokens=out_tok,
        duration_ms=duration,
    )
