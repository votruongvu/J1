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

# Install the project + its runtime deps + dev-server / dev tooling.
# Uvicorn is the local-dev API server; it's not a framework dep.
COPY pyproject.toml README.md /app/
COPY src/ /app/src/
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e ".[dev]" "uvicorn>=0.27"

# Bring the dev entrypoint scripts in.
COPY deploy/ /app/deploy/

# Workspace lives in a named volume mounted here; pre-create the dir
# so the volume mount has the right ownership.
RUN mkdir -p /var/lib/j1
ENV J1_DATA_ROOT=/var/lib/j1

# Default command runs the API. Compose overrides this for the worker.
EXPOSE 8000
CMD ["python", "-m", "deploy.dev.api"]
