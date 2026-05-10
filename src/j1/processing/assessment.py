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
    """Compile-stage intensity, vendor-neutral.

    These are descriptive labels for adapters + dashboards. The
    mapping from mode → adapter config lives in the adapter (e.g.
    RAGAnything maps `fast`→`parse_method=txt`, `deep`→
    `parse_method=ocr|auto`); do NOT reference vendor parser names
    here.

      * `fast` — readable text layer, simple layout. Adapter should
        prefer the cheapest text-extraction path. Image / formula
        processing OFF unless the plan explicitly requires them.
      * `standard` — moderate complexity. Tables / images / formulas
        may exist; adapter enables them per the plan's
        `required_capabilities`. Default for unknown documents.
      * `deep` — scanned / weak text layer / complex layout. OCR
        likely required (per the `OCR` capability flag). Adapter
        enables every supported quality knob and emits warnings
        for later optimisation.
    """

    FAST = "fast"
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


# 100%-text extensions where `fast` mode is always safe. The
# guarantee operators care about: a file in this set CAN'T contain
# embedded images / tables that need VLM extraction — the bytes ARE
# the content. PDFs, DOCX, PPTX etc. are deliberately excluded
# because we can never be sure a binary container doesn't carry
# vision-only artifacts (figures, scanned regions, equation images).
#
# When growing this set:
#   * Update `_NATIVE_TEXT_EXTENSIONS` in
#     [_bridge.py](../providers/raganything/_bridge.py) in lockstep
#     — the bridge's plaintext fast-path that skips MinerU keys off
#     the same vocabulary.
#   * Add a regression test in `test_assessment_plan.py` that pins
#     the new extension to `CompileMode.FAST`.
#
# Markup / hypertext formats (`.html`, `.xml`) are intentionally
# absent — they CAN reference vision content via `<img>`, even though
# the file bytes are text — and operators usually want layout
# detection on them. Default to STANDARD; operators that know their
# corpus is plain HTML can opt into FAST via parse_method override.
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
#   * `_DENSITY_LOW`   — chars/page < 100 OR > 50% empty pages.
#                         Likely scanned / image-heavy / poorly
#                         extracted text. Bias toward deep+OCR.
#   * `_DENSITY_HIGH`  — chars/page ≥ 800 AND ≤ 20% empty pages.
#                         Text-rich, well-extracted. Supports
#                         fast/standard mode without quality risk.
#   * `_DENSITY_MEDIUM`— anything in between OR signals unknown.
#                         Planner uses other rules; density doesn't
#                         override.
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
    when most signals are unknown. Mirrors `DefaultIngestPlanner`'s
    confidence rubric.
    """

    def assess(
        self,
        profile: DocumentProfile,
        *,
        document_type: str | None = None,
    ) -> AssessmentPlan:
        plan = self._assess_inner(profile, document_type=document_type)
        # Defensive belt: FAST mode is only safe for 100%-text
        # extensions. If a future rule ever lets FAST escape for a
        # PDF / DOCX / image binary, coerce up to STANDARD here.
        # Operators reading the audit trail see the coercion in the
        # `reason` field so the override is auditable.
        return _enforce_fast_mode_safety(plan, profile)

    def _assess_inner(
        self,
        profile: DocumentProfile,
        *,
        document_type: str | None = None,
    ) -> AssessmentPlan:
        warnings: list[str] = []
        doc_type = document_type or _infer_document_type(profile)

        # Rule 1: plain text → fast.
        if profile.extension in _PLAIN_TEXT_EXTENSIONS:
            return AssessmentPlan(
                document_id=profile.document_id,
                mode=CompileMode.FAST,
                document_type=doc_type,
                complexity=Complexity.LOW,
                confidence=1.0,
                required_capabilities=frozenset({Capability.TEXT_EXTRACTION}),
                reason=(
                    f"plain-text extension {profile.extension!r}; no parser intensity needed"
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


def _enforce_fast_mode_safety(
    plan: AssessmentPlan,
    profile: DocumentProfile,
) -> AssessmentPlan:
    """Defensive coercion: FAST is only safe for 100%-text extensions.

    The user-facing rule: a PDF / DOCX / PPTX / image container
    might carry images, tables, or scanned regions that need VLM /
    OCR; we never trust FAST mode for those. If any rule branch
    above slipped FAST through for a binary extension, override to
    STANDARD here and stamp the reason so the override is auditable.

    No-op when:
      * `plan.mode != FAST` (nothing to coerce).
      * `profile.extension` IS in `_PLAIN_TEXT_EXTENSIONS` (FAST is
        the correct mode and stays).
    """
    if plan.mode != CompileMode.FAST:
        return plan
    if profile.extension in _PLAIN_TEXT_EXTENSIONS:
        return plan
    # Coerce. Replace mode, augment required capabilities to the
    # STANDARD baseline, append the override reason so audit logs
    # show why FAST was overridden. Capabilities the original rule
    # already required are kept (don't downgrade).
    coerced_required = frozenset(plan.required_capabilities | {
        Capability.TEXT_EXTRACTION,
        Capability.LAYOUT_DETECTION,
    })
    return AssessmentPlan(
        document_id=plan.document_id,
        mode=CompileMode.STANDARD,
        document_type=plan.document_type,
        complexity=plan.complexity,
        confidence=plan.confidence,
        required_capabilities=coerced_required,
        optional_capabilities=plan.optional_capabilities,
        risk_flags=plan.risk_flags,
        fallback_policy=plan.fallback_policy,
        reason=(
            f"{plan.reason} | coerced FAST→STANDARD: extension "
            f"{profile.extension!r} is not in the 100%-text set "
            "(binary containers may carry images / tables / scanned "
            "regions that need standard mode)"
        ),
    )


def _infer_document_type(profile: DocumentProfile) -> str:
    """Derive a coarse document_type from extension. Operators with a
    real classifier (LLM-based, mime-deep) wire their own
    `AssessmentPlanner` and call this only as a fallback."""
    ext = profile.extension.lstrip(".")
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
    """Same rubric as `DefaultIngestPlanner._confidence` — counts
    populated signals, remaps to 0.5..1.0. Duplicated to avoid
    cross-module imports."""
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
#   * `fail_open` (default) — workflow logs the error, sets the
#     compile activity's `assessment_plan_payload=None`, and lets
#     the bridge fall back to `settings.parse_method`. Production
#     prefers this: a degenerate profile shouldn't block ingestion.
#   * `fail_closed` — workflow raises `_BusinessRejection`, the
#     compile step is recorded FAILED, and the run lands at
#     FAILED_FINAL. Useful for compliance deployments that require
#     explicit per-document plans.
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
