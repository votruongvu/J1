"""Real default boundary to the RAGAnything Python library.

This module is the actual integration glue that translates J1
canonical inputs (LLM clients, settings, ProjectContext) into
calls against the `raganything` package's public API, then
normalises the vendor's outputs back into J1
`ArtifactProcessingResult` / `QueryResult` shapes.

Targeted vendor surface (HKUDS/RAGAnything 1.x):

 from raganything import RAGAnything, RAGAnythingConfig
 rag = RAGAnything(
 config=RAGAnythingConfig(working_dir=...,...),
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
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from j1.connectors.graph.config import ARTIFACT_KIND_GRAPH_JSON
from j1.processing.results import (
    ARTIFACT_KIND_CHUNK,
    ARTIFACT_KIND_COMPILED_TEXT,
    ARTIFACT_KIND_PARSED_CONTENT_MANIFEST,
    ARTIFACT_KIND_PARSED_SOURCE,
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
#
# MUST stay in lockstep with `_PLAIN_TEXT_EXTENSIONS` in
# `j1.processing.assessment`. The assessor uses the same set to pick
# `CompileMode.FAST`; if the bridge's set is narrower, the assessor
# selects FAST but the bridge still routes through MinerU — defeating
# the whole point of fast mode. Touch BOTH files together when adding
# a new extension and add a regression test in
# `test_assessment_plan.py` that pins the new extension to FAST.
_NATIVE_TEXT_EXTENSIONS = frozenset({
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


class _LibreOfficeConversionError(RuntimeError):
    """Raised when soffice subprocess fails for a known runtime reason
 (non-zero exit, no output produced, timeout). Caught at the
 `default_compile` boundary and surfaced as a FAILED result.
 Distinct from `ProviderUnavailable`, which is reserved for the
 "binary not installed" infrastructure case."""


_HTTP_CLIENT_BACKENDS = frozenset({"vlm-http-client", "hybrid-http-client"})


def _apply_vlm_http_client_env(settings: "RAGAnythingSettings") -> None:
    """When `backend` is one of the HTTP-client variants
 (`vlm-http-client`, `hybrid-http-client`), propagate J1's
 vision-LLM config into the env vars MinerU's
 `mineru_vl_utils.MinerUClient` reads at runtime:
 `MINERU_VL_SERVER`, `MINERU_VL_API_KEY`, `MINERU_VL_MODEL_NAME`,
 and `MINERU_VL_MAX_CONCURRENCY` (caps the parallel-request
 fanout — defaults to 1 to protect self-hosted VLM endpoints
 from OOM under MinerU's default high-fanout pattern).

 Both backend names route VLM requests to the same OpenAI-compatible
 HTTP server; only the local-computation strategy differs. Either
 one without `MINERU_VL_SERVER` set crashes with
 "Environment variable MINERU_VL_SERVER is not set."

 The CLI accepts `-u/--vlm-url` for the server URL (we also pass
 that as a kwarg to the parse call), but the API key and
 model-name fields have no CLI flag — mineru-vl-utils reads them
 directly from the environment. Without this propagation the
 request reaches LM Studio with no Authorization header and an
 auto-detected model name (which on multi-model servers picks the
 wrong one).

 With it, the existing `J1_RAGANYTHING_VLM_HTTP_*` config (already
 wired for the rest of the stack) is the only thing the operator
 needs in place — flipping `J1_RAGANYTHING_BACKEND` to either
 HTTP-client variant is the sole additional change.

 Idempotent — only sets each env var when (a) the backend is an
 HTTP-client variant, (b) we have a value, and (c) the operator
 hasn't already exported the var directly. Operator-supplied
 `MINERU_VL_*` always wins so existing deployments keep their
 tuning.

 No-op when backend is anything else (default `None` lets MinerU
 pick its own engine and never reads these vars)."""
    if settings.backend not in _HTTP_CLIENT_BACKENDS:
        return
    # Concurrency cap is propagated as a STRING because env values
    # are strings. mineru_vl_utils parses `MINERU_VL_MAX_CONCURRENCY`
    # as int. Default of 1 is safest for self-hosted single-process
    # VLM servers (LM Studio / single llama-server) — see the
    # settings module for the full rationale.
    max_concurrency = max(int(settings.vlm_http_max_concurrency or 1), 1)
    mapping = {
        "MINERU_VL_SERVER": settings.vlm_http_server_url,
        "MINERU_VL_API_KEY": settings.vlm_http_api_key,
        "MINERU_VL_MODEL_NAME": settings.vlm_http_model_name,
        "MINERU_VL_MAX_CONCURRENCY": str(max_concurrency),
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

 Reads the source file, optionally pre-converts via LibreOffice,
 drives `RAGAnything.process_document_complete`, and walks the
 output for ArtifactDrafts. This is the single entry point: J1
 runs RAGAnything as a single-activity compile (parse + chunk +
 index together).
 """
    # Bridge the J1 vision LLM config into MinerU's expected env
    # vars when the operator has selected the HTTP-client backend.
    # No-op for the default `auto` parse method.
    _apply_vlm_http_client_env(request.settings)

    # Resolve the AssessmentPlan-derived compile config UPFRONT so
    # we can pass `config_overrides` into `_prepare_compile` (which
    # builds the RAGAnything instance + applies them to the
    # `RAGAnythingConfig`). Holds parser_kwargs + warnings for the
    # downstream call site + the SUCCEEDED-path metadata builder.
    compile_plan_config = _resolve_compile_config(request)
    rag, output_dir, source_path, dropped_config_overrides = _prepare_compile(
        request,
        config_overrides=(
            compile_plan_config.to_config_overrides()
            if compile_plan_config is not None else None
        ),
    )
    # Operator-readable path log — folks debugging slow MinerU runs
    # on macOS Docker Desktop want to confirm the parse output is
    # NOT landing in a bind-mounted folder. Prints once per compile.
    _log.info(
        "compile paths: workdir=%s output_dir=%s source=%s",
        request.settings.workdir, output_dir, source_path.name,
    )

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

    # Closure-shared holder for plan-derived warnings + unhandled
    # capabilities. The resolution itself happened above
    # (`compile_plan_config`); this dict surfaces the result to the
    # SUCCEEDED-path metadata builder via the nested `_run_compile`.
    plan_metadata: dict[str, tuple[str, ...]] = {
        "plan_warnings": (),
        "unhandled_capabilities": (),
    }
    plan_resolved_mode: list[str] = [""]
    if compile_plan_config is not None:
        # Merge the dropped-config-override field names into the
        # unhandled list — the mapper says "I asked for X" and the
        # bridge says "the installed vendor doesn't expose the
        # corresponding config field". Concrete signal for a future
        # optimisation pass to consume.
        merged_unhandled: list[str] = list(
            compile_plan_config.unhandled_capabilities
        )
        merged_warnings: list[str] = list(compile_plan_config.warnings)
        for dropped in dropped_config_overrides or ():
            merged_warnings.append(
                f"RAGAnythingConfig field {dropped!r} not exposed by "
                "installed vendor version; per-capability switch dropped"
            )
            # Map config-field-name → capability label so the
            # metadata's `unhandled_capabilities` stays in plan
            # vocabulary rather than vendor-internal naming.
            label = {
                "enable_image_processing": "image_extraction",
                "enable_table_processing": "table_extraction",
                "enable_equation_processing": "formula_extraction",
            }.get(dropped, dropped)
            if label not in merged_unhandled:
                merged_unhandled.append(label)
        plan_metadata["plan_warnings"] = tuple(merged_warnings)
        plan_metadata["unhandled_capabilities"] = tuple(merged_unhandled)
        plan_resolved_mode[0] = compile_plan_config.resolved_mode

    async def _run_compile():
        # `_ensure_lightrag_initialized` returns {"success": False, "error":...}
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
        # document has embedded text on most pages. MinerU's auto mode
        # runs full layout analysis (layout blocks, OCR, formula /
        # table detection) on every page regardless of content — for a
        # normal text PDF this adds 3–5 minutes of transformer
        # inference with no benefit. pypdf text extraction for the
        # same file completes in < 1 second.
        #
        # We only activate the fast path when:
        #  * the source is a.pdf (not already-converted other format)
        #  * at least _PDF_TEXT_THRESHOLD fraction of sampled pages
        #  have meaningful embedded text (≥ 20 non-whitespace chars)
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
        if (
            request.settings.backend in _HTTP_CLIENT_BACKENDS
            and request.settings.vlm_http_server_url
        ):
            # `-u/--vlm-url` is the CLI flag for the VLM server URL.
            # raganything's parser forwards this kwarg verbatim.
            # Both `vlm-http-client` and `hybrid-http-client` route
            # vision calls to the same OpenAI-compatible server.
            mineru_kwargs["vlm_url"] = request.settings.vlm_http_server_url

        # AssessmentPlan-driven parse_method takes precedence over
        # the static `settings.parse_method`. The plan was already
        # resolved into `compile_plan_config` outside this closure
        # (so config_overrides could feed `_prepare_compile`); here
        # we just consume the parser_kwargs slice.
        if compile_plan_config is not None:
            parser_kwargs = compile_plan_config.to_parser_kwargs()
        else:
            parser_kwargs = {"parse_method": request.settings.parse_method}

        for w in plan_metadata["plan_warnings"]:
            _log.warning(
                "compile config warning for %r: %s",
                request.document_id, w,
            )
        _log.info(
            "full-parse path: routing document %r to MinerU "
            "(parse_method=%s, backend=%s, file=%s, plan_driven=%s)",
            request.document_id,
            parser_kwargs.get("parse_method"),
            request.settings.backend or "<mineru-default>",
            source_path.name,
            compile_plan_config is not None,
        )
        await rag.process_document_complete(
            file_path=str(source_path),
            output_dir=str(output_dir),
            **parser_kwargs,
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
    from j1.providers.raganything._persistent_loop import get_persistent_loop

    # IMPORTANT: dispatch onto the process-persistent event loop.
    # `asyncio.run(...)` would create a fresh loop per call and
    # collide with LightRAG's module-level cached `asyncio.Lock`s,
    # producing `RuntimeError:... is bound to a different event
    # loop` on the second compile invocation in the same worker
    # process. See _persistent_loop.py for the full rationale.
    loop = get_persistent_loop()
    parse_start = time.monotonic()
    try:
        with attach_mineru_progress_handler(
            request.progress_reporter, request.ctx, request.run_id or "",
        ):
            loop.run_coroutine(_run_compile())
    except RuntimeError as exc:
        # "asyncio.run cannot be called from a running event loop"
        # used to be the most likely RuntimeError here. We've moved
        # off `asyncio.run` so this branch is mostly historical —
        # leaving the actionable message in case some downstream
        # callable still triggers nested-loop issues.
        if "running event loop" in str(exc):
            raise ProviderUnavailable(
                "RAGAnything's async API can't be driven from inside a "
                "running event loop. Wire your own `compile_callable` "
                "(or J1_RAGANYTHING_COMPILER_PROCESSOR) that awaits "
                "process_document_complete on the existing loop."
            ) from exc
        raise
    finally:
        parse_elapsed_ms = int((time.monotonic() - parse_start) * 1000)
        _log.info(
            "MinerU parse complete: document=%s parse_elapsed_ms=%d output_dir=%s",
            request.document_id, parse_elapsed_ms, output_dir,
        )
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

    # `storage_dir` resolution. Per-run isolation: when the request
    # carries a run_id (compile dispatched from the orchestration
    # path), LightRAG was instantiated with
    # `working_dir=<workdir>/runs/<tenant>/<project>/<doc>/<run>/`
    # — so its graphml + KV files are written there, not in the
    # global workdir. We point `storage_dir` at the same scoped path
    # so the draft collectors (`_graph_drafts_from_storage`,
    # `_chunk_drafts_from_storage`) read from the right tree.
    #
    # When no run_id is present (legacy / test callers), fall back
    # to the historical default: `settings.storage_dir or
    # settings.workdir` walked with rglob.
    scoped = workspace_path_for_run(
        request.settings, request.ctx, request.document_id, request.run_id,
    )
    storage_dir = (
        scoped if scoped is not None
        else Path(
            request.settings.storage_dir or request.settings.workdir
        ).expanduser()
    )

    # Detect LightRAG silent failures BEFORE we collect drafts. RAGAnything
    # swallows internal LightRAG errors (e.g. embedding dimension
    # mismatch, vector-store upsert failure) — `process_document_complete`
    # logs the traceback as ERROR and returns normally, leaving us to
    # report compile=succeeded for a document that produced nothing.
    # LightRAG records the real outcome in `kv_store_doc_status.json`
    # under the document id; if the status there is `failed`, surface
    # the error so the workflow's required-step contract trips and the
    # operator sees the actual cause instead of an empty Chunks tab.
    lightrag_error = _detect_lightrag_doc_failure(
        storage_dir, document_id=request.document_id,
    )
    if lightrag_error is not None:
        return ArtifactProcessingResult(
            status=ResultStatus.FAILED,
            error=lightrag_error,
            message="LightRAG marked document as failed",
            metadata={
                "provider": "raganything",
                "stage": "lightrag_postcheck",
                "document_id": request.document_id,
            },
        )

    drafts = _drafts_from_output_dir(
        output_dir, document_id=request.document_id, kind=ARTIFACT_KIND_COMPILED_TEXT,
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
    drafts.extend(_chunk_drafts_from_storage(
        storage_dir, document_id=request.document_id,
    ))

    # Build the post-parse manifest (counts, quality scores, per-image
    # triage decisions) and merge it into the result metadata. The
    # activity layer projects the recognised keys into `content_stats`
    # which the planner then merges into the DocumentProfile.
    manifest = _build_content_manifest(output_dir)
    # Tag image drafts with their per-image triage decision so the
    # `VisualContentDescriber` enricher can skip decorative images
    # without re-running the heuristics later. The bridge already
    # ran `_classify_image` per file when building the manifest;
    # propagating the decision via artifact metadata keeps the cost
    # in one place (the parse stage) and lets VCD short-circuit.
    drafts = _stamp_image_decisions(drafts, manifest.get("images") or [])
    metadata: dict[str, Any] = {
        "provider": "raganything",
        "output_dir": str(output_dir),
    }
    metadata.update(manifest)

    # Plan-derived observability. Always set the keys (with empty
    # defaults) so downstream consumers can rely on them existing —
    # absence vs. empty list ambiguity is a future-debugging trap.
    # Values come from the `_run_compile` closure's `plan_metadata`
    # holder; populated only when the caller supplied an
    # AssessmentPlan, otherwise they stay empty tuples.
    metadata["plan_warnings"] = list(plan_metadata["plan_warnings"])
    metadata["unhandled_capabilities"] = list(
        plan_metadata["unhandled_capabilities"]
    )
    if plan_resolved_mode[0]:
        metadata["assessment_mode"] = plan_resolved_mode[0]

    # Persist a normalized `ParsedContentManifest` alongside the
    # compile output so downstream consumers (post-compile replan,
    # quality projector, future tools) can read the parser's findings
    # without re-walking the storage_dir or coupling to MinerU's
    # output shape. See `j1.processing.manifest` for the schema.
    drafts.append(_build_manifest_draft(
        document_id=request.document_id,
        document_hash=getattr(request, "document_hash", None) or "",
        parser="raganything",
        parser_version=metadata.get("provider_version"),
        parse_method=getattr(request.settings, "parse_method", None),
        compile_stats=manifest,
    ))

    # Per-document MinerU output cleanup. By this point the bridge
    # has walked `output_dir` and turned every useful file into an
    # `ArtifactDraft`; the registry will materialise those into the
    # workspace's `compiled/` area. The original output_dir is
    # disposable scratch — leaving it grows the raganything workdir
    # unbounded across runs. Operator can opt into preservation for
    # debugging via `J1_KEEP_FAILED_INGEST_ARTIFACTS=true`; this
    # branch covers the SUCCEEDED path only, so failed compiles
    # leave their output_dir intact regardless of the flag (failed
    # runs always preserve evidence).
    _cleanup_output_dir(output_dir, document_id=request.document_id)

    return ArtifactProcessingResult(
        status=ResultStatus.SUCCEEDED,
        drafts=drafts,
        metadata=metadata,
    )


def _cleanup_output_dir(output_dir: Path, *, document_id: str) -> None:
    """Best-effort delete of MinerU's per-document output directory.

 Runs only on the SUCCEEDED compile path. Failed compiles preserve
 the output_dir so operators can grep through MinerU's intermediate
 files. Honors `J1_KEEP_FAILED_INGEST_ARTIFACTS` when set to a
 truthy value: skips even successful-path cleanup, useful when an
 operator wants to inspect a successful parse's intermediate
 layout/OCR files.

 Failures are non-fatal — the disposable scratch will eventually
 get GCd via `docker compose down -v` if nothing else."""
    keep_raw = os.environ.get("J1_KEEP_FAILED_INGEST_ARTIFACTS", "").strip().lower()
    if keep_raw in ("1", "true", "yes", "on"):
        _log.info(
            "compile cleanup skipped: J1_KEEP_FAILED_INGEST_ARTIFACTS=%s "
            "preserving %s for document %s",
            keep_raw, output_dir, document_id,
        )
        return
    if not output_dir.exists():
        return
    cleanup_start = time.monotonic()
    try:
        shutil.rmtree(output_dir)
    except OSError as exc:
        _log.warning(
            "compile cleanup failed for document %s output_dir=%s: %s",
            document_id, output_dir, exc,
        )
        return
    cleanup_elapsed_ms = int((time.monotonic() - cleanup_start) * 1000)
    _log.info(
        "compile cleanup complete: document=%s cleanup_elapsed_ms=%d",
        document_id, cleanup_elapsed_ms,
    )


def _stamp_image_decisions(
    drafts: list[ArtifactDraft],
    image_decisions: list[dict[str, Any]],
) -> list[ArtifactDraft]:
    """Annotate image drafts with their per-image triage decision.

 Reads the manifest's `images[]` entries (each carries
 `image_id` = the relative path under output_dir, and a
 `decision` of `skip|triage|enrich`). For each draft whose
 `metadata.relative_path` matches an entry's `image_id`, returns
 a copy of the draft with `vision_decision`/`vision_role`/
 `vision_score`/`vision_reason` merged into its metadata.

 Drafts without a matching entry pass through unchanged.
 `ArtifactDraft` is frozen, so we rebuild rather than mutate.
 """
    if not image_decisions:
        return drafts
    by_image_id: dict[str, dict[str, Any]] = {}
    for entry in image_decisions:
        image_id = entry.get("image_id")
        if isinstance(image_id, str) and image_id:
            by_image_id[image_id] = entry
    if not by_image_id:
        return drafts
    out: list[ArtifactDraft] = []
    for draft in drafts:
        relative_path = draft.metadata.get("relative_path") if isinstance(draft.metadata, dict) else None
        decision_entry = by_image_id.get(relative_path) if relative_path else None
        if decision_entry is None:
            out.append(draft)
            continue
        merged_metadata = dict(draft.metadata or {})
        merged_metadata["vision_decision"] = decision_entry.get("decision")
        merged_metadata["vision_role"] = decision_entry.get("role")
        merged_metadata["vision_score"] = decision_entry.get("score")
        merged_metadata["vision_reason"] = decision_entry.get("reason")
        out.append(ArtifactDraft(
            kind=draft.kind,
            content=draft.content,
            suggested_extension=draft.suggested_extension,
            source_document_ids=list(draft.source_document_ids),
            source_artifact_ids=list(draft.source_artifact_ids),
            metadata=merged_metadata,
            review_required=draft.review_required,
        ))
    return out


def _build_manifest_draft(
    *,
    document_id: str,
    document_hash: str,
    parser: str,
    parser_version: str | None,
    parse_method: str | None,
    compile_stats: dict[str, Any],
) -> ArtifactDraft:
    """Wrap the existing compile-stats dict in a canonical
 `ParsedContentManifest` and return it as an `ArtifactDraft` of
 kind `ARTIFACT_KIND_PARSED_CONTENT_MANIFEST`.

 Lazy-imports `j1.processing.manifest` to keep the bridge module
 importable in environments where the manifest module hasn't
 landed yet.
 """
    from j1.processing.manifest import manifest_from_compile_stats

    manifest_obj = manifest_from_compile_stats(
        document_id=document_id,
        document_hash=document_hash,
        parser=parser,
        parser_version=parser_version,
        parse_method=parse_method,
        # `profile` is unknown at the bridge layer (it lives on the
        # workflow's IngestPlan). Leaving None; the workflow can
        # backfill via metadata when it persists the artifact.
        profile=None,
        compile_stats=compile_stats,
    )
    return ArtifactDraft(
        kind=ARTIFACT_KIND_PARSED_CONTENT_MANIFEST,
        content=manifest_obj.to_json_bytes(),
        suggested_extension=".json",
        metadata={
            "filename": f"{document_id}.parsed_content_manifest.json",
            "parser": parser,
            "parse_method": parse_method or "",
        },
    )


def default_build_graph(request: "RAGAnythingGraphRequest") -> ArtifactProcessingResult:
    """Run a real RAGAnything graph build over `request.artifact_ids`.

 RAGAnything constructs the knowledge graph as a side-effect of
 document processing — this function reuses the same pipeline by
 feeding each artifact path back through `process_document_complete`
 and then collecting the graph artifacts the vendor writes to its
 storage dir.

 Per-run isolation: when `request.run_id` + `request.document_id`
 are supplied (production path), the storage dir is the same
 scoped workspace the compile stage wrote into:
 ``{workdir}/runs/{tenant}/{project}/{doc}/{run}/``. The emitted
 drafts are stamped with ``run_id`` + ``document_id`` + lineage
 fields so the orchestration registration writes them with
 correct ``metadata.run_id`` regardless of whether
 ``correlation_id`` was threaded through.
 """
    # Same env bridge as compile — the graph path also drives
    # `process_document_complete` which can re-invoke the VLM
    # backend when the storage dir is regenerated.
    _apply_vlm_http_client_env(request.settings)
    scoped = workspace_path_for_run(
        request.settings, request.ctx, request.document_id, request.run_id,
    )
    # Graph path: no AssessmentPlan today (graph plans are stage-
    # gating not compile-config); drop the dropped-overrides slot.
    rag, _ = _build_rag_instance(
        text_client=request.text_client,
        vision_client=None,
        embedding_client=request.embedding_client,
        settings=request.settings,
        working_dir_override=scoped,
    )
    # Storage dir is the same scoped path the compile stage wrote
    # into when run_id/document_id are present. Falls back to the
    # legacy unscoped workdir for direct/test callers.
    storage_dir = (
        scoped if scoped is not None
        else Path(
            request.settings.storage_dir or request.settings.workdir
        ).expanduser()
    )
    storage_dir.mkdir(parents=True, exist_ok=True)
    drafts = _graph_drafts_from_storage(
        storage_dir,
        request.artifact_ids,
        ctx=request.ctx,
        document_id=request.document_id,
        run_id=request.run_id,
    )
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
        metadata={
            "provider": "raganything",
            "storage_dir": str(storage_dir),
            "run_id": request.run_id,
            "document_id": request.document_id,
        },
    )


def default_query(request: "RAGAnythingQueryRequest") -> QueryResult:
    """Run a real RAGAnything query via `aquery`.

    Per-run workspace isolation: when ``request.run_id`` +
    ``request.document_id`` are supplied, LightRAG is instantiated
    with ``working_dir`` pointed at that specific run's storage. So
    a query against ``RunScope(run_id=X)`` reads the graph that
    run X produced — not whatever happened to be in the global
    workdir last. When the scoping inputs are absent, falls back
    to ``settings.workdir`` (legacy / workspace-scoped behaviour).
    """
    # Query path: no AssessmentPlan-driven config_overrides.
    working_dir_override = workspace_path_for_run(
        request.settings, request.ctx, request.document_id, request.run_id,
    )
    rag, _ = _build_rag_instance(
        text_client=request.text_client,
        vision_client=None,
        embedding_client=request.embedding_client,
        settings=request.settings,
        working_dir_override=working_dir_override,
    )
    aquery = getattr(rag, "aquery", None)
    if aquery is None:
        raise ProviderUnavailable(
            "RAGAnything instance has no `aquery` method (looked for it on "
            f"{type(rag).__name__}). Override via "
            "J1_RAGANYTHING_RETRIEVAL_PROCESSOR or query_callable=."
        )
    # Same persistent-loop dispatch as default_compile — LightRAG's
    # module-level cached locks must stay bound to one loop across
    # calls. See _persistent_loop.py.
    from j1.providers.raganything._persistent_loop import get_persistent_loop
    loop = get_persistent_loop()
    try:
        answer = loop.run_coroutine(aquery(request.question, mode="hybrid"))
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


def _resolve_compile_config(request):
    """Map the request's `assessment_plan` (if any) into a
 `CompileConfig`. Returns None for legacy callers without a plan;
 the bridge then falls back to `settings.parse_method` and skips
 config_overrides entirely.

 `getattr` (not direct access) tolerates legacy callers /
 SimpleNamespace-based test fixtures that build a request
 without the new `assessment_plan` field."""
    assessment_plan = getattr(request, "assessment_plan", None)
    if assessment_plan is None:
        return None
    from j1.providers.raganything.plan_mapper import (
        map_assessment_to_raganything_config,
    )
    return map_assessment_to_raganything_config(
        assessment_plan, request.settings,
    )


def _prepare_compile(
    request: "RAGAnythingCompileRequest",
    *,
    config_overrides: dict[str, Any] | None = None,
):
    """Build a RAGAnything instance + resolve I/O paths for compile.

 Vendor import happens first so a missing `raganything` package
 surfaces the actionable pip-install hint before path / env errors.

 `config_overrides` (when supplied — typically by the AssessmentPlan
 mapper) is applied to the `RAGAnythingConfig` instance before the
 `RAGAnything` instance is constructed. Returns
 `(rag, output_dir, source_path, dropped_overrides)` where
 `dropped_overrides` lists the override field names the installed
 vendor version doesn't expose. None / empty when no overrides
 were supplied.
 """
    # Per-run LightRAG workspace isolation. The compile request
    # already carries `run_id`; together with the project context
    # and document id we have everything needed to namespace the
    # graph storage so two reindex runs for the same document do
    # NOT overwrite each other's graphml. Returns None (and we fall
    # back to the legacy unscoped workdir) only when run_id is
    # missing — preserved for non-run callers / tests.
    working_dir_override = workspace_path_for_run(
        request.settings, request.ctx, request.document_id, request.run_id,
    )
    rag, dropped_overrides = _build_rag_instance(
        text_client=request.text_client,
        vision_client=request.vision_client,
        embedding_client=request.embedding_client,
        settings=request.settings,
        config_overrides=config_overrides,
        working_dir_override=working_dir_override,
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
    return rag, output_dir, source_path, dropped_overrides


def workspace_path_for_run(
    settings,
    ctx: "ProjectContext | None",
    document_id: str | None,
    run_id: str | None,
) -> Path | None:
    """Return the per-run LightRAG working directory path.

    Returns ``None`` when any of the inputs are missing — the caller
    then falls back to ``settings.workdir`` (legacy unscoped storage).
    Only when *all four* (workdir + ctx + document_id + run_id) are
    present do we build the namespaced path.

    Path shape:

        {workdir}/runs/{tenant_id}/{project_id}/{document_id}/{run_id}

    The four-level namespace mirrors the rest of J1's workspace
    layout (tenant → project → document → run) so retention /
    detach / remove can prune by deleting the appropriate subtree:

      * detach document → can delete `{document_id}/` and lose every
        run's graph in one rm -rf;
      * remove document → same as detach;
      * specific run prune (post-success → only retain active run) →
        delete sibling `{run_id}/` directories;
      * reindex (new run for same document) → write to a NEW
        `{run_id}/` subdir; the previous active run's graph stays
        intact for retrieval until the new run is promoted.

    LightRAG itself doesn't read this path's structure — it just
    writes its graphml + KV files at the path's leaf. The structural
    invariant is purely on J1's side, enforced here.

    """
    if not run_id or not document_id or ctx is None:
        return None
    workdir = getattr(settings, "workdir", None)
    if not workdir:
        return None
    tenant = getattr(ctx, "tenant_id", None)
    project = getattr(ctx, "project_id", None)
    if not tenant or not project:
        return None
    return (
        Path(str(workdir)).expanduser()
        / "runs" / str(tenant) / str(project) / str(document_id) / str(run_id)
    )


def _build_rag_instance(
    *,
    text_client,
    vision_client,
    embedding_client,
    settings,
    config_overrides: dict[str, Any] | None = None,
    working_dir_override: Path | None = None,
):
    """Construct a `raganything.RAGAnything` instance with J1 callables.

 Defensive about vendor API shape changes. Looks up the symbols
 actually exported by the installed `raganything` package; if the
 expected name isn't there, raises `ProviderUnavailable` naming
 the missing symbol.

 `config_overrides` is the AssessmentPlan-derived per-capability
 flag dict; applied via `_build_rag_config`. Returns
 `(instance, dropped_override_fields)` so the caller can surface
 the dropped names through `metadata["unhandled_capabilities"]`.

 `working_dir_override`, when provided, replaces ``settings.workdir``
 as the per-run scoped working directory passed to LightRAG. This
 is how compile + graph build + query use the same per-run path
 so a query against ``RunScope(run_id=X)`` reads the graph
 produced by run X (and only run X). Pre-existing callers that
 don't pass it continue to use the global ``settings.workdir`` —
 backward-compatible.
 """
    raganything_mod = _import_raganything()
    rag_cls = getattr(raganything_mod, "RAGAnything", None)
    if rag_cls is None:
        raise ProviderUnavailable(
            "Installed `raganything` package has no `RAGAnything` class "
            "at the top level — vendor API may have changed. Override "
            "via J1_RAGANYTHING_*_PROCESSOR or compile_callable=."
        )

    # Build the config with the per-run override, when supplied.
    # Materialise the directory before LightRAG opens it so the
    # storage backends don't bomb on the first ``open()``.
    if working_dir_override is not None:
        working_dir_override.mkdir(parents=True, exist_ok=True)
    config, dropped_overrides = _build_rag_config(
        raganything_mod, settings,
        config_overrides=config_overrides,
        working_dir_override=working_dir_override,
    )
    kwargs: dict[str, Any] = {}
    if config is not None:
        kwargs["config"] = config
    kwargs["llm_model_func"] = _make_text_callable(text_client)
    if embedding_client is not None:
        kwargs["embedding_func"] = _make_embedding_func(embedding_client)
    if vision_client is not None:
        kwargs["vision_model_func"] = _make_vision_callable(vision_client)

    try:
        return rag_cls(**kwargs), dropped_overrides
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


def _build_rag_config(
    raganything_mod, settings,
    *,
    config_overrides: dict[str, Any] | None = None,
    working_dir_override: Path | None = None,
):
    """Build a `RAGAnythingConfig` if the vendor exports it.

 Some versions of `raganything` accept `working_dir` directly on
 the constructor; in that case we skip the config object and pass
 a kwargs dict instead.

 `config_overrides` is the AssessmentPlan-derived per-capability
 flag dict from
 [`plan_mapper.CompileConfig.to_config_overrides`](./plan_mapper.py).
 Applied via `setattr` AFTER construction, and ONLY for fields
 the installed `RAGAnythingConfig` actually exposes — a vendor
 version that drops `enable_equation_processing` (say) silently
 drops that override rather than crashing the compile. The
 dropped fields are reported back so the bridge can surface
 them via `metadata["unhandled_capabilities"]`.

 Returns `(config_or_None, dropped_field_names)`.
 """
    cfg_cls = getattr(raganything_mod, "RAGAnythingConfig", None)
    if cfg_cls is None:
        return None, []
    # When the per-run override is supplied, it takes precedence over
    # ``settings.workdir``. Both paths are string-typed at the vendor
    # boundary; we always pass a string.
    working_dir_value = (
        str(working_dir_override)
        if working_dir_override is not None
        else settings.workdir
    )
    try:
        config = cfg_cls(
            working_dir=working_dir_value,
        )
    except TypeError:
        # `working_dir` keyword name varies; fall back to no config.
        return None, []
    dropped: list[str] = []
    for name, value in (config_overrides or {}).items():
        if hasattr(config, name):
            try:
                setattr(config, name, value)
            except (AttributeError, TypeError):
                # Field exists but isn't writable (frozen dataclass /
                # property without setter). Treat as not-applied.
                dropped.append(name)
        else:
            dropped.append(name)
    return config, dropped


# ---- LLM-callable adapters: J1 client → vendor-callable shape ------


def _make_text_callable(text_client) -> Callable[..., Any]:
    """RAGAnything (via LightRAG) `await`s
 ``llm_model_func(prompt, system_prompt=..., history_messages=[...], **kw)``
 → str.

 LightRAG awaits the result, so the wrapper has to be `async`.
 Underlying `TextLLMClient.generate` is synchronous, so we run it
 on a worker thread to keep the event loop free. Token usage is
 dropped at the vendor boundary — RAGAnything doesn't surface it.

 Critical contract: forward `system_prompt` to the underlying
 client AND fold `history_messages` into the user prompt. Two
 consequences flow from this:

 1. LightRAG's intended structured prompt (entity-extraction
 template + few-shot examples in `system_prompt` + the
 chunk content in `prompt`) reaches the model intact —
 dropping `system_prompt` was producing degenerate
 extractions.
 2. The token-budget pre-flight check at the OpenAI-compat
 boundary sees the WHOLE payload (system + history +
 user). Without this, an oversize prompt could slip past
 our budget check (which would only see the small user
 message) and trip LM Studio's HTTP-400 'Context size has
 been exceeded' for real.

 `history_messages` is folded into the user prompt as a labelled
 block rather than passed as an OpenAI `messages[]` array,
 because `TextLLMClient.generate` only takes
 `(prompt, system_prompt)` — the simplest correct path is to
 serialise history into the prompt string. The model still sees
 every turn; the budget check still estimates accurately.
 """

    async def _llm_callable(
        prompt: str,
        system_prompt: str | None = None,
        history_messages: list | None = None,
        *args,
        **kwargs,
    ) -> str:
        full_prompt = prompt
        if history_messages:
            history_text = "\n".join(
                f"{str(m.get('role', 'user')).upper()}: {m.get('content', '')}"
                for m in history_messages
                if isinstance(m, dict) and m.get("content")
            )
            if history_text:
                full_prompt = f"{history_text}\n\nUSER: {prompt}"
        # Inject `/no_think` into BOTH system + user prompts so qwen3
        # models skip their chain-of-thought reasoning block.
        # System-level placement is more authoritative for qwen3 than
        # user-level; we keep both as belt-and-suspenders. Without
        # this suppression, qwen3 emits up to ~2K reasoning tokens
        # BEFORE the visible answer; on local 2048-cap setups the
        # actual answer never arrives — `content=""` or content
        # without LightRAG's expected closing delimiter, leaving the
        # extractor with "0 Ent + 0 Rel" / "Complete delimiter can
        # not be found in extraction result". Models that don't
        # recognise `/no_think` (most non-qwen3 chat templates) just
        # treat it as a leading comment — safe to inject anywhere.
        no_think = "/no_think"
        if system_prompt:
            full_system = f"{no_think}\n\n{system_prompt}"
        else:
            full_system = no_think
        full_prompt = f"{no_think}\n\n{full_prompt}"
        text, _usage = await asyncio.to_thread(
            text_client.generate,
            full_prompt,
            system_prompt=full_system,
        )
        return text

    return _llm_callable


def _make_vision_callable(vision_client) -> Callable[..., Any]:
    """RAGAnything `await`s vision_model_func(prompt, image_data, **kw) → str.

 `image_data` arrives in one of two shapes depending on which call
 site inside raganything is firing:

 * **bytes** — when the caller has already read the file (the
 less common path).
 * **base64 string** — what
 `raganything.modalprocessors._encode_image_to_base64` returns,
 used by every multimodal-processor pass during compile. This
 is the dominant path; the value is the bare base64 ASCII (no
 `data:` prefix).

 Our `OpenAICompatVisionLLMClient.analyze_image` expects **bytes**
 so it can re-base64-encode them into a data URL. Calling it with a
 string blows up at `base64.b64encode("...")` with `a bytes-like
 object is required, not 'str'` — the exact error operators have
 been seeing in the worker log with no useful context. Decode the
 string back to bytes here so the client stays strict and the error
 message names the failing artifact.
 """

    async def _vision_callable(
        prompt: str,
        image_data: Any = b"",
        system_prompt: str | None = None,
        history_messages: list | None = None,
        *args,
        **kwargs,
    ) -> str:
        try:
            image_bytes = _coerce_image_bytes(image_data)
        except (ValueError, base64.binascii.Error) as exc:
            # Surface the artifact-level cause so multimodal-processing
            # failures are diagnosable without enabling DEBUG. Without
            # this the only signal in the worker log is RAGAnything's
            # generic `Error generating image description:`.
            shape = (
                f"str(len={len(image_data)})"
                if isinstance(image_data, str)
                else type(image_data).__name__
            )
            raise ValueError(
                f"vision_model_func received unsupported image_data "
                f"shape: {shape}. Expected raw bytes or a base64 string. "
                f"Underlying decode error: {exc}"
            ) from exc
        # Same fold-history-into-prompt strategy as the text
        # callable so the budget pre-flight sees the full content.
        # `analyze_image` doesn't currently accept a system_prompt
        # parameter so we prepend it onto the user prompt instead.
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n{full_prompt}"
        if history_messages:
            history_text = "\n".join(
                f"{str(m.get('role', 'user')).upper()}: {m.get('content', '')}"
                for m in history_messages
                if isinstance(m, dict) and m.get("content")
            )
            if history_text:
                full_prompt = f"{history_text}\n\nUSER: {full_prompt}"
        text, _usage = await asyncio.to_thread(
            vision_client.analyze_image, image_bytes, prompt=full_prompt,
        )
        return text

    return _vision_callable


def _coerce_image_bytes(image_data: Any) -> bytes:
    """Normalise `image_data` from RAGAnything into raw bytes.

 Accepts:
 * `bytes` / `bytearray` / `memoryview` → returned as-is.
 * `str` → assumed base64. Strips an optional `data:<mime>;base64,`
 prefix (some call sites pre-format the data URL) before
 decoding.
 Anything else raises `ValueError` so the caller logs a clear
 artifact-scoped error.
 """
    if isinstance(image_data, (bytes, bytearray, memoryview)):
        return bytes(image_data)
    if isinstance(image_data, str):
        payload = image_data
        prefix = "base64,"
        idx = payload.find(prefix)
        if idx != -1:
            payload = payload[idx + len(prefix):]
        return base64.b64decode(payload, validate=False)
    raise ValueError(
        f"image_data must be bytes or base64 string, got {type(image_data).__name__}"
    )


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
# we skip MinerU. 0.8 = at least 80 % of the first ≤5 pages carry a
# real text layer. Deliberately conservative: a mixed scan/text PDF
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
 profiling elsewhere). A page is counted as "has text" when
 `extract_text` yields ≥ 20 non-whitespace characters — this
 filters out PDFs that have only a few invisible copy-protection or
 watermark text nodes.

 Returns False (→ full MinerU path) when:
 * source is not a.pdf
 * pypdf is not importable
 * the file can't be read (corrupt, encrypted, etc.)
 * text ratio < threshold (scanned / image-heavy / formula-heavy)
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
    await _force_persist_chunks(rag)

    # Best-effort doc-status bookkeeping — keeps RAGAnything's view of
    # the document consistent with what `process_document_complete`
    # would have left behind. Failure here is non-fatal.
    mark = getattr(rag, "_mark_multimodal_processing_complete", None)
    if mark is not None:
        try:
            await mark(document_id)
        except Exception:  # noqa: BLE001 — bookkeeping must not fail compile
            _log.warning("doc-status bookkeeping failed (non-fatal)", exc_info=True)

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
 reliable embedded text. MinerU's `parse_method=auto` runs full layout
 analysis (layout blocks, OCR, formula/table detection) on every page
 regardless of content — for a normal 4-page text PDF this means several
 minutes of transformer inference on CPU/GPU with no benefit. pypdf text
 extraction completes in < 1 second for the same file.

 Caller must first confirm the document is text-extractable via
 `_is_text_extractable_pdf`. This function does NOT re-check — it
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
    await _force_persist_chunks(rag)

    # Best-effort doc-status bookkeeping.
    mark = getattr(rag, "_mark_multimodal_processing_complete", None)
    if mark is not None:
        try:
            await mark(document_id)
        except Exception:  # noqa: BLE001
            _log.warning("doc-status bookkeeping failed (non-fatal)", exc_info=True)

    # Persist the extracted text for the draft walker.
    out_path = output_dir / f"{document_id}.md"
    out_path.write_text(combined, encoding="utf-8")


async def _force_persist_chunks(rag) -> None:
    """Flush LightRAG's `text_chunks` + `doc_status` to disk
 unconditionally after a fast-path `ainsert`.

 LightRAG's `apipeline_process_enqueue_documents` writes chunks
 to in-memory storage in stage 1, then runs LLM-driven entity
 extraction in stage 2, and only calls `_insert_done` (which
 flushes EVERY storage to disk) on stage-2 success. If entity
 extraction fails — slow / unreachable LLM, prompt error, vector
 upsert race — the in-memory chunks are lost and the FE's
 Chunks tab stays empty even though parsing produced text.

 For txt/FAST-mode compiles we don't care whether entity
 extraction succeeded: the chunks are the deliverable. Force the
 flush via `index_done_callback` on the chunk storages so they
 persist regardless of upstream LLM status.

 Best-effort: any flush error logs + returns. Compile never
 fails because telemetry flushing didn't work."""
    storages = []
    lightrag = getattr(rag, "lightrag", None)
    if lightrag is None:
        return
    for name in ("text_chunks", "chunks_vdb", "full_docs", "doc_status"):
        storage = getattr(lightrag, name, None)
        if storage is None:
            continue
        callback = getattr(storage, "index_done_callback", None)
        if callback is None:
            continue
        storages.append((name, callback))
    for name, callback in storages:
        try:
            result = callback()
            # `index_done_callback` may be sync or async depending on
            # storage backend. Handle both shapes uniformly.
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:  # noqa: BLE001 — flush must not fail compile
            _log.warning(
                "force-flush of LightRAG storage %r failed (non-fatal): %s",
                name, exc,
            )


# ---- LibreOffice pre-conversion (broad format coverage) ------------
#
# RAGAnything / mineru parses PDF + modern OOXML (.docx/.xlsx/.pptx)
# + images natively. Legacy binary office formats (Word 97-2003.doc,
# Excel 97-2003.xls, PowerPoint 97-2003.ppt), OpenDocument
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
    storage_dir: Path,
    artifact_ids: Iterable[str],
    *,
    ctx: "ProjectContext | None" = None,
    document_id: str | None = None,
    run_id: str | None = None,
) -> list[ArtifactDraft]:
    """Collect graph-shaped artifacts (graph_chunk_entity_relation.json etc.).

 RAGAnything (via LightRAG) writes graph + entity relation files to
 its storage directory after processing. We surface those as
 `graph_json` drafts.

 ``ctx`` / ``document_id`` / ``run_id`` are stamped directly into
 each draft's ``metadata`` at creation time. This is the
 authoritative lineage stamping — earlier versions of this helper
 emitted drafts with empty metadata and relied on later
 registration to stamp ``run_id`` from the workflow's
 ``correlation_id``. That worked when orchestration was the only
 caller; the latest validation report flagged 7 graph_json rows
 with ``run_id=None`` because at least one producer path was
 reaching the registry WITHOUT a correlation_id. Stamping at the
 draft layer makes the lineage independent of which registration
 path is used.

 ``source_document_ids`` is set to ``[document_id]`` when known —
 that's the document binding the document-scoped invalidation
 sweep matches on. ``source_artifact_ids`` is intentionally LEFT
 EMPTY: earlier versions stamped the full upstream
 ``request.artifact_ids`` list (chunks + manifest + raw +
 everything compile produced), which broke the validator's
 chunk-grounding check (most upstream ids aren't chunks, so
 every graph artifact stranded ~40 ids and the run failed
 validation). The cross-run leakage prevention that
 ``source_artifact_ids`` would have provided is now handled by
 the per-artifact run scope check (``metadata.run_id``), which
 is more reliable.

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
    # `artifact_ids` is kept on the signature for adapter-future
    # use (per-chunk lineage stamping) but not used today — see the
    # ``source_artifact_ids=[]`` rationale below.
    _ = artifact_ids
    # Common LightRAG / RAGAnything graph filenames; pattern-match
    # against any of them.
    interesting = (
        "*graph*", "*relation*", "*entit*", "kv_store*.json",
    )
    # Filename SUBSTRINGS that mark a file as non-graph. Mirrors the
    # FE projector's `_INTERNAL_KV_PATTERNS` so producer + projector
    # agree on what counts as graph data:
    #  * `kv_store_text_chunk*` — chunk text. Already emitted as
    #    `kind="chunk"` via `_chunk_drafts_from_storage`.
    #  * `kv_store_doc_status*` — per-document state machine
    #    (PENDING/HANDLING/PROCESSED/FAILED + duplicate-detection).
    #    Surfacing it as `graph_json` puts `[DUPLICATE] Original
    #    document` rows in the Knowledge Graph tab.
    #  * `kv_store_full_doc*` — full document store. Not graph data.
    #  * `kv_store_llm*` — LLM response cache. Not graph data, and
    #    historically contained zero-node payloads that crashed the
    #    graph stage validator.
    #  * `kv_store_chunk_entity*` — internal LightRAG bookkeeping;
    #    its "entities" are pre-aggregation cache keys, not the
    #    operator-facing entity surface.
    #
    # Substring matching (not exact filename) catches versioned
    # variants like `kv_store_llm_response_cache.v2.json`.
    _non_graph_substrings = (
        "kv_store_text_chunk", "kv_store_doc_status",
        "kv_store_full_doc", "kv_store_llm", "kv_store_chunk_entity",
    )
    seen: set[Path] = set()
    for pattern in interesting:
        for path in storage_dir.rglob(pattern):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            lname = path.name.lower()
            if any(sub in lname for sub in _non_graph_substrings):
                continue
            try:
                content = path.read_bytes()
            except OSError:
                continue
            if not content:
                continue
            # Lineage stamping at the draft layer. We always emit
            # the document binding when known (so document-scoped
            # sweeps + retrieval scope checks can find this
            # artifact); we always emit the run_id when known (so
            # ``RunScope`` filtering works without falling back to
            # ``metadata.run_id == ""``). The orchestration layer's
            # ``_enforce_lineage_or_raise`` gate fires AFTER this
            # function — if ``run_id`` here is None, registration
            # raises; we deliberately do NOT silently drop the
            # draft, because that hides a producer-side bug.
            stamped_metadata: dict[str, Any] = {
                "filename": path.name,
                "relative_path": str(path.relative_to(storage_dir)),
            }
            if run_id:
                stamped_metadata["run_id"] = run_id
            if document_id:
                stamped_metadata["document_id"] = document_id
            if ctx is not None:
                tenant_id = getattr(ctx, "tenant_id", None)
                project_id = getattr(ctx, "project_id", None)
                if tenant_id:
                    stamped_metadata["tenant_id"] = tenant_id
                if project_id:
                    stamped_metadata["project_id"] = project_id
            source_document_ids = (
                [document_id] if document_id else []
            )
            drafts.append(ArtifactDraft(
                kind=ARTIFACT_KIND_GRAPH_JSON,
                content=content,
                suggested_extension=path.suffix or ".json",
                source_document_ids=source_document_ids,
                # Intentionally empty — see docstring. The
                # chunk-grounding validator iterates this list and
                # expects every entry to be a chunk artifact; mixing
                # chunk + non-chunk ids strands them. Cross-run
                # leakage prevention falls back to the
                # ``metadata.run_id`` scope check (set just above).
                source_artifact_ids=[],
                metadata=stamped_metadata,
            ))
    return drafts


def _force_clear_doc_status_for_id(
    file_name: str, doc_id: str,
) -> None:
    """Rewrite `kv_store_doc_status.json` to drop `doc_id` and any
 `dup-*` records that reference it.

 Bulletproof path for the "Duplicate document detected" symptom
 that survives `adelete_by_doc_id + delete + index_done_callback`.
 LightRAG's in-memory state can lag behind disk (or vice-versa)
 when storage is shared across processes; rewriting the file
 directly removes any possibility that stale state leaks back.

 Best-effort: file missing, malformed JSON, or write errors all
 silently no-op so the caller proceeds with the regular cleanup
 path. If both fail, the worst case is the original "Duplicate
 document detected" symptom — same as before this helper existed.
 """
    import json as _json
    import os as _os

    if not file_name or not _os.path.exists(file_name):
        return
    try:
        with open(file_name, "r", encoding="utf-8") as fh:
            data = _json.load(fh)
    except (OSError, _json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    cleaned = {
        k: v for k, v in data.items()
        if k != doc_id
        and not (
            isinstance(k, str) and k.startswith("dup-")
            and isinstance(v, dict)
            and v.get("metadata", {}).get("original_doc_id") == doc_id
        )
    }
    if cleaned == data:
        return
    # Atomic-ish write: write to temp, then rename. Avoids leaving a
    # half-written file if the process dies mid-write.
    tmp = file_name + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            _json.dump(cleaned, fh, ensure_ascii=False)
        _os.replace(tmp, file_name)
    except OSError:
        try:
            _os.unlink(tmp)
        except OSError:
            pass


def _detect_lightrag_doc_failure(
    storage_dir: Path, *, document_id: str,
) -> str | None:
    """Return the LightRAG error message for `document_id` when the
 KV doc-status file marks it as failed; `None` otherwise.

 LightRAG writes per-document outcomes to `kv_store_doc_status.json`
 keyed by document id. A `status == "failed"` entry means the
 pipeline aborted internally (most commonly embedding dimension
 mismatch on the first vector upsert). The shape we care about is:

 { "<document_id>": { "status": "failed",
 "error_msg": "<reason>",... } }

 Best-effort: missing file, malformed JSON, or no entry for our
 document id returns `None` so callers fall through to normal
 success handling. The caller decides what to do with a returned
 error — `default_compile` turns it into a FAILED ArtifactProcessingResult
 so the workflow's required-step contract surfaces the real cause."""
    if not storage_dir.exists():
        return None
    status_path: Path | None = None
    for path in storage_dir.rglob("*"):
        if path.is_file() and path.name.lower() == "kv_store_doc_status.json":
            status_path = path
            break
    if status_path is None:
        return None
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    entry = payload.get(document_id)
    if not isinstance(entry, dict):
        return None
    status = str(entry.get("status") or "").lower()
    if status != "failed":
        return None
    error_msg = str(entry.get("error_msg") or "").strip()
    return error_msg or "LightRAG reported the document as failed without a message."


def _chunk_drafts_from_storage(
    storage_dir: Path,
    *,
    document_id: str,
    doc_id: str | None = None,
) -> list[ArtifactDraft]:
    """Project LightRAG's `kv_store_text_chunks.json` into canonical
 `kind="chunk"` ArtifactDrafts — one per chunk entry **belonging
 to the document we just inserted**.

 LightRAG's storage is shared across all documents in the workdir
 (`kv_store_text_chunks.json` is a top-level dict of `chunk_id →
 {tokens, content, full_doc_id, chunk_order_index, file_path}`).
 Without filtering, every prior insert's chunks would surface
 under the current run — exactly the "Chunks tab shows another
 file" bug operators hit when they re-run after a failed insert.

 Filter rule: `entry.full_doc_id == doc_id` (LightRAG's id, which
 we pass through verbatim from `process_document_complete`).
 When `doc_id` is None we fall back to including every chunk —
 legacy callers + tests that don't pre-resolve the LightRAG doc
 id keep working unchanged. The production compile path always
 passes `doc_id` so cross-document leakage is impossible.

 Without this projector, the Chunks tab is correctly empty for
 every run (no producer in the dev stack emitted `kind="chunk"`)
 — even though LightRAG had the chunks on disk all along.
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
        # Doc-scoped filter: only include chunks whose `full_doc_id`
        # matches the document we just inserted. Without this, the
        # shared LightRAG workdir leaks chunks across documents.
        if doc_id is not None:
            chunk_full_doc_id = entry.get("full_doc_id")
            if chunk_full_doc_id != doc_id:
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
            kind=ARTIFACT_KIND_CHUNK,
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
    # `items` carries the canonical per-element list (text /
    # table / image / equation / heading) the FE Content Inventory
    # tab renders. Without these the tab can only display summary
    # counts; the user sees an empty items table.
    items: list[dict[str, Any]] = []
    text_block_count_from_list = 0
    if content_list_path is not None:
        try:
            payload = json.loads(content_list_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, list):
            for idx, item in enumerate(payload):
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type", "")).lower()
                page_idx = item.get("page_idx")
                if isinstance(page_idx, int):
                    page_indices.add(page_idx)

                normalised_type, item_entry = _normalise_content_list_item(
                    raw=item,
                    raw_type=item_type,
                    idx=idx,
                    page_idx=page_idx,
                )
                if item_entry is not None:
                    items.append(item_entry)

                if item_type in ("image", "img"):
                    images_from_list.append(item)
                elif item_type == "table":
                    table_count += 1
                elif item_type in ("equation", "formula"):
                    equation_count += 1
                elif item_type == "text":
                    text_block_count_from_list += 1

    # If the parser surfaced text blocks via the content_list but
    # didn't write per-block files, prefer the content_list count —
    # filename walks miss inline text-only documents.
    if text_block_count_from_list and not text_block_count:
        text_block_count = text_block_count_from_list

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
        # Canonical per-element list. Empty when the parser produced
        # no content_list.json — the FE renders the unavailable /
        # empty state in that case.
        "items": items,
    }
    if page_count is not None:
        manifest["page_count"] = page_count
    return manifest


# Per-block preview cap for the Content Inventory items list. Long
# text bodies + many blocks would balloon the artifact size. 280
# chars is enough for a useful preview; the FE truncates further at
# render time.
_MAX_ITEM_PREVIEW_CHARS = 280


def _normalise_content_list_item(
    *,
    raw: dict[str, Any],
    raw_type: str,
    idx: int,
    page_idx: int | None,
) -> tuple[str, dict[str, Any] | None]:
    """Project one MinerU `content_list` entry into the canonical
 `ParsedContentItem` shape.

 Returns `(normalised_type, item_entry_dict)`. `item_entry` is
 None when the entry should be dropped (empty / unrecognised
 type).
 """
    type_map: dict[str, str] = {
        "text": "text",
        "paragraph": "text",
        "title": "heading",
        "heading": "heading",
        "h1": "heading",
        "h2": "heading",
        "h3": "heading",
        "table": "table",
        "image": "image",
        "img": "image",
        "figure": "image",
        "equation": "formula",
        "formula": "formula",
    }
    normalised = type_map.get(raw_type, "other")

    text = raw.get("text") or raw.get("content") or ""
    caption = raw.get("img_caption") or raw.get("table_caption") or raw.get("caption")
    if isinstance(caption, list):
        caption = " ".join(str(c) for c in caption if c)
    elif caption is None:
        caption = ""
    if not isinstance(text, str):
        text = str(text)
    if not isinstance(caption, str):
        caption = str(caption)

    preview_source = text or caption
    preview = preview_source[:_MAX_ITEM_PREVIEW_CHARS]
    if len(preview_source) > _MAX_ITEM_PREVIEW_CHARS:
        preview = preview.rstrip() + "…"

    if not preview and normalised in ("text", "heading", "formula"):
        # No usable content for a text-shaped item — drop it
        # rather than emit a blank row.
        return normalised, None

    item_id = str(raw.get("item_id") or raw.get("id") or f"item-{idx:04d}")
    location = raw.get("img_path") or raw.get("source_path")
    metadata: dict[str, Any] = {
        "raw_type": raw_type or "unknown",
    }
    # Optional structured fields the FE / future enrichers can read.
    for key in ("text_level", "img_path", "row_count", "column_count"):
        if key in raw and raw[key] is not None:
            metadata[key] = raw[key]

    return normalised, {
        "item_id": item_id,
        "type": normalised,
        "page_idx": page_idx if isinstance(page_idx, int) else None,
        "source_path": str(location) if location else None,
        "text_preview": preview if preview else None,
        "caption": caption if caption else None,
        "metadata": metadata,
    }


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
 * `skip` — almost certainly decorative; don't burn vision LLM
 * `enrich` — likely semantic content; run the full vision pass
 * `triage` — uncertain; let a cheap vision pass classify it
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
