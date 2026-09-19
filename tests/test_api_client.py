import json

import pytest
import requests

from determined_batch.core.api_client import (
    APIError,
    DeterminedAPIClient,
    SubmissionUncertainError,
    _normalize_api_url,
)
from determined_batch.domain.experiment import Experiment, ExperimentState


class Response:
    def __init__(self, payload=None, status=200, lines=None):
        self.payload = payload
        self.status_code = status
        self.text = "" if payload is None else json.dumps(payload)
        self.content = self.text.encode()
        self.reason = "error"
        self.lines = lines or []
        self.closed = False

    def json(self):
        return self.payload

    def iter_lines(self, decode_unicode=True):
        yield from self.lines

    def close(self):
        self.closed = True


def client():
    return DeterminedAPIClient("master:8080", api_token="token")


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://cluster.example.org", "https://cluster.example.org"),
        ("host:8443", "http://host:8443"),
        ("[::1]", "http://[::1]:8080"),
        ("::1", "http://[::1]:8080"),
    ],
)
def test_url_normalization(given, expected):
    assert _normalize_api_url(given) == expected


def test_master_and_token_can_come_from_secret_file(tmp_path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text("DET_MASTER=https://cluster.example.org\nDET_API_TOKEN=secret-token\n")
    resolved = DeterminedAPIClient(secrets_path=secrets)
    assert resolved.api_url == "https://cluster.example.org"
    assert resolved.api_token == "secret-token"


def test_environment_master_precedes_secret_file(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets.env"
    secrets.write_text("DET_MASTER=https://secret.example\nDET_API_TOKEN=secret-token\n")
    monkeypatch.setenv("DET_MASTER", "https://environment.example")
    resolved = DeterminedAPIClient(secrets_path=secrets)
    assert resolved.api_url == "https://environment.example"


def test_queued_experiment_state_uses_schema_spelling():
    experiment = Experiment.from_api_data({"id": 1, "state": "STATE_QUEUED"})
    assert experiment.state is ExperimentState.QUEUED
    assert experiment.is_active()


def test_api_error_fields_and_mutation_uncertainty(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response({"message": "no"}, 403))
    with pytest.raises(APIError) as caught:
        client().get_task("command", "c1")
    assert caught.value.code == 403
    assert caught.value.retryable is False

    monkeypatch.setattr(requests, "post", lambda *a, **k: Response({"message": "proxy"}, 502))
    with pytest.raises(SubmissionUncertainError) as caught:
        client().launch_task("command", {"entrypoint": ["true"]})
    assert caught.value.code == "submission_uncertain"
    assert caught.value.retryable is False


def test_transport_read_is_retryable_but_mutation_is_uncertain(monkeypatch):
    def fail(*args, **kwargs):
        raise requests.ConnectionError("disconnected")

    monkeypatch.setattr(requests, "get", fail)
    with pytest.raises(APIError) as caught:
        client().get_task("command", "c1")
    assert caught.value.retryable is True

    monkeypatch.setattr(requests, "post", fail)
    with pytest.raises(SubmissionUncertainError):
        client().launch_task("command", {"entrypoint": ["true"]})


def test_validation_transport_failure_is_retryable_not_uncertain(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("name: validate\n")

    def fail(*args, **kwargs):
        raise requests.ReadTimeout("disconnected")

    monkeypatch.setattr(requests, "post", fail)
    with pytest.raises(APIError) as caught:
        client().create_experiment(config, validate_only=True)
    assert not isinstance(caught.value, SubmissionUncertainError)
    assert caught.value.retryable is True


def test_launch_payloads_and_shell_secret_removal(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        return Response({"shell": {"id": "s1", "privateKey": "secret", "state": "RUNNING"}})

    monkeypatch.setattr(requests, "post", post)
    result = client().launch_task("shell", {"description": "debug"})
    assert calls == [("http://master:8080/api/v1/shells", {"config": {"description": "debug"}})]
    assert "privateKey" not in result
    assert result["reconnectCommand"] == "det shell show_ssh_command s1"


def test_get_task_preserves_safe_config_for_identity_check(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: Response({
            "command": {"id": "c1"},
            "config": {
                "description": "marker\nhuman text",
                "entrypoint": ["true"],
                "environment_variables": ["PASSWORD=do-not-persist"],
                "api_token": "do-not-persist",
            },
        }),
    )
    task = client().get_task("command", "c1")
    assert task["config"]["description"].startswith("marker")
    assert task["config"]["entrypoint"] == ["true"]
    assert task["config"]["environment_variables"] == "[redacted]"
    assert "api_token" not in task["config"]


def test_redaction_covers_secret_aliases_without_masking_innocent_tokens():
    redacted = client()._redact_secrets({
        "wandb_api_key": "secret",
        "Authorization": "Bearer secret",
        "service-credential": "secret",
        "private-key": "secret",
        "session_key": "secret",
        "cookies": "secret",
        "passwd": "secret",
        "tokenizer": "bert",
        "context_tokens": 4096,
    })
    assert redacted == {"tokenizer": "bert", "context_tokens": 4096}


def test_cancel_unwraps_command_entity(monkeypatch):
    monkeypatch.setattr(
        requests,
        "post",
        lambda *a, **k: Response({"command": {"id": "c1", "state": "STATE_TERMINATED"}}),
    )
    assert client().cancel_task("command", "c1") == {
        "id": "c1",
        "state": "STATE_TERMINATED",
    }


def test_experiment_cancel_preserves_empty_response_acknowledgement(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response())
    assert client().cancel_task("experiment", "17") == {
        "id": "17",
        "acknowledged": True,
    }


@pytest.mark.parametrize("field", ["files", "data", "context", "project_root", "modelDefinition"])
def test_launch_rejects_upload_aliases(field):
    with pytest.raises(ValueError, match="uploads are not supported"):
        client().launch_task("command", {field: "anything"})


def test_launch_rejects_nested_normalized_upload_alias():
    with pytest.raises(ValueError, match=r"config\.nested\.model-definition"):
        client().launch_task("experiment", {"nested": {"model-definition": []}})


def test_get_experiment_unwrap_and_true_tail(monkeypatch):
    requested = []
    responses = {
        "/api/v1/experiments/9": Response({"experiment": {"id": 9, "state": "STATE_RUNNING"}}),
        "/api/v1/experiments/9/trials": Response({"trials": [{"id": 2}, {"id": 17}]}),
        "/api/v1/trials/17/logs": Response(
            lines=[
                json.dumps({"result": {"message": "new"}}),
                json.dumps({"result": {"message": "old"}}),
            ]
        ),
    }

    def get(url, **kwargs):
        key = url.removeprefix("http://master:8080")
        requested.append((key, kwargs.get("params")))
        return responses[key]

    monkeypatch.setattr(requests, "get", get)
    assert client().get_task("experiment", "9")["id"] == 9
    assert [item["message"] for item in client().task_logs("experiment", "9", tail=2)] == ["old", "new"]
    assert requested[-1] == (
        "/api/v1/trials/17/logs",
        {"limit": 2, "follow": False, "orderBy": "ORDER_BY_DESC"},
    )
    assert responses["/api/v1/trials/17/logs"].closed is True


def test_multi_pool_slot_membership(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: Response(
            {"agents": [{"id": "a", "resourcePools": ["p1", "p2"], "slots": {"0": {"id": "0"}}}]}
        ),
    )
    slots = client().get_slots()
    assert [(slot["slot_id"], slot["resource_pool"]) for slot in slots] == [("0", "p1"), ("0", "p2")]
