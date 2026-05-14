"""Tests for the unified runtime config — Phase 1 foundation."""

from __future__ import annotations

import pytest

from j1.config.runtime import (
    ArtifactBackend,
    CacheBackend,
    EvidenceBackend,
    GraphBackend,
    MetadataBackend,
    RagBackend,
    RuntimeProfile,
    VectorBackend,
    load_runtime_config,
)
from j1.errors.exceptions import ConfigError


# ---- Defaults ---------------------------------------------------


def test_empty_env_returns_dev_profile_with_canonical_backends():
    """The loader must never blow up on missing keys. Defaults are
    DEV profile + canonical backends (postgres / s3 / redis /
    postgres_fts / raganything). Phase 3 sets vector + graph
    defaults to ``embedded_lightrag`` because the direct Qdrant +
    Neo4j adapters aren't implemented yet."""
    cfg = load_runtime_config({})
    assert cfg.profile == RuntimeProfile.DEV
    assert cfg.metadata.backend == MetadataBackend.POSTGRES
    assert cfg.artifact.backend == ArtifactBackend.S3
    assert cfg.cache.backend == CacheBackend.REDIS
    assert cfg.vector.backend == VectorBackend.EMBEDDED_LIGHTRAG
    assert cfg.graph.backend == GraphBackend.EMBEDDED_LIGHTRAG
    assert cfg.evidence.backend == EvidenceBackend.POSTGRES_FTS
    assert cfg.rag.backend == RagBackend.RAGANYTHING


def test_loader_parses_runtime_profile():
    cfg = load_runtime_config({"J1_RUNTIME_PROFILE": "prod"})
    assert cfg.profile == RuntimeProfile.PROD


def test_loader_rejects_unknown_profile():
    with pytest.raises(ConfigError, match="not a valid RuntimeProfile"):
        load_runtime_config({"J1_RUNTIME_PROFILE": "staging"})


def test_loader_parses_provider_backends():
    cfg = load_runtime_config({
        "J1_METADATA_BACKEND": "sqlite_local",
        "J1_ARTIFACT_BACKEND": "local_fs",
        "J1_CACHE_BACKEND": "memory",
        "J1_VECTOR_BACKEND": "embedded_lightrag",
        "J1_GRAPH_BACKEND": "embedded_lightrag",
        # Phase 8: ``sqlite_fts5`` is gone. ``postgres_fts`` is the
        # only evidence backend.
    })
    assert cfg.metadata.backend == MetadataBackend.SQLITE_LOCAL
    assert cfg.artifact.backend == ArtifactBackend.LOCAL_FS
    assert cfg.cache.backend == CacheBackend.MEMORY


def test_evidence_backend_only_supports_postgres_fts():
    """Phase 8: the SQLite FTS5 fallback was deleted. Selecting
    any non-canonical backend raises at parse time."""
    with pytest.raises(ConfigError, match="EvidenceBackend"):
        load_runtime_config({"J1_EVIDENCE_BACKEND": "sqlite_fts5"})


# ---- Validation: DEV --------------------------------------------


def test_dev_profile_with_dev_backends_validates_clean():
    """DEV using local fallbacks is valid when an evidence DSN is
    supplied (Phase 8: PostgreSQL FTS is mandatory)."""
    cfg = load_runtime_config({
        "J1_RUNTIME_PROFILE": "dev",
        "J1_METADATA_BACKEND": "sqlite_local",
        "J1_ARTIFACT_BACKEND": "local_fs",
        "J1_ARTIFACT_LOCAL_ROOT": "/tmp/j1-artifacts",
        "J1_CACHE_BACKEND": "memory",
        "J1_VECTOR_BACKEND": "embedded_lightrag",
        "J1_GRAPH_BACKEND": "embedded_lightrag",
        "J1_EVIDENCE_DSN": "postgresql://j1:j1@pg/j1",
    })
    cfg.validate()  # no raise


def test_dev_profile_with_canonical_backend_but_no_dsn_fails():
    """DEV must still validate canonical-backend configs — operator
    opted into the docker stack, framework expects a working DSN."""
    cfg = load_runtime_config({
        "J1_RUNTIME_PROFILE": "dev",
        "J1_METADATA_BACKEND": "postgres",
        # No J1_METADATA_DSN.
    })
    with pytest.raises(ConfigError, match="metadata"):
        cfg.validate()


# ---- Validation: PROD ------------------------------------------


def test_prod_profile_rejects_dev_backends():
    cfg = load_runtime_config({
        "J1_RUNTIME_PROFILE": "prod",
        "J1_METADATA_BACKEND": "sqlite_local",
    })
    with pytest.raises(ConfigError, match="not allowed in PROD"):
        cfg.validate()


def test_prod_profile_requires_full_dsns_for_every_provider():
    cfg = load_runtime_config({
        "J1_RUNTIME_PROFILE": "prod",
        # All canonical backends, but missing every URL.
    })
    with pytest.raises(ConfigError) as excinfo:
        cfg.validate()
    msg = str(excinfo.value)
    # Phase 3: vector + graph default to embedded_lightrag (no
    # direct adapter yet) so they're not in the missing-config
    # list. The other canonical provider DSNs remain required.
    assert "metadata" in msg
    assert "artifact" in msg
    assert "cache" in msg
    assert "evidence" in msg


def test_prod_profile_with_full_config_validates_clean():
    cfg = load_runtime_config({
        "J1_RUNTIME_PROFILE": "prod",
        "J1_METADATA_DSN": "postgresql://j1:j1@pg/j1",
        "J1_ARTIFACT_ENDPOINT": "https://s3.example.com",
        "J1_ARTIFACT_BUCKET": "j1",
        "J1_ARTIFACT_ACCESS_KEY": "k",
        "J1_ARTIFACT_SECRET_KEY": "s",
        "J1_CACHE_URL": "redis://redis:6379/0",
        # Phase 3: vector + graph stay on embedded_lightrag because
        # the direct Qdrant + Neo4j adapters aren't implemented yet.
    })
    cfg.validate()  # no raise


def test_qdrant_backend_explicitly_fails_until_phase_4():
    cfg = load_runtime_config({"J1_VECTOR_BACKEND": "qdrant"})
    with pytest.raises(ConfigError, match="Qdrant adapter is not implemented"):
        cfg.validate()


def test_neo4j_backend_explicitly_fails_until_phase_4():
    cfg = load_runtime_config({"J1_GRAPH_BACKEND": "neo4j"})
    with pytest.raises(ConfigError, match="Neo4j adapter is not implemented"):
        cfg.validate()


# ---- Evidence DSN reuse -----------------------------------------


def test_evidence_dsn_falls_back_to_metadata_dsn():
    cfg = load_runtime_config({
        "J1_METADATA_DSN": "postgresql://j1:j1@pg/j1",
    })
    assert cfg.evidence.effective_dsn(cfg.metadata) == (
        "postgresql://j1:j1@pg/j1"
    )


def test_evidence_dsn_can_be_overridden():
    cfg = load_runtime_config({
        "J1_METADATA_DSN": "postgresql://j1:j1@pg/j1",
        "J1_EVIDENCE_DSN": "postgresql://j1:j1@fts/j1_evidence",
    })
    assert cfg.evidence.effective_dsn(cfg.metadata) == (
        "postgresql://j1:j1@fts/j1_evidence"
    )


# ---- Cleanup defaults ------------------------------------------


def test_dev_profile_defaults_to_hard_delete():
    cfg = load_runtime_config({"J1_RUNTIME_PROFILE": "dev"})
    assert cfg.cleanup.hard_delete is True


def test_prod_profile_defaults_to_soft_delete():
    cfg = load_runtime_config({"J1_RUNTIME_PROFILE": "prod"})
    assert cfg.cleanup.hard_delete is False


def test_cleanup_can_be_overridden():
    cfg = load_runtime_config({
        "J1_RUNTIME_PROFILE": "prod",
        "J1_CLEANUP_HARD_DELETE": "true",
    })
    assert cfg.cleanup.hard_delete is True


# ---- Benchmark / paths -----------------------------------------


def test_benchmark_flags_default_off():
    cfg = load_runtime_config({})
    assert cfg.benchmark.enable_stage_timing is False
    assert cfg.benchmark.enable_ingestion_benchmark is False


def test_benchmark_flags_parse_truthy_values():
    cfg = load_runtime_config({
        "J1_BENCHMARK_STAGE_TIMING": "true",
        "J1_BENCHMARK_INGESTION": "yes",
    })
    assert cfg.benchmark.enable_stage_timing is True
    assert cfg.benchmark.enable_ingestion_benchmark is True


def test_rag_workdir_inherits_data_root():
    cfg = load_runtime_config({"J1_DATA_ROOT": "/srv/j1"})
    assert str(cfg.rag.workdir) == "/srv/j1/raganything"
    assert str(cfg.rag.mineru_workdir) == "/srv/j1/raganything/mineru"
