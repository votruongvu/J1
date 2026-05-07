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
        self._model = model

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
        return {
            "processor_name": self.kind,
            "processor_version": self.version,
            "artifact_type": self.artifact_type,
            "confidence": f"{self.confidence_default:.3f}",
            "review_required": "true" if self.review_required_default else "false",
            "source_artifact_id": artifact_id,
            "prompt_name": self.prompt_name,
        }

    def _read_content(self, ctx: ProjectContext, artifact_id: str) -> bytes:
        if self._content_source is None:
            return b""
        return self._content_source(ctx, artifact_id)

    def _profile_prompt(self) -> str:
        if not self.prompt_name:
            return ""
        return self._profile.prompts.get(self.prompt_name, "")


class DocumentClassifier(_StructuredEnricher):
    kind = PROCESSOR_DOCUMENT_CLASSIFIER
    artifact_type = ARTIFACT_TYPE_DOCUMENT_MAP
    prompt_name = "classify_document"
    confidence_default = 0.7

    def _produce(self, ctx, artifact_id):
        content = self._read_content(ctx, artifact_id)
        prompt = self._profile_prompt()
        json_data = {
            "source_artifact_id": artifact_id,
            "classification": [],
            "sections": [],
            "byte_size": len(content),
            "prompt_used": bool(prompt),
        }
        md_text = (
            "# Document map\n\n"
            f"Source artifact: `{artifact_id}`\n\n"
            "Sections: 0\n"
        )
        return json_data, md_text


class RequirementExtractor(_StructuredEnricher):
    kind = PROCESSOR_REQUIREMENT_EXTRACTOR
    artifact_type = ARTIFACT_TYPE_REQUIREMENTS
    prompt_name = "extract_requirements"
    confidence_default = 0.6

    def _produce(self, ctx, artifact_id):
        json_data = {
            "source_artifact_id": artifact_id,
            "requirements": [],
            "prompt_used": bool(self._profile_prompt()),
        }
        md_text = (
            "# Requirements\n\n"
            f"Source artifact: `{artifact_id}`\n\n"
            "Total: 0\n"
        )
        return json_data, md_text


class TableExtractor(_StructuredEnricher):
    kind = PROCESSOR_TABLE_EXTRACTOR
    artifact_type = ARTIFACT_TYPE_TABLES
    prompt_name = "extract_tables"
    confidence_default = 0.7

    def _produce(self, ctx, artifact_id):
        json_data = {"source_artifact_id": artifact_id, "tables": []}
        md_text = (
            "# Tables\n\n"
            f"Source artifact: `{artifact_id}`\n\n"
            "Total: 0\n"
        )
        return json_data, md_text


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
        **kwargs: Any,
    ) -> None:
        super().__init__(profile, **kwargs)
        self._vision_client = vision_client

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


class FormulaExtractor(_StructuredEnricher):
    kind = PROCESSOR_FORMULA_EXTRACTOR
    artifact_type = ARTIFACT_TYPE_FORMULAS
    prompt_name = "extract_formulas"
    confidence_default = 0.5
    review_required_default = True

    def _produce(self, ctx, artifact_id):
        json_data = {"source_artifact_id": artifact_id, "formulas": []}
        md_text = (
            "# Formulas\n\n"
            f"Source artifact: `{artifact_id}`\n\n"
            "_Pending human review._\n"
        )
        return json_data, md_text


class RiskExtractor(_StructuredEnricher):
    kind = PROCESSOR_RISK_EXTRACTOR
    artifact_type = ARTIFACT_TYPE_RISKS
    prompt_name = "extract_risks"
    confidence_default = 0.6

    def _produce(self, ctx, artifact_id):
        json_data = {"source_artifact_id": artifact_id, "risks": []}
        md_text = (
            "# Risks\n\n"
            f"Source artifact: `{artifact_id}`\n\n"
            "Total: 0\n"
        )
        return json_data, md_text


class ConsistencyChecker(_StructuredEnricher):
    kind = PROCESSOR_CONSISTENCY_CHECKER
    artifact_type = ARTIFACT_TYPE_CONSISTENCY_FINDINGS
    prompt_name = "check_consistency"
    confidence_default = 0.5
    review_required_default = True

    def _produce(self, ctx, artifact_id):
        json_data = {"source_artifact_id": artifact_id, "findings": []}
        md_text = (
            "# Consistency findings\n\n"
            f"Source artifact: `{artifact_id}`\n\n"
            "_Pending human review._\n"
        )
        return json_data, md_text


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


class ConfidenceAssessor(_StructuredEnricher):
    kind = PROCESSOR_CONFIDENCE_ASSESSOR
    artifact_type = ARTIFACT_TYPE_CONFIDENCE_ASSESSMENT
    prompt_name = "assess_confidence"
    confidence_default = 0.8

    def _produce(self, ctx, artifact_id):
        json_data = {
            "source_artifact_id": artifact_id,
            "assessments": [],
            "default_confidence": self.confidence_default,
        }
        md_text = (
            "# Confidence assessment\n\n"
            f"Source artifact: `{artifact_id}`\n\n"
            f"Default confidence: {self.confidence_default:.3f}\n"
        )
        return json_data, md_text


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
    ) -> None:
        if enrichers is None:
            enrichers = tuple(
                _construct_child(
                    cls_,
                    profile=profile,
                    content_source=content_source,
                    vision_client=vision_client,
                )
                for cls_ in GENERIC_ENRICHERS
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
    ) -> "CompositeEnricher":
        return cls(
            profile,
            content_source=content_source,
            vision_client=vision_client,
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
) -> _StructuredEnricher:
    """Build one composite child, forwarding `vision_client` only to
    enrichers that accept it.

    Currently only `VisualContentDescriber` accepts a `vision_client`
    constructor kwarg — every other generic enricher's signature is
    `(profile, *, enabled=..., version=..., confidence=...,
    review_required=..., content_source=..., model=...)`. Passing a
    spurious `vision_client=` to those would raise `TypeError` at
    construction.

    Without this dispatch, the composite either:
      * builds VCD with `vision_client=None` → emits the
        'No vision LLM configured — visual enrichment skipped'
        markdown stub on every run, OR
      * passes `vision_client=` to every child → every other enricher
        crashes the composite at startup.
    """
    if cls_ is VisualContentDescriber:
        return cls_(
            profile,
            content_source=content_source,
            vision_client=vision_client,
        )
    return cls_(profile, content_source=content_source)
