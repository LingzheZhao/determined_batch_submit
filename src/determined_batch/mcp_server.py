"""Local stdio MCP adapter for the persistent compute service.

The configured owner is a local namespace, not an authentication mechanism.
This server is intended for a trusted same-user MCP client launched as a child
process.  No tool accepts an owner argument.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional, Sequence

from determined_batch.compute import ComputeError, ComputeProfile, ComputeService, SQLiteTaskStore
from determined_batch.compute_cli import DEFAULT_DB_PATH, _LazyClient, normalize_owner
from determined_batch.core.api_client import APIError as ClientAPIError
from determined_batch.core.api_client import DeterminedAPIClient


def _tool_error(exc: BaseException) -> dict[str, Any]:
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
    return {"error": error}


def create_server(
    service: ComputeService,
    owner: str,
    workflow_manager: Any = None,
) -> Any:
    """Create an MCP server bound to one local owner namespace."""

    owner = normalize_owner(owner)
    try:
        from mcp.server import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # pragma: no cover - exercised without the optional extra
        raise RuntimeError(
            "MCP support is not installed; install determined-batch[mcp]"
        ) from exc

    server = MCPServer("determined-compute")

    def fail(exc: BaseException) -> None:
        raise ToolError(json.dumps(_tool_error(exc), sort_keys=True, separators=(",", ":")))

    async def call(operation: Any, *args: Any) -> Any:
        try:
            return await asyncio.to_thread(operation, *args)
        except (ComputeError, ClientAPIError) as exc:
            fail(exc)
        except (OSError, ValueError) as exc:
            fail(exc)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False,
    ))
    async def compute_plan(request: dict[str, Any]) -> dict[str, Any]:
        """Validate and render a compute request without contacting the cluster."""

        return await call(service.plan, request)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_launch(request: dict[str, Any], request_id: str) -> dict[str, Any]:
        """Launch a compute request idempotently in the server's owner namespace."""

        return await call(service.launch, request, request_id, owner)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_status(task_id: str) -> dict[str, Any]:
        """Refresh and return a task in the server's owner namespace."""

        return await call(service.status, task_id, owner)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_logs(task_id: str, tail: int = 200) -> list[Any]:
        """Return the latest task log records; tail must be a positive integer."""

        if tail < 1:
            fail(ValueError("tail must be at least 1"))
        return await call(service.logs, task_id, owner, tail)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_cancel(task_id: str) -> dict[str, Any]:
        """Cancel a task in the server's owner namespace."""

        return await call(service.cancel, task_id, owner)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
    ))
    async def compute_reconcile(task_id: str, remote_id: str) -> dict[str, Any]:
        """Bind an uncertain task to a remote id after verifying its identity marker."""

        return await call(service.reconcile, task_id, owner, remote_id)

    @server.tool(annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False,
    ))
    async def compute_list_tasks() -> list[dict[str, Any]]:
        """List tasks in the server's owner namespace."""

        return await call(service.list_tasks, owner)

    if workflow_manager is not None:

        @server.tool(annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
        ))
        async def compute_consult(
            question: str,
            request_id: str,
            context: Optional[dict[str, Any]] = None,
        ) -> dict[str, Any]:
            """Queue a read-only Codex consultation for this repository."""

            try:
                return await asyncio.to_thread(
                    workflow_manager.submit, question, owner, request_id, context
                )
            except (OSError, ValueError) as exc:
                fail(exc)
            except Exception as exc:
                if getattr(exc, "code", None) == "workflow_conflict":
                    fail(exc)
                raise

        @server.tool(annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False,
        ))
        async def workflow_status(workflow_id: str) -> dict[str, Any]:
            """Return persisted status and logs for one consultation workflow."""

            try:
                return await asyncio.to_thread(workflow_manager.status, workflow_id, owner)
            except (OSError, ValueError) as exc:
                fail(exc)
            except Exception as exc:
                if getattr(exc, "code", None) == "workflow_not_found":
                    fail(exc)
                raise

    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="determined-compute-mcp",
        description="Run the trusted local Determined compute MCP server over stdio",
    )
    parser.add_argument("--profile", help="Compute profile YAML (or DETERMINED_COMPUTE_PROFILE)")
    parser.add_argument("--db", help="Shared SQLite database (or DETERMINED_COMPUTE_DB)")
    parser.add_argument(
        "--owner",
        help="Bound owner namespace (or DETERMINED_COMPUTE_OWNER)",
    )
    parser.add_argument("--repo-root", help="Repository root used by compute_consult")
    parser.add_argument("--api-url", help="Determined master URL (defaults to DET_MASTER)")
    parser.add_argument("--api-token", help="Determined API token (defaults to DET_API_TOKEN)")
    parser.add_argument("--secrets-file", help="Path to a KEY=VALUE secrets file")
    verify = parser.add_mutually_exclusive_group()
    verify.add_argument("--verify-ssl", action="store_true", dest="verify_ssl")
    verify.add_argument("--no-verify-ssl", action="store_false", dest="verify_ssl")
    parser.set_defaults(verify_ssl=None)
    return parser


def _runtime(args: argparse.Namespace) -> tuple[Any, str]:
    profile_path = args.profile or os.environ.get("DETERMINED_COMPUTE_PROFILE")
    if not profile_path:
        raise ValueError("--profile or DETERMINED_COMPUTE_PROFILE is required")

    db_path = Path(args.db or os.environ.get("DETERMINED_COMPUTE_DB") or DEFAULT_DB_PATH)
    if db_path == Path(":memory:"):
        raise ValueError("MCP requires a persistent local database; :memory: is unsupported")
    owner = args.owner or os.environ.get("DETERMINED_COMPUTE_OWNER")
    if not owner:
        raise ValueError("--owner or DETERMINED_COMPUTE_OWNER is required")
    owner = normalize_owner(owner)
    if db_path != Path(":memory:"):
        db_path.expanduser().parent.mkdir(parents=True, exist_ok=True)
        db_path = db_path.expanduser()
    repo_root = Path(
        args.repo_root or os.environ.get("DETERMINED_COMPUTE_REPO_ROOT") or os.getcwd()
    ).resolve()
    profile = ComputeProfile.from_file(profile_path)
    store = SQLiteTaskStore(db_path)

    def make_client() -> DeterminedAPIClient:
        return DeterminedAPIClient(
            api_url=args.api_url,
            api_token=args.api_token,
            secrets_path=Path(args.secrets_file) if args.secrets_file else None,
            verify_ssl=args.verify_ssl,
        )

    service = ComputeService(_LazyClient(make_client), store, profile)

    try:
        from determined_batch.agent_worker import WorkflowManager
    except ImportError:
        workflow_manager = None
    else:
        workflow_manager = WorkflowManager(db_path, repo_root)
    return create_server(service, owner, workflow_manager), owner


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        server, _owner = _runtime(args)
    except Exception as exc:
        # stdout is reserved for MCP frames.
        print(f"determined-compute-mcp: {exc}", file=os.sys.stderr)
        return 2
    server.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
