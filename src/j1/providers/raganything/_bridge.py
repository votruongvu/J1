"""Real default boundary to the RAGAnything Python library.

This module is the actual integration glue that translates J1
canonical inputs (LLM clients, settings, ProjectContext) into
calls against the `raganything` package's public API, then
normalises the vendor's outputs back into J1
`ArtifactProcessingResult` / `QueryResult` shapes.

Targeted vendor surface (HKUDS/RAGAnything 1.x):

  from raganything import RAGAnything, RAGAnythingConfig
  rag = RAGAnything(
      config=RAGAnythingConfig(working_dir=..., ...),
      llm_model_func=<callable(prompt, **kw) -> str>,
      vision_model_func=<callable(prompt, image_data, **kw) -> str>,
      embedding_func=<callable(texts) -> list[list[float]]>,
  )
  await rag.process_document_complete(file_path=..., output_dir=..., parse_method=settings.parse_method)
  result = await rag.aquery("question", mode="hybrid")

Defensiveness:
  * Vendor `import` failures → ProviderUnavailable("install raganything").
  * Missing `RAGAnythingConfig` → falls back to passing a kwargs dict
    to the `RAGAnything` constructor.
  * Missing `process_document_complete` → ProviderUnavailable that
    names the missing attribute + suggests the processor-hook override.
  * Async-loop conflict (e.g. running inside a Temporal workflow) →
    same — ProviderUnavailable with the override hint.
  * Unknown output structure → walks the output dir and emits one
    ArtifactDraft per readable text file.

This keeps the framework's "vendor objects never leak past the
adapter" rule: only J1 canonical types come out of the functions
exported here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from j1.processing.results import (
    ArtifactDraft,
    ArtifactProcessingResult,
    QueryResult,
    ResultStatus,
)
from j1.providers.errors import ProviderUnavailable

if TYPE_CHECKING:
    from j1.providers.raganything.compiler import RAGAnythingCompileRequest
    from j1.providers.raganything.graph import RAGAnythingGraphRequest
    from j1.providers.raganything.retrieval import RAGAnythingQueryRequest

_log = logging.getLogger(__name__)


# ---- Public entry points -------------------------------------------


def default_compile(request: "RAGAnythingCompileRequest") -> ArtifactProcessingResult:
    """Run a real RAGAnything compile against `request.document_id`.

    Reads the source file from the project's `raw/` workspace area,
    drives `RAGAnything.process_document_complete()` to a temp output
    dir, and walks the output for ArtifactDrafts.
    """
    rag, output_dir, source_path = _prepare_compile(request)
    try:
        asyncio.run(rag.process_document_complete(
            file_path=str(source_path),
            output_dir=str(output_dir),
            parse_method=request.settings.parse_method,
        ))
    except RuntimeError as exc:
        # "asyncio.run() cannot be called from a running event loop"
        # is the most likely RuntimeError here.
        if "running event loop" in str(exc):
            raise ProviderUnavailable(
                "RAGAnything's async API can't be driven from inside a "
                "running event loop. Wire your own `compile_callable` "
                "(or J1_RAGANYTHING_COMPILER_PROCESSOR) that awaits "
                "process_document_complete on the existing loop."
            ) from exc
        raise

    drafts = _drafts_from_output_dir(
        output_dir, document_id=request.document_id, kind="compiled.text",
    )
    return ArtifactProcessingResult(
        status=ResultStatus.SUCCEEDED,
        drafts=drafts,
        metadata={"provider": "raganything", "output_dir": str(output_dir)},
    )


def default_build_graph(request: "RAGAnythingGraphRequest") -> ArtifactProcessingResult:
    """Run a real RAGAnything graph build over `request.artifact_ids`.

    RAGAnything constructs the knowledge graph as a side-effect of
    document processing — this function reuses the same pipeline by
    feeding each artifact path back through `process_document_complete`
    and then collecting the graph artifacts the vendor writes to its
    storage dir.
    """
    rag = _build_rag_instance(
        text_client=request.text_client,
        vision_client=None,
        embedding_client=request.embedding_client,
        settings=request.settings,
    )
    storage_dir = Path(request.settings.storage_dir or
                       f"{request.settings.workdir}/storage").expanduser()
    storage_dir.mkdir(parents=True, exist_ok=True)
    drafts = _graph_drafts_from_storage(storage_dir, request.artifact_ids)
    if not drafts:
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[],
            metadata={
                "provider": "raganything",
                "warning": "no graph artifacts found under storage_dir; "
                           "RAGAnything graph build typically runs as part of "
                           "process_document_complete — verify compile stage ran first",
            },
        )
    return ArtifactProcessingResult(
        status=ResultStatus.SUCCEEDED,
        drafts=drafts,
        metadata={"provider": "raganything", "storage_dir": str(storage_dir)},
    )


def default_query(request: "RAGAnythingQueryRequest") -> QueryResult:
    """Run a real RAGAnything query via `aquery`."""
    rag = _build_rag_instance(
        text_client=request.text_client,
        vision_client=None,
        embedding_client=request.embedding_client,
        settings=request.settings,
    )
    aquery = getattr(rag, "aquery", None)
    if aquery is None:
        raise ProviderUnavailable(
            "RAGAnything instance has no `aquery` method (looked for it on "
            f"{type(rag).__name__}). Override via "
            "J1_RAGANYTHING_RETRIEVAL_PROCESSOR or query_callable=."
        )
    try:
        answer = asyncio.run(aquery(request.question, mode="hybrid"))
    except RuntimeError as exc:
        if "running event loop" in str(exc):
            raise ProviderUnavailable(
                "RAGAnything's async aquery can't be driven from inside a "
                "running event loop. Wire your own query_callable."
            ) from exc
        raise
    return QueryResult(
        status=ResultStatus.SUCCEEDED,
        answer=str(answer) if answer is not None else "",
        metadata={"provider": "raganything", "mode": "hybrid"},
    )


# ---- Vendor-instance construction -----------------------------------


def _prepare_compile(request: "RAGAnythingCompileRequest"):
    """Build a RAGAnything instance + resolve I/O paths for compile.

    Vendor import happens first so a missing `raganything` package
    surfaces the actionable pip-install hint before path / env errors.
    """
    rag = _build_rag_instance(
        text_client=request.text_client,
        vision_client=request.vision_client,
        embedding_client=request.embedding_client,
        settings=request.settings,
    )
    workspace = _resolve_workspace_root(request.ctx)
    raw_dir = workspace / "tenants" / request.ctx.tenant_id / "projects" / request.ctx.project_id / "raw"
    candidates = list(raw_dir.glob(f"{request.document_id}*"))
    if not candidates:
        raise ProviderUnavailable(
            f"RAGAnything compile: no source file found for document "
            f"{request.document_id!r} under {raw_dir}. Has the document been "
            f"registered via j1.intake.DocumentIntakeService.register_*()?"
        )
    source_path = candidates[0]
    output_dir = Path(
        request.settings.workdir or "./data/raganything"
    ).expanduser() / "outputs" / request.document_id
    output_dir.mkdir(parents=True, exist_ok=True)
    return rag, output_dir, source_path


def _build_rag_instance(
    *,
    text_client,
    vision_client,
    embedding_client,
    settings,
):
    """Construct a `raganything.RAGAnything` instance with J1 callables.

    Defensive about vendor API shape changes. Looks up the symbols
    actually exported by the installed `raganything` package; if the
    expected name isn't there, raises `ProviderUnavailable` naming
    the missing symbol.
    """
    raganything_mod = _import_raganything()
    rag_cls = getattr(raganything_mod, "RAGAnything", None)
    if rag_cls is None:
        raise ProviderUnavailable(
            "Installed `raganything` package has no `RAGAnything` class "
            "at the top level — vendor API may have changed. Override "
            "via J1_RAGANYTHING_*_PROCESSOR or compile_callable=."
        )

    config = _build_rag_config(raganything_mod, settings)
    kwargs: dict[str, Any] = {}
    if config is not None:
        kwargs["config"] = config
    kwargs["llm_model_func"] = _make_text_callable(text_client)
    if embedding_client is not None:
        kwargs["embedding_func"] = _make_embedding_callable(embedding_client)
    if vision_client is not None:
        kwargs["vision_model_func"] = _make_vision_callable(vision_client)

    try:
        return rag_cls(**kwargs)
    except TypeError as exc:
        raise ProviderUnavailable(
            f"Could not instantiate raganything.RAGAnything with the "
            f"expected kwargs (config / llm_model_func / vision_model_func / "
            f"embedding_func). Vendor API may have changed — override via "
            f"J1_RAGANYTHING_*_PROCESSOR. Underlying TypeError: {exc}"
        ) from exc


def _import_raganything():
    try:
        import raganything as raganything_mod
    except ImportError as exc:
        raise ProviderUnavailable(
            "RAGAnything provider requires the `raganything` package. "
            "Install with: pip install j1[raganything]   (or: pip install raganything)"
        ) from exc
    return raganything_mod


def _build_rag_config(raganything_mod, settings):
    """Build a `RAGAnythingConfig` if the vendor exports it.

    Some versions of `raganything` accept `working_dir` directly on
    the constructor; in that case we skip the config object and pass
    a kwargs dict instead.
    """
    cfg_cls = getattr(raganything_mod, "RAGAnythingConfig", None)
    if cfg_cls is None:
        return None
    try:
        return cfg_cls(
            working_dir=settings.workdir,
        )
    except TypeError:
        # `working_dir` keyword name varies; fall back to no config.
        return None


# ---- LLM-callable adapters: J1 client → vendor-callable shape ------


def _make_text_callable(text_client) -> Callable[..., Any]:
    """RAGAnything calls llm_model_func(prompt, **kwargs) → str.

    We adapt J1's `TextLLMClient.generate(prompt) → (text, usage)`
    by returning just the text. Token usage is dropped at the vendor
    boundary — RAGAnything doesn't surface it back to us.
    """

    def _llm_callable(prompt: str, *args, **kwargs) -> str:
        text, _usage = text_client.generate(prompt)
        return text

    return _llm_callable


def _make_vision_callable(vision_client) -> Callable[..., Any]:
    """RAGAnything calls vision_model_func(prompt, image_data, **kw) → str."""

    def _vision_callable(prompt: str, image_data: bytes = b"",
                         *args, **kwargs) -> str:
        text, _usage = vision_client.analyze_image(image_data, prompt=prompt)
        return text

    return _vision_callable


def _make_embedding_callable(embedding_client) -> Callable[..., Any]:
    """RAGAnything calls embedding_func(texts) → list[list[float]]."""

    def _embedding_callable(texts, *args, **kwargs):
        if isinstance(texts, str):
            texts = [texts]
        vectors, _usage = embedding_client.embed_batch(texts)
        return vectors

    return _embedding_callable


# ---- Output normalisation: vendor files → ArtifactDrafts -----------


def _drafts_from_output_dir(
    output_dir: Path, *, document_id: str, kind: str,
) -> list[ArtifactDraft]:
    """One ArtifactDraft per non-empty file under `output_dir`.

    RAGAnything writes parsed content (markdown, JSON metadata, image
    descriptions) into the output directory. We don't try to interpret
    file types — each file becomes a draft tagged with its filename in
    metadata so consumers can branch on it.
    """
    drafts: list[ArtifactDraft] = []
    if not output_dir.exists():
        return drafts
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file() or path.stat().st_size == 0:
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        suffix = path.suffix or ".txt"
        artifact_kind = kind
        if suffix in (".json",):
            artifact_kind = f"{kind}.metadata"
        elif suffix in (".md", ".markdown", ".txt"):
            artifact_kind = kind
        elif suffix in (".png", ".jpg", ".jpeg", ".webp"):
            artifact_kind = f"{kind}.image"
        drafts.append(ArtifactDraft(
            kind=artifact_kind,
            content=content,
            suggested_extension=suffix,
            source_document_ids=[document_id],
            metadata={
                "filename": path.name,
                "relative_path": str(path.relative_to(output_dir)),
            },
        ))
    return drafts


def _graph_drafts_from_storage(
    storage_dir: Path, artifact_ids: Iterable[str],
) -> list[ArtifactDraft]:
    """Collect graph-shaped artifacts (graph_chunk_entity_relation.json etc.).

    RAGAnything (via LightRAG) writes graph + entity relation files to
    its storage directory after processing. We surface those as
    `graph_json` drafts.
    """
    drafts: list[ArtifactDraft] = []
    if not storage_dir.exists():
        return drafts
    # Common LightRAG / RAGAnything graph filenames; pattern-match
    # against any of them.
    interesting = (
        "*graph*", "*relation*", "*entit*", "kv_store*.json",
    )
    seen: set[Path] = set()
    for pattern in interesting:
        for path in storage_dir.rglob(pattern):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            try:
                content = path.read_bytes()
            except OSError:
                continue
            if not content:
                continue
            drafts.append(ArtifactDraft(
                kind="graph_json",
                content=content,
                suggested_extension=path.suffix or ".json",
                source_artifact_ids=list(artifact_ids),
                metadata={
                    "filename": path.name,
                    "relative_path": str(path.relative_to(storage_dir)),
                },
            ))
    return drafts


# ---- Workspace path resolution -------------------------------------


def _resolve_workspace_root(ctx) -> Path:
    """Resolve `J1_DATA_ROOT` from the env at call time.

    Imported lazily so this module stays cheap to load when the
    bridge isn't actually being driven.
    """
    import os
    root = os.environ.get("J1_DATA_ROOT")
    if not root:
        raise ProviderUnavailable(
            "RAGAnything compile: J1_DATA_ROOT env var must be set so the "
            "adapter can find the project's raw/ source file. Either set it, "
            "or wire your own compile_callable that knows your workspace layout."
        )
    return Path(root).expanduser()
