"""Tests for the provider adapter packages.

Covers:
  * RAGAnything adapters: lazy-import path raises `ProviderUnavailable`
    when `raganything` isn't installed
  * Test-friendly path: passing a custom `compile_callable` skips the
    vendor library entirely
  * Adapter normalises exceptions into `ArtifactProcessingResult` with
    `status=FAILED` (so workflow retries / error handling work)
  * Same shape for `RAGAnythingGraphBuilder` + `RAGAnythingQueryProvider`
  * Graphify adapter: same lazy-import + injectable callable pattern
  * Settings loaders honour `J1_RAGANYTHING_*` and `J1_GRAPHIFY_*`
"""

import pytest

from j1.processing.results import (
    ArtifactDraft,
    ArtifactProcessingResult,
    QueryResult,
    ResultStatus,
)
from j1.projects.context import ProjectContext
from j1.providers.errors import ProviderUnavailable
from j1.providers.graphify import (
    GraphifyGraphBuilder,
    GraphifySettings,
    load_graphify_settings,
)
from j1.providers.raganything import (
    PROVIDER_NAME,
    RAGAnythingCompiler,
    RAGAnythingGraphBuilder,
    RAGAnythingQueryProvider,
    RAGAnythingSettings,
    load_raganything_settings,
)
from j1.providers.raganything.compiler import RAGAnythingCompileRequest
from j1.providers.raganything.graph import RAGAnythingGraphRequest
from j1.providers.raganything.retrieval import RAGAnythingQueryRequest
from j1.llm import (
    LLM_ROLE_EMBEDDING,
    LLM_ROLE_TEXT,
    LLMProviderRegistry,
)


class _FakeText:
    provider = "fake"
    model = "fake-text"


class _FakeEmbed:
    provider = "fake"
    model = "fake-embed"

    def dimension(self) -> int:
        return 1024

    def max_tokens(self) -> int:
        return 8192

    def embed_batch(self, texts):
        texts = list(texts)
        return ([[0.0] * 1024] * len(texts), None)


def _registry() -> LLMProviderRegistry:
    reg = LLMProviderRegistry()
    reg.register(LLM_ROLE_TEXT, _FakeText())
    reg.register(LLM_ROLE_EMBEDDING, _FakeEmbed())
    return reg


def _ctx() -> ProjectContext:
    return ProjectContext(tenant_id="acme", project_id="alpha")


# ---- RAGAnything settings loader ------------------------------------


def test_raganything_settings_defaults():
    s = load_raganything_settings(env={})
    assert s.mode == "local"
    assert s.workdir == "./data/raganything"
    # `storage_dir` defaults to the workdir itself — LightRAG writes
    # `kv_store_*.json` directly into working_dir, not into a
    # `storage` subdirectory. The chunk + graph extractors expect
    # this default to match LightRAG's actual write location.
    assert s.storage_dir == "./data/raganything"
    assert s.cache_dir.endswith("/cache")


def test_raganything_settings_workdir_inherits_into_storage():
    s = load_raganything_settings(env={
        "J1_RAGANYTHING_WORKDIR": "/var/data/rag",
    })
    # storage_dir defaults to the workdir; cache_dir keeps its
    # `<workdir>/cache` convention since the cache layer is ours,
    # not LightRAG's.
    assert s.storage_dir == "/var/data/rag"
    assert s.cache_dir == "/var/data/rag/cache"


def test_raganything_settings_explicit_subdirs_take_precedence():
    s = load_raganything_settings(env={
        "J1_RAGANYTHING_WORKDIR": "/var/data/rag",
        "J1_RAGANYTHING_STORAGE_DIR": "/elsewhere/store",
        "J1_RAGANYTHING_CACHE_DIR": "/elsewhere/cache",
    })
    assert s.storage_dir == "/elsewhere/store"
    assert s.cache_dir == "/elsewhere/cache"


# ---- RAGAnything compiler -------------------------------------------


def test_compiler_uses_injected_callable():
    """Tests inject a fake; vendor library is never imported."""
    seen: list[RAGAnythingCompileRequest] = []

    def fake_callable(request: RAGAnythingCompileRequest) -> ArtifactProcessingResult:
        seen.append(request)
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[ArtifactDraft(
                kind="compiled.text",
                content=b"hello compiled",
                source_document_ids=[request.document_id],
            )],
        )

    compiler = RAGAnythingCompiler(
        llm_registry=_registry(),
        settings=RAGAnythingSettings(),
        compile_callable=fake_callable,
    )
    result = compiler.compile(_ctx(), document_id="doc-1")
    assert result.status is ResultStatus.SUCCEEDED
    assert len(seen) == 1
    assert seen[0].document_id == "doc-1"
    assert seen[0].text_client.model == "fake-text"
    assert seen[0].embedding_client.model == "fake-embed"
    assert seen[0].vision_client is None  # not registered in this fixture


def _simulate_raganything_missing(monkeypatch):
    """Make `import raganything` (anywhere) raise `ImportError`.

    The `[raganything]` extra is now installed in the framework's own
    Docker image + recommended local-dev install, so `raganything` is
    actually present in the test environment. To verify the bridge's
    missing-package error path, we have to force the import to fail.

    Patches `builtins.__import__` for the duration of the test, plus
    deletes any cached `raganything` modules so the bridge's
    `import raganything` re-runs the import and hits our hook.
    """
    import builtins
    import sys

    monkeypatch.delitem(sys.modules, "raganything", raising=False)
    for mod_name in [m for m in sys.modules if m.startswith("raganything.")]:
        monkeypatch.delitem(sys.modules, mod_name, raising=False)

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "raganything" or name.startswith("raganything."):
            raise ImportError(f"test: simulating missing {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)


def test_compiler_default_path_raises_when_raganything_missing(monkeypatch):
    """Without the `raganything` package installed, the real bridge
    raises `ProviderUnavailable` with an actionable pip-install hint."""
    _simulate_raganything_missing(monkeypatch)
    compiler = RAGAnythingCompiler.from_default(
        llm_registry=_registry(),
        settings=RAGAnythingSettings(),
    )
    with pytest.raises(ProviderUnavailable, match="pip install"):
        compiler.compile(_ctx(), document_id="doc-1")


def test_compiler_normalises_exceptions_to_failed_result():
    """A callable raising a non-`ProviderUnavailable` exception turns
    into a FAILED result, never a raw raise — keeps the workflow retry
    path predictable."""
    def boom(_request):
        raise RuntimeError("upstream blew up")

    compiler = RAGAnythingCompiler(
        llm_registry=_registry(),
        settings=RAGAnythingSettings(),
        compile_callable=boom,
    )
    result = compiler.compile(_ctx(), document_id="doc-x")
    assert result.status is ResultStatus.FAILED
    assert "upstream blew up" in (result.error or "")
    assert result.metadata.get("provider") == PROVIDER_NAME


def test_compiler_lets_provider_unavailable_propagate():
    """Vendor-missing errors must be observable to operators — don't
    swallow them into a FAILED result."""
    def boom(_request):
        raise ProviderUnavailable("install raganything")

    compiler = RAGAnythingCompiler(
        llm_registry=_registry(),
        settings=RAGAnythingSettings(),
        compile_callable=boom,
    )
    with pytest.raises(ProviderUnavailable):
        compiler.compile(_ctx(), document_id="doc-1")


def test_compiler_kind_is_provider_name():
    compiler = RAGAnythingCompiler(
        llm_registry=_registry(),
        settings=RAGAnythingSettings(),
        compile_callable=lambda r: ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED, drafts=[],
        ),
    )
    assert compiler.kind == "raganything"


# ---- RAGAnything graph builder --------------------------------------


def test_graph_builder_uses_injected_callable():
    def fake(request: RAGAnythingGraphRequest) -> ArtifactProcessingResult:
        assert request.artifact_ids == ["a-1", "a-2"]
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[ArtifactDraft(
                kind="graph_json", content=b'{"nodes":[]}',
                source_artifact_ids=request.artifact_ids,
            )],
        )

    builder = RAGAnythingGraphBuilder(
        llm_registry=_registry(),
        settings=RAGAnythingSettings(),
        graph_callable=fake,
    )
    result = builder.build(_ctx(), ["a-1", "a-2"])
    assert result.status is ResultStatus.SUCCEEDED


def test_graph_builder_default_path_raises_when_raganything_missing(monkeypatch):
    _simulate_raganything_missing(monkeypatch)
    builder = RAGAnythingGraphBuilder.from_default(
        llm_registry=_registry(),
        settings=RAGAnythingSettings(),
    )
    with pytest.raises(ProviderUnavailable, match="pip install"):
        builder.build(_ctx(), ["a-1"])


# ---- RAGAnything query provider -------------------------------------


def test_query_provider_uses_injected_callable():
    def fake(request: RAGAnythingQueryRequest) -> QueryResult:
        return QueryResult(
            status=ResultStatus.SUCCEEDED,
            answer=f"answer to: {request.question}",
        )

    provider = RAGAnythingQueryProvider(
        llm_registry=_registry(),
        settings=RAGAnythingSettings(),
        query_callable=fake,
    )
    result = provider.query(_ctx(), "what is x?")
    assert result.status is ResultStatus.SUCCEEDED
    assert result.answer == "answer to: what is x?"


def test_query_provider_default_path_raises_when_raganything_missing(monkeypatch):
    _simulate_raganything_missing(monkeypatch)
    provider = RAGAnythingQueryProvider.from_default(
        llm_registry=_registry(),
        settings=RAGAnythingSettings(),
    )
    with pytest.raises(ProviderUnavailable, match="pip install"):
        provider.query(_ctx(), "what is x?")


# ---- Graphify settings + adapter ------------------------------------


def test_graphify_settings_defaults_disabled():
    s = load_graphify_settings(env={})
    assert s.enabled is False
    assert s.mode == "cli"
    assert s.command == "graphify"
    assert s.workdir == "./data/graphify"


def test_graphify_settings_enabled():
    s = load_graphify_settings(env={
        "J1_GRAPHIFY_ENABLED": "true",
        "J1_GRAPHIFY_COMMAND": "/usr/local/bin/graphify-cli",
    })
    assert s.enabled is True
    assert s.command == "/usr/local/bin/graphify-cli"


def test_graphify_default_cli_path_raises_when_binary_missing(monkeypatch):
    """`mode=cli` (default) raises with an actionable message when
    `J1_GRAPHIFY_COMMAND` isn't on $PATH."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    builder = GraphifyGraphBuilder.from_default(
        settings=GraphifySettings(command="definitely-not-installed-xyz"),
    )
    with pytest.raises(ProviderUnavailable, match="not found on \\$PATH"):
        builder.build(_ctx(), ["a-1"])


def _simulate_graphify_missing(monkeypatch):
    """Make `import graphify` raise `ImportError` for the duration
    of one test.

    The `[all-providers]` extra now pulls the `graphifyy` PyPI
    distribution (which provides the `graphify` import name + CLI),
    so the module is actually present in the test environment. To
    verify the bridge's missing-package error path, force the
    import to fail.
    """
    import builtins
    import sys

    monkeypatch.delitem(sys.modules, "graphify", raising=False)
    for mod_name in [m for m in sys.modules if m.startswith("graphify.")]:
        monkeypatch.delitem(sys.modules, mod_name, raising=False)

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "graphify" or name.startswith("graphify."):
            raise ImportError(f"test: simulating missing {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)


def test_graphify_python_mode_raises_when_module_missing(monkeypatch):
    """`mode=python` raises with a pip-install hint when the package
    isn't on sys.path."""
    _simulate_graphify_missing(monkeypatch)
    builder = GraphifyGraphBuilder.from_default(
        settings=GraphifySettings(mode="python"),
    )
    with pytest.raises(ProviderUnavailable, match="pip install"):
        builder.build(_ctx(), ["a-1"])


def test_graphify_unknown_mode_raises_clearly():
    builder = GraphifyGraphBuilder.from_default(
        settings=GraphifySettings(mode="rest-api"),
    )
    with pytest.raises(ProviderUnavailable, match="Unknown Graphify mode"):
        builder.build(_ctx(), ["a-1"])


def test_graphify_uses_injected_callable():
    def fake(request) -> ArtifactProcessingResult:
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[ArtifactDraft(
                kind="graph_json", content=b'{"nodes":[]}',
                source_artifact_ids=request.artifact_ids,
            )],
        )

    builder = GraphifyGraphBuilder(
        settings=GraphifySettings(enabled=True),
        graph_callable=fake,
    )
    result = builder.build(_ctx(), ["a-1"])
    assert result.status is ResultStatus.SUCCEEDED
    assert builder.kind == "graphify"


# ---- Processor-hook auto-construction (env-driven) ------------------


def test_compiler_from_default_loads_env_processor(monkeypatch):
    """`J1_RAGANYTHING_COMPILER_PROCESSOR` lets a deployment wire the
    compile callable via env without subclassing."""
    import sys, types
    from j1.llm import register_trusted_prefix

    register_trusted_prefix("processors_dummy_pkg")
    seen: list = []

    def my_compile(request):
        seen.append(request.document_id)
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED, drafts=[],
        )

    mod = types.ModuleType("processors_dummy_pkg")
    mod.compile_doc = my_compile
    monkeypatch.setitem(sys.modules, "processors_dummy_pkg", mod)

    compiler = RAGAnythingCompiler.from_default(
        llm_registry=_registry(),
        settings=RAGAnythingSettings(
            compiler_processor="processors_dummy_pkg:compile_doc",
        ),
    )
    result = compiler.compile(_ctx(), document_id="doc-X")
    assert result.status is ResultStatus.SUCCEEDED
    assert seen == ["doc-X"]


def test_graph_from_default_loads_env_processor(monkeypatch):
    import sys, types
    from j1.llm import register_trusted_prefix

    register_trusted_prefix("processors_dummy_pkg2")

    def my_graph(request):
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[ArtifactDraft(kind="graph_json", content=b"{}")],
        )

    mod = types.ModuleType("processors_dummy_pkg2")
    mod.build_graph = my_graph
    monkeypatch.setitem(sys.modules, "processors_dummy_pkg2", mod)

    builder = RAGAnythingGraphBuilder.from_default(
        llm_registry=_registry(),
        settings=RAGAnythingSettings(
            graph_processor="processors_dummy_pkg2:build_graph",
        ),
    )
    result = builder.build(_ctx(), ["a-1"])
    assert result.status is ResultStatus.SUCCEEDED


def test_retrieval_from_default_loads_env_processor(monkeypatch):
    import sys, types
    from j1.llm import register_trusted_prefix

    register_trusted_prefix("processors_dummy_pkg3")

    def my_query(request):
        return QueryResult(
            status=ResultStatus.SUCCEEDED, answer=f"Q: {request.question}",
        )

    mod = types.ModuleType("processors_dummy_pkg3")
    mod.query = my_query
    monkeypatch.setitem(sys.modules, "processors_dummy_pkg3", mod)

    provider = RAGAnythingQueryProvider.from_default(
        llm_registry=_registry(),
        settings=RAGAnythingSettings(
            retrieval_processor="processors_dummy_pkg3:query",
        ),
    )
    result = provider.query(_ctx(), "what is x?")
    assert result.status is ResultStatus.SUCCEEDED
    assert result.answer == "Q: what is x?"


def test_graphify_from_default_loads_env_processor(monkeypatch):
    import sys, types
    from j1.llm import register_trusted_prefix

    register_trusted_prefix("processors_dummy_pkg4")

    def my_build(request):
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[ArtifactDraft(kind="graph_json", content=b"{}")],
        )

    mod = types.ModuleType("processors_dummy_pkg4")
    mod.gfy = my_build
    monkeypatch.setitem(sys.modules, "processors_dummy_pkg4", mod)

    builder = GraphifyGraphBuilder.from_default(settings=GraphifySettings(
        enabled=True, graph_processor="processors_dummy_pkg4:gfy",
    ))
    result = builder.build(_ctx(), ["a-1"])
    assert result.status is ResultStatus.SUCCEEDED


def test_settings_loaders_pick_up_processor_env_vars():
    """Env loaders thread the processor strings through."""
    s = load_raganything_settings(env={
        "J1_RAGANYTHING_COMPILER_PROCESSOR": "mypkg:compile",
        "J1_RAGANYTHING_GRAPH_PROCESSOR": "mypkg:graph",
        "J1_RAGANYTHING_RETRIEVAL_PROCESSOR": "mypkg:query",
    })
    assert s.compiler_processor == "mypkg:compile"
    assert s.graph_processor == "mypkg:graph"
    assert s.retrieval_processor == "mypkg:query"

    g = load_graphify_settings(env={
        "J1_GRAPHIFY_GRAPH_PROCESSOR": "mypkg:gfy",
    })
    assert g.graph_processor == "mypkg:gfy"


# ---- Positive boundary tests: from_default reaches the real vendor --
# These tests prove the real default path actually drives the vendor
# entry point — they mock ONLY at the vendor module boundary
# (sys.modules / subprocess.run), not the adapter callable.


def _install_fake_raganything(monkeypatch, *, captured: dict):
    """Inject a fake `raganything` module at sys.modules level.

    Records the constructor kwargs and what process_document_complete /
    aquery were called with. Writes one fake output file when the
    compile path runs so the bridge has something to walk.
    """
    import sys
    import types

    class _FakeConfig:
        def __init__(self, **kwargs):
            captured["config_kwargs"] = kwargs

    class _FakeRAG:
        def __init__(self, **kwargs):
            captured["rag_kwargs"] = kwargs

        async def _ensure_lightrag_initialized(self):
            return {"success": True}

        async def process_document_complete(
            self, *, file_path, output_dir, parse_method,
        ):
            captured["compile_call"] = {
                "file_path": file_path,
                "output_dir": output_dir,
                "parse_method": parse_method,
            }
            from pathlib import Path
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / "parsed.md").write_text("# parsed by fake raganything")
            (out / "metadata.json").write_text('{"source": "fake"}')

        async def aquery(self, question, mode="hybrid"):
            captured["query_call"] = {"question": question, "mode": mode}
            return f"vendor-answer to: {question}"

    fake_mod = types.ModuleType("raganything")
    fake_mod.RAGAnything = _FakeRAG
    fake_mod.RAGAnythingConfig = _FakeConfig
    monkeypatch.setitem(sys.modules, "raganything", fake_mod)

    # Make sure the bridge re-imports a clean state — the bridge itself
    # may have been imported earlier by other tests.
    import j1.providers.raganything._bridge as bridge_mod
    return bridge_mod


def test_compiler_default_path_invokes_real_raganything_when_installed(
    monkeypatch, tmp_path,
):
    """from_default() → bridge → vendor `RAGAnything.process_document_complete`.

    Proves the real default path reaches the vendor boundary; only the
    vendor module itself is replaced.
    """
    captured: dict = {}
    _install_fake_raganything(monkeypatch, captured=captured)

    # Workspace + raw source file the bridge will look for.
    monkeypatch.setenv("J1_DATA_ROOT", str(tmp_path))
    raw_dir = tmp_path / "tenants" / "acme" / "projects" / "alpha" / "raw"
    raw_dir.mkdir(parents=True)
    source_file = raw_dir / "doc-1.pdf"
    source_file.write_bytes(b"%PDF-fake")

    workdir = tmp_path / "rag-workdir"
    compiler = RAGAnythingCompiler.from_default(
        llm_registry=_registry(),
        settings=RAGAnythingSettings(workdir=str(workdir)),
    )
    result = compiler.compile(_ctx(), document_id="doc-1")

    assert result.status is ResultStatus.SUCCEEDED, result.error
    # Vendor was actually invoked through the real bridge.
    assert "rag_kwargs" in captured
    assert "llm_model_func" in captured["rag_kwargs"]
    assert "embedding_func" in captured["rag_kwargs"]
    assert captured["compile_call"]["file_path"] == str(source_file)
    assert captured["compile_call"]["parse_method"] == "auto"
    # Bridge walked the output dir → drafts.
    assert len(result.drafts) == 2
    kinds = {d.kind for d in result.drafts}
    assert "compiled.text" in kinds
    assert "compiled.text.metadata" in kinds


def test_query_provider_default_path_invokes_real_aquery_when_installed(
    monkeypatch,
):
    """from_default() → bridge → vendor `RAGAnything.aquery`."""
    captured: dict = {}
    _install_fake_raganything(monkeypatch, captured=captured)

    provider = RAGAnythingQueryProvider.from_default(
        llm_registry=_registry(),
        settings=RAGAnythingSettings(),
    )
    result = provider.query(_ctx(), "what is x?")

    assert result.status is ResultStatus.SUCCEEDED, result.error
    assert captured["query_call"]["question"] == "what is x?"
    assert captured["query_call"]["mode"] == "hybrid"
    assert result.answer == "vendor-answer to: what is x?"


def test_graph_builder_default_path_invokes_real_storage_walk_when_installed(
    monkeypatch, tmp_path,
):
    """from_default() → bridge → walks RAGAnything storage dir for graph files."""
    captured: dict = {}
    _install_fake_raganything(monkeypatch, captured=captured)

    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "graph_chunk_entity_relation.json").write_text(
        '{"nodes": [{"id": "n1"}], "edges": []}',
    )
    (storage / "kv_store_full_docs.json").write_text('{"doc-1": "..."}')

    builder = RAGAnythingGraphBuilder.from_default(
        llm_registry=_registry(),
        settings=RAGAnythingSettings(
            workdir=str(tmp_path), storage_dir=str(storage),
        ),
    )
    result = builder.build(_ctx(), ["a-1"])

    assert result.status is ResultStatus.SUCCEEDED, result.error
    # Vendor instance was constructed (proves real bridge path was taken).
    assert "rag_kwargs" in captured
    # Bridge walked the storage dir and surfaced graph artifacts.
    assert len(result.drafts) >= 1
    assert all(d.kind == "graph_json" for d in result.drafts)
    filenames = {d.metadata.get("filename") for d in result.drafts}
    assert "graph_chunk_entity_relation.json" in filenames


def test_graphify_cli_default_path_invokes_subprocess_when_binary_present(
    monkeypatch, tmp_path,
):
    """from_default() → bridge → runs the CLI subprocess.

    Mocks `shutil.which` (binary discovery) and `subprocess.run` only.
    The bridge composes argv, writes input.json, and parses output.json
    for real.
    """
    import json as _json
    import subprocess
    from types import SimpleNamespace
    import j1.providers.graphify._bridge as gbridge

    captured: dict = {}
    monkeypatch.setattr(gbridge.shutil, "which", lambda _name: "/usr/bin/graphify-fake")

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        # Find the --output path from argv and write a real JSON file.
        out_idx = argv.index("--output") + 1
        out_path = Path(argv[out_idx])
        out_path.write_text(_json.dumps({
            "nodes": [{"id": "n1"}, {"id": "n2"}],
            "edges": [{"src": "n1", "dst": "n2"}],
        }))
        # Verify the bridge actually wrote the input file too.
        in_idx = argv.index("--input") + 1
        in_payload = _json.loads(Path(argv[in_idx]).read_text())
        captured["input_payload"] = in_payload
        return SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    from pathlib import Path
    workdir = tmp_path / "graphify-work"
    builder = GraphifyGraphBuilder.from_default(
        settings=GraphifySettings(
            enabled=True, mode="cli",
            command="/usr/bin/graphify-fake", workdir=str(workdir),
        ),
    )
    result = builder.build(_ctx(), ["a-1", "a-2"])

    assert result.status is ResultStatus.SUCCEEDED, result.error
    # Subprocess WAS called.
    assert captured["argv"][0] == "/usr/bin/graphify-fake"
    assert "--input" in captured["argv"]
    assert "--output" in captured["argv"]
    assert "--workdir" in captured["argv"]
    # Input JSON had the canonical payload.
    assert captured["input_payload"] == {
        "tenant_id": "acme", "project_id": "alpha",
        "artifact_ids": ["a-1", "a-2"],
    }
    # Bridge produced exactly one graph_json draft from output.json.
    assert len(result.drafts) == 1
    assert result.drafts[0].kind == "graph_json"
    payload = _json.loads(result.drafts[0].content)
    assert payload["nodes"] == [{"id": "n1"}, {"id": "n2"}]


def test_graphify_python_default_path_invokes_vendor_when_installed(monkeypatch):
    """from_default(mode=python) → bridge → vendor `graphify.build_graph`."""
    import sys
    import types

    captured: dict = {}

    def fake_build_graph(payload):
        captured["payload"] = payload
        return {
            "nodes": [{"id": "x"}],
            "edges": [{"src": "x", "dst": "x"}],
        }

    fake_mod = types.ModuleType("graphify")
    fake_mod.build_graph = fake_build_graph
    monkeypatch.setitem(sys.modules, "graphify", fake_mod)

    builder = GraphifyGraphBuilder.from_default(
        settings=GraphifySettings(
            enabled=True, mode="python", workdir="/tmp/gf-test",
        ),
    )
    result = builder.build(_ctx(), ["a-1"])

    assert result.status is ResultStatus.SUCCEEDED, result.error
    assert captured["payload"] == {
        "tenant_id": "acme", "project_id": "alpha",
        "artifact_ids": ["a-1"], "workdir": "/tmp/gf-test",
    }
    assert len(result.drafts) == 1
    assert result.drafts[0].kind == "graph_json"
