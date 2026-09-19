from determined_compute.utils.secrets import default_secrets_path, load_secrets


def test_compute_credentials_environment_variable(tmp_path, monkeypatch):
    path = tmp_path / 'credentials.env'
    path.write_text('DET_MASTER=https://cluster.example\nDET_API_TOKEN=test-value\n')
    monkeypatch.setenv('DETERMINED_COMPUTE_SECRETS', str(path))
    assert default_secrets_path() == path
    assert load_secrets()['DET_MASTER'] == 'https://cluster.example'


def test_default_compute_credentials_filename(tmp_path, monkeypatch):
    monkeypatch.delenv('DETERMINED_COMPUTE_SECRETS', raising=False)
    monkeypatch.chdir(tmp_path)
    assert default_secrets_path() == tmp_path / '.determined_compute.env'
    assert load_secrets() == {}


def test_explicit_credential_file_overrides_environment(tmp_path, monkeypatch):
    path = tmp_path / 'explicit.env'
    path.write_text('DET_USERNAME=example-user\n')
    monkeypatch.setenv('DETERMINED_COMPUTE_SECRETS', str(tmp_path / 'missing.env'))
    assert load_secrets(path) == {'DET_USERNAME': 'example-user'}


def test_quoted_ssh_credentials_are_literal_not_shell_code(tmp_path):
    path = tmp_path / 'credentials.env'
    path.write_text("export SSH_USERNAME='example-user'\nSSH_PASSWORD=\"$literal=$(never-run)#value\"\n")
    assert load_secrets(path) == {
        'SSH_USERNAME': 'example-user',
        'SSH_PASSWORD': '$literal=$(never-run)#value',
    }
