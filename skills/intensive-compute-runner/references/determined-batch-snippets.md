# Determined Batch Snippets

## 目标

- 优先把重任务提交到 Determined。
- 优先把 CPU-only 重任务提交为 0-GPU 任务。
- 交互调试优先使用 Determined AI shell。
- 长时间运行、批量、调参、ablation 优先使用 Determined AI experiments。
- 多 GPU 任务必须提交到集群，必要时排队等待，不转本地。
- 先把代码同步到 `/UNSAFE_SSD4`，再让容器从 `/run/determined/workdir/UNSAFE_SSD4` 读取。

## 常用命令

### 1. 设置 secrets 文件

```bash
export DETERMINED_BATCH_SECRETS=~/.config/determined_batch/.secrets.env
```

如 shell 里尚未设置 `DET_MASTER`，再按 `determined_batch_submit/README.md` 设置：

```bash
export DET_MASTER=http://<determined-master-host>:8080
```

### 2. 查看资源池

```bash
determined-batch list-pools \
  --available-only \
  --min-free-slots 1 \
  --secrets-file ~/.config/determined_batch/.secrets.env
```

对 GPU 任务，将 `--min-free-slots` 改为所需 GPU 数。

对多 GPU 任务，不因当前无空闲资源而改本地；应直接排队提交。

### 3. 同步代码到 `/UNSAFE_SSD4`

仅在目标目录为专用目录时使用 `--delete`：

```bash
TASK_ROOT="/UNSAFE_SSD4/${USER}/codex_jobs/<task-name>"
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

### 4. 提交实验

```bash
determined-batch submit \
  --config /path/to/task.yaml \
  --secrets-file ~/.config/determined_batch/.secrets.env
```

如确实需要把当前目录整体作为 `modelDefinition` 一起上传，再补 `--project-root /path/to/project`。但只要任务入口已能从 `/UNSAFE_SSD4` 读取，同步目录 + bind mount 通常更稳妥。

### 5. 交互调试优先 shell

交互调试、现场复现、环境探查优先用 Determined AI shell；若 GPU shell 无法获得资源，则立刻切回本地：

```bash
zsh -lc 'mamba activate cu129_py311_gcc11 && python train.py --config configs/train.yaml'
```

建议回报是否因为“集群当前无空闲 GPU”而转本地。

## YAML 片段

### CPU-only 0-GPU 任务

`slots_per_trial: 0` 可用于 0-GPU 任务；参考上游样例：`/home/lzzhao/hmnd_ws/whole_body_tracking/determined_batch_submit/upstream/determined/examples/features/unmanaged/2.yaml`。

```yaml
resources:
  slots_per_trial: 0
  resource_pool: "<cpu-or-compatible-pool>"

bind_mounts:
  - host_path: /UNSAFE_SSD4/
    container_path: /run/determined/workdir/UNSAFE_SSD4/

entrypoint: |
  set -eu
  cd /run/determined/workdir/UNSAFE_SSD4/${USER}/codex_jobs/<task-name>/repo
  python scripts/heavy_cpu_job.py --arg value
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
  - host_path: /UNSAFE_SSD4/
    container_path: /run/determined/workdir/UNSAFE_SSD4/

entrypoint: |
  set -eu
  cd /run/determined/workdir/UNSAFE_SSD4/${USER}/codex_jobs/<task-name>/repo
  python train.py --config configs/train.yaml
```

### 典型 `/UNSAFE_SSD4` 挂载样例

- `/home/lzzhao/hmnd_ws/whole_body_tracking/determined_batch_submit/cfg/rebuild_lafan1_rollouts_vae_batch_reboot_v2_mp8_96cpu.yaml`
- `/home/lzzhao/hmnd_ws/whole_body_tracking/determined_batch_submit/cfg/train_meanflow_reboot_v2_hdf5_vds_1gpu.yaml`

## 本地回退

仅在任务必须使用 GPU、但不是多 GPU 任务，且集群当前没有足够空闲 GPU 时本地执行。

```bash
zsh -lc 'mamba activate cu129_py311_gcc11 && python train.py --config configs/train.yaml > logs/local-train.log 2>&1 & echo $!'
```

建议向用户回报：环境、命令、PID、日志路径、停止方式。

## 最终回报模板

- 执行位置：`集群` / `本地`
- 同步目录：`/UNSAFE_SSD4/...`
- 配置文件：`/path/to/task.yaml`
- 模式：`shell` / `experiment`
- 资源：`resource_pool=<pool>`, `slots_per_trial=<n>`
- 标识：`experiment_id=<id>` 或 `pid=<pid>`
- 日志：`/path/to/log`
- 说明：如有本地回退，写明“集群无空闲 GPU”；如为多 GPU 任务，写明已提交集群并等待排队
