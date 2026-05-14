#!/bin/sh
# Postgres init script — runs once on first boot of the volume.
#
# Creates the two databases the dev stack uses:
#   * ``j1``       — application metadata + Postgres FTS (evidence)
#                    Already created by POSTGRES_DB in docker-compose;
#                    this script is a no-op for it but kept for clarity.
#   * ``temporal`` — Temporal's own storage (auto-setup populates the
#                    schema on first run).
#
# Idempotent: ``CREATE DATABASE IF NOT EXISTS`` doesn't exist in
# Postgres, so the script uses the standard ``SELECT … WHERE NOT
# EXISTS`` pattern.

set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
    SELECT 'CREATE DATABASE temporal'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'temporal')\gexec

    -- Reserve a dedicated schema for J1 application data inside
    -- the ``j1`` database. Phase 2 migrations will populate it.
    \c j1
    CREATE SCHEMA IF NOT EXISTS j1;
EOSQL
