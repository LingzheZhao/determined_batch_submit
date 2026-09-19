"""Durable, read-only Codex consultation workflows.

The MCP server submits work here and immediately returns a workflow id.  Each
request is executed by a detached ``python -m determined_batch.agent_worker``
process so the work does not depend on the lifetime of an MCP connection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple
import uuid


DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_STALE_AFTER_SECONDS = 120.0
MAX_QUESTION_BYTES = 16 * 1024
MAX_CONTEXT_BYTES = 64 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_SKILL_BYTES = 128 * 1024

_TERMINAL_STATUSES = frozenset(("succeeded", "failed", "timed_out"))
_SENSITIVE_KEY_CONTAINS = frozenset(
    (
        "authorization",
        "cookie",
        "credential",
        "passwd",
        "password",
        "secret",
    )
)
_SENSITIVE_KEY_SUFFIXES = frozenset(
    (
        "accesskey",
        "accesstoken",
        "apikey",
        "apitoken",
        "authtoken",
        "bearertoken",
        "privatekey",
        "refreshtoken",
        "sessionkey",
        "sessiontoken",
    )
)
_SENSITIVE_KEY_EXACT = frozenset(("auth", "cookie", "token"))


class WorkflowNotFoundError(KeyError):
    """Raised when a workflow is absent or belongs to another owner."""

    code = "workflow_not_found"


class WorkflowConflictError(ValueError):
    """Raised when a request id is reused with a different payload."""

    code = "workflow_conflict"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _json_size(value: str) -> int:
    return len(value.encode("utf-8"))


def _normalize_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _is_sensitive_key(key: Any) -> bool:
    normalized = _normalize_key(key)
    return (
        normalized in _SENSITIVE_KEY_EXACT
        or any(part in normalized for part in _SENSITIVE_KEY_CONTAINS)
        or any(normalized.endswith(part) for part in _SENSITIVE_KEY_SUFFIXES)
    )


def _find_sensitive_key(value: Any, path: str = "context") -> Optional[str]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _is_sensitive_key(key):
                return f"{path}.{key}"
            found = _find_sensitive_key(child, f"{path}.{key}")
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found = _find_sensitive_key(child, f"{path}[{index}]")
            if found:
                return found
    return None


def _pid_is_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _bounded_text(path: Path, limit: int) -> Tuple[str, bool]:
    try:
        with path.open("rb") as handle:
            content = handle.read(limit + 1)
    except OSError:
        return "", False
    truncated = len(content) > limit
    if truncated:
        content = content[:limit]
    return content.decode("utf-8", errors="replace"), truncated


def _sanitized_environment(repo_root: Path) -> Dict[str, str]:
    """Build the small environment inherited by worker and Codex processes.

    Codex authentication still comes from ``CODEX_HOME``.  Cluster/API tokens,
    secrets-file variables, and user MCP configuration are deliberately absent.
    """

    allowed = (
        "CODEX_HOME",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "USER",
    )
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    source_root = repo_root / "src"
    if source_root.is_dir():
        env["PYTHONPATH"] = str(source_root)
    env["PYTHONUNBUFFERED"] = "1"
    return env


class WorkflowManager:
    """Persist, launch, and inspect bounded Codex consultation workflows."""

    def __init__(
        self,
        db_path: Any,
        repo_root: Any,
        *,
        codex_bin: str = "codex",
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        auto_start: bool = True,
        python_executable: Optional[str] = None,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.skill_path = (
            self.repo_root / "skills" / "intensive-compute-runner" / "SKILL.md"
        )
        self.codex_bin = str(codex_bin)
        self.model = str(model)
        self.timeout_seconds = float(timeout_seconds)
        self.stale_after_seconds = float(stale_after_seconds)
        self.auto_start = bool(auto_start)
        self.python_executable = python_executable or sys.executable

        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        if not self.repo_root.is_dir():
            raise ValueError("repo_root must be an existing directory")
        if not self.skill_path.is_file():
            raise ValueError(
                "repo_root must contain skills/intensive-compute-runner/SKILL.md"
            )

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_workflows (
                    workflow_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    heartbeat_at TEXT,
                    lease_id TEXT,
                    worker_pid INTEGER,
                    agent_pid INTEGER,
                    result TEXT,
                    error TEXT,
                    UNIQUE(owner, request_id),
                    CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'timed_out'))
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_workflow_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT NOT NULL,
                    at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    FOREIGN KEY(workflow_id) REFERENCES agent_workflows(workflow_id)
                        ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_workflow_logs_workflow
                ON agent_workflow_logs(workflow_id, id)
                """
            )

    @staticmethod
    def _validate_identifier(value: str, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        value = value.strip()
        if _json_size(value) > 256:
            raise ValueError(f"{name} is too long")
        return value

    @staticmethod
    def _prepare_payload(
        question: str, context: Optional[Dict[str, Any]]
    ) -> Tuple[str, str, str]:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a non-empty string")
        question = question.strip()
        if _json_size(question) > MAX_QUESTION_BYTES:
            raise ValueError("question is too long")
        if context is None:
            context = {}
        if not isinstance(context, dict):
            raise ValueError("context must be a JSON object")
        sensitive_path = _find_sensitive_key(context)
        if sensitive_path:
            raise ValueError(f"sensitive context field is not allowed: {sensitive_path}")
        try:
            context_json = json.dumps(
                context,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("context must contain only finite JSON values") from exc
        if _json_size(context_json) > MAX_CONTEXT_BYTES:
            raise ValueError("context is too large")
        digest_input = json.dumps(
            {"question": question, "context": context},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        payload_hash = hashlib.sha256(digest_input).hexdigest()
        return question, context_json, payload_hash

    def submit(
        self,
        question: str,
        owner: str,
        request_id: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Persist a request and launch an independent worker process.

        Reusing ``(owner, request_id)`` with identical content is idempotent.
        Reusing it with different content raises :class:`WorkflowConflictError`.
        """

        owner = self._validate_identifier(owner, "owner")
        request_id = self._validate_identifier(request_id, "request_id")
        question, context_json, payload_hash = self._prepare_payload(question, context)
        workflow_id = str(uuid.uuid4())
        now = _utc_now()
        dispatch_worker = self.auto_start
        retry_dispatch = False
        previous_heartbeat: Optional[str] = None

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT workflow_id, request_id, status, created_at, heartbeat_at,
                       payload_hash
                FROM agent_workflows WHERE owner = ? AND request_id = ?
                """,
                (owner, request_id),
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    connection.rollback()
                    raise WorkflowConflictError(
                        "request_id already exists for this owner with different content"
                    )
                workflow_id = existing["workflow_id"]
                previous_heartbeat = existing["heartbeat_at"]
                response = {
                    "workflow_id": existing["workflow_id"],
                    "request_id": existing["request_id"],
                    "status": existing["status"],
                    "deduplicated": True,
                    "created_at": existing["created_at"],
                }
                dispatch_worker = False
                if (
                    self.auto_start
                    and existing["status"] == "queued"
                    and self._timestamp_is_stale(
                        existing["heartbeat_at"] or existing["created_at"]
                    )
                ):
                    dispatch_worker = True
                    retry_dispatch = True
                    connection.execute(
                        """
                        UPDATE agent_workflows
                        SET heartbeat_at = ?, updated_at = ?, worker_pid = NULL
                        WHERE workflow_id = ? AND status = 'queued'
                        """,
                        (now, now, workflow_id),
                    )
                    self._append_log(
                        connection,
                        workflow_id,
                        "warning",
                        "Retrying dispatch for a stale queued workflow.",
                        now,
                    )
            else:
                connection.execute(
                    """
                    INSERT INTO agent_workflows (
                        workflow_id, owner, request_id, question, context_json,
                        payload_hash, status, created_at, updated_at, heartbeat_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                    """,
                    (
                        workflow_id,
                        owner,
                        request_id,
                        question,
                        context_json,
                        payload_hash,
                        now,
                        now,
                        now,
                    ),
                )
                self._append_log(connection, workflow_id, "info", "Workflow queued.", now)
                response = {
                    "workflow_id": workflow_id,
                    "request_id": request_id,
                    "status": "queued",
                    "deduplicated": False,
                    "created_at": now,
                }
            connection.commit()
        finally:
            connection.close()

        if dispatch_worker:
            try:
                worker_pid = self._spawn_worker(workflow_id)
            except Exception:
                if retry_dispatch:
                    self._mark_retry_dispatch_failure(workflow_id, previous_heartbeat)
                else:
                    self._mark_launch_failure(workflow_id)
                    response["status"] = "failed"
            else:
                try:
                    self._record_worker_dispatch(workflow_id, worker_pid)
                except sqlite3.Error:
                    # The worker has already started and will claim transactionally.
                    pass

        return response

    def _timestamp_is_stale(self, value: Optional[str]) -> bool:
        timestamp = _parse_time(value)
        if timestamp is None:
            return True
        return (
            datetime.now(timezone.utc) - timestamp
        ).total_seconds() > self.stale_after_seconds

    def _spawn_worker(self, workflow_id: str) -> int:
        command = [
            self.python_executable,
            "-m",
            "determined_batch.agent_worker",
            "worker",
            "--db",
            str(self.db_path),
            "--repo-root",
            str(self.repo_root),
            "--workflow-id",
            workflow_id,
            "--codex-bin",
            self.codex_bin,
            "--model",
            self.model,
            "--timeout-seconds",
            str(self.timeout_seconds),
            "--stale-after-seconds",
            str(self.stale_after_seconds),
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(self.repo_root),
            env=_sanitized_environment(self.repo_root),
            close_fds=True,
            start_new_session=True,
        )
        return process.pid

    def _record_worker_dispatch(self, workflow_id: str, worker_pid: int) -> None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_workflows
                SET worker_pid = ?, heartbeat_at = ?, updated_at = ?
                WHERE workflow_id = ? AND status = 'queued'
                """,
                (worker_pid, now, now, workflow_id),
            )

    def _mark_retry_dispatch_failure(
        self, workflow_id: str, previous_heartbeat: Optional[str]
    ) -> None:
        now = _utc_now()
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE agent_workflows
                SET heartbeat_at = ?, updated_at = ?, worker_pid = NULL
                WHERE workflow_id = ? AND status = 'queued'
                """,
                (previous_heartbeat, now, workflow_id),
            ).rowcount
            if updated:
                self._append_log(
                    connection,
                    workflow_id,
                    "error",
                    "Replacement worker launch failed; workflow remains recoverable.",
                    now,
                )

    def _mark_launch_failure(self, workflow_id: str) -> None:
        now = _utc_now()
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE agent_workflows
                SET status = 'failed', updated_at = ?, finished_at = ?,
                    error = 'The independent workflow worker could not be started.'
                WHERE workflow_id = ? AND status = 'queued'
                """,
                (now, now, workflow_id),
            ).rowcount
            if updated:
                self._append_log(
                    connection, workflow_id, "error", "Worker launch failed.", now
                )

    def status(self, workflow_id: str, owner: str) -> Dict[str, Any]:
        """Return owner-scoped workflow state, bounded logs, and final output."""

        owner = self._validate_identifier(owner, "owner")
        workflow_id = self._validate_identifier(workflow_id, "workflow_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT workflow_id, request_id, status, created_at, updated_at,
                       started_at, finished_at, heartbeat_at, worker_pid,
                       agent_pid, result, error
                FROM agent_workflows WHERE workflow_id = ? AND owner = ?
                """,
                (workflow_id, owner),
            ).fetchone()
            if row is None:
                raise WorkflowNotFoundError("workflow not found")
            logs = connection.execute(
                """
                SELECT at, level, message FROM agent_workflow_logs
                WHERE workflow_id = ? ORDER BY id ASC LIMIT 100
                """,
                (workflow_id,),
            ).fetchall()
        result = dict(row)
        heartbeat = _parse_time(result.pop("heartbeat_at"))
        worker_pid = result.pop("worker_pid")
        agent_pid = result.pop("agent_pid")
        stale = False
        recoverable = False
        if result["status"] in ("queued", "running"):
            stale = self._timestamp_is_stale(
                heartbeat.isoformat() if heartbeat is not None else None
            )
            if result["status"] == "queued":
                # A second worker is safe: the database claim permits one Codex run.
                recoverable = stale
            else:
                recoverable = stale and not (
                    _pid_is_alive(worker_pid) or _pid_is_alive(agent_pid)
                )
        result["stale"] = stale
        result["recoverable"] = recoverable
        result["logs"] = [dict(log) for log in logs]
        return result

    def _status_unscoped(self, workflow_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT owner FROM agent_workflows WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
        if row is None:
            raise WorkflowNotFoundError("workflow not found")
        return self.status(workflow_id, row["owner"])

    @staticmethod
    def _append_log(
        connection: sqlite3.Connection,
        workflow_id: str,
        level: str,
        message: str,
        at: Optional[str] = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO agent_workflow_logs(workflow_id, at, level, message)
            VALUES (?, ?, ?, ?)
            """,
            (workflow_id, at or _utc_now(), level, message),
        )

    def _claim(self, workflow_id: str) -> Optional[Tuple[sqlite3.Row, str]]:
        now = _utc_now()
        lease_id = str(uuid.uuid4())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM agent_workflows WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WorkflowNotFoundError("workflow not found")
            if row["status"] in _TERMINAL_STATUSES:
                connection.commit()
                return None

            reclaimed = False
            if row["status"] == "running":
                if not self._timestamp_is_stale(row["heartbeat_at"]):
                    connection.commit()
                    return None
                if _pid_is_alive(row["worker_pid"]) or _pid_is_alive(row["agent_pid"]):
                    connection.commit()
                    return None
                reclaimed = True

            connection.execute(
                """
                UPDATE agent_workflows
                SET status = 'running', updated_at = ?,
                    started_at = COALESCE(started_at, ?), finished_at = NULL,
                    heartbeat_at = ?, lease_id = ?, worker_pid = ?, agent_pid = NULL,
                    result = NULL, error = NULL
                WHERE workflow_id = ?
                """,
                (now, now, now, lease_id, os.getpid(), workflow_id),
            )
            message = (
                "Reclaimed an interrupted stale workflow after verifying its processes exited."
                if reclaimed
                else "Worker claimed workflow."
            )
            self._append_log(
                connection, workflow_id, "warning" if reclaimed else "info", message, now
            )
            claimed = connection.execute(
                "SELECT * FROM agent_workflows WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            connection.commit()
            return claimed, lease_id
        finally:
            connection.close()

    def _set_agent_pid(self, workflow_id: str, lease_id: str, pid: int) -> None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_workflows SET agent_pid = ?, heartbeat_at = ?, updated_at = ?
                WHERE workflow_id = ? AND lease_id = ? AND status = 'running'
                """,
                (pid, now, now, workflow_id, lease_id),
            )

    def _heartbeat(self, workflow_id: str, lease_id: str) -> bool:
        now = _utc_now()
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE agent_workflows SET heartbeat_at = ?, updated_at = ?
                WHERE workflow_id = ? AND lease_id = ? AND status = 'running'
                """,
                (now, now, workflow_id, lease_id),
            ).rowcount
        return bool(updated)

    def _finish(
        self,
        workflow_id: str,
        lease_id: str,
        status: str,
        *,
        result: Optional[str] = None,
        error: Optional[str] = None,
        log_message: str,
    ) -> None:
        now = _utc_now()
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE agent_workflows
                SET status = ?, updated_at = ?, finished_at = ?, heartbeat_at = ?,
                    worker_pid = NULL, agent_pid = NULL, result = ?, error = ?
                WHERE workflow_id = ? AND lease_id = ? AND status = 'running'
                """,
                (status, now, now, now, result, error, workflow_id, lease_id),
            ).rowcount
            if updated:
                self._append_log(
                    connection,
                    workflow_id,
                    "info" if status == "succeeded" else "error",
                    log_message,
                    now,
                )

    def _build_prompt(self, row: sqlite3.Row) -> str:
        skill_text, truncated = _bounded_text(self.skill_path, MAX_SKILL_BYTES)
        if not skill_text:
            raise RuntimeError("The repository compute skill could not be read.")
        if truncated:
            raise RuntimeError("The repository compute skill exceeds the supported size.")
        context = json.loads(row["context_json"])
        context_text = json.dumps(context, indent=2, sort_keys=True, ensure_ascii=False)
        return (
            "You are the dedicated compute consultation worker for this repository.\n"
            "Use the repository skill below as policy and inspect the repository when useful.\n"
            "This workflow is consult/diagnose only: do not launch, cancel, submit, mutate, "
            "or edit anything. Do not call an MCP server or delegate to another agent. "
            "Give a bounded, practical diagnosis or plan that a caller can review before "
            "using explicit deterministic service tools for mutations. Never reveal secrets.\n\n"
            "<repository_skill>\n"
            + skill_text
            + "\n</repository_skill>\n\n"
            "<question>\n"
            + row["question"]
            + "\n</question>\n\n"
            "<curated_context>\n"
            + context_text
            + "\n</curated_context>\n"
        )

    @staticmethod
    def _stop_process_group(process: subprocess.Popen[Any]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, AttributeError):
            try:
                process.terminate()
            except OSError:
                return
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            try:
                process.kill()
            except OSError:
                return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def run_pending(self, workflow_id: str) -> Dict[str, Any]:
        """Claim and run one queued (or safely reclaimable stale) workflow."""

        workflow_id = self._validate_identifier(workflow_id, "workflow_id")
        claimed = self._claim(workflow_id)
        if claimed is None:
            return self._status_unscoped(workflow_id)
        row, lease_id = claimed

        try:
            prompt = self._build_prompt(row)
        except Exception:
            self._finish(
                workflow_id,
                lease_id,
                "failed",
                error="The repository compute skill could not be loaded.",
                log_message="Workflow setup failed.",
            )
            return self._status_unscoped(workflow_id)

        with tempfile.TemporaryDirectory(prefix="determined-agent-") as temp_dir:
            output_path = Path(temp_dir) / "last-message.txt"
            prompt_path = Path(temp_dir) / "prompt.txt"
            command = [
                self.codex_bin,
                "exec",
                "--ignore-user-config",
                "--ignore-rules",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--model",
                self.model,
                "--json",
                "--color",
                "never",
                "--output-last-message",
                str(output_path),
                "--cd",
                str(self.repo_root),
                "--config",
                "mcp_servers={}",
                "-",
            ]
            process: Optional[subprocess.Popen[Any]] = None
            try:
                prompt_path.write_text(prompt, encoding="utf-8")
                with prompt_path.open("r", encoding="utf-8") as prompt_input:
                    process = subprocess.Popen(
                        command,
                        stdin=prompt_input,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        cwd=str(self.repo_root),
                        env=_sanitized_environment(self.repo_root),
                        close_fds=True,
                        start_new_session=True,
                    )
                self._set_agent_pid(workflow_id, lease_id, process.pid)
                started = time.monotonic()
                heartbeat_due = started
                timed_out = False
                while process.poll() is None:
                    now_mono = time.monotonic()
                    if now_mono - started >= self.timeout_seconds:
                        timed_out = True
                        self._stop_process_group(process)
                        break
                    if now_mono >= heartbeat_due:
                        if not self._heartbeat(workflow_id, lease_id):
                            self._stop_process_group(process)
                            return self._status_unscoped(workflow_id)
                        heartbeat_due = now_mono + min(5.0, self.stale_after_seconds / 3.0)
                    time.sleep(0.1)

                if timed_out:
                    self._finish(
                        workflow_id,
                        lease_id,
                        "timed_out",
                        error="The consultation exceeded its execution time limit.",
                        log_message="Workflow timed out and its process group was stopped.",
                    )
                elif process.returncode != 0:
                    self._finish(
                        workflow_id,
                        lease_id,
                        "failed",
                        error=f"Codex exited unsuccessfully (exit code {process.returncode}).",
                        log_message="Codex consultation failed.",
                    )
                else:
                    result, truncated = _bounded_text(output_path, MAX_RESULT_BYTES)
                    result = result.strip()
                    if truncated:
                        result += "\n\n[Result truncated at 64 KiB.]"
                    if not result:
                        self._finish(
                            workflow_id,
                            lease_id,
                            "failed",
                            error="Codex completed without a final response.",
                            log_message="Codex produced no final response.",
                        )
                    else:
                        self._finish(
                            workflow_id,
                            lease_id,
                            "succeeded",
                            result=result,
                            log_message="Workflow completed.",
                        )
            except FileNotFoundError:
                self._finish(
                    workflow_id,
                    lease_id,
                    "failed",
                    error="The configured Codex executable was not found.",
                    log_message="Codex could not be started.",
                )
            except Exception:
                if process is not None:
                    self._stop_process_group(process)
                self._finish(
                    workflow_id,
                    lease_id,
                    "failed",
                    error="The consultation worker encountered an internal execution error.",
                    log_message="Workflow execution failed.",
                )
        return self._status_unscoped(workflow_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Durable Codex consultation worker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    worker = subparsers.add_parser("worker", help="Run one queued workflow")
    worker.add_argument("--db", required=True, help="Workflow SQLite database")
    worker.add_argument("--repo-root", required=True, help="Repository root")
    worker.add_argument("--workflow-id", required=True, help="Workflow UUID")
    worker.add_argument("--codex-bin", default="codex", help="Codex executable")
    worker.add_argument("--model", default=DEFAULT_MODEL, help="Dedicated worker model")
    worker.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS
    )
    worker.add_argument(
        "--stale-after-seconds", type=float, default=DEFAULT_STALE_AFTER_SECONDS
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "worker":
        manager = WorkflowManager(
            args.db,
            args.repo_root,
            codex_bin=args.codex_bin,
            model=args.model,
            timeout_seconds=args.timeout_seconds,
            stale_after_seconds=args.stale_after_seconds,
            auto_start=False,
        )
        result = manager.run_pending(args.workflow_id)
        return 0 if result["status"] in ("succeeded", "running") else 1
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_MODEL",
    "WorkflowConflictError",
    "WorkflowManager",
    "WorkflowNotFoundError",
    "build_parser",
    "main",
]
