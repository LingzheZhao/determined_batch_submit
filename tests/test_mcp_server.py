from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import Client, StdioServerParameters

from determined_batch.mcp_server import create_server


class FakeService:
    def __init__(self) -> None:
        self.calls = []

    def plan(self, request):
        self.calls.append(("plan", request))
        return {"kind": "command", "config": request}

    def launch(self, request, request_id, owner):
        self.calls.append(("launch", request, request_id, owner))
        return {"task_id": "task-1", "owner": owner}

    def status(self, task_id, owner):
        self.calls.append(("status", task_id, owner))
        return {"task_id": task_id, "owner": owner}

    def logs(self, task_id, owner, tail):
        self.calls.append(("logs", task_id, owner, tail))
        return [{"message": "hello"}]

    def cancel(self, task_id, owner):
        self.calls.append(("cancel", task_id, owner))
        return {"task_id": task_id, "state": "cancelling"}

    def reconcile(self, task_id, owner, remote_id):
        self.calls.append(("reconcile", task_id, owner, remote_id))
        return {"task_id": task_id, "remote_id": remote_id, "owner": owner}

    def list_tasks(self, owner):
        self.calls.append(("list", owner))
        return [{"task_id": "task-1", "owner": owner}]


class FakeWorkflowManager:
    def __init__(self) -> None:
        self.calls = []

    def submit(self, question, owner, request_id, context=None):
        self.calls.append(("submit", question, owner, request_id, context))
        return {"workflow_id": "workflow-1", "status": "queued"}

    def status(self, workflow_id, owner):
        self.calls.append(("status", workflow_id, owner))
        return {"workflow_id": workflow_id, "owner": owner, "status": "succeeded"}


def _structured(result):
    return result.structured_content


def test_real_sdk_client_lists_tools_and_invokes_bound_owner():
    async def exercise():
        service = FakeService()
        workflows = FakeWorkflowManager()
        server = create_server(service, "alice", workflows)
        async with Client(server) as client:
            listed = await client.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            assert set(tools) == {
                "compute_plan",
                "compute_launch",
                "compute_status",
                "compute_logs",
                "compute_cancel",
                "compute_reconcile",
                "compute_list_tasks",
                "compute_consult",
                "workflow_status",
            }
            for tool in tools.values():
                assert "owner" not in tool.input_schema.get("properties", {})

            launched = await client.call_tool(
                "compute_launch",
                {"request": {"command": "true"}, "request_id": "req-1"},
            )
            assert _structured(launched)["owner"] == "alice"

            logs = await client.call_tool("compute_logs", {"task_id": "task-1", "tail": 5})
            assert _structured(logs) == {"result": [{"message": "hello"}]}

            consulted = await client.call_tool(
                "compute_consult",
                {
                    "question": "How should this run?",
                    "request_id": "consult-1",
                    "context": {"kind": "command"},
                },
            )
            assert _structured(consulted)["workflow_id"] == "workflow-1"

        assert ("launch", {"command": "true"}, "req-1", "alice") in service.calls
        assert workflows.calls == [
            (
                "submit",
                "How should this run?",
                "alice",
                "consult-1",
                {"kind": "command"},
            )
        ]

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))


def test_tool_errors_are_structured():
    async def exercise():
        server = create_server(FakeService(), "alice")
        async with Client(server) as client:
            result = await client.call_tool(
                "compute_logs", {"task_id": "task-1", "tail": 0}
            )
            assert result.is_error is True
            assert result.structured_content is None
            encoded = result.content[0].text.split(": ", 1)[1]
            assert json.loads(encoded) == {
                "error": {"code": "internal_error", "message": "tail must be at least 1"}
            }

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))


def test_slow_service_call_does_not_block_other_tools():
    class SlowService(FakeService):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def launch(self, request, request_id, owner):
            self.started.set()
            self.release.wait(timeout=2)
            return super().launch(request, request_id, owner)

    async def exercise():
        service = SlowService()
        server = create_server(service, "alice")
        launch = asyncio.create_task(
            server.call_tool(
                "compute_launch",
                {"request": {"command": "true"}, "request_id": "req-1"},
            )
        )
        for _ in range(100):
            if service.started.is_set():
                break
            await asyncio.sleep(0.01)
        assert service.started.is_set()
        try:
            listed = await asyncio.wait_for(
                server.call_tool("compute_list_tasks", {}), timeout=0.5
            )
            assert listed.is_error is False
        finally:
            service.release.set()
        await launch

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))


def test_stdio_subprocess_initializes_and_calls_offline_plan(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "mounts:\n  - host_path: /shared\n    container_path: /shared\n"
        "defaults:\n  image: image\n  pool: pool\n",
        encoding="utf-8",
    )
    # Tests may run from a source checkout or an installed wheel.
    project_root = Path(__file__).resolve().parents[1]
    source_root = str(project_root / "src")
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "determined_batch.mcp_server",
            "--profile",
            str(profile),
            "--db",
            str(tmp_path / "tasks.db"),
            "--owner",
            "alice",
            "--repo-root",
            str(project_root),
        ],
        env={"PYTHONPATH": source_root},
        cwd=str(tmp_path),
    )

    async def exercise():
        async with Client(params) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert "compute_plan" in tools
            result = await client.call_tool(
                "compute_plan",
                {
                    "request": {
                        "command": ["echo", "hello"],
                        "workdir": "/shared/work",
                        "output_dir": "/shared/output",
                    }
                },
            )
            assert result.is_error is False
            assert result.structured_content["kind"] == "command"

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))
