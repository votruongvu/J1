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

        await rag.process_document_complete(
            file_path=str(source_path),
            output_dir=str(output_dir),
            parse_method=request.settings.parse_method,
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
