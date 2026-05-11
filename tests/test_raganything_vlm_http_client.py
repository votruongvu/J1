"""Tests for the VLM-HTTP-client wiring in the RAGAnything bridge.

MinerU separates the PDF parse method (CLI -m, values
{auto, txt, ocr}) from the inference backend (CLI -b, values
{pipeline, vlm-http-client, hybrid-http-client, vlm-auto-engine,
hybrid-auto-engine}). The user-facing knob for offloading VLM is
`J1_RAGANYTHING_BACKEND=vlm-http-client`; misusing
`J1_RAGANYTHING_PARSE_METHOD=vlm-http-client` is rejected at
settings-load with a clear migration error.

When backend=vlm-http-client, the bridge propagates the operator's
vision-LLM config into the env vars MinerU's
`mineru_vl_utils.MinerUClient` reads:
 * `MINERU_VL_SERVER` (also overridable via the `-u` CLI flag,
 forwarded as the `vlm_url` kwarg)
 * `MINERU_VL_API_KEY`
 * `MINERU_VL_MODEL_NAME`

Without the env propagation the request reaches LM Studio with no
Authorization header and an auto-detected model name. With it, the
existing `J1_VISION_LLM_*` env vars are the only thing the operator
needs — flipping the backend env var is the sole additional change.
"""

from __future__ import annotations

import os

import pytest

from j1.providers.raganything._bridge import _apply_vlm_http_client_env
from j1.providers.raganything.settings import (
    RAGAnythingSettings,
    load_raganything_settings,
)


# ---- Settings loader fallback chain --------------------------------


def test_settings_inherit_vlm_url_from_j1_vision_when_unset():
    """The operator typically only sets `J1_VISION_LLM_BASE_URL` for
 the rest of the stack. Reading the same value into the
 raganything settings means flipping parse_method is the only
 additional change required."""
    s = load_raganything_settings(env={
        "J1_VISION_LLM_BASE_URL": "http://host.docker.internal:1234/v1",
        "J1_VISION_LLM_API_KEY": "lm-studio",
        "J1_VISION_LLM_MODEL": "gemma-4-e4b",
    })
    assert s.vlm_http_server_url == "http://host.docker.internal:1234/v1"
    assert s.vlm_http_api_key == "lm-studio"
    assert s.vlm_http_model_name == "gemma-4-e4b"


def test_settings_explicit_vlm_overrides_j1_vision():
    """When MinerU should hit a DIFFERENT VLM than the project-wide
 vision LLM (e.g. a faster but less-accurate model just for
 layout), the explicit `J1_RAGANYTHING_VLM_HTTP_*` vars win."""
    s = load_raganything_settings(env={
        "J1_VISION_LLM_BASE_URL": "http://primary:1234/v1",
        "J1_VISION_LLM_API_KEY": "primary-key",
        "J1_VISION_LLM_MODEL": "primary-model",
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://mineru:9999/v1",
        "J1_RAGANYTHING_VLM_HTTP_API_KEY": "mineru-key",
        "J1_RAGANYTHING_VLM_HTTP_MODEL_NAME": "mineru-model",
    })
    assert s.vlm_http_server_url == "http://mineru:9999/v1"
    assert s.vlm_http_api_key == "mineru-key"
    assert s.vlm_http_model_name == "mineru-model"


def test_settings_load_rejects_when_no_vlm_url_set():
    """J1 forces MinerU into HTTP-client mode and refuses to start
 without an externally-managed VLM endpoint. An empty env (no
 `J1_RAGANYTHING_VLM_HTTP_SERVER_URL` and no `J1_VISION_LLM_BASE_URL`
 fallback) must fail at load with `ConfigError` — operators get
 a clear migration message instead of a mid-compile crash."""
    from j1.errors.exceptions import ConfigError
    with pytest.raises(ConfigError) as excinfo:
        load_raganything_settings(env={})
    msg = str(excinfo.value)
    assert "VLM server URL" in msg
    assert "J1_RAGANYTHING_VLM_HTTP_SERVER_URL" in msg


def test_settings_load_rejects_when_vlm_url_blank():
    """An operator who exports `J1_VISION_LLM_BASE_URL=` (empty
 string) must hit the same rejection as the unset case —
 whitespace-only strings are normalised to "no URL"."""
    from j1.errors.exceptions import ConfigError
    with pytest.raises(ConfigError):
        load_raganything_settings(env={
            "J1_VISION_LLM_BASE_URL": "",
        })


# ---- _apply_vlm_http_client_env behaviour ---------------------------


@pytest.fixture(autouse=True)
def _isolate_mineru_env(monkeypatch):
    """Each test starts with a clean MinerU env so leakage between
 tests can't paper over a real bug."""
    for name in ("MINERU_VL_SERVER", "MINERU_VL_API_KEY", "MINERU_VL_MODEL_NAME"):
        monkeypatch.delenv(name, raising=False)


def test_apply_is_noop_when_backend_is_unset():
    """The default backend (None) lets MinerU pick its own engine —
 typically a local one. The bridge must NOT consult VLM env vars
 in that case; setting them could accidentally break local runs
 where mineru's code branches on env-var presence."""
    settings = RAGAnythingSettings(
        parse_method="auto",
        backend=None,
        vlm_http_server_url="http://x:1234",
        vlm_http_api_key="k",
        vlm_http_model_name="m",
    )
    _apply_vlm_http_client_env(settings)
    assert os.environ.get("MINERU_VL_SERVER") is None
    assert os.environ.get("MINERU_VL_API_KEY") is None
    assert os.environ.get("MINERU_VL_MODEL_NAME") is None


def test_apply_is_noop_for_local_backends():
    """Pipeline / vlm-auto-engine / hybrid-auto-engine all run the
 VLM locally — they shouldn't touch the HTTP client env vars."""
    for backend in ("pipeline", "vlm-auto-engine", "hybrid-auto-engine"):
        settings = RAGAnythingSettings(
            parse_method="auto",
            backend=backend,
            vlm_http_server_url="http://x:1234",
            vlm_http_api_key="k",
            vlm_http_model_name="m",
        )
        _apply_vlm_http_client_env(settings)
        assert os.environ.get("MINERU_VL_SERVER") is None, backend


def test_apply_sets_env_vars_for_vlm_http_client():
    settings = RAGAnythingSettings(
        parse_method="auto",
        backend="vlm-http-client",
        vlm_http_server_url="http://host.docker.internal:1234/v1",
        vlm_http_api_key="lm-studio",
        vlm_http_model_name="gemma-4-e4b",
    )
    _apply_vlm_http_client_env(settings)
    assert os.environ["MINERU_VL_SERVER"] == "http://host.docker.internal:1234/v1"
    assert os.environ["MINERU_VL_API_KEY"] == "lm-studio"
    assert os.environ["MINERU_VL_MODEL_NAME"] == "gemma-4-e4b"


def test_apply_skips_unset_fields():
    """A field of None means 'don't touch this env var' — preserves
 operator-supplied values and lets MinerU fall through to its
 own defaults (e.g. auto-detect model from /v1/models)."""
    settings = RAGAnythingSettings(
        parse_method="auto",
        backend="vlm-http-client",
        vlm_http_server_url="http://x:1234",
        vlm_http_api_key=None,
        vlm_http_model_name=None,
    )
    _apply_vlm_http_client_env(settings)
    assert os.environ["MINERU_VL_SERVER"] == "http://x:1234"
    assert "MINERU_VL_API_KEY" not in os.environ
    assert "MINERU_VL_MODEL_NAME" not in os.environ


def test_apply_does_not_overwrite_operator_supplied_env(monkeypatch):
    """If the operator explicitly exported `MINERU_VL_*` env vars
 (e.g. wrapping the worker with a different VLM than J1's vision
 role), the settings-derived values must NOT shadow them.
 Operator intent always wins."""
    monkeypatch.setenv("MINERU_VL_SERVER", "http://operator-set:7777")
    monkeypatch.setenv("MINERU_VL_MODEL_NAME", "operator-pinned")
    settings = RAGAnythingSettings(
        parse_method="auto",
        backend="vlm-http-client",
        vlm_http_server_url="http://settings-supplied:1234",
        vlm_http_api_key="settings-key",
        vlm_http_model_name="settings-model",
    )
    _apply_vlm_http_client_env(settings)
    # Operator-set values survive.
    assert os.environ["MINERU_VL_SERVER"] == "http://operator-set:7777"
    assert os.environ["MINERU_VL_MODEL_NAME"] == "operator-pinned"
    # API key wasn't operator-set → settings value applied.
    assert os.environ["MINERU_VL_API_KEY"] == "settings-key"


def test_apply_idempotent_when_called_twice():
    """The bridge calls this from both `default_compile` and
 `default_build_graph` per request. Calling it twice in a row
 must not blow up or change the env state on the second call."""
    settings = RAGAnythingSettings(
        parse_method="auto",
        backend="vlm-http-client",
        vlm_http_server_url="http://x:1234",
        vlm_http_api_key="k",
        vlm_http_model_name="m",
    )
    _apply_vlm_http_client_env(settings)
    first = dict(os.environ)
    _apply_vlm_http_client_env(settings)
    assert os.environ["MINERU_VL_SERVER"] == first["MINERU_VL_SERVER"]
    assert os.environ["MINERU_VL_API_KEY"] == first["MINERU_VL_API_KEY"]
    assert os.environ["MINERU_VL_MODEL_NAME"] == first["MINERU_VL_MODEL_NAME"]


# ---- Settings validation: catches the misuse the user just hit ----


def test_parse_method_rejects_backend_value_with_migration_message():
    """Operators previously told to set
 `J1_RAGANYTHING_PARSE_METHOD=vlm-http-client` (incorrect — that's
 a backend value) must get a clear migration error at startup
 instead of mineru's cryptic 'Invalid value for -m' mid-compile."""
    with pytest.raises(ValueError) as excinfo:
        load_raganything_settings(env={
            "J1_RAGANYTHING_PARSE_METHOD": "vlm-http-client",
        })
    msg = str(excinfo.value)
    assert "BACKEND value" in msg
    assert "J1_RAGANYTHING_BACKEND" in msg
    assert "vlm-http-client" in msg


def test_parse_method_rejects_unknown_value():
    with pytest.raises(ValueError) as excinfo:
        load_raganything_settings(env={
            "J1_RAGANYTHING_PARSE_METHOD": "garbage",
        })
    assert "garbage" in str(excinfo.value)
    assert "auto" in str(excinfo.value)


def test_backend_rejects_unknown_value():
    from j1.errors.exceptions import ConfigError
    with pytest.raises(ConfigError) as excinfo:
        load_raganything_settings(env={
            "J1_RAGANYTHING_BACKEND": "made-up-engine",
            "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://x:1/v1",
        })
    assert "made-up-engine" in str(excinfo.value)


def test_backend_defaults_to_vlm_http_client_when_unset():
    """J1 forces external-only operation — the loader's default is
 `vlm-http-client` so an operator who exports just the VLM URL
 gets a working setup with no extra config."""
    s = load_raganything_settings(env={
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://x:1/v1",
    })
    assert s.backend == "vlm-http-client"


def test_backend_accepts_only_external_http_client_variants():
    """`vlm-http-client` and `hybrid-http-client` are the only
 permitted backends. The local-model variants (`pipeline` /
 `vlm-auto-engine` / `hybrid-auto-engine`) are rejected at load
 time with an actionable migration message — no surprise
 multi-gigabyte HF downloads inside the worker."""
    from j1.errors.exceptions import ConfigError
    for backend in ("vlm-http-client", "hybrid-http-client"):
        s = load_raganything_settings(env={
            "J1_RAGANYTHING_BACKEND": backend,
            "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://x:1/v1",
        })
        assert s.backend == backend
    for rejected in ("pipeline", "vlm-auto-engine", "hybrid-auto-engine"):
        with pytest.raises(ConfigError) as excinfo:
            load_raganything_settings(env={
                "J1_RAGANYTHING_BACKEND": rejected,
                "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://x:1/v1",
            })
        msg = str(excinfo.value)
        assert "local model" in msg.lower()
        assert "vlm-http-client" in msg or "hybrid-http-client" in msg


def test_parse_method_accepts_all_documented_values():
    for method in ("auto", "txt", "ocr"):
        s = load_raganything_settings(env={
            "J1_RAGANYTHING_PARSE_METHOD": method,
            "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://x:1/v1",
        })
        assert s.parse_method == method
