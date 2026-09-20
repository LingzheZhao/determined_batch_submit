<a id="troubleshooting"></a>
# 故障排查

[English](troubleshooting.md) | [简体中文](troubleshooting.zh.md)

[首页](../README.zh.md) · [Agent 工作流](agent-workflow.zh.md) · [计算服务参考](compute-service.zh.md) · [共享存储访问](shared-storage-access.zh.md)

<a id="the-mcp-server-does-not-start"></a>
## MCP 服务无法启动

确认客户端使用虚拟环境中可执行程序的绝对路径，且配置中的每个文件路径都是绝对路径。MCP 进程需要可读的计算 profile、本地持久数据库路径和非空 owner。服务会自动创建数据库父目录，但进程必须有写入权限。

```bash
/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute-mcp --help
/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute --profile /absolute/path/to/profile.yaml plan --request-file /absolute/path/to/request.json
```

服务使用 stdout 传输 MCP 协议帧，启动错误写入 stderr。请在 MCP 客户端的服务日志中查看准确错误。更改安装或升级服务后，重启共享该数据库的所有 MCP 进程，使其加载相同的工具和数据库结构。

计算任务不需要 `--storage-config`。共享路径与已配置的 `host_path` 在本机一致时，存储工具会自动使用该本地路径。需要自定义本地映射或登录节点 SSH 时，将 `cfg/storage-access.example.yaml` 复制为 `.local/storage.yaml`，编辑后再添加 `--storage-config /absolute/path/to/.local/storage.yaml`。

<a id="authentication-fails"></a>
## 身份认证失败

确认 API 地址和凭据属于同一个 Determined 部署。Secrets 文件可以使用 `DET_API_TOKEN`，也可以同时使用 `DET_USERNAME` 和 `DET_PASSWORD`：

```dotenv
DET_MASTER=https://determined.example.org
DET_API_TOKEN=replace-with-your-token
```

不要把凭据放进计算 profile、任务请求、owner、任务名称或描述。限制 secrets 文件的访问权限；排查时只检查必需变量名是否存在，不要读取其值。

配置的 `owner` 不会选择 Determined 用户，它只是本地 SQLite 数据库中的命名空间。远端权限来自 API 凭据。因此更换 owner 无法修复 API 权限错误，共享 owner 也不代表共享远端权限。

<a id="tls-certificate-verification-fails"></a>
## TLS 证书验证失败

使用该部署发布的 CA 证书。如果它已经安装在 MCP 进程使用的操作系统或 Python 运行时信任库中，则不需要额外设置 CA。应用需要独立 PEM bundle 时，将 Requests 的 `REQUESTS_CA_BUNDLE` 设置为其绝对路径，并启用验证：

```bash
export REQUESTS_CA_BUNDLE=/absolute/path/to/organization-ca-bundle.pem
export DET_VERIFY_SSL=true
```

使用 MCP 时，在服务参数中保留 `--verify-ssl`，并通过 stdio 客户端的环境配置传入 CA 变量。外围字段需按客户端的 MCP 语法调整：

```json
{
  "command": "/absolute/path/to/determined_cluster_mcp/.venv/bin/determined-compute-mcp",
  "args": ["--profile", "/absolute/path/to/profile.yaml", "--db", "/absolute/local/path/to/tasks.sqlite3", "--owner", "your-owner", "--secrets-file", "/absolute/path/to/credentials.env", "--verify-ssl"],
  "env": {
    "REQUESTS_CA_BUNDLE": "/absolute/path/to/organization-ca-bundle.pem"
  }
}
```

GUI 应用可能不会继承终端中导出的变量。应在客户端的 MCP 环境设置中配置该变量，或从包含该变量的环境启动客户端，然后重启 MCP 服务。MCP 进程必须能够读取 CA bundle。

正确的 CA 链可以解决未知签发者问题。证书过期或主机名不匹配必须由部署运维方修正；关闭验证不能修复证书身份。

<a id="a-shared-path-is-rejected-or-missing"></a>
## 共享路径被拒绝或不存在

先确认参数要求哪一种路径空间。任务的 `workdir` 和 `output_dir`、`storage_check.path`，以及传输的 `shared_dir` 一侧，都使用计算 profile 中的容器路径。`mounts[].host_path` 是集群计算节点路径。`local_dir` 是运行 MCP 服务的机器上的绝对路径。

规划会检查配置的路径边界，但不会查询远端文件或权限。MCP 服务具有已配置的本地或 SSH 访问方式时，可以使用 `storage_check`。如果任务文件已经位于共享存储，而且不需要从客户端检查或传输，计算操作可以不配置存储访问。

标记为 `read_only: true` 的挂载允许读取和取回，但会拒绝其下的工作目录、输出目录、检查点目标或同步目标。本地映射、SSH 主机密钥、认证和 rsync 要求见[共享存储访问](shared-storage-access.zh.md)。

<a id="ssh-storage-access-fails"></a>
## SSH 存储访问失败

使用能够访问已配置集群计算节点宿主机路径的登录节点 `Host` 别名。网关应配置在 `ProxyJump` 中，而不是作为存储端点。自动化前先完成首次交互连接并核对主机密钥。

使用 `auth: openssh` 时，服务只继承已有 agent。stdio MCP 进程必须继承可用的 `SSH_AUTH_SOCK`；更早启动的 GUI 客户端可能没有该变量。使用密码或 keyring 认证时，遵循[共享存储访问](shared-storage-access.zh.md)中的凭据放置规则。

修正 SSH、路径或权限错误后，始终重新执行 dry run。不要在未审核新的解析端点和逐项变更时，直接把失败的预览改为实际传输。

<a id="capacity-is-unavailable-or-unknown"></a>
## 容量不足或无法确定

针对所需资源池和槽位数调用 `compute_resources`。零槽位检查辅助容器容量，而不是空闲 GPU。容量结果只是快照；新任务提交前服务会再次检查。

当 `allow_queue: false` 时，容量不足或无法确定会拒绝提交，而不是进入队列。不要擅自选择建议的其他资源池、减少资源或设置 `allow_queue: true`；这些变化需要明确的任务决策。认证或资源清单结构错误属于错误，不能当作容量存在的证据。

<a id="submission-outcome-is-uncertain"></a>
## 提交结果不确定

修改请求发出后的连接故障可能表示远端已经接受任务，但客户端没有收到 ID。服务会记录 `submission_uncertain`，且不会自动重试。

保留原本的本地 `task_id`、请求和 `request_id`。不要使用新的 request ID 再次提交。检查该 Determined 账户下的对应远端任务，然后仅对同一条状态不确定的本地提交调用 `compute_reconcile(task_id, remote_id)`。Reconcile 会先验证保留的提交标记，再绑定记录；标记不符会被拒绝。

对于独立创建的远端任务使用 `compute_adopt`。Adopt 不能作为状态不确定的本地提交的绕过手段。具体边界见[发现并登记](agent-workflow.zh.md#discover-and-adopt-existing-remote-tasks)。

<a id="a-task-is-terminal-but-the-result-is-unclear"></a>
## 任务已终止但结果不明确

使用本地 task ID 调用 `compute_status` 和 `compute_logs`。API 提交成功、获得远端 ID 或任务进入终态，本身都不能证明工作负载成功。检查退出信息和提交前定义的成功判据。已经配置存储访问时，用 `storage_check` 验证预期共享产物；需要本地副本时，先预览再执行 `storage_fetch`。没有存储访问时，使用任务输出或另一项明确的任务内检查。

Experiment 在 trial 启动前可能没有 trial 日志。Shell 仍然可用时，可以使用经过清理的重连命令。报告可以包含任务 ID、状态、经过清理的命令、路径和错误，但不得包含凭据值或 secrets 文件内容。

<a id="a-transfer-is-partial-or-different-from-the-preview"></a>
## 传输不完整或与预览不同

传输不会添加 `--delete`，因此目标中无关的文件会保留。正常 rsync 行为仍可能覆盖同名文件。执行中的失败可能留下不完整目标；rsync 退出码 23 明确表示部分文件或属性未能传输。

检查长度受限的传输输出，修正文件系统或配置问题，重新执行 dry run 并审核后再执行。不要自动更改权限保留参数后重试。详细规则见[共享存储访问](shared-storage-access.zh.md)。
