"""JSON command-line interface for persistent Determined compute tasks."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import yaml

from determined_compute.compute import ComputeError, ComputeProfile, ComputeService, SQLiteTaskStore
from determined_compute.core.api_client import APIError as ClientAPIError
from determined_compute.core.api_client import DeterminedAPIClient


DEFAULT_DB_PATH = Path("~/.local/state/determined-compute/tasks.sqlite3").expanduser()


def normalize_owner(value: str) -> str:
    """Use the same startup namespace for tasks and consultation workflows."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("owner must be a non-empty string")
    value = value.strip()
    if len(value.encode("utf-8")) > 256:
        raise ValueError("owner is too long")
    return value


class _LazyClient:
    """Construct the API client only when a service operation needs it."""

    def __init__(self, factory: Callable[[], DeterminedAPIClient]) -> None:
        import threading

        self._factory = factory
        self._client: Optional[DeterminedAPIClient] = None
        self._lock = threading.Lock()

    def _get(self) -> DeterminedAPIClient:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    client = self._factory()
                    self._client = client
        return self._client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get(), name)


def _json_dump(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _error_payload(exc: BaseException) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": getattr(exc, "code", "internal_error"),
        "message": str(exc),
    }
    retryable = getattr(exc, "retryable", None)
    if retryable is not None:
        error["retryable"] = bool(retryable)
    details = getattr(exc, "details", None)
    if isinstance(details, dict) and details.get("task_id"):
        error["details"] = {"task_id": str(details["task_id"])}
    elif getattr(exc, "task_id", None):
        error["details"] = {"task_id": str(exc.task_id)}
    return {"ok": False, "error": error}


def _success_payload(result: Any) -> dict[str, Any]:
    return {"ok": True, "result": result}


def _load_request(args: argparse.Namespace) -> dict[str, Any]:
    if args.request_file:
        if args.request_file == "-":
            raw = sys.stdin.read()
        else:
            raw = Path(args.request_file).read_text(encoding="utf-8")
    else:
        raw = args.request
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = yaml.safe_load(raw)
    if not isinstance(value, dict):
        raise ValueError("compute request must be a JSON object")
    return value


def _client_factory(args: argparse.Namespace) -> DeterminedAPIClient:
    return DeterminedAPIClient(
        api_url=args.api_url,
        api_token=args.api_token,
        secrets_path=Path(args.secrets_file) if args.secrets_file else None,
        verify_ssl=args.verify_ssl,
    )


def _resolve_runtime(args: argparse.Namespace) -> tuple[ComputeService, str]:
    profile_path = args.profile or os.environ.get("DETERMINED_COMPUTE_PROFILE")
    if not profile_path:
        raise ValueError("--profile or DETERMINED_COMPUTE_PROFILE is required")

    profile = ComputeProfile.from_file(profile_path)
    client = _LazyClient(lambda: _client_factory(args))
    if args.command == "plan":
        return ComputeService(client, SQLiteTaskStore(":memory:"), profile), ""

    db_path = Path(args.db or os.environ.get("DETERMINED_COMPUTE_DB") or DEFAULT_DB_PATH)
    if db_path == Path(":memory:"):
        raise ValueError("Task management requires a persistent local database; :memory: is unsupported")
    owner = args.owner or os.environ.get("DETERMINED_COMPUTE_OWNER")
    if not owner:
        raise ValueError("--owner or DETERMINED_COMPUTE_OWNER is required")
    owner = normalize_owner(owner)
    if db_path != Path(":memory:"):
        db_path.expanduser().parent.mkdir(parents=True, exist_ok=True)
        db_path = db_path.expanduser()
    store = SQLiteTaskStore(db_path)
    return ComputeService(client, store, profile), owner


def _add_request_args(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--request", help="Compute request as a JSON or YAML object")
    source.add_argument(
        "--request-file",
        metavar="PATH",
        help="Read a JSON/YAML compute request from PATH, or '-' for stdin",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="determined-compute",
        description="Plan and manage persistent Determined compute tasks",
    )
    parser.add_argument("--profile", help="Compute profile YAML (or DETERMINED_COMPUTE_PROFILE)")
    parser.add_argument("--db", help="Shared SQLite task database (or DETERMINED_COMPUTE_DB)")
    parser.add_argument(
        "--owner",
        help="Local owner namespace (or DETERMINED_COMPUTE_OWNER)",
    )
    parser.add_argument("--api-url", help="Determined master URL (defaults to DET_MASTER)")
    parser.add_argument("--api-token", help="Determined API token (defaults to DET_API_TOKEN)")
    parser.add_argument("--secrets-file", help="Path to a KEY=VALUE secrets file")
    verify = parser.add_mutually_exclusive_group()
    verify.add_argument("--verify-ssl", action="store_true", dest="verify_ssl")
    verify.add_argument("--no-verify-ssl", action="store_false", dest="verify_ssl")
    parser.set_defaults(verify_ssl=None)

    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Validate and render a request without cluster access")
    _add_request_args(plan)

    launch = commands.add_parser("launch", help="Launch a request idempotently")
    _add_request_args(launch)
    launch.add_argument("--request-id", required=True, help="Caller-generated idempotency key")

    status = commands.add_parser("status", help="Refresh and show one task")
    status.add_argument("task_id")

    logs = commands.add_parser("logs", help="Fetch the tail of one task's logs")
    logs.add_argument("task_id")
    logs.add_argument("--tail", type=int, default=200)

    cancel = commands.add_parser("cancel", help="Cancel one task")
    cancel.add_argument("task_id")

    reconcile = commands.add_parser(
        "reconcile", help="Bind an uncertain task to a verified remote task id"
    )
    reconcile.add_argument("task_id")
    reconcile.add_argument("remote_id")

    commands.add_parser("list", help="List tasks in the bound owner namespace")
    return parser


def _dispatch(args: argparse.Namespace, service: ComputeService, owner: str) -> Any:
    if args.command == "plan":
        return service.plan(_load_request(args))
    if args.command == "launch":
        return service.launch(_load_request(args), args.request_id, owner)
    if args.command == "status":
        return service.status(args.task_id, owner)
    if args.command == "logs":
        if args.tail < 1:
            raise ValueError("--tail must be at least 1")
        return service.logs(args.task_id, owner, args.tail)
    if args.command == "cancel":
        return service.cancel(args.task_id, owner)
    if args.command == "reconcile":
        return service.reconcile(args.task_id, owner, args.remote_id)
    if args.command == "list":
        return service.list_tasks(owner)
    raise ValueError(f"unknown command: {args.command}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        service, owner = _resolve_runtime(args)
        _json_dump(_success_payload(_dispatch(args, service, owner)))
        return 0
    except (ComputeError, ClientAPIError, OSError, ValueError, yaml.YAMLError) as exc:
        _json_dump(_error_payload(exc))
        return 2
    except Exception as exc:  # keep stdout machine-readable at the process boundary
        _json_dump(_error_payload(exc))
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
