---
name: intensive-compute-runner
description: Plan, launch, inspect, and stop resource-intensive GPU or CPU work through this repository's Determined compute service. Use for heavy compute or managed cluster tasks; small local checks and hardware inspection alone are outside scope.
---

# Intensive Compute Runner

Use this repository's `ComputeService` as the control plane for heavy compute. The service owns planning, launch idempotency, task records, status, logs, and cancellation. Keep this skill in the repository; it is loaded by the dedicated worker rather than installed globally. A dedicated compute worker receives this repository copy of the skill with each consult request.

## Choose a mode

Let `kind: auto` select from intent when the request is clear:

- Use `command` for a one-off, non-interactive job expected to finish during an ordinary working session.
- Use `shell` for interactive debugging, environment inspection, and iterative work. A deployment may advertise an inactivity window such as about two hours; treat it as an advisory, configurable site policy rather than a Determined guarantee.
- Use `experiment` for overnight or durable work, and whenever experiment features such as search, trial tracking, or checkpoint lifecycle are actually needed. There is no rigid midnight cutoff.

Set `interactive` or `overnight` explicitly when intent would otherwise be ambiguous. Use the minimum suitable `slots`; heavy CPU work can use zero GPU slots only if the service and target pool support it.

## Prepare durable inputs

Put code, configs, datasets, packages, outputs, checkpoints, and other artifacts on storage covered by the compute profile's `mounts`. Use the mapped container path for `workdir` and `output_dir`. Never send source through an experiment `modelDefinition`, project archive, or other upload field.

For durable jobs, run a stable code revision from its own shared-storage directory and record `code_revision`. A mutable shared workspace is suitable for shell debugging, but it is a poor provenance boundary for an unattended run.

If files must be copied into shared storage, read [references/compute-workflow.md](references/compute-workflow.md). Preserve its secret exclusions and safe sync rules.

## Plan, then execute

1. Call `compute_plan` with the proposed request. Inspect the resolved kind, rendered config, mapped paths, revision, and advisories.
2. Resolve unsafe or ambiguous plan output before launch. Never include credentials in requests, configs, logs, or reports.
3. Call `compute_launch` with a stable `request_id`. Keep the returned local `task_id`; it is distinct from the remote Determined ID.
4. Use `compute_status`, `compute_logs`, and `compute_list_tasks` for observation. Use `compute_cancel` only for the intended task.

If launch outcome is unknown after a timeout or connection loss, do not submit again blindly. Use `compute_reconcile` only with a verified remote ID for the known task; the service checks its submission marker before binding. If authentication fails, stop and report the configuration problem; do not fall back to local execution.

`compute_consult` may ask the dedicated `gpt-5.6-sol` worker for a read-only plan or diagnosis. Its workflow state persists, but it cannot launch or cancel work; deterministic service tools perform mutations.

## Report

Return the selected mode, local task ID, remote ID when known, state, pool, slot count, mapped work/output paths, code revision, and the next status/log/cancel action. Omit secrets and secret-file contents.

Read [references/compute-workflow.md](references/compute-workflow.md) for request fields, storage preparation, failure handling, and deployment-specific shell policy.
