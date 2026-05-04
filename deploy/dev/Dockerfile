# Local-development image for the J1 stack.
#
# Single image runs both the API and the worker; the compose file
# overrides CMD per service. Not optimised for production (single
# stage, full tool chain present); see deploy/dev/README.md for the
# rationale.

FROM python:3.13-slim

# Build deps for any C extensions in dependencies (PyYAML, sqlite3 is
# already in the stdlib build for python:3.13-slim with FTS5).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

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
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e ".[dev,dev-server,all-providers]"

# Bring the dev entrypoint scripts in.
COPY deploy/ /app/deploy/

# Workspace lives in a named volume mounted here; pre-create the dir
# so the volume mount has the right ownership.
RUN mkdir -p /var/lib/j1
ENV J1_DATA_ROOT=/var/lib/j1

# Default command runs the API. Compose overrides this for the worker.
EXPOSE 8000
CMD ["python", "-m", "deploy.dev.api"]
