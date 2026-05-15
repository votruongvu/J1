"""PR-04 contract — Assessment Plan actually controls compile.

Per ``docs/j1_sequential_pr_implementation_plan.md``'s PR-04, J1
MUST guarantee:

  1. Assessment Plan's selected mode reaches the compile request.
  2. ``parse_method`` derived from the plan reaches the
     RAGAnything adapter (and is independent of the env-default
     when a plan is present).
  3. Env override (deployment allow-list) disables expensive
     behaviour even when the plan recommends it.
  4. Unsupported controls produce warnings (or raise under
     ``FallbackPolicy.FAIL``) rather than silently doing nothing.
  5. Compile does NOT silently fall back to settings defaults when
     a plan is present — plan > settings precedence is the
     load-bearing rule.

The mapper ``map_assessment_to_raganything_config`` is the
single canonical seam. The bridge's ``_resolve_compile_config``
calls it iff the request carries an ``assessment_plan``. Both
sides are exercised here. The activity → bridge wiring is
exercised transitively (the bridge function under test reads
``request.assessment_plan`` and ``request.settings``).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from j1.processing.assessment import (
    AssessmentPlan,
    Capability,
    CompileMode,
    Complexity,
    FallbackPolicy,
)
from j1.providers.raganything.plan_mapper import (
    CompileCapabilityUnsupported,
    map_assessment_to_raganything_config,
)
from j1.providers.raganything.settings import RAGAnythingSettings


def _settings(
    *,
    parse_method: str = "auto",
    backend: str = "vlm-http-client",
    allowed_parse_methods: tuple[str, ...] = (),
    supports_image: bool = True,
    supports_table: bool = True,
    supports_equation: bool = True,
) -> RAGAnythingSettings:
    return RAGAnythingSettings(
        workdir="/tmp/raganything-test",
        parse_method=parse_method,
        backend=backend,
        vlm_http_server_url="http://localhost:9000/v1",
        allowed_parse_methods=allowed_parse_methods,
        supports_image=supports_image,
        supports_table=supports_table,
        supports_equation=supports_equation,
    )


def _plan(
    *,
    mode: CompileMode = CompileMode.STANDARD,
    required: frozenset[Capability] = frozenset({Capability.TEXT_EXTRACTION}),
    optional: frozenset[Capability] = frozenset(),
    fallback: FallbackPolicy = FallbackPolicy.DEGRADE_WITH_WARNING,
) -> AssessmentPlan:
    return AssessmentPlan(
        document_id="doc-pr04",
        mode=mode,
        document_type="pdf",
        complexity=Complexity.MEDIUM,
        confidence=0.85,
        required_capabilities=required,
        optional_capabilities=optional,
        fallback_policy=fallback,
    )


# ---- Contract 1: mode reaches compile request -------------------


def test_contract_1_standard_mode_reaches_resolved_compile_config():
    config = map_assessment_to_raganything_config(
        _plan(mode=CompileMode.STANDARD), _settings(),
    )
    assert config.resolved_mode == "standard", (
        f"plan.mode=STANDARD must surface on the resolved config; "
        f"got resolved_mode={config.resolved_mode!r}"
    )


def test_contract_1_deep_mode_reaches_resolved_compile_config():
    config = map_assessment_to_raganything_config(
        _plan(
            mode=CompileMode.DEEP,
            required=frozenset({Capability.TEXT_EXTRACTION, Capability.OCR}),
        ),
        _settings(),
    )
    assert config.resolved_mode == "deep"


# ---- Contract 2: parse_method derived from plan ------------------


def test_contract_2_standard_plan_resolves_to_parse_method_auto():
    config = map_assessment_to_raganything_config(
        _plan(mode=CompileMode.STANDARD), _settings(),
    )
    assert config.parse_method == "auto"
    assert config.to_parser_kwargs() == {"parse_method": "auto"}


def test_contract_2_deep_plan_without_ocr_resolves_to_auto():
    config = map_assessment_to_raganything_config(
        _plan(mode=CompileMode.DEEP), _settings(),
    )
    # No OCR required → DEEP still resolves to "auto" (RAGAnything
    # picks OCR per-page when needed). Operators get DEEP capability
    # toggles without a hard-coded OCR pass.
    assert config.parse_method == "auto"


def test_contract_2_deep_plan_with_ocr_resolves_to_ocr():
    config = map_assessment_to_raganything_config(
        _plan(
            mode=CompileMode.DEEP,
            required=frozenset({Capability.TEXT_EXTRACTION, Capability.OCR}),
        ),
        _settings(),
    )
    assert config.parse_method == "ocr"


# ---- Contract 3: env override (allow-list) wins for safety ------


def test_contract_3_env_allow_list_constrains_plan_with_warning():
    """``J1_RAGANYTHING_ALLOWED_PARSE_METHODS=auto`` is the
    operator's safety hatch when the worker can't actually run
    OCR. A plan requesting OCR is degraded to "auto" with a
    warning — the operator's deployment-level constraint wins."""
    config = map_assessment_to_raganything_config(
        _plan(
            mode=CompileMode.DEEP,
            required=frozenset({Capability.TEXT_EXTRACTION, Capability.OCR}),
        ),
        _settings(allowed_parse_methods=("auto",)),
    )
    assert config.parse_method == "auto"
    # Warning surfaces the override so it's visible in the report.
    assert any("allow-list" in w for w in config.warnings), (
        f"env-constrained downgrade should produce a warning; "
        f"got warnings={config.warnings!r}"
    )


def test_contract_3_env_supports_flag_records_unhandled_capability():
    """Setting ``J1_RAGANYTHING_SUPPORTS_IMAGE=false`` marks image
    processing unsupported at the deployment layer. A plan that
    REQUIRES image extraction must surface a warning and stamp
    ``unhandled_capabilities`` with the capability name."""
    config = map_assessment_to_raganything_config(
        _plan(
            mode=CompileMode.STANDARD,
            required=frozenset({
                Capability.TEXT_EXTRACTION, Capability.IMAGE_EXTRACTION,
            }),
        ),
        _settings(supports_image=False),
    )
    assert "image_extraction" in config.unhandled_capabilities
    assert any(
        "image processing unsupported" in w for w in config.warnings
    ), f"image-disabled deployment should warn; got {config.warnings!r}"


# ---- Contract 4: FAIL policy raises instead of warning ----------


def test_contract_4_fail_policy_raises_when_required_capability_unsupported():
    """Operators that want hard guarantees set
    ``fallback_policy=FAIL`` on the plan. The mapper MUST raise
    ``CompileCapabilityUnsupported`` rather than silently degrade —
    a compliance run that MUST OCR every page would otherwise pass
    quietly with mangled output."""
    with pytest.raises(CompileCapabilityUnsupported):
        map_assessment_to_raganything_config(
            _plan(
                mode=CompileMode.STANDARD,
                required=frozenset({
                    Capability.TEXT_EXTRACTION, Capability.IMAGE_EXTRACTION,
                }),
                fallback=FallbackPolicy.FAIL,
            ),
            _settings(supports_image=False),
        )


def test_contract_4_degrade_policy_records_warning_without_raising():
    """Default policy degrades — operators see the warning on the
    compile report and can decide what to do, but the run still
    progresses."""
    # No raise; just warnings.
    config = map_assessment_to_raganything_config(
        _plan(
            mode=CompileMode.STANDARD,
            required=frozenset({
                Capability.TEXT_EXTRACTION, Capability.IMAGE_EXTRACTION,
            }),
            fallback=FallbackPolicy.DEGRADE_WITH_WARNING,
        ),
        _settings(supports_image=False),
    )
    assert config.warnings  # non-empty
    assert "image_extraction" in config.unhandled_capabilities


# ---- Contract 5: plan > settings precedence ---------------------


def test_contract_5_plan_overrides_settings_parse_method_default():
    """When ``settings.parse_method="ocr"`` (a deployment-level
    default) but the plan says STANDARD, the resolved
    ``parse_method`` MUST be "auto" (plan-derived) — NOT "ocr"
    (settings default). Plan > settings.

    The plan is the per-document decision; the settings default
    only applies when no plan is present at all (legacy callers /
    bulk-job mode)."""
    config = map_assessment_to_raganything_config(
        _plan(mode=CompileMode.STANDARD),
        _settings(parse_method="ocr"),  # deployment default
    )
    assert config.parse_method == "auto", (
        "plan-derived parse_method must override settings default; "
        f"got {config.parse_method!r}"
    )


def test_contract_5_plan_overrides_settings_when_settings_default_is_txt():
    """Same precedence rule the other direction: settings says
    "txt" (a hypothetical operator misconfiguration); plan says
    DEEP+OCR. Plan wins → "ocr"."""
    config = map_assessment_to_raganything_config(
        _plan(
            mode=CompileMode.DEEP,
            required=frozenset({Capability.TEXT_EXTRACTION, Capability.OCR}),
        ),
        _settings(parse_method="txt", allowed_parse_methods=()),
    )
    assert config.parse_method == "ocr"


# ---- Activity → bridge wiring (integration seam) ----------------


def test_bridge_resolve_compile_config_calls_mapper_when_plan_present():
    """``_resolve_compile_config`` is the single bridge-layer seam
    that links the workflow's AssessmentPlan to the mapper. When a
    request carries a plan, the bridge MUST consult the mapper —
    not silently bypass it."""
    from j1.providers.raganything._bridge import _resolve_compile_config

    @dataclass
    class _Request:
        assessment_plan: AssessmentPlan | None
        settings: RAGAnythingSettings

    request = _Request(
        assessment_plan=_plan(mode=CompileMode.DEEP, required=frozenset({
            Capability.TEXT_EXTRACTION, Capability.OCR,
        })),
        settings=_settings(),
    )
    config = _resolve_compile_config(request)
    assert config is not None
    assert config.resolved_mode == "deep"
    assert config.parse_method == "ocr"


def test_bridge_resolve_compile_config_returns_none_when_no_plan():
    """Legacy callers (bulk-job mode) build a request without a
    plan. The bridge MUST return None so the call site falls back
    to ``settings.parse_method``. Pinned so a future refactor that
    requires a plan everywhere doesn't silently break legacy."""
    from j1.providers.raganything._bridge import _resolve_compile_config

    @dataclass
    class _Request:
        assessment_plan: AssessmentPlan | None
        settings: RAGAnythingSettings

    request = _Request(
        assessment_plan=None, settings=_settings(),
    )
    assert _resolve_compile_config(request) is None


def test_compile_request_dataclass_carries_assessment_plan_field():
    """The ``RAGAnythingCompileRequest`` dataclass MUST expose
    ``assessment_plan`` — that's the seam the workflow → service
    chain consumes. A future refactor that drops it would break
    the whole PR-04 wiring."""
    from j1.providers.raganything.compiler import RAGAnythingCompileRequest
    fields = RAGAnythingCompileRequest.__dataclass_fields__
    assert "assessment_plan" in fields, (
        "RAGAnythingCompileRequest must expose `assessment_plan` — "
        "PR-04's wiring requires it"
    )
