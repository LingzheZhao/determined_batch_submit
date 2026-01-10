"""Example: submit every YAML config in ``cfg/examples``.

Adjust ``CONFIG_DIR`` or pass a path on the command line. Export
``DET_MASTER`` and ``DET_API_TOKEN`` (or provide a secrets file) before running.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from determined_batch.submission import submit_directory

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "cfg" / "examples"


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit example configs")
    parser.add_argument("config_dir", nargs="?", default=DEFAULT_CONFIG_DIR, help="Directory containing YAML configs")
    args = parser.parse_args()

    results = submit_directory(Path(args.config_dir))
    for result in results:
        status = "OK" if result.get("success") == "True" else "FAIL"
        print(f"[{status}] {result['config']}" + (f" -> {result['experiment_id']}" if result.get("experiment_id") else ""))
        if result.get("error"):
            print(f"    {result['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
