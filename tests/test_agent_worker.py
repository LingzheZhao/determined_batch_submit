from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from determined_compute.agent_worker import (
    WorkflowConflictError,
    WorkflowManager,
    WorkflowNotFoundError,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fake_codex(tmp_path: Path) -> Path:
    executable = tmp_path / "fake-codex"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

args = sys.argv[1:]
prompt = sys.stdin.read()
if "SLOW_TEST" in prompt:
    time.sleep(5)
if "FAIL_TEST" in prompt:
    raise SystemExit(7)
output = Path(args[args.index("--output-last-message") + 1])
output.write_text(json.dumps({
    "args": args,
    "prompt": prompt,
    "environment_keys": sorted(os.environ),
}), encoding="utf-8")
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def make_manager(
    tmp_path: Path, fake_codex: Path, **kwargs: object
) -> WorkflowManager:
    return WorkflowManager(
        tmp_path / "workflows.sqlite3",
        REPO_ROOT,
        codex_bin=str(fake_codex),
        auto_start=False,
        **kwargs,
    )


def test_submit_is_owner_scoped_and_idempotent(
    tmp_path: Path, fake_codex: Path
) -> None:
    manager = make_manager(tmp_path, fake_codex)
    submitted = manager.submit(
        "Diagnose an undersubscribed training run.",
        owner="session-a",
        request_id="request-1",
        context={"experiment_id": 42},
    )
    duplicate = manager.submit(
        "Diagnose an undersubscribed training run.",
        owner="session-a",
        request_id="request-1",
        context={"experiment_id": 42},
    )

    assert submitted["status"] == "queued"
    assert duplicate["workflow_id"] == submitted["workflow_id"]
    assert duplicate["deduplicated"] is True
    assert manager.status(submitted["workflow_id"], "session-a")["status"] == "queued"

    with pytest.raises(WorkflowNotFoundError):
        manager.status(submitted["workflow_id"], "session-b")
    with pytest.raises(WorkflowConflictError) as conflict:
        manager.submit(
            "A different question.", owner="session-a", request_id="request-1"
        )
    assert conflict.value.code == "workflow_conflict"
    assert WorkflowNotFoundError.code == "workflow_not_found"

    benign = manager.submit(
        "Review input handling.",
        owner="session-a",
        request_id="request-2",
        context={"tokenizer": "sentencepiece", "context_tokens": 2048},
    )
    assert benign["status"] == "queued"


@pytest.mark.parametrize(
    "context",
    [
        {"api_token": "nope"},
        {"apiToken": "nope"},
        {"accessKey": "nope"},
        {"nested": {"password": "nope"}},
        {"items": [{"private-key": "nope"}]},
        {"sessionKey": "nope"},
    ],
)
def test_submit_rejects_sensitive_context_fields(
    tmp_path: Path, fake_codex: Path, context: dict
) -> None:
    manager = make_manager(tmp_path, fake_codex)
    with pytest.raises(ValueError, match="sensitive context field"):
        manager.submit("Help", "owner", "request", context)


def test_run_pending_uses_isolated_read_only_codex_invocation(
    tmp_path: Path, fake_codex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DET_API_TOKEN", "must-not-be-forwarded")
    monkeypatch.setenv("GRAFANA_PASSWORD", "must-not-be-forwarded")
    manager = make_manager(tmp_path, fake_codex)
    submitted = manager.submit(
        "Explain the likely bottleneck.",
        "owner",
        "request",
        {"pool": "gpu-a"},
    )

    completed = manager.run_pending(submitted["workflow_id"])

    assert completed["status"] == "succeeded"
    payload = json.loads(completed["result"])
    args = payload["args"]
    assert args[:2] == ["exec", "--ignore-user-config"]
    assert "--ignore-rules" in args
    assert "--ephemeral" in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert args[args.index("--model") + 1] == "gpt-5.6-sol"
    assert args[args.index("--config") + 1] == "mcp_servers={}"
    assert "Intensive Compute Runner" in payload["prompt"]
    assert '"pool": "gpu-a"' in payload["prompt"]
    assert "DET_API_TOKEN" not in payload["environment_keys"]
    assert "GRAFANA_PASSWORD" not in payload["environment_keys"]
    assert completed["error"] is None
    assert [entry["message"] for entry in completed["logs"]][-1] == (
        "Workflow completed."
    )


def test_submit_launches_an_independent_worker(
    tmp_path: Path, fake_codex: Path
) -> None:
    manager = WorkflowManager(
        tmp_path / "async.sqlite3",
        REPO_ROOT,
        codex_bin=str(fake_codex),
        timeout_seconds=5,
    )
    submitted = manager.submit("Inspect this run.", "owner", "async-request")

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = manager.status(submitted["workflow_id"], "owner")
        if current["status"] in ("succeeded", "failed", "timed_out"):
            break
        time.sleep(0.05)

    assert current["status"] == "succeeded", current


def test_fresh_queued_duplicate_does_not_dispatch_again(
    tmp_path: Path, fake_codex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = WorkflowManager(
        tmp_path / "fresh-queue.sqlite3",
        REPO_ROOT,
        codex_bin=str(fake_codex),
        stale_after_seconds=30,
    )
    dispatches = []

    def record_dispatch(workflow_id: str) -> int:
        dispatches.append(workflow_id)
        return os.getpid()

    monkeypatch.setattr(manager, "_spawn_worker", record_dispatch)
    first = manager.submit("Wait for claim.", "owner", "fresh-request")
    duplicate = manager.submit("Wait for claim.", "owner", "fresh-request")

    assert duplicate["workflow_id"] == first["workflow_id"]
    assert dispatches == [first["workflow_id"]]
    current = manager.status(first["workflow_id"], "owner")
    assert current["status"] == "queued"
    assert current["stale"] is False
    assert current["recoverable"] is False


def test_stale_queued_request_is_recoverable_and_resubmission_dispatches(
    tmp_path: Path, fake_codex: Path
) -> None:
    first_manager = WorkflowManager(
        tmp_path / "commit-gap.sqlite3",
        REPO_ROOT,
        codex_bin=str(fake_codex),
        stale_after_seconds=0.05,
        auto_start=False,
    )
    first = first_manager.submit(
        "Recover after commit.", "owner", "commit-before-spawn"
    )
    time.sleep(0.08)

    stranded = first_manager.status(first["workflow_id"], "owner")
    assert stranded["status"] == "queued"
    assert stranded["stale"] is True
    assert stranded["recoverable"] is True

    restarted_manager = WorkflowManager(
        tmp_path / "commit-gap.sqlite3",
        REPO_ROOT,
        codex_bin=str(fake_codex),
        stale_after_seconds=0.05,
    )
    retried = restarted_manager.submit(
        "Recover after commit.", "owner", "commit-before-spawn"
    )
    assert retried["workflow_id"] == first["workflow_id"]
    assert retried["deduplicated"] is True

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = restarted_manager.status(first["workflow_id"], "owner")
        if current["status"] in ("succeeded", "failed", "timed_out"):
            break
        time.sleep(0.05)

    assert current["status"] == "succeeded", current
    assert any(
        log["message"] == "Retrying dispatch for a stale queued workflow."
        for log in current["logs"]
    )


def test_timeout_stops_process_and_persists_safe_error(
    tmp_path: Path, fake_codex: Path
) -> None:
    manager = make_manager(
        tmp_path,
        fake_codex,
        timeout_seconds=0.2,
        stale_after_seconds=1,
    )
    submitted = manager.submit("SLOW_TEST", "owner", "slow-request")

    completed = manager.run_pending(submitted["workflow_id"])

    assert completed["status"] == "timed_out"
    assert completed["result"] is None
    assert completed["error"] == "The consultation exceeded its execution time limit."


def test_stale_dead_worker_is_reclaimed_with_visible_log(
    tmp_path: Path, fake_codex: Path
) -> None:
    manager = make_manager(tmp_path, fake_codex, stale_after_seconds=0.1)
    submitted = manager.submit("Retry safely.", "owner", "stale-request")
    with sqlite3.connect(str(manager.db_path)) as connection:
        connection.execute(
            """
            UPDATE agent_workflows
            SET status = 'running', heartbeat_at = '2000-01-01T00:00:00+00:00',
                worker_pid = 99999999, agent_pid = 99999998
            WHERE workflow_id = ?
            """,
            (submitted["workflow_id"],),
        )

    stale = manager.status(submitted["workflow_id"], "owner")
    assert stale["stale"] is True
    assert stale["recoverable"] is True

    completed = manager.run_pending(submitted["workflow_id"])

    assert completed["status"] == "succeeded"
    assert any(
        "Reclaimed an interrupted stale workflow" in log["message"]
        for log in completed["logs"]
    )


def test_stale_record_with_live_process_is_not_reexecuted(
    tmp_path: Path, fake_codex: Path
) -> None:
    manager = make_manager(tmp_path, fake_codex, stale_after_seconds=0.1)
    submitted = manager.submit("Do not duplicate.", "owner", "live-request")
    with sqlite3.connect(str(manager.db_path)) as connection:
        connection.execute(
            """
            UPDATE agent_workflows
            SET status = 'running', heartbeat_at = '2000-01-01T00:00:00+00:00',
                worker_pid = ?, agent_pid = NULL
            WHERE workflow_id = ?
            """,
            (os.getpid(), submitted["workflow_id"]),
        )

    stale = manager.status(submitted["workflow_id"], "owner")
    assert stale["stale"] is True
    assert stale["recoverable"] is False

    current = manager.run_pending(submitted["workflow_id"])

    assert current["status"] == "running"
    assert current["result"] is None


def test_missing_codex_reports_sanitized_failure(tmp_path: Path) -> None:
    manager = WorkflowManager(
        tmp_path / "workflows.sqlite3",
        REPO_ROOT,
        codex_bin=str(tmp_path / "path-containing-secret-value"),
        auto_start=False,
    )
    submitted = manager.submit("Help", "owner", "missing-codex")

    completed = manager.run_pending(submitted["workflow_id"])

    assert completed["status"] == "failed"
    assert completed["error"] == "The configured Codex executable was not found."
    assert "secret-value" not in json.dumps(completed)


def test_submit_reports_synchronous_worker_launch_failure(
    tmp_path: Path, fake_codex: Path
) -> None:
    manager = WorkflowManager(
        tmp_path / "launch.sqlite3",
        REPO_ROOT,
        codex_bin=str(fake_codex),
        python_executable=str(tmp_path / "missing-python"),
    )

    submitted = manager.submit("Help", "owner", "launch-failure")

    assert submitted["status"] == "failed"
    current = manager.status(submitted["workflow_id"], "owner")
    assert current["status"] == "failed"
    assert current["error"] == "The independent workflow worker could not be started."


def test_module_cli_help_does_not_invoke_codex() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-m", "determined_compute.agent_worker", "--help"],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0
    assert "Durable Codex consultation worker" in result.stdout
