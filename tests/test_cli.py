from __future__ import annotations

import json

from determined_batch import cli
from determined_batch.core.api_client import APIError


def test_legacy_cli_reports_api_error_as_json(monkeypatch, capsys):
    def fail(_args):
        raise APIError(
            "master unavailable",
            code="transport_error",
            details={"endpoint": "api/v1/experiments"},
            retryable=True,
        )

    monkeypatch.setattr(cli, "_cmd_experiments", fail)
    parser = cli.build_parser()
    args = parser.parse_args(["--json", "experiments"])
    args.func = fail
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    monkeypatch.setattr(parser, "parse_args", lambda _argv: args)

    assert cli.main([]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": {
            "code": "transport_error",
            "message": "master unavailable",
            "retryable": True,
        },
    }


def test_list_pools_renders_unknown_capacity(monkeypatch, capsys):
    class Pool:
        name = "gpu-pool"
        capacity_known = False

    class Service:
        def __init__(self, _client):
            pass

        def get_all_pools(self, force_refresh=False):
            return [Pool()]

    monkeypatch.setattr(cli, "_build_client", lambda _args: object())
    monkeypatch.setattr(cli, "ResourcePoolService", Service)
    args = cli.build_parser().parse_args(["list-pools"])

    assert args.func(args) == 0
    output = capsys.readouterr().out
    assert "gpu-pool" in output
    assert "free:  ?" in output


def test_submit_dir_dry_run_does_not_construct_api_client(tmp_path, monkeypatch, capsys):
    (tmp_path / "job.yaml").write_text("name: job\n", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "_build_client",
        lambda _args: (_ for _ in ()).throw(AssertionError("API client constructed")),
    )
    args = cli.build_parser().parse_args(
        ["submit-dir", "--config-dir", str(tmp_path), "--dry-run"]
    )

    assert args.func(args) == 0
    assert "job.yaml" in capsys.readouterr().out
