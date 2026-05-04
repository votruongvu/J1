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
    """

    ctx: ProjectContext
    document_id: str
    settings: RAGAnythingSettings
    text_client: Any
    vision_client: Any | None
    embedding_client: Any | None


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
        self, ctx: ProjectContext, document_id: str,
    ) -> ArtifactProcessingResult:
        request = RAGAnythingCompileRequest(
            ctx=ctx,
            document_id=document_id,
            settings=self._settings,
            text_client=self._llm_registry.text(),
            vision_client=self._llm_registry.try_vision(),
            embedding_client=self._llm_registry.try_embedding(),
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
    """Return a callable that delegates to the `raganything` library.

    Lazy-imports the vendor library on the first call. If the import
    fails, raises `ProviderUnavailable` with the install hint — the
    framework's own test suite never triggers this path.
    """

    def _delegate(request: RAGAnythingCompileRequest) -> ArtifactProcessingResult:
        try:
            import raganything  # noqa: F401
        except ImportError as exc:
            raise ProviderUnavailable(
                "RAGAnything compiler requires the `raganything` package. "
                "Install with: pip install raganything"
            ) from exc

        # The integration glue between J1 canonical inputs and
        # RAGAnything's call surface lives here. Today this is a
        # documented stub — the framework provides the seam, the
        # deployment fills in the vendor-specific orchestration.
        # See docs/architecture.md § "RAGAnything integration" and
        # docs/troubleshooting.md.
        raise ProviderUnavailable(
            "RAGAnything compile() is not yet wired in this build. Provide "
            "a custom `compile_callable` to RAGAnythingCompiler(...) until "
            "the default integration ships, or override the adapter."
        )

    return _delegate
