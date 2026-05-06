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
    """

    ctx: ProjectContext
    document_id: str
    settings: RAGAnythingSettings
    text_client: Any
    vision_client: Any | None
    embedding_client: Any | None
    progress_reporter: Any = None  # ProgressReporter | None — Any to keep this dataclass importable without j1.runs.
    run_id: str | None = None


CompileCallable = Callable[[RAGAnythingCompileRequest], ArtifactProcessingResult]


class RAGAnythingCompiler:
    kind: str = PROVIDER_NAME
    # Bump when the produced artifact shape changes — e.g. a new
    # raganything release or a content-format migration. The
    # processing-result cache uses this to partition cache rows so
    # cross-version artifacts don't get reused. `version` is part of
    # the `KnowledgeCompiler` informal interface; activities read it
    # via `getattr(compiler, "version", "")`.
    version: str = "1"

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
    ) -> ArtifactProcessingResult:
        """Run the wrapped vendor compile.

        `progress_reporter` and `run_id` are optional. When both are
        supplied, the bridge attaches the MinerU log handler so
        vendor progress lines become structured `step.progress`
        events. The `KnowledgeCompiler` Protocol's compile method
        only requires `(ctx, document_id)` — these kwargs are
        additive and existing callers stay working."""
        request = RAGAnythingCompileRequest(
            ctx=ctx,
            document_id=document_id,
            settings=self._settings,
            text_client=self._llm_registry.text(),
            vision_client=self._llm_registry.try_vision(),
            embedding_client=self._llm_registry.try_embedding(),
            progress_reporter=progress_reporter,
            run_id=run_id,
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


def _build_default_compile_callable() -> CompileCallable:
    """Real default boundary to the `raganything` library.

    Delegates to `j1.providers.raganything._bridge.default_compile`,
    which lazy-imports the vendor package + drives its public
    `RAGAnything.process_document_complete()` API + normalises the
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
