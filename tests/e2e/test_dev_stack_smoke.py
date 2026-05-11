"""Opt-in end-to-end smoke test.

Boots the full dev stack via `deploy/dev/docker-compose.yml`,
uploads a tiny fixture document, and polls the runs API until the
ingest run reaches a terminal status. Gives an operator a single
command to confirm worker / API / Temporal wiring is healthy on a
fresh checkout.

Deliberately opt-in:

 * Marked `@pytest.mark.e2e` (declared in `pyproject.toml`) so it
 is excluded from the default `pytest -q` run.
 * Skipped unless `J1_E2E=1` is exported.

Invoke locally:

 J1_E2E=1.venv/bin/pytest -m e2e -s

Tear-down always runs (even on failure) to leave the host clean.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest


REQUIRED_ENV = "J1_E2E"
COMPOSE_FILE = (
    Path(__file__).resolve().parents[2]
    / "deploy" / "dev" / "docker-compose.yml"
)
API_BASE = os.environ.get("J1_E2E_API_BASE", "http://localhost:8000")
COMPOSE_BIN = shutil.which("docker") and ["docker", "compose"]
STARTUP_TIMEOUT_S = 180
RUN_TIMEOUT_S = 600


pytestmark = pytest.mark.e2e


def _docker_available() -> bool:
    if COMPOSE_BIN is None:
        return False
    try:
        subprocess.run(
            [*COMPOSE_BIN, "version"],
            check=True, capture_output=True, timeout=10,
        )
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


@pytest.fixture(scope="module")
def dev_stack():
    if os.environ.get(REQUIRED_ENV) != "1":
        pytest.skip(f"set {REQUIRED_ENV}=1 to run the e2e smoke test")
    if not _docker_available():
        pytest.skip("docker compose not available on PATH")
    if not COMPOSE_FILE.exists():
        pytest.skip(f"{COMPOSE_FILE} missing")

    up = subprocess.run(
        [*COMPOSE_BIN, "-f", str(COMPOSE_FILE), "up", "-d", "--wait"],
        capture_output=True, text=True, timeout=STARTUP_TIMEOUT_S,
    )
    if up.returncode != 0:
        pytest.skip(f"docker compose up failed:\n{up.stderr}")

    try:
        yield API_BASE
    finally:
        subprocess.run(
            [*COMPOSE_BIN, "-f", str(COMPOSE_FILE), "down", "-v"],
            capture_output=True, timeout=STARTUP_TIMEOUT_S,
        )


def _wait_terminal(httpx_mod, base: str, run_id: str, headers: dict[str, str]):
    deadline = time.monotonic() + RUN_TIMEOUT_S
    last: dict | None = None
    while time.monotonic() < deadline:
        resp = httpx_mod.get(f"{base}/ingestion-runs/{run_id}", headers=headers)
        if resp.status_code == 200:
            last = resp.json()["data"]
            if last["status"] in {"succeeded", "failed", "cancelled"}:
                return last
        time.sleep(2)
    raise AssertionError(
        f"run {run_id} did not reach terminal state within "
        f"{RUN_TIMEOUT_S}s; last snapshot: {last}"
    )


def test_dev_stack_smoke(dev_stack):
    httpx = pytest.importorskip("httpx")
    base = dev_stack
    headers = {"X-Tenant-Id": "acme", "X-Project-Id": "alpha"}

    fixture = b"# hello\n\nThis is a tiny smoke-test document.\n"
    upload = httpx.post(
        f"{base}/documents",
        headers=headers,
        files={"file": ("smoke.md", fixture, "text/markdown")},
    )
    assert upload.status_code in {200, 201}, upload.text
    run_id = upload.json()["data"]["runId"]

    final = _wait_terminal(httpx, base, run_id, headers)
    assert final["status"] == "succeeded", final
