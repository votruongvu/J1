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
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
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
    from j1.providers.raganything.settings import RAGAnythingSettings

_log = logging.getLogger(__name__)


# Plain-text extensions where MinerU's parse path is actively harmful.
# `MineruParser.parse_text_file` (raganything ≤ current) renders the text
# to a PDF via reportlab and then runs the full PyTorch model stack
# (layout / OCR / formula / table) on the rendered PDF — pinning every
# CPU core for tens of seconds even on a 10-character file. For these
# extensions the bridge reads the bytes directly and feeds them to
# LightRAG, skipping mineru entirely.
_NATIVE_TEXT_EXTENSIONS = frozenset({".txt", ".md", ".markdown"})


class _LibreOfficeConversionError(RuntimeError):
    """Raised when soffice subprocess fails for a known runtime reason
    (non-zero exit, no output produced, timeout). Caught at the
    `default_compile` boundary and surfaced as a FAILED result.
    Distinct from `ProviderUnavailable`, which is reserved for the
    "binary not installed" infrastructure case."""


def _apply_vlm_http_client_env(settings: "RAGAnythingSettings") -> None:
    """When `backend=vlm-http-client`, propagate J1's vision-LLM
    config into the env vars MinerU's `mineru_vl_utils.MinerUClient`
    reads at runtime: `MINERU_VL_SERVER`, `MINERU_VL_API_KEY`,
    `MINERU_VL_MODEL_NAME`.

    The CLI accepts `-u/--vlm-url` for the server URL (we pass that
    as a kwarg to `process_document_complete`), but the API key and
    model-name fields have no CLI flag — mineru-vl-utils reads them
    directly from the environment. Without this propagation the
    request reaches LM Studio with no Authorization header and an
    auto-detected model name (which on multi-model servers picks the
    wrong one).

    With it, the existing `J1_VISION_LLM_*` config (already wired for
    the rest of the stack) is the only thing the operator needs in
    place — flipping `J1_RAGANYTHING_BACKEND=vlm-http-client` is the
    sole additional change.

    Idempotent — only sets each env var when (a) the backend is
    `vlm-http-client`, (b) we have a value, and (c) the operator
    hasn't already exported the var directly. Operator-supplied
    `MINERU_VL_*` always wins so existing deployments keep their
    tuning.

    No-op when backend is anything else (default `None` lets MinerU
    pick its own engine and never reads these vars)."""
    if settings.backend != "vlm-http-client":
        return
    mapping = {
        "MINERU_VL_SERVER": settings.vlm_http_server_url,
        "MINERU_VL_API_KEY": settings.vlm_http_api_key,
        "MINERU_VL_MODEL_NAME": settings.vlm_http_model_name,
    }
    for name, value in mapping.items():
        if not value:
            continue
        if os.environ.get(name):
            # Operator already exported this — don't shadow it.
            continue
        os.environ[name] = value


# ---- Public entry points -------------------------------------------


def default_compile(request: "RAGAnythingCompileRequest") -> ArtifactProcessingResult:
    """Run a real RAGAnything compile against `request.document_id`.

    Reads the source file from the project's `raw/` workspace area,
    optionally pre-converts legacy / non-OOXML formats to PDF via
    LibreOffice (so raganything's PDF parser can pick up where
    mineru's pure-Python format readers can't), then drives
    `RAGAnything.process_document_complete()` to a temp output dir
    and walks the output for ArtifactDrafts.
    """
    # Bridge the J1 vision LLM config into MinerU's expected env
    # vars when the operator has selected the HTTP-client backend.
    # No-op for the default `auto` parse method.
    _apply_vlm_http_client_env(request.settings)
    rag, output_dir, source_path = _prepare_compile(request)

    # Pre-conversion: legacy/non-OOXML → PDF via LibreOffice headless.
    # The conversion is no-op (passthrough) for formats raganything
    # parses natively. Errors during conversion surface as a FAILED
    # result rather than a raise, so the workflow's retry / review
    # paths handle them uniformly with other compile failures.
    converted_path: Path | None = None
    try:
        if _needs_pdf_conversion(source_path, request.settings):
            converted_path = _convert_to_pdf(source_path, request.settings)
            source_path = converted_path
    except ProviderUnavailable:
        raise
    except _LibreOfficeConversionError as exc:
        return ArtifactProcessingResult(
            status=ResultStatus.FAILED,
            error=str(exc),
            message="LibreOffice conversion failed",
            metadata={
                "provider": "raganything",
                "stage": "preconvert",
                "source_extension": source_path.suffix,
            },
        )

    async def _run_compile():
        # `_ensure_lightrag_initialized` returns {"success": False, "error": ...}
        # on failure WITHOUT raising — and `process_document_complete`
        # then proceeds with `self.lightrag = None`, which blows up
        # later as `'NoneType' object has no attribute 'ainsert'`.
        # Surface the real init error here instead of letting the
        # downstream AttributeError mask it.
        init = await rag._ensure_lightrag_initialized()
        if isinstance(init, dict) and not init.get("success", True):
            raise ProviderUnavailable(
                "RAGAnything failed to initialize LightRAG: "
                f"{init.get('error', 'unknown error')}"
            )

        if _is_plain_text(source_path):
            await _insert_plain_text_directly(
                rag=rag,
                source_path=source_path,
                document_id=request.document_id,
                output_dir=output_dir,
            )
            return

        # Fast path for text-layer PDFs: skip MinerU entirely when the
        # document has embedded text on most pages.  MinerU's auto mode
        # runs full layout analysis (layout blocks, OCR, formula /
        # table detection) on every page regardless of content — for a
        # normal text PDF this adds 3–5 minutes of transformer
        # inference with no benefit.  pypdf text extraction for the
        # same file completes in < 1 second.
        #
        # We only activate the fast path when:
        #   * the source is a .pdf (not already-converted other format)
        #   * at least _PDF_TEXT_THRESHOLD fraction of sampled pages
        #     have meaningful embedded text  (≥ 20 non-whitespace chars)
        #
        # Complex documents (scanned, image-heavy, table-heavy,
        # equation-heavy) have low text extraction ratios and fall
        # through to the full MinerU pipeline unchanged.
        if _is_text_extractable_pdf(source_path):
            _log.info(
                "fast-text path: PDF has extractable text layer, "
                "skipping MinerU for document %r (%s)",
                request.document_id,
                source_path.name,
            )
            await _insert_pdf_text_directly(
                rag=rag,
                source_path=source_path,
                document_id=request.document_id,
                output_dir=output_dir,
            )
            return

        # Build backend + vlm_url kwargs from settings. mineru's CLI
        # validates `--method` (parse_method) against {auto, txt, ocr}
        # and `--backend` against {pipeline, vlm-http-client, …}; we
        # validated both at settings-load time so this assembly is
        # straightforward.
        mineru_kwargs: dict[str, Any] = {}
        if request.settings.backend:
            mineru_kwargs["backend"] = request.settings.backend
        if request.settings.backend == "vlm-http-client" and request.settings.vlm_http_server_url:
            # `-u/--vlm-url` is the CLI flag for the VLM server URL.
            # raganything's parser forwards this kwarg verbatim.
            mineru_kwargs["vlm_url"] = request.settings.vlm_http_server_url

        _log.info(
            "full-parse path: routing document %r to MinerU "
            "(parse_method=%s, backend=%s, file=%s)",
            request.document_id,
            request.settings.parse_method,
            request.settings.backend or "<mineru-default>",
            source_path.name,
        )
        await rag.process_document_complete(
            file_path=str(source_path),
            output_dir=str(output_dir),
            parse_method=request.settings.parse_method,
            **mineru_kwargs,
        )

    # When the caller supplied a `progress_reporter` + `run_id`,
    # attach the MinerU log-handler so vendor progress lines
    # (`[MinerU] Layout Preparation: 80% | 35/44`, etc.) become
    # structured `step.progress` events for the user-facing UI.
    # The handler is removed on context exit, so the rest of the
    # process's logging stays clean. When either field is absent
    # — the typical path for callers that haven't opted into the
    # runs surface — `attach_mineru_progress_handler` is a no-op.
    from j1.providers.raganything._log_bridge import attach_mineru_progress_handler

    try:
        with attach_mineru_progress_handler(
            request.progress_reporter, request.ctx, request.run_id or "",
        ):
            asyncio.run(_run_compile())
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
    finally:
        # Best-effort cleanup of the converted intermediate; raganything
        # has already consumed it by this point. Failure to clean up
        # is non-fatal — the temp directory will get reaped eventually.
        if converted_path is not None:
            try:
                converted_path.unlink(missing_ok=True)
                # Also try to remove the parent (created via mkdtemp)
                converted_path.parent.rmdir()
            except OSError:
                pass

    drafts = _drafts_from_output_dir(
        output_dir, document_id=request.document_id, kind="compiled.text",
    )
    # LightRAG persists per-chunk text into
    # `kv_store_text_chunks.json` inside its storage dir during
    # `process_document_complete`. Surface those entries as canonical
    # `kind="chunk"` artifacts so the Results > Chunks review tab has
    # real text to render. Without this, the FE's Chunks tab stays
    # disabled for every run because no producer ever emits the
    # canonical chunk kind. Best-effort: missing storage_dir / empty
    # file silently produces zero chunk drafts — that's the no-op
    # contract that lets `process_document_complete` fall back to
    # writing nothing without us crashing the compile.
    storage_dir = Path(request.settings.storage_dir or
                       f"{request.settings.workdir}/storage").expanduser()
    drafts.extend(_chunk_drafts_from_storage(
        storage_dir, document_id=request.document_id,
    ))

    # Build the post-parse manifest (counts, quality scores, per-image
    # triage decisions) and merge it into the result metadata. The
    # activity layer projects the recognised keys into `content_stats`
    # which the planner then merges into the DocumentProfile.
    manifest = _build_content_manifest(output_dir)
    metadata: dict[str, Any] = {
        "provider": "raganything",
        "output_dir": str(output_dir),
    }
    metadata.update(manifest)
    return ArtifactProcessingResult(
        status=ResultStatus.SUCCEEDED,
        drafts=drafts,
        metadata=metadata,
    )


def default_build_graph(request: "RAGAnythingGraphRequest") -> ArtifactProcessingResult:
    """Run a real RAGAnything graph build over `request.artifact_ids`.

    RAGAnything constructs the knowledge graph as a side-effect of
    document processing — this function reuses the same pipeline by
    feeding each artifact path back through `process_document_complete`
    and then collecting the graph artifacts the vendor writes to its
    storage dir.
    """
    # Same env bridge as compile — the graph path also drives
    # `process_document_complete` which can re-invoke the VLM
    # backend when the storage dir is regenerated.
    _apply_vlm_http_client_env(request.settings)
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
        kwargs["embedding_func"] = _make_embedding_func(embedding_client)
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
    """RAGAnything (via LightRAG) `await`s llm_model_func(prompt, **kw) → str.

    LightRAG awaits the result, so the wrapper has to be `async`.
    Underlying `TextLLMClient.generate` is synchronous, so we run it
    on a worker thread to keep the event loop free. Token usage is
    dropped at the vendor boundary — RAGAnything doesn't surface it.
    """

    async def _llm_callable(prompt: str, *args, **kwargs) -> str:
        text, _usage = await asyncio.to_thread(text_client.generate, prompt)
        return text

    return _llm_callable


def _make_vision_callable(vision_client) -> Callable[..., Any]:
    """RAGAnything `await`s vision_model_func(prompt, image_data, **kw) → str."""

    async def _vision_callable(prompt: str, image_data: bytes = b"",
                               *args, **kwargs) -> str:
        text, _usage = await asyncio.to_thread(
            vision_client.analyze_image, image_data, prompt=prompt,
        )
        return text

    return _vision_callable


def _make_embedding_func(embedding_client):
    """Wrap an `EmbeddingClient` in `lightrag.utils.EmbeddingFunc`.

    LightRAG does NOT accept a plain callable here — it accesses
    `.embedding_dim` / `.max_token_size` / `.func` on this object
    during init (see `lightrag.lightrag.LightRAG.__post_init__`).
    Passing a plain callable causes init to silently fail and
    `RAGAnything.lightrag` to stay `None`, surfacing later as
    `'NoneType' object has no attribute 'ainsert'`. The wrapper's
    inner `func` is awaited by LightRAG, so it must be async.

    LightRAG ALSO expects the awaited result to be a numpy
    `ndarray` — `lightrag.utils.EmbeddingFunc.__call__` reads
    `result.size` and computes `result.size % embedding_dim` to
    validate vector count. A Python `list[list[float]]` raises
    `AttributeError: 'list' object has no attribute 'size'`. We
    convert here.
    """
    try:
        from lightrag.utils import EmbeddingFunc
    except ImportError as exc:
        raise ProviderUnavailable(
            "RAGAnything requires `lightrag` to be importable for "
            "embedding wrapping (lightrag.utils.EmbeddingFunc). "
            "Install with: pip install j1[raganything]"
        ) from exc

    # numpy arrives as a transitive dependency of lightrag (and torch /
    # transformers) — safe to import unconditionally inside this branch.
    import numpy as np

    async def _embedding_callable(texts, *args, **kwargs):
        if isinstance(texts, str):
            texts = [texts]
        vectors, _usage = await asyncio.to_thread(
            embedding_client.embed_batch, list(texts),
        )
        return np.asarray(vectors, dtype=np.float32)

    return EmbeddingFunc(
        embedding_dim=embedding_client.dimension(),
        func=_embedding_callable,
        max_token_size=embedding_client.max_tokens(),
    )


# ---- Plain-text fast path (skip mineru's PDF-render-then-OCR loop) -


def _is_plain_text(source_path: Path) -> bool:
    return source_path.suffix.lower() in _NATIVE_TEXT_EXTENSIONS


# Fraction of sampled PDF pages that must have extractable text before
# we skip MinerU.  0.8 = at least 80 % of the first ≤5 pages carry a
# real text layer.  Deliberately conservative: a mixed scan/text PDF
# still routes to MinerU so we don't drop scanned content.
_PDF_TEXT_THRESHOLD = 0.8


def _is_text_extractable_pdf(
    source_path: Path,
    *,
    threshold: float = _PDF_TEXT_THRESHOLD,
    sample_pages: int = 5,
) -> bool:
    """Return True when `source_path` is a PDF whose embedded text layer
    is rich enough for the fast-text path.

    Samples up to `sample_pages` pages with pypdf (which is already a
    transitive dependency of raganything and is imported for page-count
    profiling elsewhere).  A page is counted as "has text" when
    `extract_text()` yields ≥ 20 non-whitespace characters — this
    filters out PDFs that have only a few invisible copy-protection or
    watermark text nodes.

    Returns False (→ full MinerU path) when:
      * source is not a .pdf
      * pypdf is not importable
      * the file can't be read (corrupt, encrypted, etc.)
      * text ratio < threshold  (scanned / image-heavy / formula-heavy)
    """
    if source_path.suffix.lower() != ".pdf":
        return False
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(source_path))
        total = len(reader.pages)
        if total == 0:
            return False
        n = min(sample_pages, total)
        pages_with_text = sum(
            1
            for i in range(n)
            if len((reader.pages[i].extract_text() or "").strip()) >= 20
        )
        return (pages_with_text / n) >= threshold
    except Exception:  # noqa: BLE001 — any failure → conservative full path
        return False


async def _insert_plain_text_directly(
    *,
    rag,
    source_path: Path,
    document_id: str,
    output_dir: Path,
) -> None:
    """Read `source_path` and insert its text directly into LightRAG.

    Bypasses `RAGAnything.process_document_complete` entirely for plain
    text — mineru's text path renders the file to a PDF and runs the
    full PyTorch model pipeline on the rendered output, which pegs all
    CPU cores even on a 10-byte file. Reading the bytes ourselves and
    handing them to `lightrag.ainsert` is functionally equivalent for
    plain text (no images / tables / formulas to extract) and orders
    of magnitude cheaper.

    Also writes the text into `output_dir` so the existing draft-walker
    (`_drafts_from_output_dir`) discovers it without special-casing.
    """
    text = source_path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        # Mirror raganything's own behaviour: skip the insert entirely
        # for empty / whitespace-only documents (it would otherwise
        # produce a chunk with no content and confuse retrieval).
        return

    await rag.lightrag.ainsert(
        input=text,
        file_paths=str(source_path),
        ids=document_id,
    )

    # Best-effort doc-status bookkeeping — keeps RAGAnything's view of
    # the document consistent with what `process_document_complete`
    # would have left behind. Failure here is non-fatal.
    mark = getattr(rag, "_mark_multimodal_processing_complete", None)
    if mark is not None:
        try:
            await mark(document_id)
        except Exception:  # noqa: BLE001 — bookkeeping must not fail compile
            _log.debug("doc-status bookkeeping failed (non-fatal)", exc_info=True)

    # Persist the text so the standard output-dir draft walker picks it up.
    out_path = output_dir / f"{document_id}.md"
    out_path.write_text(text, encoding="utf-8")


async def _insert_pdf_text_directly(
    *,
    rag,
    source_path: Path,
    document_id: str,
    output_dir: Path,
) -> None:
    """Extract text from a text-layer PDF with pypdf and insert into LightRAG.

    Bypasses `RAGAnything.process_document_complete` for PDFs that have
    reliable embedded text.  MinerU's `parse_method=auto` runs full layout
    analysis (layout blocks, OCR, formula/table detection) on every page
    regardless of content — for a normal 4-page text PDF this means several
    minutes of transformer inference on CPU/GPU with no benefit.  pypdf text
    extraction completes in < 1 second for the same file.

    Caller must first confirm the document is text-extractable via
    `_is_text_extractable_pdf()`.  This function does NOT re-check — it
    trusts the caller's decision and proceeds directly.

    Also writes the concatenated text into `output_dir` so the standard
    `_drafts_from_output_dir` walker discovers it without special-casing.
    """
    from pypdf import PdfReader

    reader = PdfReader(str(source_path))
    pages_text: list[str] = []
    for page in reader.pages:
        page_text = page.extract_text() or ""
        if page_text.strip():
            pages_text.append(page_text)

    combined = "\n\n".join(pages_text)
    if not combined.strip():
        # Empty extraction (all pages had no text); nothing to insert.
        return

    await rag.lightrag.ainsert(
        input=combined,
        file_paths=str(source_path),
        ids=document_id,
    )

    # Best-effort doc-status bookkeeping.
    mark = getattr(rag, "_mark_multimodal_processing_complete", None)
    if mark is not None:
        try:
            await mark(document_id)
        except Exception:  # noqa: BLE001
            _log.debug("doc-status bookkeeping failed (non-fatal)", exc_info=True)

    # Persist the extracted text for the draft walker.
    out_path = output_dir / f"{document_id}.md"
    out_path.write_text(combined, encoding="utf-8")


# ---- LibreOffice pre-conversion (broad format coverage) ------------
#
# RAGAnything / mineru parses PDF + modern OOXML (.docx/.xlsx/.pptx)
# + images natively. Legacy binary office formats (Word 97-2003 .doc,
# Excel 97-2003 .xls, PowerPoint 97-2003 .ppt), OpenDocument
# (.odt/.ods/.odp), Rich Text (.rtf), Apple iWork (.pages/.numbers/
# .key), and Microsoft Works (.wps) are NOT in mineru's parser
# coverage. For those, we shell out to `soffice --headless
# --convert-to pdf` to produce a PDF that raganything can then
# process via its standard pipeline.
#
# The set of "needs conversion" extensions is configured per
# `RAGAnythingSettings.pdf_convert_extensions` (env-driven). Setting
# the env var to empty disables conversion entirely.


def _needs_pdf_conversion(
    source_path: Path, settings: "RAGAnythingSettings",
) -> bool:
    """Return True iff this source's extension is on the
    per-deployment "convert before raganything" list."""
    if not settings.pdf_convert_extensions:
        return False
    return source_path.suffix.lower() in settings.pdf_convert_extensions


def _convert_to_pdf(
    source_path: Path, settings: "RAGAnythingSettings",
) -> Path:
    """Run `soffice --headless --convert-to pdf` against `source_path`.

    Returns the path to the produced PDF (lives in a fresh temp dir
    under the system tempdir; caller is responsible for cleanup).
    Raises:
      * `ProviderUnavailable` when the LibreOffice binary isn't on
        $PATH (operator-actionable: install libreoffice or override
        the compiler hook).
      * `_LibreOfficeConversionError` for runtime failures
        (non-zero exit, no output PDF produced, timeout). These are
        caught at the `default_compile` boundary and surfaced as a
        FAILED `ArtifactProcessingResult`.
    """
    binary = shutil.which(settings.libreoffice_binary)
    if binary is None:
        raise ProviderUnavailable(
            f"LibreOffice headless binary {settings.libreoffice_binary!r} "
            f"not found on $PATH — required to pre-convert "
            f"{source_path.suffix!r} files for raganything. Install "
            f"libreoffice (e.g. `apt-get install libreoffice-core "
            f"libreoffice-writer`), set "
            f"J1_RAGANYTHING_LIBREOFFICE_BINARY to the absolute path, "
            f"shrink J1_RAGANYTHING_PDF_CONVERT_EXTENSIONS to exclude "
            f"this format, or override the whole compile step via "
            f"J1_RAGANYTHING_COMPILER_PROCESSOR."
        )

    # mkdtemp (NOT TemporaryDirectory) — caller cleans up; soffice
    # writes the PDF here and we hand the path back without holding
    # an open context manager.
    tmpdir = Path(tempfile.mkdtemp(prefix="j1-soffice-"))
    argv = [
        binary,
        "--headless",
        "--convert-to", "pdf",
        "--outdir", str(tmpdir),
        str(source_path),
    ]

    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            timeout=settings.libreoffice_timeout_seconds,
            # No shell=True — argv is fully composed.
        )
    except subprocess.TimeoutExpired as exc:
        # Best-effort cleanup before re-raising as a conversion error.
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise _LibreOfficeConversionError(
            f"LibreOffice conversion of {source_path.name!r} timed out "
            f"after {settings.libreoffice_timeout_seconds}s. Increase "
            f"J1_RAGANYTHING_LIBREOFFICE_TIMEOUT_SECONDS or investigate "
            f"the source document."
        ) from exc
    except OSError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise _LibreOfficeConversionError(
            f"LibreOffice invocation failed at the OS level: {exc}"
        ) from exc

    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise _LibreOfficeConversionError(
            f"LibreOffice exited {completed.returncode} converting "
            f"{source_path.name!r}: {stderr[:512]}"
        )

    # soffice writes "<source-stem>.pdf" into --outdir.
    pdf_path = tmpdir / f"{source_path.stem}.pdf"
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise _LibreOfficeConversionError(
            f"LibreOffice produced no output for {source_path.name!r}. "
            f"stderr: {stderr[:512]}"
        )
    return pdf_path


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

    Excludes `kv_store_text_chunks.json` — that file contains text
    chunks, not graph data. It's surfaced separately as
    `kind="chunk"` artifacts via `_chunk_drafts_from_storage` so the
    Results > Chunks tab can review them with the canonical chunk
    DTO. Lumping them together as `graph_json` would force the chunk
    projector to scrape them out of every graph artifact at read
    time.
    """
    drafts: list[ArtifactDraft] = []
    if not storage_dir.exists():
        return drafts
    # Common LightRAG / RAGAnything graph filenames; pattern-match
    # against any of them.
    interesting = (
        "*graph*", "*relation*", "*entit*", "kv_store*.json",
    )
    # Files we exclude from graph_json — they're surfaced under their
    # own kind elsewhere. Match by lowercased filename so a
    # `kv_store_text_chunks.json` (canonical) and a hypothetical
    # `KV_STORE_TEXT_CHUNKS.JSON` both filter correctly.
    chunk_filenames = {"kv_store_text_chunks.json"}
    seen: set[Path] = set()
    for pattern in interesting:
        for path in storage_dir.rglob(pattern):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            if path.name.lower() in chunk_filenames:
                continue
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


def _chunk_drafts_from_storage(
    storage_dir: Path,
    *,
    document_id: str,
) -> list[ArtifactDraft]:
    """Project LightRAG's `kv_store_text_chunks.json` into canonical
    `kind="chunk"` ArtifactDrafts — one per chunk entry.

    The file is a top-level dict keyed by chunk id; each value carries
    `{tokens, content, full_doc_id, chunk_order_index, file_path}`.
    We map onto the neutral chunk shape the `ChunkProjector` expects
    (`{chunkId, body, tokenCount}`) so the Results > Chunks tab gets
    real reviewable text from real runs.

    Without this, the Chunks tab is correctly empty for every run
    (no producer in the dev stack emitted `kind="chunk"`) — even
    though LightRAG had the chunks on disk all along.
    """
    drafts: list[ArtifactDraft] = []
    if not storage_dir.exists():
        return drafts

    # Find the chunks file — LightRAG variants name it consistently.
    chunks_path: Path | None = None
    for path in storage_dir.rglob("*"):
        if path.is_file() and path.name.lower() == "kv_store_text_chunks.json":
            chunks_path = path
            break
    if chunks_path is None:
        return drafts

    try:
        payload = json.loads(chunks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return drafts
    if not isinstance(payload, dict):
        return drafts

    for chunk_id, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        body = entry.get("content")
        if not isinstance(body, str) or not body.strip():
            # Skip empty chunks — LightRAG sometimes emits
            # placeholders for split boundaries. Reviewing those
            # surfaces noise.
            continue
        # `tokens` is LightRAG's field for token count; pass through
        # as the canonical `tokenCount`. The projector also accepts
        # `tokens`, but we normalise here so the JSON-on-disk uses
        # the FE-facing field name.
        token_count = entry.get("tokens")
        chunk_payload: dict[str, Any] = {
            "chunkId": str(chunk_id),
            "body": body,
            "tokenCount": int(token_count) if isinstance(token_count, (int, float)) else None,
            "metadata": {
                "fullDocId": entry.get("full_doc_id"),
                "chunkOrderIndex": entry.get("chunk_order_index"),
                "filePath": entry.get("file_path"),
            },
        }
        # One draft per chunk so the Chunks tab can paginate them.
        # Each chunk artifact's source_document_ids points at the
        # ingest's document so the lineage fallback resolves it
        # cleanly even on legacy untagged runs.
        drafts.append(ArtifactDraft(
            kind="chunk",
            content=json.dumps(chunk_payload, ensure_ascii=False).encode("utf-8"),
            suggested_extension=".json",
            source_document_ids=[document_id],
            metadata={
                "chunk_id": str(chunk_id),
                "filename": chunks_path.name,
            },
        ))
    return drafts


# ---- Content manifest from MinerU output ---------------------------
#
# The post-parse planner reads aggregate counts (image_count,
# table_count, page_count, …) and per-image triage decisions to
# decide which optional stages to run. MinerU writes a structured
# `*_content_list*.json` alongside the markdown / image files when
# it finishes parsing — this helper pulls that file (when present)
# and falls back to filename-based heuristics otherwise. Output is
# the dict that the activity layer projects into `content_stats`.

_IMAGE_SUFFIXES: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif",
})
_TEXT_SUFFIXES: frozenset[str] = frozenset({
    ".md", ".markdown", ".txt",
})

# Filename heuristics for vision-triage decisions when MinerU does
# not surface per-image semantic data. These are coarse — the actual
# semantic decision lives in the vision LLM (when wired) or a deeper
# profiler (LLM-assisted).
_DECORATIVE_NAME_RE = re.compile(
    r"(logo|icon|watermark|header|footer|bullet|sprite)", re.IGNORECASE,
)
_SEMANTIC_NAME_RE = re.compile(
    r"(diagram|chart|figure|graph|workflow|architecture|screenshot)",
    re.IGNORECASE,
)
# Size thresholds (bytes). Tuned conservatively: under 2KB is almost
# always an icon/sprite; over 30KB is large enough to plausibly carry
# semantic content. The middle range gets `triage` so a fast vision
# pass can decide.
_DECORATIVE_MAX_BYTES = 2 * 1024
_SEMANTIC_MIN_BYTES = 30 * 1024


def _build_content_manifest(output_dir: Path) -> dict[str, Any]:
    """Build the post-parse content manifest from MinerU output.

    Returns a dict with aggregate counts, derived quality scores, and
    per-image triage decisions. Empty / missing output_dir yields an
    empty manifest; the planner treats `None` fields as "unknown" and
    falls back to the deterministic profile.

    The dict is shaped to match the keys the activity layer (`_artifact_result`
    in `j1.orchestration.activities.processing`) projects into the
    `ArtifactActivityResult.content_stats` field. Adding a new key here
    requires a corresponding entry in that projection list."""
    if not output_dir.exists():
        return {}

    text_chars = 0
    image_files: list[Path] = []
    table_count = 0
    equation_count = 0
    text_block_count = 0
    page_indices: set[int] = set()

    # Walk the output dir once; categorise by suffix + filename.
    content_list_path: Path | None = None
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        name = path.name.lower()
        if suffix in _IMAGE_SUFFIXES:
            image_files.append(path)
            continue
        if suffix in _TEXT_SUFFIXES:
            text_block_count += 1
            try:
                text_chars += len(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
            continue
        if suffix == ".json" and "content_list" in name and content_list_path is None:
            # MinerU writes `*_content_list.json` (or similar) with
            # the structured per-element list. Defer parsing until
            # after the walk so we have a single canonical path.
            content_list_path = path

    # Parse the structured content_list if present — it carries
    # per-element page indices, captions, and types we can't infer
    # from filenames alone.
    images_from_list: list[dict[str, Any]] = []
    if content_list_path is not None:
        try:
            payload = json.loads(content_list_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type", "")).lower()
                page_idx = item.get("page_idx")
                if isinstance(page_idx, int):
                    page_indices.add(page_idx)
                if item_type in ("image", "img"):
                    images_from_list.append(item)
                elif item_type == "table":
                    table_count += 1
                elif item_type in ("equation", "formula"):
                    equation_count += 1
                elif item_type == "text":
                    body = item.get("text") or ""
                    if isinstance(body, str):
                        text_chars = max(text_chars, text_chars)  # already counted via files

    image_count = max(len(image_files), len(images_from_list))
    page_count = max(page_indices) + 1 if page_indices else None

    images = _build_image_triage(image_files, images_from_list, output_dir)

    parse_quality_score = _score_parse_quality(text_block_count, text_chars)
    text_sufficiency_score = _score_text_sufficiency(text_chars, page_count)
    layout_complexity_score = _score_layout_complexity(
        image_count, table_count, equation_count, page_count,
    )

    manifest: dict[str, Any] = {
        "image_count": image_count,
        "table_count": table_count,
        "equation_count": equation_count,
        "text_block_count": text_block_count,
        "total_text_chars": text_chars,
        "has_images": image_count > 0,
        "has_tables": table_count > 0,
        "parse_quality_score": parse_quality_score,
        "text_sufficiency_score": text_sufficiency_score,
        "layout_complexity_score": layout_complexity_score,
        "images": images,
    }
    if page_count is not None:
        manifest["page_count"] = page_count
    return manifest


def _build_image_triage(
    image_files: list[Path],
    images_from_list: list[dict[str, Any]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    """Per-image triage decisions surfaced on the plan.

    Combines two information sources:
      * filesystem image files (size, filename heuristics)
      * MinerU's structured content_list entries (page_idx, caption,
        img_path) — when available

    Each entry is a flat dict so the audit-log payload stays compact
    and forward-compatible. Decision vocabulary mirrors
    `j1.processing.planning.VISION_DECISION_*` (skip / triage /
    enrich)."""
    decisions: list[dict[str, Any]] = []

    # Index the structured entries by image filename for cross-correlation.
    by_name: dict[str, dict[str, Any]] = {}
    for entry in images_from_list:
        img_path = entry.get("img_path") or entry.get("image_path") or ""
        if isinstance(img_path, str) and img_path:
            by_name[Path(img_path).name.lower()] = entry

    for path in image_files:
        try:
            size_bytes = path.stat().st_size
        except OSError:
            size_bytes = 0
        meta = by_name.get(path.name.lower(), {})
        caption = meta.get("img_caption") or meta.get("caption") or ""
        if isinstance(caption, list):
            caption = " ".join(str(c) for c in caption if c)
        page = meta.get("page_idx")
        decision, role, score, reason = _classify_image(
            filename=path.name,
            size_bytes=size_bytes,
            caption=str(caption or ""),
        )
        decisions.append({
            "image_id": str(path.relative_to(output_dir)),
            "decision": decision,
            "role": role,
            "score": score,
            "reason": reason,
            "size_bytes": size_bytes,
            "page": page if isinstance(page, int) else None,
            "caption": str(caption or "")[:200] or None,
        })

    # Structured-list entries that didn't resolve to a file on disk
    # (MinerU sometimes references images that were inlined or
    # discarded). Keep them so the operator can see the count match.
    seen_names = {Path(d["image_id"]).name.lower() for d in decisions}
    for entry in images_from_list:
        img_path = entry.get("img_path") or entry.get("image_path") or ""
        if not isinstance(img_path, str) or not img_path:
            continue
        if Path(img_path).name.lower() in seen_names:
            continue
        decisions.append({
            "image_id": img_path,
            "decision": "triage",
            "role": "unknown",
            "score": 0.5,
            "reason": "image referenced in content_list but not on disk",
            "size_bytes": None,
            "page": entry.get("page_idx") if isinstance(entry.get("page_idx"), int) else None,
            "caption": str(entry.get("img_caption") or entry.get("caption") or "")[:200] or None,
        })
    return decisions


def _classify_image(
    *, filename: str, size_bytes: int, caption: str,
) -> tuple[str, str, float, str]:
    """Filename + size + caption heuristics for one image.

    Returns (decision, role, score, reason). Decisions land at one of:
      * `skip`    — almost certainly decorative; don't burn vision LLM
      * `enrich`  — likely semantic content; run the full vision pass
      * `triage`  — uncertain; let a cheap vision pass classify it
    Score is 0..1 confidence in the chosen decision."""
    if _DECORATIVE_NAME_RE.search(filename):
        return ("skip", "decorative", 0.9,
                f"filename matches decorative pattern ({filename!r})")
    if size_bytes and size_bytes <= _DECORATIVE_MAX_BYTES:
        return ("skip", "icon", 0.85,
                f"file is {size_bytes} bytes, under decorative threshold")
    if _SEMANTIC_NAME_RE.search(filename):
        return ("enrich", "diagram", 0.9,
                f"filename matches semantic pattern ({filename!r})")
    if caption and len(caption) >= 20:
        # Long caption = MinerU thinks this image carries enough
        # context to talk about. Vision enrichment is high-value.
        return ("enrich", "captioned", 0.8,
                "image has a substantive caption")
    if size_bytes >= _SEMANTIC_MIN_BYTES:
        return ("enrich", "large", 0.65,
                f"file is {size_bytes} bytes, above semantic threshold")
    return ("triage", "unknown", 0.5,
            "no strong filename / size / caption signal — needs cheap triage")


def _score_parse_quality(text_block_count: int, text_chars: int) -> float:
    """0..1 score for how well MinerU extracted text content.

    A clean text PDF yields several markdown files and many chars;
    a failed parse yields nothing or near-empty output."""
    if text_block_count == 0 or text_chars == 0:
        return 0.0
    if text_chars < 100:
        return 0.3  # very thin output, possibly a parse failure
    if text_block_count >= 1 and text_chars >= 500:
        return 1.0
    return 0.7


def _score_text_sufficiency(text_chars: int, page_count: int | None) -> float:
    """0..1 score for whether the document has enough text to skip
    vision/OCR enrichment. ~1000 chars/page is the rule of thumb for
    a 'text-rich' page."""
    if text_chars == 0:
        return 0.0
    pages = page_count or 1
    chars_per_page = text_chars / max(pages, 1)
    if chars_per_page >= 1000:
        return 1.0
    return min(1.0, chars_per_page / 1000)


def _score_layout_complexity(
    image_count: int, table_count: int, equation_count: int,
    page_count: int | None,
) -> float:
    """0..1 score for how visually busy the document is. ≥5 visual
    elements per page caps at 1.0 (complex). Empty / pure-text
    documents score 0."""
    visuals = image_count + table_count + equation_count
    if visuals == 0:
        return 0.0
    pages = page_count or 1
    per_page = visuals / max(pages, 1)
    return min(1.0, per_page / 5)


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
