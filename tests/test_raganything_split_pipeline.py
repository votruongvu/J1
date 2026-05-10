"""Tests for the split_parse_insert RAGAnything pipeline mode.

Pins the public contract of `default_parse_source` and
`default_insert_content`:

  * Parse-only path: drives `rag.parse_document`, persists the raw
    `content_list` as a `parsed_source` artifact, builds the FE
    Content Inventory manifest with real items[] populated from
    the same content_list.
  * Insert-only path: drives `rag.insert_content_list`, surfaces
    LightRAG chunk artifacts, marks insert/chunk/graph boundaries
    honestly in metadata.
  * Auto-fallback: when the installed RAGAnything doesn't expose
    `parse_document`, `default_parse_source` falls back to the
    legacy combined path with a clear warning marker.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from j1.processing.results import (
    ARTIFACT_KIND_CHUNK,
    ARTIFACT_KIND_COMPILED_TEXT,
    ARTIFACT_KIND_PARSED_CONTENT_MANIFEST,
    ARTIFACT_KIND_PARSED_SOURCE,
    ResultStatus,
)
from j1.providers.raganything._bridge import (
    _split_methods_available,
    default_insert_content,
    default_parse_source,
)
from j1.providers.raganything.compiler import RAGAnythingCompileRequest
from j1.providers.raganything.settings import (
    PIPELINE_MODE_COMPLETE,
    PIPELINE_MODE_SPLIT_PARSE_INSERT,
    load_raganything_settings,
)


# ---- Fixtures + fakes ----------------------------------------------


def _settings(tmp_path: Path, mode: str = PIPELINE_MODE_SPLIT_PARSE_INSERT):
    """Build minimal RAGAnything settings rooted at tmp_path.

    The loader requires a VLM HTTP server URL since J1 forces MinerU
    into HTTP-client mode (see settings.py); a stub URL is enough
    for these tests because the bridge fakes never actually contact
    it."""
    return load_raganything_settings(env={
        "J1_RAGANYTHING_WORKDIR": str(tmp_path / "raganything"),
        "J1_RAGANYTHING_PIPELINE_MODE": mode,
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
    })


class _FakeRAG:
    """Minimal stand-in for the RAGAnything class.

    Captures `parse_document` / `insert_content_list` calls and
    returns a configurable content_list. `_ensure_lightrag_initialized`
    is async because the bridge awaits it before insert."""

    def __init__(self, content_list, doc_id="doc-stub"):
        self._content_list = content_list
        self._doc_id = doc_id
        self.parse_calls: list[dict] = []
        self.insert_calls: list[dict] = []

    async def parse_document(
        self, *, file_path, output_dir=None, parse_method=None, **kwargs,
    ):
        self.parse_calls.append({
            "file_path": file_path,
            "output_dir": output_dir,
            "parse_method": parse_method,
            "kwargs": kwargs,
        })
        # Mimic vendor: write a content_list.json so the bridge's
        # `_build_content_manifest` walker has something to read.
        if output_dir:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            (Path(output_dir) / "doc_content_list.json").write_text(
                json.dumps(self._content_list), encoding="utf-8",
            )
        return (self._content_list, self._doc_id)

    async def insert_content_list(
        self, *, content_list, file_path, doc_id=None, **kwargs,
    ):
        self.insert_calls.append({
            "content_list": content_list,
            "file_path": file_path,
            "doc_id": doc_id,
            "kwargs": kwargs,
        })

    async def _ensure_lightrag_initialized(self):
        return {"success": True}


def _make_request(
    tmp_path: Path,
    document_id="doc-1",
    mode=PIPELINE_MODE_SPLIT_PARSE_INSERT,
    *,
    monkeypatch=None,
):
    """Build a CompileRequest backed by a tmp workspace + fake clients.

    Sets `J1_DATA_ROOT` to `tmp_path` via monkeypatch so the bridge's
    `_resolve_workspace_root` finds the source file we wrote here."""
    raw_dir = tmp_path / "tenants" / "acme" / "projects" / "alpha" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{document_id}.txt").write_text(
        "Sample content for testing.", encoding="utf-8",
    )
    if monkeypatch is not None:
        monkeypatch.setenv("J1_DATA_ROOT", str(tmp_path))
    settings = _settings(tmp_path, mode=mode)
    return RAGAnythingCompileRequest(
        ctx=SimpleNamespace(tenant_id="acme", project_id="alpha"),
        document_id=document_id,
        settings=settings,
        text_client=SimpleNamespace(provider="stub", model="stub"),
        vision_client=None,
        embedding_client=SimpleNamespace(provider="stub", model="stub"),
    )


# ---- Detection -----------------------------------------------------


def test_split_methods_detected_on_installed_raganything():
    """The pinned RAGAnything dep (1.2.10) exposes both methods —
    if this ever flips to False, an upgrade silently broke the
    split path."""
    assert _split_methods_available() is True


# ---- default_parse_source ------------------------------------------


def test_parse_source_persists_parsed_source_and_manifest(tmp_path, monkeypatch):
    """The parse-only path emits parsed_source + manifest drafts
    (no chunks). Items[] in the manifest comes from the same
    content_list the parser returned."""
    content_list = [
        {"type": "title", "text": "Quarterly Report Q1", "page_idx": 0},
        {"type": "text", "text": "Revenue grew 12% over Q4.", "page_idx": 0},
        {"type": "table", "table_caption": "Sales by region", "page_idx": 1},
        {"type": "image", "img_path": "img_1.png", "img_caption": "Pipeline chart", "page_idx": 2},
    ]
    fake = _FakeRAG(content_list, doc_id="rag-doc-99")

    # Use a non-text-extension source so the bridge takes the
    # `rag.parse_document` path (not the plaintext / pypdf
    # fast paths).
    raw_dir = tmp_path / "tenants" / "acme" / "projects" / "alpha" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "doc-1.docx").write_bytes(b"PK\x03\x04binarydocxbody")
    monkeypatch.setenv("J1_DATA_ROOT", str(tmp_path))
    settings = _settings(tmp_path)
    request = RAGAnythingCompileRequest(
        ctx=SimpleNamespace(tenant_id="acme", project_id="alpha"),
        document_id="doc-1",
        settings=settings,
        text_client=SimpleNamespace(provider="stub", model="stub"),
        vision_client=None,
        embedding_client=SimpleNamespace(provider="stub", model="stub"),
    )
    # Disable PDF pre-conversion so docx → docx (no soffice call).
    monkeypatch.setattr(
        "j1.providers.raganything._bridge._needs_pdf_conversion",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "j1.providers.raganything._bridge._build_rag_instance",
        lambda **kw: fake,
    )
    # Skip env-side application so the test stays hermetic.
    monkeypatch.setattr(
        "j1.providers.raganything._bridge._apply_vlm_http_client_env",
        lambda settings: None,
    )

    result = default_parse_source(request)

    assert result.status == ResultStatus.SUCCEEDED
    assert fake.parse_calls, "parse_document was not called"
    assert fake.insert_calls == [], (
        "parse-only path must NOT call insert_content_list"
    )

    kinds = [d.kind for d in result.drafts]
    assert ARTIFACT_KIND_PARSED_SOURCE in kinds
    assert ARTIFACT_KIND_PARSED_CONTENT_MANIFEST in kinds
    # Chunks come from the insert phase — not in parse output.
    assert ARTIFACT_KIND_CHUNK not in kinds

    # parsed_source draft carries the raw content_list + doc_id.
    parsed = next(d for d in result.drafts if d.kind == ARTIFACT_KIND_PARSED_SOURCE)
    payload = json.loads(parsed.content)
    assert payload["doc_id"] == "rag-doc-99"
    assert len(payload["content_list"]) == 4

    # manifest draft has items populated from content_list.
    manifest = next(d for d in result.drafts if d.kind == ARTIFACT_KIND_PARSED_CONTENT_MANIFEST)
    manifest_payload = json.loads(manifest.content)
    assert len(manifest_payload["items"]) >= 3
    types = {it["type"] for it in manifest_payload["items"]}
    assert "image" in types
    assert "table" in types

    # Honest metadata — operators verify boundaries.
    assert result.metadata["pipeline_mode"] == "split_parse_insert"
    assert result.metadata["parse_boundary"] == "real"
    assert result.metadata["doc_id"] == "rag-doc-99"
    assert result.metadata["item_count"] == 4


def test_parse_source_uses_plaintext_fast_path_for_txt(tmp_path, monkeypatch):
    """Plain-text inputs go through the pypdf-free fast path —
    `parse_document` is NOT invoked, but the parsed_source draft
    is still persisted so the insert step has something to read."""
    fake = _FakeRAG([], doc_id="should-not-be-used")
    request = _make_request(
        tmp_path, document_id="doc-plain", monkeypatch=monkeypatch,
    )

    monkeypatch.setattr(
        "j1.providers.raganything._bridge._build_rag_instance",
        lambda **kw: fake,
    )
    monkeypatch.setattr(
        "j1.providers.raganything._bridge._apply_vlm_http_client_env",
        lambda settings: None,
    )

    result = default_parse_source(request)

    assert result.status == ResultStatus.SUCCEEDED
    assert fake.parse_calls == [], (
        "fast path must skip vendor parse_document for plain text"
    )
    assert result.metadata["parser_engine"] == "j1.fast_path.plaintext"
    parsed = next(d for d in result.drafts if d.kind == ARTIFACT_KIND_PARSED_SOURCE)
    payload = json.loads(parsed.content)
    assert payload["content_list"][0]["type"] == "text"
    assert "Sample content" in payload["content_list"][0]["text"]


def test_parse_source_falls_back_when_methods_unavailable(tmp_path, monkeypatch):
    """When the installed RAGAnything lacks parse_document, the
    parse-only path falls back to the legacy combined behaviour
    and surfaces a clear warning marker so reviewers can see why
    the boundary collapsed."""
    request = _make_request(tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(
        "j1.providers.raganything._bridge._split_methods_available",
        lambda: False,
    )
    # Fake legacy compile to a known shape so we can assert the
    # fallback path was taken without invoking real RAGAnything.
    fake_legacy_result = type(
        "FakeResult", (object,),
        {
            "status": ResultStatus.SUCCEEDED,
            "drafts": [],
            "artifacts": [],
            "cost_events": [],
            "message": None,
            "error": None,
            "metadata": {"provider": "raganything"},
        },
    )()
    monkeypatch.setattr(
        "j1.providers.raganything._bridge._default_compile_complete",
        lambda req: fake_legacy_result,
    )

    result = default_parse_source(request)

    assert result.status == ResultStatus.SUCCEEDED
    assert result.metadata["parse_boundary"] == "legacy_combined"
    assert "pipeline_mode_warning" in result.metadata


# ---- default_insert_content ----------------------------------------


def test_insert_content_drives_insert_content_list(tmp_path, monkeypatch):
    """The insert-only path calls `rag.insert_content_list` with the
    content_list + doc_id we feed it, marks all three boundary
    flags honestly, and surfaces chunk drafts from storage."""
    content_list = [{"type": "text", "text": "stub", "page_idx": 0}]
    fake = _FakeRAG(content_list, doc_id="rag-doc-99")

    request = _make_request(tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(
        "j1.providers.raganything._bridge._build_rag_instance",
        lambda **kw: fake,
    )
    monkeypatch.setattr(
        "j1.providers.raganything._bridge._apply_vlm_http_client_env",
        lambda settings: None,
    )
    # Stub the chunk-storage walker so we don't need a real
    # LightRAG kv_store_text_chunks.json on disk.
    monkeypatch.setattr(
        "j1.providers.raganything._bridge._chunk_drafts_from_storage",
        lambda storage_dir, document_id, doc_id=None: [],
    )

    result = default_insert_content(
        request,
        content_list=content_list,
        doc_id="rag-doc-99",
        source_filename="doc-1.txt",
    )

    assert result.status == ResultStatus.SUCCEEDED
    assert fake.insert_calls, "insert_content_list was not called"
    assert fake.insert_calls[0]["content_list"] == content_list
    assert fake.insert_calls[0]["doc_id"] == "rag-doc-99"
    assert fake.insert_calls[0]["file_path"] == "doc-1.txt"

    # The insert step should NOT also call parse_document.
    assert fake.parse_calls == []

    # Boundary metadata — pinned so a future refactor that loses
    # honesty about the chunk/graph coupling fails loudly.
    assert result.metadata["pipeline_mode"] == "split_parse_insert"
    assert result.metadata["parse_boundary"] == "real"
    assert result.metadata["insert_boundary"] == "raganything_insert_content_list"
    assert result.metadata["chunk_graph_boundary"] == (
        "combined_if_raganything_controls_it"
    )


def test_insert_content_returns_failed_when_methods_missing(tmp_path, monkeypatch):
    """Older RAGAnything versions can't run the insert-only path
    at all. Surface that as a typed FAILED result so the workflow
    treats the step as a hard error rather than silently no-oping."""
    request = _make_request(tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(
        "j1.providers.raganything._bridge._split_methods_available",
        lambda: False,
    )

    result = default_insert_content(
        request,
        content_list=[],
        doc_id="doc",
    )

    assert result.status == ResultStatus.FAILED
    assert result.metadata["insert_boundary"] == "unsupported"


# ---- default_compile dispatch --------------------------------------


def test_default_compile_dispatches_to_parse_source_in_split_mode(
    tmp_path, monkeypatch,
):
    """`default_compile` is the activity-side entrypoint; it must
    delegate to `default_parse_source` when settings.pipeline_mode
    is split. Without this dispatch, the workflow's compile activity
    would still run the legacy combined path."""
    from j1.providers.raganything._bridge import default_compile

    captured: dict = {}

    def _spy(request):
        captured["called"] = True
        return type(
            "FakeResult", (object,),
            {
                "status": ResultStatus.SUCCEEDED,
                "drafts": [],
                "artifacts": [],
                "cost_events": [],
                "message": None,
                "error": None,
                "metadata": {"provider": "raganything"},
            },
        )()

    monkeypatch.setattr(
        "j1.providers.raganything._bridge.default_parse_source",
        _spy,
    )

    request = _make_request(
        tmp_path, mode=PIPELINE_MODE_SPLIT_PARSE_INSERT,
        monkeypatch=monkeypatch,
    )
    default_compile(request)
    assert captured.get("called") is True


def test_default_compile_uses_legacy_path_in_complete_mode(
    tmp_path, monkeypatch,
):
    """`pipeline_mode=complete` keeps the existing
    `process_document_complete` flow. Pinned so a future default
    flip doesn't accidentally remove the legacy escape hatch."""
    from j1.providers.raganything._bridge import default_compile

    captured: dict = {}

    def _spy(request):
        captured["called"] = True
        return type(
            "FakeResult", (object,),
            {
                "status": ResultStatus.SUCCEEDED,
                "drafts": [],
                "artifacts": [],
                "cost_events": [],
                "message": None,
                "error": None,
                "metadata": {"provider": "raganything"},
            },
        )()

    monkeypatch.setattr(
        "j1.providers.raganything._bridge._default_compile_complete",
        _spy,
    )

    request = _make_request(
        tmp_path, mode=PIPELINE_MODE_COMPLETE, monkeypatch=monkeypatch,
    )
    default_compile(request)
    assert captured.get("called") is True
