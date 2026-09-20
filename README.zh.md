# Determined Compute Service

[English](README.md)

通过 MCP 或 JSON CLI 运行 Determined 任务。代码、数据和输出保存在映射的共享存储中，不打包上传项目。一次性任务使用 `command`，交互调试使用 `shell`，长时间训练或试验管理使用 `experiment`。

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

使用 Codex 时，在仓库根目录执行：

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

其他 MCP 客户端可通过 stdio 启动同一程序，并传入相同参数。请使用绝对路径。使用相同数据库和 owner 的会话共享任务记录；owner 是命名空间，不是认证机制。

1. 为请求填写清晰的 `name` 和 `description`，再将 `request.json` 的内容传给 `compute_plan(request)`。
2. 调用 `compute_launch(request, request_id)`，保存返回的 `task_id`。
3. 使用 `compute_status(task_id)` 和 `compute_logs(task_id)` 跟进进度；调用 `compute_cancel(task_id)` 停止任务。

提交前默认检查可用容量并避免排队；确实需要排队时显式设置 `allow_queue: true`。重试同一次提交时复用原 `request_id`。如果无法确定是否提交成功，先检查已有任务，再决定后续操作。

可选：调用 `compute_consult(question, request_id)` 启动只读的 `gpt-5.6-sol` 咨询，再用 `workflow_status(workflow_id)` 获取结果。此功能需要已安装并登录的 Codex CLI；worker 会自动读取仓库内的 skill。

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
