# Compute consultation workflows

The service can run a dedicated Codex consultation before a caller chooses an
explicit compute action. The consultation reads this repository and
`skills/intensive-compute-runner/SKILL.md`, then returns a diagnosis or plan. It
does not submit, launch, cancel, or edit anything. Those state changes remain in
the deterministic MCP tools.

## Lifecycle

`WorkflowManager.submit()` stores the request in SQLite and starts a detached
worker process. It returns a workflow ID without waiting for the model. Because
the worker is an independent process and session, an MCP client disconnect does
not cancel it. Call `WorkflowManager.status()` (or the MCP workflow-status tool)
to poll for its persisted result.

Statuses are `queued`, `running`, `succeeded`, `failed`, and `timed_out`. The
status response includes bounded lifecycle logs, a result on success, or a
short sanitized error on failure. Questions, curated context, and results are
stored in the workflow database, so that database must be protected like other
service state.

Status also reports `stale` and `recoverable`. A queued request becomes
recoverable when its dispatch timestamp is stale; this covers a service crash
between the database commit and worker spawn. Repeating the same idempotent
submission after that interval dispatches a replacement worker, or an operator
can run the worker command below. The transactional claim permits only one of
those workers to start Codex. A stale running record is recoverable only after
both recorded processes are no longer alive. There is no automatic retry loop.

The Python interface is:

```python
from determined_compute.agent_worker import WorkflowManager

manager = WorkflowManager(
    db_path="/var/lib/determined-compute/agent-workflows.sqlite3",
    repo_root="/path/to/determined-compute-service",
    codex_bin="codex",
    model="gpt-5.6-sol",
    timeout_seconds=900,
)

accepted = manager.submit(
    question="Why is experiment 123 making no progress?",
    owner="session-group",
    request_id="client-generated-idempotency-key",
    context={"experiment_id": 123, "recent_state": "QUEUED"},
)
current = manager.status(accepted["workflow_id"], owner="session-group")
```

The service must derive `owner` from its configured caller
namespace. It must not accept an arbitrary owner value from MCP tool arguments.
The pair `(owner, request_id)` is an idempotency key. Repeating the same payload
returns the existing workflow; reusing it for different content is an error.
Looking up a workflow with another owner behaves like a missing workflow.

Context must be a small JSON object curated by the caller. Fields whose names
look like passwords, tokens, API keys, cookies, private keys, authorization, or
credentials are rejected recursively. Do not place secrets in the question or
in otherwise innocently named fields. Inputs and final results have fixed size
limits.

## Worker command and isolation

For normal service use, `submit()` starts the worker automatically. A queued or
interrupted record can also be processed by a supervisor with:

```bash
python -m determined_compute.agent_worker worker \
  --db /var/lib/determined-compute/agent-workflows.sqlite3 \
  --repo-root /path/to/determined-compute-service \
  --workflow-id <workflow-id> \
  --codex-bin codex \
  --model gpt-5.6-sol \
  --timeout-seconds 900 \
  --stale-after-seconds 120
```

The worker invokes Codex with an argument array and prompt on standard input;
no shell evaluates caller text. Its invocation uses `--ignore-user-config`,
`--ignore-rules`, `--ephemeral`, `--sandbox read-only`, JSON event mode, and an
explicit `mcp_servers={}` override. The repository is the Codex working
directory. The process environment is rebuilt from a small allowlist, retaining
the normal `CODEX_HOME` authentication location while omitting Determined,
Grafana, and other service credentials. This also prevents the consultation
from recursively calling the service's MCP server.

The fixed default model is `gpt-5.6-sol`. A deployment may supply a different
model explicitly for testing or controlled migration. Before deployment, check
the installed CLI contract with `codex exec --help`; tests use a fake executable
and never make a paid model call.

Each run has a wall-clock timeout. On timeout, the worker terminates the whole
Codex process group and persists a `timed_out` state. Internal database
heartbeats record worker-process liveness; they are unrelated to Determined
shell keepalive or experiment health. Heartbeats and process IDs guard stale
recovery. A second worker will not execute a fresh run or a stale run whose
recorded worker/agent process is still alive. When both processes are gone, an
explicit worker invocation may reclaim the workflow and writes that recovery to
the persisted log before retrying.

## Operational limits

SQLite provides durable state and transactional claiming on one host; it is not
a distributed job queue. Detached workers need access to the same local database
and repository. Use a process supervisor to revisit queued records after a host
restart. PID liveness is deliberately conservative: PID reuse can delay recovery
until an operator inspects the record, while a hard crash can cause a read-only
consultation to run again after the stale interval. The visible reclaim log is
the audit signal for that retry.

Codex output is advisory and untrusted. Review it before passing any proposed
operation to launch or cancel tools. The read-only sandbox and empty MCP config
limit model-side mutation, but host filesystem permissions and protection of the
workflow database remain deployment responsibilities.
