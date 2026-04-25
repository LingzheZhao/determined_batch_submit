# Determined Batch Snippets

This file is for reusable command and YAML snippets only. Workflow, setup, and decision rules live in `SKILL.md`.

## Auth and pool checks

```bash
export DETERMINED_BATCH_SECRETS=<secrets-file>
export DET_MASTER=http://<determined-master-host>:8080

determined-batch list-pools \
  --available-only \
  --min-free-slots 1 \
  --secrets-file <secrets-file>
```

For GPU jobs, change `--min-free-slots` to the required GPU count.

## Sync to shared storage

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

If the target shared store rejects owner/group/perms preservation:

```bash
rsync -a --delete --no-owner --no-group --no-perms --omit-dir-times ...
```

## Submit experiment

```bash
determined-batch submit \
  --config <task.yaml> \
  --secrets-file <secrets-file>
```

If the task must upload the current directory as `modelDefinition`, add:

```bash
--project-root <project-root>
```

## Recent log tail

Prefer recent tail logs over rereading from the beginning.

```bash
<experiment-log-command> <experiment-id> --tail <N>
```

## Local fallback

```bash
<shell> -lc '<activate-project-env> && python train.py --config configs/train.yaml > logs/local-train.log 2>&1 & echo $!'
```

## YAML fragments

### CPU-only 0-GPU task

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

### Single-GPU task

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
