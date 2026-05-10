"""Tests for the LibreOffice pre-conversion step in the RAGAnything bridge.

Behaviour under test:
  * Settings: extension list + binary + timeout parse correctly from env.
  * Detection: `_needs_pdf_conversion` honours the configured set
    (case-insensitive; respects empty-list disable).
  * Conversion: `_convert_to_pdf` shells out to soffice with the
    correct argv shape (mocked subprocess); returns the output path.
  * Missing binary → `ProviderUnavailable` with actionable message.
  * Subprocess non-zero exit / no output / timeout → wrapped in
    `_LibreOfficeConversionError` (caught at the compile boundary
    and surfaced as a FAILED result).
  * End-to-end: when `default_compile` is called against a `.doc`
    source, the conversion runs first and raganything sees the
    converted PDF, not the original.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from j1.providers.errors import ProviderUnavailable
from j1.providers.raganything._bridge import (
    _LibreOfficeConversionError,
    _convert_to_pdf,
    _needs_pdf_conversion,
)
from j1.providers.raganything.settings import (
    DEFAULT_LIBREOFFICE_BINARY,
    DEFAULT_LIBREOFFICE_TIMEOUT,
    DEFAULT_PDF_CONVERT_EXTENSIONS,
    RAGAnythingSettings,
    load_raganything_settings,
)


# ---- Settings parsing ----------------------------------------------


def test_settings_default_pdf_convert_extensions():
    s = load_raganything_settings(env={"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1"})
    assert s.pdf_convert_extensions == DEFAULT_PDF_CONVERT_EXTENSIONS
    # Sanity: every default entry has a leading dot and is lowercase.
    for ext in s.pdf_convert_extensions:
        assert ext.startswith(".")
        assert ext == ext.lower()


def test_settings_default_libreoffice_binary_and_timeout():
    s = load_raganything_settings(env={"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1"})
    assert s.libreoffice_binary == DEFAULT_LIBREOFFICE_BINARY
    assert s.libreoffice_timeout_seconds == DEFAULT_LIBREOFFICE_TIMEOUT


def test_settings_custom_extension_list_normalised():
    """User can supply with or without leading dot, any case."""
    s = load_raganything_settings(env={
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_RAGANYTHING_PDF_CONVERT_EXTENSIONS": ".doc, xls, DOCX",
    })
    assert s.pdf_convert_extensions == (".doc", ".xls", ".docx")


def test_settings_empty_extension_list_disables_conversion():
    s = load_raganything_settings(env={
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_RAGANYTHING_PDF_CONVERT_EXTENSIONS": "",
    })
    assert s.pdf_convert_extensions == ()


def test_settings_custom_libreoffice_binary():
    s = load_raganything_settings(env={
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_RAGANYTHING_LIBREOFFICE_BINARY": "/opt/libreoffice/program/soffice",
    })
    assert s.libreoffice_binary == "/opt/libreoffice/program/soffice"


def test_settings_custom_libreoffice_timeout():
    s = load_raganything_settings(env={
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_RAGANYTHING_LIBREOFFICE_TIMEOUT_SECONDS": "60",
    })
    assert s.libreoffice_timeout_seconds == 60.0


def test_settings_libreoffice_timeout_must_be_positive():
    with pytest.raises(ValueError, match="must be > 0"):
        load_raganything_settings(env={
            "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
            "J1_RAGANYTHING_LIBREOFFICE_TIMEOUT_SECONDS": "0",
        })


def test_settings_libreoffice_timeout_must_be_numeric():
    with pytest.raises(ValueError, match="must be a number"):
        load_raganything_settings(env={
            "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
            "J1_RAGANYTHING_LIBREOFFICE_TIMEOUT_SECONDS": "fast",
        })


# ---- Detection -----------------------------------------------------


def _settings_with_default_extensions() -> RAGAnythingSettings:
    return load_raganything_settings(env={"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1"})


def test_needs_pdf_conversion_for_legacy_doc():
    assert _needs_pdf_conversion(Path("file.doc"), _settings_with_default_extensions())


def test_needs_pdf_conversion_is_case_insensitive():
    assert _needs_pdf_conversion(Path("FILE.DOC"), _settings_with_default_extensions())
    assert _needs_pdf_conversion(Path("file.OdT"), _settings_with_default_extensions())


@pytest.mark.parametrize(
    "ext", [".pdf", ".docx", ".xlsx", ".pptx", ".png", ".jpg", ".txt", ".html"],
)
def test_native_formats_are_not_converted(ext):
    """Anything raganything / mineru handles natively must pass through."""
    assert not _needs_pdf_conversion(
        Path(f"file{ext}"), _settings_with_default_extensions(),
    )


def test_no_extensions_configured_means_no_conversion():
    """Empty extension set disables conversion globally (escape hatch)."""
    settings = load_raganything_settings(env={
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_RAGANYTHING_PDF_CONVERT_EXTENSIONS": "",
    })
    assert not _needs_pdf_conversion(Path("file.doc"), settings)


# ---- Subprocess invocation -----------------------------------------


def test_convert_to_pdf_invokes_soffice_with_expected_argv(tmp_path, monkeypatch):
    """The subprocess call shape: --headless --convert-to pdf
    --outdir <tmp> <source>."""
    settings = load_raganything_settings(env={"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1"})
    source = tmp_path / "demo.doc"
    source.write_bytes(b"junk binary doc")

    captured: dict = {}

    def fake_which(name):
        return f"/fake/bin/{name}"

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        # Simulate soffice writing the output PDF.
        outdir = Path(argv[argv.index("--outdir") + 1])
        (outdir / "demo.pdf").write_bytes(b"%PDF-1.4 fake content")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("j1.providers.raganything._bridge.shutil.which", fake_which)
    monkeypatch.setattr("j1.providers.raganything._bridge.subprocess.run", fake_run)

    result = _convert_to_pdf(source, settings)

    assert captured["argv"][0] == "/fake/bin/soffice"
    assert "--headless" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--convert-to") + 1] == "pdf"
    assert str(source) == captured["argv"][-1]
    # No shell=True, ever.
    assert captured["kwargs"].get("shell") is not True
    # Timeout was forwarded.
    assert captured["kwargs"]["timeout"] == settings.libreoffice_timeout_seconds
    # Result is the produced PDF.
    assert result.exists()
    assert result.suffix == ".pdf"
    assert result.read_bytes().startswith(b"%PDF-")


# ---- Missing binary path ------------------------------------------


def test_convert_to_pdf_raises_provider_unavailable_when_binary_missing(
    tmp_path, monkeypatch,
):
    settings = load_raganything_settings(env={"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1"})
    source = tmp_path / "demo.doc"
    source.write_bytes(b"junk")

    monkeypatch.setattr(
        "j1.providers.raganything._bridge.shutil.which",
        lambda _name: None,
    )
    with pytest.raises(ProviderUnavailable, match="not found on \\$PATH"):
        _convert_to_pdf(source, settings)


def test_convert_to_pdf_provider_unavailable_message_is_actionable(
    tmp_path, monkeypatch,
):
    """The error must name (a) install, (b) override-binary path,
    (c) shrink-extensions option, (d) processor-hook escape."""
    settings = load_raganything_settings(env={"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1"})
    source = tmp_path / "demo.doc"
    source.write_bytes(b"junk")

    monkeypatch.setattr(
        "j1.providers.raganything._bridge.shutil.which",
        lambda _name: None,
    )
    with pytest.raises(ProviderUnavailable) as exc_info:
        _convert_to_pdf(source, settings)
    msg = str(exc_info.value)
    # Mention each escape hatch operators have:
    assert "apt-get install" in msg or "libreoffice" in msg.lower()
    assert "J1_RAGANYTHING_LIBREOFFICE_BINARY" in msg
    assert "J1_RAGANYTHING_PDF_CONVERT_EXTENSIONS" in msg
    assert "J1_RAGANYTHING_COMPILER_PROCESSOR" in msg


# ---- Conversion failure paths --------------------------------------


def test_convert_to_pdf_wraps_nonzero_exit_in_conversion_error(
    tmp_path, monkeypatch,
):
    settings = load_raganything_settings(env={"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1"})
    source = tmp_path / "demo.doc"
    source.write_bytes(b"junk")

    monkeypatch.setattr(
        "j1.providers.raganything._bridge.shutil.which",
        lambda _name: "/fake/bin/soffice",
    )
    monkeypatch.setattr(
        "j1.providers.raganything._bridge.subprocess.run",
        lambda argv, **kwargs: SimpleNamespace(
            returncode=2, stdout=b"", stderr=b"soffice: error opening file",
        ),
    )
    with pytest.raises(_LibreOfficeConversionError, match="exited 2"):
        _convert_to_pdf(source, settings)


def test_convert_to_pdf_wraps_no_output_in_conversion_error(
    tmp_path, monkeypatch,
):
    """Even when soffice returns 0, missing/empty output PDF is fatal."""
    settings = load_raganything_settings(env={"J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1"})
    source = tmp_path / "demo.doc"
    source.write_bytes(b"junk")

    monkeypatch.setattr(
        "j1.providers.raganything._bridge.shutil.which",
        lambda _name: "/fake/bin/soffice",
    )
    # Returns success but writes nothing.
    monkeypatch.setattr(
        "j1.providers.raganything._bridge.subprocess.run",
        lambda argv, **kwargs: SimpleNamespace(
            returncode=0, stdout=b"", stderr=b"",
        ),
    )
    with pytest.raises(_LibreOfficeConversionError, match="produced no output"):
        _convert_to_pdf(source, settings)


def test_convert_to_pdf_wraps_timeout_in_conversion_error(
    tmp_path, monkeypatch,
):
    settings = load_raganything_settings(env={
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_RAGANYTHING_LIBREOFFICE_TIMEOUT_SECONDS": "1",
    })
    source = tmp_path / "demo.doc"
    source.write_bytes(b"junk")

    monkeypatch.setattr(
        "j1.providers.raganything._bridge.shutil.which",
        lambda _name: "/fake/bin/soffice",
    )

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    monkeypatch.setattr(
        "j1.providers.raganything._bridge.subprocess.run", fake_run,
    )
    with pytest.raises(_LibreOfficeConversionError, match="timed out"):
        _convert_to_pdf(source, settings)


# ---- End-to-end: default_compile invokes conversion ---------------


def test_default_compile_pre_converts_legacy_doc_then_calls_raganything(
    tmp_path, monkeypatch,
):
    """When the registered raw document is `.doc`, the bridge converts
    to PDF first, then hands the converted path to raganything's
    `process_document_complete`. The original `.doc` path is never
    seen by raganything."""
    import sys
    import types

    from j1.providers.raganything.compiler import RAGAnythingCompileRequest
    from j1.providers.raganything._bridge import default_compile

    # --- Fake raganything module ---
    captured: dict = {}

    class _FakeConfig:
        def __init__(self, **kwargs): pass

    class _FakeRAG:
        def __init__(self, **kwargs): pass

        async def _ensure_lightrag_initialized(self):
            return {"success": True}

        async def process_document_complete(
            self, *, file_path, output_dir, parse_method, **_extra,
        ):
            # `**_extra` swallows backend / vlm_url that the bridge
            # now forwards by default (since J1 forces vlm-http-client).
            captured["compile_file_path"] = file_path
            captured["compile_parse_method"] = parse_method
            outdir = Path(output_dir)
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "out.md").write_text("ok")

    fake_mod = types.ModuleType("raganything")
    fake_mod.RAGAnything = _FakeRAG
    fake_mod.RAGAnythingConfig = _FakeConfig
    monkeypatch.setitem(sys.modules, "raganything", fake_mod)

    # --- Workspace + a fake .doc on disk ---
    monkeypatch.setenv("J1_DATA_ROOT", str(tmp_path))
    raw_dir = tmp_path / "tenants" / "acme" / "projects" / "alpha" / "raw"
    raw_dir.mkdir(parents=True)
    doc_path = raw_dir / "doc-1.doc"
    doc_path.write_bytes(b"\x00\x01\x02OLE2 fake legacy doc")

    # --- Mock soffice ---
    monkeypatch.setattr(
        "j1.providers.raganything._bridge.shutil.which",
        lambda _name: "/fake/bin/soffice",
    )

    def fake_run(argv, **kwargs):
        outdir = Path(argv[argv.index("--outdir") + 1])
        (outdir / "doc-1.pdf").write_bytes(b"%PDF-1.4 converted")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(
        "j1.providers.raganything._bridge.subprocess.run", fake_run,
    )

    # --- Drive the bridge ---
    workdir = tmp_path / "rag-workdir"
    settings = load_raganything_settings(env={
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_RAGANYTHING_WORKDIR": str(workdir),
    })

    class _FakeText:
        def generate(self, prompt): return ("ok", None)

    class _FakeEmbed:
        def embed_batch(self, texts): return ([[0.0] * 8] * len(texts), None)
        def dimension(self): return 8
        def max_tokens(self): return 512

    from j1.projects.context import ProjectContext

    request = RAGAnythingCompileRequest(
        ctx=ProjectContext(tenant_id="acme", project_id="alpha"),
        document_id="doc-1",
        settings=settings,
        text_client=_FakeText(),
        vision_client=None,
        embedding_client=_FakeEmbed(),
    )

    result = default_compile(request)

    # raganything was handed the CONVERTED PDF, not the original .doc.
    assert captured["compile_file_path"].endswith(".pdf"), (
        f"expected raganything to receive the converted PDF, got {captured['compile_file_path']}"
    )
    assert "doc-1.doc" not in captured["compile_file_path"]
    # And the compile succeeded.
    from j1.processing.results import ResultStatus
    assert result.status is ResultStatus.SUCCEEDED


def test_default_compile_passes_through_native_pdf_unchanged(
    tmp_path, monkeypatch,
):
    """For natively-supported formats, the bridge MUST NOT invoke
    LibreOffice — extension isn't on the convert list, so no
    subprocess call should happen."""
    import sys
    import types

    from j1.providers.raganything.compiler import RAGAnythingCompileRequest
    from j1.providers.raganything._bridge import default_compile

    captured: dict = {}

    class _FakeConfig:
        def __init__(self, **kwargs): pass

    class _FakeRAG:
        def __init__(self, **kwargs): pass

        async def _ensure_lightrag_initialized(self):
            return {"success": True}

        async def process_document_complete(
            self, *, file_path, output_dir, parse_method, **_extra,
        ):
            # `**_extra` swallows backend / vlm_url forwarded by the
            # bridge in the default vlm-http-client mode.
            captured["compile_file_path"] = file_path
            outdir = Path(output_dir)
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "out.md").write_text("ok")

    fake_mod = types.ModuleType("raganything")
    fake_mod.RAGAnything = _FakeRAG
    fake_mod.RAGAnythingConfig = _FakeConfig
    monkeypatch.setitem(sys.modules, "raganything", fake_mod)

    monkeypatch.setenv("J1_DATA_ROOT", str(tmp_path))
    raw_dir = tmp_path / "tenants" / "acme" / "projects" / "alpha" / "raw"
    raw_dir.mkdir(parents=True)
    pdf_path = raw_dir / "doc-1.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 native pdf")

    # If the bridge mistakenly invoked subprocess.run, fail loudly.
    def must_not_be_called(*args, **kwargs):
        raise AssertionError(
            "subprocess.run was invoked for a native-PDF source — "
            "conversion path should be skipped"
        )
    monkeypatch.setattr(
        "j1.providers.raganything._bridge.subprocess.run", must_not_be_called,
    )

    workdir = tmp_path / "rag-workdir"
    settings = load_raganything_settings(env={
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_RAGANYTHING_WORKDIR": str(workdir),
    })

    class _FakeText:
        def generate(self, prompt): return ("ok", None)

    class _FakeEmbed:
        def embed_batch(self, texts): return ([[0.0] * 8] * len(texts), None)
        def dimension(self): return 8
        def max_tokens(self): return 512

    from j1.projects.context import ProjectContext

    request = RAGAnythingCompileRequest(
        ctx=ProjectContext(tenant_id="acme", project_id="alpha"),
        document_id="doc-1",
        settings=settings,
        text_client=_FakeText(),
        vision_client=None,
        embedding_client=_FakeEmbed(),
    )

    result = default_compile(request)

    # PDF was passed through unchanged.
    assert captured["compile_file_path"].endswith("doc-1.pdf")
    from j1.processing.results import ResultStatus
    assert result.status is ResultStatus.SUCCEEDED


# ---- Regression: LightRAG init failure surfaces as ProviderUnavailable -


def test_default_compile_surfaces_lightrag_init_failure_as_provider_unavailable(
    tmp_path, monkeypatch,
):
    """RAGAnything's `_ensure_lightrag_initialized` returns
    `{"success": False, "error": ...}` on failure WITHOUT raising,
    then `process_document_complete` proceeds with `self.lightrag = None`
    and crashes downstream as `'NoneType' object has no attribute 'ainsert'`.
    The bridge must intercept the init result and raise a clear
    `ProviderUnavailable` instead, so operators see the real cause."""
    import sys
    import types

    from j1.providers.raganything._bridge import default_compile
    from j1.providers.raganything.compiler import RAGAnythingCompileRequest

    class _FakeConfig:
        def __init__(self, **kwargs): pass

    class _FakeRAG:
        def __init__(self, **kwargs): pass

        async def _ensure_lightrag_initialized(self):
            return {"success": False, "error": "embedding_func must be provided"}

        async def process_document_complete(self, **_):
            raise AssertionError(
                "process_document_complete should NOT be called when init fails"
            )

    fake_mod = types.ModuleType("raganything")
    fake_mod.RAGAnything = _FakeRAG
    fake_mod.RAGAnythingConfig = _FakeConfig
    monkeypatch.setitem(sys.modules, "raganything", fake_mod)

    monkeypatch.setenv("J1_DATA_ROOT", str(tmp_path))
    raw_dir = tmp_path / "tenants" / "acme" / "projects" / "alpha" / "raw"
    raw_dir.mkdir(parents=True)
    (raw_dir / "doc-1.pdf").write_bytes(b"%PDF-1.4 native pdf")

    from j1.projects.context import ProjectContext

    class _FakeText:
        def generate(self, prompt): return ("ok", None)

    class _FakeEmbed:
        def embed_batch(self, texts): return ([[0.0] * 8] * len(texts), None)
        def dimension(self): return 8
        def max_tokens(self): return 512

    settings = load_raganything_settings(env={
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_RAGANYTHING_WORKDIR": str(tmp_path / "rag-workdir"),
    })
    request = RAGAnythingCompileRequest(
        ctx=ProjectContext(tenant_id="acme", project_id="alpha"),
        document_id="doc-1",
        settings=settings,
        text_client=_FakeText(),
        vision_client=None,
        embedding_client=_FakeEmbed(),
    )

    with pytest.raises(ProviderUnavailable, match="embedding_func must be provided"):
        default_compile(request)


# ---- Regression: embedding wrapper has the LightRAG-required shape ----


def test_make_embedding_func_inner_callable_returns_numpy_array():
    """LightRAG's `EmbeddingFunc.__call__` reads `result.size` to
    validate vector count — a Python `list[list[float]]` raises
    `AttributeError: 'list' object has no attribute 'size'`. Our
    wrapper must return an ndarray shaped (n_texts, dim).
    Regression test for the LightRAG-AttributeError seen in prod."""
    import asyncio

    import numpy as np

    from j1.providers.raganything._bridge import _make_embedding_func

    class _FakeEmbed:
        def embed_batch(self, texts):
            return ([[0.1, 0.2, 0.3, 0.4]] * len(list(texts)), None)
        def dimension(self): return 4
        def max_tokens(self): return 256

    wrapped = _make_embedding_func(_FakeEmbed())
    result = asyncio.run(wrapped.func(["hi", "there", "y'all"]))
    assert isinstance(result, np.ndarray), (
        f"expected numpy ndarray (LightRAG reads .size on it), got {type(result).__name__}"
    )
    assert result.dtype == np.float32
    assert result.shape == (3, 4)
    # And it satisfies LightRAG's modulo check: size % embedding_dim == 0.
    assert result.size % 4 == 0


def test_make_embedding_func_returns_lightrag_embeddingfunc_with_dim_and_max_tokens():
    """LightRAG's `LightRAG.__post_init__` accesses `.embedding_dim`,
    `.max_token_size`, and `.func` on the `embedding_func` argument.
    Passing a plain callable causes init to silently fail and the
    AttributeError-on-None downstream. Verify our wrapper exposes the
    full `lightrag.utils.EmbeddingFunc` shape and forwards `dimension()` /
    `max_tokens()` from the J1 client."""
    from lightrag.utils import EmbeddingFunc

    from j1.providers.raganything._bridge import _make_embedding_func

    class _FakeEmbed:
        def embed_batch(self, texts): return ([[0.1] * 8] * len(texts), None)
        def dimension(self): return 8
        def max_tokens(self): return 4096

    wrapped = _make_embedding_func(_FakeEmbed())
    assert isinstance(wrapped, EmbeddingFunc)
    assert wrapped.embedding_dim == 8
    assert wrapped.max_token_size == 4096
    assert callable(wrapped.func)


# ---- Regression: plain-text fast path bypasses mineru entirely --------


def test_default_compile_short_circuits_plain_text_to_lightrag_ainsert(
    tmp_path, monkeypatch,
):
    """`.txt` files MUST NOT go through `process_document_complete` —
    mineru renders text → PDF → runs the full PyTorch model pipeline
    on the result, which pegs every CPU core even on a 10-byte file.
    The bridge reads the text and calls `lightrag.ainsert` directly."""
    import sys
    import types

    from j1.providers.raganything._bridge import default_compile
    from j1.providers.raganything.compiler import RAGAnythingCompileRequest

    captured: dict = {"ainsert_calls": [], "process_called": False}

    class _FakeLightRAG:
        async def ainsert(self, *, input, file_paths=None, ids=None, **_):
            captured["ainsert_calls"].append(
                {"input": input, "file_paths": file_paths, "ids": ids},
            )

    class _FakeConfig:
        def __init__(self, **_): pass

    class _FakeRAG:
        def __init__(self, **_):
            self.lightrag = _FakeLightRAG()

        async def _ensure_lightrag_initialized(self):
            return {"success": True}

        async def process_document_complete(self, **_):
            captured["process_called"] = True

    fake_mod = types.ModuleType("raganything")
    fake_mod.RAGAnything = _FakeRAG
    fake_mod.RAGAnythingConfig = _FakeConfig
    monkeypatch.setitem(sys.modules, "raganything", fake_mod)

    monkeypatch.setenv("J1_DATA_ROOT", str(tmp_path))
    raw_dir = tmp_path / "tenants" / "acme" / "projects" / "alpha" / "raw"
    raw_dir.mkdir(parents=True)
    txt_path = raw_dir / "doc-1.txt"
    txt_path.write_text("hello world", encoding="utf-8")

    from j1.projects.context import ProjectContext

    class _FakeText:
        def generate(self, prompt): return ("ok", None)

    class _FakeEmbed:
        def embed_batch(self, texts): return ([[0.0] * 8] * len(texts), None)
        def dimension(self): return 8
        def max_tokens(self): return 512

    settings = load_raganything_settings(env={
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_RAGANYTHING_WORKDIR": str(tmp_path / "rag-workdir"),
    })
    request = RAGAnythingCompileRequest(
        ctx=ProjectContext(tenant_id="acme", project_id="alpha"),
        document_id="doc-1",
        settings=settings,
        text_client=_FakeText(),
        vision_client=None,
        embedding_client=_FakeEmbed(),
    )

    result = default_compile(request)

    # mineru pathway must be untouched.
    assert captured["process_called"] is False, (
        "process_document_complete was called for a .txt file — "
        "the plain-text fast path didn't fire"
    )
    # ainsert was called with the raw text and J1's document_id.
    assert len(captured["ainsert_calls"]) == 1
    call = captured["ainsert_calls"][0]
    assert call["input"] == "hello world"
    assert call["ids"] == "doc-1"
    assert call["file_paths"].endswith("doc-1.txt")
    # Compile reports success and emits a draft from the output dir.
    from j1.processing.results import ResultStatus
    assert result.status is ResultStatus.SUCCEEDED
    assert any(d.content == b"hello world" for d in result.drafts)


def test_default_compile_skips_ainsert_for_empty_text_file(
    tmp_path, monkeypatch,
):
    """Empty / whitespace-only files must not produce a no-op LightRAG
    chunk — mirror raganything's own behaviour and skip the insert."""
    import sys
    import types

    from j1.providers.raganything._bridge import default_compile
    from j1.providers.raganything.compiler import RAGAnythingCompileRequest

    ainsert_calls: list = []

    class _FakeLightRAG:
        async def ainsert(self, **kwargs):
            ainsert_calls.append(kwargs)

    class _FakeConfig:
        def __init__(self, **_): pass

    class _FakeRAG:
        def __init__(self, **_):
            self.lightrag = _FakeLightRAG()

        async def _ensure_lightrag_initialized(self):
            return {"success": True}

        async def process_document_complete(self, **_):
            raise AssertionError("must not be called for plain text")

    fake_mod = types.ModuleType("raganything")
    fake_mod.RAGAnything = _FakeRAG
    fake_mod.RAGAnythingConfig = _FakeConfig
    monkeypatch.setitem(sys.modules, "raganything", fake_mod)

    monkeypatch.setenv("J1_DATA_ROOT", str(tmp_path))
    raw_dir = tmp_path / "tenants" / "acme" / "projects" / "alpha" / "raw"
    raw_dir.mkdir(parents=True)
    (raw_dir / "doc-1.md").write_text("   \n  \t\n", encoding="utf-8")

    from j1.projects.context import ProjectContext

    class _FakeText:
        def generate(self, prompt): return ("", None)

    class _FakeEmbed:
        def embed_batch(self, texts): return ([[0.0]] * len(list(texts)), None)
        def dimension(self): return 1
        def max_tokens(self): return 4

    settings = load_raganything_settings(env={
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://stub-vlm:1234/v1",
        "J1_RAGANYTHING_WORKDIR": str(tmp_path / "rag-workdir"),
    })
    request = RAGAnythingCompileRequest(
        ctx=ProjectContext(tenant_id="acme", project_id="alpha"),
        document_id="doc-1",
        settings=settings,
        text_client=_FakeText(),
        vision_client=None,
        embedding_client=_FakeEmbed(),
    )

    result = default_compile(request)

    assert ainsert_calls == [], "empty file produced an ainsert call"
    from j1.processing.results import ResultStatus
    assert result.status is ResultStatus.SUCCEEDED


def test_make_text_callable_returns_async_callable_that_unwraps_usage():
    """LightRAG `await`s the llm_model_func — the wrapper must be async,
    must invoke `text_client.generate(prompt, system_prompt=...)`, and
    must drop the usage half of the (text, usage) tuple.

    The fake matches `TextLLMClient.generate`'s real signature
    (`prompt` + keyword `system_prompt`) so the wrapper's kwarg-
    forwarding path is exercised. Accepting `**_` lets the fake
    tolerate any future kwargs the wrapper learns to forward
    without needing to update this test."""
    import asyncio

    from j1.providers.raganything._bridge import _make_text_callable

    seen = []

    class _FakeText:
        def generate(self, prompt, *, system_prompt=None, **_):
            seen.append({"prompt": prompt, "system_prompt": system_prompt})
            return ("hello back", {"input_tokens": 1})

    callable_ = _make_text_callable(_FakeText())
    result = asyncio.run(callable_("hi there"))
    assert result == "hello back"
    # Wrapper prepends `/no_think` to both system + user prompts
    # (qwen3 reasoning suppression); original user content must
    # still be present unmodified.
    assert len(seen) == 1
    assert "hi there" in seen[0]["prompt"]
    assert "/no_think" in seen[0]["prompt"]
    # system_prompt becomes "/no_think" when caller didn't set one,
    # otherwise it's prepended.
    assert seen[0]["system_prompt"] == "/no_think"
