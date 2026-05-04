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
    assert s.storage_dir.endswith("/storage")
    assert s.cache_dir.endswith("/cache")


def test_raganything_settings_workdir_overrides_inferred_subdirs():
    s = load_raganything_settings(env={
        "J1_RAGANYTHING_WORKDIR": "/var/data/rag",
    })
    assert s.storage_dir == "/var/data/rag/storage"
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


def test_compiler_default_path_raises_provider_unavailable():
    """Without a custom callable, the default path tries to import
    `raganything` and raises `ProviderUnavailable` (we don't have it
    installed in the framework's hermetic test env)."""
    compiler = RAGAnythingCompiler.from_default(
        llm_registry=_registry(),
        settings=RAGAnythingSettings(),
    )
    with pytest.raises(ProviderUnavailable):
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


def test_graph_builder_default_path_raises_provider_unavailable():
    builder = RAGAnythingGraphBuilder.from_default(
        llm_registry=_registry(),
        settings=RAGAnythingSettings(),
    )
    with pytest.raises(ProviderUnavailable):
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


def test_query_provider_default_path_raises_provider_unavailable():
    provider = RAGAnythingQueryProvider.from_default(
        llm_registry=_registry(),
        settings=RAGAnythingSettings(),
    )
    with pytest.raises(ProviderUnavailable):
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


def test_graphify_default_path_raises_provider_unavailable():
    builder = GraphifyGraphBuilder.from_default(settings=GraphifySettings())
    with pytest.raises(ProviderUnavailable):
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
