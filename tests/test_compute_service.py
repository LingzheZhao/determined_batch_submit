from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from determined_batch.compute import (
    ComputeProfile,
    ComputeService,
    ConflictError,
    NotFoundError,
    SQLiteTaskStore,
    SubmissionUncertainError,
    ValidationError,
)


class FakeClient:
    api_url = "https://det.example.test"

    def __init__(self, delay=0.0):
        self.delay = delay
        self.launches = []
        self.gets = []
        self.log_calls = []
        self.cancel_calls = []
        self._lock = threading.Lock()
        self.entities = {}

    def launch_task(self, kind, config):
        with self._lock:
            self.launches.append((kind, config))
            remote_id = str(len(self.launches))
        if self.delay:
            time.sleep(self.delay)
        entity = {
            "id": remote_id,
            "state": "RUNNING",
            "config": config,
        }
        self.entities[(kind, remote_id)] = entity
        return entity

    def get_task(self, kind, remote_id):
        self.gets.append((kind, remote_id))
        return self.entities[(kind, remote_id)]

    def task_logs(self, kind, remote_id, tail):
        self.log_calls.append((kind, remote_id, tail))
        return [{"message": "ok"}]

    def cancel_task(self, kind, remote_id):
        self.cancel_calls.append((kind, remote_id))
        return {"id": remote_id, "state": "TERMINATING", "exitCode": None}


class UncertainClient(FakeClient):
    def launch_task(self, kind, config):
        self.launches.append((kind, config))
        raise SubmissionUncertainError("connection closed after request")


class EmptyCancelClient(FakeClient):
    def cancel_task(self, kind, remote_id):
        self.cancel_calls.append((kind, remote_id))
        return {}


@pytest.fixture
def profile():
    return ComputeProfile.from_dict(
        {
            "mounts": [
                {"host_path": "/shared/host", "container_path": "/shared/container"}
            ],
            "defaults": {"image": "registry/image:stable", "pool": "gpu", "slots": 1},
            "shell_inactivity_seconds": 7200,
            "cluster_identity": "test-cluster",
        }
    )


@pytest.fixture
def command_request():
    return {
        "kind": "auto",
        "command": ["python", "train.py", "--name", "space value"],
        "workdir": "/shared/container/jobs/code",
        "output_dir": "/shared/container/jobs/out",
        "code_revision": "git:abc123-dirty=false",
    }


def test_plan_is_offline_and_builds_shared_storage_command(tmp_path, profile, command_request):
    client = FakeClient()
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)

    plan = service.plan(command_request)

    assert client.launches == []
    assert client.gets == []
    assert plan["kind"] == "command"
    assert plan["code_revision"] == "git:abc123-dirty=false"
    assert plan["config"]["resources"] == {"slots": 1, "resource_pool": "gpu"}
    assert plan["config"]["bind_mounts"] == [
        {"host_path": "/shared/host", "container_path": "/shared/container"}
    ]
    assert plan["config"]["entrypoint"] == [
        "/bin/bash",
        "-lc",
        "mkdir -p /shared/container/jobs/out && cd /shared/container/jobs/code && "
        "python train.py --name 'space value'",
    ]


def test_auto_modes_and_shell_advisory(tmp_path, profile, command_request):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    shell = dict(command_request)
    shell.pop("command")
    shell.update({"interactive": True, "overnight": True})

    plan = service.plan(shell)

    assert plan["kind"] == "shell"
    assert "entrypoint" not in plan["config"]
    assert plan["config"]["resources"]["slots"] == 1
    assert {item["code"] for item in plan["advisories"]} == {
        "overnight_experiment_recommended",
        "shell_inactivity_policy",
    }


def test_command_and_shell_use_native_entrypoint_shapes(
    tmp_path, profile, command_request
):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)

    command = service.plan(command_request)
    shell_request = dict(command_request)
    shell_request.update({"kind": "shell", "interactive": True})
    shell_request.pop("command")
    shell = service.plan(shell_request)

    assert command["config"]["entrypoint"][:2] == ["/bin/bash", "-lc"]
    assert isinstance(command["config"]["entrypoint"][2], str)
    assert "entrypoint" not in shell["config"]


def test_overnight_command_becomes_experiment(tmp_path, profile, command_request):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    request = dict(command_request, overnight=True)

    plan = service.plan(request)

    assert plan["kind"] == "experiment"
    assert plan["config"]["resources"]["slots_per_trial"] == 1
    assert plan["config"]["entrypoint"].endswith("python train.py --name 'space value'")


@pytest.mark.parametrize(
    "change",
    [
        {"workdir": "/local/code"},
        {"output_dir": "/shared/container/jobs/../escape"},
        {"workdir": "shared/container/code"},
        {"upload_context": "/tmp/context"},
        {"experiment_config": {"files": [{"path": "secret"}]}},
        {"experiment_config": {"context_path": "/tmp/upload"}},
        {"experiment_config": {"bind_mounts": []}},
    ],
)
def test_rejects_unmapped_paths_and_upload_or_mount_fields(
    tmp_path, profile, command_request, change
):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    request = dict(command_request)
    request.update(change)
    if "experiment_config" in change:
        request.update({"kind": "experiment", "command": None})

    with pytest.raises(ValidationError):
        service.plan(request)


def test_context_named_hyperparameters_are_allowed(tmp_path, profile):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    request = {
        "kind": "experiment",
        "workdir": "/shared/container/code",
        "output_dir": "/shared/container/out",
        "experiment_config": {
            "entrypoint": "python train.py",
            "hyperparameters": {"context_length": 8192, "model_context_window": 16384},
        },
    }

    plan = service.plan(request)

    assert plan["config"]["hyperparameters"]["context_length"] == 8192


def test_concurrent_duplicate_launch_calls_remote_once(tmp_path, profile, command_request):
    client = FakeClient(delay=0.15)
    db_path = tmp_path / "tasks.db"
    services = [
        ComputeService(client, SQLiteTaskStore(db_path), profile) for _ in range(8)
    ]

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(
                service.launch, command_request, "same-request", "session-a"
            )
            for service in services
        ]
        results = [future.result() for future in futures]

    assert len(client.launches) == 1
    assert len({result["task_id"] for result in results}) == 1
    assert {result["state"] for result in results}.issubset({"pending", "submitting", "submitted"})
    assert services[0].list_tasks("session-a")[0]["state"] == "submitted"


def test_request_id_payload_conflict(tmp_path, profile, command_request):
    service = ComputeService(FakeClient(), SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    service.launch(command_request, "request-1", "session-a")

    with pytest.raises(ConflictError) as caught:
        service.launch(dict(command_request, command="echo changed"), "request-1", "session-a")

    assert caught.value.code == "idempotency_conflict"


def test_uncertain_submission_is_recorded_and_never_retried(
    tmp_path, profile, command_request
):
    client = UncertainClient()
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)

    with pytest.raises(SubmissionUncertainError) as caught:
        service.launch(command_request, "uncertain-1", "session-a")

    task_id = caught.value.details["task_id"]
    assert caught.value.code == "submission_uncertain"
    assert service.status(task_id, "session-a")["state"] == "submission_uncertain"
    duplicate = service.launch(command_request, "uncertain-1", "session-a")
    assert duplicate["task_id"] == task_id
    assert duplicate["state"] == "submission_uncertain"
    assert len(client.launches) == 1


@pytest.mark.parametrize("initial_state", ["pending", "submitting"])
def test_stale_crash_record_transitions_to_uncertain_without_resubmit(
    tmp_path, profile, command_request, initial_state
):
    client = FakeClient()
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    service = ComputeService(client, store, profile, submission_stale_seconds=0)
    plan = service.plan(command_request)
    record, created = store.claim(
        request_id=f"crash-{initial_state}",
        owner="session-a",
        payload_hash=service._payload_hash(plan),
        profile_hash=profile.fingerprint,
        kind=plan["kind"],
        code_revision=plan["code_revision"],
        workdir=command_request["workdir"],
        output_dir=command_request["output_dir"],
        cluster_identity=service._cluster_identity(),
    )
    assert created
    if initial_state == "submitting":
        store.mark_submitting(record.task_id)

    recovered = service.status(record.task_id, "session-a")

    assert recovered["state"] == "submission_uncertain"
    assert recovered["recovery"]["action"] == "reconcile"
    assert recovered["recovery"]["safe_to_resubmit"] is False
    assert client.launches == []
    assert client.gets == []


def test_stale_after_dispatch_can_only_bind_by_verified_reconcile(
    tmp_path, profile, command_request
):
    client = FakeClient()
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    service = ComputeService(client, store, profile, submission_stale_seconds=0)
    plan = service.plan(command_request)
    record, _ = store.claim(
        request_id="crash-after-dispatch",
        owner="session-a",
        payload_hash=service._payload_hash(plan),
        profile_hash=profile.fingerprint,
        kind=plan["kind"],
        code_revision=plan["code_revision"],
        workdir=command_request["workdir"],
        output_dir=command_request["output_dir"],
        cluster_identity=service._cluster_identity(),
    )
    store.mark_submitting(record.task_id)
    client.entities[("command", "remote-after-crash")] = {
        "id": "remote-after-crash",
        "state": "RUNNING",
        "config": {"description": record.submission_marker},
    }

    assert service.status(record.task_id, "session-a")["state"] == "submission_uncertain"
    reconciled = service.reconcile(record.task_id, "session-a", "remote-after-crash")

    assert reconciled["remote_id"] == "remote-after-crash"
    assert reconciled["remote_state"] == "RUNNING"
    assert client.launches == []


def test_restart_preserves_task_and_owner_scope(tmp_path, profile, command_request):
    db_path = tmp_path / "tasks.db"
    first_store = SQLiteTaskStore(db_path)
    task = ComputeService(FakeClient(), first_store, profile).launch(
        command_request, "request-1", "session-a"
    )
    first_store.close()

    second_client = FakeClient()
    service = ComputeService(second_client, SQLiteTaskStore(db_path), profile)
    assert service.list_tasks("session-a")[0]["task_id"] == task["task_id"]
    assert service.list_tasks("session-b") == []
    with pytest.raises(NotFoundError):
        service.status(task["task_id"], "session-b")
    assert second_client.gets == []


def test_status_logs_cancel_preserve_remote_evidence(tmp_path, profile, command_request):
    client = FakeClient()
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    launched = service.launch(command_request, "request-1", "session-a")
    remote_id = launched["remote_id"]
    client.entities[("command", remote_id)] = {
        "id": remote_id,
        "state": "TERMINATED",
        "exitStatus": 23,
        "failureReason": "process failed",
    }

    status = service.status(launched["task_id"], "session-a")

    assert status["remote_state"] == "TERMINATED"
    assert status["remote"]["exitStatus"] == 23
    assert status["remote"]["failureReason"] == "process failed"
    assert service.logs(launched["task_id"], "session-a", 10) == [{"message": "ok"}]
    cancelled = service.cancel(launched["task_id"], "session-a")
    assert cancelled["remote"]["state"] == "TERMINATING"


def test_empty_cancel_ack_preserves_last_observed_remote_state(
    tmp_path, profile, command_request
):
    client = EmptyCancelClient()
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    launched = service.launch(command_request, "request-1", "session-a")
    remote_id = launched["remote_id"]
    client.entities[("command", remote_id)] = {"id": remote_id, "state": "RUNNING"}
    service.status(launched["task_id"], "session-a")

    cancelled = service.cancel(launched["task_id"], "session-a")

    assert cancelled["cancellation_acknowledged"] is True
    assert cancelled["remote_state"] == "RUNNING"
    assert cancelled["remote"] == {}


def test_changed_profile_or_cluster_cannot_query_bound_remote(
    tmp_path, profile, command_request
):
    client = FakeClient()
    db_path = tmp_path / "tasks.db"
    task = ComputeService(client, SQLiteTaskStore(db_path), profile).launch(
        command_request, "request-1", "session-a"
    )
    changed = ComputeProfile.from_dict(
        {
            "mounts": [
                {"host_path": "/shared/host", "container_path": "/shared/container"}
            ],
            "defaults": {"image": "other/image", "pool": "gpu", "slots": 1},
            "cluster_identity": "other-cluster",
        }
    )
    changed_client = FakeClient()
    service = ComputeService(changed_client, SQLiteTaskStore(db_path), changed)

    with pytest.raises(ConflictError) as caught:
        service.status(task["task_id"], "session-a")

    assert caught.value.code == "binding_mismatch"
    assert changed_client.gets == []


def test_same_cluster_label_with_changed_api_url_cannot_query_bound_remote(
    tmp_path, profile, command_request
):
    db_path = tmp_path / "tasks.db"
    first_client = FakeClient()
    task = ComputeService(first_client, SQLiteTaskStore(db_path), profile).launch(
        command_request, "request-1", "session-a"
    )
    changed_client = FakeClient()
    changed_client.api_url = "https://different-det.example.test"
    service = ComputeService(changed_client, SQLiteTaskStore(db_path), profile)

    with pytest.raises(ConflictError) as caught:
        service.status(task["task_id"], "session-a")

    assert caught.value.code == "binding_mismatch"
    assert changed_client.gets == []


def test_reconcile_requires_remote_identity_marker(tmp_path, profile, command_request):
    client = UncertainClient()
    service = ComputeService(client, SQLiteTaskStore(tmp_path / "tasks.db"), profile)
    with pytest.raises(SubmissionUncertainError) as caught:
        service.launch(command_request, "request-1", "session-a")
    task_id = caught.value.details["task_id"]
    marker = client.launches[0][1]["description"].split("\n", 1)[0]
    client.entities[("command", "remote-9")] = {
        "id": "remote-9",
        "state": "RUNNING",
        "config": {"description": "wrong marker"},
    }

    with pytest.raises(ConflictError):
        service.reconcile(task_id, "session-a", "remote-9")

    client.entities[("command", "remote-9")]["config"]["description"] = marker
    reconciled = service.reconcile(task_id, "session-a", "remote-9")
    assert reconciled["remote_id"] == "remote-9"


def test_secret_request_values_are_not_persisted(tmp_path, profile):
    db_path = tmp_path / "tasks.db"
    secret = "SUPER_SECRET_VALUE_6d0b"
    request = {
        "kind": "experiment",
        "workdir": "/shared/container/code",
        "output_dir": "/shared/container/out",
        "experiment_config": {
            "entrypoint": "python train.py",
            "environment": {"environment_variables": [f"TOKEN={secret}"]},
        },
    }
    service = ComputeService(FakeClient(), SQLiteTaskStore(db_path), profile)

    service.launch(request, "request-1", "session-a")

    assert secret.encode() not in db_path.read_bytes()


def test_profile_json_and_yaml(tmp_path):
    value = {
        "mounts": [{"host_path": "/host", "container_path": "/container"}],
        "defaults": {"image": "image", "pool": "pool", "slots": 0},
    }
    json_path = tmp_path / "profile.json"
    yaml_path = tmp_path / "profile.yaml"
    json_path.write_text(json.dumps(value), encoding="utf-8")
    yaml_path.write_text(
        "mounts:\n  - host_path: /host\n    container_path: /container\n"
        "defaults:\n  image: image\n  pool: pool\n  slots: 0\n",
        encoding="utf-8",
    )

    assert ComputeProfile.from_file(json_path) == ComputeProfile.from_file(yaml_path)
