# determined-batch

Lightweight utilities for submitting many Determined AI experiments without
carrying project-specific baggage. The installable package lives under
`src/determined_batch`; examples and upstream references stay outside the
package so end users only install the reusable code.

## Features
- Minimal `DeterminedAPIClient` wrapper around the REST API (submit, list, delete).
- Higher-level services for experiments and resource pools.
- Batch submission helpers (`submit_directory`) plus an `argparse` CLI
  (`determined-batch`) for quick use from the shell.
- Clean separation of examples/templates from installable code.

## Install
```bash
git clone https://github.com/LingzheZhao/determined_batch_submit
cd determined_batch_submit
python -m pip install -e .
```
Requires Python 3.8+ and the `requests`/`pyyaml` dependencies declared in
`pyproject.toml`.

## Quick start (toy example)
This launches a lightweight matrix-multiply loop so you can validate connectivity
and then stop the run when you are done.

1) Configure paths and sync the example to the shared home:
```bash
export TOY_USERNAME=peter
export LOCAL_PROJECT_ROOT=/path/to/determined_batch_submit
export REMOTE_PROJECT_ROOT=/workspace/${TOY_USERNAME}/determined_batch_submit
export MOUNTED_REMOTE_PROJECT_ROOT=/run/user/1000/gvfs/sftp:host=login/workspace/${TOY_USERNAME}/determined_batch_submit

# Update cfg/examples/toy_matrix_loop_6000ada.yaml to match your username/project root.
rsync -a --delete "${LOCAL_PROJECT_ROOT}/scripts/examples/toy_example/" \
  "${MOUNTED_REMOTE_PROJECT_ROOT}/scripts/examples/toy_example/"
```

2) Submit the experiment (uses the 6000 Ada pool config):
```bash
export DETERMINED_BATCH_SECRETS=/path/to/.secrets.env

export DET_MASTER=http://10.0.1.66:8080

determined-batch submit \
  --config cfg/examples/toy_matrix_loop_6000ada.yaml
```

3) Kill the experiment when you are done:
```bash
determined-batch kill <id>
```

## Authentication and master
Set the Determined master address via `DET_MASTER` (e.g. `http://det.example:8080`).
Provide an API token with `DET_API_TOKEN` or pass `--api-token` on the CLI.
If you prefer username/password login, place `DET_USERNAME` and `DET_PASSWORD`
in a secrets file (default: `.determined_batch.env` in the working directory)
and point to it with `--secrets-file` or `DETERMINED_BATCH_SECRETS`.

## CLI usage
```bash
# Submit a single config
DETERMINED_BATCH_SECRETS=~/.det-secrets \
DET_MASTER=http://det.example:8080 \
determined-batch submit --config cfg/examples/basic_experiment.yaml \
  --project-root /path/to/your/code

# Submit every YAML in a directory (optionally in parallel)
determined-batch submit-dir --config-dir cfg/examples --parallel 4 --delay 0.5

# List resource pools and their free slots
determined-batch list-pools --available-only --min-free-slots 1

# List experiments
determined-batch experiments --state COMPLETED --limit 20
```

## Library usage
```python
from pathlib import Path
from determined_batch.submission import submit_directory

results = submit_directory(Path("cfg/examples"), project_root=Path("/path/to/code"))
for result in results:
    print(result)
```

## Project layout
- `src/determined_batch`: installable package (API client, services, CLI, submission helpers).
- `cfg/examples`: starter Determined configs; copy and customise for your workloads.
- `scripts/examples`: small, non-packaged helper scripts.
- `upstream`: Determined upstream sources as references.
