"""Transport-independent planning and persistent compute task orchestration."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shlex
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from .models import (
    APIError,
    ConflictError,
    SubmissionUncertainError,
    TaskRecord,
    ValidationError,
)
from .profile import ComputeProfile
from .store import SQLiteTaskStore


_REQUEST_FIELDS = {
    "kind",
    "interactive",
    "overnight",
    "command",
    "workdir",
    "output_dir",
    "slots",
    "pool",
    "image",
    "code_revision",
    "experiment_config",
}
_FORBIDDEN_FIELDS = {
    "context",
    "contextdir",
    "contextpath",
    "files",
    "includes",
    "modeldefinition",
    "projectroot",
    "upload",
    "uploadcontext",
    "uploads",
}


def _normalized_key(key: Any) -> str:
    return "".join(character for character in str(key).lower() if character.isalnum())


def _reject_upload_fields(value: Any, path: str = "request") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = _normalized_key(key)
            if normalized in _FORBIDDEN_FIELDS:
                raise ValidationError(
                    f"{path}.{key} is forbidden; compute tasks use shared mounts only"
                )
            _reject_upload_fields(nested, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_upload_fields(nested, f"{path}[{index}]")


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} must be a non-empty string")
    return value


def _remote_id(entity: Any) -> str:
    if not isinstance(entity, Mapping):
        raise SubmissionUncertainError("launch response was not an object")
    value = entity.get("id")
    if value is None:
        raise SubmissionUncertainError("launch response did not contain a remote task id")
    return str(value)


def _remote_state(entity: Any) -> Optional[str]:
    if not isinstance(entity, Mapping):
        return None
    value = entity.get("state") or entity.get("status")
    if value is None:
        return None
    state = str(value)
    return state if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", state) else None


class ComputeService:
    """Plan and launch tasks while preserving local ownership and idempotency."""

    def __init__(
        self,
        client: Any,
        store: SQLiteTaskStore,
        profile: ComputeProfile,
        submission_stale_seconds: int = 300,
    ) -> None:
        if (
            isinstance(submission_stale_seconds, bool)
            or not isinstance(submission_stale_seconds, int)
            or submission_stale_seconds < 0
        ):
            raise ValueError("submission_stale_seconds must be a non-negative integer")
        self.client = client
        self.store = store
        self.profile = profile
        self.submission_stale_seconds = submission_stale_seconds

    def plan(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and normalize a request without contacting Determined."""

        if not isinstance(request, Mapping):
            raise ValidationError("request must be an object")
        unknown = set(request) - _REQUEST_FIELDS
        if unknown:
            raise ValidationError(f"request has unknown fields: {sorted(unknown)}")
        _reject_upload_fields(request)

        raw_kind = request.get("kind", "auto")
        if raw_kind not in {"auto", "command", "shell", "experiment"}:
            raise ValidationError("kind must be auto, command, shell, or experiment")
        interactive = request.get("interactive", False)
        overnight = request.get("overnight", False)
        if not isinstance(interactive, bool) or not isinstance(overnight, bool):
            raise ValidationError("interactive and overnight must be booleans")
        experiment_config = request.get("experiment_config")
        if experiment_config is not None and not isinstance(experiment_config, Mapping):
            raise ValidationError("experiment_config must be an object")

        if raw_kind == "auto":
            if interactive:
                kind = "shell"
            elif overnight or experiment_config is not None:
                kind = "experiment"
            else:
                kind = "command"
        else:
            kind = raw_kind
        if interactive and kind != "shell":
            raise ValidationError("interactive work must use a shell")
        if experiment_config is not None and kind != "experiment":
            raise ValidationError("experiment_config requires experiment kind")

        workdir = self.profile.validate_container_path(request.get("workdir"), "workdir")
        output_dir = self.profile.validate_container_path(request.get("output_dir"), "output_dir")
        slots = request.get("slots", self.profile.default_slots)
        if isinstance(slots, bool) or not isinstance(slots, int) or slots < 0:
            raise ValidationError("slots must be a non-negative integer")
        pool = request.get("pool", self.profile.default_pool)
        image = request.get("image", self.profile.default_image)
        _required_text(pool, "pool")
        _required_text(image, "image")
        code_revision = request.get("code_revision")
        if code_revision is not None and not isinstance(code_revision, str):
            raise ValidationError("code_revision must be a string or null")

        if kind == "experiment":
            config = self._experiment_config(
                experiment_config,
                request.get("command"),
                workdir,
                output_dir,
                slots,
                pool,
                image,
                code_revision,
            )
        else:
            config = self._task_config(
                kind, request, workdir, output_dir, slots, pool, image, code_revision
            )

        advisories: List[Dict[str, Any]] = []
        if overnight and kind != "experiment":
            advisories.append(
                {
                    "code": "overnight_experiment_recommended",
                    "message": "Long or overnight work is more robust as an experiment.",
                }
            )
        if kind == "shell" and self.profile.shell_inactivity_seconds is not None:
            advisories.append(
                {
                    "code": "shell_inactivity_policy",
                    "seconds": self.profile.shell_inactivity_seconds,
                    "message": (
                        "The deployment may stop an inactive shell after "
                        f"{self.profile.shell_inactivity_seconds} seconds."
                    ),
                }
            )
        return {
            "kind": kind,
            "config": config,
            "code_revision": code_revision,
            "advisories": advisories,
        }

    def _base_config(
        self,
        kind: str,
        workdir: str,
        output_dir: str,
        slots: int,
        pool: str,
        image: str,
        code_revision: Optional[str],
    ) -> Dict[str, Any]:
        variables = [
            f"COMPUTE_WORKDIR={workdir}",
            f"COMPUTE_OUTPUT_DIR={output_dir}",
        ]
        if code_revision is not None:
            variables.append(f"COMPUTE_CODE_REVISION={code_revision}")
        return {
            "resources": {
                ("slots_per_trial" if kind == "experiment" else "slots"): slots,
                "resource_pool": pool,
            },
            "environment": {
                "image": image,
                "environment_variables": variables,
            },
            "bind_mounts": [mount.as_config() for mount in self.profile.mounts],
        }

    def _task_config(
        self,
        kind: str,
        request: Mapping[str, Any],
        workdir: str,
        output_dir: str,
        slots: int,
        pool: str,
        image: str,
        code_revision: Optional[str],
    ) -> Dict[str, Any]:
        config = self._base_config(
            kind, workdir, output_dir, slots, pool, image, code_revision
        )
        command = request.get("command")
        if kind == "command":
            config["entrypoint"] = [
                "/bin/bash",
                "-lc",
                self._render_entrypoint(command, workdir, output_dir),
            ]
        elif command is not None:
            raise ValidationError(
                "shell kind does not accept command; startup is managed by Determined"
            )
        return config

    def _experiment_config(
        self,
        value: Optional[Mapping[str, Any]],
        command: Any,
        workdir: str,
        output_dir: str,
        slots: int,
        pool: str,
        image: str,
        code_revision: Optional[str],
    ) -> Dict[str, Any]:
        config = copy.deepcopy(dict(value or {}))
        self._validate_config_paths(config)
        if "bind_mounts" in config or "bindMounts" in config:
            raise ValidationError("experiment bind mounts come only from the compute profile")
        if config.get("description") is not None and not isinstance(
            config["description"], str
        ):
            raise ValidationError("experiment_config.description must be a string")

        resources = config.get("resources", {})
        if not isinstance(resources, Mapping):
            raise ValidationError("experiment_config.resources must be an object")
        resources = copy.deepcopy(dict(resources))
        resources.update({"slots_per_trial": slots, "resource_pool": pool})
        config["resources"] = resources

        environment = config.get("environment", {})
        if not isinstance(environment, Mapping):
            raise ValidationError("experiment_config.environment must be an object")
        environment = copy.deepcopy(dict(environment))
        environment["image"] = image
        variables = environment.get("environment_variables", [])
        if not isinstance(variables, list) or not all(isinstance(item, str) for item in variables):
            raise ValidationError("environment.environment_variables must be a list of strings")
        managed_names = {"COMPUTE_WORKDIR", "COMPUTE_OUTPUT_DIR", "COMPUTE_CODE_REVISION"}
        for item in variables:
            if item.split("=", 1)[0] in managed_names:
                raise ValidationError("compute-managed environment variables cannot be overridden")
        variables = list(variables) + [
            f"COMPUTE_WORKDIR={workdir}",
            f"COMPUTE_OUTPUT_DIR={output_dir}",
        ]
        if code_revision is not None:
            variables.append(f"COMPUTE_CODE_REVISION={code_revision}")
        environment["environment_variables"] = variables
        config["environment"] = environment
        config["bind_mounts"] = [mount.as_config() for mount in self.profile.mounts]
        if command is not None:
            if "entrypoint" in config:
                raise ValidationError(
                    "provide command or experiment_config.entrypoint, not both"
                )
            config["entrypoint"] = self._render_entrypoint(command, workdir, output_dir)
        elif "entrypoint" in config:
            config["entrypoint"] = self._render_entrypoint(
                config["entrypoint"], workdir, output_dir
            )
        else:
            raise ValidationError("experiment requires command or experiment_config.entrypoint")
        return config

    def _validate_config_paths(self, value: Any, path: str = "experiment_config") -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                normalized = _normalized_key(key)
                nested_path = f"{path}.{key}"
                if normalized == "hostpath":
                    self.profile.validate_host_path(nested, nested_path)
                elif normalized == "containerpath":
                    self.profile.validate_container_path(nested, nested_path)
                else:
                    self._validate_config_paths(nested, nested_path)
        elif isinstance(value, (list, tuple)):
            for index, nested in enumerate(value):
                self._validate_config_paths(nested, f"{path}[{index}]")

    @staticmethod
    def _render_entrypoint(command: Any, workdir: str, output_dir: str) -> str:
        if isinstance(command, str):
            if not command:
                raise ValidationError("command must not be empty")
            rendered = command
        elif isinstance(command, (list, tuple)):
            if not command or not all(isinstance(part, str) and part for part in command):
                raise ValidationError("command list must contain non-empty strings")
            rendered = " ".join(shlex.quote(part) for part in command)
        else:
            raise ValidationError("command must be a string or string-list")
        return (
            f"mkdir -p {shlex.quote(output_dir)} && "
            f"cd {shlex.quote(workdir)} && {rendered}"
        )

    def launch(self, request: Dict[str, Any], request_id: str, owner: str) -> Dict[str, Any]:
        request_id = _required_text(request_id, "request_id")
        owner = _required_text(owner, "owner")
        plan = self.plan(request)
        payload_hash = self._payload_hash(plan)
        workdir = self.profile.validate_container_path(request.get("workdir"), "workdir")
        output_dir = self.profile.validate_container_path(request.get("output_dir"), "output_dir")
        record, created = self.store.claim(
            request_id=request_id,
            owner=owner,
            payload_hash=payload_hash,
            profile_hash=self.profile.fingerprint,
            kind=plan["kind"],
            code_revision=plan["code_revision"],
            workdir=workdir,
            output_dir=output_dir,
            cluster_identity=self._cluster_identity(),
        )
        if not created:
            return self._public(record)

        self.store.mark_submitting(record.task_id)
        launch_config = self._with_submission_marker(plan["config"], record.submission_marker)
        try:
            entity = self.client.launch_task(plan["kind"], launch_config)
            remote_id = _remote_id(entity)
            return self._public(self.store.mark_submitted(record.task_id, remote_id))
        except SubmissionUncertainError as exc:
            self._best_effort_submission_state(record.task_id, "uncertain")
            self._attach_task_details(exc, record)
            raise
        except APIError as exc:
            # API-provided strings can echo request data; persist only a fixed class.
            self._best_effort_submission_state(record.task_id, "failed")
            self._attach_task_details(exc, record)
            raise
        except Exception as exc:
            self._best_effort_submission_state(record.task_id, "uncertain")
            error = SubmissionUncertainError("remote submission outcome is uncertain")
            self._attach_task_details(error, record)
            raise error from exc

    def _best_effort_submission_state(self, task_id: str, state: str) -> None:
        try:
            if state == "failed":
                self.store.mark_failed(task_id, "api_error")
            else:
                self.store.mark_uncertain(task_id)
        except Exception:
            # A durable pending/submitting record is already safe: retrying it cannot
            # launch again. Preserve the original API/outcome error for the caller.
            pass

    def status(self, task_id: str, owner: str) -> Dict[str, Any]:
        record = self.store.get_owned(task_id, owner)
        self._validate_binding(record)
        if record.remote_id is None and record.state in {"pending", "submitting"}:
            record = self.store.mark_stale_submission_uncertain(
                task_id, owner, self.submission_stale_seconds
            )
        result = self._public(record)
        if record.remote_id is None:
            return result
        entity = self.client.get_task(record.kind, record.remote_id)
        state = _remote_state(entity)
        if state != record.remote_state:
            record = self.store.update_remote_state(record.task_id, state)
            result = self._public(record)
        result["remote"] = entity
        return result

    def logs(self, task_id: str, owner: str, tail: int = 100) -> List[Any]:
        if isinstance(tail, bool) or not isinstance(tail, int) or tail <= 0:
            raise ValidationError("tail must be a positive integer")
        record = self.store.get_owned(task_id, owner)
        self._validate_binding(record)
        if record.remote_id is None:
            return []
        result = self.client.task_logs(record.kind, record.remote_id, tail)
        if not isinstance(result, list):
            raise APIError("task log response was not a list", code="invalid_api_response")
        return result

    def cancel(self, task_id: str, owner: str) -> Dict[str, Any]:
        record = self.store.get_owned(task_id, owner)
        self._validate_binding(record)
        if record.remote_id is None:
            raise ConflictError(
                "remote task id is unknown; reconcile the submission before cancelling",
                code="remote_id_unknown",
            )
        entity = self.client.cancel_task(record.kind, record.remote_id)
        state = _remote_state(entity)
        if state is not None:
            record = self.store.update_remote_state(record.task_id, state)
        updated = self._public(record)
        updated["cancellation_acknowledged"] = True
        updated["remote"] = entity
        return updated

    def list_tasks(self, owner: str) -> List[Dict[str, Any]]:
        owner = _required_text(owner, "owner")
        return [self._public(record) for record in self.store.list_owned(owner)]

    def reconcile(self, task_id: str, owner: str, remote_id: str) -> Dict[str, Any]:
        """Bind an uncertain record only after verifying its unguessable remote marker."""

        remote_id = _required_text(remote_id, "remote_id")
        record = self.store.get_owned(task_id, owner)
        self._validate_binding(record)
        if record.remote_id is not None:
            if record.remote_id != remote_id:
                raise ConflictError("task is already bound to a different remote id")
            return self._public(record)
        if record.state not in {"pending", "submitting", "submission_uncertain"}:
            raise ConflictError("task is not eligible for reconciliation")
        entity = self.client.get_task(record.kind, remote_id)
        description = self._entity_description(entity)
        if not description or description.split("\n", 1)[0] != record.submission_marker:
            raise ConflictError(
                "remote task identity marker does not match; refusing unsafe binding",
                code="identity_mismatch",
            )
        return self._public(
            self.store.bind_reconciled(record.task_id, remote_id, _remote_state(entity))
        )

    def _payload_hash(self, plan: Mapping[str, Any]) -> str:
        value = {
            "plan": plan,
            "profile_hash": self.profile.fingerprint,
            "cluster_identity": self._cluster_identity(),
        }
        try:
            encoded = json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValidationError("request must contain JSON-compatible values") from exc
        return hashlib.sha256(encoded).hexdigest()

    def _cluster_identity(self) -> Optional[str]:
        label = self.profile.cluster_identity
        api_url = getattr(self.client, "api_url", None)
        if api_url is not None:
            parsed = urlsplit(str(api_url))
            if not parsed.hostname:
                raise ValidationError("client api_url has no host for cluster binding")
            try:
                port = f":{parsed.port}" if parsed.port is not None else ""
            except ValueError as exc:
                raise ValidationError("client api_url has an invalid port") from exc
            hostname = parsed.hostname.lower()
            if ":" in hostname:
                hostname = f"[{hostname}]"
            path = parsed.path.rstrip("/")
            endpoint = f"{parsed.scheme.lower()}://{hostname}{port}{path}"
        else:
            client_identity = getattr(self.client, "cluster_identity", None)
            endpoint = str(client_identity) if client_identity is not None else None
        if label is None and endpoint is None:
            return None
        # Keep the operator-facing label and the resolved endpoint in the binding.
        # A reused label must never authorize operations against a different master.
        return json.dumps(
            {"endpoint": endpoint, "label": label},
            sort_keys=True,
            separators=(",", ":"),
        )

    def _validate_binding(self, record: TaskRecord) -> None:
        if (
            record.profile_hash != self.profile.fingerprint
            or record.cluster_identity != self._cluster_identity()
        ):
            raise ConflictError(
                "task belongs to a different compute profile or cluster",
                code="binding_mismatch",
            )

    @staticmethod
    def _public(record: TaskRecord) -> Dict[str, Any]:
        result = record.public_dict()
        if record.remote_id is None and record.state in {
            "pending",
            "submitting",
            "submission_uncertain",
        }:
            result["recovery"] = {
                "action": "reconcile",
                "safe_to_resubmit": False,
                "message": (
                    "The remote outcome is not bound. Inspect Determined and reconcile "
                    "a matching remote id; do not relaunch this request_id."
                ),
            }
        return result

    @staticmethod
    def _attach_task_details(exc: BaseException, record: TaskRecord) -> None:
        safe_details = {"task_id": record.task_id, "request_id": record.request_id}
        try:
            setattr(exc, "details", safe_details)
            setattr(exc, "task_id", record.task_id)
        except Exception:
            pass

    @staticmethod
    def _with_submission_marker(config: Mapping[str, Any], marker: str) -> Dict[str, Any]:
        result = copy.deepcopy(dict(config))
        description = result.get("description")
        if description is not None and not isinstance(description, str):
            raise ValidationError("config description must be a string")
        result["description"] = marker + (("\n" + description) if description else "")
        return result

    @staticmethod
    def _entity_description(entity: Any) -> Optional[str]:
        if not isinstance(entity, Mapping):
            return None
        candidates: Sequence[Any] = (
            entity.get("description"),
            entity.get("config", {}).get("description")
            if isinstance(entity.get("config"), Mapping)
            else None,
        )
        for candidate in candidates:
            if isinstance(candidate, str):
                return candidate
        return None


__all__ = ["ComputeService"]
