#!/usr/bin/env bash
# Dev-only: wipe the entire local docker-compose state.
#
# Phase 4 (canonical Postgres FTS evidence path) policy: dev data is
# THROWAWAY. Two reset triggers:
#
#   1. Pre-Phase-3 data has no ``active_snapshot_id`` and is now
#      invisible to queries. Reset + re-ingest.
#   2. SQLite evidence rows from Phase 1/2 are no longer the canonical
#      search target. The default backend is PostgreSQL FTS; SQLite
#      runs only when ``J1_LEGACY_SQLITE_EVIDENCE_ENABLED=true``.
#
# This script stops the stack, removes every named volume defined in
# ``deploy/dev/docker-compose.yml``, and clears any host-side temp
# directories used for benchmarks / scratch.
#
# After reset:
#   1. ``docker compose up`` brings the stack back.
#   2. Re-ingest documents through ``POST /documents`` + the normal
#      Temporal workflow. Phase 4 writes snapshot-scoped evidence
#      rows to Postgres and stamps ``document.active_snapshot_id``
#      on promotion.
#
# Does NOT touch:
#   * Source code / git state
#   * Images (use ``docker compose down --rmi all`` separately when
#     you want to rebuild from a clean slate)
#   * ``.env`` files
#
# Usage:
#   scripts/dev/reset_docker.sh                # asks before destroying
#   scripts/dev/reset_docker.sh --yes          # non-interactive
#   scripts/dev/reset_docker.sh --yes --rmi    # also remove images

set -euo pipefail

YES=0
RMI=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y) YES=1 ;;
    --rmi)    RMI=1 ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/deploy/dev/docker-compose.yml"

if [ ! -f "$COMPOSE_FILE" ]; then
  echo "[reset] cannot find $COMPOSE_FILE" >&2
  exit 1
fi

# Discover the named volumes the compose file declares. Falls back to
# a hard-coded list when ``yq`` isn't available so the script stays
# usable on a bare macOS without extra deps.
VOLUMES=$(awk '
  /^volumes:/ { in_vol=1; next }
  /^[^[:space:]]/ { in_vol=0 }
  in_vol && /^[[:space:]]{2}[a-zA-Z_][a-zA-Z0-9_-]*:/ {
    gsub(/:.*/, "")
    gsub(/^[[:space:]]+/, "")
    print "j1-dev_" $0
  }
' "$COMPOSE_FILE")

echo "[reset] this will DESTROY the following:"
echo "  * all containers from $COMPOSE_FILE"
echo "  * named volumes:"
echo "$VOLUMES" | sed 's/^/      - /'
if [ "$RMI" = "1" ]; then
  echo "  * built images (--rmi)"
fi
echo

if [ "$YES" != "1" ]; then
  read -r -p "Proceed? [y/N] " ans
  case "$ans" in
    y|Y|yes|YES) ;;
    *) echo "[reset] aborted."; exit 1 ;;
  esac
fi

echo "[reset] stopping stack…"
if [ "$RMI" = "1" ]; then
  docker compose -f "$COMPOSE_FILE" down --volumes --rmi local --remove-orphans
else
  docker compose -f "$COMPOSE_FILE" down --volumes --remove-orphans
fi

# ``down --volumes`` removes the volumes that compose KNOWS about. If
# the stack was renamed or restarted across compose-project changes,
# some volumes may persist under the old name. Sweep them explicitly.
echo "[reset] sweeping named volumes…"
for v in $VOLUMES; do
  if docker volume inspect "$v" >/dev/null 2>&1; then
    docker volume rm "$v" >/dev/null
    echo "  ✓ removed $v"
  fi
done

# Clear any host-side scratch the framework may have written outside
# of docker (rare, but ``J1_DATA_ROOT`` can be set to a host path).
HOST_DATA_ROOT="${J1_DATA_ROOT:-}"
if [ -n "$HOST_DATA_ROOT" ] && [ -d "$HOST_DATA_ROOT" ] && \
   [ "$HOST_DATA_ROOT" != "/" ] && [ "$HOST_DATA_ROOT" != "/var/lib/j1" ]; then
  echo "[reset] clearing host data root: $HOST_DATA_ROOT"
  rm -rf "$HOST_DATA_ROOT"/*
fi

echo "[reset] done. Next: docker compose -f $COMPOSE_FILE up -d"
