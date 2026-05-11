import json
from collections.abc import Callable
from typing import Any

from j1.processing.contracts import ModelProvider
from j1.processing.results import ArtifactDraft, ArtifactProcessingResult
from j1.processing.status import ResultStatus
from j1.profiles.model import Profile
from j1.projects.context import ProjectContext

DEFAULT_PROCESSOR_VERSION = "0.1.0"

ARTIFACT_TYPE_DOCUMENT_MAP = "enriched.document_map"
ARTIFACT_TYPE_REQUIREMENTS = "enriched.requirements"
ARTIFACT_TYPE_TABLES = "enriched.tables"
ARTIFACT_TYPE_VISUALS = "enriched.visuals"
ARTIFACT_TYPE_FORMULAS = "enriched.formulas"
ARTIFACT_TYPE_RISKS = "enriched.risks"
ARTIFACT_TYPE_CONSISTENCY_FINDINGS = "enriched.consistency_findings"
ARTIFACT_TYPE_SOURCE_MAP = "enriched.source_map"
ARTIFACT_TYPE_CONFIDENCE_ASSESSMENT = "enriched.confidence_assessment"

PROCESSOR_DOCUMENT_CLASSIFIER = "enricher.document_classifier"
PROCESSOR_REQUIREMENT_EXTRACTOR = "enricher.requirement_extractor"
PROCESSOR_TABLE_EXTRACTOR = "enricher.table_extractor"
PROCESSOR_VISUAL_DESCRIBER = "enricher.visual_describer"
PROCESSOR_FORMULA_EXTRACTOR = "enricher.formula_extractor"
PROCESSOR_RISK_EXTRACTOR = "enricher.risk_extractor"
PROCESSOR_CONSISTENCY_CHECKER = "enricher.consistency_checker"
PROCESSOR_SOURCE_MAPPER = "enricher.source_mapper"
PROCESSOR_CONFIDENCE_ASSESSOR = "enricher.confidence_assessor"


class _StructuredEnricher:
    """Base class for generic enrichment processors.

    Subclasses set: kind, artifact_type, prompt_name, confidence_default,
    review_required_default, and override _produce(ctx, artifact_id) to return
    (json_data, markdown_text). Profiles supply prompts and config; processors
    themselves carry no domain logic.
    """

    kind: str = "enricher.base"
    artifact_type: str = "enriched.unknown"
    prompt_name: str = ""
    version: str = DEFAULT_PROCESSOR_VERSION
    confidence_default: float = 0.5
    review_required_default: bool = False
    formats: tuple[str, ...] = ("json", "md")

    def __init__(
        self,
        profile: Profile,
        *,
        enabled: bool = True,
        version: str | None = None,
        confidence: float | None = None,
        review_required: bool | None = None,
        content_source: Callable[[ProjectContext, str], bytes] | None = None,
        model: ModelProvider | None = None,
        text_client: Any | None = None,
        embedding_client: Any | None = None,
        domain_prompt_addon: str = "",
        domain_id: str | None = None,
    ) -> None:
        self._profile = profile
        self._enabled = enabled
        if version is not None:
            self.version = version
        if confidence is not None:
            self.confidence_default = confidence
        if review_required is not None:
            self.review_required_default = review_required
        self._content_source = content_source
        # `_model` is the legacy slot kept for any deployment that
        # already wires a `ModelProvider`. New work should prefer
        # `_text_client` / `_embedding_client` which are populated
        # from the project-wide `LLMProviderRegistry`.
        self._model = model
        self._text_client = text_client
        self._embedding_client = embedding_client
        # Domain-pack prompt augmentation. Empty by default; when set,
        # the LLM-backed enrichers prepend it to the per-enricher
        # prompt so domain-specific guidance reaches the model.
        # See `j1.domains.models.DomainPack.prompt_addon`. `domain_id`
        # is recorded on every artifact's metadata for provenance.
        self._domain_prompt_addon = domain_prompt_addon.strip()
        self._domain_id = domain_id

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def profile(self) -> Profile:
        return self._profile

    def enrich(
        self, ctx: ProjectContext, artifact_id: str
    ) -> ArtifactProcessingResult:
        if not self._enabled:
            return ArtifactProcessingResult(
                status=ResultStatus.SKIPPED,
                message=f"{self.kind} disabled",
                metadata={"processor_name": self.kind},
            )
        try:
            json_data, md_text = self._produce(ctx, artifact_id)
        except Exception as exc:
            return ArtifactProcessingResult(
                status=ResultStatus.FAILED,
                error=str(exc),
                message=type(exc).__name__,
                metadata={"processor_name": self.kind},
            )
        drafts = self._build_drafts(artifact_id, json_data, md_text)
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=drafts,
            metadata={"processor_name": self.kind, "processor_version": self.version},
        )

    def _produce(
        self, ctx: ProjectContext, artifact_id: str
    ) -> tuple[dict[str, Any], str]:
        raise NotImplementedError

    def _build_drafts(
        self,
        artifact_id: str,
        json_data: dict[str, Any],
        md_text: str,
    ) -> list[ArtifactDraft]:
        meta = self._build_metadata(artifact_id)
        drafts: list[ArtifactDraft] = []
        if "json" in self.formats:
            drafts.append(
                ArtifactDraft(
                    kind=self.artifact_type,
                    content=json.dumps(json_data, indent=2, default=str).encode(
                        "utf-8"
                    ),
                    suggested_extension=".json",
                    source_artifact_ids=[artifact_id],
                    metadata={**meta, "format": "json"},
                    review_required=self.review_required_default,
                )
            )
        if "md" in self.formats:
            drafts.append(
                ArtifactDraft(
                    kind=self.artifact_type,
                    content=md_text.encode("utf-8"),
                    suggested_extension=".md",
                    source_artifact_ids=[artifact_id],
                    metadata={**meta, "format": "markdown"},
                    review_required=self.review_required_default,
                )
            )
        return drafts

    def _build_metadata(self, artifact_id: str) -> dict[str, str]:
        meta: dict[str, str] = {
            "processor_name": self.kind,
            "processor_version": self.version,
            "artifact_type": self.artifact_type,
            "confidence": f"{self.confidence_default:.3f}",
            "review_required": "true" if self.review_required_default else "false",
            "source_artifact_id": artifact_id,
            "prompt_name": self.prompt_name,
        }
        # Provenance trail: when a domain pack augmented the prompt
        # for this run, record its id + an addon-applied flag on the
        # artifact metadata so reviewers can see which packs shaped
        # which enriched outputs.
        if self._domain_id:
            meta["domain_id"] = self._domain_id
        if self._domain_prompt_addon:
            meta["domain_prompt_addon_applied"] = "true"
        return meta

    def _read_content(self, ctx: ProjectContext, artifact_id: str) -> bytes:
        if self._content_source is None:
            return b""
        return self._content_source(ctx, artifact_id)

    def _profile_prompt(self) -> str:
        if not self.prompt_name:
            return ""
        return self._profile.prompts.get(self.prompt_name, "")


# ---- LLM-backed enricher base class --------------------------------
#
# The next 6 classes (DocumentClassifier / RequirementExtractor /
# TableExtractor / FormulaExtractor / RiskExtractor / RiskExtractor /
# ConsistencyChecker / ConfidenceAssessor) all follow the same shape:
#
#   1. Skip when the artifact isn't text-shaped (chunks + compile
#      markdown count; images / graph_json don't).
#   2. Skip when no `text_client` was wired (legacy stub behaviour).
#   3. Read the artifact bytes via `content_source`, decode as UTF-8.
#   4. Build a prompt = profile_prompt() OR class default.
#   5. Call `text_client.extract(prompt, schema)` for structured JSON.
#   6. Render a small markdown summary alongside the JSON.
#
# The shared base class consolidates the boilerplate so subclasses
# only have to declare {output_key, schema, default_prompt,
# render_md}. Each subclass remains a thin specialisation of one
# extraction concern.

# Cap how much content we send the LLM per call. Most providers'
# context windows are larger, but the failure mode of overflowing
# is awful (silent truncation or 500). Bias toward "extract the
# top of the document" — the FE shows partial extraction with a
# warning rather than dropping the artifact entirely.
_MAX_CONTENT_CHARS = 20_000


# Image-specific kinds are matched by `_is_image_kind` (defined
# below). For text-shaped enrichers, we skip kinds that we KNOW
# are non-textual; everything else is fair game (chunks, compile
# markdown, compile.metadata JSON).
_NON_TEXT_KINDS = frozenset({
    "graph_json",
    ARTIFACT_TYPE_VISUALS.lower(),
})


def _is_text_kind(kind: str | None) -> bool:
    """True when an artifact of `kind` is reasonable to feed an
    LLM-backed text enricher. False for binary / image / graph
    artifacts. Empty/None falls back to True so callers without an
    `artifact_lookup` don't lose the legacy behaviour."""
    if not kind:
        return True
    normalised = kind.strip().lower()
    if normalised in _NON_TEXT_KINDS:
        return False
    if _is_image_kind(normalised):
        return False
    return True


class _LLMBackedEnricher(_StructuredEnricher):
    """Shared scaffolding for enrichers that delegate extraction to
    a structured-output LLM call.

    Subclasses declare:
      * `_OUTPUT_KEY` — top-level JSON key in the response (e.g.
        `"tables"`, `"requirements"`)
      * `_OUTPUT_SCHEMA` — JSON schema passed to `text_client.extract`
      * `_DEFAULT_PROMPT` — fallback when profile prompt is absent
      * `_render_md(json_data)` — turn the structured response into
        the markdown sibling artifact

    The base handles the kind gate, content read, error wrapping,
    and the legacy stub fallback when `text_client` is None.
    """

    _OUTPUT_KEY: str = "items"
    _OUTPUT_SCHEMA: dict[str, Any] = {}
    _DEFAULT_PROMPT: str = ""

    def __init__(
        self,
        profile: Profile,
        *,
        artifact_lookup: Callable[[ProjectContext, str], str | None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(profile, **kwargs)
        # Same gate VCD uses, but applied to text artifacts. None
        # means "run on every artifact" (legacy behaviour).
        self._artifact_lookup = artifact_lookup

    def enrich(self, ctx, artifact_id):
        if not self._enabled:
            return ArtifactProcessingResult(
                status=ResultStatus.SKIPPED,
                message=f"{self.kind} disabled",
                metadata={"processor_name": self.kind},
            )
        if self._artifact_lookup is not None:
            kind = self._artifact_lookup(ctx, artifact_id)
            if kind and not _is_text_kind(kind):
                return ArtifactProcessingResult(
                    status=ResultStatus.SKIPPED,
                    message=f"{self.kind} skipped: artifact kind {kind!r} is not text",
                    metadata={
                        "processor_name": self.kind,
                        "skip_reason": "non_text_artifact",
                        "artifact_kind": kind,
                    },
                )
        return super().enrich(ctx, artifact_id)

    def _produce(self, ctx, artifact_id):
        # Fallback to the legacy empty-output stub when the operator
        # hasn't wired a text LLM. Preserves the contract callers
        # have relied on (the enricher always returns a draft, even
        # if empty).
        if self._text_client is None:
            return self._stub_response(artifact_id), self._render_md(
                self._stub_response(artifact_id),
            )

        content = self._read_content(ctx, artifact_id)
        if not content:
            return self._stub_response(artifact_id), self._render_md(
                self._stub_response(artifact_id),
            )
        try:
            text = content.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — defensive, never observed
            text = ""
        # Char-cap as a first-line defence (the legacy behaviour) —
        # caps wildly oversized inputs (a 5MB markdown file) at
        # 20K chars before we hand it to the budget helper, which
        # is O(log n) but no faster than not running on a 5MB body.
        body = text[:_MAX_CONTENT_CHARS]
        prompt = self._profile_prompt() or self._DEFAULT_PROMPT
        # When the LLM client carries a context-window budget,
        # shrink `body` further so prompt + schema + instructions
        # fit. The 25% buffer accommodates the schema serialisation
        # the `extract()` wrapper appends + safety drift in the
        # fallback estimator. With no configured window this is a
        # no-op (body stays at the char cap above).
        body = self._fit_body_to_budget(body, prompt)
        # Domain-pack prompt addon: prepend the active domain's
        # guidance ahead of the per-enricher prompt so the model has
        # domain context BEFORE the task-specific instructions. The
        # addon is empty for runs without an active domain pack —
        # the resulting `full_prompt` matches the pre-Phase-2-W2
        # shape in that case.
        if self._domain_prompt_addon:
            full_prompt = (
                f"{self._domain_prompt_addon}\n\n"
                f"{prompt}\n\n"
                "Respond ONLY with JSON matching the schema. "
                "Do not include prose around the JSON.\n\n"
                "---\n"
                f"{body}\n"
            )
        else:
            full_prompt = (
                f"{prompt}\n\n"
                "Respond ONLY with JSON matching the schema. "
                "Do not include prose around the JSON.\n\n"
                "---\n"
                f"{body}\n"
            )

        try:
            parsed, _usage = self._text_client.extract(
                full_prompt,
                self._OUTPUT_SCHEMA,
                metadata={"processor_name": self.kind, "artifact_id": artifact_id},
            )
        except Exception as exc:  # noqa: BLE001 — surface as soft skip
            error_response = self._error_response(artifact_id, exc)
            return error_response, self._render_md(error_response)

        # Carry the full parsed response into json_data so subclass
        # schemas with multiple top-level fields (e.g. DocumentClassifier
        # = {classification, sections}, ConfidenceAssessor =
        # {overall_confidence, assessments}) preserve everything the
        # LLM returned. Then explicitly normalise the primary
        # `_OUTPUT_KEY` so it's always a list.
        json_data: dict[str, Any] = {
            "source_artifact_id": artifact_id,
            "model": getattr(self._text_client, "model", None),
            "provider": getattr(self._text_client, "provider", None),
        }
        if isinstance(parsed, dict):
            for k, v in parsed.items():
                # Don't shadow the lineage / model fields if the
                # LLM happened to mention them (unlikely but safe).
                if k in ("source_artifact_id", "model", "provider"):
                    continue
                json_data[k] = v
        items = parsed.get(self._OUTPUT_KEY) if isinstance(parsed, dict) else None
        json_data[self._OUTPUT_KEY] = list(items) if isinstance(items, list) else []
        return json_data, self._render_md(json_data)

    # ---- Token-budget helpers -----------------------------------

    def _fit_body_to_budget(self, body: str, profile_prompt: str) -> str:
        """Shrink `body` so the assembled prompt fits the LLM's
        configured context window (when one is set).

        The prompt the enricher sends has three components:
        `profile_prompt` (with schema appended by `extract()`),
        boilerplate instructions, and `body`. Reserve a bookkeeping
        budget for everything that ISN'T body and then truncate
        body to fit. Falls back gracefully when no context window
        is configured (returns body unchanged).
        """
        text_client = self._text_client
        context_window = getattr(
            getattr(text_client, "_settings", None),
            "context_window_tokens", None,
        )
        if context_window is None:
            # No budget configured — preserve legacy behaviour.
            return body
        from j1.llm.budget import (
            TokenBudget,
            estimate_tokens,
            pack_text_for_budget,
        )
        settings = text_client._settings  # type: ignore[union-attr]
        budget = TokenBudget(
            context_window_tokens=context_window,
            reserved_output_tokens=getattr(settings, "max_output_tokens", 0),
            safety_margin_tokens=getattr(settings, "safety_margin_tokens", 0),
        )
        available = budget.available_input_tokens
        if available is None or available <= 0:
            return body
        # Reserve room for the wrapper text the enricher appends
        # AROUND the body (profile prompt, instructions, schema
        # boilerplate). Estimating the actual schema cost would
        # require the wrapped prompt the client builds — fine to
        # over-estimate here since being conservative is the goal.
        wrapper_tokens = estimate_tokens(profile_prompt) + estimate_tokens(
            "Respond ONLY with JSON matching the schema. "
            "Do not include prose around the JSON.\n\n---\n\n"
        )
        # Reserve an extra schema-serialisation margin: `extract()`
        # serialises the JSON schema into the prompt, which can
        # be 200-1500 tokens depending on shape. Use 25% of the
        # available budget as a safe reservation.
        schema_reserve = max(256, int(available * 0.25))
        body_budget = available - wrapper_tokens - schema_reserve
        if body_budget <= 0:
            # The wrapper alone exceeds the budget — don't try to
            # send anything; the boundary check will raise the
            # actionable LLMContextOverflowError when the prompt
            # is built.
            return ""
        return pack_text_for_budget(body, body_budget)

    # ---- Hooks subclasses customise -----------------------------

    def _stub_response(self, artifact_id: str) -> dict[str, Any]:
        """Empty-output shape returned when the LLM isn't available
        or content is empty. Subclasses can override to add fields."""
        return {
            "source_artifact_id": artifact_id,
            self._OUTPUT_KEY: [],
        }

    def _error_response(
        self, artifact_id: str, exc: Exception,
    ) -> dict[str, Any]:
        return {
            "source_artifact_id": artifact_id,
            self._OUTPUT_KEY: [],
            "error": f"{type(exc).__name__}: {exc}",
        }

    def _render_md(self, json_data: dict[str, Any]) -> str:
        """Default markdown renderer — subclasses override for
        domain-specific formatting (tables, requirements, etc.)."""
        items = json_data.get(self._OUTPUT_KEY) or []
        title = self._OUTPUT_KEY.replace("_", " ").title()
        lines = [f"# {title}", "", f"Total: {len(items)}", ""]
        for entry in items[:50]:
            if isinstance(entry, dict):
                # Pretty-print up to a few key fields.
                bits = ", ".join(f"{k}={v!r}" for k, v in list(entry.items())[:4])
                lines.append(f"- {bits}")
            else:
                lines.append(f"- {entry}")
        return "\n".join(lines) + "\n"


# ---- The 8 generic enrichers ---------------------------------------


class DocumentClassifier(_LLMBackedEnricher):
    kind = PROCESSOR_DOCUMENT_CLASSIFIER
    artifact_type = ARTIFACT_TYPE_DOCUMENT_MAP
    prompt_name = "classify_document"
    confidence_default = 0.7

    _OUTPUT_KEY = "sections"
    _OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "classification": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["label"],
                },
            },
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "page_start": {"type": "integer"},
                        "page_end": {"type": "integer"},
                    },
                    "required": ["title"],
                },
            },
        },
        "required": ["classification", "sections"],
    }
    _DEFAULT_PROMPT = (
        "You are a document analyst. Classify the document type "
        "(report / spec / contract / paper / memo / other) and list "
        "its top-level sections. Each classification entry includes "
        "a `label` and `confidence` (0..1). Each section includes a "
        "`title`, optional `summary`, and `page_start`/`page_end` "
        "when discernible."
    )

    def _produce(self, ctx, artifact_id):
        json_data, _md = super()._produce(ctx, artifact_id)
        # Preserve legacy fields the original stub emitted so existing
        # consumers / tests don't break:
        #   * `byte_size`   — informative when text_client is unwired
        #   * `prompt_used` — bool, true iff the active profile
        #                     supplied the `classify_document` prompt
        #   * `classification` — always present (defaults to [] when
        #                       the model didn't emit one)
        json_data["byte_size"] = len(self._read_content(ctx, artifact_id))
        json_data["prompt_used"] = bool(self._profile_prompt())
        json_data.setdefault("classification", [])
        return json_data, self._render_md(json_data)

    def _render_md(self, json_data: dict[str, Any]) -> str:
        sections = json_data.get("sections") or []
        classification = json_data.get("classification") or []
        lines = ["# Document map", ""]
        if classification:
            lines.append("## Classification")
            for c in classification:
                if isinstance(c, dict):
                    label = c.get("label", "?")
                    conf = c.get("confidence")
                    lines.append(
                        f"- {label}" + (f" ({conf})" if conf is not None else "")
                    )
            lines.append("")
        lines.append(f"Sections: {len(sections)}")
        for s in sections[:50]:
            if isinstance(s, dict):
                title = s.get("title", "?")
                pages = s.get("page_start")
                pages_str = f" (p. {pages})" if pages is not None else ""
                lines.append(f"- {title}{pages_str}")
        return "\n".join(lines) + "\n"


class RequirementExtractor(_LLMBackedEnricher):
    kind = PROCESSOR_REQUIREMENT_EXTRACTOR
    artifact_type = ARTIFACT_TYPE_REQUIREMENTS
    prompt_name = "extract_requirements"
    confidence_default = 0.6

    _OUTPUT_KEY = "requirements"
    _OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "requirements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "text": {"type": "string"},
                        "priority": {
                            "type": "string",
                            "enum": ["MUST", "SHOULD", "MAY", "informative"],
                        },
                        "section": {"type": "string"},
                        "page": {"type": "integer"},
                    },
                    "required": ["text"],
                },
            },
        },
        "required": ["requirements"],
    }
    _DEFAULT_PROMPT = (
        "Extract requirements from the document. A requirement is a "
        "MUST / SHOULD / MAY statement, an explicit obligation, or "
        "a constraint the system / process / contract must satisfy. "
        "For each, include the verbatim `text`, the `priority` "
        "(MUST / SHOULD / MAY / informative), and the `section` and "
        "`page` when known. Skip background prose and definitions."
    )

    def _render_md(self, json_data: dict[str, Any]) -> str:
        items = json_data.get("requirements") or []
        lines = ["# Requirements", "", f"Total: {len(items)}", ""]
        for r in items[:100]:
            if isinstance(r, dict):
                priority = r.get("priority", "?")
                text = r.get("text", "")
                rid = r.get("id") or ""
                lines.append(f"- [{priority}] {rid} {text}".strip())
        return "\n".join(lines) + "\n"


class TableExtractor(_LLMBackedEnricher):
    kind = PROCESSOR_TABLE_EXTRACTOR
    artifact_type = ARTIFACT_TYPE_TABLES
    prompt_name = "extract_tables"
    confidence_default = 0.7

    _OUTPUT_KEY = "tables"
    _OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "tables": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "caption": {"type": "string"},
                        "columns": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "rows": {
                            "type": "array",
                            "items": {
                                "type": "array",
                                "items": {
                                    "type": ["string", "number", "null"],
                                },
                            },
                        },
                        "page": {"type": "integer"},
                    },
                    "required": ["columns", "rows"],
                },
            },
        },
        "required": ["tables"],
    }
    _DEFAULT_PROMPT = (
        "Extract every tabular structure from the document. For "
        "each table, return the column headers as `columns` (array "
        "of strings) and the body cells as `rows` (array of arrays, "
        "one per row). Include `title` / `caption` / `page` when "
        "the document supplies them. Numbers stay numeric; missing "
        "cells are null. Tables that span multiple pages should be "
        "merged into a single entry."
    )

    def _render_md(self, json_data: dict[str, Any]) -> str:
        tables = json_data.get("tables") or []
        lines = ["# Tables", "", f"Total: {len(tables)}", ""]
        for i, t in enumerate(tables[:25], 1):
            if not isinstance(t, dict):
                continue
            title = t.get("title") or t.get("caption") or f"Table {i}"
            page = t.get("page")
            page_str = f" (p. {page})" if page is not None else ""
            lines.append(f"## {title}{page_str}")
            cols = t.get("columns") or []
            rows = t.get("rows") or []
            if cols:
                lines.append("| " + " | ".join(str(c) for c in cols) + " |")
                lines.append("|" + "|".join(["---"] * len(cols)) + "|")
            for row in rows[:20]:
                if isinstance(row, list):
                    cells = ["" if v is None else str(v) for v in row]
                    lines.append("| " + " | ".join(cells) + " |")
            if len(rows) > 20:
                lines.append(f"_…{len(rows) - 20} more rows_")
            lines.append("")
        return "\n".join(lines) + "\n"


class VisualContentDescriber(_StructuredEnricher):
    """Vision-LLM enricher for image artifacts.

    Three-state behaviour, picked based on what the deployment wired:
      * `vision_client` is None         → no-op (returns empty visuals
        list). Keeps existing tests / deployments without a vision
        provider working.
      * `vision_client` set, no bytes   → no-op with a `reason` field
        explaining why (the artifact registry didn't expose payload
        loading for this artifact).
      * `vision_client` + bytes         → calls
        `vision_client.analyze_image(...)` with the prompt configured
        in the active profile (`describe_visuals` key) and packs the
        response into the structured `visuals` entry.

    The result is marked `review_required=True` (the class default)
    so a human gets to confirm vision-generated descriptions before
    they're treated as authoritative."""

    kind = PROCESSOR_VISUAL_DESCRIBER
    artifact_type = ARTIFACT_TYPE_VISUALS
    prompt_name = "describe_visuals"
    confidence_default = 0.5
    review_required_default = True

    # Default prompt used when the active profile has no
    # `describe_visuals` entry. Generic so it works on any image —
    # diagrams, charts, screenshots, photos. Not domain-specific.
    _DEFAULT_PROMPT = (
        "Describe this image in 3-5 sentences. List any visible "
        "labels, captions, or text verbatim. Identify the type of "
        "visual (diagram / chart / photograph / screenshot / table "
        "image / icon). If the image is decorative (logo, watermark, "
        "icon) say so explicitly."
    )

    def __init__(
        self,
        profile: Profile,
        *,
        vision_client: Any | None = None,
        artifact_lookup: Callable[[ProjectContext, str], str | None] | None = None,
        artifact_record_lookup: Callable[[ProjectContext, str], Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(profile, **kwargs)
        self._vision_client = vision_client
        # When set, called per artifact_id to fetch the artifact's
        # `kind`. VCD short-circuits with SKIPPED for kinds that
        # don't look image-shaped — the workflow runs enrich on
        # EVERY compile artifact (chunks + metadata + images), and
        # without this gate VCD pollutes the Visuals card with stub
        # "Image bytes not available" markdown for every non-image
        # artifact in the run. None disables the skip — falls back
        # to the legacy behaviour where VCD runs on everything.
        self._artifact_lookup = artifact_lookup
        # When set, called per artifact_id to fetch the full
        # `ArtifactRecord`. VCD reads `metadata["vision_decision"]`
        # off the record to short-circuit decorative / icon images
        # (decision == "skip"). The bridge stamps these decisions
        # at parse time via `_stamp_image_decisions`. None disables
        # per-image triage and falls back to "describe every image".
        self._artifact_record_lookup = artifact_record_lookup

    def enrich(self, ctx, artifact_id):
        """Override to short-circuit non-image artifacts BEFORE
        `_produce` runs. Returns `SKIPPED` (no drafts) so the
        composite doesn't add this no-op to the Visuals card."""
        if not self._enabled:
            return ArtifactProcessingResult(
                status=ResultStatus.SKIPPED,
                message=f"{self.kind} disabled",
                metadata={"processor_name": self.kind},
            )
        if self._artifact_lookup is not None:
            kind = self._artifact_lookup(ctx, artifact_id)
            if kind and not _is_image_kind(kind):
                # Not an image artifact — skip silently without
                # producing any `enriched.visuals` drafts. This is
                # the difference between "Visuals card empty" and
                # "Visuals card full of stub messages".
                return ArtifactProcessingResult(
                    status=ResultStatus.SKIPPED,
                    message=f"{self.kind} skipped: artifact kind {kind!r} is not visual",
                    metadata={
                        "processor_name": self.kind,
                        "skip_reason": "non_image_artifact",
                        "artifact_kind": kind,
                    },
                )
        # Per-image triage: skip artifacts the parser-side classifier
        # tagged as decorative (logos, icons, watermarks) so we don't
        # burn vision-LLM tokens describing them. The decision was
        # made at compile time in `_classify_image`; we read it back
        # from artifact metadata here. When the lookup isn't wired
        # OR the artifact wasn't tagged (e.g. a non-bridge producer
        # wrote it), VCD falls through to describing everything —
        # the legacy behaviour.
        if self._artifact_record_lookup is not None:
            record = self._artifact_record_lookup(ctx, artifact_id)
            metadata = (
                record.metadata if record is not None and hasattr(record, "metadata")
                else None
            )
            if isinstance(metadata, dict):
                vision_decision = metadata.get("vision_decision")
                if vision_decision == "skip":
                    return ArtifactProcessingResult(
                        status=ResultStatus.SKIPPED,
                        message=(
                            f"{self.kind} skipped: vision_decision=skip "
                            f"({metadata.get('vision_reason') or 'decorative image'})"
                        ),
                        metadata={
                            "processor_name": self.kind,
                            "skip_reason": "decorative_image",
                            "vision_role": metadata.get("vision_role"),
                            "vision_score": metadata.get("vision_score"),
                        },
                    )
        return super().enrich(ctx, artifact_id)

    def _produce(self, ctx, artifact_id):
        # Read image bytes through the standard `_read_content` hook.
        # When the wiring layer didn't supply a `content_source` the
        # call returns b"" and we degrade gracefully — same as the
        # other enrichers.
        content = self._read_content(ctx, artifact_id)
        prompt = self._profile_prompt() or self._DEFAULT_PROMPT

        if self._vision_client is None:
            # No vision client wired — preserve the original
            # placeholder shape so existing fixtures see the same
            # output. The `reason` field tells the operator why
            # nothing happened.
            return {
                "source_artifact_id": artifact_id,
                "visuals": [],
                "reason": "no vision_client wired into VisualContentDescriber",
            }, _no_vision_md(artifact_id)

        if not content:
            return {
                "source_artifact_id": artifact_id,
                "visuals": [],
                "reason": "no image bytes available for artifact",
            }, _no_bytes_md(artifact_id)

        # Call the vision LLM. `media_type=None` lets the implementation
        # default (typically image/png). `metadata` is forwarded for
        # the provider's telemetry — useful when reconciling spend.
        try:
            description, usage = self._vision_client.analyze_image(
                content,
                prompt=prompt,
                metadata={
                    "processor_name": self.kind,
                    "artifact_id": artifact_id,
                },
            )
        except Exception as exc:  # noqa: BLE001 — surface the failure as a soft skip
            return {
                "source_artifact_id": artifact_id,
                "visuals": [],
                "reason": f"vision LLM call failed: {type(exc).__name__}: {exc}",
            }, _vision_failure_md(artifact_id, exc)

        visual_entry: dict[str, Any] = {
            "artifact_id": artifact_id,
            "description": description,
            "model": getattr(self._vision_client, "model", None),
            "provider": getattr(self._vision_client, "provider", None),
            "byte_size": len(content),
        }
        if usage is not None:
            visual_entry["usage"] = {
                "input_tokens": getattr(usage, "input_tokens", 0),
                "output_tokens": getattr(usage, "output_tokens", 0),
                "total_tokens": getattr(usage, "total_tokens", 0),
                "estimated_cost": getattr(usage, "estimated_cost", None),
            }
        json_data = {
            "source_artifact_id": artifact_id,
            "visuals": [visual_entry],
        }
        md_text = (
            "# Visual content\n\n"
            f"Source artifact: `{artifact_id}`\n\n"
            f"## Description\n\n{description}\n\n"
            "_Pending human review._\n"
        )
        return json_data, md_text


def _is_image_kind(kind: str | None) -> bool:
    """Decide whether VCD should run on an artifact of `kind`.

    Image-shaped kinds in the J1 taxonomy:
      * `compile.image` — the `_drafts_from_output_dir` helper stamps
        `.png` / `.jpg` / `.webp` files with this suffix.
      * Anything ending in `.image` for forward-compat with future
        producers that follow the same convention.
      * `enriched.visuals` — already a visual artifact, but
        re-enriching it would loop. Treat as image-shaped so the
        composite doesn't double-enrich (and so a deployment that
        chains enrich passes still has VCD see it).

    Everything else (chunks, compile.metadata, graph_json,
    enriched.tables, etc.) is non-image and gets a SKIPPED result."""
    if not kind:
        return False
    normalised = kind.strip().lower()
    if normalised == ARTIFACT_TYPE_VISUALS.lower():
        return True
    return normalised.endswith(".image") or ".image." in normalised


def _no_vision_md(artifact_id: str) -> str:
    return (
        "# Visual content\n\n"
        f"Source artifact: `{artifact_id}`\n\n"
        "_No vision LLM configured — visual enrichment skipped._\n"
    )


def _no_bytes_md(artifact_id: str) -> str:
    return (
        "# Visual content\n\n"
        f"Source artifact: `{artifact_id}`\n\n"
        "_Image bytes not available — visual enrichment skipped._\n"
    )


def _vision_failure_md(artifact_id: str, exc: Exception) -> str:
    return (
        "# Visual content\n\n"
        f"Source artifact: `{artifact_id}`\n\n"
        f"_Vision LLM call failed: `{type(exc).__name__}`. "
        "Visual enrichment skipped — operator should investigate._\n"
    )


class FormulaExtractor(_LLMBackedEnricher):
    kind = PROCESSOR_FORMULA_EXTRACTOR
    artifact_type = ARTIFACT_TYPE_FORMULAS
    prompt_name = "extract_formulas"
    confidence_default = 0.5
    review_required_default = True

    _OUTPUT_KEY = "formulas"
    _OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "formulas": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "tex": {"type": "string"},
                        "description": {"type": "string"},
                        "variables": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "meaning": {"type": "string"},
                                },
                                "required": ["name"],
                            },
                        },
                        "page": {"type": "integer"},
                    },
                    "required": ["tex"],
                },
            },
        },
        "required": ["formulas"],
    }
    _DEFAULT_PROMPT = (
        "Extract every mathematical formula or equation from the "
        "document. For each, include the LaTeX representation as "
        "`tex`, an optional `description` of what it computes, and "
        "the `variables` (each with `name` and `meaning`) when the "
        "document defines them. Skip mere references to formulas "
        "elsewhere — only emit ones the text actually contains."
    )

    def _render_md(self, json_data: dict[str, Any]) -> str:
        items = json_data.get("formulas") or []
        lines = ["# Formulas", "", f"Total: {len(items)}", ""]
        if not items:
            lines.append("_Pending human review._")
        for f in items[:50]:
            if isinstance(f, dict):
                tex = f.get("tex", "")
                desc = f.get("description")
                lines.append(f"- `{tex}`" + (f" — {desc}" if desc else ""))
        return "\n".join(lines) + "\n"


class RiskExtractor(_LLMBackedEnricher):
    kind = PROCESSOR_RISK_EXTRACTOR
    artifact_type = ARTIFACT_TYPE_RISKS
    prompt_name = "extract_risks"
    confidence_default = 0.6

    _OUTPUT_KEY = "risks"
    _OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "risks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "severity": {
                            "type": "string",
                            "enum": ["low", "medium", "high", "critical"],
                        },
                        "category": {"type": "string"},
                        "mitigation": {"type": "string"},
                        "page": {"type": "integer"},
                    },
                    "required": ["title", "severity"],
                },
            },
        },
        "required": ["risks"],
    }
    _DEFAULT_PROMPT = (
        "Extract risk-relevant statements from the document. A "
        "risk is any explicit threat, hazard, vulnerability, "
        "compliance gap, or forward-looking concern. For each, "
        "return `title`, `severity` (low/medium/high/critical), "
        "`category` (e.g. financial, operational, legal, security, "
        "reputational), `description`, and `mitigation` if the "
        "document describes one. Include the `page` when known."
    )

    def _render_md(self, json_data: dict[str, Any]) -> str:
        items = json_data.get("risks") or []
        lines = ["# Risks", "", f"Total: {len(items)}", ""]
        for r in items[:100]:
            if isinstance(r, dict):
                sev = r.get("severity", "?")
                title = r.get("title", "")
                cat = r.get("category", "")
                cat_str = f" [{cat}]" if cat else ""
                lines.append(f"- ({sev}){cat_str} {title}")
        return "\n".join(lines) + "\n"


class ConsistencyChecker(_LLMBackedEnricher):
    kind = PROCESSOR_CONSISTENCY_CHECKER
    artifact_type = ARTIFACT_TYPE_CONSISTENCY_FINDINGS
    prompt_name = "check_consistency"
    confidence_default = 0.5
    review_required_default = True

    _OUTPUT_KEY = "findings"
    _OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "category": {
                            "type": "string",
                            "enum": [
                                "contradiction",
                                "duplicate",
                                "missing_section",
                                "ambiguity",
                                "terminology_drift",
                                "other",
                            ],
                        },
                        "message": {"type": "string"},
                        "page": {"type": "integer"},
                        "score": {"type": "number"},
                    },
                    "required": ["category", "message"],
                },
            },
        },
        "required": ["findings"],
    }
    _DEFAULT_PROMPT = (
        "Find consistency issues within the document. Look for "
        "contradictions (statement A vs statement B), duplicates "
        "(the same requirement / claim repeated), missing sections "
        "(referenced but never defined), terminology drift (the "
        "same concept named two different ways), and ambiguities "
        "(statements with no clear interpretation). For each, "
        "return `category`, a short `message` explaining the "
        "issue, the `page` when known, and a `score` 0..1 (1 = "
        "high confidence the issue is real)."
    )

    def _render_md(self, json_data: dict[str, Any]) -> str:
        items = json_data.get("findings") or []
        lines = ["# Consistency findings", "", f"Total: {len(items)}", ""]
        if not items:
            lines.append("_Pending human review._")
        for f in items[:100]:
            if isinstance(f, dict):
                cat = f.get("category", "?")
                msg = f.get("message", "")
                page = f.get("page")
                page_str = f" (p. {page})" if page is not None else ""
                lines.append(f"- [{cat}]{page_str} {msg}")
        return "\n".join(lines) + "\n"


class SourceMapper(_StructuredEnricher):
    kind = PROCESSOR_SOURCE_MAPPER
    artifact_type = ARTIFACT_TYPE_SOURCE_MAP
    prompt_name = "map_sources"
    confidence_default = 0.9
    formats = ("json",)

    def _produce(self, ctx, artifact_id):
        content = self._read_content(ctx, artifact_id)
        json_data = {
            "source_artifact_id": artifact_id,
            "sources": [
                {
                    "artifact_id": artifact_id,
                    "byte_size": len(content),
                }
            ],
        }
        return json_data, ""


class ConfidenceAssessor(_LLMBackedEnricher):
    kind = PROCESSOR_CONFIDENCE_ASSESSOR
    artifact_type = ARTIFACT_TYPE_CONFIDENCE_ASSESSMENT
    prompt_name = "assess_confidence"
    confidence_default = 0.8

    _OUTPUT_KEY = "assessments"
    _OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "overall_confidence": {"type": "number"},
            "assessments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "modality": {
                            "type": "string",
                            "description": (
                                "What's being assessed: 'tables', 'ocr', "
                                "'reasoning', 'numbers', 'citations', etc."
                            ),
                        },
                        "confidence": {"type": "number"},
                        "page": {"type": "integer"},
                        "category": {"type": "string"},
                        "message": {"type": "string"},
                    },
                    "required": ["modality", "confidence"],
                },
            },
        },
        "required": ["assessments"],
    }
    _DEFAULT_PROMPT = (
        "Assess the extraction confidence for this content. For "
        "each assessable modality (tables, ocr, reasoning, numeric "
        "values, citations, etc.), return a `confidence` 0..1 with "
        "1 = highest. When you spot a specific concern (a low-OCR "
        "region, a contradicted claim, a numeric value that "
        "doesn't add up), include the `page` and a brief "
        "`message`. Return `overall_confidence` as the weighted "
        "mean across modalities."
    )

    def _produce(self, ctx, artifact_id):
        json_data, _md = super()._produce(ctx, artifact_id)
        # Honest fallback: only carry `default_confidence` through when
        # the LLM call actually ran. The parent's `_error_response`
        # records an `error` field on failure; in that case let the
        # quality projector fall through to a real "no measurement"
        # state ("—" in the FE) instead of a fabricated 0.7-0.8 score
        # that masks the failure. Operators see the assessment
        # artifact's `error` and the projector surfaces it as a
        # quality warning rather than a healthy-looking score.
        if "error" not in json_data:
            json_data.setdefault("default_confidence", self.confidence_default)
        return json_data, self._render_md(json_data)

    def _render_md(self, json_data: dict[str, Any]) -> str:
        assessments = json_data.get("assessments") or []
        overall = json_data.get("overall_confidence")
        default = json_data.get("default_confidence")
        error = json_data.get("error")
        lines = ["# Confidence assessment", ""]
        if error:
            # LLM failure path — keep the markdown honest. Without
            # this branch the renderer falls back to "Default
            # confidence: 0.800" which reads as a healthy measurement.
            lines.append("Confidence assessment unavailable (LLM extraction failed).")
            lines.append(f"Error: {error}")
        elif overall is not None:
            lines.append(f"Overall confidence: {float(overall):.2f}")
        elif default is not None:
            lines.append(f"Default confidence: {float(default):.3f}")
        else:
            lines.append("Confidence not measured.")
        lines.append("")
        if assessments:
            lines.append(f"Modality assessments ({len(assessments)}):")
            for a in assessments[:50]:
                if isinstance(a, dict):
                    mod = a.get("modality", "?")
                    conf = a.get("confidence")
                    msg = a.get("message")
                    head = f"- {mod}: {conf:.2f}" if isinstance(conf, (int, float)) else f"- {mod}"
                    if msg:
                        head += f" — {msg}"
                    lines.append(head)
        return "\n".join(lines) + "\n"


GENERIC_ENRICHERS: tuple[type[_StructuredEnricher], ...] = (
    DocumentClassifier,
    RequirementExtractor,
    TableExtractor,
    VisualContentDescriber,
    FormulaExtractor,
    RiskExtractor,
    ConsistencyChecker,
    SourceMapper,
    ConfidenceAssessor,
)


COMPOSITE_ENRICHER_KIND = "j1.enricher.composite"


def _filter_generic_enrichers(
    classes: tuple[type[_StructuredEnricher], ...],
    *,
    images_enabled: bool | None,
    tables_enabled: bool | None,
    diagrams_enabled: bool | None = None,
    scanned_pages_enabled: bool | None = None,
) -> tuple[type[_StructuredEnricher], ...]:
    """Drop sub-enrichers whose modality the deployment disabled.

    Mapping (only modalities with a single owning enricher are
    gated here; per-modality split for the rest is design-future):

      * `tables_enabled=False` → drop `TableExtractor`. Sole
        producer of `enriched.tables`.
      * `images_enabled` / `diagrams_enabled` / `scanned_pages_enabled`
        — `VisualContentDescriber` (VCD) is the only generic
        enricher that consumes the vision LLM, and it doesn't
        differentiate photos from diagrams from scanned-page
        captures (vendor parser tags artifacts uniformly). VCD
        runs when **any** of the three visual flags is enabled and
        is dropped only when **all three** are explicitly False.
        That matches the operator-facing semantic "kill all visual
        enrichment" without falsely promising fine-grained control
        the parser doesn't provide today.

    `None` (the default for any flag) means "no opinion — keep the
    enricher". Callers that don't pass settings see the legacy
    "run everything" behaviour. Anything not mapped above always
    runs.
    """
    visual_flags = (images_enabled, diagrams_enabled, scanned_pages_enabled)
    visual_explicit_off = all(flag is False for flag in visual_flags)
    if visual_explicit_off:
        classes = tuple(
            c for c in classes if c is not VisualContentDescriber
        )
    if tables_enabled is False:
        classes = tuple(
            c for c in classes if c is not TableExtractor
        )
    return classes


class CompositeEnricher:
    """Bundles every generic enricher and runs them in sequence,
    returning the union of their `ArtifactDraft`s.

    The Results > Assets tab needs `enriched.tables` /
    `enriched.visuals` / `enriched.formulas` artifacts to populate.
    Each child enricher produces ONE kind, so wiring them
    individually means an upload would pick exactly one (the
    workflow runs one `enricher_kind` per run). Bundling them as a
    single registered kind keeps the auto-default semantic intact —
    one registered kind → unambiguous auto-pick on FE upload — while
    still emitting the full set of enriched.* artifacts the Assets +
    Quality tabs rely on.

    The composite degrades gracefully: a child that raises (e.g. its
    profile prompt is missing) is logged and skipped — the rest still
    run. Failures of individual children do NOT fail the workflow's
    enrich step; the operator sees the failures via the artifacts
    that DID land + the quality report's surfaced warnings.
    """

    kind = COMPOSITE_ENRICHER_KIND

    def __init__(
        self,
        profile: Profile,
        *,
        enrichers: tuple[_StructuredEnricher, ...] | None = None,
        content_source: Callable[[ProjectContext, str], bytes] | None = None,
        vision_client: Any | None = None,
        text_client: Any | None = None,
        embedding_client: Any | None = None,
        artifact_lookup: Callable[[ProjectContext, str], str | None] | None = None,
        # Optional record-fetcher for per-image triage. When set,
        # `VisualContentDescriber` reads `metadata["vision_decision"]`
        # off the artifact record and short-circuits decorative
        # images. Wiring layer typically passes a closure that calls
        # `artifact_registry.get(ctx, artifact_id)`.
        artifact_record_lookup: Callable[[ProjectContext, str], Any] | None = None,
        # Per-modality kill switches. `None` (the default) keeps every
        # generic enricher in the bundle — that's the legacy behaviour.
        # Pass `False` for a modality to skip its enricher entirely.
        # Plumbed from `EnrichmentSettings` at the deployment-wiring
        # layer; the composite stays loose-typed so no import cycle
        # with `j1.compose`.
        #
        # `images` / `diagrams` / `scanned_pages` collectively gate
        # the visual content describer — see `_filter_generic_enrichers`
        # for the rationale.
        images_enabled: bool | None = None,
        tables_enabled: bool | None = None,
        diagrams_enabled: bool | None = None,
        scanned_pages_enabled: bool | None = None,
    ) -> None:
        if enrichers is None:
            child_classes = _filter_generic_enrichers(
                GENERIC_ENRICHERS,
                images_enabled=images_enabled,
                tables_enabled=tables_enabled,
                diagrams_enabled=diagrams_enabled,
                scanned_pages_enabled=scanned_pages_enabled,
            )
            enrichers = tuple(
                _construct_child(
                    cls_,
                    profile=profile,
                    content_source=content_source,
                    vision_client=vision_client,
                    text_client=text_client,
                    embedding_client=embedding_client,
                    artifact_lookup=artifact_lookup,
                    artifact_record_lookup=artifact_record_lookup,
                )
                for cls_ in child_classes
            )
        self._profile = profile
        self._enrichers = enrichers

    @classmethod
    def from_default(
        cls,
        profile: Profile,
        *,
        content_source: Callable[[ProjectContext, str], bytes] | None = None,
        vision_client: Any | None = None,
        text_client: Any | None = None,
        embedding_client: Any | None = None,
        artifact_lookup: Callable[[ProjectContext, str], str | None] | None = None,
        artifact_record_lookup: Callable[[ProjectContext, str], Any] | None = None,
        images_enabled: bool | None = None,
        tables_enabled: bool | None = None,
        diagrams_enabled: bool | None = None,
        scanned_pages_enabled: bool | None = None,
    ) -> "CompositeEnricher":
        return cls(
            profile,
            content_source=content_source,
            vision_client=vision_client,
            text_client=text_client,
            embedding_client=embedding_client,
            artifact_lookup=artifact_lookup,
            artifact_record_lookup=artifact_record_lookup,
            images_enabled=images_enabled,
            tables_enabled=tables_enabled,
            diagrams_enabled=diagrams_enabled,
            scanned_pages_enabled=scanned_pages_enabled,
        )

    def enrich(
        self, ctx: ProjectContext, artifact_id: str,
    ) -> ArtifactProcessingResult:
        drafts: list[ArtifactDraft] = []
        cost_events: list[Any] = []
        skipped_kinds: list[str] = []
        failed_kinds: list[dict[str, str]] = []
        for enricher in self._enrichers:
            try:
                result = enricher.enrich(ctx, artifact_id)
            except Exception as exc:  # noqa: BLE001 — defensive isolation
                failed_kinds.append({
                    "kind": enricher.kind,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue
            if result.status == ResultStatus.SKIPPED:
                skipped_kinds.append(enricher.kind)
                continue
            if result.status == ResultStatus.FAILED:
                failed_kinds.append({
                    "kind": enricher.kind,
                    "error": result.error or result.message or "unknown",
                })
                continue
            drafts.extend(result.drafts)
            cost_events.extend(result.cost_events)
        # Surface composite outcome via top-level status:
        #   * Any drafts produced → SUCCEEDED.
        #   * No drafts but at least one child ran successfully (all
        #     skipped / no-op) → SUCCEEDED with empty drafts.
        #   * Every child failed → FAILED so the workflow records the
        #     enrich step as failed-optional.
        if not drafts and failed_kinds and not skipped_kinds:
            return ArtifactProcessingResult(
                status=ResultStatus.FAILED,
                error="every enricher failed",
                message="composite enricher saw no successful children",
                metadata={
                    "processor_name": self.kind,
                    "failed_kinds": failed_kinds,
                },
            )
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=drafts,
            cost_events=cost_events,
            metadata={
                "processor_name": self.kind,
                "child_count": len(self._enrichers),
                "skipped_kinds": skipped_kinds,
                "failed_kinds": failed_kinds,
            },
        )


def _construct_child(
    cls_: type[_StructuredEnricher],
    *,
    profile: Profile,
    content_source: Callable[[ProjectContext, str], bytes] | None,
    vision_client: Any | None,
    text_client: Any | None = None,
    embedding_client: Any | None = None,
    artifact_lookup: Callable[[ProjectContext, str], str | None] | None = None,
    artifact_record_lookup: Callable[[ProjectContext, str], Any] | None = None,
) -> _StructuredEnricher:
    """Build one composite child, forwarding clients per child class.

    Vision client + artifact_lookup → only `VisualContentDescriber`.
      * `vision_client` is the only client VCD specifically uses.
      * `artifact_lookup` lets VCD ask "is this artifact an image?"
        without itself depending on the registry — the wiring layer
        provides the closure. With it, VCD returns SKIPPED for
        non-image artifacts (chunks, metadata) instead of polluting
        the Visuals card with stub "Image bytes not available"
        markdown for every chunk artifact in the run.

    Text + embedding clients → forwarded to every child via the base
    `_StructuredEnricher.__init__(..., text_client=, embedding_client=)`
    kwargs. Today most concrete enrichers ignore these (their
    `_produce` methods emit empty arrays — see module docstring), but
    the wiring is in place so a future LLM-backed implementation can
    pick the clients up via `self._text_client` / `self._embedding_client`
    without re-plumbing the composite. This is the same pattern the
    `model:` slot was reserved for; it stays in place too for
    backwards-compat with adapters that read `_model`.

    Without this dispatch, the composite either:
      * silently keeps text/embedding clients out of reach for any
        future enricher implementation, OR
      * passes `vision_client=` to every child → every other enricher
        crashes the composite at startup.
    """
    common_kwargs: dict[str, Any] = {"content_source": content_source}
    if text_client is not None:
        common_kwargs["text_client"] = text_client
    if embedding_client is not None:
        common_kwargs["embedding_client"] = embedding_client
    if cls_ is VisualContentDescriber:
        return cls_(
            profile,
            vision_client=vision_client,
            artifact_lookup=artifact_lookup,
            artifact_record_lookup=artifact_record_lookup,
            **common_kwargs,
        )
    return cls_(profile, **common_kwargs)
