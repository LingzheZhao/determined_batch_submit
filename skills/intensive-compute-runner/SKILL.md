---
name: intensive-compute-runner
description: Use when a task is GPU-intensive, CPU-intensive, long-running, memory-heavy, or would monopolize local resources, including training, evaluation, rendering, simulation, large preprocessing, or batch inference.
---

# Intensive Compute Runner

## Overview

- Default to Determined AI cluster execution for heavy GPU or CPU work.
- Prefer the `determined_batch_submit` package and its `determined-batch` CLI; if it is missing, install it before inventing ad hoc submission flows.
- Treat CPU-only heavy jobs as cluster candidates too; prefer `slots_per_trial: 0` before using the local machine.
- Use Determined AI shell for interactive debugging and short iterative investigation on cluster GPUs.
- Use Determined AI experiments for long-running jobs, batch sweeps, tuning, ablations, rendering, and queued submissions.
- Multi-GPU work must run on the Determined cluster; queue and wait if necessary instead of moving it local.
- Sync runnable code and configs into `/UNSAFE_SSD4` before cluster submission so containers can read the same files from `/run/determined/workdir/UNSAFE_SSD4`.
- Fall back to local execution only when GPU is required, the task is not multi-GPU, and the cluster does not currently have enough free GPU slots.
- Prefer asynchronous submission and report experiment IDs, shell sessions, config paths, and logs instead of blocking the terminal on long runs.

## When To Use

- GPU training, finetuning, evaluation, rendering, simulation, export, or batch inference.
- CPU-only jobs that are still heavy: large preprocessing, dataset materialization, multi-process conversion, long analysis scripts, or jobs that need many cores or large memory.
- Any task that would occupy the local machine long enough to hurt development flow.

## Do Not Use

- Short local commands that finish quickly and do not meaningfully occupy CPU or GPU.
- Small verification jobs unless the user explicitly wants the cluster path.

## Workflow

### 1. Decide cluster vs local

- First determine whether the task is interactive debugging, a long-running job, a batch queue, or a local-sized quick check.
- Then determine whether the task truly requires GPU, and whether it requires more than one GPU.
- If the task is interactive debugging and needs a cluster GPU, prefer Determined AI shell.
- If the task is long-running, batched, or reproducible, prefer a Determined experiment.
- If the task is CPU-only but heavy, still prefer Determined with `slots_per_trial: 0`.
- If the task needs multiple GPUs, it must stay on the cluster. Submit it and wait in queue if resources are busy.
- Only run locally when the task needs GPU, does not require multiple GPUs, and the cluster currently cannot provide enough free GPU slots.

### 2. Prepare Determined tooling and auth

- Prefer the `determined-batch` CLI from `https://github.com/LingzheZhao/determined_batch_submit`.
- If it is not installed, install it first:
  - `git clone https://github.com/LingzheZhao/determined_batch_submit`
  - `cd determined_batch_submit`
  - `python -m pip install -e .`
- Use `~/.config/determined_batch/.secrets.env` for authentication via `--secrets-file` or `DETERMINED_BATCH_SECRETS`.
- If `DET_MASTER` is not set, follow the repository README and export it before submitting.
- Never print secret values; reference only variable names, paths, and command forms.
- Treat this skill as the default Codex policy for heavy compute work: future tasks should prefer this path unless the user explicitly asks for a different execution location.

### 3. Sync code into `/UNSAFE_SSD4`

- Sync the runnable code, configs, helper scripts, and lightweight assets into a dedicated directory such as `/UNSAFE_SSD4/${USER}/codex_jobs/<task-name>/`.
- In the cluster, that path is available under `/run/determined/workdir/UNSAFE_SSD4/...`.
- Prefer `rsync` into a dedicated target directory. Use `--delete` only when the destination is clearly task-specific and safe to overwrite.
- Exclude `.git/`, caches, and irrelevant experiment outputs unless the task explicitly needs them.

### 4. Build or edit the Determined YAML

- Add a bind mount for `/UNSAFE_SSD4/` to `/run/determined/workdir/UNSAFE_SSD4/`.
- In `entrypoint`, `cd` into the synced project directory under `/run/determined/workdir/UNSAFE_SSD4/...` before launching the task.
- For CPU-only jobs, prefer `resources.slots_per_trial: 0`.
- For GPU jobs, set `slots_per_trial`, `resource_pool`, image, and environment variables explicitly.
- For long runs, sweeps, ablations, and batch jobs, prefer one YAML per task or per sweep shard so runs can queue independently.
- Reuse existing examples from `/home/lzzhao/hmnd_ws/whole_body_tracking/determined_batch_submit/cfg/` whenever possible instead of inventing a YAML from scratch.

### 5. Check pools and submit

- Check pools before choosing execution location.
- Prefer commands of the form:
  - `determined-batch list-pools --available-only --min-free-slots <n> --secrets-file ~/.config/determined_batch/.secrets.env`
  - `determined-batch submit --config /path/to/task.yaml --secrets-file ~/.config/determined_batch/.secrets.env`
- For interactive shell debugging, use the same secrets file and pool checks before starting the shell session.
- For CPU-only jobs, still inspect pool availability and confirm the selected pool accepts `slots_per_trial: 0`.
- Preserve the YAML path and synced code path so the run is reproducible.
- For interactive debugging, prefer a Determined shell workflow instead of forcing everything through experiment YAML.
- For queued experiment batches, preserve the submission command template so additional YAMLs can be submitted in parallel or sequence.

### 6. Interactive shell vs experiment

- Use Determined AI shell for interactive debugging, environment inspection, step-by-step repro, and short-run investigation that still benefits from cluster GPUs.
- If an interactive GPU shell cannot start because the cluster has no available GPU slots, switch immediately to local execution for that debugging session.
- Use Determined experiments for long training, evaluation, rendering, preprocessing, tuning, ablations, and any batchable workload.
- For batches, sweeps, or ablations, prefer submitting many experiments and letting the cluster queue them rather than serializing them locally.
- For long-running jobs, batch queues, tuning, and ablations, prefer asynchronous experiment submission over keeping a local terminal occupied.
- For multi-GPU work, do not use local fallback; submit to the cluster and wait for scheduling.

### 7. Local fallback

- Local fallback is the exception, not the default.
- Only use it when GPU is mandatory, the task is not multi-GPU, and the cluster lacks enough free GPU slots right now.
- Launch through `zsh`, then activate the local environment with `mamba activate cu129_py311_gcc11`.
- For long local jobs, redirect logs to a file and report the command, PID, log path, and stop method.

### 8. Report back

- State whether execution is on the cluster or local.
- For cluster runs, report:
  - sync directory
  - YAML path
  - mode: `shell` or `experiment`
  - resource pool
  - `slots_per_trial`
  - experiment ID or shell/session identifier
  - log or status command
- For local runs, report:
  - shell and environment
  - command
  - PID
  - log path
- If local fallback was used, explicitly state that the reason was insufficient free GPU capacity on the cluster.

## Quick Checklist

- Determine whether the task is truly heavy enough to warrant cluster usage.
- Prefer `determined_batch_submit` / `determined-batch`; install it first if missing.
- Prefer Determined for both GPU-heavy and CPU-heavy work.
- Prefer Determined shell for interactive debugging.
- Prefer Determined experiments for long-running and batched work.
- Sync code to `/UNSAFE_SSD4` before submission.
- Use `/run/determined/workdir/UNSAFE_SSD4` inside Determined containers.
- Try `slots_per_trial: 0` for CPU-only heavy jobs.
- Fall back locally only for non-multi-GPU jobs that require GPU when cluster GPU resources are unavailable.
- Keep multi-GPU jobs on the cluster even when that means waiting in queue.
- Return experiment IDs or PIDs instead of blocking on long runs unless the user explicitly requests monitoring.

## References

- Load `references/determined-batch-snippets.md` for reusable command templates, `rsync` snippets, and YAML fragments.
- Prefer `/home/lzzhao/hmnd_ws/whole_body_tracking/determined_batch_submit/README.md` for installation, CLI usage, and auth flow.
- Prefer `/home/lzzhao/hmnd_ws/whole_body_tracking/determined_batch_submit/cfg/rebuild_lafan1_rollouts_vae_batch_reboot_v2_mp8_96cpu.yaml` for `/UNSAFE_SSD4` mount patterns.
- Prefer `/home/lzzhao/hmnd_ws/whole_body_tracking/determined_batch_submit/cfg/train_meanflow_reboot_v2_hdf5_vds_1gpu.yaml` for single-GPU submission patterns.
