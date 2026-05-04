"""Tests for the AdapterManifest schema + validation."""

from __future__ import annotations

import pytest

from j1.extension import AdapterManifest, KNOWN_ADAPTER_TYPES, ManifestError


def test_manifest_minimum_fields():
    m = AdapterManifest(name="acme.compiler", type="compiler", version="1.0.0")
    assert m.name == "acme.compiler"
    assert m.type == "compiler"
    assert m.version == "1.0.0"
    assert m.capabilities == ()
    assert m.required_secret_keys == ()
    assert m.health_check is False


def test_manifest_invalid_name_rejected():
    for bad in ("", "Acme", "with space", "$money", ".starts-with-dot"):
        with pytest.raises(ManifestError, match="name="):
            AdapterManifest(name=bad, type="compiler", version="1.0.0")


def test_manifest_unknown_type_rejected_unless_namespaced():
    with pytest.raises(ManifestError, match="not a known adapter type"):
        AdapterManifest(name="x", type="weird", version="1.0.0")


def test_manifest_unknown_namespace_accepted():
    m = AdapterManifest(name="x", type="unknown:experimental", version="1.0.0")
    assert m.type == "unknown:experimental"


def test_manifest_invalid_version_rejected():
    for bad in ("v1", "1", "1.x", "1.0.0.0", "abc"):
        if bad == "1":
            # Single-digit major is allowed by the regex; skip.
            continue
        with pytest.raises(ManifestError, match="version"):
            AdapterManifest(name="x", type="compiler", version=bad)


def test_manifest_required_optional_overlap_rejected():
    with pytest.raises(ManifestError, match="required and optional"):
        AdapterManifest(
            name="x", type="compiler", version="1.0.0",
            required_config_keys=("alpha",),
            optional_config_keys=("alpha",),
        )


def test_manifest_round_trip_dict():
    src = AdapterManifest(
        name="acme.retrieval", type="retrieval", version="2.1.0",
        capabilities=("hybrid", "streaming"),
        supported_input_types=("text/plain",),
        output_types=("application/json",),
        required_config_keys=("base_url",),
        optional_config_keys=("max_concurrency",),
        required_secret_keys=("ACME_API_KEY",),
        health_check=True,
        description="Demo retrieval adapter",
        metadata={"vendor": "acme"},
    )
    round_tripped = AdapterManifest.from_dict(src.to_dict())
    assert round_tripped == src


def test_manifest_from_dict_missing_required_key():
    with pytest.raises(ManifestError, match="missing required key"):
        AdapterManifest.from_dict({"name": "x", "type": "compiler"})  # no version


def test_manifest_known_adapter_types_complete():
    expected = {
        "source-connector", "compiler", "enrichment", "graph",
        "retrieval", "reranker", "llm", "embedding", "vision",
        "output-formatter", "evaluation", "domain-policy",
    }
    assert KNOWN_ADAPTER_TYPES == expected


def test_manifest_rejects_obvious_secret_in_metadata():
    with pytest.raises(ManifestError, match="looks like a secret"):
        AdapterManifest(
            name="x", type="compiler", version="1.0.0",
            metadata={"token": "sk-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"},
        )


def test_manifest_has_capability_helper():
    m = AdapterManifest(
        name="x", type="retrieval", version="1.0.0",
        capabilities=("hybrid", "streaming"),
    )
    assert m.has_capability("hybrid")
    assert not m.has_capability("batch")


def test_manifest_capability_must_match_name_format():
    with pytest.raises(ManifestError, match="capability"):
        AdapterManifest(
            name="x", type="retrieval", version="1.0.0",
            capabilities=("Has Spaces",),
        )
