"""Reliable, dependency-light access to the Determined REST API."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
import yaml

from determined_batch.utils.secrets import load_secrets

ErrorCode = Union[int, str, None]


class APIError(RuntimeError):
    def __init__(self, message: str, *, code: ErrorCode = None, details: Any = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.code, self.details, self.retryable = code, details, retryable


class SubmissionUncertainError(APIError):
    """A mutation failed after dispatch, so its server-side outcome is unknown."""

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message, code="submission_uncertain", details=details, retryable=False)


def _normalize_api_url(api_url: Optional[str]) -> str:
    url = api_url or os.environ.get("DET_MASTER") or os.environ.get("DET_MASTER_ADDR") or os.environ.get("DET_MASTER_HOST")
    if not url:
        url = "http://localhost:8080"
    elif url.startswith(("http://", "https://")):
        return url.rstrip("/")
    else:
        if url.count(":") > 1 and not url.startswith("["):
            url = f"[{url}]"
        parts = urlsplit(f"http://{url}")
        netloc = parts.netloc if parts.port is not None else f"{parts.netloc}:8080"
        url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url.rstrip("/")


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.lower() in {"1", "true", "yes", "y", "on"}


def _error_from_response(response: requests.Response) -> APIError:
    try:
        payload = response.json()
    except (ValueError, requests.exceptions.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    status = response.status_code
    message = payload.get("message") or payload.get("error") or response.text or getattr(response, "reason", "API request failed")
    return APIError(
        f"{status} {message}", code=payload.get("code", status), details=payload.get("details"),
        retryable=status == 429 or status >= 500,
    )


def _login_for_token(api_url: str, username: str, password: str, verify_ssl: bool) -> str:
    endpoint = urljoin(api_url.rstrip("/") + "/", "api/v1/auth/login")
    try:
        response = requests.post(endpoint, headers={"Content-Type": "application/json"}, json={"username": username, "password": password}, timeout=15, verify=verify_ssl)
    except requests.RequestException as exc:
        raise APIError("Could not authenticate with Determined", code="transport_error", details={"endpoint": "api/v1/auth/login"}, retryable=True) from exc
    if response.status_code >= 400:
        raise _error_from_response(response)
    try:
        payload = response.json()
    except (ValueError, requests.exceptions.JSONDecodeError) as exc:
        raise APIError("Determined login returned invalid JSON", code="invalid_response") from exc
    token = payload.get("token") if isinstance(payload, dict) else None
    if not token:
        raise APIError("Determined login response did not contain a token", code="invalid_response")
    return str(token)


class DeterminedAPIClient:
    _TASK_KINDS = {"command", "shell", "experiment"}

    def __init__(self, api_url: Optional[str] = None, api_token: Optional[str] = None, secrets_path: Optional[Path] = None, verify_ssl: Optional[bool] = None) -> None:
        secrets = load_secrets(secrets_path)
        secret_master = secrets.get("DET_MASTER") or secrets.get("DET_MASTER_ADDR") or secrets.get("DET_MASTER_HOST")
        environment_master = (
            os.environ.get("DET_MASTER")
            or os.environ.get("DET_MASTER_ADDR")
            or os.environ.get("DET_MASTER_HOST")
        )
        self.api_url = _normalize_api_url(api_url or environment_master or secret_master)
        self.verify_ssl = _bool_env("DET_VERIFY_SSL", False) if verify_ssl is None else verify_ssl
        self.api_token = self._resolve_token(api_token, secrets)
        self.headers: Dict[str, str] = {}
        if self.api_token:
            self.headers["Authorization"] = f"Bearer {self.api_token}"

    def _url(self, endpoint: str) -> str:
        return urljoin(self.api_url.rstrip("/") + "/", endpoint)

    @staticmethod
    def _json_response(response: requests.Response, *, mutation: bool = False) -> Dict[str, Any]:
        if response.status_code >= 400:
            error = _error_from_response(response)
            if mutation and response.status_code >= 500:
                raise SubmissionUncertainError(
                    "Determined mutation outcome is unknown after a server error",
                    details={"status_code": response.status_code, "error": str(error)},
                ) from error
            raise error
        if not getattr(response, "content", None) and not response.text:
            return {}
        try:
            data = response.json()
        except (ValueError, requests.exceptions.JSONDecodeError) as exc:
            if mutation:
                raise SubmissionUncertainError("Determined returned invalid JSON after accepting the request", details={"status_code": response.status_code}) from exc
            raise APIError("Determined returned invalid JSON", code="invalid_response") from exc
        if not isinstance(data, dict):
            if mutation:
                raise SubmissionUncertainError("Determined returned a non-object JSON response")
            raise APIError("Determined returned a non-object JSON response", code="invalid_response")
        return data

    def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            response = requests.get(self._url(endpoint), headers=self.headers, params=params, timeout=30, verify=self.verify_ssl)
        except requests.RequestException as exc:
            raise APIError("Determined request failed", code="transport_error", details={"endpoint": endpoint}, retryable=True) from exc
        return self._json_response(response)

    def _post(
        self,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        *,
        mutation: bool = True,
    ) -> Dict[str, Any]:
        if files:
            raise ValueError("File uploads are not supported by determined-batch")
        try:
            response = requests.post(self._url(endpoint), headers={**self.headers, "Content-Type": "application/json"}, json=data, timeout=60, verify=self.verify_ssl)
        except requests.RequestException as exc:
            if mutation:
                raise SubmissionUncertainError("Determined mutation outcome is unknown", details={"endpoint": endpoint}) from exc
            raise APIError(
                "Determined validation request failed",
                code="transport_error",
                details={"endpoint": endpoint},
                retryable=True,
            ) from exc
        return self._json_response(response, mutation=mutation)

    def _delete(self, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            response = requests.delete(self._url(endpoint), headers={**self.headers, "Content-Type": "application/json"}, json=data, timeout=60, verify=self.verify_ssl)
        except requests.RequestException as exc:
            raise SubmissionUncertainError("Determined mutation outcome is unknown", details={"endpoint": endpoint}) from exc
        return self._json_response(response, mutation=True)

    def _stream_logs(self, endpoint: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            response = requests.get(self._url(endpoint), headers=self.headers, params=params, timeout=30, verify=self.verify_ssl, stream=True)
        except requests.RequestException as exc:
            raise APIError("Determined log request failed", code="transport_error", details={"endpoint": endpoint}, retryable=True) from exc
        if response.status_code >= 400:
            raise _error_from_response(response)
        logs: List[Dict[str, Any]] = []
        try:
            try:
                for raw_line in response.iter_lines(decode_unicode=True):
                    if not raw_line:
                        continue
                    try:
                        item = json.loads(raw_line)
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise APIError("Determined log stream contained invalid JSON", code="invalid_response") from exc
                    if not isinstance(item, dict):
                        raise APIError("Determined log stream contained a non-object item", code="invalid_response")
                    if item.get("error"):
                        error = item["error"]
                        if isinstance(error, dict):
                            http_code = error.get("httpCode") or 0
                            raise APIError(str(error.get("message") or "Determined log stream failed"), code=error.get("grpcCode") or http_code, details=error.get("details"), retryable=http_code >= 500)
                        raise APIError(str(error))
                    result = item.get("result")
                    if not isinstance(result, dict):
                        raise APIError("Determined log stream item had no result", code="invalid_response")
                    logs.append(result)
            except requests.RequestException as exc:
                raise APIError(
                    "Determined log stream failed", code="transport_error",
                    details={"endpoint": endpoint}, retryable=True,
                ) from exc
        finally:
            close = getattr(response, "close", None)
            if close:
                close()
        return logs

    def _resolve_token(self, api_token: Optional[str], secrets: Dict[str, str]) -> Optional[str]:
        if api_token:
            return api_token
        if os.environ.get("DET_API_TOKEN"):
            return os.environ["DET_API_TOKEN"]
        if secrets.get("DET_API_TOKEN"):
            return secrets["DET_API_TOKEN"]
        username = secrets.get("DET_USERNAME") or os.environ.get("DET_USERNAME")
        password = secrets.get("DET_PASSWORD") or os.environ.get("DET_PASSWORD")
        return _login_for_token(self.api_url, username, password, self.verify_ssl) if username and password else None

    @classmethod
    def _kind(cls, kind: str) -> str:
        normalized = kind.lower().rstrip("s")
        if normalized not in cls._TASK_KINDS:
            raise ValueError("kind must be one of: command, shell, experiment")
        return normalized

    @staticmethod
    def _entity(response: Dict[str, Any], kind: str, *, mutation: bool = False) -> Dict[str, Any]:
        entity = response.get(kind)
        if not isinstance(entity, dict) or entity.get("id") is None:
            message = f"Determined response did not contain a {kind} with an id"
            if mutation:
                raise SubmissionUncertainError(message, details=response)
            raise APIError(message, code="invalid_response", details=response)
        return entity

    @staticmethod
    def _safe_shell(entity: Dict[str, Any]) -> Dict[str, Any]:
        safe = DeterminedAPIClient._redact_secrets(entity)
        safe.setdefault("reconnectCommand", f"det shell show_ssh_command {safe['id']}")
        return safe

    @staticmethod
    def _redact_secrets(value: Any) -> Any:
        if isinstance(value, dict):
            safe: Dict[str, Any] = {}
            for key, item in value.items():
                normalized = "".join(char for char in key.lower() if char.isalnum())
                embedded = (
                    "password",
                    "passwd",
                    "secret",
                    "credential",
                    "authorization",
                    "cookie",
                )
                sensitive_key = (
                    normalized in {"auth", "token"}
                    or any(part in normalized for part in embedded)
                    or any(
                        normalized.endswith(suffix)
                        for suffix in ("apikey", "accesskey", "sessionkey", "privatekey")
                    )
                    or normalized.endswith("token")
                )
                if sensitive_key:
                    continue
                if normalized in {"environmentvariables", "proxyenvironmentvariables"}:
                    safe[key] = "[redacted]"
                else:
                    safe[key] = DeterminedAPIClient._redact_secrets(item)
            return safe
        if isinstance(value, list):
            return [DeterminedAPIClient._redact_secrets(item) for item in value]
        return value

    @staticmethod
    def _upload_fields(value: Any, path: str = "config") -> List[str]:
        forbidden = {
            "context",
            "contextdir",
            "contextpath",
            "data",
            "files",
            "includes",
            "modeldefinition",
            "projectroot",
            "upload",
            "uploadcontext",
            "uploads",
        }
        found: List[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                nested_path = f"{path}.{key}"
                normalized = "".join(char for char in str(key).lower() if char.isalnum())
                if normalized in forbidden:
                    found.append(nested_path)
                found.extend(DeterminedAPIClient._upload_fields(item, nested_path))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                found.extend(DeterminedAPIClient._upload_fields(item, f"{path}[{index}]"))
        return found

    def launch_task(self, kind: str, config: Dict[str, Any]) -> Dict[str, Any]:
        kind = self._kind(kind)
        if not isinstance(config, dict):
            raise TypeError("config must be a dictionary")
        present = self._upload_fields(config)
        if present:
            raise ValueError("Code/data uploads are not supported; remove: " + ", ".join(present))
        body = {"config": yaml.safe_dump(config, sort_keys=False), "activate": True} if kind == "experiment" else {"config": config}
        entity = self._entity(self._post(f"api/v1/{kind}s", data=body), kind, mutation=True)
        return self._safe_shell(entity) if kind == "shell" else entity

    def get_task(self, kind: str, task_id: str) -> Dict[str, Any]:
        kind = self._kind(kind)
        response = self._get(f"api/v1/{kind}s/{task_id}")
        entity = dict(self._entity(response, kind))
        if isinstance(response.get("config"), dict):
            entity["config"] = self._redact_secrets(response["config"])
        return self._safe_shell(entity) if kind == "shell" else entity

    def task_logs(self, kind: str, task_id: str, tail: int = 100) -> List[Dict[str, Any]]:
        kind = self._kind(kind)
        if tail < 0:
            raise ValueError("tail must be non-negative")
        if kind == "experiment":
            trials = self.get_trials(task_id)
            if not trials:
                return []

            def trial_key(trial: Dict[str, Any]) -> tuple:
                try:
                    return (1, int(trial.get("id")))
                except (TypeError, ValueError):
                    return (0, str(trial.get("id") or ""))
            return self.get_trial_logs(str(max(trials, key=trial_key)["id"]), limit=tail)
        entries = self._stream_logs(f"api/v1/tasks/{task_id}/logs", {"limit": tail, "follow": False, "orderBy": "ORDER_BY_DESC"})
        entries.reverse()
        return entries

    def cancel_task(self, kind: str, task_id: str) -> Dict[str, Any]:
        kind = self._kind(kind)
        action = "cancel" if kind == "experiment" else "kill"
        response = self._post(f"api/v1/{kind}s/{task_id}/{action}", data={})
        if kind == "experiment":
            # The schema intentionally defines an empty response.  Preserve
            # successful acknowledgement without claiming a remote state.
            return {"id": str(task_id), "acknowledged": True, **response}
        entity = self._entity(response, kind, mutation=True)
        return self._safe_shell(entity) if kind == "shell" else entity

    # Existing raw API methods.
    def get_experiments(self, limit: int = 100, offset: int = 0, states: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if states:
            params["states"] = states
        value = self._get("api/v1/experiments", params=params).get("experiments")
        if not isinstance(value, list):
            raise APIError("Experiment list response had no experiments array", code="invalid_response")
        return value

    def get_experiment(self, experiment_id: str) -> Dict[str, Any]:
        return self._get(f"api/v1/experiments/{experiment_id}")

    def get_trials(self, experiment_id: str) -> List[Dict[str, Any]]:
        value = self._get(f"api/v1/experiments/{experiment_id}/trials").get("trials")
        if not isinstance(value, list):
            raise APIError("Trial list response had no trials array", code="invalid_response")
        return value

    def get_trial_logs(self, trial_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        entries = self._stream_logs(f"api/v1/trials/{trial_id}/logs", {"limit": limit, "follow": False, "orderBy": "ORDER_BY_DESC"})
        entries.reverse()
        return entries

    def get_experiment_logs(self, experiment_id: str, tail: int = 100) -> Optional[str]:
        entries = self.task_logs("experiment", experiment_id, tail=tail)
        messages = [str(item.get("message") or item.get("log")) for item in entries if item.get("message") is not None or item.get("log") is not None]
        return "\n".join(messages) if messages else None

    def get_resource_pools(self) -> List[Dict[str, Any]]:
        value = self._get("api/v1/resource-pools").get("resourcePools")
        if not isinstance(value, list):
            raise APIError("Resource-pool response had no resourcePools array", code="invalid_response")
        return value

    def get_slots(self) -> List[Dict[str, Any]]:
        agents = self._get("api/v1/agents").get("agents")
        if not isinstance(agents, list):
            raise APIError("Agent response had no agents array", code="invalid_response")
        slots: List[Dict[str, Any]] = []
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            raw_slots = agent.get("slots", {})
            slot_items = list(raw_slots.values()) if isinstance(raw_slots, dict) else raw_slots
            if not isinstance(slot_items, list):
                continue
            memberships = agent.get("resourcePools")
            pool_names: List[Optional[str]] = ([memberships] if isinstance(memberships, str) else [str(pool) for pool in memberships]) if memberships else [None]
            for slot in slot_items:
                if not isinstance(slot, dict):
                    continue
                container = slot.get("container")
                container_id = container.get("id") if isinstance(container, dict) else container
                for pool_name in pool_names:
                    slots.append({"agent_id": agent.get("id"), "agent_label": agent.get("label"), "slot_id": slot.get("id"), "device": slot.get("device"), "enabled": slot.get("enabled", True), "container_id": container_id, "resource_pool": pool_name})
        return slots

    def get_workspace_id(self, workspace_name: str = "default") -> Optional[int]:
        value = self._get("api/v1/workspaces", params={"name": workspace_name}).get("workspaces")
        if not isinstance(value, list):
            raise APIError("Workspace response had no workspaces array", code="invalid_response")
        return value[0].get("id") if value else None

    def get_project(self, workspace_name: str, project_name: str) -> Optional[Dict[str, Any]]:
        workspace_id = self.get_workspace_id(workspace_name)
        if workspace_id is None:
            return None
        value = self._get(f"api/v1/workspaces/{workspace_id}/projects", params={"name": project_name}).get("projects")
        if not isinstance(value, list):
            raise APIError("Project response had no projects array", code="invalid_response")
        return value[0] if value else None

    def create_project(self, workspace_name: str, project_name: str, description: Optional[str] = None) -> Dict[str, Any]:
        workspace_id = self.get_workspace_id(workspace_name)
        if workspace_id is None:
            raise APIError(f"Workspace not found: {workspace_name}", code="not_found")
        body: Dict[str, Any] = {"name": project_name}
        if description:
            body["description"] = description
        data = self._post(f"api/v1/workspaces/{workspace_id}/projects", data=body)
        if not isinstance(data.get("project"), dict):
            raise SubmissionUncertainError("Project creation response had no project", details=data)
        return data["project"]

    def ensure_project_exists(self, workspace_name: str, project_name: str, description: Optional[str] = None) -> bool:
        if not self.get_project(workspace_name, project_name):
            self.create_project(workspace_name, project_name, description)
        return True

    def create_experiment(self, config_path: Path, project_root: Optional[Path] = None, model_definition: Optional[List[Dict[str, Any]]] = None, activate: bool = True, validate_only: bool = False) -> Dict[str, Any]:
        if project_root is not None:
            raise ValueError("project_root uploads are not supported; config must reference remote code")
        if model_definition:
            raise ValueError("model_definition uploads are not supported; config must reference remote code")
        return self._post(
            "api/v1/experiments",
            data={
                "config": Path(config_path).read_text(encoding="utf-8"),
                "activate": activate,
                "validateOnly": validate_only,
            },
            mutation=not validate_only,
        )

    def delete_experiment(self, experiment_id: int) -> None:
        self._delete(f"api/v1/experiments/{experiment_id}")

    def kill_experiment(self, experiment_id: int) -> None:
        self._post(f"api/v1/experiments/{experiment_id}/kill", data={})

    def cancel_experiment(self, experiment_id: int) -> None:
        self._post(f"api/v1/experiments/{experiment_id}/cancel", data={})

    def delete_experiments(self, experiment_ids: List[int], project_id: Optional[int] = None) -> Dict[str, Any]:
        if project_id is not None:
            return self._delete(f"api/v1/projects/{project_id}/experiments/delete", data={"experimentIds": experiment_ids})
        results = []
        for experiment_id in experiment_ids:
            try:
                self.delete_experiment(experiment_id)
            except APIError as exc:
                results.append({"id": experiment_id, "error": str(exc)})
            else:
                results.append({"id": experiment_id, "error": None})
        return {"results": results}


__all__ = ["APIError", "SubmissionUncertainError", "DeterminedAPIClient"]
