---
name: intensive-compute-runner
description: Use when a task is GPU-intensive, CPU-intensive, long-running, memory-heavy, or would monopolize local resources, including training, evaluation, rendering, simulation, large preprocessing, or batch inference.
---

# Intensive Compute Runner

## Overview

- Default to Determined AI cluster execution for heavy GPU or CPU work.
- Prefer the configured Determined submission helper and its `determined-batch` CLI; if it is missing, install or activate the project-approved helper before inventing ad hoc submission flows.
- Treat CPU-only heavy jobs as cluster candidates too; prefer `slots_per_trial: 0` before using the local machine.
- Use Determined AI shell for interactive debugging and short iterative investigation on cluster GPUs.
- Use Determined AI experiments for long-running jobs, batch sweeps, tuning, ablations, rendering, and queued submissions.
- Multi-GPU work must run on the Determined cluster; queue and wait if necessary instead of moving it local.
- Sync runnable code and configs into cluster-visible shared storage before submission so containers can read the same files from the configured container mount path.
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

- Prefer the project-approved `determined-batch` CLI or equivalent Determined submission wrapper.
- If it is not installed, install it first:
  - obtain the configured submission helper repository or package
  - enter that helper's project directory
  - `python -m pip install -e .`
- Use the configured secrets file for authentication via `--secrets-file` or `DETERMINED_BATCH_SECRETS`.
- If `DET_MASTER` is not set, follow the project-local Determined helper documentation and export it before submitting.
- Never print secret values; reference only variable names, paths, and command forms.
- Treat this skill as the default Codex policy for heavy compute work: future tasks should prefer this path unless the user explicitly asks for a different execution location.

### 3. Sync code into shared storage

- Sync the runnable code, configs, helper scripts, and lightweight assets into a dedicated task directory under the configured shared storage root.
- In the cluster, that path must be available under the corresponding configured container mount root.
- Prefer `rsync` into a dedicated target directory. Use `--delete` only when the destination is clearly task-specific and safe to overwrite.
- Do not assume all shared storage has the same ownership behavior. Inspect or infer the target mount's permissions before adding ownership flags.
- For shared stores that reject owner/group/permission preservation, such as known `nobody:nogroup` mounts, add `--no-owner --no-group --no-perms --omit-dir-times`; do not apply these flags blindly to normal user-owned storage.
- Exclude `.git/`, caches, and irrelevant experiment outputs unless the task explicitly needs them.

### 4. Build or edit the Determined YAML

- Add a bind mount from the configured shared storage root to the configured container mount root.
- In `entrypoint`, `cd` into the synced project directory under the configured container mount root before launching the task.
- For CPU-only jobs, prefer `resources.slots_per_trial: 0`.
- For GPU jobs, set `slots_per_trial`, `resource_pool`, image, and environment variables explicitly.
- For experiments, explicitly override `checkpoint_storage` to a known shared filesystem path for the target cluster/pool. Do not rely on the Determined default checkpoint storage unless it has already been verified for that workload.
- For long runs, sweeps, ablations, and batch jobs, prefer one YAML per task or per sweep shard so runs can queue independently.
- Reuse project-local templates or checked-in examples whenever possible instead of inventing a YAML from scratch. Do not cite machine-specific absolute template paths in the skill or final report.

### 5. Check pools and submit

- Check pools before choosing execution location.
- Prefer commands of the form:
  - `determined-batch list-pools --available-only --min-free-slots <n> --secrets-file <secrets-file>`
  - `determined-batch submit --config <task.yaml> --secrets-file <secrets-file>`
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
- Launch through the user's preferred shell, then activate the project-approved local environment.
- For long local jobs, redirect logs to a file and report the command, PID, log path, and stop method.

### 8. Report back

- State whether execution is on the cluster or local.
- For cluster experiment runs, report:
  - sync directory
  - YAML path
  - mode: `experiment`
  - resource pool
  - `slots_per_trial`
  - experiment ID
  - `checkpoint_storage` override
  - log or status command
- For cluster shell runs, report:
  - shell config path
  - mode: `shell`
  - resource pool
  - slots requested
  - shell ID/session identifier
  - reconnect command or helper command
  - expected lifetime and kill command, if known
- For local runs, report:
  - shell and environment
  - command
  - PID
  - log path
- If local fallback was used, explicitly state that the reason was insufficient free GPU capacity on the cluster.

## Quick Checklist

- Determine whether the task is truly heavy enough to warrant cluster usage.
- Prefer the configured Determined submission helper / `determined-batch`; install or activate it first if missing.
- Prefer Determined for both GPU-heavy and CPU-heavy work.
- Prefer Determined shell for interactive debugging.
- Prefer Determined experiments for long-running and batched work.
- Sync code to the configured shared storage root before submission.
- Use the configured container mount root inside Determined containers.
- Use ownership-preserving `rsync` only when the target storage supports it; add `--no-owner --no-group --no-perms --omit-dir-times` only for mounts that need it.
- Override experiment `checkpoint_storage` explicitly.
- Try `slots_per_trial: 0` for CPU-only heavy jobs.
- Fall back locally only for non-multi-GPU jobs that require GPU when cluster GPU resources are unavailable.
- Keep multi-GPU jobs on the cluster even when that means waiting in queue.
- Return experiment IDs, shell IDs, or PIDs instead of blocking on long runs unless the user explicitly requests monitoring.

## References

- Load `references/determined-batch-snippets.md` for reusable command templates, `rsync` snippets, and YAML fragments.
- Prefer project-local Determined helper documentation for installation, CLI usage, authentication, mount patterns, and single-GPU submission patterns.
