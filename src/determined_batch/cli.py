"""Command line interface for the Determined batch utilities."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional

from determined_batch.core.api_client import DeterminedAPIClient
from determined_batch.services.experiment_service import ExperimentService
from determined_batch.services.resource_pool_service import ResourcePoolService
from determined_batch.submission import submit_directory, submit_experiment


def _build_client(args: argparse.Namespace) -> DeterminedAPIClient:
    return DeterminedAPIClient(
        api_url=args.api_url,
        api_token=args.api_token,
        secrets_path=Path(args.secrets_file) if args.secrets_file else None,
        verify_ssl=args.verify_ssl,
    )


def _add_common_client_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-url", dest="api_url", help="Determined master URL (defaults to DET_MASTER env)")
    parser.add_argument("--api-token", dest="api_token", help="API token (overrides DET_API_TOKEN)")
    parser.add_argument("--secrets-file", dest="secrets_file", help="Path to a KEY=VALUE secrets file")
    parser.add_argument(
        "--verify-ssl",
        dest="verify_ssl",
        action="store_true",
        help="Verify SSL certificates when talking to the master",
    )
    parser.add_argument(
        "--no-verify-ssl",
        dest="verify_ssl",
        action="store_false",
        help="Disable SSL verification (default)",
    )
    parser.set_defaults(verify_ssl=None)


def _cmd_submit(args: argparse.Namespace) -> int:
    client = _build_client(args)
    success, exp_id, error = submit_experiment(
        config_path=Path(args.config),
        project_root=Path(args.project_root) if args.project_root else None,
        client=client,
        activate=not args.dont_activate,
        validate_only=args.validate_only,
        ensure_project=not args.skip_project_check,
    )
    if success:
        msg = f"Submitted {args.config}"
        if exp_id:
            msg += f" (experiment id: {exp_id})"
        print(msg)
        return 0
    print(f"Failed to submit {args.config}: {error}")
    return 1


def _cmd_submit_dir(args: argparse.Namespace) -> int:
    client = _build_client(args)
    results = submit_directory(
        config_dir=Path(args.config_dir),
        project_root=Path(args.project_root) if args.project_root else None,
        client=client,
        parallel=args.parallel,
        delay=args.delay,
        activate=not args.dont_activate,
        validate_only=args.validate_only,
        ensure_project=not args.skip_project_check,
        dry_run=args.dry_run,
    )
    successes = [r for r in results if r.get("success") == "True" or r.get("success") == "dry-run"]
    failures = [r for r in results if r not in successes]
    for result in results:
        status = "OK" if result in successes else "FAIL"
        print(f"[{status}] {result['config']}" + (f" -> {result['experiment_id']}" if result.get("experiment_id") else ""))
        if result.get("error"):
            print(f"    {result['error']}")
    print(f"\nSubmitted {len(results)} configs: {len(successes)} succeeded, {len(failures)} failed")
    return 0 if not failures else 1


def _cmd_list_pools(args: argparse.Namespace) -> int:
    client = _build_client(args)
    service = ResourcePoolService(client)
    pools = service.get_available_pools(
        min_free_slots=args.min_free_slots,
        prefer_pools=args.prefer or None,
        avoid_pools=set(args.avoid or []),
        max_pools=None,
    ) if args.available_only else service.get_all_pools(force_refresh=True)

    if not pools:
        print("No resource pools found")
        return 1

    for pool in pools:
        utilization = pool.get_utilization_rate() * 100
        print(
            f"{pool.name:30} free:{pool.available_slots:3d} total:{pool.total_slots:3d} "
            f"util:{utilization:5.1f}%"
        )
    return 0


def _cmd_experiments(args: argparse.Namespace) -> int:
    client = _build_client(args)
    service = ExperimentService(client)
    experiments = service.get_experiments(state_names=args.state, limit=args.limit, offset=args.offset)
    if not experiments:
        print("No experiments found")
        return 0
    for exp in experiments:
        desc = exp.description or ""
        print(f"{exp.id:6d} {exp.state.value:15} {exp.owner:15} {exp.name}")
        if args.show_description and desc:
            print(f"    {desc}")
    return 0


def _cmd_kill(args: argparse.Namespace) -> int:
    client = _build_client(args)
    service = ExperimentService(client)
    if service.kill_experiment(args.experiment_id):
        print(f"Killed experiment {args.experiment_id}")
        return 0
    print(f"Failed to kill experiment {args.experiment_id}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Batch tools for Determined experiments")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # submit
    submit_parser = subparsers.add_parser("submit", help="Submit a single config file")
    submit_parser.add_argument("--config", required=True, help="Path to a YAML config")
    submit_parser.add_argument("--project-root", help="Directory to package as modelDefinition")
    submit_parser.add_argument("--validate-only", action="store_true", help="Validate config without launching")
    submit_parser.add_argument("--dont-activate", action="store_true", help="Create but do not activate the experiment")
    submit_parser.add_argument("--skip-project-check", action="store_true", help="Skip ensuring the workspace/project exists")
    _add_common_client_args(submit_parser)
    submit_parser.set_defaults(func=_cmd_submit)

    # submit-dir
    submit_dir_parser = subparsers.add_parser("submit-dir", help="Submit every YAML file in a directory")
    submit_dir_parser.add_argument("--config-dir", required=True, help="Directory containing YAML configs")
    submit_dir_parser.add_argument("--project-root", help="Directory to package as modelDefinition")
    submit_dir_parser.add_argument("--parallel", type=int, default=1, help="Number of submissions in parallel")
    submit_dir_parser.add_argument("--delay", type=float, default=0.0, help="Delay between submissions (seconds)")
    submit_dir_parser.add_argument("--validate-only", action="store_true", help="Validate configs without launching")
    submit_dir_parser.add_argument("--dont-activate", action="store_true", help="Create but do not activate the experiments")
    submit_dir_parser.add_argument("--skip-project-check", action="store_true", help="Skip ensuring the workspace/project exists")
    submit_dir_parser.add_argument("--dry-run", action="store_true", help="List configs without submitting")
    _add_common_client_args(submit_dir_parser)
    submit_dir_parser.set_defaults(func=_cmd_submit_dir)

    # list-pools
    pools_parser = subparsers.add_parser("list-pools", help="List resource pools")
    pools_parser.add_argument("--min-free-slots", type=int, default=0, help="Only show pools with at least this many free slots")
    pools_parser.add_argument("--prefer", nargs="*", help="Optional ordered list of preferred pools")
    pools_parser.add_argument("--avoid", nargs="*", help="Pools to exclude from the output")
    pools_parser.add_argument("--available-only", action="store_true", help="Only show pools meeting availability criteria")
    _add_common_client_args(pools_parser)
    pools_parser.set_defaults(func=_cmd_list_pools)

    # experiments
    exp_parser = subparsers.add_parser("experiments", help="List experiments")
    exp_parser.add_argument("--state", action="append", help="Filter by state name (e.g. COMPLETED). Can be repeated")
    exp_parser.add_argument("--limit", type=int, default=100, help="Number of experiments to fetch")
    exp_parser.add_argument("--offset", type=int, default=0, help="Pagination offset")
    exp_parser.add_argument("--show-description", action="store_true", help="Print experiment descriptions")
    _add_common_client_args(exp_parser)
    exp_parser.set_defaults(func=_cmd_experiments)

    # kill
    kill_parser = subparsers.add_parser("kill", help="Force-kill a running experiment")
    kill_parser.add_argument("experiment_id", type=int, help="Experiment ID to kill")
    _add_common_client_args(kill_parser)
    kill_parser.set_defaults(func=_cmd_kill)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
