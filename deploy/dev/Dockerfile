# Local-development image for the J1 stack.
#
# Single image runs both the API and the worker; the compose file
# overrides CMD per service. Not optimised for production (single
# stage, full tool chain present); see deploy/dev/README.md for the
# rationale.
#
# System-dep set is aligned with the Q1 reference Dockerfile, which
# has been validated across multiple document formats / use cases
# for the raganything stack. Ours adds a few extra OpenCV C libs
# (libgomp1/libsm6/libxcb1/libxext6/libxrender1) that we hit as
# real `ImportError: lib*.so.N` failures during testing.

FROM python:3.13-slim

# Standard image-hardening env (matches Q1).
#   PYTHONDONTWRITEBYTECODE — no .pyc files written to the image
#   PYTHONUNBUFFERED        — stdout/stderr flush immediately
#   PIP_NO_CACHE_DIR        — don't cache wheels (smaller image layers)
#   PIP_DISABLE_PIP_VERSION_CHECK — skip the network call on each `pip` invocation
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System libraries — the deliberate, audited list for J1's
# `[all-providers]` install (raganything + graphifyy + langchain-*).
#
# === Build tooling ===
# `build-essential` — gcc/make for any pip dep that builds from source.
# `curl`            — used by some setup scripts; harmless to keep.
#
# === OpenCV runtime (raganything → mineru → cv2 dlopen chain) ===
# All seven of these are required by `import cv2`. Without them
# raganything's first compile call surfaces an `ImportError:
# libfoo.so.N` that MinerU re-throws as a misleading
# `HybridDependencyError`.
#   libgl1            — libGL.so.1
#   libglib2.0-0      — libgthread-2.0.so.0
#   libgomp1          — libgomp.so.1   (OpenMP runtime; also used by torch)
#   libsm6            — libSM.so.6
#   libxcb1           — libxcb.so.1
#   libxext6          — libXext.so.6
#   libxrender1       — libXrender.so.1
#
# === LibreOffice (RAGAnything multi-format coverage) ===
# Per the Q1 reference Dockerfile (validated across multiple use
# cases): RAGAnything / mineru's broader document-format coverage
# needs the LibreOffice headless toolchain. We pull only the writer
# + core packages rather than the full LibreOffice suite — that's
# enough for `.doc` / `.docx` / `.odt` ingestion without ballooning
# the image with Calc / Impress / Draw.
#   libreoffice-core
#   libreoffice-writer
#
# === Fonts (PDF text rendering) ===
# Required by both LibreOffice (for layout) and Pillow / mineru
# (for PDF rendering — without them Pillow falls back to substitute
# fonts and emits warnings, and certain non-ASCII PDFs render
# incorrectly).
#   fonts-dejavu-core
#   fonts-liberation
#
# === DELIBERATELY NOT INSTALLED — audited ===
#   * poppler-utils (pdftotext/pdfinfo/pdftoppm) — mineru uses
#     `pypdf` + `pypdfium2` + `pdfminer.six` + `pdftext` (all pure
#     Python). Add only if a "pdftotext: not found" error appears.
#   * tesseract-ocr — mineru's default pipeline uses neural OCR
#     models (via torch), not tesseract. Add if you explicitly
#     enable tesseract-backed OCR.
#   * ffmpeg, unoconv — not in the raganything code path.
#   * libpq-dev — J1 uses SQLite + JSON registries; no PostgreSQL.
# Add any of the above ONLY if you see a concrete "<binary> not
# found" error that pins blame on the missing package.
#
# Graphifyy and langchain-* require zero additional system libraries
# (all pure-Python or precompiled-C wheels).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        libsm6 \
        libxcb1 \
        libxext6 \
        libxrender1 \
        libreoffice-core \
        libreoffice-writer \
        fonts-dejavu-core \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives

WORKDIR /app

# Install the project + its runtime deps + dev-server / dev tooling +
# every supported vendor adapter. Every package is declared in
# `pyproject.toml` — nothing is installed standalone here, so
# `pyproject.toml` stays the single source of truth for what's in
# the image.
#
# Extras included:
#   [dev]           — test deps (pytest, httpx). Useful inside the
#                     container for ad-hoc testing.
#   [dev-server]    — uvicorn (the local-dev ASGI host). Required by
#                     `python -m deploy.dev.api`.
#   [all-providers] — every bundled vendor adapter:
#                       * raganything     (compile / graph / retrieval)
#                       * graphifyy       (graph builder; PyPI name
#                                          `graphifyy`, import name
#                                          `graphify`, CLI binary
#                                          `graphify` — same install
#                                          covers both `mode=python`
#                                          and `mode=cli`)
#                       * langchain-core + langchain-openai
#                         + langchain-anthropic + langchain-google
#                         + langchain-ollama
#                     This lets the runtime switch providers via
#                     env config (`J1_DEFAULT_COMPILER`,
#                     `J1_DEFAULT_GRAPH_PROVIDER=graphify`,
#                     `J1_TEXT_LLM_PROVIDER=langchain`, …) without
#                     rebuilding the image. Heavy (~700MB just for
#                     raganything's transitive deps — torch,
#                     transformers, mineru, opencv; graphify adds
#                     20+ tree-sitter language grammars). For lean
#                     deployments swap `[all-providers]` for the
#                     specific extras you need (e.g. `[raganything]`
#                     alone, or `[langchain-openai]` alone).
COPY pyproject.toml README.md /app/
COPY src/ /app/src/
# `PIP_NO_CACHE_DIR=1` (set in ENV above) already disables the
# wheel cache for both invocations — matches the Q1 pattern.
RUN pip install --upgrade pip \
    && pip install -e ".[dev,dev-server,all-providers]"

# Bring the dev entrypoint scripts in.
COPY deploy/ /app/deploy/

# Workspace lives in a named volume mounted here; pre-create the dir
# so the volume mount has the right ownership.
RUN mkdir -p /var/lib/j1
ENV J1_DATA_ROOT=/var/lib/j1

# Default command runs the API. Compose overrides this for the worker.
EXPOSE 8000
CMD ["python", "-m", "deploy.dev.api"]
