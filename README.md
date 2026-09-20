# Determined Compute Service

[中文](README.zh.md)

Run Determined jobs through MCP or a JSON CLI. Code, data and outputs stay on mapped shared storage; no project uploads. Use `command` for one-off jobs, `shell` for interactive debugging, and `experiment` for long-running training or trial management.

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

For Codex, run from the repository root:

```bash
codex mcp add determined-compute -- \
  "$PWD/.venv/bin/determined-compute-mcp" \
  --profile "$PWD/.local/profile.yaml" \
  --db "$PWD/.local/tasks.sqlite3" \
  --owner "$USER" \
  --repo-root "$PWD" \
  --secrets-file "$PWD/.local/credentials.env" \
  --verify-ssl
```

Other MCP clients can launch the same executable and arguments using stdio. Use absolute paths. Sessions with the same database and owner share task records; owner names are namespaces, not authentication.

1. Give the request a meaningful `name` and `description`, then call `compute_plan(request)` with the contents of `request.json`.
2. Call `compute_launch(request, request_id)` and keep the returned `task_id`.
3. Use `compute_status(task_id)` and `compute_logs(task_id)` to follow progress; `compute_cancel(task_id)` stops the task.

Launches check current capacity and avoid queuing by default; set `allow_queue: true` only when queuing is intended. Reuse the same `request_id` when retrying the same launch. If acceptance is uncertain, inspect the existing task before starting another.

Optional: `compute_consult(question, request_id)` starts a read-only `gpt-5.6-sol` consultation; retrieve its result with `workflow_status(workflow_id)`. This requires an installed, signed-in Codex CLI. The worker reads the repository skill automatically.

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
```

See [request examples](cfg/examples), the [service reference](docs/compute-service.md), or `determined-compute --help`.
