"""Tests for the CapabilityRegistry."""

from __future__ import annotations

import pytest

from j1.extension import (
    AdapterManifest,
    CapabilityRegistry,
    ManifestError,
    RegistryError,
)
from j1.extension.mocks import (
    MockCompilerAdapter,
    MockRetrievalAdapter,
)


def test_register_and_get():
    reg = CapabilityRegistry()
    adapter = MockCompilerAdapter()
    entry = reg.register(adapter.MANIFEST, adapter)
    assert entry.adapter is adapter
    assert reg.get("compiler", "mock") is entry
    assert ("compiler", "mock") in reg
    assert len(reg) == 1


def test_register_rejects_non_manifest():
    reg = CapabilityRegistry()
    with pytest.raises(ManifestError):
        reg.register({"name": "x"}, MockCompilerAdapter())  # type: ignore[arg-type]


def test_duplicate_registration_rejected():
    reg = CapabilityRegistry()
    a1 = MockCompilerAdapter()
    a2 = MockCompilerAdapter()
    reg.register(a1.MANIFEST, a1)
    with pytest.raises(RegistryError, match="already registered"):
        reg.register(a2.MANIFEST, a2)


def test_kind_disagreement_rejected():
    """If `adapter.kind` disagrees with `manifest.name`, the registry
    refuses to index the entry — would otherwise create a confusing
    two-identity adapter."""
    reg = CapabilityRegistry()
    adapter = MockCompilerAdapter(kind="confused")
    with pytest.raises(RegistryError, match="kind="):
        reg.register(adapter.MANIFEST, adapter)


def test_unregister_silent_when_absent():
    reg = CapabilityRegistry()
    reg.unregister("compiler", "nope")  # no-op


def test_unregister_removes_from_indexes():
    reg = CapabilityRegistry()
    adapter = MockRetrievalAdapter()
    reg.register(adapter.MANIFEST, adapter, role="hybrid-retrieval")
    assert reg.find_by_type("retrieval")
    assert reg.find_by_role("hybrid-retrieval")
    reg.unregister("retrieval", "mock")
    assert not reg.find_by_type("retrieval")
    assert not reg.find_by_role("hybrid-retrieval")


def test_lookups_by_type_capability_role():
    reg = CapabilityRegistry()
    compiler = MockCompilerAdapter()
    retrieval = MockRetrievalAdapter()
    reg.register(compiler.MANIFEST, compiler, role="primary-compile")
    reg.register(retrieval.MANIFEST, retrieval, role="primary-retrieve")

    assert len(reg.find_by_type("compiler")) == 1
    assert len(reg.find_by_type("retrieval")) == 1
    assert len(reg.find_by_type("graph")) == 0

    assert len(reg.find_by_capability("text")) == 1
    assert len(reg.find_by_capability("synchronous")) == 1
    assert len(reg.find_by_capability("nope")) == 0

    assert len(reg.find_by_role("primary-compile")) == 1
    assert len(reg.find_by_role("primary-retrieve")) == 1
    assert len(reg.find_by_role("missing")) == 0


def test_require_raises_when_missing():
    reg = CapabilityRegistry()
    with pytest.raises(RegistryError, match="no adapter registered"):
        reg.require("compiler", "nope")


def test_iteration_and_snapshot():
    reg = CapabilityRegistry()
    reg.register(MockCompilerAdapter().MANIFEST, MockCompilerAdapter())
    reg.register(MockRetrievalAdapter().MANIFEST, MockRetrievalAdapter(), role="r")

    listed = list(reg)
    assert len(listed) == 2

    snap = reg.snapshot()
    assert {entry["manifest"]["type"] for entry in snap} == {"compiler", "retrieval"}
    assert {entry["role"] for entry in snap} == {None, "r"}
