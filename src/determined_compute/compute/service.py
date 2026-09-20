"""Transport-independent planning and persistent compute task orchestration."""

from __future__ import annotations

import copy
import hashlib
import json
import posixpath
import re
import shlex
import unicodedata
import uuid
from pathlib import PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from determined_compute.core.api_client import DeterminedAPIClient

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
    "name",
    "description",
    "allow_queue",
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
_NAME_MAX_LENGTH = 128
_DESCRIPTION_MAX_LENGTH = 2048
_SUBMISSION_MARKER_VARIABLE = "COMPUTE_SUBMISSION_MARKER"
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


def _display_name(value: Any, field: str = "name") -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    result = value.strip()
    if not result:
        raise ValidationError(f"{field} must not be empty")
    if len(result) > _NAME_MAX_LENGTH:
        raise ValidationError(f"{field} must be at most {_NAME_MAX_LENGTH} characters")
    if any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in result
    ):
        raise ValidationError(f"{field} must not contain control characters")
    return result


def _display_description(value: Any, field: str = "description") -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string or null")
    result = value.strip()
    if not result:
        return None
    if len(result) > _DESCRIPTION_MAX_LENGTH:
        raise ValidationError(
            f"{field} must be at most {_DESCRIPTION_MAX_LENGTH} characters"
        )
    if any(
        unicodedata.category(character).startswith("C")
        and character not in {"\n", "\t"}
        for character in result
    ):
        raise ValidationError(f"{field} contains an unsupported control character")
    return result


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
        inspector: Any = None,
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
        self.inspector = inspector

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
        allow_queue = request.get("allow_queue", False)
        if (
            not isinstance(interactive, bool)
            or not isinstance(overnight, bool)
            or not isinstance(allow_queue, bool)
        ):
            raise ValidationError(
                "interactive, overnight, and allow_queue must be booleans"
            )
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

        workdir = self.profile.validate_writable_container_path(
            request.get("workdir"), "workdir"
        )
        output_dir = self.profile.validate_writable_container_path(
            request.get("output_dir"), "output_dir"
        )
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
        requested_name = request.get("name")
        requested_description = request.get("description")
        if requested_name is not None:
            name = _display_name(requested_name)
        elif (
            kind == "experiment"
            and experiment_config is not None
            and experiment_config.get("name") is not None
        ):
            name = _display_name(
                experiment_config["name"], "experiment_config.name"
            )
        else:
            name = None
        if requested_description is not None:
            description = _display_description(requested_description)
        elif kind == "experiment" and experiment_config is not None:
            description = _display_description(
                experiment_config.get("description"),
                "experiment_config.description",
            )
        else:
            description = None
        generated_name = name is None
        if generated_name:
            basename = PurePosixPath(workdir).name or "shared-root"
            name = _display_name(f"{kind}: {basename}")

        if kind == "experiment":
            config = self._experiment_config(
                experiment_config,
                name,
                description,
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
                kind,
                request,
                name,
                description,
                workdir,
                output_dir,
                slots,
                pool,
                image,
                code_revision,
            )

        advisories: List[Dict[str, Any]] = []
        if generated_name:
            advisories.append(
                {
                    "code": "generated_task_name",
                    "message": (
                        f"Generated task name '{name}'. Supply name and description "
                        "to make task lists easier to scan."
                    ),
                }
            )
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
            "name": name,
            "description": description,
            "allow_queue": allow_queue,
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
        name: str,
        description: Optional[str],
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
        config["description"] = name + (
            ("\n" + description) if description is not None else ""
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
        name: str,
        description: Optional[str],
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
        checkpoint = config.get("checkpoint_storage")
        if checkpoint is not None:
            if not isinstance(checkpoint, Mapping) or checkpoint.get("type") != "shared_fs":
                raise ValidationError("checkpoint_storage must be a shared_fs configuration on mapped storage")
            if any(_normalized_key(key) in {"checkpointpath", "tensorboardpath"} for key in checkpoint):
                raise ValidationError("checkpoint_storage must use storage_path instead of legacy path aliases")
            checkpoint_root = self.profile.validate_writable_host_path(
                checkpoint.get("host_path"), "experiment_config.checkpoint_storage.host_path"
            )
            if checkpoint.get("storage_path") is not None:
                storage_path = checkpoint["storage_path"]
                if not isinstance(storage_path, str):
                    raise ValidationError("checkpoint_storage.storage_path must be a string")
                resolved_storage = self.profile.validate_writable_host_path(
                    posixpath.join(checkpoint_root, storage_path),
                    "experiment_config.checkpoint_storage.storage_path",
                )
                if resolved_storage != checkpoint_root and not resolved_storage.startswith(checkpoint_root.rstrip("/") + "/"):
                    raise ValidationError("checkpoint_storage.storage_path must remain inside host_path")
            if checkpoint.get("container_path") is not None:
                self.profile.validate_writable_container_path(
                    checkpoint["container_path"], "experiment_config.checkpoint_storage.container_path"
                )
        if "bind_mounts" in config or "bindMounts" in config:
            raise ValidationError("experiment bind mounts come only from the compute profile")
        config["name"] = name
        if description is None:
            config.pop("description", None)
        else:
            config["description"] = description

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
        managed_names = {
            "COMPUTE_WORKDIR",
            "COMPUTE_OUTPUT_DIR",
            "COMPUTE_CODE_REVISION",
            _SUBMISSION_MARKER_VARIABLE,
        }
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
        existing = self.store.lookup_request(request_id, owner)
        if existing is not None:
            self._validate_idempotent_payload(existing, payload_hash, request, plan)
            return self._public(existing)

        if not plan["allow_queue"]:
            try:
                self._inspector().require_capacity(plan["kind"], plan["config"])
            except APIError:
                # A concurrent process may have claimed this id after our lookup.
                existing = self.store.lookup_request(request_id, owner)
                if existing is not None:
                    self._validate_idempotent_payload(
                        existing, payload_hash, request, plan
                    )
                    return self._public(existing)
                raise
        workdir = self.profile.validate_container_path(request.get("workdir"), "workdir")
        output_dir = self.profile.validate_container_path(request.get("output_dir"), "output_dir")
        record, created = self.store.claim(
            request_id=request_id,
            owner=owner,
            payload_hash=payload_hash,
            profile_hash=self.profile.fingerprint,
            kind=plan["kind"],
            code_revision=plan["code_revision"],
            name=plan["name"],
            description=plan["description"],
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

    def _inspector(self) -> Any:
        if self.inspector is None:
            from .admission import ResourceInspector

            self.inspector = ResourceInspector(self.client)
        return self.inspector

    def _validate_idempotent_payload(
        self,
        record: TaskRecord,
        payload_hash: str,
        request: Mapping[str, Any],
        plan: Mapping[str, Any],
    ) -> None:
        if record.origin == "adopted":
            raise ConflictError(
                "an adopted task cannot be used as a launch retry",
                code="idempotency_conflict",
            )
        if record.payload_hash == payload_hash:
            return
        new_fields = {"name", "description", "allow_queue"}
        legacy_record = record.name is None and record.description is None
        legacy_request = not new_fields.intersection(request)
        if (
            legacy_record
            and legacy_request
            and record.payload_hash == self._legacy_payload_hash(plan, request)
        ):
            return
        raise ConflictError(
            "request_id was already used with a different request",
            code="idempotency_conflict",
        )

    def _legacy_payload_hash(
        self, plan: Mapping[str, Any], request: Mapping[str, Any]
    ) -> str:
        """Reconstruct the 0.4 plan hash for migrated task rows only."""

        config = copy.deepcopy(dict(plan["config"]))
        if plan["kind"] == "experiment":
            original = request.get("experiment_config")
            original = original if isinstance(original, Mapping) else {}
            for field in ("name", "description"):
                if field in original:
                    config[field] = copy.deepcopy(original[field])
                else:
                    config.pop(field, None)
        else:
            config.pop("description", None)
        legacy_plan = {
            "kind": plan["kind"],
            "config": config,
            "code_revision": plan["code_revision"],
            "advisories": [
                copy.deepcopy(item)
                for item in plan["advisories"]
                if item.get("code") != "generated_task_name"
            ],
        }
        return self._payload_hash(legacy_plan)

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
        if record.origin == "adopted":
            self._check_adopted_entity(record, entity)
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
        if record.origin == "adopted":
            self._check_adopted_entity(
                record, self.client.get_task(record.kind, record.remote_id)
            )
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
        if record.origin == "adopted":
            self._check_adopted_entity(
                record, self.client.get_task(record.kind, record.remote_id)
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

    @staticmethod
    def _management_kind(kind: Any) -> str:
        if not isinstance(kind, str) or kind not in {"command", "shell", "experiment"}:
            raise ValidationError("kind must be command, shell, or experiment")
        return kind

    @staticmethod
    def _management_remote_id(kind: str, value: Any) -> str:
        if kind == "experiment":
            try:
                return DeterminedAPIClient.normalize_user_id(value)
            except ValueError as exc:
                raise ValidationError("experiment remote_id must be a positive integer") from exc
        if not isinstance(value, str):
            raise ValidationError("command and shell remote_id must be a UUID")
        try:
            return str(uuid.UUID(value))
        except ValueError as exc:
            raise ValidationError("command and shell remote_id must be a UUID") from exc

    def _remote_account(self) -> tuple[str, Dict[str, str]]:
        cluster_id = self.client.get_cluster_id()
        user = self.client.get_current_user()
        if (
            not isinstance(cluster_id, str)
            or not cluster_id.strip()
            or len(cluster_id.encode("utf-8")) > 256
            or not isinstance(user, Mapping)
            or not isinstance(user.get("username"), str)
            or not user["username"].strip()
        ):
            raise APIError("Remote cluster or account identity is unavailable", code="invalid_response")
        try:
            user_id = DeterminedAPIClient.normalize_user_id(user.get("id"))
        except ValueError as exc:
            raise APIError("Remote account identity is unavailable", code="invalid_response") from exc
        return cluster_id.strip(), {"id": user_id, "username": user["username"].strip()}

    @staticmethod
    def _check_remote_owner(entity: Any, user_id: str) -> None:
        if not isinstance(entity, Mapping):
            raise APIError("Remote task response is malformed", code="invalid_response")
        try:
            task_user_id = DeterminedAPIClient.normalize_user_id(entity.get("userId"))
        except ValueError as exc:
            raise APIError("Remote task ownership cannot be established", code="ownership_unavailable") from exc
        if task_user_id != user_id:
            raise ConflictError(
                "remote task is not owned by the authenticated account",
                code="ownership_mismatch",
            )

    def _check_remote_entity(self, kind: str, remote_id: str, entity: Any, user_id: str) -> None:
        self._check_remote_owner(entity, user_id)
        try:
            actual_id = self._management_remote_id(kind, entity.get("id"))
        except ValidationError as exc:
            raise APIError("Remote task identity is malformed", code="invalid_response") from exc
        if actual_id != remote_id:
            raise ConflictError("remote task identity does not match", code="identity_mismatch")

    def _check_adopted_entity(self, record: TaskRecord, entity: Any) -> None:
        self._check_remote_entity(record.kind, record.remote_id, entity, record.remote_user_id)

    @staticmethod
    def _remote_text(value: Any, limit: int = 4096) -> Optional[str]:
        if not isinstance(value, str):
            return None
        text = value.strip()
        return text[:limit] if text else None

    @classmethod
    def _remote_metadata(cls, entity: Mapping[str, Any]) -> Dict[str, Optional[str]]:
        description = cls._remote_text(entity.get("description"))
        name = cls._remote_text(entity.get("name"), 256) or cls._remote_text(entity.get("displayName"), 256)
        if name is None and description:
            name = description.splitlines()[0][:256]
        return {"name": name, "description": description}

    def discover(self, kind: str, owner: str, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        """Read one remote page for the authenticated account without registering tasks."""
        kind = self._management_kind(kind)
        owner = _required_text(owner, "owner")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValidationError("limit must be an integer between 1 and 100")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValidationError("offset must be a non-negative integer")
        cluster_id, user = self._remote_account()
        page = self.client.list_remote_tasks(kind, user_id=user["id"], limit=limit, offset=offset)
        if not isinstance(page, Mapping) or not isinstance(page.get("tasks"), list):
            raise APIError("Remote task page is malformed", code="invalid_response")
        pagination = page.get("pagination")
        if (
            not isinstance(pagination, Mapping)
            or pagination.get("limit") != limit
            or pagination.get("offset") != offset
            or isinstance(pagination.get("total"), bool)
            or not isinstance(pagination.get("total"), int)
            or pagination["total"] < 0
            or (
                page["tasks"]
                and pagination["total"] < offset + len(page["tasks"])
            )
            or len(page["tasks"]) > limit
        ):
            raise APIError("Remote task pagination is malformed", code="invalid_response")
        tasks = []
        binding = self._cluster_identity()
        for entity in page["tasks"]:
            self._check_remote_owner(entity, user["id"])
            try:
                remote_id = self._management_remote_id(kind, entity.get("id"))
            except ValidationError as exc:
                raise APIError("Remote task identity is malformed", code="invalid_response") from exc
            local = self.store.lookup_remote(
                owner=owner, kind=kind, remote_id=remote_id,
                cluster_identity=binding, remote_cluster_id=cluster_id,
            )
            if (
                local is not None
                and local.origin == "adopted"
                and local.remote_user_id != user["id"]
            ):
                local = None
            tasks.append({
                "kind": kind,
                "remote_id": remote_id,
                **self._remote_metadata(entity),
                "remote_state": _remote_state(entity),
                "remote_user_id": user["id"],
                "remote_username": self._remote_text(entity.get("username"), 256),
                "resource_pool": self._remote_text(entity.get("resourcePool"), 256),
                "start_time": self._remote_text(entity.get("startTime"), 128),
                "local_task_id": local.task_id if local is not None else None,
            })
        next_offset = offset + len(tasks)
        return {
            "kind": kind,
            "remote_cluster_id": cluster_id,
            "account": user,
            "tasks": tasks,
            "pagination": {
                "offset": offset, "limit": limit, "total": pagination["total"],
                "next_offset": next_offset if tasks and next_offset < pagination["total"] else None,
            },
        }

    def adopt(self, kind: str, remote_id: str, owner: str) -> Dict[str, Any]:
        """Register an existing owned task locally; never submit or edit a remote task."""
        kind = self._management_kind(kind)
        remote_id = self._management_remote_id(kind, remote_id)
        owner = _required_text(owner, "owner")
        cluster_id, user = self._remote_account()
        entity = self.client.get_task(kind, remote_id)
        self._check_remote_entity(kind, remote_id, entity, user["id"])
        record, _created = self.store.adopt(
            owner=owner, kind=kind, remote_id=remote_id,
            remote_state=_remote_state(entity),
            cluster_identity=self._cluster_identity(),
            remote_cluster_id=cluster_id, remote_user_id=user["id"],
            profile_hash=self.profile.fingerprint,
            submission_marker=(
                entity.get("submissionMarker")
                if isinstance(entity.get("submissionMarker"), str)
                else None
            ),
            legacy_submission_markers=tuple(
                marker
                for description in self._entity_descriptions(entity)
                if (
                    marker := DeterminedAPIClient._valid_submission_marker(
                        description.splitlines()[0] if description else None
                    )
                ) is not None
            ),
            **self._remote_metadata(entity),
        )
        # A task launched through this owner/database retains its original binding.
        if record.origin == "submitted":
            self._validate_binding(record)
        if record.remote_state != _remote_state(entity):
            record = self.store.update_remote_state(record.task_id, _remote_state(entity))
        return self._public(record)

    def reconcile(self, task_id: str, owner: str, remote_id: str) -> Dict[str, Any]:
        """Bind an uncertain record only after verifying its unguessable remote marker."""

        remote_id = _required_text(remote_id, "remote_id")
        record = self.store.get_owned(task_id, owner)
        if record.origin == "adopted":
            raise ConflictError("an adopted task is not a submission to reconcile", code="invalid_operation")
        self._validate_binding(record)
        if record.remote_id is not None:
            if record.remote_id != remote_id:
                raise ConflictError("task is already bound to a different remote id")
            return self._public(record)
        if record.state not in {"pending", "submitting", "submission_uncertain"}:
            raise ConflictError("task is not eligible for reconciliation")
        entity = self.client.get_task(record.kind, remote_id)
        marker = entity.get("submissionMarker") if isinstance(entity, Mapping) else None
        legacy_match = (
            record.name is None
            and record.description is None
            and any(
                self._legacy_description_marker(
                    description, record.submission_marker
                )
                for description in self._entity_descriptions(entity)
            )
        )
        if marker != record.submission_marker and not legacy_match:
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
        if record.origin == "adopted":
            cluster_id, user = self._remote_account()
            if cluster_id != record.remote_cluster_id:
                raise ConflictError("task belongs to a different remote cluster", code="binding_mismatch")
            if user["id"] != record.remote_user_id:
                raise ConflictError("task belongs to a different authenticated account", code="ownership_mismatch")
            return
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
        environment = result.get("environment")
        if not isinstance(environment, Mapping):
            raise ValidationError("config environment must be an object")
        environment = copy.deepcopy(dict(environment))
        variables = environment.get("environment_variables")
        if not isinstance(variables, list) or not all(
            isinstance(item, str) for item in variables
        ):
            raise ValidationError(
                "config environment.environment_variables must be a list of strings"
            )
        for item in variables:
            if item.split("=", 1)[0] == _SUBMISSION_MARKER_VARIABLE:
                raise ValidationError(
                    f"{_SUBMISSION_MARKER_VARIABLE} is reserved for task identity"
                )
        environment["environment_variables"] = list(variables) + [
            f"{_SUBMISSION_MARKER_VARIABLE}={marker}"
        ]
        result["environment"] = environment
        return result

    @staticmethod
    def _legacy_description_marker(
        description: Optional[str], marker: str
    ) -> bool:
        if not description:
            return False
        # Backward compatibility only: pre-metadata jobs used the first line.
        return description.splitlines()[0] == marker

    @staticmethod
    def _entity_descriptions(entity: Any) -> Sequence[str]:
        if not isinstance(entity, Mapping):
            return ()
        candidates: Sequence[Any] = (
            entity.get("description"),
            entity.get("config", {}).get("description")
            if isinstance(entity.get("config"), Mapping)
            else None,
        )
        return tuple(candidate for candidate in candidates if isinstance(candidate, str))


__all__ = ["ComputeService"]
