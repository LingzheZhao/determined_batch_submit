# Determined Compute Service

[English](README.md)

通过 MCP 或 JSON CLI 运行 Determined 任务。代码、数据和输出保存在映射的共享存储中，不打包上传项目。一次性任务使用 `command`，交互调试使用 `shell`，长时间训练或试验管理使用 `experiment`。

任何能够启动本地 stdio MCP 服务的 agent 客户端都可以使用。模型由客户端选择，常规计算和存储工作流不依赖 Codex 或 GPT。

## 安装

需要 Python 3.10+，以及可访问的 Determined 集群和共享存储。

```bash
git clone https://github.com/WU-CVGL/determined_cluster_mcp.git
cd determined_cluster_mcp
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[mcp]'
```

## 配置

```bash
mkdir -p .local
cp cfg/compute-profile.example.yaml .local/profile.yaml
cp cfg/examples/command_request.json .local/request.json
```

在 `profile.yaml` 中设置共享存储的宿主机路径、容器路径、镜像和资源池。在 `request.json` 中设置命令，并将 `workdir`、`output_dir` 改为映射内的**容器路径**。启动前，将运行所需的文件放到共享存储中。本机没有挂载时，参见[共享存储接入](docs/shared-storage-access.zh.md)。

创建 `.local/credentials.env`：

```dotenv
DET_MASTER=https://your-determined-server
DET_API_TOKEN=your-api-token
```

也可在此文件中使用 `DET_USERNAME` 和 `DET_PASSWORD` 进行认证。SQLite 数据库应保存在本地磁盘上，不要放在共享 NFS 存储中。

## 接入 MCP 客户端

在客户端的 MCP 设置中添加名为 `determined-compute` 的 stdio 服务。按客户端的配置格式填写以下命令和参数，并替换 `/absolute/path/to/repo` 与 `your-owner`：

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

请使用绝对路径。使用相同数据库和 owner 的会话共享任务记录；owner 是命名空间，不是认证机制。若存储访问依赖 SSH 认证代理，请通过客户端的环境设置，让 MCP 进程继承 `SSH_AUTH_SOCK`。

1. 为请求填写清晰的 `name` 和 `description`，再将 `request.json` 的内容传给 `compute_plan(request)`。
2. 调用 `compute_launch(request, request_id)`，保存返回的 `task_id`。
3. 使用 `compute_status(task_id)` 和 `compute_logs(task_id)` 跟进进度；调用 `compute_cancel(task_id)` 停止任务。

提交前默认检查可用容量并避免排队；确实需要排队时显式设置 `allow_queue: true`。重试同一次提交时复用原 `request_id`。如果无法确定是否提交成功，先检查已有任务，再决定后续操作。

客户端可以直接用这些工具规划任务。服务端咨询默认关闭；如需启用可选的 Codex 后端并指定其模型，请参阅[咨询配置](docs/agent-workflow.md)。咨询后端的模型与客户端使用的模型分别配置。

## 使用 CLI

CLI 可以与 MCP 共用任务记录。将 `TASK_ID` 替换为提交时返回的 ID：

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

更多用法见[请求示例](cfg/examples)、[服务参考](docs/compute-service.md)或 `determined-compute --help`。
