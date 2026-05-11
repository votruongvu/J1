"""RAGAnything provider settings.

Mirrors the shape of every other `j1.*.settings` module — frozen
dataclass + env-driven loader. Honours the `J1_*` convention.

The three `*_processor` fields are import strings (e.g.
``"mypkg.processors:compile_doc"``) that name a deployment-supplied
callable. When set, the adapter resolves the callable via the safe
class-loader and uses it instead of the built-in stub. This is the
recommended way to wire RAGAnything against a real
`raganything` install.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass

ENV_RAGANYTHING_MODE = "J1_RAGANYTHING_MODE"
ENV_RAGANYTHING_WORKDIR = "J1_RAGANYTHING_WORKDIR"
ENV_RAGANYTHING_STORAGE_DIR = "J1_RAGANYTHING_STORAGE_DIR"
ENV_RAGANYTHING_CACHE_DIR = "J1_RAGANYTHING_CACHE_DIR"
ENV_RAGANYTHING_PARSE_METHOD = "J1_RAGANYTHING_PARSE_METHOD"
ENV_RAGANYTHING_BACKEND = "J1_RAGANYTHING_BACKEND"
ENV_RAGANYTHING_COMPILER = "J1_RAGANYTHING_COMPILER_PROCESSOR"
ENV_RAGANYTHING_GRAPH = "J1_RAGANYTHING_GRAPH_PROCESSOR"
ENV_RAGANYTHING_RETRIEVAL = "J1_RAGANYTHING_RETRIEVAL_PROCESSOR"
ENV_RAGANYTHING_PDF_CONVERT_EXTENSIONS = "J1_RAGANYTHING_PDF_CONVERT_EXTENSIONS"
ENV_RAGANYTHING_LIBREOFFICE_BINARY = "J1_RAGANYTHING_LIBREOFFICE_BINARY"
ENV_RAGANYTHING_LIBREOFFICE_TIMEOUT = "J1_RAGANYTHING_LIBREOFFICE_TIMEOUT_SECONDS"
# Adapter capability flags. Consumed by
# [`plan_mapper.map_assessment_to_raganything_config`](./plan_mapper.py)
# when an `AssessmentPlan` requires a per-capability toggle. When a
# capability is marked unsupported AND the plan requires it, the
# mapper records a warning (default `degrade_with_warning` policy)
# or raises (`fail` policy). Default True everywhere so existing
# deployments keep working unchanged.
ENV_RAGANYTHING_SUPPORTS_IMAGE = "J1_RAGANYTHING_SUPPORTS_IMAGE"
ENV_RAGANYTHING_SUPPORTS_TABLE = "J1_RAGANYTHING_SUPPORTS_TABLE"
ENV_RAGANYTHING_SUPPORTS_EQUATION = "J1_RAGANYTHING_SUPPORTS_EQUATION"
# Allow-list narrowing the plan mapper's parse_method choice. Empty
# (the default) means "every value MinerU's CLI accepts is permitted"
# — the plan picks freely. Set to e.g.
# `J1_RAGANYTHING_ALLOWED_PARSE_METHODS=auto,txt` to lock OCR out of
# a deployment that doesn't have the OCR backend wired.
ENV_RAGANYTHING_ALLOWED_PARSE_METHODS = "J1_RAGANYTHING_ALLOWED_PARSE_METHODS"

# VLM HTTP client configuration. Consumed by MinerU when
# `parse_method=vlm-http-client` — MinerU reads `MINERU_VL_SERVER` /
# `MINERU_VL_API_KEY` / `MINERU_VL_MODEL_NAME` directly. The bridge
# applies these env vars from settings at compile time so the
# operator doesn't need to set both J1 and MinerU env vars by hand.
#
# Defaults: fall back to the project-wide `J1_VISION_LLM_*` config
# the operator already wired for the rest of the stack — that way
# turning on `vlm-http-client` requires ONE env change
# (J1_RAGANYTHING_PARSE_METHOD), not three.
ENV_RAGANYTHING_VLM_HTTP_SERVER_URL = "J1_RAGANYTHING_VLM_HTTP_SERVER_URL"
ENV_RAGANYTHING_VLM_HTTP_API_KEY = "J1_RAGANYTHING_VLM_HTTP_API_KEY"
ENV_RAGANYTHING_VLM_HTTP_MODEL_NAME = "J1_RAGANYTHING_VLM_HTTP_MODEL_NAME"
# Cap on parallel VLM requests MinerU will fire against the
# externally-managed VLM endpoint. MinerU's default fans out N
# requests in parallel via `asyncio.gather`; under load this can
# OOM / crash the upstream model (we've observed llama.cpp's
# `decode: failed to find a memory slot for batch of size N`
# under the default fanout). Default 1 = strictly serial — safest
# for self-hosted single-process VLM servers (LM Studio / single
# llama-server). Operators with a horizontally-scaled VLM endpoint
# can raise to whatever the cluster can serve concurrently.
ENV_RAGANYTHING_VLM_HTTP_MAX_CONCURRENCY = "J1_RAGANYTHING_VLM_HTTP_MAX_CONCURRENCY"
# Project-wide vision LLM env vars used as fallbacks. We deliberately
# read these from the env at load-time rather than importing the
# `j1.llm.*` modules to keep the provider self-contained — the LLM
# layer doesn't depend on raganything, and the reverse must hold.
ENV_J1_VISION_LLM_BASE_URL = "J1_VISION_LLM_BASE_URL"
ENV_J1_VISION_LLM_API_KEY = "J1_VISION_LLM_API_KEY"
ENV_J1_VISION_LLM_MODEL = "J1_VISION_LLM_MODEL"

DEFAULT_MODE = "local"
DEFAULT_WORKDIR = "./data/raganything"
DEFAULT_PARSE_METHOD = "auto"
# J1 runs MinerU as an HTTP client only — local-model backends are
# rejected at startup (see `EXTERNAL_ONLY_BACKENDS` + the policy
# checks in `_validate_backend` and `load_raganything_settings`).
# `vlm-http-client` offloads VLM inference to an externally-managed
# OpenAI-compatible endpoint we control (LM Studio / vLLM / hosted),
# so MinerU never downloads multi-gigabyte weights inside the J1
# container. Operators who genuinely need local models must fork
# the deployment and revert this default + the policy guard.
DEFAULT_BACKEND: str = "vlm-http-client"
DEFAULT_LIBREOFFICE_BINARY = "soffice"
DEFAULT_LIBREOFFICE_TIMEOUT = 120.0  # seconds — soffice can be slow on first launch

# Valid values for `parse_method` per MinerU's CLI choices. Validated
# at settings-load time so a misuse of `parse_method=vlm-http-client`
# (which actually belongs in `backend`) fails at startup with a
# clear message instead of mid-compile with a cryptic mineru error.
VALID_PARSE_METHODS = frozenset({"auto", "txt", "ocr"})

# Valid values for `backend` per MinerU's CLI choices. The default
# (None) lets MinerU pick its own default — currently
# `hybrid-auto-engine` for parse_method=auto on local-CPU/GPU. Set
# to `vlm-http-client` to offload VLM inference to an OpenAI-compat
# endpoint (LM Studio / vLLM / hosted), which is dramatically
# faster on machines without GPU passthrough.
VALID_BACKENDS = frozenset({
    "pipeline",
    "vlm-http-client",
    "hybrid-http-client",
    "vlm-auto-engine",
    "hybrid-auto-engine",
})

# Subset of `VALID_BACKENDS` that J1 actually permits at startup.
# Anything outside this set runs MinerU's local model code path
# (model downloads, in-process inference, optional GPU usage) — none
# of which J1 is willing to manage. Operators who need a local
# backend must fork the deployment and remove this guard explicitly.
EXTERNAL_ONLY_BACKENDS = frozenset({
    "vlm-http-client",
    "hybrid-http-client",
})

# Mapping from rejected (local-model) backend names → the
# closest externally-hosted equivalent. Surfaced in the rejection
# error message so operators get an actionable migration path.
_LOCAL_BACKEND_REPLACEMENTS = {
    "pipeline": "vlm-http-client",
    "vlm-auto-engine": "vlm-http-client",
    "hybrid-auto-engine": "hybrid-http-client",
}

# Document formats RAGAnything / mineru cannot parse natively (the
# pure-Python parsers it ships handle modern OOXML + PDF + images,
# but not the legacy binary office formats or several open / vendor
# alternatives). For these, the bridge pre-converts to PDF via
# `soffice --headless --convert-to pdf` and feeds the result to
# raganything.
#
# Conservative default — covers the common "Word 97 / Excel 97 /
# PowerPoint 97 / OpenDocument / RTF / iWork" gap. Add or remove
# extensions via `J1_RAGANYTHING_PDF_CONVERT_EXTENSIONS`. Set to an
# empty value to disable conversion entirely.
DEFAULT_PDF_CONVERT_EXTENSIONS: tuple[str, ...] = (
    # Microsoft Office 97-2003 binary formats (raganything's parsers
    # don't reach these; they require LibreOffice / antiword / similar).
    ".doc", ".xls", ".ppt",
    # Rich Text — broadly supported by LibreOffice; raganything's
    # `python-docx` doesn't accept it.
    ".rtf",
    # OpenDocument family — LibreOffice's native formats; raganything
    # has no OOO/ODF parser.
    ".odt", ".ods", ".odp",
    # Apple iWork — LibreOffice has limited but functional support.
    ".pages", ".numbers", ".key",
    # Microsoft Works — legacy.
    ".wps",
)


@dataclass(frozen=True)
class RAGAnythingSettings:
    """Configuration the RAGAnything adapters need to bootstrap.

 `mode` is currently informational ("local" / "service") — the
 adapter inspects it to decide whether to spin up an in-process
 pipeline vs. talk to a service. Today only "local" is wired.

 `*_processor` fields each name an importable callable. When set,
 the matching adapter delegates to it — turning "you must subclass
 the adapter" into "you can wire it via env". The callable
 receives the `RAGAnything*Request` value object documented next
 to each adapter and returns the canonical `ArtifactProcessingResult`
 / `QueryResult`.
 """

    mode: str = DEFAULT_MODE
    workdir: str = DEFAULT_WORKDIR
    storage_dir: str | None = None
    cache_dir: str | None = None
    # MinerU `--method` (-m) value: one of `auto` / `txt` / `ocr`.
    # Selects the **PDF-parsing strategy**, NOT the inference engine.
    # The engine is selected via `backend` below. Setting this to
    # `vlm-http-client` (a backend value) is a misuse caught at
    # load time — see `_validate_parse_method`.
    parse_method: str = DEFAULT_PARSE_METHOD
    # MinerU `--backend` (-b) value: one of `pipeline` /
    # `vlm-http-client` / `hybrid-http-client` / `vlm-auto-engine` /
    # `hybrid-auto-engine`, or None to let MinerU pick the default.
    # Set to `vlm-http-client` for CPU-only deployments to offload
    # VLM inference to an OpenAI-compat endpoint — combine with the
    # `vlm_http_*` fields below (or the `J1_VISION_LLM_*` fallback).
    backend: str | None = DEFAULT_BACKEND
    compiler_processor: str | None = None
    graph_processor: str | None = None
    retrieval_processor: str | None = None
    # File extensions (lowercase, including the leading dot) for which
    # the bridge will pre-convert to PDF via LibreOffice before handing
    # the document to raganything. Leave empty (set
    # `J1_RAGANYTHING_PDF_CONVERT_EXTENSIONS=`) to disable conversion.
    pdf_convert_extensions: tuple[str, ...] = DEFAULT_PDF_CONVERT_EXTENSIONS
    # LibreOffice headless binary name or absolute path. Default
    # "soffice"; some distros use "libreoffice" as the user-facing
    # symlink. The bridge resolves via `shutil.which`.
    libreoffice_binary: str = DEFAULT_LIBREOFFICE_BINARY
    # Per-conversion timeout (seconds). LibreOffice can be slow on
    # first launch (font cache rebuild) — keep generous.
    libreoffice_timeout_seconds: float = DEFAULT_LIBREOFFICE_TIMEOUT
    # VLM HTTP client wiring. Only consulted when
    # `parse_method=vlm-http-client`. None on any field means "let
    # MinerU's auto-detection do its thing" — typically that means
    # `MINERU_VL_MODEL_NAME` is None and MinerU pulls the first
    # registered model from `<server>/v1/models`. The bridge skips
    # applying any env var that's None at compile time, so leaving
    # these unset preserves the legacy behaviour for callers that
    # were already exporting `MINERU_VL_*` themselves.
    vlm_http_server_url: str | None = None
    vlm_http_api_key: str | None = None
    vlm_http_model_name: str | None = None
    # Cap on parallel VLM requests MinerU dispatches per compile.
    # Default 1 = strictly serial; raises only when the operator has
    # a VLM endpoint that can actually serve N parallel requests.
    # Propagated to MinerU as `MINERU_VL_MAX_CONCURRENCY` at compile
    # time by the bridge's `_apply_vlm_http_client_env`. See the env
    # constant comment above for the full rationale.
    vlm_http_max_concurrency: int = 1
    # Adapter capability advertisements. Consumed by the
    # AssessmentPlan → compile-config mapper to decide whether a
    # required capability can be honoured by THIS deployment. Default
    # True because RAGAnything's stock pipeline supports all three
    # at the document-output level (the `to_mineru_kwargs` contract
    # docstring explains that these are advisory at the moment —
    # MinerU's top-level API doesn't expose per-capability switches,
    # so the mapper can only USE them to gate "would the plan fail
    # vs. degrade" decisions).
    supports_image: bool = True
    supports_table: bool = True
    supports_equation: bool = True
    # Empty tuple = no restriction (every method MinerU accepts is
    # allowed). When non-empty, the mapper degrades plan-requested
    # methods that aren't in the list back to the deployment default.
    # Drives the env-as-safety-hatch story: operators can lock out
    # OCR / txt without forking the planner.
    allowed_parse_methods: tuple[str, ...] = ()


def load_raganything_settings(
    env: Mapping[str, str] | None = None,
) -> RAGAnythingSettings:
    source = env if env is not None else os.environ
    workdir = source.get(ENV_RAGANYTHING_WORKDIR, DEFAULT_WORKDIR)
    parse_method = _validate_parse_method(
        source.get(ENV_RAGANYTHING_PARSE_METHOD, DEFAULT_PARSE_METHOD),
    )
    backend = _validate_backend(source.get(ENV_RAGANYTHING_BACKEND))
    # Server URL is required because every allowed backend
    # (`vlm-http-client`, `hybrid-http-client`) is HTTP-only — without
    # a URL, MinerU has nowhere to send the request. Fail loudly at
    # load time rather than mid-compile.
    vlm_server_url = (
        source.get(ENV_RAGANYTHING_VLM_HTTP_SERVER_URL)
        or source.get(ENV_J1_VISION_LLM_BASE_URL)
        or None
    )
    if vlm_server_url is None or not vlm_server_url.strip():
        from j1.errors.exceptions import ConfigError
        raise ConfigError(
            f"MinerU is configured for HTTP-client backend "
            f"({backend}) but no VLM server URL is set. Set "
            f"{ENV_RAGANYTHING_VLM_HTTP_SERVER_URL} (or the "
            f"project-wide {ENV_J1_VISION_LLM_BASE_URL} fallback) to "
            "the externally-managed VLM endpoint J1 should call. "
            "Local-model backends are not permitted — see "
            "docs/raganything-vlm-setup.md."
        )
    return RAGAnythingSettings(
        mode=source.get(ENV_RAGANYTHING_MODE, DEFAULT_MODE),
        workdir=workdir,
        # Default to workdir itself — LightRAG writes
        # `kv_store_*.json` and graph artifacts directly into
        # `working_dir`, NOT into a `storage` subdirectory. The
        # extractors (`_chunk_drafts_from_storage`,
        # `_graph_drafts_from_storage`) use `rglob`, so a deeper
        # path supplied explicitly via `J1_RAGANYTHING_STORAGE_DIR`
        # still works — but the default must point where LightRAG
        # actually writes, otherwise the FE Chunks tab stays empty
        # because no `kv_store_text_chunks.json` is found.
        storage_dir=source.get(ENV_RAGANYTHING_STORAGE_DIR) or workdir,
        cache_dir=source.get(ENV_RAGANYTHING_CACHE_DIR)
        or f"{workdir.rstrip('/')}/cache",
        parse_method=parse_method,
        backend=backend,
        compiler_processor=source.get(ENV_RAGANYTHING_COMPILER) or None,
        graph_processor=source.get(ENV_RAGANYTHING_GRAPH) or None,
        retrieval_processor=source.get(ENV_RAGANYTHING_RETRIEVAL) or None,
        pdf_convert_extensions=_parse_extensions(
            source.get(ENV_RAGANYTHING_PDF_CONVERT_EXTENSIONS),
        ),
        libreoffice_binary=(
            source.get(ENV_RAGANYTHING_LIBREOFFICE_BINARY)
            or DEFAULT_LIBREOFFICE_BINARY
        ),
        libreoffice_timeout_seconds=_parse_timeout(
            source.get(ENV_RAGANYTHING_LIBREOFFICE_TIMEOUT),
        ),
        # Already resolved + presence-validated above.
        vlm_http_server_url=vlm_server_url,
        vlm_http_api_key=(
            source.get(ENV_RAGANYTHING_VLM_HTTP_API_KEY)
            or source.get(ENV_J1_VISION_LLM_API_KEY)
            or None
        ),
        vlm_http_model_name=(
            source.get(ENV_RAGANYTHING_VLM_HTTP_MODEL_NAME)
            or source.get(ENV_J1_VISION_LLM_MODEL)
            or None
        ),
        vlm_http_max_concurrency=_parse_max_concurrency(
            source.get(ENV_RAGANYTHING_VLM_HTTP_MAX_CONCURRENCY),
        ),
        supports_image=_parse_bool(
            source.get(ENV_RAGANYTHING_SUPPORTS_IMAGE), default=True,
        ),
        supports_table=_parse_bool(
            source.get(ENV_RAGANYTHING_SUPPORTS_TABLE), default=True,
        ),
        supports_equation=_parse_bool(
            source.get(ENV_RAGANYTHING_SUPPORTS_EQUATION), default=True,
        ),
        allowed_parse_methods=_parse_allowed_methods(
            source.get(ENV_RAGANYTHING_ALLOWED_PARSE_METHODS),
        ),
    )


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    """Standard env-var bool parsing. `None` / empty → default."""
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return default
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    return default


def _parse_allowed_methods(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated parse-method allow-list. Validates
 each entry against `VALID_PARSE_METHODS` and drops unknowns
 (with no error — operator typos shouldn't kill startup; the
 mapper's safety net catches the empty-allow-list case)."""
    if not raw:
        return ()
    cleaned = [
        m.strip().lower() for m in raw.split(",")
        if m.strip()
    ]
    return tuple(m for m in cleaned if m in VALID_PARSE_METHODS)


def _parse_max_concurrency(raw: str | None) -> int:
    """Parse the VLM-max-concurrency env var. Defaults / clamps to 1
 on missing / invalid / non-positive input — never crash startup
 on a typo, never let an operator accidentally enable parallel
 fanout via a malformed value."""
    if raw is None or not raw.strip():
        return 1
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(value, 1)


def _parse_extensions(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated extension list into a normalised tuple.

 `None` → bundled default set.
 `""` / whitespace → empty (conversion disabled).
 Otherwise → each comma-separated entry, lowercased, with
 a leading dot ensured.
 """
    if raw is None:
        return DEFAULT_PDF_CONVERT_EXTENSIONS
    cleaned = [e.strip() for e in raw.split(",") if e.strip()]
    if not cleaned:
        return ()
    return tuple(
        ext.lower() if ext.startswith(".") else f".{ext.lower()}"
        for ext in cleaned
    )


def _parse_timeout(raw: str | None) -> float:
    if not raw:
        return DEFAULT_LIBREOFFICE_TIMEOUT
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{ENV_RAGANYTHING_LIBREOFFICE_TIMEOUT} must be a number, got {raw!r}"
        ) from exc
    if value <= 0:
        raise ValueError(
            f"{ENV_RAGANYTHING_LIBREOFFICE_TIMEOUT} must be > 0, got {value}"
        )
    return value


def _validate_parse_method(raw: str | None) -> str:
    """Reject the common misuse where an operator sets
 `J1_RAGANYTHING_PARSE_METHOD=vlm-http-client` (a backend value).

 Surface a clear migration message at startup instead of letting
 mineru fail mid-compile with the cryptic 'Invalid value for -m'
 error. Backend values are forwarded through `J1_RAGANYTHING_BACKEND`
 instead — that's the correct mineru flag for engine selection.
 """
    if not raw:
        return DEFAULT_PARSE_METHOD
    value = raw.strip()
    if value in VALID_PARSE_METHODS:
        return value
    if value in VALID_BACKENDS:
        raise ValueError(
            f"{ENV_RAGANYTHING_PARSE_METHOD}={value!r} is a BACKEND value, "
            f"not a parse method. Move it to "
            f"{ENV_RAGANYTHING_BACKEND}={value!r} and set "
            f"{ENV_RAGANYTHING_PARSE_METHOD}=auto (or txt/ocr)."
        )
    raise ValueError(
        f"{ENV_RAGANYTHING_PARSE_METHOD}={value!r} is invalid; "
        f"expected one of {sorted(VALID_PARSE_METHODS)}."
    )


def _validate_backend(raw: str | None) -> str:
    """Validate the backend env var against the J1-allowed subset of
 MinerU's CLI choices.

 Empty / unset → `DEFAULT_BACKEND` (`vlm-http-client`). Anything
 else must match `EXTERNAL_ONLY_BACKENDS` — the local-model
 backends (`pipeline` / `vlm-auto-engine` / `hybrid-auto-engine`)
 are rejected at startup with an actionable migration message
 pointing at the closest HTTP-client equivalent.

 The rejection is by design: J1 doesn't host MinerU's models in-
 process. Local-model backends would trigger multi-gigabyte HF
 downloads and inference on the worker — neither of which the
 deployment is configured to handle. Operators who need local
 inference must fork the deployment and remove this guard."""
    if not raw:
        return DEFAULT_BACKEND
    value = raw.strip()
    if not value:
        return DEFAULT_BACKEND
    if value not in VALID_BACKENDS:
        from j1.errors.exceptions import ConfigError
        raise ConfigError(
            f"{ENV_RAGANYTHING_BACKEND}={value!r} is invalid; "
            f"expected one of {sorted(VALID_BACKENDS)}."
        )
    if value not in EXTERNAL_ONLY_BACKENDS:
        replacement = _LOCAL_BACKEND_REPLACEMENTS.get(value, "vlm-http-client")
        from j1.errors.exceptions import ConfigError
        raise ConfigError(
            f"{ENV_RAGANYTHING_BACKEND}={value!r} runs MinerU's local "
            "model code path (model downloads, in-process inference). "
            "J1 only permits the HTTP-client backends "
            f"({sorted(EXTERNAL_ONLY_BACKENDS)}). "
            f"Switch to {ENV_RAGANYTHING_BACKEND}={replacement!r} and "
            f"point {ENV_RAGANYTHING_VLM_HTTP_SERVER_URL} at your "
            "externally-hosted VLM endpoint."
        )
    return value
