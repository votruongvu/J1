"""SmartQueryOrchestrator query layer.

The legacy ``HybridQueryEngine`` + 5-provider stack (Knowledge /
Graph / Evidence / Consistency / Report) has been removed. Every
query path now flows through :class:`SmartQueryOrchestrator`.

Public surface kept narrow — only the orchestrator entrypoints +
the scope primitives + the plan / trace data classes are exported.
"""

from j1.query.intent_classifier import QueryIntentClassifier
from j1.query.orchestrator import (
    OrchestratorRequest,
    OrchestratorResult,
    SmartQueryOrchestrator,
)
from j1.query.query_plan import (
    AnswerShape,
    EvidenceBlock,
    EvidenceCandidate,
    EvidenceGroupSpec,
    EvidencePack,
    GateResult,
    Intent,
    QualityPolicy,
    QueryPlan,
    RetrievalJob,
    RetrievalRouteKind,
    SufficiencyPolicy,
    SynthesisMode,
)
from j1.query.query_trace import QueryTrace
from j1.query.scope import (
    ActiveScope,
    QueryScope,
    RunScope,
    WorkspaceScope,
    default_scope,
)

__all__ = [
    "ActiveScope",
    "AnswerShape",
    "EvidenceBlock",
    "EvidenceCandidate",
    "EvidenceGroupSpec",
    "EvidencePack",
    "GateResult",
    "Intent",
    "OrchestratorRequest",
    "OrchestratorResult",
    "QualityPolicy",
    "QueryIntentClassifier",
    "QueryPlan",
    "QueryScope",
    "QueryTrace",
    "RetrievalJob",
    "RetrievalRouteKind",
    "RunScope",
    "SmartQueryOrchestrator",
    "SufficiencyPolicy",
    "SynthesisMode",
    "WorkspaceScope",
    "default_scope",
]
