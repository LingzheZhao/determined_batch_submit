from pathlib import Path

import pytest

from determined_batch.core.api_client import SubmissionUncertainError
from determined_batch.submission import submit_directory, submit_experiment


class FakeClient:
    def __init__(self, response=None):
        self.response = response or {"experiment": {"id": 12}}
        self.project_checks = 0
        self.creates = 0

    def get_project(self, workspace, project):
        self.project_checks += 1
        return {"id": 1}

    def create_experiment(self, **kwargs):
        self.creates += 1
        self.kwargs = kwargs
        return self.response


def write_config(path: Path, text="name: test\n"):
    path.write_text(text, encoding="utf-8")


def test_dry_run_is_offline_and_checks_yaml(tmp_path, monkeypatch):
    write_config(tmp_path / "ok.yaml")
    write_config(tmp_path / "bad.yml", "- not\n- a mapping\n")

    def should_not_construct():
        raise AssertionError("dry run constructed a network client")

    monkeypatch.setattr("determined_batch.submission.DeterminedAPIClient", should_not_construct)
    results = submit_directory(tmp_path, dry_run=True)
    assert {item["config"]: item["outcome"] for item in results} == {
        "bad.yml": "failed",
        "ok.yaml": "dry-run",
    }


def test_validate_does_not_check_or_create_project(tmp_path):
    path = tmp_path / "config.yaml"
    write_config(path, "workspace: w\nproject: p\nname: test\n")
    fake = FakeClient(response={"config": {}})
    assert submit_experiment(path, client=fake, validate_only=True) == (True, None, None)
    assert fake.project_checks == 0
    assert fake.creates == 1
    assert fake.kwargs["validate_only"] is True


def test_project_root_rejected_before_client_construction(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    write_config(path)
    monkeypatch.setattr(
        "determined_batch.submission.DeterminedAPIClient",
        lambda: (_ for _ in ()).throw(AssertionError("constructed")),
    )
    success, _, error = submit_experiment(path, project_root=tmp_path)
    assert success is False
    assert "uploads are not supported" in error


def test_missing_launch_id_is_unknown_outcome(tmp_path):
    path = tmp_path / "config.yaml"
    write_config(path)
    with pytest.raises(SubmissionUncertainError):
        submit_experiment(path, client=FakeClient(response={"config": {}}), ensure_project=False)
