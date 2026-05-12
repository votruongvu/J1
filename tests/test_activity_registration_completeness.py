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

import importlib
import inspect
import re

import pytest


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


def _registry_method_references(cls: type) -> set[str] | None:
    """Find the class's `all_activities()`-style registry method and
 return the set of `self.<name>` references inside it. Returns
 None if the class doesn't declare a registry. Source-inspection
 avoids needing to instantiate activity classes with their
 specific dependencies."""
    for name in ("all_activities", "activities", "list_activities"):
        if hasattr(cls, name) and callable(getattr(cls, name)):
            method = getattr(cls, name)
            try:
                src = inspect.getsource(method)
            except (OSError, TypeError):
                return None
            return set(re.findall(r"self\.([a-zA-Z_][a-zA-Z0-9_]*)", src))
    return None


_ACTIVITY_CLASSES = [
    ("j1.orchestration.activities.processing", "ProcessingActivities"),
    ("j1.orchestration.activities.accounting", "AccountingActivities"),
    ("j1.orchestration.activities.lifecycle", "ProjectLifecycleActivities"),
    ("j1.orchestration.activities.profiling", "ProfilingActivities"),
    ("j1.orchestration.activities.project", "ProjectActivities"),
    ("j1.orchestration.activities.review", "ReviewActivities"),
    ("j1.orchestration.activities.runs", "RunsActivities"),
    ("j1.orchestration.activities.search", "SearchActivities"),
    ("j1.orchestration.activities.knowledge", "KnowledgeProcessingActivities"),
]


@pytest.mark.parametrize("module_path,class_name", _ACTIVITY_CLASSES)
def test_activity_class_registry_includes_every_decorated_method(
    module_path, class_name,
):
    """Every `@activity.defn`-decorated method on an activity class
 must appear in that class's `all_activities()` registry. The
 Temporal worker registers activities only from what this method
 returns; any decorated method missing from the list silently
 becomes a `NotFoundError: Activity function ... is not registered
 on this worker` at activity-dispatch time.

 Pin against the historical drift on `ProcessingActivities` that
 left six activities missing — including
 `build_initial_execution_plan` (whose silent NotFoundError
 produced the "No AssessmentPlan was attached" banner) and
 `persist_compile_result_summary` (a NotFoundError mid-compile)."""
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    decorated = _decorated_activity_methods(cls)
    if not decorated:
        pytest.skip(f"{class_name} declares no activities")
    referenced = _registry_method_references(cls)
    if referenced is None:
        pytest.skip(f"{class_name} has no all_activities()-style registry")
    missing = sorted(decorated - referenced)
    assert missing == [], (
        f"{class_name}.all_activities() is missing decorated methods: "
        f"{missing}. The Temporal worker won't register them, so "
        f"workflows that call them will fail with NotFoundError."
    )
