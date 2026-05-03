from typing import Any

from temporalio import activity


def heartbeat(detail: Any | None = None) -> None:
    """Emit a Temporal activity heartbeat — safe outside an activity.

    Long-running J1 activities should call this periodically so Temporal
    knows the worker is alive and can refresh the activity timeout. Outside
    a Temporal activity context (e.g., direct unit tests) this is a no-op.
    """
    try:
        if detail is None:
            activity.heartbeat()
        else:
            activity.heartbeat(detail)
    except RuntimeError:
        # Not inside an activity context.
        return
