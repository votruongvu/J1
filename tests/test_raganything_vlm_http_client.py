"""Tests for the VLM-HTTP-client wiring in the RAGAnything bridge.

When `J1_RAGANYTHING_PARSE_METHOD=vlm-http-client` is set, the bridge
must propagate the operator's vision-LLM config into the env vars
MinerU's `mineru_vl_utils.MinerUClient` reads:
  * `MINERU_VL_SERVER`
  * `MINERU_VL_API_KEY`
  * `MINERU_VL_MODEL_NAME`

Without this, switching parse_method on its own gives operators a
'RequestError: Invalid server URL' from mineru-vl-utils. With it,
the existing `J1_VISION_LLM_*` env vars are the only thing the
operator needs to set — flipping parse_method routes MinerU through
the same LM Studio / vLLM / hosted endpoint already serving the J1
vision role.
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


def test_settings_vlm_fields_default_to_none_when_neither_set():
    """When neither `J1_VISION_LLM_*` nor `J1_RAGANYTHING_VLM_HTTP_*`
    is set, the fields stay None — `_apply_vlm_http_client_env` will
    skip applying anything, so MinerU falls back to its own defaults
    (typically the public hosted endpoint)."""
    s = load_raganything_settings(env={})
    assert s.vlm_http_server_url is None
    assert s.vlm_http_api_key is None
    assert s.vlm_http_model_name is None


def test_settings_blank_vlm_url_treated_as_unset():
    """An operator who exports `J1_VISION_LLM_BASE_URL=` (empty
    string) shouldn't end up with a literal empty server URL — the
    loader normalises empty to None."""
    s = load_raganything_settings(env={
        "J1_VISION_LLM_BASE_URL": "",
    })
    assert s.vlm_http_server_url is None


# ---- _apply_vlm_http_client_env behaviour ---------------------------


@pytest.fixture(autouse=True)
def _isolate_mineru_env(monkeypatch):
    """Each test starts with a clean MinerU env so leakage between
    tests can't paper over a real bug."""
    for name in ("MINERU_VL_SERVER", "MINERU_VL_API_KEY", "MINERU_VL_MODEL_NAME"):
        monkeypatch.delenv(name, raising=False)


def test_apply_is_noop_when_parse_method_is_auto():
    """The default `auto` parse method runs MinerU's hybrid backend
    locally and must NOT consult these env vars. Setting them when
    not needed could accidentally break local-mode runs (mineru's
    code paths sometimes branch on env-var presence)."""
    settings = RAGAnythingSettings(
        parse_method="auto",
        vlm_http_server_url="http://x:1234",
        vlm_http_api_key="k",
        vlm_http_model_name="m",
    )
    _apply_vlm_http_client_env(settings)
    assert os.environ.get("MINERU_VL_SERVER") is None
    assert os.environ.get("MINERU_VL_API_KEY") is None
    assert os.environ.get("MINERU_VL_MODEL_NAME") is None


def test_apply_sets_env_vars_for_vlm_http_client():
    settings = RAGAnythingSettings(
        parse_method="vlm-http-client",
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
        parse_method="vlm-http-client",
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
        parse_method="vlm-http-client",
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
        parse_method="vlm-http-client",
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
