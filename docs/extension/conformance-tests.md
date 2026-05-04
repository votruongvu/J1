# Adapter Conformance Tests

J1 ships shared test harnesses for every extension contract. Vendor
test suites use the same harnesses against their own adapters so
"it claims to be a `RetrievalAdapter`" gets verified mechanically,
not by code review alone.

The harnesses live in
[`j1.extension.conformance`](../../src/j1/extension/conformance.py)
and are runnable from any pytest test file.

---

## 1. What each harness checks

| Harness function | Verifies |
|---|---|
| `assert_source_connector_conformance(adapter, ctx)` | `kind` non-empty; `list()` returns `list[SourceMetadata]`; `fetch()` returns a `Source` with matching `metadata.uri`; no secret leakage. |
| `assert_compiler_adapter_conformance(adapter, ctx, document_id)` | Returns `ArtifactProcessingResult` with a valid `ResultStatus` + `drafts: list`; no secret leakage. |
| `assert_enrichment_adapter_conformance(adapter, ctx, artifact_id)` | Same shape as compiler. |
| `assert_graph_adapter_conformance(adapter, ctx, artifact_ids)` | Tolerates `[]` input; otherwise produces an `ArtifactProcessingResult`. |
| `assert_retrieval_adapter_conformance(adapter, ctx, question)` | Returns `RetrievalResult` with `evidences: list[Evidence]` (citations typed); empty question doesn't crash; no secret leakage. |
| `assert_reranker_adapter_conformance(adapter, ctx)` | Empty input → empty output; inputs MUST NOT be mutated. |
| `assert_llm_provider_adapter_conformance(adapter, ctx)` | `generate()` returns `dict` with `text: str`; empty prompt doesn't crash. |
| `assert_embedding_provider_adapter_conformance(adapter, ctx)` | Vectors match `dimension()`; ordered like input; empty input → empty list. |
| `assert_vision_provider_adapter_conformance(adapter, ctx)` | `analyze()` returns dict with `text: str`. |
| `assert_output_formatter_conformance(formatter, ctx)` | Tolerates empty evidence list; returns dict. |
| `assert_evaluation_adapter_conformance(adapter, ctx)` | Returns `EvaluationResult`; `score in [0,1]` when set; **deterministic** for the same input. |

Every harness also runs a generous wall-clock deadline (`5s` per
adapter call) to catch hung mocks in CI.

### What the harnesses do NOT check

- **Performance** — that's a deployment concern.
- **Real-world relevance** — a mock that returns `score=0.0` for
  everything is conformant; quality eval is a separate job.
- **Network / disk side effects** — the harnesses pass minimal /
  empty inputs; if the adapter wants to make a real API call,
  that's the adapter's choice.

---

## 2. Running the harnesses against your adapter

```python
# tests/test_my_adapter_conformance.py
import pytest

from j1.extension.conformance import (
    assert_compiler_adapter_conformance,
    assert_retrieval_adapter_conformance,
)
from j1.extension.primitives import ProjectContext, Evidence, Citation
from acme_pkg import AcmeCompiler, AcmeRetrieval


def _ctx() -> ProjectContext:
    return ProjectContext(tenant_id="acme", project_id="alpha")


def test_acme_compiler_conformance():
    assert_compiler_adapter_conformance(
        AcmeCompiler.from_test_config(),
        _ctx(),
        document_id="acme-test-doc",
    )


def test_acme_retrieval_conformance():
    assert_retrieval_adapter_conformance(
        AcmeRetrieval.from_test_config(),
        _ctx(),
        question="hello",
    )
```

Run with:

```bash
.venv/bin/pytest tests/test_my_adapter_conformance.py -v
```

A failing harness prints the contract violation directly — e.g.:

```
AssertionError: AcmeRetrieval.retrieve() must return RetrievalResult,
got dict
```

---

## 3. Reference: the framework's own conformance suite

[`tests/extension/test_conformance_mocks.py`](../../tests/extension/test_conformance_mocks.py)
runs every harness against the bundled mock adapters
([`j1.extension.mocks`](../../src/j1/extension/mocks.py)). It is
the canonical example of how vendor / domain test suites should be
shaped — copy the file and substitute your adapter classes.

```bash
.venv/bin/pytest tests/extension/test_conformance_mocks.py -v
```

---

## 4. When a contract change requires harness updates

A contract change is a breaking change for everyone who built
against the old shape. The expected workflow:

1. Open an issue describing the change + rationale.
2. Update the contract Protocol in
   [`src/j1/extension/contracts.py`](../../src/j1/extension/contracts.py).
3. Update the matching harness in
   [`src/j1/extension/conformance.py`](../../src/j1/extension/conformance.py).
4. Update the bundled mock(s) under
   [`src/j1/extension/mocks.py`](../../src/j1/extension/mocks.py).
5. Bump the major version of any affected adapter's manifest
   (`AdapterManifest.version`) when you re-release.
6. Document the change in the contract reference and announce.

---

## 5. Cross-references

- [`docs/extension/contracts.md`](contracts.md) — what each
  contract requires
- [`docs/extension/manifest-and-registry.md`](manifest-and-registry.md)
  — how the registry validates `manifest` ↔ `adapter`
- [`docs/extension/add-a-provider.md`](add-a-provider.md) — full
  provider-implementation recipe; the conformance test is the
  final acceptance gate
- [`src/j1/extension/conformance.py`](../../src/j1/extension/conformance.py)
- [`src/j1/extension/mocks.py`](../../src/j1/extension/mocks.py)
- [`tests/extension/test_conformance_mocks.py`](../../tests/extension/test_conformance_mocks.py)
