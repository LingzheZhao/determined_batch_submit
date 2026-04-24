# Determined Batch Snippets

## 目标

- 优先把重任务提交到 Determined。
- 优先把 CPU-only 重任务提交为 0-GPU 任务。
- 交互调试优先使用 Determined AI shell。
- 长时间运行、批量、调参、ablation 优先使用 Determined AI experiments。
- 多 GPU 任务必须提交到集群，必要时排队等待，不转本地。
- 先把代码同步到配置好的共享存储，再让容器从对应的容器挂载路径读取。

## 常用命令

### 0. 首次使用前先确认

- 当前项目里谁提供以下必填信息：
  - `DET_MASTER`
  - `DETERMINED_BATCH_SECRETS` 或等价 `--secrets-file`
  - 共享存储根目录
  - 容器内对应挂载根目录
  - experiment 的 `checkpoint_storage` 根目录
- 按任务需要再确认：
  - W&B 变量
  - proxy 变量
  - 默认镜像
  - 默认 resource pool
- 仅本地 fallback 时再确认：
  - 使用哪个 shell
  - 本地环境如何激活
- 不要假设这些值都来自同一个地方；先确认它们分别来自用户 export、secrets 文件、helper script、模板，还是生成后的 YAML。

### 1. 设置 secrets 文件

```bash
export DETERMINED_BATCH_SECRETS=<secrets-file>
```

如 shell 里尚未设置 `DET_MASTER`，再按本项目 Determined helper 文档设置：

```bash
export DET_MASTER=http://<determined-master-host>:8080
```

### 2. 查看资源池

```bash
determined-batch list-pools \
  --available-only \
  --min-free-slots 1 \
  --secrets-file <secrets-file>
```

对 GPU 任务，将 `--min-free-slots` 改为所需 GPU 数。

对多 GPU 任务，不因当前无空闲资源而改本地；应直接排队提交。

### 3. 同步代码到共享存储

仅在目标目录为专用目录时使用 `--delete`：

```bash
TASK_ROOT="<shared-storage-root>/<user-or-namespace>/codex_jobs/<task-name>"
mkdir -p "${TASK_ROOT}"

rsync -a --delete \
  --exclude '.git/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude 'experiments_*/' \
  --exclude 'datasets/' \
  --exclude 'deps/' \
  "$PWD/" "${TASK_ROOT}/repo/"
```

如果目标共享存储拒绝保留 owner/group/perms，再加：

```bash
rsync -a --delete --no-owner --no-group --no-perms --omit-dir-times ...
```

不要默认对所有共享存储都加这些参数；普通 user-owned 存储可能需要保留权限位。

### 4. 提交实验

```bash
determined-batch submit \
  --config <task.yaml> \
  --secrets-file <secrets-file>
```

如确实需要把当前目录整体作为 `modelDefinition` 一起上传，再补 `--project-root <project-root>`。但只要任务入口已能从共享存储读取，同步目录 + bind mount 通常更稳妥。

### 5. 交互调试优先 shell

交互调试、现场复现、环境探查优先用 Determined AI shell；若 GPU shell 无法获得资源，则立刻切回本地：

```bash
<shell> -lc '<activate-project-env> && python train.py --config configs/train.yaml'
```

建议回报是否因为“集群当前无空闲 GPU”而转本地。

## YAML 片段

### CPU-only 0-GPU 任务

`slots_per_trial: 0` 可用于 0-GPU 任务；优先参考项目内已验证的 0-GPU Determined 模板。

```yaml
resources:
  slots_per_trial: 0
  resource_pool: "<cpu-or-compatible-pool>"

bind_mounts:
  - host_path: <shared-storage-root>
    container_path: <container-shared-storage-root>

entrypoint: |
  set -eu
  cd <container-shared-storage-root>/<user-or-namespace>/codex_jobs/<task-name>/repo
  python scripts/heavy_cpu_job.py --arg value

checkpoint_storage:
  type: shared_fs
  host_path: <checkpoint-shared-storage-root>
  storage_path: determined-checkpoints
```

### 单 GPU 任务

```yaml
resources:
  slots_per_trial: 1
  resource_pool: "<gpu-pool>"

environment:
  image: "<image>"
  environment_variables:
    - PYTHONUNBUFFERED=1
    - NVIDIA_VISIBLE_DEVICES=all
    - NVIDIA_DRIVER_CAPABILITIES=all

bind_mounts:
  - host_path: <shared-storage-root>
    container_path: <container-shared-storage-root>

entrypoint: |
  set -eu
  cd <container-shared-storage-root>/<user-or-namespace>/codex_jobs/<task-name>/repo
  python train.py --config configs/train.yaml

checkpoint_storage:
  type: shared_fs
  host_path: <checkpoint-shared-storage-root>
  storage_path: determined-checkpoints
```

`checkpoint_storage` 要按目标集群实际可写共享路径显式覆盖；不要假设 Determined 默认 checkpoint storage 对当前 pool 和任务可用。

### 模板来源

优先使用当前项目内已验证的 Determined YAML 模板；不要在 skill 或最终汇报中引用机器本地的绝对模板路径。

## 本地回退

仅在任务必须使用 GPU、但不是多 GPU 任务，且集群当前没有足够空闲 GPU 时本地执行。

```bash
<shell> -lc '<activate-project-env> && python train.py --config configs/train.yaml > logs/local-train.log 2>&1 & echo $!'
```

建议向用户回报：环境、命令、PID、日志路径、停止方式。

## 最终回报模板

- 执行位置：`集群` / `本地`
- 同步目录：`<task-sync-dir>`
- experiment 配置文件：`<experiment.yaml>`
- experiment 资源：`resource_pool=<pool>`, `slots_per_trial=<n>`
- experiment 标识：`experiment_id=<id>`
- experiment checkpoint：`checkpoint_storage=<explicit override>`
- shell 配置文件：`<shell.yaml>`
- shell 资源：`resource_pool=<pool>`, `slots=<n>`
- shell 标识：`shell_id=<id>`
- shell reconnect：`det shell open <id>` 或等价 helper
- 本地标识：`pid=<pid>`
- 日志：`<log-path>`
- 说明：如有本地回退，写明“集群无空闲 GPU”；如为多 GPU 任务，写明已提交集群并等待排队
