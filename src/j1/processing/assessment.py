"""Vendor-neutral Assessment Plan for the compile stage.

An `AssessmentPlan` describes the *intent* of compile for one
document — what mode (fast/standard/deep), which capabilities the
adapter must enable (text/layout/table/image/formula/ocr), and the
risk flags / fallback policy. It does NOT contain vendor-specific
fields (no `parse_method`, no `parser`, no `mineru_*`).

The adapter (e.g.
[`j1.providers.raganything.plan_mapper`](../providers/raganything/plan_mapper.py))
is responsible for translating an AssessmentPlan into its own
runtime config. If a different compiler ships later (Unstructured,
custom) it brings its own mapper; the core flow doesn't change.

Distinct from `IngestPlan` ([planning.py](./planning.py)):
 * `IngestPlan` decides WHICH stages run (compile / enrich / graph
 / index) — stage gating.
 * `AssessmentPlan` decides HOW the compile stage runs — parser
 intensity + per-capability toggles.

Both are derived from the same `DocumentProfile` and travel side by
side. They're separate because conflating them produces a 7-mode ×
4-stage matrix that's hard to reason about.

Field hygiene: all fields are short operational values. `reason` is
a human-readable explanation of WHY the plan picked this mode;
`warnings` are flags the adapter should record on the result for
later optimisation passes. Never put document content here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from j1.processing.profiling import DocumentProfile


class CompileMode(StrEnum):
    """Compile-stage intensity, vendor-neutral. Two official modes:
 `standard` and `deep`.

 These are descriptive labels for adapters + dashboards. The
 mapping from mode → adapter config lives in the adapter (e.g.
 RAGAnything maps `standard`→`parse_method=auto`, `deep`→
 `parse_method=ocr|auto`); do NOT reference vendor parser names
 here.

 * `standard` — reliable default for normal text-first
 documents. Adapter runs the standard parse path with
 capability flags from the plan; quality gates still apply.
 Does NOT mean "fast" — it means "normal reliable compile".
 * `deep` — complex / low-confidence / multimodal / scanned /
 layout-heavy documents. Adapter enables every supported
 quality knob (OCR, layout, multimodal) and emits warnings
 for later optimisation.

 Legacy ``"fast"`` payloads from before the two-mode refactor
 are tolerated on the read path: ``AssessmentPlan.from_payload``
 catches the ``ValueError`` and falls back to ``STANDARD``. The
 planner never emits any value outside this enum."""

    STANDARD = "standard"
    DEEP = "deep"


class Capability(StrEnum):
    """Vendor-neutral compile capabilities the adapter MUST attempt
 when listed in `AssessmentPlan.required_capabilities`. The
 adapter MAY also enable them when listed in `optional_capabilities`
 — that's a hint, not a contract.

 `text_extraction` is implicitly required by every mode; listing
 it explicitly is harmless. The other six are opt-in per
 document.
 """

    TEXT_EXTRACTION = "text_extraction"
    LAYOUT_DETECTION = "layout_detection"
    TABLE_EXTRACTION = "table_extraction"
    IMAGE_EXTRACTION = "image_extraction"
    FORMULA_EXTRACTION = "formula_extraction"
    OCR = "ocr"


class Complexity(StrEnum):
    """Operator-facing complexity bucket. Drives dashboard filters
 and helps explain WHY a `deep` mode was selected without forcing
 the operator to re-derive from raw signals."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FallbackPolicy(StrEnum):
    """How the adapter should react when a required capability isn't
 supported by the underlying parser.

 * `degrade_with_warning` (default) — adapter omits the
 capability + records a warning on the compile result. The
 run continues; later optimisation passes can decide what
 to do. THIS IS WHAT THE SPEC ASKS FOR (`degrade gracefully
 and record a warning/flag for later processing`).
 * `fail` — adapter aborts the compile with a clear error.
 Useful for callers that want hard guarantees (e.g. a
 compliance run that MUST OCR every page).
 """

    DEGRADE_WITH_WARNING = "degrade_with_warning"
    FAIL = "fail"


class RecommendedProcessingPath(StrEnum):
    """Operator-facing recommended processing path. One value per
 canonical Compile-Stage outcome the planner can recommend.

 Two-mode model: every non-skip recommendation maps onto either
 STANDARD_COMPILE or DEEP_COMPILE, mirroring `CompileMode`.

 * `STANDARD_COMPILE` — reliable default. Maps to
 `CompileMode.STANDARD`. Used for normal text-first docs,
 including 100%-text formats (the bridge takes a plaintext
 bypass for those, but they still report standard mode).
 * `DEEP_COMPILE` — richer parsing for scanned, layout-heavy,
 multimodal, OCR-required, or low-confidence documents.
 Maps to `CompileMode.DEEP`.
 * `SKIP_EMPTY_DOCUMENT` — profile shows non-zero page count
 but every content signal is zero. Surfaced by the
 post-compile enrich assessor; the planner mirrors it so
 the FE has a single canonical signal.
 * `FAILED` — assessment couldn't complete (profile build
 failure, missing extension, etc.).

 Pre-two-mode payloads (`fast_text_compile`, `multimodal_compile`,
 `ocr_parse`) are tolerated on the read path — `from_payload`
 falls back to `STANDARD_COMPILE` for any value not in the enum.

 Wire strings are stable — dashboards key off them, FE renders
 human-readable labels via a separate mapper."""

    STANDARD_COMPILE = "standard_compile"
    DEEP_COMPILE = "deep_compile"
    SKIP_EMPTY_DOCUMENT = "skip_empty_document"
    FAILED = "failed"


@dataclass(frozen=True)
class AssessmentPlan:
    """The planner's compile-stage decision for one document.

 Vendor-neutral. The adapter consumes it via its own mapper
 function (e.g. `map_assessment_to_raganything_config`).

 `mode` is the high-level intensity. `required_capabilities` is
 the minimum the adapter MUST attempt; `optional_capabilities`
 is hints the adapter MAY enable when supported. `risk_flags`
 surfaces signals the planner couldn't fully address (e.g.
 "scanned PDF but OCR not yet wired") so a later optimisation
 pass can revisit. `reason` is a one-line explanation operators
 read first when triaging.
 """

    document_id: str
    mode: CompileMode
    document_type: str
    complexity: Complexity
    confidence: float  # 0..1
    required_capabilities: frozenset[Capability] = field(default_factory=frozenset)
    optional_capabilities: frozenset[Capability] = field(default_factory=frozenset)
    risk_flags: tuple[str, ...] = ()
    fallback_policy: FallbackPolicy = FallbackPolicy.DEGRADE_WITH_WARNING
    reason: str = ""
    # Operator-facing intent — distinct from `mode` (the adapter
    # intensity knob). The planner sets this from `mode + profile`;
    # operators read it as the canonical "what J1 plans to do next"
    # signal. Defaults to STANDARD_COMPILE which is the safe
    # interpretation when the planner couldn't decide.
    recommended_path: RecommendedProcessingPath = (
        RecommendedProcessingPath.STANDARD_COMPILE
    )

    def requires(self, capability: Capability) -> bool:
        return capability in self.required_capabilities

    def is_helpful(self, capability: Capability) -> bool:
        """True if the capability is required OR optional. Adapters
 that find a capability cheap to enable can use this to
 decide whether to bother."""
        return (
            capability in self.required_capabilities
            or capability in self.optional_capabilities
        )

    def to_payload(self) -> dict:
        """JSON-friendly dict for Temporal data-converter transit.
 Round-trips via `from_payload`. Used by the workflow →
 compile-activity boundary so the dataclass doesn't need to
 be serialised by the Temporal codec directly (frozensets /
 StrEnum need explicit handling)."""
        return {
            "schema_version": "1",
            "document_id": self.document_id,
            "mode": self.mode.value,
            "document_type": self.document_type,
            "complexity": self.complexity.value,
            "confidence": self.confidence,
            "required_capabilities": sorted(c.value for c in self.required_capabilities),
            "optional_capabilities": sorted(c.value for c in self.optional_capabilities),
            "risk_flags": list(self.risk_flags),
            "fallback_policy": self.fallback_policy.value,
            "reason": self.reason,
            "recommended_path": self.recommended_path.value,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "AssessmentPlan":
        """Inverse of `to_payload`. Tolerates unknown values (drops
 them) so a payload written by a future planner version that
 adds a new capability doesn't crash an older worker."""
        def _to_caps(values) -> frozenset[Capability]:
            out: set[Capability] = set()
            for v in values or ():
                try:
                    out.add(Capability(v))
                except ValueError:
                    continue
            return frozenset(out)

        try:
            mode = CompileMode(payload.get("mode", "standard"))
        except ValueError:
            mode = CompileMode.STANDARD
        try:
            complexity = Complexity(payload.get("complexity", "medium"))
        except ValueError:
            complexity = Complexity.MEDIUM
        try:
            policy = FallbackPolicy(
                payload.get("fallback_policy", "degrade_with_warning"),
            )
        except ValueError:
            policy = FallbackPolicy.DEGRADE_WITH_WARNING
        # `recommended_path` is additive — older payloads omit it
        # or carry pre-two-mode values. Tolerate missing / unknown
        # values rather than crashing replay; unknown coerces to
        # STANDARD_COMPILE (see ``_coerce_legacy_recommended_path``).
        raw_path = payload.get("recommended_path")
        path = _coerce_legacy_recommended_path(raw_path)
        return cls(
            document_id=str(payload.get("document_id", "")),
            mode=mode,
            document_type=str(payload.get("document_type", "unknown")),
            complexity=complexity,
            confidence=float(payload.get("confidence", 0.5)),
            required_capabilities=_to_caps(payload.get("required_capabilities")),
            optional_capabilities=_to_caps(payload.get("optional_capabilities")),
            risk_flags=tuple(payload.get("risk_flags") or ()),
            fallback_policy=policy,
            reason=str(payload.get("reason", "")),
            recommended_path=path,
        )


class AssessmentPlanner:
    """Planner interface. Implementations MUST be deterministic with
 respect to (profile, policy_overrides) so workflow replay
 produces stable plans."""

    def assess(
        self,
        profile: DocumentProfile,
        *,
        document_type: str | None = None,
    ) -> AssessmentPlan:
        raise NotImplementedError


# ---- Default rule-based planner ------------------------------------


# 100%-text extensions where the bridge's plaintext fast-path can
# bypass MinerU entirely. The guarantee operators care about: a
# file in this set CAN'T contain embedded images / tables that
# need VLM extraction — the bytes ARE the content. PDFs, DOCX,
# PPTX etc. are deliberately excluded because we can never be
# sure a binary container doesn't carry vision-only artifacts
# (figures, scanned regions, equation images).
#
# When growing this set, update `_NATIVE_TEXT_EXTENSIONS` in
# [_bridge.py](../providers/raganything/_bridge.py) in lockstep —
# the bridge's plaintext fast-path keys off the same vocabulary.
#
# Markup / hypertext formats (`.html`, `.xml`) are intentionally
# absent — they CAN reference vision content via `<img>`, even though
# the file bytes are text — and operators usually want layout
# detection on them. Default to STANDARD; the bridge's plaintext
# bypass is independent of `CompileMode`.
_PLAIN_TEXT_EXTENSIONS: frozenset[str] = frozenset({
    # Documentation / log formats.
    ".txt", ".md", ".markdown", ".rst", ".log",
    # Structured-data text formats. Bytes ARE the content; no
    # embedded vision artifacts possible.
    ".json", ".jsonl", ".ndjson",
    ".yaml", ".yml",
    ".toml",
    ".tsv",
    # Config / data formats.
    ".ini", ".cfg", ".conf", ".env",
})

# Extensions whose contents are typically scanned (need OCR).
_LIKELY_SCANNED_EXTENSIONS: frozenset[str] = frozenset({
    ".tiff", ".tif", ".bmp",
})

# Tabular extensions — biases toward standard mode + table extraction.
_LIKELY_TABLE_EXTENSIONS: frozenset[str] = frozenset({
    ".xls", ".xlsx", ".csv", ".ods",
})

# Density classification — the planner reads
# `total_text_chars` + `page_count` + `empty_page_ratio` from the
# DocumentProfile (populated by the lightweight pypdf-based
# profiler) and buckets the document into one of three signals:
#
#  * `_DENSITY_LOW` — chars/page < 100 OR > 50% empty pages.
#  Likely scanned / image-heavy / poorly
#  extracted text. Bias toward deep+OCR.
#  * `_DENSITY_HIGH` — chars/page ≥ 800 AND ≤ 20% empty pages.
#  Text-rich, well-extracted. Supports
#  fast/standard mode without quality risk.
#  * `_DENSITY_MEDIUM`— anything in between OR signals unknown.
#  Planner uses other rules; density doesn't
#  override.
#
# Thresholds are deliberately wide. A normal book page is ~2500
# chars; a Word-export PDF with margin chrome is ~600 — both fall
# in the high-density range. A scan-with-OCR-text page often has
# ~50 chars (page numbers + headers leaked through). Operators
# tuning for unusual documents can wire a `MinerUProfilerAdapter`
# behind the same Protocol later.
_DENSITY_LOW = "low"
_DENSITY_MEDIUM = "medium"
_DENSITY_HIGH = "high"

_DENSITY_LOW_AVG_CHARS_PER_PAGE = 100
_DENSITY_LOW_EMPTY_PAGE_RATIO = 0.5
_DENSITY_HIGH_AVG_CHARS_PER_PAGE = 800
_DENSITY_HIGH_EMPTY_PAGE_RATIO = 0.2


def _classify_density(profile: DocumentProfile) -> str:
    """Bucket `profile` into LOW / MEDIUM / HIGH text density.

 Pure derivation — uses only fields the lightweight profiler
 populates (`total_text_chars`, `page_count`, `empty_page_ratio`).
 Returns `MEDIUM` whenever the inputs aren't available so the
 classifier never flips a decision based on missing data."""
    total = profile.total_text_chars
    pages = profile.page_count or 0
    empty_ratio = profile.empty_page_ratio
    if total is None or pages <= 0:
        return _DENSITY_MEDIUM
    avg_chars = total / pages
    # LOW: very few chars per page OR most sampled pages were empty.
    if avg_chars < _DENSITY_LOW_AVG_CHARS_PER_PAGE:
        return _DENSITY_LOW
    if (
        empty_ratio is not None
        and empty_ratio > _DENSITY_LOW_EMPTY_PAGE_RATIO
    ):
        return _DENSITY_LOW
    # HIGH: substantial text per page AND most sampled pages had text.
    if avg_chars >= _DENSITY_HIGH_AVG_CHARS_PER_PAGE and (
        empty_ratio is None or empty_ratio <= _DENSITY_HIGH_EMPTY_PAGE_RATIO
    ):
        return _DENSITY_HIGH
    return _DENSITY_MEDIUM


@dataclass(frozen=True)
class DefaultAssessmentPlanner(AssessmentPlanner):
    """Deterministic rule-based AssessmentPlanner.

 Decision tree, applied in order:

 1. Plain-text extensions (`.txt` / `.md` / etc.) → `fast`.
 2. Scanned PDF / weak text layer / scan-only image extension
 → `deep` with `OCR` required.
 3. Tabular extensions → `standard` with `TABLE_EXTRACTION`
 required.
 4. PDFs with images flagged → `standard` with
 `IMAGE_EXTRACTION` optional.
 5. Default → `standard` with text + layout required.

 Confidence: 1.0 when every relevant signal is known; 0.7 when
 one major signal (e.g. text_extractable_ratio) is unknown; 0.5
 when most signals are unknown. Mirrors the retired planning
 implementation's confidence rubric.
 """

    def assess(
        self,
        profile: DocumentProfile,
        *,
        document_type: str | None = None,
    ) -> AssessmentPlan:
        plan = self._assess_inner(profile, document_type=document_type)
        # Stamp `recommended_path` as the LAST step so it always
        # reflects the rule-emitted `mode + capabilities`. Single
        # source of truth for the mode → operator-intent mapping —
        # every rule branch flows through here.
        return _stamp_recommended_path(plan, profile)

    def _assess_inner(
        self,
        profile: DocumentProfile,
        *,
        document_type: str | None = None,
    ) -> AssessmentPlan:
        warnings: list[str] = []
        doc_type = document_type or _infer_document_type(profile)

        # Rule 1: plain text → STANDARD with the plaintext-bypass
        # hint in the reason. The bridge takes a fast plaintext
        # path for these extensions (skips MinerU), but the
        # compile mode itself is STANDARD — the two-mode model has
        # no FAST. Operators reading the audit see why this is
        # cheap even though the mode is "standard".
        if profile.extension in _PLAIN_TEXT_EXTENSIONS:
            return AssessmentPlan(
                document_id=profile.document_id,
                mode=CompileMode.STANDARD,
                document_type=doc_type,
                complexity=Complexity.LOW,
                confidence=1.0,
                required_capabilities=frozenset({Capability.TEXT_EXTRACTION}),
                reason=(
                    f"plain-text extension {profile.extension!r}; "
                    "bridge uses the plaintext bypass — no MinerU/VLM."
                ),
            )

        # Density-derived bias. `avg_chars_per_page` is the cheapest
        # text-density estimate the lightweight profiler can produce
        # (total_text_chars / page_count). Combined with
        # `non_empty_page_ratio` (= 1 - empty_page_ratio) it tells
        # us how text-rich the document REALLY is, beyond the
        # binary `text_extractable_ratio`. Values below the
        # `_DENSITY_LOW_*` thresholds bias toward deep+OCR; values
        # above the `_DENSITY_HIGH_*` thresholds support fast/standard.
        density_bias = _classify_density(profile)

        # Rule 2: scanned / weak text layer / scan-only extension → deep + OCR.
        scanned = (
            profile.has_scanned_pages is True
            or (
                profile.text_extractable_ratio is not None
                and profile.text_extractable_ratio < 0.1
            )
            or profile.extension in _LIKELY_SCANNED_EXTENSIONS
            # Density-driven OCR trigger: even when the per-page text
            # presence sample didn't trip the `< 0.1` ratio, an
            # avg-chars-per-page < threshold OR low non_empty ratio
            # signals a scanned-like document the binary check missed.
            or density_bias == _DENSITY_LOW
        )
        if scanned:
            required = {
                Capability.TEXT_EXTRACTION,
                Capability.LAYOUT_DETECTION,
                Capability.OCR,
            }
            if profile.has_tables is True:
                required.add(Capability.TABLE_EXTRACTION)
            if profile.has_images is True:
                required.add(Capability.IMAGE_EXTRACTION)
            # Density flag enriches the reason string so operators
            # reading the audit trail see WHY OCR fired — was it the
            # binary text-ratio rule, the scan-only extension, or
            # the density classifier? All three can trigger this
            # branch and they're operationally distinct.
            reason = (
                "scanned PDF or text_extractable_ratio < 0.1; "
                "OCR required, full layout analysis"
            )
            if density_bias == _DENSITY_LOW:
                avg = (
                    (profile.total_text_chars or 0)
                    / (profile.page_count or 1)
                )
                reason = (
                    f"density signals indicate scanned-like document "
                    f"(avg_chars_per_page≈{avg:.0f}, "
                    f"empty_page_ratio={profile.empty_page_ratio}); "
                    "OCR required, full layout analysis"
                )
            return AssessmentPlan(
                document_id=profile.document_id,
                mode=CompileMode.DEEP,
                document_type=doc_type,
                complexity=Complexity.HIGH,
                confidence=_confidence(profile),
                required_capabilities=frozenset(required),
                optional_capabilities=frozenset({
                    Capability.IMAGE_EXTRACTION,
                    Capability.FORMULA_EXTRACTION,
                    Capability.TABLE_EXTRACTION,
                }) - frozenset(required),
                risk_flags=(
                    "scanned_or_low_text_layer; quality may degrade",
                ),
                reason=reason,
            )

        # Rule 3: tabular extension → standard + table extraction.
        if profile.extension in _LIKELY_TABLE_EXTENSIONS:
            return AssessmentPlan(
                document_id=profile.document_id,
                mode=CompileMode.STANDARD,
                document_type=doc_type,
                complexity=Complexity.MEDIUM,
                confidence=_confidence(profile),
                required_capabilities=frozenset({
                    Capability.TEXT_EXTRACTION,
                    Capability.TABLE_EXTRACTION,
                }),
                reason=(
                    f"tabular extension {profile.extension!r}; "
                    "table extraction required"
                ),
            )

        # Rule 4: PDF (or other) with images / tables / equations flagged.
        required: set[Capability] = {
            Capability.TEXT_EXTRACTION, Capability.LAYOUT_DETECTION,
        }
        optional: set[Capability] = set()
        if profile.has_tables is True:
            required.add(Capability.TABLE_EXTRACTION)
        elif profile.has_tables is None:
            optional.add(Capability.TABLE_EXTRACTION)
        if profile.has_images is True:
            optional.add(Capability.IMAGE_EXTRACTION)
        # Formulas: profilers don't surface this signal yet, but the
        # contract reserves the slot — adapter mapper degrades when the
        # underlying parser doesn't support it.
        if profile.equation_count is not None and profile.equation_count > 0:
            required.add(Capability.FORMULA_EXTRACTION)

        complexity = Complexity.MEDIUM
        if profile.layout_complexity_score is not None:
            if profile.layout_complexity_score >= 0.7:
                complexity = Complexity.HIGH
            elif profile.layout_complexity_score < 0.3:
                complexity = Complexity.LOW
        # Density bias: a clearly text-rich document with no
        # `layout_complexity_score` flag-out is downgraded to
        # `Complexity.LOW` so the FE / operator dashboard sees
        # "this should be cheap to compile" without waiting for the
        # post-compile manifest. Doesn't downgrade DEEP — that path
        # already returned above.
        if (
            density_bias == _DENSITY_HIGH
            and profile.layout_complexity_score is None
        ):
            complexity = Complexity.LOW

        # Rule 5 — text-extractable + simple → still STANDARD by
        # default (operators upgrade via deep policy elsewhere). FAST
        # mode for non-plain-text is reserved for the fast-path
        # heuristic in the bridge (extractable PDF) which sits OUTSIDE
        # the plan — see `_is_text_extractable_pdf` in _bridge.py.
        reason = (
            f"default standard mode for {profile.extension!r}; "
            f"layout + text required"
        )
        if density_bias == _DENSITY_HIGH:
            avg = (
                (profile.total_text_chars or 0)
                / (profile.page_count or 1)
            )
            reason = (
                f"density signals indicate text-rich document "
                f"(avg_chars_per_page≈{avg:.0f}, "
                f"empty_page_ratio={profile.empty_page_ratio}); "
                "standard mode supports fast extraction"
            )
        return AssessmentPlan(
            document_id=profile.document_id,
            mode=CompileMode.STANDARD,
            document_type=doc_type,
            complexity=complexity,
            confidence=_confidence(profile),
            required_capabilities=frozenset(required),
            optional_capabilities=frozenset(optional - required),
            reason=reason,
        )


def _coerce_legacy_recommended_path(
    raw: object,
) -> RecommendedProcessingPath:
    """Map any value onto the two-mode ``RecommendedProcessingPath``
    vocabulary. Pure / safe / never raises — used on the
    deserialisation path where pre-two-mode payloads (``fast_text_compile``,
    ``multimodal_compile``, ``ocr_parse``) still flow through.
    Unknown values fall back to ``STANDARD_COMPILE`` — the load-
    bearing ``mode`` field on the same payload still determines
    actual compile behaviour."""
    if isinstance(raw, RecommendedProcessingPath):
        return raw
    if not isinstance(raw, str):
        return RecommendedProcessingPath.STANDARD_COMPILE
    cleaned = raw.strip().lower()
    try:
        return RecommendedProcessingPath(cleaned)
    except ValueError:
        return RecommendedProcessingPath.STANDARD_COMPILE


def _stamp_recommended_path(
    plan: AssessmentPlan,
    profile: DocumentProfile,
) -> AssessmentPlan:
    """Derive `recommended_path` from `plan.mode` (post-coercion)
 and return a new plan with the field stamped.

 Two-mode model: every non-skip outcome maps to either
 STANDARD_COMPILE or DEEP_COMPILE — the recommended-path enum
 mirrors `CompileMode`.

 Decision table:
 * `mode == DEEP` → DEEP_COMPILE
 * `Capability.OCR` in required (and mode!=DEEP,
 which shouldn't happen but defend against it) → DEEP_COMPILE
 * `mode == STANDARD` → STANDARD_COMPILE
 * `mode == FAST` (legacy, post-belt should never
 see this; coerce defensively) → STANDARD_COMPILE
 * Anything else → STANDARD_COMPILE

 SKIP_EMPTY_DOCUMENT is NOT emitted here — that verdict lives on
 the post-compile enrich plan because it requires content-level
 signals (zero text/image/table counts despite a non-zero page
 count) that aren't available pre-compile."""
    if plan.mode == CompileMode.DEEP:
        path = RecommendedProcessingPath.DEEP_COMPILE
    elif Capability.OCR in plan.required_capabilities:
        path = RecommendedProcessingPath.DEEP_COMPILE
    else:
        # STANDARD or legacy FAST (the safety belt above already
        # coerced FAST → STANDARD; this default is belt-and-
        # suspenders for any code path that constructs an
        # AssessmentPlan manually without going through `assess`).
        path = RecommendedProcessingPath.STANDARD_COMPILE
    if plan.recommended_path == path:
        return plan
    # Rebuild with the new field — AssessmentPlan is frozen.
    return AssessmentPlan(
        document_id=plan.document_id,
        mode=plan.mode,
        document_type=plan.document_type,
        complexity=plan.complexity,
        confidence=plan.confidence,
        required_capabilities=plan.required_capabilities,
        optional_capabilities=plan.optional_capabilities,
        risk_flags=plan.risk_flags,
        fallback_policy=plan.fallback_policy,
        reason=plan.reason,
        recommended_path=path,
    )


def _infer_document_type(profile: DocumentProfile) -> str:
    """Derive a coarse document_type from extension. Operators with a
 real classifier (LLM-based, mime-deep) wire their own
 `AssessmentPlanner` and call this only as a fallback.

 Defensive: `profile.extension` is typed `str` on the dataclass but
 a `None` can sneak through if the field round-trips through a
 data converter that loses required-field defaults (Temporal's
 JSON converter does this for some payload shapes). Treat any
 non-string as the "no extension" case rather than crashing the
 planner — empty extension already maps to `"unknown"`."""
    raw = getattr(profile, "extension", None)
    if not isinstance(raw, str):
        return "unknown"
    ext = raw.lstrip(".")
    if not ext:
        return "unknown"
    if ext in {"txt", "md", "markdown", "rst", "log"}:
        return "plain_text"
    if ext in {"xls", "xlsx", "csv", "ods"}:
        return "spreadsheet"
    if ext == "pdf":
        return "pdf"
    if ext in {"doc", "docx", "rtf"}:
        return "word_document"
    if ext in {"ppt", "pptx", "key"}:
        return "presentation"
    if ext in {"png", "jpg", "jpeg", "tiff", "tif", "bmp", "gif"}:
        return "image"
    if ext in {"html", "htm"}:
        return "html"
    return ext


def _confidence(profile: DocumentProfile) -> float:
    """Same rubric as the retired planning implementation's
 `_confidence` — counts populated signals, remaps to 0.5..1.0.
 Duplicated to avoid cross-module imports."""
    signals = (
        profile.text_extractable_ratio,
        profile.has_images,
        profile.has_tables,
        profile.has_scanned_pages,
    )
    populated = sum(1 for s in signals if s is not None)
    if populated == 4:
        return 1.0
    if populated >= 3:
        return 0.85
    if populated >= 1:
        return 0.7
    return 0.5


# ---- Workflow-level failure policy --------------------------------


# What to do when AssessmentPlan construction itself fails (e.g.
# planner raised, profile is missing required fields). Distinct from
# `FallbackPolicy` (which controls per-capability degradation
# INSIDE a successfully-built plan).
#
#  * `fail_open` (default) — workflow logs the error, sets the
#  compile activity's `assessment_plan_payload=None`, and lets
#  the bridge fall back to `settings.parse_method`. Production
#  prefers this: a degenerate profile shouldn't block ingestion.
#  * `fail_closed` — workflow raises `_BusinessRejection`, the
#  compile step is recorded FAILED, and the run lands at
#  FAILED_FINAL. Useful for compliance deployments that require
#  explicit per-document plans.
#
# Set via `J1_ASSESSMENT_FAILURE_POLICY=fail_open|fail_closed`.
ASSESSMENT_FAILURE_POLICY_FAIL_OPEN = "fail_open"
ASSESSMENT_FAILURE_POLICY_FAIL_CLOSED = "fail_closed"
DEFAULT_ASSESSMENT_FAILURE_POLICY = ASSESSMENT_FAILURE_POLICY_FAIL_OPEN
ENV_ASSESSMENT_FAILURE_POLICY = "J1_ASSESSMENT_FAILURE_POLICY"

_VALID_FAILURE_POLICIES = frozenset({
    ASSESSMENT_FAILURE_POLICY_FAIL_OPEN,
    ASSESSMENT_FAILURE_POLICY_FAIL_CLOSED,
})


def load_assessment_failure_policy(env: dict | None = None) -> str:
    """Read `J1_ASSESSMENT_FAILURE_POLICY` from `env` (or `os.environ`
 when None). Unknown values quietly downgrade to `fail_open` —
 the assessment plan exists for cost optimisation, not as a
 correctness gate, so a typo in the env shouldn't break ingest.
 """
    import os
    source = env if env is not None else os.environ
    raw = source.get(ENV_ASSESSMENT_FAILURE_POLICY)
    if not raw:
        return DEFAULT_ASSESSMENT_FAILURE_POLICY
    value = raw.strip().lower()
    if value in _VALID_FAILURE_POLICIES:
        return value
    return DEFAULT_ASSESSMENT_FAILURE_POLICY


__all__ = [
    "ASSESSMENT_FAILURE_POLICY_FAIL_CLOSED",
    "ASSESSMENT_FAILURE_POLICY_FAIL_OPEN",
    "AssessmentPlan",
    "AssessmentPlanner",
    "Capability",
    "CompileMode",
    "Complexity",
    "DEFAULT_ASSESSMENT_FAILURE_POLICY",
    "DefaultAssessmentPlanner",
    "ENV_ASSESSMENT_FAILURE_POLICY",
    "FallbackPolicy",
    "load_assessment_failure_policy",
]
