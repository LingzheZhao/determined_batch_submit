# Determined Compute Service

[中文](README.zh.md)

Run Determined jobs through MCP or a JSON CLI. Code, data and outputs stay on mapped shared storage; no project uploads. Use `command` for one-off jobs, `shell` for interactive debugging, and `experiment` for long-running training or trial management.

Use any agent client that can start a local stdio MCP server. The client chooses its model; the standard compute and storage workflow requires neither Codex nor GPT.

## Install

Requires Python 3.10+ and access to a Determined cluster with shared storage.

```bash
git clone https://github.com/WU-CVGL/determined_cluster_mcp.git
cd determined_cluster_mcp
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[mcp]'
```

## Configure

```bash
mkdir -p .local
cp cfg/compute-profile.example.yaml .local/profile.yaml
cp cfg/examples/command_request.json .local/request.json
```

Edit `profile.yaml` with your shared host/container paths, image and resource pool. Edit `request.json` with your command and mapped **container paths** for `workdir` and `output_dir`. Place the workload on shared storage before launching. For login-node access without a local mount, see [shared storage access](docs/shared-storage-access.md).

Create `.local/credentials.env`:

```dotenv
DET_MASTER=https://your-determined-server
DET_API_TOKEN=your-api-token
```

Username/password authentication also supports `DET_USERNAME` and `DET_PASSWORD` in this file. Keep the SQLite database on local disk, outside shared NFS storage.

## Connect an MCP client

Add a stdio server named `determined-compute` in your client's MCP settings. Use these command and argument values in the client's configuration format, replacing `/absolute/path/to/repo` and `your-owner`:

```json
{
  "command": "/absolute/path/to/repo/.venv/bin/determined-compute-mcp",
  "args": [
    "--profile", "/absolute/path/to/repo/.local/profile.yaml",
    "--db", "/absolute/path/to/repo/.local/tasks.sqlite3",
    "--owner", "your-owner",
    "--secrets-file", "/absolute/path/to/repo/.local/credentials.env",
    "--verify-ssl"
  ]
}
```

Use absolute paths. Sessions with the same database and owner share task records; owner names are namespaces, not authentication. If storage access uses an SSH agent, let the MCP process inherit `SSH_AUTH_SOCK` through your client's environment settings.

1. Give the request a meaningful `name` and `description`, then call `compute_plan(request)` with the contents of `request.json`.
2. Call `compute_launch(request, request_id)` and keep the returned `task_id`.
3. Use `compute_status(task_id)` and `compute_logs(task_id)` to follow progress; `compute_cancel(task_id)` stops the task.

Launches check current capacity and avoid queuing by default; set `allow_queue: true` only when queuing is intended. Reuse the same `request_id` when retrying the same launch. If acceptance is uncertain, inspect the existing task before starting another.

To manage a task created through the WebUI, native CLI, or another device under the same Determined account, call `compute_discover(kind, limit=50, offset=0)`, then `compute_adopt(kind, remote_id)`. Discovery is read-only and does not register or submit anything. Adoption verifies the current cluster and account, returns a local `task_id`, and never relaunches the remote task. Use that local ID with the existing status, logs, and cancel tools.

The client can plan directly with these tools. Server-side consultation is disabled by default. To add the optional Codex backend and choose its model, see [consultation setup](docs/agent-workflow.md); it is separate from the client's model choice.

## Use the CLI

The CLI can share the same task records as MCP. Replace `TASK_ID` with the ID returned by launch:

```bash
export DETERMINED_COMPUTE_PROFILE="$PWD/.local/profile.yaml"
export DETERMINED_COMPUTE_DB="$PWD/.local/tasks.sqlite3"
export DETERMINED_COMPUTE_OWNER="$USER"
export DETERMINED_COMPUTE_SECRETS="$PWD/.local/credentials.env"
export DET_VERIFY_SSL=true

determined-compute plan --request-file .local/request.json
determined-compute launch --request-file .local/request.json --request-id my-job-001
determined-compute status TASK_ID
determined-compute logs TASK_ID

determined-compute discover command --limit 20 --offset 0
determined-compute adopt command REMOTE_ID
```

See [request examples](cfg/examples), the [service reference](docs/compute-service.md), or `determined-compute --help`.
