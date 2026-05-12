"""Regression guard: every `@activity.defn`-decorated method on an
activity class must appear in that class's `all_activities()` registry.

The Temporal worker registers activities only from the list each class
returns from `all_activities()`. A decorated method missing from that
list silently becomes a `NotFoundError: Activity function ... is not
registered on this worker` at activity-dispatch time — usually mid-run,
after compile or earlier stages already wrote artifacts. That's exactly
the failure mode `ProcessingActivities` shipped with for several months
before this regression test existed (six missing entries including
`build_initial_execution_plan` and `persist_compile_result_summary`).

The check is structural: iterate every method on the class, find the
ones the Temporal SDK decorated with `__temporal_activity_definition`,
diff against the methods returned from `all_activities()`.
"""

from __future__ import annotations

import pytest

from j1.audit.recorder import AuditRecorder
from j1.workspace.resolver import WorkspaceResolver


def _decorated_activity_methods(cls: type) -> set[str]:
    """Names of methods on `cls` carrying the Temporal SDK's activity
 marker. Walks the class dict (not instances) so we don't need to
 construct the activity class with all its dependencies."""
    out: set[str] = set()
    for name in dir(cls):
        if name.startswith("_"):
            continue
        attr = getattr(cls, name, None)
        if attr is None:
            continue
        if hasattr(attr, "__temporal_activity_definition"):
            out.add(name)
    return out


def _registered_activity_names(activity_obj) -> set[str]:
    """Names registered via `obj.all_activities()`. We compare unbound
 method names by reading `.__func__.__name__` (bound) or
 `.__name__` (function)."""
    out: set[str] = set()
    for method in activity_obj.all_activities():
        fn = getattr(method, "__func__", None) or method
        out.add(fn.__name__)
    return out


# ---- ProcessingActivities -----------------------------------------


def test_processing_activities_registry_includes_every_decorated_method(
    tmp_path,
):
    """Every `@activity.defn`-decorated method on `ProcessingActivities`
 must appear in `all_activities()`. Pin against the historical
 drift that left six activities missing — including the
 `build_initial_execution_plan` activity (whose silent failure
 produced the "No AssessmentPlan was attached" banner) and
 `persist_compile_result_summary` (a NotFoundError mid-run)."""
    from j1.orchestration.activities.processing import ProcessingActivities

    decorated = _decorated_activity_methods(ProcessingActivities)
    # Construct a real instance so `all_activities()` returns bound
    # methods we can name-match against the decorated set.
    workspace = WorkspaceResolver(tmp_path)
    activities = ProcessingActivities(
        workspace=workspace,
        audit=AuditRecorder(workspace),
    )
    registered = _registered_activity_names(activities)
    missing = sorted(decorated - registered)
    assert missing == [], (
        f"ProcessingActivities.all_activities() is missing decorated "
        f"methods: {missing}. The Temporal worker won't register them, "
        f"so workflows that call them will fail with NotFoundError."
    )


# ---- Sister activity classes (defence-in-depth) -------------------


@pytest.mark.parametrize("module_path,class_name", [
    ("j1.orchestration.activities.accounting", "AccountingActivities"),
    ("j1.orchestration.activities.lifecycle", "LifecycleActivities"),
    ("j1.orchestration.activities.profiling", "ProfilingActivities"),
    ("j1.orchestration.activities.project", "ProjectActivities"),
    ("j1.orchestration.activities.review", "ReviewActivities"),
    ("j1.orchestration.activities.runs", "RunsActivities"),
    ("j1.orchestration.activities.search", "SearchActivities"),
    ("j1.orchestration.activities.knowledge", "KnowledgeProcessingActivities"),
])
def test_activity_class_decorated_methods_are_in_registry(
    module_path, class_name,
):
    """Same invariant for the sister activity classes — check the set
 of decorated method names against the class's registry method
 without actually constructing instances. We don't need bound
 methods for the name check; the class-level walk is enough."""
    import importlib
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    decorated = _decorated_activity_methods(cls)
    if not decorated:
        pytest.skip(f"{class_name} declares no activities")

    # Find the registry method by name. Different classes use
    # different conventions (`all_activities`, etc.) — accept any
    # method whose name suggests "activity registry".
    candidate_names = ("all_activities", "activities", "list_activities")
    registry_method = None
    for name in candidate_names:
        if hasattr(cls, name) and callable(getattr(cls, name)):
            registry_method = getattr(cls, name)
            break
    if registry_method is None:
        pytest.skip(f"{class_name} has no all_activities()-style registry")

    # Read the source to find which `self.<method>` references the
    # registry returns. Avoids needing to construct the class with its
    # specific dependencies.
    import inspect
    try:
        src = inspect.getsource(registry_method)
    except (OSError, TypeError):
        pytest.skip(f"can't read source of {class_name}.{registry_method.__name__}")
    import re
    referenced = set(re.findall(r"self\.([a-zA-Z_][a-zA-Z0-9_]*)", src))
    missing = sorted(decorated - referenced)
    assert missing == [], (
        f"{class_name}.{registry_method.__name__} is missing decorated "
        f"methods: {missing}"
    )
