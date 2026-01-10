"""Helpers for submitting experiments in bulk."""

from __future__ import annotations

import concurrent.futures
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
import time

import yaml

from determined_batch.core.api_client import DeterminedAPIClient


SubmissionResult = Tuple[bool, Optional[str], Optional[str]]


def _ensure_project(client: DeterminedAPIClient, config_path: Path) -> None:
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        workspace = config.get("workspace", "default")
        project = config.get("project")
        if project:
            client.ensure_project_exists(workspace_name=str(workspace), project_name=str(project))
    except Exception:
        # Best-effort; failures will surface during submission
        return


def submit_experiment(
    config_path: Path,
    project_root: Optional[Path] = None,
    client: Optional[DeterminedAPIClient] = None,
    activate: bool = True,
    validate_only: bool = False,
    ensure_project: bool = True,
) -> SubmissionResult:
    """Submit a single Determined experiment.

    Returns (success, experiment_id, error_message).
    """
    api_client = client or DeterminedAPIClient()
    config_path = Path(config_path)

    if not config_path.exists():
        return False, None, f"Config file not found: {config_path}"

    if ensure_project:
        _ensure_project(api_client, config_path)

    try:
        response = api_client.create_experiment(
            config_path=config_path,
            project_root=project_root,
            activate=activate,
            validate_only=validate_only,
        )
        experiment = response.get("experiment", {}) if isinstance(response, dict) else {}
        exp_id = experiment.get("id")
        return True, str(exp_id) if exp_id is not None else None, None
    except Exception as exc:
        return False, None, str(exc)


def submit_directory(
    config_dir: Path,
    project_root: Optional[Path] = None,
    client: Optional[DeterminedAPIClient] = None,
    parallel: int = 1,
    delay: float = 0.0,
    activate: bool = True,
    validate_only: bool = False,
    ensure_project: bool = True,
    dry_run: bool = False,
) -> List[Dict[str, Optional[str]]]:
    """Submit every ``*.yaml``/``*.yml`` file in a directory.

    Returns a list of dictionaries with keys ``config``, ``success``,
    ``experiment_id``, and ``error``.
    """
    config_dir = Path(config_dir)
    api_client = client or DeterminedAPIClient()
    files = sorted(list(config_dir.glob("*.yaml")) + list(config_dir.glob("*.yml")))
    results: List[Dict[str, Optional[str]]] = []

    if dry_run:
        for cfg in files:
            results.append({"config": cfg.name, "success": "dry-run", "experiment_id": None, "error": None})
        return results

    if parallel <= 1:
        for cfg in files:
            success, exp_id, error = submit_experiment(
                cfg,
                project_root=project_root,
                client=api_client,
                activate=activate,
                validate_only=validate_only,
                ensure_project=ensure_project,
            )
            results.append({"config": cfg.name, "success": str(success), "experiment_id": exp_id, "error": error})
            if delay:
                time.sleep(delay)
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as executor:
        future_map = {
            executor.submit(
                submit_experiment,
                cfg,
                project_root,
                api_client,
                activate,
                validate_only,
                ensure_project,
            ): cfg
            for cfg in files
        }
        for future, cfg in future_map.items():
            success, exp_id, error = future.result()
            results.append({"config": cfg.name, "success": str(success), "experiment_id": exp_id, "error": error})
            if delay:
                time.sleep(delay)

    return results


__all__ = ["submit_experiment", "submit_directory", "SubmissionResult"]
