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
ENV_RAGANYTHING_COMPILER = "J1_RAGANYTHING_COMPILER_PROCESSOR"
ENV_RAGANYTHING_GRAPH = "J1_RAGANYTHING_GRAPH_PROCESSOR"
ENV_RAGANYTHING_RETRIEVAL = "J1_RAGANYTHING_RETRIEVAL_PROCESSOR"
ENV_RAGANYTHING_PDF_CONVERT_EXTENSIONS = "J1_RAGANYTHING_PDF_CONVERT_EXTENSIONS"
ENV_RAGANYTHING_LIBREOFFICE_BINARY = "J1_RAGANYTHING_LIBREOFFICE_BINARY"
ENV_RAGANYTHING_LIBREOFFICE_TIMEOUT = "J1_RAGANYTHING_LIBREOFFICE_TIMEOUT_SECONDS"

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
DEFAULT_LIBREOFFICE_BINARY = "soffice"
DEFAULT_LIBREOFFICE_TIMEOUT = 120.0  # seconds — soffice can be slow on first launch

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
    # MinerU parse_method passed to process_document_complete().
    # Use "vlm-http-client" for lightweight deployments that have VLM
    # API access but no local torch / GPU (avoids the hybrid-auto-engine
    # dependency on libxcb and mineru[pipeline]).
    parse_method: str = DEFAULT_PARSE_METHOD
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


def load_raganything_settings(
    env: Mapping[str, str] | None = None,
) -> RAGAnythingSettings:
    source = env if env is not None else os.environ
    workdir = source.get(ENV_RAGANYTHING_WORKDIR, DEFAULT_WORKDIR)
    return RAGAnythingSettings(
        mode=source.get(ENV_RAGANYTHING_MODE, DEFAULT_MODE),
        workdir=workdir,
        storage_dir=source.get(ENV_RAGANYTHING_STORAGE_DIR)
        or f"{workdir.rstrip('/')}/storage",
        cache_dir=source.get(ENV_RAGANYTHING_CACHE_DIR)
        or f"{workdir.rstrip('/')}/cache",
        parse_method=source.get(ENV_RAGANYTHING_PARSE_METHOD, DEFAULT_PARSE_METHOD),
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
        vlm_http_server_url=(
            source.get(ENV_RAGANYTHING_VLM_HTTP_SERVER_URL)
            or source.get(ENV_J1_VISION_LLM_BASE_URL)
            or None
        ),
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
    )


def _parse_extensions(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated extension list into a normalised tuple.

    `None`              → bundled default set.
    `""` / whitespace   → empty (conversion disabled).
    Otherwise           → each comma-separated entry, lowercased, with
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
