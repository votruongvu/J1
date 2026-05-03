from j1.query.models import QueryMode

# (mode, keyword tuple) — order matters: first match wins.
_RULES: tuple[tuple[QueryMode, tuple[str, ...]], ...] = (
    (
        QueryMode.GRAPH_FIRST,
        ("relationship", "dependency", "depend", "path", "connect", "related"),
    ),
    (
        QueryMode.EVIDENCE_FIRST,
        ("where", "source", "evidence", "verify", "cite"),
    ),
    (
        QueryMode.CONSISTENCY_CHECK,
        ("conflict", "mismatch", "inconsistent", "consistency", "contradict"),
    ),
    (
        QueryMode.REPORT_GENERATION,
        ("report", "matrix", "outline", "generate"),
    ),
    (
        QueryMode.KNOWLEDGE_FIRST,
        ("summary", "summarize", "scope", "requirement", "risk"),
    ),
)


class QueryIntentClassifier:
    """Maps a natural-language question to a `QueryMode`.

    Falls back to `KNOWLEDGE_FIRST` when no keyword matches; the engine
    additionally tries `GRAPH_FIRST` as a follow-up when knowledge yields
    no sources.
    """

    def classify(self, question: str) -> QueryMode:
        lowered = question.lower()
        for mode, keywords in _RULES:
            if any(kw in lowered for kw in keywords):
                return mode
        return QueryMode.KNOWLEDGE_FIRST
