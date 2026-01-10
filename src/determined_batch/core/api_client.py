"""Thin wrapper around the Determined REST API.

This client intentionally keeps dependencies light so it can run in automation
or CI environments. Authentication is resolved from, in order:

1. Explicit ``api_token`` passed to the constructor.
2. ``DET_API_TOKEN`` environment variable.
3. ``DET_USERNAME``/``DET_PASSWORD`` (either env vars or values loaded from
   ``.determined_batch.env``) via a login request.

The API host is taken from ``DET_MASTER``/``DET_MASTER_ADDR``/``DET_MASTER_HOST``
(env vars) or defaults to ``http://localhost:8080``.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests

from determined_batch.utils.secrets import load_secrets


def _normalize_api_url(api_url: Optional[str]) -> str:
    url = api_url or os.environ.get("DET_MASTER") or os.environ.get("DET_MASTER_ADDR") or os.environ.get("DET_MASTER_HOST")
    if url:
        if not url.startswith("http"):
            url = f"http://{url}"
    else:
        url = "http://localhost:8080"

    # Append default port if none provided
    if ":" not in url.split("//", 1)[-1]:
        url = f"{url}:8080"
    return url.rstrip("/")


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def _login_for_token(api_url: str, username: str, password: str, verify_ssl: bool) -> Optional[str]:
    login_endpoint = urljoin(api_url.rstrip("/") + "/", "api/v1/auth/login")
    payload = json.dumps({"username": username, "password": password})
    try:
        response = requests.post(
            login_endpoint,
            headers={"Content-Type": "application/json"},
            data=payload,
            timeout=15,
            verify=verify_ssl,
        )
        if response.status_code == 200:
            data = response.json()
            return data.get("token", "") or None
    except Exception:
        return None
    return None


class DeterminedAPIClient:
    """Simple REST client used by the higher-level services."""

    def __init__(
        self,
        api_url: Optional[str] = None,
        api_token: Optional[str] = None,
        secrets_path: Optional[Path] = None,
        verify_ssl: Optional[bool] = None,
    ) -> None:
        self.api_url = _normalize_api_url(api_url)
        self.verify_ssl = _bool_env("DET_VERIFY_SSL", default=False) if verify_ssl is None else verify_ssl
        self.api_token = self._resolve_token(api_token, secrets_path)
        self.headers: Dict[str, str] = {}
        if self.api_token:
            self.headers["Authorization"] = f"Bearer {self.api_token}"

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = urljoin(self.api_url.rstrip("/") + "/", endpoint)
        response = requests.get(url, headers=self.headers, params=params, timeout=30, verify=self.verify_ssl)
        response.raise_for_status()
        return response.json()

    def _post(self, endpoint: str, data: Optional[Dict[str, Any]] = None, files: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = urljoin(self.api_url.rstrip("/") + "/", endpoint)
        if files:
            response = requests.post(url, headers=self.headers, files=files, timeout=60, verify=self.verify_ssl)
        else:
            headers = {**self.headers, "Content-Type": "application/json"}
            response = requests.post(url, headers=headers, json=data, timeout=60, verify=self.verify_ssl)
        if response.status_code >= 400:
            try:
                error_data = response.json()
                message = error_data.get("message") or error_data.get("error") or response.text
                raise requests.exceptions.HTTPError(f"{response.status_code} {response.reason}: {message}", response=response)
            except (ValueError, requests.exceptions.JSONDecodeError):
                response.raise_for_status()
        return response.json()

    def _delete(self, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        url = urljoin(self.api_url.rstrip("/") + "/", endpoint)
        headers = {**self.headers}
        if data:
            headers["Content-Type"] = "application/json"
        response = requests.delete(url, headers=headers, json=data if data else None, timeout=60, verify=self.verify_ssl)
        if response.status_code >= 400:
            try:
                error_data = response.json()
                message = error_data.get("message") or error_data.get("error") or response.text
                raise requests.exceptions.HTTPError(f"{response.status_code} {response.reason}: {message}", response=response)
            except (ValueError, requests.exceptions.JSONDecodeError):
                response.raise_for_status()
        if response.text:
            try:
                return response.json()
            except (ValueError, requests.exceptions.JSONDecodeError):
                return None
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _resolve_token(self, api_token: Optional[str], secrets_path: Optional[Path]) -> Optional[str]:
        if api_token:
            return api_token
        token = os.environ.get("DET_API_TOKEN")
        if token:
            return token

        secrets = load_secrets(secrets_path)
        username = secrets.get("DET_USERNAME") or os.environ.get("DET_USERNAME")
        password = secrets.get("DET_PASSWORD") or os.environ.get("DET_PASSWORD")
        if username and password:
            return _login_for_token(self.api_url, username, password, verify_ssl=self.verify_ssl)
        return None

    def _package_model_definition(self, project_root: Path) -> List[Dict[str, str]]:
        """Package a project directory into the payload Determined expects."""
        exclude = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ipynb_checkpoints", "wandb"}
        model_files: List[Dict[str, str]] = []
        for file_path in project_root.rglob("*"):
            if not file_path.is_file():
                continue
            if any(part.startswith(".") for part in file_path.parts):
                continue
            if any(excluded in file_path.parts for excluded in exclude):
                continue
            try:
                content = file_path.read_bytes()
            except (OSError, PermissionError):
                continue
            rel_path = file_path.relative_to(project_root)
            model_files.append({
                "path": str(rel_path),
                "content": base64.b64encode(content).decode("utf-8"),
            })
        return model_files

    # ------------------------------------------------------------------
    # API surface
    # ------------------------------------------------------------------
    def get_experiments(
        self,
        limit: int = 100,
        offset: int = 0,
        states: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if states:
            params["states"] = states
        data = self._get("api/v1/experiments", params=params)
        return data.get("experiments", []) if isinstance(data, dict) else []

    def get_experiment(self, experiment_id: str) -> Dict[str, Any]:
        return self._get(f"api/v1/experiments/{experiment_id}")

    def get_trials(self, experiment_id: str) -> List[Dict[str, Any]]:
        data = self._get(f"api/v1/experiments/{experiment_id}/trials")
        return data.get("trials", []) if isinstance(data, dict) else []

    def get_trial_logs(self, trial_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        try:
            url = urljoin(self.api_url.rstrip("/") + "/", f"api/v1/trials/{trial_id}/logs")
            response = requests.get(url, headers=self.headers, params={"limit": limit}, timeout=30, verify=self.verify_ssl, stream=True)
            response.raise_for_status()
            logs: List[Dict[str, Any]] = []
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    log_data = json.loads(line)
                    if "result" in log_data:
                        logs.append(log_data["result"])
                    elif "error" in log_data:
                        break
                except json.JSONDecodeError:
                    continue
            return logs
        except Exception:
            return []

    def get_experiment_logs(self, experiment_id: str, tail: int = 100) -> Optional[str]:
        trials = self.get_trials(experiment_id)
        if not trials:
            return None
        trial_id = str(trials[0].get("id"))
        if not trial_id:
            return None
        entries = self.get_trial_logs(trial_id, limit=tail)
        messages: List[str] = []
        for entry in entries:
            if isinstance(entry, dict):
                if "message" in entry:
                    msg = entry["message"]
                    if isinstance(msg, str):
                        messages.append(msg)
                    elif isinstance(msg, dict) and "message" in msg:
                        messages.append(str(msg["message"]))
                elif "log" in entry:
                    messages.append(str(entry["log"]))
        return "\n".join(messages) if messages else None

    def get_resource_pools(self) -> List[Dict[str, Any]]:
        data = self._get("api/v1/resource-pools")
        return data.get("resourcePools", []) if isinstance(data, dict) else []

    def get_slots(self) -> List[Dict[str, Any]]:
        data = self._get("api/v1/agents")
        slots: List[Dict[str, Any]] = []
        if not isinstance(data, dict):
            return slots
        agents = data.get("agents", [])
        if not isinstance(agents, list):
            return slots
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            agent_slots = agent.get("slots", {})
            if isinstance(agent_slots, dict):
                slot_items = agent_slots.values()
            elif isinstance(agent_slots, list):
                slot_items = agent_slots
            else:
                continue
            resource_pools = agent.get("resourcePools")
            if isinstance(resource_pools, list):
                resource_pool = resource_pools[0] if resource_pools else None
            else:
                resource_pool = resource_pools
            for slot in slot_items:
                if not isinstance(slot, dict):
                    continue
                container = slot.get("container")
                container_id = None
                if isinstance(container, dict):
                    container_id = container.get("id")
                elif isinstance(container, str):
                    container_id = container
                slots.append(
                    {
                        "agent_id": agent.get("id"),
                        "agent_label": agent.get("label"),
                        "slot_id": slot.get("id"),
                        "device": slot.get("device"),
                        "enabled": slot.get("enabled", True),
                        "container_id": container_id,
                        "resource_pool": resource_pool,
                    }
                )
        return slots

    def get_workspace_id(self, workspace_name: str = "default") -> Optional[int]:
        try:
            data = self._get("api/v1/workspaces", params={"name": workspace_name})
            workspaces = data.get("workspaces", []) if isinstance(data, dict) else []
            if workspaces:
                return workspaces[0].get("id")
        except Exception:
            return None
        return None

    def get_project(self, workspace_name: str, project_name: str) -> Optional[Dict[str, Any]]:
        try:
            workspace_id = self.get_workspace_id(workspace_name)
            if not workspace_id:
                return None
            data = self._get(f"api/v1/workspaces/{workspace_id}/projects", params={"name": project_name})
            projects = data.get("projects", []) if isinstance(data, dict) else []
            if projects:
                return projects[0]
        except Exception:
            return None
        return None

    def create_project(self, workspace_name: str, project_name: str, description: Optional[str] = None) -> Optional[Dict[str, Any]]:
        workspace_id = self.get_workspace_id(workspace_name)
        if not workspace_id:
            return None
        payload: Dict[str, Any] = {"name": project_name}
        if description:
            payload["description"] = description
        data = self._post(f"api/v1/workspaces/{workspace_id}/projects", data=payload)
        return data.get("project") if isinstance(data, dict) else None

    def ensure_project_exists(self, workspace_name: str, project_name: str, description: Optional[str] = None) -> bool:
        if self.get_project(workspace_name, project_name):
            return True
        created = self.create_project(workspace_name, project_name, description)
        return created is not None

    def create_experiment(
        self,
        config_path: Path,
        project_root: Optional[Path] = None,
        model_definition: Optional[List[Dict[str, Any]]] = None,
        activate: bool = True,
        validate_only: bool = False,
    ) -> Dict[str, Any]:
        config_path = Path(config_path)
        config_content = config_path.read_text(encoding="utf-8")
        data: Dict[str, Any] = {
            "config": config_content,
            "activate": activate,
            "validateOnly": validate_only,
        }

        if model_definition is not None:
            data["modelDefinition"] = model_definition
        elif project_root:
            project_root = Path(project_root)
            if project_root.exists():
                packaged = self._package_model_definition(project_root)
                if packaged:
                    data["modelDefinition"] = packaged

        return self._post("api/v1/experiments", data=data)

    def delete_experiment(self, experiment_id: int) -> None:
        self._delete(f"api/v1/experiments/{experiment_id}")

    def kill_experiment(self, experiment_id: int) -> None:
        """Force-kill a running experiment."""
        self._post(f"api/v1/experiments/{experiment_id}/kill", data={})

    def cancel_experiment(self, experiment_id: int) -> None:
        """Gracefully cancel a running experiment."""
        self._post(f"api/v1/experiments/{experiment_id}/cancel", data={})

    def delete_experiments(self, experiment_ids: List[int], project_id: Optional[int] = None) -> Dict[str, Any]:
        if project_id:
            payload: Dict[str, Any] = {"experimentIds": experiment_ids}
            return self._delete(f"api/v1/projects/{project_id}/experiments/delete", data=payload) or {}
        results = []
        for exp_id in experiment_ids:
            try:
                self.delete_experiment(exp_id)
                results.append({"id": exp_id, "error": None})
            except Exception as exc:  # pragma: no cover - best effort
                results.append({"id": exp_id, "error": str(exc)})
        return {"results": results}


__all__ = ["DeterminedAPIClient"]
