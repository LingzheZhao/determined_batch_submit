"""Example: submit a caller-selected directory of shared-storage experiment YAMLs.

Pass the directory on the command line. Export
``DET_MASTER`` and ``DET_API_TOKEN`` (or provide a secrets file) before running.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from determined_batch.submission import submit_directory

def main() -> int:
    parser = argparse.ArgumentParser(description="Submit prepared shared-storage experiment configs")
    parser.add_argument("config_dir", help="Directory containing prepared experiment YAML configs")
    args = parser.parse_args()

    results = submit_directory(Path(args.config_dir))
    if not results:
        parser.error("the selected directory contains no experiment YAML configs")
    for result in results:
        status = "OK" if result.get("success") == "True" else "FAIL"
        print(f"[{status}] {result['config']}" + (f" -> {result['experiment_id']}" if result.get("experiment_id") else ""))
        if result.get("error"):
            print(f"    {result['error']}")
    return 0 if all(result.get("success") == "True" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
