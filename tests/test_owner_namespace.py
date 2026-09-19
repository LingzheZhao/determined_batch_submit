import asyncio

import pytest

from determined_compute.compute_cli import normalize_owner


@pytest.mark.parametrize('value', ['', '   ', 'x' * 257, '中' * 86])
def test_invalid_owner_is_rejected(value):
    with pytest.raises(ValueError):
        normalize_owner(value)


def test_mcp_uses_one_normalized_owner_for_tasks_and_consultations():
    pytest.importorskip('mcp')
    from mcp import Client
    from determined_compute.mcp_server import create_server

    class Service:
        def list_tasks(self, owner):
            return [{'owner': owner}]

    class Workflows:
        def submit(self, question, owner, request_id, context):
            return {'owner': owner, 'workflow_id': 'fixture'}

    async def exercise():
        async with Client(create_server(Service(), ' session-a ', Workflows())) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            for name in ('compute_plan', 'compute_status', 'compute_logs',
                         'compute_list_tasks', 'workflow_status'):
                assert tools[name].annotations.read_only_hint is True
            for name in ('compute_launch', 'compute_cancel', 'compute_reconcile',
                         'compute_consult'):
                assert tools[name].annotations.read_only_hint is False
            assert tools['compute_cancel'].annotations.destructive_hint is True
            tasks = await client.call_tool('compute_list_tasks', {})
            consultation = await client.call_tool('compute_consult', {
                'question': 'fixture', 'request_id': 'fixture',
            })
            assert tasks.structured_content['result'][0]['owner'] == 'session-a'
            assert consultation.structured_content['owner'] == 'session-a'
    asyncio.run(asyncio.wait_for(exercise(), 10))


def test_mcp_rejects_memory_database_before_creating_workflow_files(tmp_path, monkeypatch):
    from determined_compute.mcp_server import _runtime, build_parser
    monkeypatch.chdir(tmp_path)
    args = build_parser().parse_args([
        '--profile', 'unused.yaml', '--owner', 'alice', '--db', ':memory:',
    ])
    with pytest.raises(ValueError, match='persistent local database'):
        _runtime(args)
    assert not (tmp_path / ':memory:').exists()


def test_stateful_cli_rejects_memory_database(tmp_path):
    from determined_compute.compute_cli import _resolve_runtime, build_parser
    profile = tmp_path / 'profile.yaml'
    profile.write_text('mounts:\n- host_path: /shared\n  container_path: /shared\n'
                       'defaults:\n  image: example\n  pool: example\n')
    args = build_parser().parse_args([
        '--profile', str(profile), '--owner', 'alice', '--db', ':memory:', 'list',
    ])
    with pytest.raises(ValueError, match='persistent local database'):
        _resolve_runtime(args)
