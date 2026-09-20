import json

import pytest
import requests

from determined_compute.core.api_client import (
    APIError,
    DeterminedAPIClient,
    SubmissionUncertainError,
    _normalize_api_url,
)


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
                "environment_variables": [
                    "PASSWORD=do-not-persist",
                    "COMPUTE_SUBMISSION_MARKER="
                    "determined-compute:11111111-1111-1111-1111-111111111111",
                ],
                "api_token": "do-not-persist",
            },
        }),
    )
    task = client().get_task("command", "c1")
    assert task["config"]["description"].startswith("marker")
    assert task["config"]["entrypoint"] == ["true"]
    assert task["config"]["environment_variables"] == "[redacted]"
    assert "api_token" not in task["config"]
    assert task["submissionMarker"] == (
        "determined-compute:11111111-1111-1111-1111-111111111111"
    )


@pytest.mark.parametrize(
    "environment_variables",
    [
        [
            "SAFE=value",
            "COMPUTE_SUBMISSION_MARKER=determined-compute:22222222-2222-2222-2222-222222222222",
        ],
        {
            "cpu": [
                "COMPUTE_SUBMISSION_MARKER=determined-compute:22222222-2222-2222-2222-222222222222"
            ],
            "cuda": ["SAFE=value"],
        },
        {
            "cpu": {
                "COMPUTE_SUBMISSION_MARKER": (
                    "determined-compute:22222222-2222-2222-2222-222222222222"
                )
            }
        },
    ],
)
def test_get_task_extracts_only_safe_marker_before_environment_redaction(
    monkeypatch, environment_variables
):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: Response(
            {
                "command": {"id": "c1"},
                "config": {
                    "description": "human task description",
                    "environment": {
                        "environment_variables": environment_variables,
                    },
                },
            }
        ),
    )

    task = client().get_task("command", "c1")

    assert task["submissionMarker"] == (
        "determined-compute:22222222-2222-2222-2222-222222222222"
    )
    assert task["config"]["description"] == "human task description"
    assert task["config"]["environment"]["environment_variables"] == "[redacted]"


def test_get_task_rejects_malformed_marker_metadata(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: Response(
            {
                "command": {
                    "id": "c1",
                    "submissionMarker": (
                        "determined-compute:33333333-3333-3333-3333-333333333333"
                    ),
                    "environmentVariables": ["TOKEN=raw-entity-secret"],
                },
                "config": {
                    "environment": {
                        "environment_variables": [
                            "COMPUTE_SUBMISSION_MARKER=not-a-safe-marker",
                            "TOKEN=secret",
                        ]
                    }
                },
            }
        ),
    )

    task = client().get_task("command", "c1")

    assert "submissionMarker" not in task
    assert task["environmentVariables"] == "[redacted]"
    assert task["config"]["environment"]["environment_variables"] == "[redacted]"


def test_get_task_extracts_marker_from_yaml_experiment_config(monkeypatch):
    import yaml

    config = yaml.safe_dump(
        {
            "name": "human experiment",
            "environment": {
                "environment_variables": {
                    "cuda": [
                        "COMPUTE_SUBMISSION_MARKER="
                        "determined-compute:44444444-4444-4444-4444-444444444444"
                    ]
                }
            },
        }
    )
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: Response({"experiment": {"id": "e1"}, "config": config}),
    )

    task = client().get_task("experiment", "e1")

    assert task["submissionMarker"] == (
        "determined-compute:44444444-4444-4444-4444-444444444444"
    )
    assert task["config"]["name"] == "human experiment"
    assert task["config"]["environment"]["environment_variables"] == "[redacted]"


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
    messages = [
        item["message"] for item in client().task_logs("experiment", "9", tail=2)
    ]
    assert messages == ["old", "new"]
    assert requested[-1] == (
        "/api/v1/trials/17/logs",
        {"limit": 2, "follow": False, "orderBy": "ORDER_BY_DESC"},
    )
    assert responses["/api/v1/trials/17/logs"].closed is True


def test_experiment_launch_sends_only_yaml_config_and_activation(monkeypatch):
    import yaml
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs['json']))
        return Response({'experiment': {'id': 12}})
    monkeypatch.setattr(requests, 'post', post)
    config = {'name': 'example', 'entrypoint': 'python train.py',
              'bind_mounts': [{'host_path': '/SSD', 'container_path': '/SSD'}]}
    assert client().launch_task('experiment', config)['id'] == 12
    url, payload = calls[0]
    assert url.endswith('/api/v1/experiments')
    assert set(payload) == {'config', 'activate'}
    assert payload['activate'] is True
    assert yaml.safe_load(payload['config']) == config


def test_shell_cancel_unwraps_response_and_removes_private_key(monkeypatch):
    monkeypatch.setattr(requests, 'post', lambda *a, **kw: Response({
        'shell': {'id': 's1', 'state': 'STATE_TERMINATED', 'privateKey': 'fixture-secret'},
    }))
    result = client().cancel_task('shell', 's1')
    assert result['id'] == 's1'
    assert result['state'] == 'STATE_TERMINATED'
    assert 'privateKey' not in result
    assert result['reconnectCommand'] == 'det shell show_ssh_command s1'


def test_get_current_user_normalizes_positive_id_and_returns_only_identity(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs.get("params")))
        return Response(
            {
                "user": {
                    "id": "0007",
                    "username": " alice ",
                    "password": "must-not-leak",
                    "admin": True,
                }
            }
        )

    monkeypatch.setattr(requests, "get", get)
    assert client().get_current_user() == {"id": "7", "username": "alice"}
    assert calls == [("http://master:8080/api/v1/me", None)]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"user": None},
        {"user": {"id": 0, "username": "alice"}},
        {"user": {"id": True, "username": "alice"}},
        {"user": {"id": " 7", "username": "alice"}},
        {"user": {"id": 7, "username": ""}},
        {"user": {"id": 7, "username": 8}},
    ],
)
def test_get_current_user_rejects_malformed_response_without_echo(monkeypatch, payload):
    payload["raw_secret"] = "do-not-echo"
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response(payload))
    with pytest.raises(APIError) as caught:
        client().get_current_user()
    assert caught.value.code == "invalid_response"
    assert "do-not-echo" not in str(caught.value)
    assert caught.value.details is None


def test_get_cluster_id_uses_root_info_cluster_id(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return Response({"cluster_id": " cluster-123 ", "master_id": "wrong-value"})

    monkeypatch.setattr(requests, "get", get)
    assert client().get_cluster_id() == "cluster-123"
    assert calls == ["http://master:8080/info"]


@pytest.mark.parametrize(
    "payload",
    [
        {"master_id": "not-the-cluster-id"},
        {"cluster_id": ""},
        {"cluster_id": 123},
        {"cluster_id": "x" * 257},
    ],
)
def test_get_cluster_id_rejects_malformed_response(monkeypatch, payload):
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response(payload))
    with pytest.raises(APIError) as caught:
        client().get_cluster_id()
    assert caught.value.code == "invalid_response"
    assert caught.value.details is None


@pytest.mark.parametrize("kind", ["command", "shell", "experiment"])
def test_list_remote_tasks_filters_pages_and_redacts(kind, monkeypatch):
    calls = []
    collection_key = f"{kind}s"

    def get(url, **kwargs):
        calls.append((url, kwargs.get("params")))
        return Response(
            {
                collection_key: [
                    {
                        "id": 19,
                        "userId": "7",
                        "username": "alice",
                        "name": "native-name",
                        "displayName": "display",
                        "description": "safe description",
                        "state": "STATE_RUNNING",
                        "resourcePool": "gpu",
                        "startTime": "2026-09-20T00:00:00Z",
                        "endTime": None,
                        "config": {"environment": {"TOKEN": "secret"}},
                        "originalConfig": "secret config",
                        "privateKey": "secret key",
                        "environment": {"PASSWORD": "secret"},
                        "hyperparameters": {"secret": "value"},
                    },
                    {"id": "native-string-id", "state": "STATE_COMPLETED"},
                ],
                "pagination": {
                    "limit": 25,
                    "offset": 5,
                    "startIndex": 5,
                    "endIndex": 7,
                    "total": 42,
                    "ignored": "value",
                },
            }
        )

    monkeypatch.setattr(requests, "get", get)
    result = client().list_remote_tasks(kind, user_id="007", limit=25, offset=5)

    assert calls == [
        (
            f"http://master:8080/api/v1/{kind}s",
            {
                "userIds": [7],
                "limit": 25,
                "offset": 5,
                "orderBy": "ORDER_BY_DESC",
                "sortBy": "SORT_BY_START_TIME",
            },
        )
    ]
    assert result["pagination"] == {
        "limit": 25,
        "offset": 5,
        "startIndex": 5,
        "endIndex": 7,
        "total": 42,
    }
    assert result["tasks"][0] == {
        "id": 19,
        "userId": "7",
        "username": "alice",
        "name": "native-name",
        "displayName": "display",
        "description": "safe description",
        "state": "STATE_RUNNING",
        "resourcePool": "gpu",
        "startTime": "2026-09-20T00:00:00Z",
        "endTime": None,
    }
    assert result["tasks"][1] == {
        "id": "native-string-id",
        "state": "STATE_COMPLETED",
    }
    encoded = json.dumps(result)
    for forbidden in ("config", "privateKey", "environment", "hyperparameters", "secret"):
        assert forbidden not in encoded


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"kind": "commands", "user_id": "7"}, "kind must be"),
        ({"kind": "command", "user_id": "0"}, "positive numeric"),
        ({"kind": "command", "user_id": "7", "limit": 0}, "between 1 and 100"),
        ({"kind": "command", "user_id": "7", "limit": True}, "between 1 and 100"),
        ({"kind": "command", "user_id": "7", "offset": -1}, "non-negative"),
        ({"kind": "command", "user_id": "7", "offset": False}, "non-negative"),
    ],
)
def test_list_remote_tasks_validates_inputs(kwargs, message):
    kind = kwargs.pop("kind")
    with pytest.raises(ValueError, match=message):
        client().list_remote_tasks(kind, **kwargs)


@pytest.mark.parametrize(
    "payload",
    [
        {"commands": {}, "pagination": {}},
        {"commands": ["not-an-object"], "pagination": {}},
        {
            "commands": [{"id": "task", "description": {"privateKey": "secret"}}],
            "pagination": {
                "limit": 50,
                "offset": 0,
                "startIndex": 0,
                "endIndex": 1,
                "total": 1,
            },
        },
        {"commands": [], "pagination": None},
        {
            "commands": [],
            "pagination": {
                "limit": 50,
                "offset": 0,
                "startIndex": 0,
                "endIndex": 0,
            },
        },
        {
            "commands": [],
            "pagination": {
                "limit": True,
                "offset": 0,
                "startIndex": 0,
                "endIndex": 0,
                "total": 0,
            },
        },
    ],
)
def test_list_remote_tasks_rejects_malformed_pages_without_echo(monkeypatch, payload):
    payload["raw_secret"] = "do-not-echo"
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response(payload))
    with pytest.raises(APIError) as caught:
        client().list_remote_tasks("command", user_id="7")
    assert caught.value.code == "invalid_response"
    assert "do-not-echo" not in str(caught.value)
    assert caught.value.details is None


def test_remote_discovery_does_not_swallow_authentication_error(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: Response({"message": "denied"}, status=401),
    )
    with pytest.raises(APIError) as caught:
        client().list_remote_tasks("command", user_id="7")
    assert caught.value.code == 401
    assert caught.value.retryable is False
