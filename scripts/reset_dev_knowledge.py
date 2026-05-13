"""Dev-only reset: wipe ALL document knowledge state.

Removes everything the J1 stack persists to disk for documents +
runs + audit + workspace data, so a clean re-ingest can start
from scratch. Deliberately aggressive: it does NOT preserve any
records, and is NOT intended for production.

Usage:

    python -m scripts.reset_dev_knowledge                    # dry run
    python -m scripts.reset_dev_knowledge --confirm          # actually delete
    python -m scripts.reset_dev_knowledge --confirm --root /var/lib/j1
    python -m scripts.reset_dev_knowledge --confirm --include-temporal

What it removes (when --confirm is passed):

  * The entire ``J1_DATA_ROOT`` tree (workspace, registries,
    audit, run-store, RAW files, LightRAG dirs).
  * The RAGAnything workdir (``J1_RAGANYTHING_WORKDIR``) — the
    per-run LightRAG storage actually lives here, not under
    ``J1_DATA_ROOT``.

What it doesn't touch (by design):

  * Temporal workflow history (set ``--include-temporal`` to
    truncate the dev Temporal DB).
  * LM Studio / vLLM caches.
  * Code or virtualenv.

Refuses to run unless ``--confirm`` is passed. Safe-to-loop: the
helper functions are idempotent — deleting an already-deleted
path is success.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


_DEFAULT_DATA_ROOT = "/var/lib/j1"
_DEFAULT_RAGANYTHING_WORKDIR = "/var/lib/j1/raganything"


def _resolve_targets(*, data_root: str, raganything_workdir: str) -> list[Path]:
    """List of root paths the script will wipe."""
    targets: list[Path] = []
    for raw in (data_root, raganything_workdir):
        path = Path(raw).expanduser().resolve()
        if path in targets:
            continue
        targets.append(path)
    return targets


def _safe_rmtree(path: Path) -> tuple[bool, str]:
    """Idempotent recursive delete.

    Returns (existed, message). ``existed=False`` is a success
    case (already gone). Errors raise to the caller; this is a
    dev script and we want to see them.
    """
    if not path.exists():
        return (False, f"already gone: {path}")
    if path.is_file():
        path.unlink()
        return (True, f"deleted file: {path}")
    shutil.rmtree(path, ignore_errors=False)
    return (True, f"deleted tree: {path}")


def _print_summary(reports: list[tuple[bool, str]]) -> None:
    deleted = sum(1 for existed, _ in reports if existed)
    skipped = len(reports) - deleted
    print(f"\n=== Done. Deleted {deleted}, skipped {skipped} (already gone). ===")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Wipe ALL J1 document / run / workspace state for a "
            "clean dev re-ingest. Refuses without --confirm."
        ),
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "Required to actually delete anything. Without this, "
            "the script prints what would be removed."
        ),
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("J1_DATA_ROOT", _DEFAULT_DATA_ROOT),
        help="Override J1_DATA_ROOT for this run.",
    )
    parser.add_argument(
        "--raganything-workdir",
        default=os.environ.get(
            "J1_RAGANYTHING_WORKDIR", _DEFAULT_RAGANYTHING_WORKDIR,
        ),
        help="Override J1_RAGANYTHING_WORKDIR for this run.",
    )
    parser.add_argument(
        "--include-temporal",
        action="store_true",
        help=(
            "Also truncate the dev Temporal DB volume "
            "(docker compose down + volume rm). Off by default; "
            "Temporal history survives this script unless requested."
        ),
    )
    args = parser.parse_args(argv)

    targets = _resolve_targets(
        data_root=args.root,
        raganything_workdir=args.raganything_workdir,
    )
    print("Targets:")
    for target in targets:
        print(f"  - {target}")

    if not args.confirm:
        print(
            "\nDry run. Pass --confirm to actually delete. "
            "This script is destructive and not reversible.",
        )
        return 0

    print("\n--confirm given, proceeding...\n")
    reports: list[tuple[bool, str]] = []
    for target in targets:
        try:
            existed, message = _safe_rmtree(target)
        except OSError as exc:
            print(f"  ! failed: {target}: {exc}", file=sys.stderr)
            reports.append((False, f"failed: {target}: {exc}"))
            continue
        print(f"  {message}")
        reports.append((existed, message))

    if args.include_temporal:
        # Truncating the Temporal DB is a docker-volume operation;
        # we don't try to run it from Python because the script can
        # be invoked outside the compose context. Just print the
        # commands so the operator can run them.
        print(
            "\nTo truncate Temporal history, run from deploy/dev/:\n"
            "  docker compose down\n"
            "  docker volume rm dev_temporal_data\n"
            "  docker compose up -d\n",
        )

    _print_summary(reports)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
