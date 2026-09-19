# Compute workflow reference

Load this reference when preparing a service request, copying a workspace to shared storage, or diagnosing an uncertain launch.

## Request fields

`ComputeService.plan` and `ComputeService.launch` accept a request mapping with these fields:

| Field | Meaning |
| --- | --- |
| `kind` | `auto`, `command`, `shell`, or `experiment` |
| `interactive` | Selects `shell` when `kind` is `auto` |
| `overnight` | Selects `experiment` when `kind` is `auto` |
| `command` | Command string or argument list |
| `workdir` | Absolute container path covered by a configured mount |
| `output_dir` | Absolute container path covered by a configured mount |
| `slots` | Requested slot count |
| `pool`, `image` | Optional overrides of profile defaults |
| `code_revision` | Stable revision or content identifier for reproducibility |
| `experiment_config` | Experiment-only configuration; selects `experiment` in auto mode |

Auto mode otherwise resolves to `command`. Call `plan` before `launch`; planning is read-only.

## Shared storage

Every runtime dependency and output must be reachable through a profile mapping:

```yaml
mounts:
  - host_path: /workspace/<user>
    container_path: /run/determined/workdir/home
```

Configured shared roots may include `/SSD`, `/SSD_home`, `/SSD_datasets`, `/SSD3`, `/SSD3_home`, `/SSD3_datasets`, and `/UNSAFE_SSD4`. Cluster host paths need not be mounted on the MCP client machine.

Translate paths by replacing the matching host prefix with its container prefix. Do not assume that old image names, pool names, master addresses, or site paths are current; read them from the deployment profile or the user.

For an unattended or durable run, copy or check out the exact revision into a revision-specific directory such as `/workspace/<user>/compute/runs/<project>/<revision>/repo`. Record the revision in the request. Reserve a mutable directory such as `/workspace/<user>/compute/debug/<project>` for interactive shells.

When direct sync is needed, use an explicit destination and avoid `--delete`:

```bash
rsync -a \
  --exclude '.git/' \
  --exclude '.env' \
  --exclude '.env.*' \
  --exclude '.determined_compute.env' \
  --exclude '.secrets*' \
  --exclude '.ssh/' \
  --exclude '.netrc' \
  --exclude '.npmrc' \
  --exclude '.pypirc' \
  --exclude '*.pem' \
  --exclude '*.key' \
  --exclude '*credentials*' \
  --exclude '*token*' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  <source>/ <shared-task-directory>/repo/
```

Review project-specific secret filenames before copying. Never use an experiment `modelDefinition`, project archive, or upload option; the Determined payload should contain mapped paths only.

## Failure handling

`request_id` makes a known launch retry idempotent. It does not justify resubmitting after an unknown outcome. If submission times out, retain the local record and call `compute_reconcile` only with a verified remote ID. The service will require the remote submission marker to match. If there is no trustworthy link, report the task as uncertain and require investigation before another launch.

Authentication failure is a configuration failure. Do not run the workload locally as a fallback and do not expose credentials while diagnosing it.

Logs and reports may contain commands, paths, IDs, states, and sanitized errors. They must not include tokens, passwords, authorization headers, environment-file contents, or copied secret values.

## Shell lifetime

A deployment may reclaim inactive shells through an external scheduler or watchdog. Read the configured `shell_inactivity_seconds` as an advisory and consult the cluster policy for the actual inactivity definition and enforcement. Save work on mapped shared storage so it survives shell termination.
