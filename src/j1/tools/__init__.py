"""Operator-facing developer tools.

Tools in this package are read-only by contract — they evaluate
or report on the system without mutating registry, snapshot, or
run state. Each tool ships a ``main()`` so it can be invoked via
``python -m j1.tools.<name>``.
"""
