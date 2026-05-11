"""RAGAnything-backed `KnowledgeCompiler`.

Wraps the external `raganything` library so it satisfies the
framework's existing Protocol — the rest of J1 (ProcessingService,
ProjectProcessingWorkflow, etc.) keeps working unchanged.

Two construction paths:

 * `RAGAnythingCompiler.from_default(llm_registry, settings)` —
 lazy-imports `raganything`, raises `ProviderUnavailable` with
 pip-install hint if missing.
 * `RAGAnythingCompiler(llm_registry, settings, compile_callable=...)`
 — tests inject a fake callable directly. No vendor library
 needed.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from j1.llm.registry import (
    LLM_ROLE_EMBEDDING,
    LLM_ROLE_TEXT,
    LLM_ROLE_VISION,
    LLMProviderRegistry,
)
from j1.processing.results import (
    ArtifactDraft,
    ArtifactProcessingResult,
    ResultStatus,
)
from j1.projects.context import ProjectContext
from j1.providers.errors import ProviderUnavailable
from j1.providers.raganything.settings import RAGAnythingSettings

PROVIDER_NAME = "raganything"

# Adapter-side schema version. Bump when the J1 bridge changes the
# shape of artifacts it produces (e.g. new ARTIFACT_KIND added to
# the compile output, chunk-metadata change). Independent from the
# vendor `raganything` package version, which we capture separately
# at runtime when available.
_ADAPTER_SCHEMA_VERSION = "2"


@dataclass(frozen=True)
class RAGAnythingCompileRequest:
    """What the compile callable receives.

 Fully canonical — no provider-native types in or out. The callable
 returns an `ArtifactProcessingResult` whose drafts the framework
 materialises into the project workspace.

 `progress_reporter` and `run_id` are optional plumbing for the
 user-facing progress surface: when both are present the bridge
 attaches the MinerU log handler so vendor progress lines turn
 into structured `step.progress` events. When absent (default for
 callers that don't use the runs surface) the bridge runs
 unchanged.

 `assessment_plan` is the per-document compile plan
 ([j1.processing.assessment](../../processing/assessment.py)). When
 set, the bridge derives `parse_method` (and any future per-
 capability toggles) via
 [`map_assessment_to_raganything_config`](./plan_mapper.py)
 instead of using `settings.parse_method`. When None (legacy
 callers + tests), the bridge falls back to the static settings
 field — preserves backward compatibility.
 """

    ctx: ProjectContext
    document_id: str
    settings: RAGAnythingSettings
    text_client: Any
    vision_client: Any | None
    embedding_client: Any | None
    progress_reporter: Any = None  # ProgressReporter | None — Any to keep this dataclass importable without j1.runs.
    run_id: str | None = None
    # Vendor-neutral plan. `Any` typing keeps this dataclass importable
    # without forcing every consumer to import `j1.processing.assessment`.
    assessment_plan: Any = None


CompileCallable = Callable[[RAGAnythingCompileRequest], ArtifactProcessingResult]


class RAGAnythingCompiler:
    kind: str = PROVIDER_NAME

    def __init__(
        self,
        *,
        llm_registry: LLMProviderRegistry,
        settings: RAGAnythingSettings,
        compile_callable: CompileCallable,
    ) -> None:
        self._llm_registry = llm_registry
        self._settings = settings
        self._compile_callable = compile_callable

    @property
    def version(self) -> str:
        """Cache-partitioning version string.

 Composed of the adapter-side schema version plus a stable
 hash of the settings fields that change parser output:
 `parse_method`, `backend`, and the VLM-HTTP-client wiring.
 The vendor `raganything` package version is captured at
 runtime when importable; otherwise omitted (the adapter
 schema version is sufficient to invalidate cache on J1-side
 upgrades).

 Two compiles with the same `(document_hash, processor_kind,
 version, mode)` are guaranteed to produce equivalent
 artifacts. Changing any of: `parse_method`, `backend`,
 `vlm_http_*` settings, or the `raganything` package version
 — forces a fresh parse instead of reusing stale rows.
 """
        return _build_compiler_version(self._settings)

    @property
    def mode(self) -> str:
        """Parser mode (e.g. `auto`, `vlm-http-client`). The cache
 key includes this so a `parse_method` change forces a fresh
 parse instead of reusing artifacts produced by a different
 backend."""
        return str(getattr(self._settings, "parse_method", "") or "")

    @classmethod
    def from_default(
        cls,
        *,
        llm_registry: LLMProviderRegistry,
        settings: RAGAnythingSettings,
    ) -> "RAGAnythingCompiler":
        """Construct the production adapter.

 If `settings.compiler_processor` is set (e.g. via
 `J1_RAGANYTHING_COMPILER_PROCESSOR=mypkg.processors:compile_doc`),
 the named callable is loaded via the safe class-loader. Otherwise
 falls back to a built-in stub that raises `ProviderUnavailable`
 with a clear "wire your own processor" message.
 """
        compile_callable: CompileCallable
        if settings.compiler_processor:
            from j1.llm.classloader import resolve_callable
            compile_callable = resolve_callable(settings.compiler_processor)
        else:
            compile_callable = _build_default_compile_callable()
        return cls(
            llm_registry=llm_registry,
            settings=settings,
            compile_callable=compile_callable,
        )

    def compile(
        self,
        ctx: ProjectContext,
        document_id: str,
        *,
        progress_reporter: Any = None,
        run_id: str | None = None,
        assessment_plan: Any = None,
    ) -> ArtifactProcessingResult:
        """Run the wrapped vendor compile.

 `progress_reporter` and `run_id` are optional. When both are
 supplied, the bridge attaches the MinerU log handler so
 vendor progress lines become structured `step.progress`
 events. The `KnowledgeCompiler` Protocol's compile method
 only requires `(ctx, document_id)` — these kwargs are
 additive and existing callers stay working.

 `assessment_plan` is the per-document compile plan
 ([j1.processing.assessment.AssessmentPlan](../../processing/assessment.py)).
 When set, the bridge derives `parse_method` (and any future
 per-capability toggles) from the plan via the mapper. None
 falls back to `settings.parse_method` — preserves backward
 compatibility for callers that don't yet build a plan."""
        request = RAGAnythingCompileRequest(
            ctx=ctx,
            document_id=document_id,
            settings=self._settings,
            text_client=self._llm_registry.text(),
            vision_client=self._llm_registry.try_vision(),
            embedding_client=self._llm_registry.try_embedding(),
            progress_reporter=progress_reporter,
            run_id=run_id,
            assessment_plan=assessment_plan,
        )
        try:
            return self._compile_callable(request)
        except ProviderUnavailable:
            raise
        except Exception as exc:
            return ArtifactProcessingResult(
                status=ResultStatus.FAILED,
                error=str(exc),
                message=type(exc).__name__,
                drafts=[],
                metadata={"provider": PROVIDER_NAME},
            )

def _vendor_version() -> str:
    """Return the installed `raganything` package version, or empty
 string when the package isn't importable at this moment.

 Read once via `importlib.metadata` — no I/O on the import path.
 Failures are silent: the cache key falls back to the adapter
 schema version alone, which is the same partition behaviour as
 before the version field existed.
 """
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("raganything")
        except PackageNotFoundError:
            return ""
    except Exception:  # noqa: BLE001 — version sniff must never crash compile
        return ""


def _build_compiler_version(settings: RAGAnythingSettings) -> str:
    """Compose the cache-partitioning version string.

 Format: `{adapter_schema}|{vendor_version}|{settings_hash}` —
 short, stable, deterministic across runs. The settings hash
 intentionally excludes paths (workdir/storage_dir/cache_dir) and
 LibreOffice plumbing — those don't change parser output.
 """
    parts = "|".join((
        getattr(settings, "parse_method", "") or "",
        getattr(settings, "backend", "") or "",
        getattr(settings, "vlm_http_server_url", "") or "",
        getattr(settings, "vlm_http_model_name", "") or "",
    ))
    settings_hash = hashlib.sha256(parts.encode("utf-8")).hexdigest()[:8]
    return f"{_ADAPTER_SCHEMA_VERSION}|{_vendor_version()}|{settings_hash}"


def _build_default_compile_callable() -> CompileCallable:
    """Real default boundary to the `raganything` library.

 Delegates to `j1.providers.raganything._bridge.default_compile`,
 which lazy-imports the vendor package + drives its public
 `RAGAnything.process_document_complete` API + normalises the
 output directory into J1 `ArtifactDraft`s.

 Raises `ProviderUnavailable` only when:
 * the `raganything` package isn't installed (actionable: pip install)
 * the vendor API shape doesn't match (actionable: names the
 missing symbol + points at the J1_RAGANYTHING_*_PROCESSOR
 override seam)
 * the document's source file isn't found in the workspace
 * a runtime constraint prevents the call (e.g. running event
 loop conflict)
 """

    def _delegate(request: RAGAnythingCompileRequest) -> ArtifactProcessingResult:
        from j1.providers.raganything._bridge import default_compile
        return default_compile(request)

    return _delegate
