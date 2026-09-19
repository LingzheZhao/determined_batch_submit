"""Helpers for submitting experiments in bulk without uploading local code."""

from __future__ import annotations

import concurrent.futures
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from determined_batch.core.api_client import (
    DeterminedAPIClient,
    SubmissionUncertainError,
)

SubmissionResult = Tuple[bool, Optional[str], Optional[str]]


def _load_config(config_path: Path) -> Dict[str, object]:
    parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"Config must contain a YAML mapping: {config_path}")
    return parsed


def _check_project(client: DeterminedAPIClient, config: Dict[str, object]) -> Optional[str]:
    """Verify a requested project without ever creating one."""
    project = config.get("project")
    if not project:
        return None
    workspace = str(config.get("workspace", "default"))
    if client.get_project(workspace, str(project)) is None:
        return f"Project does not exist: {workspace}/{project}"
    return None


def submit_experiment(
    config_path: Path,
    project_root: Optional[Path] = None,
    client: Optional[DeterminedAPIClient] = None,
    activate: bool = True,
    validate_only: bool = False,
    ensure_project: bool = True,
) -> SubmissionResult:
    """Submit one experiment, returning ``(success, id, error)``.

    ``SubmissionUncertainError`` deliberately propagates so callers can avoid
    blindly resubmitting a request that the master may already have accepted.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        return False, None, f"Config file not found: {config_path}"
    if project_root is not None:
        return False, None, "project_root uploads are not supported; config must reference remote code"
    try:
        config = _load_config(config_path)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        return False, None, str(exc)

    api_client = client or DeterminedAPIClient()
    try:
        # validateOnly is itself the read-only server validation.  Project
        # lookup and especially creation are unrelated and must not occur.
        if ensure_project and not validate_only:
            project_error = _check_project(api_client, config)
            if project_error:
                return False, None, project_error
        response = api_client.create_experiment(
            config_path=config_path,
            project_root=None,
            activate=activate,
            validate_only=validate_only,
        )
    except SubmissionUncertainError:
        raise
    except Exception as exc:
        return False, None, str(exc)

    if validate_only:
        return True, None, None
    experiment = response.get("experiment") if isinstance(response, dict) else None
    if not isinstance(experiment, dict) or experiment.get("id") is None:
        raise SubmissionUncertainError(
            "Experiment submission response had no experiment id", details=response
        )
    return True, str(experiment["id"]), None


def _result_for(
    config_path: Path,
    project_root: Optional[Path],
    client: DeterminedAPIClient,
    activate: bool,
    validate_only: bool,
    ensure_project: bool,
) -> Dict[str, Optional[str]]:
    try:
        success, experiment_id, error = submit_experiment(
            config_path,
            project_root=project_root,
            client=client,
            activate=activate,
            validate_only=validate_only,
            ensure_project=ensure_project,
        )
        outcome = "submitted" if success else "failed"
    except SubmissionUncertainError as exc:
        success, experiment_id, error, outcome = False, None, str(exc), "unknown"
    return {
        "config": config_path.name,
        "success": str(success),
        "experiment_id": experiment_id,
        "error": error,
        "outcome": outcome,
    }


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
    """Submit every YAML file in a directory; dry runs stay fully offline."""
    config_dir = Path(config_dir)
    files = sorted([*config_dir.glob("*.yaml"), *config_dir.glob("*.yml")])
    if dry_run:
        results: List[Dict[str, Optional[str]]] = []
        for config_path in files:
            try:
                _load_config(config_path)
            except (OSError, yaml.YAMLError, ValueError) as exc:
                results.append({
                    "config": config_path.name,
                    "success": "False",
                    "experiment_id": None,
                    "error": str(exc),
                    "outcome": "failed",
                })
            else:
                results.append({
                    "config": config_path.name,
                    "success": "dry-run",
                    "experiment_id": None,
                    "error": None,
                    "outcome": "dry-run",
                })
        return results

    if project_root is not None:
        return [{
            "config": path.name,
            "success": "False",
            "experiment_id": None,
            "error": "project_root uploads are not supported; config must reference remote code",
            "outcome": "failed",
        } for path in files]

    api_client = client or DeterminedAPIClient()
    args = (project_root, api_client, activate, validate_only, ensure_project)
    if parallel <= 1:
        results = []
        for config_path in files:
            results.append(_result_for(config_path, *args))
            if delay:
                time.sleep(delay)
        return results

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as executor:
        future_map = {
            executor.submit(_result_for, config_path, *args): config_path for config_path in files
        }
        for future in concurrent.futures.as_completed(future_map):
            results.append(future.result())
            if delay:
                time.sleep(delay)
    results.sort(key=lambda item: str(item["config"]))
    return results


__all__ = [
    "submit_experiment",
    "submit_directory",
    "SubmissionResult",
    "SubmissionUncertainError",
]
