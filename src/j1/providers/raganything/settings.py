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

DEFAULT_MODE = "local"
DEFAULT_WORKDIR = "./data/raganything"
DEFAULT_PARSE_METHOD = "auto"


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
    )
